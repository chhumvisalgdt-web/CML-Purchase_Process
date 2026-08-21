"""Validation for the CML purchase-order Excel upload.

Pure functions: no Telegram, no gspread, no I/O. The caller reads the workbook
into raw rows, calls build_index() over the two master tabs, then validate().

Design rules this file enforces:
  * The requester identifies an item by NAME, inside one supplier's list. The
    Material Code is still the master's own key and still resolves -- templates
    issued before v2.0 carry codes in that same cell -- but nothing asks her
    for one.
  * A code that appears more than once across the master tabs is UNUSABLE, not
    guessable -- it blocks rather than resolving to whichever row came first.
    A name repeated inside ONE supplier's list is unusable for the same reason
    and by the same rule: it never resolves to whichever pack came first.
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
# Same wire value on purpose: the cell changed from a code to a name, the
# fault did not, and Upload_Rows history stays comparable across the change.
STATUS_MISSING_ITEM = STATUS_MISSING_CODE
STATUS_DUPLICATE_LINE = "duplicate_line"
STATUS_OVER_LIMIT = "over_limit"
STATUS_SUPPLIER_CONFLICT = "supplier_conflict"
STATUS_MIXED_CATEGORY = "mixed_category"
# The form now asks for the item by NAME, so a name can fail in ways a code
# never could: it can be spelt for a list that does not exist, or it can be
# carried by two rows of one supplier's list at once.
STATUS_AMBIGUOUS_NAME = "ambiguous_name"
STATUS_UNKNOWN_LIST = "unknown_list"

BLOCKING = {
    STATUS_NOT_FOUND, STATUS_DUPLICATE_CODE, STATUS_NO_PRICE, STATUS_BAD_QTY,
    STATUS_MISSING_CODE, STATUS_DUPLICATE_LINE, STATUS_OVER_LIMIT,
    STATUS_SUPPLIER_CONFLICT, STATUS_MIXED_CATEGORY, STATUS_AMBIGUOUS_NAME,
    STATUS_UNKNOWN_LIST,
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
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


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


def to_units(value):
    """Tests (or mL, or pieces) in one pack. Positive number, or None.

    None is not 0 and must never become 0: it means 'nobody has said', and the
    only honest thing to do with an unknown pack size is decline to compare.
    """
    v = to_float(value)
    if v is None or v <= 0:
        return None
    return v


# A pack whose size is stated plainly: "25 Tests", "50/Kit", "25T/Kit",
# "100pcs/box", "150 test". Deliberately NOT multi-component packs like
# "1x800+1x200mL" or "4x40+4x10mL" -- the leading 1 or 4 there is a number of
# bottles, not a number of tests, and reading it as a divisor would produce a
# confident, wrong percentage.
_SIMPLE_PACK = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:t\b|test|tests|pcs|"
                          r"papers?|kit|unit|units)?\s*[/]?\s*"
                          r"(?:kit|box|bag|pack|unit|test|tests)?\s*$",
                          re.IGNORECASE)


def units_from_pack(pack):
    """Tests per pack read from the Pack text, or None when it is not obvious.

    Only unambiguous shapes are accepted. Anything containing 'x' or '+' is
    refused outright: a wrong divisor is worse than no divisor, because the
    first prints a percentage nobody can tell is nonsense and the second
    prints a dash.
    """
    s = re.sub(r"\s+", " ", str(pack or "")).strip()
    if not s or "x" in s.lower() or "+" in s:
        return None
    m = _SIMPLE_PACK.match(s)
    if not m:
        return None
    return to_units(m.group(1))


def per_unit(price, units):
    """Price for one test. None when either side is unknown -- comparing a
    25-test box with a 50-test kit on headline price alone points at the wrong
    supplier, which is the whole reason this exists."""
    if price is None or units is None or units <= 0:
        return None
    return price / units


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
    # code -> {(category, supplier)} for every row a duplicated code appeared
    # on. Duplicates are dropped from by_code, so without this the template can
    # only report how many codes are duplicated ANYWHERE -- which told a
    # requester ordering from one supplier that 12 items were unavailable when
    # none of them were his.
    dup_where: dict = field(default_factory=dict)

    # (category, supplier, normalised name) -> the one row that name identifies
    # inside that supplier's list.
    #
    # The supplier is part of the key because names are deliberately NOT unique
    # across suppliers: two rows are DECLARED equivalent precisely by carrying
    # the same CML Reagent name, which is what feeds "Also sold by" and the
    # alternatives table. A name is therefore only ever an identifier within
    # one supplier's list, never on its own.
    by_name: dict = field(default_factory=dict)
    # Keys whose name appears more than once inside ONE supplier's list --
    # usually the same product in two pack sizes. Such a name cannot identify
    # one row, so it is dropped from by_name exactly as a duplicated code is
    # dropped from by_code: unusable, not guessable. Maps to the name as
    # written, for reporting.
    dup_names: dict = field(default_factory=dict)

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

    def equivalents(self, code):
        """Master rows that are the same thing as `code`, from a DIFFERENT
        supplier.

        Two rows are the same item when their CML Reagent name matches exactly
        (case and spacing ignored). That column is CML's own name, under CML's
        control, which makes it the one field that can be made to line up --
        unlike Supplier Reagent, which is the supplier's wording and changes
        when they rebrand. Nothing is guessed: near-misses stay separate, so
        'DENGUE NS1 Ag' and 'DENGUE NS1 AG FIA' -- different platforms at very
        different prices -- are never treated as one.

        The consequence is that equivalence is declared by NAMING, not by a
        separate column: two suppliers' rows only pair up once someone gives
        them the same CML Reagent name.
        """
        mine = self.by_code.get(norm_code(code))
        if not mine:
            return []
        key = norm_name(mine["item"])
        if not key:
            return []
        return [r for r in self.by_code.values()
                if norm_name(r["item"]) == key
                and r["supplier"] != mine["supplier"]]

    def excluded_for(self, category, supplier):
        """How many duplicated codes would have belonged on THIS template.
        What the requester needs to know is what is missing from the file in
        front of her, not the size of the master list's problem."""
        return sum(1 for where in self.dup_where.values()
                   if (category, supplier) in where)

    def item(self, category, supplier, name):
        """The one row a name identifies inside one supplier's list, or None."""
        return self.by_name.get((category, supplier, norm_name(name)))

    def name_is_ambiguous(self, category, supplier, name):
        return (category, supplier, norm_name(name)) in self.dup_names

    def has_scope(self, category, supplier):
        """Whether this category+supplier names a list that actually exists.
        A form that identifies its rows by name is only meaningful inside one
        such list, so an unknown pair has to block rather than fall back."""
        return any(c == category and s == supplier for c, s, _n in self.by_name)

    def excluded_names_for(self, category, supplier):
        return sum(1 for c, s, _n in self.dup_names
                   if (c, s) == (category, supplier))

    def clashing_names(self):
        """[(supplier, name)] for every name that cannot identify one item
        inside its own supplier's list. Feeds /mastercheck.

        Names repeated ACROSS suppliers are absent on purpose: that is not a
        fault, it is how equivalence is declared.
        """
        return sorted({(s, written)
                       for (_c, s, _n), written in self.dup_names.items()})


def build_index(reagent_rows, other_rows):
    """reagent_rows / other_rows: dicts with material_code, item, supplier,
    unit_price, and optionally pack and supplier_reagent."""
    by_code, dup, first_seen = {}, set(), {}
    best, dup_where = {}, {}
    by_name, dup_names = {}, {}
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
                # How many tests are in one pack -- the divisor that makes a
                # 25-box and a 50-kit comparable. Read from the optional
                # 'Tests per pack' column, falling back to the Pack text when
                # that is unambiguous.
                "tests_per_pack": (to_units(raw.get("tests_per_pack"))
                                   or units_from_pack(raw.get("pack"))),
            }
            if code in first_seen:
                dup.add(code)
                by_code.pop(code, None)
                seen = first_seen[code]
                # The row that claimed this code FIRST is unusable too, and it
                # was indexed by name before the collision came to light. Drop
                # it from there as well, or an unusable row stays reachable by
                # the one route the requester's form actually uses -- usable by
                # name, unorderable by code, which is the worst of both.
                by_name.pop((seen["category"], seen["supplier"],
                             norm_name(seen["item"])), None)
                # Remember every place this code claimed to live -- the first
                # row's home as well as this one -- so a template can say how
                # many of ITS items went missing.
                dup_where.setdefault(code, set()).add(
                    (seen["category"], seen["supplier"]))
                dup_where[code].add((category, row["supplier"]))
                continue
            first_seen[code] = row
            by_code[code] = row

            # Name index, scoped to one supplier's list. A row whose CODE is
            # duplicated never reaches here: it is unusable, and an unusable
            # row must not be reachable by a second route either.
            nkey = (category, row["supplier"], norm_name(row["item"]))
            if nkey[2]:
                if nkey in by_name or nkey in dup_names:
                    dup_names[nkey] = by_name.get(nkey, {}).get(
                        "item", row["item"]) or row["item"]
                    by_name.pop(nkey, None)
                else:
                    by_name[nkey] = row

            price = row["unit_price"]
            if price is not None and price > 0 and row["item"]:
                key = norm_name(row["item"])
                cur = best.setdefault(key, {})
                sup = row["supplier"]
                if sup and (sup not in cur or price < cur[sup]):
                    cur[sup] = price
    return MasterIndex(by_code=by_code, dup_codes=dup, best_by_item=best,
                       dup_where=dup_where, by_name=by_name,
                       dup_names=dup_names)


def cheaper_alternative(code, index):
    """The single best equivalent from another supplier, judged per test.

    Returns (supplier, diff_pct). diff_pct is None when a pack size is missing
    on either side -- the supplier is still named, because "someone else sells
    this" is worth knowing even when the size of the gap is not calculable.
    (None, None) when no equivalent is registered at all.
    """
    mine = index.by_code.get(norm_code(code))
    if not mine:
        return None, None
    our_per = per_unit(mine["unit_price"], mine.get("tests_per_pack"))
    best = None
    for a in index.equivalents(code):
        if a["unit_price"] is None or a["unit_price"] <= 0:
            continue
        a_per = per_unit(a["unit_price"], a.get("tests_per_pack"))
        pct = None
        if our_per is not None and a_per is not None and our_per > 0:
            pct = (a_per - our_per) / our_per * 100.0
        # Rank by the comparable figure where there is one; rows we cannot
        # compare rank last rather than pretending to be the best offer.
        rank = (pct is None, pct if pct is not None else 0.0)
        if best is None or rank < best[0]:
            best = (rank, a["supplier"], pct)
    if best is None:
        return None, None
    return best[1], best[2]


def alternatives_for(items, index):
    """Rows for the 'Also available from another supplier' table on the
    approver PDF: one per alternative supplier, per PO line that has one.

    Lines with no declared equivalent are simply absent -- the table is a
    signal, not furniture, and it disappears entirely when there is nothing to
    say. Where a pack size is missing on either side the row is still shown,
    with both packs and prices, but `diff_pct` is None: an unnormalised
    comparison dressed up as a normalised one is worse than no comparison.
    """
    out = []
    for i, it in enumerate(items, 1):
        code = norm_code(it.get("material_code"))
        mine = index.by_code.get(code)
        if not mine:
            continue
        alts = index.equivalents(code)
        if not alts:
            continue
        our_price = to_float(it.get("unit_price"))
        if our_price is None:
            our_price = mine["unit_price"]
        our_units = mine.get("tests_per_pack")
        our_per = per_unit(our_price, our_units)

        # One row per supplier: their best offer, not every pack they sell.
        best = {}
        for a in alts:
            if a["unit_price"] is None or a["unit_price"] <= 0:
                continue
            a_per = per_unit(a["unit_price"], a.get("tests_per_pack"))
            key = a["supplier"]
            rank = (a_per if a_per is not None else a["unit_price"], a_per is None)
            if key not in best or rank < best[key][0]:
                best[key] = (rank, a, a_per)

        qty = to_qty(it.get("qty")) or 0
        for supplier, (_rank, a, a_per) in best.items():
            diff_pct = diff_amount = None
            if our_per is not None and a_per is not None and our_per > 0:
                diff_pct = (a_per - our_per) / our_per * 100.0
                if our_units:
                    diff_amount = (a_per - our_per) * qty * our_units
            out.append({
                "no": i,
                "item": mine["item"] or str(it.get("item", "")),
                "pack": mine["pack"] or str(it.get("pack", "")),
                "price": our_price,
                "alt_supplier": supplier,
                "alt_item": a["item"],
                "alt_pack": a["pack"],
                "alt_price": a["unit_price"],
                "diff_pct": diff_pct,
                "diff_amount": diff_amount,
            })
    # Cheapest alternative first; rows we could not compare sink to the bottom
    # rather than sitting among figures that look authoritative.
    out.sort(key=lambda r: (r["diff_pct"] is None,
                            r["diff_pct"] if r["diff_pct"] is not None else 0,
                            r["no"]))
    return out


def orderable_rows(index, category, supplier):
    """The rows a name-driven template for this list may offer.

    Three exclusions, all of them the same rule -- an item that cannot be
    identified unambiguously is not offered:
      * code duplicated anywhere in the master (already absent from by_code),
      * name repeated inside this supplier's list,
      * no usable price.
    Sorted by name, because a name is what the requester now scans for.
    """
    out = [r for r in index.rows_for(category, supplier)
           if r["unit_price"] and r["unit_price"] > 0
           and index.item(category, supplier, r["item"]) is not None]
    return sorted(out, key=lambda r: (norm_name(r["item"]), r["material_code"]))


def resolve_key(value, index, category=None, supplier=None):
    """Resolve one cell of the item column to a master row.

    Returns (row, status, message); status is STATUS_OK when row is not None.

    The name is tried inside the template's own list, never across the whole
    master: names are deliberately shared BETWEEN suppliers -- that is how an
    equivalent is declared -- so a name is only ever an identifier within one
    supplier's list.

    A code is tried first. Templates issued before v2.0 put a code in this
    cell and are still in circulation, and the code is unambiguous where it
    resolves at all, so honouring it costs nothing and rescues those files.
    """
    raw = "" if value is None else re.sub(r"\s+", " ", str(value)).strip()
    if not raw:
        return None, STATUS_MISSING_ITEM, (
            "Item is missing. Clear the whole row, or pick an item from the "
            "drop-down list.")

    code = norm_code(value)
    if code and code in index.dup_codes:
        return None, STATUS_DUPLICATE_CODE, (
            f"Code {code} appears more than once in the master list, so it "
            f"cannot be ordered until that is corrected.")
    row = index.by_code.get(code)
    if row is not None:
        return row, STATUS_OK, ""

    if category and supplier:
        if not index.has_scope(category, supplier):
            return None, STATUS_UNKNOWN_LIST, (
                f"This file says it is the {CATEGORY_LABEL.get(category, category)} "
                f"list for {supplier}, and no such list exists. Ask me for a "
                f"fresh template.")
        if index.name_is_ambiguous(category, supplier, raw):
            return None, STATUS_AMBIGUOUS_NAME, (
                f"\u201c{raw}\u201d now names more than one item on "
                f"{supplier}'s list \u2014 usually the same product in two pack "
                f"sizes. It cannot say which is meant. Ask me for a fresh "
                f"template.")
        row = index.item(category, supplier, raw)
        if row is not None:
            return row, STATUS_OK, ""
        return None, STATUS_NOT_FOUND, (
            f"\u201c{raw}\u201d is not on this template. Pick the item from the "
            f"drop-down list rather than typing it, or you may be using the "
            f"wrong template.")

    # No template scope to work in: only a name that is unique across the whole
    # master can be trusted, and most are not -- shared names are the point.
    nkey = norm_name(raw)
    hits = [r for (_c, _s, n), r in index.by_name.items() if n == nkey]
    if len(hits) == 1 and not any(n == nkey for _c, _s, n in index.dup_names):
        return hits[0], STATUS_OK, ""
    if hits or any(n == nkey for _c, _s, n in index.dup_names):
        return None, STATUS_AMBIGUOUS_NAME, (
            f"\u201c{raw}\u201d is sold by more than one supplier, and this file "
            f"does not say which list it is. Ask me for a fresh template.")
    return None, STATUS_NOT_FOUND, (
        f"\u201c{raw}\u201d is not in the master list. Ask me for a fresh "
        f"template.")


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
        key_raw = "" if raw.get("code") is None else str(raw.get("code")).strip()
        qty_raw = raw.get("qty")
        note = re.sub(r"\s+", " ", str(raw.get("note") or "")).strip()
        # A typed 0 makes the row populated, so it is reported as a bad
        # quantity rather than skipped as if nobody had touched it.
        has_qty = (to_qty(qty_raw) is not None
                   or (qty_raw is not None and str(qty_raw).strip() != ""))

        if not key_raw and not has_qty and not note:
            continue
        populated += 1

        if populated > max_lines:
            report.append(_report_row(
                row_no, raw, STATUS_OVER_LIMIT,
                f"A PO can hold at most {max_lines} lines. Move this line to a "
                f"second file."))
            continue

        master, status, message = resolve_key(
            raw.get("code"), index, template_category, template_supplier)
        if master is None:
            report.append(_report_row(row_no, raw, status, message))
            continue

        # From here the row IS identified, so everything downstream keys on the
        # master's own code exactly as before -- the name was only ever the way
        # in.
        code = master["material_code"]
        if code in seen_codes:
            report.append(_report_row(
                row_no, raw, STATUS_DUPLICATE_LINE,
                f"{master['item']} is already on row {seen_codes[code]}. Combine "
                f"the quantities into one line.", master))
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
