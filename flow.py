"""Stage order, transitions, keyboards, and the PO summary text (HTML-formatted)."""
import html
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from clock import local_now
from config import Config

STAGE_STOCK = "stock"
STAGE_BOOK = "book"
STAGE_FIN = "fin"
STAGE_GM = "gm"
STAGE_BOARD = "board"
STAGE_APPROVED = "approved"
STAGE_RECEIVING = "receiving"
STAGE_CLOSED = "closed"
STAGE_RETURNED = "returned"

STAGE_LABEL = {
    "stock": "Stock controller",
    "book": "Bookkeeping",
    "fin": "Finance manager",
    "gm": "General manager",
    "board": "Board of director",
    "approved": "Approved",
    "receiving": "Awaiting delivery",
    "closed": "Closed",
    "returned": "Returned to requester",
}

STAGE_AUDIT = {
    "stock": ("stock_by", "stock_at"),
    "book": ("book_by", "book_at"),
    "fin": ("fin_by", "fin_at"),
    "gm": ("gm_by", "gm_at"),
    "board": ("board_by", "board_at"),
}

# Past-tense verb shown when a stage acts (default "Approved")
ACTION_VERB = {"stock": "Checked", "book": "Booked"}
# Short position tag shown in the action line (only these stages)
POSITION = {"fin": "FM", "gm": "GM", "board": "BoD"}

BOLT = "\u26a1"  # ⚡

# PO categories the requester picks at /new
CATEGORY_LABEL = {"lab": "Laboratory consumption", "other": "Other"}
CATEGORY_CODE = {v.lower(): k for k, v in CATEGORY_LABEL.items()}

# Finance manager's payment route
PAYMENT_LABEL = {"ca": "Cash Advance", "ap": "Account Payable"}


def is_working_time(dt):
    return (dt.weekday() in Config.WORK_DAYS
            and Config.WORK_START_HOUR <= dt.hour < Config.WORK_END_HOUR)


def next_working_moment(dt=None):
    """When this order can actually go to the supplier.

    Inside working hours that is now. Outside them -- evenings, Sundays -- the
    order is recorded for the next working day's opening, because nobody is
    there to send it and dating it 'now' would overstate how promptly it went
    out. Public holidays are NOT handled: Cambodia has many, and the bot has no
    calendar for them, so a holiday will still be treated as a working day.
    """
    dt = dt or local_now()
    if is_working_time(dt):
        return dt, True
    cur = dt
    if dt.weekday() in Config.WORK_DAYS and dt.hour < Config.WORK_START_HOUR:
        return dt.replace(hour=Config.WORK_START_HOUR, minute=0, second=0,
                          microsecond=0), False
    for _ in range(8):
        cur = (cur + timedelta(days=1)).replace(
            hour=Config.WORK_START_HOUR, minute=0, second=0, microsecond=0)
        if cur.weekday() in Config.WORK_DAYS:
            return cur, False
    return dt, False


def order_due_text(dt=None):
    when, now = next_working_moment(dt)
    if now:
        return when.strftime("%d-%b-%Y %H:%M"), "send today"
    return (when.strftime("%d-%b-%Y %H:%M"),
            f"outside working hours \u2014 send {when:%a %d-%b} from "
            f"{Config.WORK_START_HOUR:02d}:00")


def is_urgent(po):
    return str(po.get("urgent", "")).strip().lower() in ("yes", "true", "1")


def action_verb(stage):
    return ACTION_VERB.get(stage, "Approved")


def position(stage):
    return POSITION.get(stage)


def next_stage(stage, urgent):
    """Approval is no longer terminal: an approved PO goes on to be ordered,
    received, and only then closed. Urgent still skips GM/Board APPROVAL --
    they are notified -- but never skips receiving."""
    if stage == STAGE_STOCK:
        return STAGE_BOOK
    if stage == STAGE_BOOK:
        return STAGE_FIN
    if stage == STAGE_FIN:
        return STAGE_APPROVED if urgent else STAGE_GM
    if stage == STAGE_GM:
        return STAGE_BOARD
    if stage == STAGE_BOARD:
        return STAGE_APPROVED
    if stage == STAGE_APPROVED:
        return STAGE_RECEIVING
    if stage == STAGE_RECEIVING:
        return STAGE_CLOSED
    return STAGE_CLOSED


# Stages that hold an approval decision. Receiving is execution, not approval:
# it cannot reject a PO back to the requester, only report a discrepancy, so it
# is deliberately absent here.
APPROVAL_STAGES = (STAGE_STOCK, STAGE_BOOK, STAGE_FIN, STAGE_GM, STAGE_BOARD)


def positive_action(stage):
    return "booked" if stage == STAGE_BOOK else "ok"


def positive_label(stage):
    if stage == STAGE_STOCK:
        return "Checked"
    if stage == STAGE_BOOK:
        return "Booked"
    return "Approve"


def action_keyboard(stage, po_no):
    if stage == STAGE_BOOK:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f4b2 Confirm / update prices",
                                  callback_data=f"bk:price:{po_no}")],
            [InlineKeyboardButton("\u2705 Booked",
                                  callback_data=f"a:book:booked:{po_no}"),
             InlineKeyboardButton("\u274c Reject",
                                  callback_data=f"a:book:no:{po_no}")],
        ])
    if stage == STAGE_STOCK:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f4ca Enter stock on hand",
                                  callback_data=f"sc:ask:{po_no}")],
            [InlineKeyboardButton("\u2705 Checked",
                                  callback_data=f"a:stock:ok:{po_no}"),
             InlineKeyboardButton("\u274c Reject",
                                  callback_data=f"a:stock:no:{po_no}")],
        ])
    if stage == STAGE_FIN:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2705 Approve \u00b7 Cash Advance", callback_data=f"a:fin:ca:{po_no}")],
            [InlineKeyboardButton("\u2705 Approve \u00b7 A/P", callback_data=f"a:fin:ap:{po_no}")],
            [InlineKeyboardButton("\u274c Reject", callback_data=f"a:fin:no:{po_no}")],
        ])
    pos = positive_action(stage)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 " + positive_label(stage), callback_data=f"a:{stage}:{pos}:{po_no}"),
        InlineKeyboardButton("\u274c Reject", callback_data=f"a:{stage}:no:{po_no}"),
    ]])


def money(value):
    try:
        return f"{Config.CURRENCY}{float(value):,.2f}"
    except (TypeError, ValueError):
        return f"{Config.CURRENCY}{value}"


def price_drift(it):
    """Return (ref, unit, pct, flagged) when this line's price differs from its reference,
    else None. pct is None when the reference price is 0 (no percentage possible).
    flagged=True when the change is at/above Config.OTHER_PRICE_TOLERANCE_PCT."""
    try:
        unit = float(it.get("unit_price", 0))
        ref = float(it.get("ref_price"))
    except (TypeError, ValueError):
        return None
    if abs(unit - ref) <= 0.005:
        return None
    pct = ((unit - ref) / ref * 100.0) if ref > 0 else None
    flagged = (pct is None) or (abs(pct) >= Config.OTHER_PRICE_TOLERANCE_PCT)
    return ref, unit, pct, flagged


def _item_line(it, show_prices=True):
    name = html.escape(str(it.get("item", "")))
    pack = f" ({html.escape(str(it['pack']))})" if it.get("pack") else ""
    if not show_prices:
        return f"- {name} \u00d7{it.get('qty', 0)}{pack}"
    s = (f"- {name} \u00d7{it.get('qty', 0)}{pack} "
         f"@ {money(it.get('unit_price', 0))} = {money(it.get('line_total', 0))}")
    d = price_drift(it)
    if d:
        ref, _unit, pct, flagged = d
        if flagged:
            pct_txt = f" ({pct:+.0f}%)" if pct is not None else ""
            s += f"\n   \u26a0\ufe0f price changed: was {money(ref)}{pct_txt}"
        else:
            s += f" (was {money(ref)})"
    vr = str(it.get("variant_reason", "")).strip()
    if vr:
        s += f"\n   \U0001f4dd supplier choice: {html.escape(vr)}"
    return s


def po_summary(po, items, header=None, show_prices=True):
    """HTML-formatted summary posted to the approver groups.
    show_prices=False produces a price-blind card (used for the Stock controller)."""
    e = html.escape
    supplier = e(str(po.get("supplier", "")))
    requester = e(str(po.get("requester_name", "")))

    head = f"PO #{po['po_no']}"
    category = str(po.get("category", "")).strip()
    if category:
        head += f" \u00b7 {e(category)}"
    if header:
        head += f"  {e(header)}"
    if is_urgent(po):
        head += f"  {BOLT} URGENT"

    line2 = f"Requestor: {requester}  |  To: {supplier}"
    ptype = str(po.get("payment_type", "")).strip()
    if ptype:
        line2 += f"  |  {e(ptype)}"
    lines = [head, line2, ""]
    lines += [_item_line(it, show_prices) for it in items]
    if show_prices:
        lines.append(f"<b>Total: {money(po.get('total', 0))}</b>")
    return "\n".join(lines)
