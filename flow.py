"""Stage order, transitions, labels, keyboards and the PO summary text."""
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

# Audit columns (in the POs sheet) written when a stage acts
STAGE_AUDIT = {
    "stock": ("stock_by", "stock_at"),
    "book": ("book_by", "book_at"),
    "fin": ("fin_by", "fin_at"),
    "gm": ("gm_by", "gm_at"),
    "board": ("board_by", "board_at"),
}


def is_urgent(po):
    return str(po.get("urgent", "")).strip().lower() in ("yes", "true", "1")


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
    return "Booked" if stage == STAGE_BOOK else "Approve"


def action_keyboard(stage, po_no):
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


def po_summary(po, items, header=None):
    lines = []
    title = f"PO #{po['po_no']}"
    if is_urgent(po):
        title += "   \u26a1 URGENT"
    lines.append(title)
    if header:
        lines.append(header)
    lines.append("")
    lines.append(f"Requester: {po.get('requester_name', '')}")
    lines.append(f"Supplier: {po.get('supplier', '')}")
    lines.append("Items:")
    for it in items:
        pack = f"  ({it['pack']})" if it.get("pack") else ""
        lines.append(
            f"  \u2022 {it['item']}  \u00d7{it['qty']}{pack}  @ {money(it['unit_price'])}  = {money(it['line_total'])}"
        )
    lines.append(f"Total: {money(po.get('total', 0))}")
    return "\n".join(lines)
