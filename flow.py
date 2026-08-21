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


# Who may send a PO back to the requester. Rejecting is a spending decision,
# and the two stages below it are not spending roles: the stock controller is
# price-blind and counts the shelf, bookkeeping confirms a price and books it.
# "We already have plenty" belongs in the count, where Finance, the GM and the
# Board can see the number -- not in a veto that removes the evidence and
# quietly moves the decision to someone who cannot see prices.
REJECT_STAGES = (STAGE_FIN, STAGE_GM, STAGE_BOARD)


def can_reject(stage):
    return stage in REJECT_STAGES


# Stages that hold an approval decision, in the order a PO passes through them.
# Receiving is execution, not approval: it cannot reject a PO back to the
# requester, only report a discrepancy, so it is deliberately absent here.
# The ORDER is load-bearing -- rejection_notices slices it to work out which
# stages sit below the one that rejected.
APPROVAL_STAGES = (STAGE_STOCK, STAGE_BOOK, STAGE_FIN, STAGE_GM, STAGE_BOARD)


# What each stage is left holding when a PO it already signed off is rejected.
# This is the whole reason these groups are told at all: a rejection three
# stages up leaves real work behind -- a purchase order sitting in QBO, a shelf
# counted for an order that is not going out, an approval that will have to be
# given again.
REJECT_FOLLOWUP = {
    "stock": ("Your count stays on file. If it comes back you will be asked to "
              "count again, because the quantities may have changed."),
    "book": "You had already booked this one — void the QBO purchase order.",
    "fin": ("You had already approved this one. It re-runs from the Stock "
            "controller, so it will come back to you."),
    "gm": ("You had already approved this one. It re-runs from the Stock "
           "controller, so it will come back to you."),
}
# Told that it happened, but never why. The stock controller is price-blind by
# design, and a rejection reason is very often a price -- "too expensive", "we
# can get it cheaper elsewhere". Handing him the reason would walk a figure
# back into the one seat this whole flow keeps prices out of, through the door
# marked audit trail. Bookkeeping is held to the same rule so there is one
# rule, not an exception someone has to remember. Finance and the GM are not on
# this list: they approve on price and already see every figure.
REASON_BLIND = ("stock", "book")


def rejection_notices(po, po_no, stage, by, reason=""):
    """Who is told that a PO was rejected, and what each of them is told.

    Pure: returns [{'chat', 'with_reason', 'text'}] for the caller to send, so
    the routing rules can be tested without a Telegram client.

    Two rules decide the list, and the ordering group on top of them:

      * BELOW the stage that rejected, never above. A stage above has not seen
        this PO, has nothing to undo, and telling it is noise. The rejecting
        stage itself is excluded too -- it is the one that tapped the button
        and watched its own card change.
      * and only if it ACTUALLY ACTED. In the ordinary run of things that is
        implied by being below, but a sign-off is the honest test of whether
        there is anything to undo, so it is checked rather than assumed.

    Everyone on the list gets the reason except REASON_BLIND.
    """
    head = (f"❌ PO #{po_no} · {po.get('supplier', '')} was rejected "
            f"at {STAGE_LABEL[stage]} by {by}.")
    reason = str(reason or "").strip()
    said = (f"Reason: {reason}" if reason else "No reason was given.")
    out = [{
        "chat": "approved",
        "with_reason": True,
        "text": (head + "\n" + said
                 + "\nIt has gone back to the requester — do not order it."),
    }]
    below = (APPROVAL_STAGES[:APPROVAL_STAGES.index(stage)]
             if stage in APPROVAL_STAGES else ())
    for key in below:
        by_col, at_col = STAGE_AUDIT[key]
        if not str(po.get(by_col, "")).strip():
            continue
        when = str(po.get(at_col, "")).strip()
        tells = key not in REASON_BLIND
        out.append({
            "chat": key,
            "with_reason": tells,
            "text": (head + (f"\nYou handled it on {when}." if when else "")
                     + ("\n" + said if tells else "")
                     + "\n" + REJECT_FOLLOWUP[key]),
        })
    return out


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
                                  callback_data=f"a:book:booked:{po_no}")],
        ])
    if stage == STAGE_STOCK:
        # No buttons at all. Returning the completed count file IS the check,
        # and this stage cannot reject, so there is nothing left to tap.
        return None
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
        # This is the requester's Note column from the template, not a
        # justification of the supplier. It was labelled "supplier choice" back
        # when the bot asked that question separately; it no longer does, and
        # "Reagent out of stock" is plainly a reason to order, not a reason to
        # pick a vendor.
        s += f"\n   \U0001f4dd note: {html.escape(vr)}"
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
