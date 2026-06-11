"""Stage order, transitions, keyboards, and the PO summary text (HTML-formatted)."""
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config import Config

STAGE_STOCK = "stock"
STAGE_BOOK = "book"
STAGE_FIN = "fin"
STAGE_GM = "gm"
STAGE_BOARD = "board"
STAGE_APPROVED = "approved"
STAGE_RETURNED = "returned"

STAGE_LABEL = {
    "stock": "Stock controller",
    "book": "Bookkeeping",
    "fin": "Finance manager",
    "gm": "General manager",
    "board": "Board of director",
    "approved": "Approved",
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


def is_urgent(po):
    return str(po.get("urgent", "")).strip().lower() in ("yes", "true", "1")


def action_verb(stage):
    return ACTION_VERB.get(stage, "Approved")


def position(stage):
    return POSITION.get(stage)


def next_stage(stage, urgent):
    """Return the next stage. Urgent skips GM/Board approval (they are only notified)."""
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
    return STAGE_APPROVED


def positive_action(stage):
    return "booked" if stage == STAGE_BOOK else "ok"


def positive_label(stage):
    if stage == STAGE_STOCK:
        return "Checked"
    if stage == STAGE_BOOK:
        return "Booked"
    return "Approve"


def action_keyboard(stage, po_no):
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


def _item_line(it):
    name = html.escape(str(it.get("item", "")))
    pack = f" ({html.escape(str(it['pack']))})" if it.get("pack") else ""
    return (f"- {name} \u00d7{it.get('qty', 0)}{pack} "
            f"@ {money(it.get('unit_price', 0))} = {money(it.get('line_total', 0))}")


def po_summary(po, items, header=None):
    """HTML-formatted summary posted to the approver groups."""
    e = html.escape
    supplier = e(str(po.get("supplier", "")))
    requester = e(str(po.get("requester_name", "")))
    total = money(po.get("total", 0))

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
    lines += [_item_line(it) for it in items]
    lines.append(f"<b>Total: {total}</b>")
    return "\n".join(lines)
