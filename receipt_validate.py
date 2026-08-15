"""Validation for a goods-receipt upload.

Pure functions: no Telegram, no gspread, no I/O. The caller reads the workbook
into raw rows, builds an outstanding-lines map from the PO and its receipts so
far, then calls validate().

Design rules this file enforces:
  * Receipts are EVENTS, not a status. One row per line per lot per delivery.
    A correction is a negative row with a reason, never an edit.
  * The same CML code may legitimately appear on several rows of one file --
    a single line can arrive as two lots with different expiry dates. This is
    the one place the PO validator's duplicate_line rule is deliberately off.
  * An ambiguous date is REJECTED, never guessed. 03/15/2027 and 15/03/2027
    are the same string to different people; a wrong expiry in stock records
    is permanent and invisible.
  * Over-receipt and expired stock CONFIRM rather than block. Refusing to
    record what actually arrived just makes the record disagree with the shelf.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime

SHORT_DATED_DAYS = 90

ST_OK = "ok"
ST_NOT_OUTSTANDING = "not_outstanding"
ST_UNKNOWN_CODE = "unknown_code"
ST_BAD_QTY = "bad_qty"
ST_BAD_INVOICE_QTY = "bad_invoice_qty"
# A missing invoice NUMBER is a header problem, not a bad invoice quantity on
# some row. It used to be logged under bad_invoice_qty, which put the wrong
# cause in the audit record for the commonest blocking mistake there is.
ST_MISSING_INVOICE = "missing_invoice_no"
ST_MISSING_LOT = "missing_lot"
ST_BAD_EXPIRY = "bad_expiry"
ST_AMBIGUOUS_EXPIRY = "ambiguous_expiry"
ST_MISSING_EXPIRY = "missing_expiry"
ST_NEGATIVE_TOTAL = "negative_total"
ST_NOTHING_ENTERED = "nothing_entered"

# Confirmations, not blocks: the receiver ticks a box and the fact is recorded.
ST_OVER_RECEIPT = "over_receipt"
ST_EXPIRED = "expired"
ST_SHORT_DATED = "short_dated"

BLOCKING = {
    ST_NOT_OUTSTANDING, ST_UNKNOWN_CODE, ST_BAD_QTY, ST_BAD_INVOICE_QTY,
    ST_MISSING_INVOICE, ST_MISSING_LOT, ST_BAD_EXPIRY, ST_AMBIGUOUS_EXPIRY,
    ST_MISSING_EXPIRY, ST_NEGATIVE_TOTAL, ST_NOTHING_ENTERED,
}
CONFIRMABLE = {ST_OVER_RECEIPT, ST_EXPIRED, ST_SHORT_DATED}

NA_VALUES = {"n/a", "na", "#n/a", "-", "none", "nil"}

# Unambiguous only. A slash-separated date is refused on purpose.
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY = re.compile(r"^(\d{1,2})[-\s]([A-Za-z]{3,9})[-\s](\d{4})$")
_SLASH = re.compile(r"^\d{1,2}[/.]\d{1,2}[/.]\d{2,4}$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def norm_code(value):
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip().upper()


def norm_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_na(value):
    return norm_text(value).casefold() in NA_VALUES


def to_int(value, allow_negative=False):
    """Whole number, or None. Accepts 4, '4', 4.0, ' 4 '. Rejects 4.5."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            value = float(s.replace(",", ""))
        except ValueError:
            return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    if value < 0 and not allow_negative:
        return None
    return value


def parse_expiry(value):
    """Return (date, error). error is 'ambiguous' | 'bad' | '' .

    A real date object from the spreadsheet is trusted. A string is accepted
    only in ISO or 15-Mar-2027 form. Anything slash-separated is ambiguous by
    construction and refused -- one re-upload is cheap, a silently wrong
    expiry in stock records is not.
    """
    if value is None:
        return None, ""
    if isinstance(value, datetime):
        return value.date(), ""
    if isinstance(value, date):
        return value, ""
    s = norm_text(value)
    if not s:
        return None, ""
    if _SLASH.match(s):
        return None, "ambiguous"
    m = _ISO.match(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), ""
        except ValueError:
            return None, "bad"
    m = _DMY.match(s)
    if m:
        mon = _MONTHS.get(m.group(2)[:3].casefold())
        if not mon:
            return None, "bad"
        try:
            return date(int(m.group(3)), mon, int(m.group(1))), ""
        except ValueError:
            return None, "bad"
    return None, "bad"


@dataclass
class Result:
    receipts: list = field(default_factory=list)
    report: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    @property
    def blocked(self):
        return any(r["status"] in BLOCKING for r in self.report)

    @property
    def needs_confirm(self):
        return any(r["status"] in CONFIRMABLE for r in self.report)


def _row(row_no, raw, status, message, line=None):
    ln = line or {}
    return {
        "row_no": row_no,
        "raw_code": "" if raw.get("code") is None else str(raw.get("code")),
        "raw_qty": "" if raw.get("qty") is None else str(raw.get("qty")),
        "raw_invoice_qty": "" if raw.get("invoice_qty") is None
                           else str(raw.get("invoice_qty")),
        "raw_lot": norm_text(raw.get("lot")),
        "raw_expiry": "" if raw.get("expiry") is None else str(raw.get("expiry")),
        "raw_note": norm_text(raw.get("note")),
        "status": status,
        "message": message,
        "line_id": ln.get("line_id", ""),
        "matched_item": ln.get("item", ""),
    }


def validate(rows, outstanding, invoice_no="", invoice_date=None, today=None,
             confirmed=False):
    """rows: [{'row_no', 'code', 'qty', 'invoice_qty', 'lot', 'expiry', 'note'}]
    outstanding: {code: {'line_id', 'item', 'pack', 'ordered', 'received'}}

    Returns Result. Callers must check .blocked (nothing written) and
    .needs_confirm (ask, then re-call with confirmed=True).
    """
    today = today or date.today()
    report, receipts = [], []
    running = {}          # code -> qty entered so far in THIS file
    populated = 0

    for raw in rows:
        row_no = raw.get("row_no")
        code = norm_code(raw.get("code"))
        qty_raw = raw.get("qty")
        lot = norm_text(raw.get("lot"))
        note = norm_text(raw.get("note"))
        # The code column is PRE-FILLED by the generator, on the spare lot rows
        # too. Only the columns the receiver types in decide whether a row was
        # touched -- otherwise every untouched line blocks as bad_qty.
        has_any = any(norm_text(raw.get(k)) for k in
                      ("qty", "invoice_qty", "lot", "expiry", "note"))
        if not has_any:
            continue
        populated += 1

        if not code:
            report.append(_row(row_no, raw, ST_UNKNOWN_CODE,
                               "Material code is missing on this row."))
            continue

        line = outstanding.get(code)
        if line is None:
            report.append(_row(
                row_no, raw, ST_NOT_OUTSTANDING,
                f"{code} is not an outstanding line on this PO. It may already "
                f"be fully received, or belong to a different PO."))
            continue

        qty = to_int(qty_raw, allow_negative=True)
        if qty is None:
            report.append(_row(row_no, raw, ST_BAD_QTY,
                               "Quantity must be a whole number.", line))
            continue
        if qty == 0 and not note:
            report.append(_row(
                row_no, raw, ST_NOTHING_ENTERED,
                "Quantity is 0. Leave the row blank if nothing came, or add a "
                "note explaining why it is recorded as zero.", line))
            continue

        inv_qty = None
        if norm_text(raw.get("invoice_qty")):
            inv_qty = to_int(raw.get("invoice_qty"), allow_negative=True)
            if inv_qty is None:
                report.append(_row(row_no, raw, ST_BAD_INVOICE_QTY,
                                   "Invoice quantity must be a whole number.",
                                   line))
                continue

        if qty > 0 and not lot:
            report.append(_row(
                row_no, raw, ST_MISSING_LOT,
                "Lot number is required. Enter n/a if this item has no lot.",
                line))
            continue

        exp, err = (None, "") if is_na(raw.get("expiry")) \
            else parse_expiry(raw.get("expiry"))
        if err == "ambiguous":
            report.append(_row(
                row_no, raw, ST_AMBIGUOUS_EXPIRY,
                "Date like 03/15/2027 can be read two ways. Use 15-Mar-2027 "
                "or 2027-03-15.", line))
            continue
        if err == "bad":
            report.append(_row(row_no, raw, ST_BAD_EXPIRY,
                               "Expiry date is not readable. Use 15-Mar-2027.",
                               line))
            continue
        if qty > 0 and exp is None and not is_na(raw.get("expiry")):
            report.append(_row(
                row_no, raw, ST_MISSING_EXPIRY,
                "Expiry date is required. Enter n/a if this item has none.",
                line))
            continue

        running[code] = running.get(code, 0) + qty
        total_after = line["received"] + running[code]
        if total_after < 0:
            report.append(_row(
                row_no, raw, ST_NEGATIVE_TOTAL,
                f"This would make total received {total_after} for "
                f"{line['item']}. A correction cannot go below zero.", line))
            running[code] -= qty
            continue

        entry = {
            "line_id": line["line_id"], "code": code, "item": line["item"],
            "qty_received": qty, "invoice_qty": "" if inv_qty is None else inv_qty,
            "lot_no": lot, "expiry": exp.isoformat() if exp else "n/a",
            "note": note,
        }

        status, message = ST_OK, ""
        if total_after > line["ordered"]:
            status = ST_OVER_RECEIPT
            message = (f"Total received would be {total_after} against "
                       f"{line['ordered']} ordered.")
        elif exp and exp < today:
            status = ST_EXPIRED
            message = f"Already expired ({exp.isoformat()})."
        elif exp and (exp - today).days <= SHORT_DATED_DAYS:
            status = ST_SHORT_DATED
            message = (f"Expires in {(exp - today).days} days "
                       f"({exp.isoformat()}).")

        # Expired stock is worth flagging even when the line is also over --
        # keep both facts on the receipt so the ordering group sees them.
        if exp and exp < today and status == ST_OVER_RECEIPT:
            message += f" Also already expired ({exp.isoformat()})."

        report.append(_row(row_no, raw, status, message, line))
        receipts.append(entry)

    if not norm_text(invoice_no):
        report.append({
            "row_no": None, "raw_code": "", "raw_qty": "", "raw_invoice_qty": "",
            "raw_lot": "", "raw_expiry": "", "raw_note": "",
            "status": ST_MISSING_INVOICE,
            "message": "Invoice number is missing. It is the link between this "
                       "record and the supplier's paperwork.",
            "line_id": "", "matched_item": "",
        })

    report.sort(key=lambda r: (r["row_no"] is None, r["row_no"] or 0))

    blocked = [r for r in report if r["status"] in BLOCKING]
    confirms = [r for r in report if r["status"] in CONFIRMABLE]
    by_line = {}
    for e in receipts:
        by_line[e["code"]] = by_line.get(e["code"], 0) + e["qty_received"]

    lines_after = []
    for code, line in outstanding.items():
        got = by_line.get(code, 0)
        total = line["received"] + got
        lines_after.append({
            "code": code, "line_id": line["line_id"], "item": line["item"],
            "ordered": line["ordered"], "received_before": line["received"],
            "received_now": got, "total": total,
            "state": ("complete" if total == line["ordered"] else
                      "over" if total > line["ordered"] else
                      "partial" if total > 0 else "open"),
        })

    summary = {
        "rows_read": len(rows),
        "rows_populated": populated,
        "rows_ok": len(receipts),
        "rows_blocked": len(blocked),
        "blocked_by_status": {s: sum(1 for r in blocked if r["status"] == s)
                              for s in sorted({r["status"] for r in blocked})},
        "confirm_by_status": {s: sum(1 for r in confirms if r["status"] == s)
                              for s in sorted({r["status"] for r in confirms})},
        "total_units": sum(e["qty_received"] for e in receipts),
        "invoice_no": norm_text(invoice_no),
        "invoice_date": invoice_date.isoformat()
                        if isinstance(invoice_date, (date, datetime))
                        else norm_text(invoice_date),
        "lines_after": lines_after,
        "all_complete": bool(lines_after) and all(
            l["state"] in ("complete", "over") for l in lines_after),
        "confirmed": bool(confirmed),
    }
    return Result(receipts=receipts, report=report, summary=summary)


def outstanding_from(line_items, receipts):
    """Build the outstanding map. Lines already fully received are omitted, so
    the generated file shrinks with each delivery.

    A cancelled quantity is no longer expected, so it is subtracted from what
    is still owed. Without that subtraction the monthly review wrote
    'cancelled' into the sheet and nothing acted on it: the line stayed
    receivable, and because the PO only closes when this map empties, a PO
    whose remainder was cancelled stayed open for ever -- while dropping off
    the review list, which does count cancellations. Open, still receivable,
    and invisible.
    """
    got = {}
    for r in receipts:
        lid = str(r.get("line_id", ""))
        got[lid] = got.get(lid, 0) + (to_int(r.get("qty_received"),
                                             allow_negative=True) or 0)
    out = {}
    for it in line_items:
        lid = str(it.get("line_id", ""))
        ordered = to_int(it.get("qty")) or 0
        cancelled = to_int(it.get("cancelled_qty")) or 0
        # What is still expected. Over-receipt is judged against this too: if
        # 4 of 10 were cancelled, the 7th unit to arrive is one too many.
        expected = max(ordered - cancelled, 0)
        received = got.get(lid, 0)
        if received >= expected:
            continue
        code = norm_code(it.get("material_code"))
        if not code:
            continue
        out[code] = {"line_id": lid, "item": it.get("item", ""),
                     "pack": it.get("pack", ""), "ordered": expected,
                     "ordered_gross": ordered, "cancelled": cancelled,
                     "received": received}
    return out
