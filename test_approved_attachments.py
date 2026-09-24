"""Supporting documents after approval: the Approved POs and Cash Advance
groups receive them, threaded under the approval copy.

Async, but plain pytest -- asyncio.run(), like test_reject_flow. Sheets,
Telegram and the PDF builder are replaced with recorders; nothing here
touches a network.

What it is guarding:
  * the files hang under the approval copy, never under the order PDF that is
    forwarded to the supplier,
  * the Cash Advance group gets them only when Finance chose cash advance,
  * delivery is recorded per group, and a switched-off group gets nothing.
"""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "1:x")
os.environ.setdefault("SPREADSHEET_ID", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS_JSON", "{}")

import attach_handlers     # noqa: E402
import bot as b            # noqa: E402
import flow                # noqa: E402
from config import Config  # noqa: E402

CHATS = {"stock": -1001, "book": -1002, "fin": -1003, "gm": -1004,
         "board": -1005, "approved": -1006, "cash": -1007}

PO = {
    "po_no": "201", "supplier": "Borey Pharma", "requester_name": "Sophea",
    "requester_id": "555", "stage": "board", "status": "active",
    "urgent": "no", "total": "320.00", "category": "Other",
    "payment_type": flow.PAYMENT_LABEL["ca"],
}
FILES = [
    {"po_no": "201", "seq": 1, "file_name": "quotation.pdf", "file_id": "F1",
     "sha256": "a" * 64},
    {"po_no": "201", "seq": 2, "file_name": "spec.pdf", "file_id": "F2",
     "sha256": "b" * 64},
]


class FakeSheets:
    def __init__(self, po):
        self.po, self.delivered = dict(po), []

    def get_po(self, po_no):
        return dict(self.po)

    def update_po(self, po_no, **fields):
        self.po.update({k: str(v) for k, v in fields.items()})
        return True

    def get_line_items(self, po_no):
        return []

    def get_attachments(self, po_no):
        return [dict(f) for f in FILES]

    def mark_delivered(self, po_no, stage):
        self.delivered.append(stage)
        return len(FILES)


class Msg:
    def __init__(self, mid):
        self.message_id = mid


class FakeBot:
    def __init__(self):
        self.docs = []   # dicts: chat, filename, document, reply_to, mid

    async def send_document(self, chat_id, document, filename=None,
                            reply_parameters=None, **k):
        mid = 100 + len(self.docs)
        self.docs.append({
            "chat": chat_id, "filename": filename, "document": document,
            "reply_to": reply_parameters.message_id if reply_parameters else None,
            "mid": mid})
        return Msg(mid)

    async def send_message(self, chat_id, text, **k):
        return Msg(0)

    def to(self, chat):
        return [d for d in self.docs if d["chat"] == chat]


class Ctx:
    def __init__(self):
        self.bot = FakeBot()


def _run(po=None, stages=None):
    Config.CHAT_IDS.update(CHATS)
    saved = Config.ATTACH_STAGES_RAW
    Config.ATTACH_STAGES_RAW = stages or ["book", "fin", "gm", "board",
                                          "approved", "cash"]
    fake = FakeSheets(po or PO)
    patched = {(b, "sheets"): fake, (attach_handlers, "sheets"): fake,
               (b, "generate_po_pdf"): lambda *a, **k: b"%PDF-fake"}

    async def no_alternatives(items, show_prices=True):
        return []
    patched[(b, "_alternatives")] = no_alternatives
    originals = {key: getattr(*key) for key in patched}
    for (mod, name), value in patched.items():
        setattr(mod, name, value)
    ctx = Ctx()
    try:
        asyncio.run(b.finalize(ctx, "201"))
    finally:
        Config.ATTACH_STAGES_RAW = saved
        for (mod, name), value in originals.items():
            setattr(mod, name, value)
    return fake, ctx


def test_the_approved_group_gets_the_files_as_replies_to_the_approval_copy():
    _, ctx = _run()
    sent = ctx.bot.to(CHATS["approved"])
    assert [d["filename"] for d in sent] == [
        "PO_201.pdf", "PO_201_approval.pdf", "quotation.pdf", "spec.pdf"]
    approval_mid = sent[1]["mid"]
    assert [d["reply_to"] for d in sent[2:]] == [approval_mid, approval_mid]


def test_no_file_ever_hangs_under_the_order_that_goes_to_the_supplier():
    _, ctx = _run()
    order_mid = ctx.bot.to(CHATS["approved"])[0]["mid"]
    assert all(d["reply_to"] != order_mid for d in ctx.bot.docs)


def test_files_are_resent_by_telegram_file_id_not_reuploaded():
    _, ctx = _run()
    files = [d for d in ctx.bot.to(CHATS["approved"]) if d["reply_to"]]
    assert [d["document"] for d in files] == ["F1", "F2"]


def test_the_cash_group_gets_the_files_under_its_po_card():
    _, ctx = _run()
    sent = ctx.bot.to(CHATS["cash"])
    assert sent[0]["filename"] == "PO_201_cash.pdf"
    assert [d["filename"] for d in sent[1:]] == ["quotation.pdf", "spec.pdf"]
    assert all(d["reply_to"] == sent[0]["mid"] for d in sent[1:])


def test_the_cash_group_hears_nothing_when_finance_chose_ap():
    po = dict(PO, payment_type=flow.PAYMENT_LABEL["ap"])
    _, ctx = _run(po)
    assert ctx.bot.to(CHATS["cash"]) == []
    assert len(ctx.bot.to(CHATS["approved"])) == 4


def test_delivery_is_recorded_for_each_group_that_received_the_files():
    fake, _ = _run()
    assert fake.delivered == ["approved", "cash"]


def test_a_group_switched_off_in_attach_stages_gets_the_pdfs_but_no_files():
    fake, ctx = _run(stages=["book", "fin", "gm", "board"])
    assert [d["filename"] for d in ctx.bot.to(CHATS["approved"])] == [
        "PO_201.pdf", "PO_201_approval.pdf"]
    assert [d["filename"] for d in ctx.bot.to(CHATS["cash"])] == [
        "PO_201_cash.pdf"]
    assert fake.delivered == []


def test_the_stock_controller_is_still_refused_whatever_the_config_says():
    fake, ctx = _run(stages=["stock", "approved"])
    assert ctx.bot.to(CHATS["stock"]) == []
    assert fake.delivered == ["approved"]
