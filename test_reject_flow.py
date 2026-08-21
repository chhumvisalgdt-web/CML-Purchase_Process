"""End-to-end test of the rejection path: one tap, and who hears what.

Async, but plain pytest -- asyncio.run() rather than pytest-asyncio, so the
suite keeps its "pip install -r requirements.txt pytest" contract. Google
Sheets and Telegram are replaced with recorders; nothing here touches a
network.

What it is guarding, in order of how much it would cost to get wrong:
  * the reason never reaches the price-blind stages,
  * the buttons are gone the moment Reject is tapped,
  * the PO is returned in that same moment, not when a reason arrives,
  * a reason typed after the requester has already resubmitted is refused.
"""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "1:x")
os.environ.setdefault("SPREADSHEET_ID", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS_JSON", "{}")

import bot as b            # noqa: E402
import flow                # noqa: E402
from config import Config  # noqa: E402

CHATS = {"stock": -1001, "book": -1002, "fin": -1003, "gm": -1004,
         "board": -1005, "approved": -1006, "cash": -1007}
REQUESTER_ID = 555
SECRET = "too expensive, 40% cheaper at Borey Pharma"

# Sitting at the Board, so on the non-urgent path every stage below it has
# signed off: stock counted, bookkeeping booked, Finance and the GM approved.
PO = {
    "po_no": "187", "supplier": "BIO-TECHEM(CAM)", "requester_name": "Sophea",
    "requester_id": str(REQUESTER_ID), "stage": "board", "status": "active",
    "urgent": "no", "total": "1240.00",
    "stock_by": "Dara", "stock_at": "19-Aug-2026 09:12",
    "book_by": "Lina", "book_at": "20-Aug-2026 14:40",
    "fin_by": "Mr. Sok", "fin_at": "21-Aug-2026 10:02",
    "gm_by": "Mr. Ny", "gm_at": "21-Aug-2026 11:30",
}


class FakeSheets:
    def __init__(self, po):
        self.po = dict(po)
        self.writes = []

    def get_po(self, po_no):
        return dict(self.po)

    def update_po(self, po_no, **fields):
        self.writes.append(fields)
        self.po.update({k: str(v) for k, v in fields.items()})
        return True


class FakeBot:
    def __init__(self):
        self.sent = []          # (chat_id, text, has_buttons)

    async def send_message(self, chat_id, text, reply_markup=None, **k):
        self.sent.append((chat_id, text, reply_markup is not None))
        return FakeMsg(mid=900 + len(self.sent), text=text)

    def to(self, chat_id):
        return [t for cid, t, _ in self.sent if cid == chat_id]


class FakeMsg:
    def __init__(self, mid=1, text="PO #187 card", chat_id=CHATS["board"]):
        self.message_id, self.chat_id = mid, chat_id
        self.caption = self.caption_html = None
        self.text = self.text_html = text
        self.replies = []
        self.reply_to_message = None

    async def reply_text(self, text, **k):
        m = FakeMsg(mid=self.message_id + 100, text=text,
                    chat_id=self.chat_id)
        self.replies.append(m)
        return m


class FakeUser:
    def __init__(self, uid=7, name="Mr. Chan"):
        self.id, self.full_name, self.username = uid, name, None


class FakeQuery:
    def __init__(self, user, message):
        self.from_user, self.message = user, message
        self.alerts, self.card = [], None
        # Editing a message without passing reply_markup is what removes the
        # inline keyboard, per the Bot API. Model that, so a change that stops
        # clearing the buttons fails here.
        self.buttons_live = True

    async def answer(self, text=None, show_alert=False):
        if text:
            self.alerts.append(text)

    async def edit_message_text(self, text, **k):
        self.card, self.buttons_live = text, False

    async def edit_message_caption(self, caption, **k):
        self.card, self.buttons_live = caption, False

    async def edit_message_reply_markup(self, reply_markup=None):
        self.buttons_live = reply_markup is not None


class FakeContext:
    def __init__(self):
        self.bot, self.chat_data, self.user_data = FakeBot(), {}, {}


class FakeUpdate:
    """A group text message, as _maybe_capture_reason sees it."""
    def __init__(self, user, text, reply_to=None, chat_id=CHATS["board"]):
        self.effective_user = user
        self.message = FakeMsg(mid=500, text=text, chat_id=chat_id)
        self.message.reply_to_message = reply_to
        self.effective_chat = type("C", (), {"id": chat_id, "type": "group"})()


def _wire(po=None):
    Config.CHAT_IDS.update(CHATS)
    fake = FakeSheets(po or PO)
    b.sheets = fake
    return fake


def _tap_reject(po=None, stage="board"):
    """Tap the Reject button. Returns (sheets, query, context)."""
    fake = _wire(po)
    ctx, user = FakeContext(), FakeUser()
    q = FakeQuery(user, FakeMsg())
    asyncio.run(b._cb_action(q, ctx, f"a:{stage}:no:187"))
    return fake, q, ctx


# ---- the tap itself ----

def test_the_buttons_are_gone_the_moment_reject_is_tapped():
    _, q, _ = _tap_reject()
    assert q.buttons_live is False
    assert "Rejected by Mr. Chan" in q.card


def test_the_po_is_returned_immediately_not_when_a_reason_arrives():
    fake, _, _ = _tap_reject()
    assert fake.po["stage"] == flow.STAGE_RETURNED
    assert fake.po["status"] == "returned"
    assert fake.po["reject_stage"] == "board"
    assert fake.po["reject_reason"] == ""


# ---- who hears what ----

def test_the_requester_is_dmed_with_the_resubmit_button():
    _, _, ctx = _tap_reject()
    dms = [(t, kb) for cid, t, kb in ctx.bot.sent if cid == REQUESTER_ID]
    assert len(dms) == 1
    text, has_button = dms[0]
    assert "No reason was given." in text and has_button


def test_the_ordering_group_and_every_stage_below_are_told():
    _, _, ctx = _tap_reject()
    told = {cid for cid, _, _ in ctx.bot.sent}
    for key in ("approved", "stock", "book", "fin", "gm"):
        assert CHATS[key] in told, f"{key} should have been told"
    assert CHATS["cash"] not in told


def test_finance_gets_the_reason_when_the_board_rejects():
    """The FM is the one who chases approvals, and /pending cannot show a
    returned PO -- this message is the only way Finance finds out."""
    _, q, ctx = _tap_reject()
    _give_reason(ctx, q, SECRET)
    assert any(SECRET in t for t in ctx.bot.to(CHATS["fin"]))


def test_stages_above_the_rejecter_are_never_told():
    po = dict(PO, stage="fin")
    _, _, ctx = _tap_reject(po, stage="fin")
    told = {cid for cid, _, _ in ctx.bot.sent}
    assert CHATS["gm"] not in told and CHATS["board"] not in told


def test_a_stage_that_never_acted_is_not_told():
    po = dict(PO, stage="fin", book_by="", book_at="")
    _, _, ctx = _tap_reject(po, stage="fin")
    told = {cid for cid, _, _ in ctx.bot.sent}
    assert CHATS["stock"] in told and CHATS["book"] not in told


# ---- the late reason ----

def _give_reason(ctx, q, text, user=None, reply=True):
    prompt = q.message.replies[-1]
    upd = FakeUpdate(user or q.from_user, text,
                     reply_to=prompt if reply else None)
    asyncio.run(b._maybe_capture_reason(upd, ctx))
    return upd


def test_a_late_reason_is_recorded_and_forwarded():
    fake, q, ctx = _tap_reject()
    before = len(ctx.bot.sent)
    _give_reason(ctx, q, SECRET)
    assert fake.po["reject_reason"] == SECRET
    fresh = ctx.bot.sent[before:]
    assert any(cid == REQUESTER_ID and SECRET in t for cid, t, _ in fresh)
    assert any(cid == CHATS["approved"] and SECRET in t for cid, t, _ in fresh)


def test_the_reason_never_reaches_the_price_blind_stages():
    """The whole flow keeps prices away from the stock controller. A rejection
    reason is very often a price, so it must not arrive by this door either."""
    _, q, ctx = _tap_reject()
    _give_reason(ctx, q, SECRET)
    for key in ("stock", "book"):
        for text in ctx.bot.to(CHATS[key]):
            assert SECRET not in text


def test_a_reason_after_the_requester_resubmitted_is_refused():
    """By then the PO is active again at the Stock controller; writing a reason
    onto it would label a live PO as rejected."""
    fake, q, ctx = _tap_reject()
    fake.po.update(status="active", stage="stock", reject_stage="")
    upd = _give_reason(ctx, q, SECRET)
    assert fake.po.get("reject_reason") == ""
    assert "already moved on" in upd.message.replies[-1].text


def test_a_one_character_reason_is_not_stored():
    fake, q, ctx = _tap_reject()
    _give_reason(ctx, q, "x")
    assert fake.po["reject_reason"] == ""


def test_giving_no_reason_at_all_leaves_the_rejection_standing():
    fake, _, _ = _tap_reject()
    assert fake.po["status"] == "returned" and fake.po["reject_reason"] == ""
