"""Validation for the CML purchase-order Excel upload.

Pure functions: no Telegram, no gspread, no I/O. The caller reads the workbook
into raw rows, calls build_index() over the two master tabs, then validate().

Design rules this file enforces:
  * Material Code is the only key. Item names never resolve anything.
  * A code that appears more than once across the master tabs is UNUSABLE, not
    guessable -- it blocks rather than resolving to whichever row came first.
  * The requester is price-blind. Nothing returned for display to her carries a
    price; cheaper-elsewhere is reported as item names and a count only.
  * Supplier and category are DERIVED from the codes, never taken from the file.
  * All-or-nothing: if any row is blocking, the caller must not submit anything.
"""
import re
from dataclasses import dataclass, field

MAX_LINES = 12

CAT_REAGENT = "reagent"
CAT_OTHER = "other"

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_DUPLICATE_CODE = "duplicate_code"
STATUS_NO_PRICE = "no_price"
STATUS_BAD_QTY = "bad_qty"
STATUS_MISSING_CODE = "missing_code"
STATUS_DUPLICATE_LINE = "duplicate_line"
STATUS_OVER_LIMIT = "over_limit"
STATUS_SUPPLIER_CONFLICT = "supplier_conflict"
STATUS_MIXED_CATEGORY = "mixed_category"

BLOCKING = {
    STATUS_NOT_FOUND, STATUS_DUPLICATE_CODE, STATUS_NO_PRICE, STATUS_BAD_QTY,
    STATUS_MISSING_CODE, STATUS_DUPLICATE_LINE, STATUS_OVER_LIMIT,
    STATUS_SUPPLIER_CONFLICT, STATUS_MIXED_CATEGORY,
}

CATEGORY_LABEL = {CAT_REAGENT: "Laboratory consumption", CAT_OTHER: "Other"}


def norm_code(value):
    """Normalise a material code from either side of the comparison.

    Google Sheets hands back '11533' as text; Excel hands back the integer 11533
    (or 11533.0). Without this the numeric-coded half of the master list would
    silently fail to match while the alphanumeric half worked.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip().upper()


def norm_name(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def to_float(value):
    if value is None:
        return None
    s = str(value).replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_qty(value):
    """Whole number > 0, or None. Accepts 4, '4', 4.0, ' 4 '. Rejects 4.5."""
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
    if not isinstance(value, int) or value <= 0:
        return None
    return value


@dataclass
class MasterIndex:
    by_code: dict = field(default_factory=dict)
    dup_codes: set = field(default_factory=set)
    best_by_item: dict = field(default_factory=dict)

    def suppliers(self, category=None):
        out, seen = [], set()
        for row in self.by_code.values():
            if category and row["category"] != category:
                continue
            s = row["supplier"]
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return sorted(out)

    def rows_for(self, category, supplier):
        """Rows a template for this category+supplier may offer. Codes that are
        duplicated anywhere in the master are excluded: unusable, not guessable."""
        return [r for r in self.by_code.values()
                if r["category"] == category and r["supplier"] == supplier]


def build_index(reagent_rows, other_rows):
    """reagent_rows / other_rows: dicts with material_code, item, supplier,
    unit_price, and optionally pack and supplier_reagent."""
    by_code, dup, first_seen = {}, set(), {}
    best = {}
    for rows, category in ((reagent_rows, CAT_REAGENT), (other_rows, CAT_OTHER)):
        for raw in rows:
            code = norm_code(raw.get("material_code"))
            if not code:
                continue
            row = {
                "material_code": code,
                "raw_code": str(raw.get("material_code", "")).strip(),
                "item": str(raw.get("item", "")).strip(),
                "supplier": str(raw.get("supplier", "")).strip(),
                "supplier_reagent": str(raw.get("supplier_reagent", "")).strip(),
                "pack": str(raw.get("pack", "")).strip(),
                "unit_price": to_float(raw.get("unit_price")),
                "category": category,
            }
            if code in first_seen:
                dup.add(code)
                by_code.pop(code, None)
                continue
            first_seen[code] = row
            by_code[code] = row

            price = row["unit_price"]
            if price is not None and price > 0 and row["item"]:
                key = norm_name(row["item"])
                cur = best.setdefault(key, {})
                sup = row["supplier"]
                if sup and (sup not in cur or price < cur[sup]):
                    cur[sup] = price
    return MasterIndex(by_code=by_code, dup_codes=dup, best_by_item=best)


@dataclass
class Result:
    items: list = field(default_factory=list)
    report: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    @property
    def blocked(self):
        return any(r["status"] in BLOCKING for r in self.report)


def _report_row(row_no, raw, status, message, matched=None):
    m = matched or {}
    return {
        "row_no": row_no,
        "raw_code": "" if raw.get("code") is None else str(raw.get("code")),
        "raw_qty": "" if raw.get("qty") is None else str(raw.get("qty")),
        "raw_note": "" if raw.get("note") is None else str(raw.get("note")),
        "status": status,
        "message": message,
        "matched_code": m.get("material_code", ""),
        "matched_item": m.get("item", ""),
        "matched_supplier": m.get("supplier", ""),
        "matched_pack": m.get("pack", ""),
    }


def validate(rows, index, max_lines=MAX_LINES, template_supplier=None,
             template_category=None):
    """rows: [{'row_no': int, 'code': raw, 'qty': raw, 'note': raw}, ...] --
    every row the reader saw, in sheet order, blanks included.

    Returns Result. Callers must check .blocked and submit nothing when True.
    """
    report, resolved = [], []
    seen_codes = {}
    populated = 0

    for raw in rows:
        row_no = raw.get("row_no")
        code = norm_code(raw.get("code"))
        qty_raw = raw.get("qty")
        note = re.sub(r"\s+", " ", str(raw.get("note") or "")).strip()
        has_qty = to_qty(qty_raw) is not None or str(qty_raw or "").strip() != ""

        if not code and not has_qty and not note:
            continue
        populated += 1

        if populated > max_lines:
            report.append(_report_row(
                row_no, raw, STATUS_OVER_LIMIT,
                f"A PO can hold at most {max_lines} lines. Move this line to a "
                f"second file."))
            continue

        if not code:
            report.append(_report_row(
                row_no, raw, STATUS_MISSING_CODE,
                "Material code is missing. Clear the whole row, or enter a code."))
            continue

        if code in index.dup_codes:
            report.append(_report_row(
                row_no, raw, STATUS_DUPLICATE_CODE,
                f"Code {code} appears more than once in the master list, so it "
                f"cannot be ordered until that is corrected."))
            continue

        master = index.by_code.get(code)
        if master is None:
            report.append(_report_row(
                row_no, raw, STATUS_NOT_FOUND,
                f"Code {code} is not in this template. Check the code, or you may "
                f"be using the wrong template."))
            continue

        if code in seen_codes:
            report.append(_report_row(
                row_no, raw, STATUS_DUPLICATE_LINE,
                f"Code {code} is already on row {seen_codes[code]}. Combine the "
                f"quantities into one line.", master))
            continue

        price = master["unit_price"]
        if price is None or price <= 0:
            report.append(_report_row(
                row_no, raw, STATUS_NO_PRICE,
                f"{master['item']} has no usable price in the master list, so it "
                f"cannot be ordered until that is corrected.", master))
            continue

        qty = to_qty(qty_raw)
        if qty is None:
            report.append(_report_row(
                row_no, raw, STATUS_BAD_QTY,
                "Quantity must be a whole number of 1 or more.", master))
            continue

        seen_codes[code] = row_no
        resolved.append((row_no, raw, master, qty, note))

    anchor = resolved[0][2] if resolved else None
    items = []
    for row_no, raw, master, qty, note in resolved:
        if anchor and master["category"] != anchor["category"]:
            report.append(_report_row(
                row_no, raw, STATUS_MIXED_CATEGORY,
                f"{master['item']} is on the "
                f"{CATEGORY_LABEL[master['category']]} list, but this order is "
                f"{CATEGORY_LABEL[anchor['category']]}. One list per PO.", master))
            continue
        if anchor and master["supplier"] != anchor["supplier"]:
            report.append(_report_row(
                row_no, raw, STATUS_SUPPLIER_CONFLICT,
                f"{master['item']} is supplied by {master['supplier']}, but this "
                f"order is for {anchor['supplier']}. One supplier per PO.", master))
            continue

        price = master["unit_price"]
        items.append({
            "item": master["item"],
            "qty": qty,
            "unit_price": price,
            "line_total": round(price * qty, 2),
            "ref_price": price,
            "variant_reason": note,
            "material_code": master["raw_code"],
            "supplier_reagent": master["supplier_reagent"],
            "pack": master["pack"],
        })
        report.append(_report_row(row_no, raw, STATUS_OK, "", master))

    report.sort(key=lambda r: (r["row_no"] is None, r["row_no"]))

    cheaper = []
    if anchor:
        for master_item in {i["item"] for i in items}:
            by_sup = index.best_by_item.get(norm_name(master_item), {})
            mine = by_sup.get(anchor["supplier"])
            if mine is None:
                continue
            if any(p < mine - 0.005 for s, p in by_sup.items()
                   if s != anchor["supplier"]):
                cheaper.append(master_item)

    blocked = [r for r in report if r["status"] in BLOCKING]
    summary = {
        "rows_read": len(rows),
        "rows_populated": populated,
        "rows_ok": len(items),
        "rows_blocked": len(blocked),
        "blocked_by_status": {s: sum(1 for r in blocked if r["status"] == s)
                              for s in sorted({r["status"] for r in blocked})},
        "total_units": sum(i["qty"] for i in items),
        "supplier": anchor["supplier"] if anchor else "",
        "category": anchor["category"] if anchor else "",
        "category_label": CATEGORY_LABEL.get(anchor["category"], "") if anchor else "",
        "cheaper_elsewhere": sorted(cheaper),
        "template_supplier": template_supplier or "",
        "template_category": template_category or "",
        "template_matches": (
            (template_supplier is None or not anchor
             or template_supplier == anchor["supplier"])
            and (template_category is None or not anchor
                 or template_category == anchor["category"])
        ),
    }
    return Result(items=items, report=report, summary=summary)
