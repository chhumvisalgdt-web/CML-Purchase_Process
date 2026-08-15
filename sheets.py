"""Google Sheets storage layer (gspread + service account).

- POs and Line_Items live in SPREADSHEET_ID.
- The master list is read from MASTER_SPREADSHEET_ID / MASTER_TAB (defaults to the same
  spreadsheet). Column headers are matched by name, so your existing "Supplier MasterList"
  (No. / Material Code / CML Reagent / Supplier / Supplier Reagent / Price / Pack) works as-is.
"""
import json
import re
import time
import difflib

import gspread
from google.oauth2.service_account import Credentials

import clock
from config import Config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Default headers used only if we have to auto-create a master tab in the main spreadsheet.
MASTER_DEFAULT_HEADERS = ["No.", "Material Code", "CML Reagent", "Supplier",
                          "Supplier Reagent", "Price", "Pack",
                          "Supplier Material Code", "Equivalent",
                          "Tests per pack"]

PO_HEADERS = [
    "po_no", "created_at", "requester_id", "requester_name", "supplier",
    "total", "urgent", "stage", "status",
    "stock_by", "stock_at", "book_by", "book_at", "fin_by", "fin_at",
    "gm_by", "gm_at", "board_by", "board_at",
    "reject_stage", "reject_reason", "updated_at", "reason", "category", "payment_type",
    "upload_id", "supplier_reason",
    "price_confirmed_by", "price_confirmed_at",
    "approved_at", "order_due", "order_due_note",
    "received_status", "closed_at", "closed_reason",
]
LINE_HEADERS = ["po_no", "line_id", "material_code", "supplier_code", "item",
                "supplier_reagent", "pack", "qty", "unit_price", "line_total",
                "ref_price", "variant_reason", "on_hand",
                "cancelled_qty", "cancelled_by", "cancelled_at", "cancel_reason"]
# Frozen record of the old free-entry "Other" list. The bot no longer reads or
# writes it -- "Other" items now live in Config.OTHER_MASTER_TAB with a code,
# a supplier and a price, exactly like reagents. Kept only so past POs remain
# explainable.
OTHER_HEADERS = ["item", "unit_price", "supplier", "updated_at", "updated_by"]

# ---- Excel upload audit tabs ----
# Uploads records one row PER ATTEMPT, written before parsing decides anything.
# sheet_count / max_row are taken from the workbook BEFORE row iteration: parsed
# output alone can never show that a row was silently dropped.
UPLOAD_HEADERS = [
    "upload_id", "uploaded_at", "uploaded_by", "uploaded_by_id", "file_name",
    "file_id", "sha256", "size", "sheet_count", "sheet_names", "max_row",
    "rows_in_range", "populated_below_range", "template_version",
    "template_supplier", "template_category", "rows_populated", "rows_ok",
    "rows_blocked", "derived_supplier", "derived_category", "template_matches",
    "result", "po_no", "supersedes",
]
# Upload_Rows records every row the READER SAW, raw values verbatim, before
# validation is applied. A log of what passed says nothing about what was
# rejected; the gap between raw_* and matched_* is the audit evidence.
UPLOAD_ROW_HEADERS = [
    "upload_id", "row_no", "raw_code", "raw_qty", "raw_note", "status",
    "message", "matched_code", "matched_item", "matched_supplier", "matched_pack",
]

# ---- goods receipt ----
# Append-only, one row per line PER LOT per delivery. A correction is a
# negative row with a reason, never an edit: overwriting a quantity loses the
# delivery history exactly when it is needed.
RECEIPT_HEADERS = [
    "receipt_id", "po_no", "line_id", "material_code", "item", "qty_received",
    "invoice_qty", "lot_no", "expiry", "invoice_no", "invoice_date",
    "received_by", "received_at", "note",
]
# Stock on hand recorded by the stock controller at the stock stage. Also
# append-only -- a re-count is a new row, so the sequence stays readable.
STOCK_COUNT_HEADERS = [
    "po_no", "line_id", "material_code", "item", "on_hand", "ordered",
    "counted_by", "counted_at",
]


def _to_float(v):
    if v is None:
        return 0.0
    s = str(v).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _norm(h):
    return str(h).strip().lower().rstrip(".").strip()


def norm_supplier(s):
    """Normalize a supplier name for duplicate detection: casefold, keep letters/digits only.
    'ABC Co.' / 'abc co' / 'ABC-CO' all become 'abcco'."""
    return re.sub(r"[^a-z0-9]+", "", str(s).casefold())


class Sheets:
    def __init__(self):
        self._gc = None
        self._sh = None       # POs / Line_Items spreadsheet
        self._msh = None      # master spreadsheet (may be the same object)
        self._master_cache = None
        self._master_cache_at = 0.0
        self._other_cache = None
        self._other_cache_at = 0.0
        self._ws_cache = {}       # tab name -> worksheet handle
        self._po_rows = {}        # po_no -> row number

    # ---- clients ----
    def _client(self):
        if self._gc is None:
            info = json.loads(Config.GOOGLE_CREDENTIALS_JSON)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
            self._gc = gspread.authorize(creds)
        return self._gc

    def _open(self):
        if self._sh is None:
            self._sh = self._client().open_by_key(Config.SPREADSHEET_ID)
        return self._sh

    def _open_master(self):
        mid = Config.MASTER_SPREADSHEET_ID or Config.SPREADSHEET_ID
        if mid == Config.SPREADSHEET_ID:
            return self._open()
        if self._msh is None:
            self._msh = self._client().open_by_key(mid)
        return self._msh

    @staticmethod
    def _ensure_capacity(ws, needed):
        """A new tab holds 1000 rows and gspread's append fails once that is
        used up. Receipts and Upload_Rows are the fast growers -- one row per
        line per lot per delivery -- so grow the tab before writing rather than
        discovering the ceiling on a live receipt."""
        try:
            used = len(ws.col_values(1))
            spare = (ws.row_count or 0) - used
            if spare < needed + 50:
                ws.add_rows(max(1000, needed + 500))
        except Exception:
            pass

    def _ws(self, name, headers):
        """Cached worksheet handle. The header check ran on EVERY call before,
        which was roughly a third of the API traffic; once per process is
        enough, since only the bot writes these tabs."""
        ws = self._ws_cache.get(name)
        if ws is not None:
            return ws
        sh = self._open()
        try:
            ws = sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=2000, cols=max(10, len(headers)))
            ws.update(values=[headers], range_name="A1")
            self._ws_cache[name] = ws
            return ws
        first = [h.strip().lower() for h in ws.row_values(1)]
        if first != headers:
            ws.update(values=[headers], range_name="A1")
        self._ws_cache[name] = ws
        return ws

    def ensure_tabs(self):
        self._ws("POs", PO_HEADERS)
        self._ws("Line_Items", LINE_HEADERS)
        self._ws("Uploads", UPLOAD_HEADERS)
        self._ws("Upload_Rows", UPLOAD_ROW_HEADERS)
        self._ws("Receipts", RECEIPT_HEADERS)
        self._ws("Stock_Counts", STOCK_COUNT_HEADERS)
        # If the master list is meant to be in the main spreadsheet, make sure the tab exists.
        mid = Config.MASTER_SPREADSHEET_ID or Config.SPREADSHEET_ID
        if mid == Config.SPREADSHEET_ID:
            sh = self._open()
            try:
                sh.worksheet(Config.MASTER_TAB)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(title=Config.MASTER_TAB, rows=200, cols=8)
                ws.update(values=[MASTER_DEFAULT_HEADERS], range_name="A1")
        try:
            return json.loads(Config.GOOGLE_CREDENTIALS_JSON).get("client_email", "")
        except Exception:
            return ""

    # ---- master lists ----
    def _read_master_tab(self, tab, category):
        """Read one master tab. Column headers are matched by name so both
        `Reagent Master` (CML Reagent / Pack) and `Other Master` (Name / Unit)
        work without a schema change."""
        try:
            ws = self._open_master().worksheet(tab)
        except gspread.WorksheetNotFound:
            return []
        rows = ws.get_all_values()
        if not rows:
            return []
        norm = [_norm(h) for h in rows[0]]

        def idx(*keys):
            for k in keys:
                if k in norm:
                    return norm.index(k)
            return -1

        ci = {
            "name": idx("cml reagent", "reagent", "item", "name"),
            "code": idx("material code", "code"),
            "supplier": idx("supplier"),
            "sreagent": idx("supplier reagent"),
            "price": idx("price", "unit price"),
            "pack": idx("pack", "unit"),
            # Optional. Both blank on most rows; the bot ignores a row for
            # cross-supplier comparison unless BOTH are filled in.
            "equivalent": idx("equivalent", "equivalent group", "equiv"),
            "tests_per_pack": idx("tests per pack", "tests/pack",
                                  "units per pack", "qty per pack"),
        }
        out = []
        for r in rows[1:]:
            def g(key):
                i = ci[key]
                return r[i].strip() if 0 <= i < len(r) else ""
            name = g("name")
            if not name:
                continue
            out.append({
                "item": name,
                "material_code": g("code"),
                "supplier": g("supplier"),
                "supplier_reagent": g("sreagent"),
                "unit_price": _to_float(g("price")),
                "pack": g("pack"),
                "category": category,
                "equivalent": g("equivalent"),
                "tests_per_pack": g("tests_per_pack"),
            })
        return out

    def get_master(self, force=False):
        if (not force) and self._master_cache is not None and (time.time() - self._master_cache_at) < 60:
            return self._master_cache
        out = self._read_master_tab(Config.MASTER_TAB, "reagent")
        self._master_cache, self._master_cache_at = out, time.time()
        return out

    def get_other_master(self, force=False):
        """Read-only. The bot must never write to a catalogue that carries prices:
        that would put the requester back in the price-setting seat."""
        if (not force) and self._other_cache is not None and (time.time() - self._other_cache_at) < 60:
            return self._other_cache
        out = self._read_master_tab(Config.OTHER_MASTER_TAB, "other")
        self._other_cache, self._other_cache_at = out, time.time()
        return out

    def all_known_suppliers(self):
        """Distinct suppliers across both master tabs, original casing.
        Feeds the template picker."""
        seen, out = set(), []
        for m in self.get_master() + self.get_other_master():
            s = m["supplier"]
            if s and norm_supplier(s) not in seen:
                seen.add(norm_supplier(s))
                out.append(s)
        return out

    def find_items(self, name):
        """All master rows whose item name matches exactly — one per supplier/pack variant.
        The same CML reagent can be listed under several suppliers; return every variant."""
        name_l = name.strip().lower()
        return [m for m in self.get_master() if m["item"].lower() == name_l]

    def find_item(self, name):
        matches = self.find_items(name)
        return matches[0] if matches else None

    def suggest_items(self, name, n=3):
        """Suggest distinct item NAMES (duplicates across suppliers collapsed consistently
        to the first occurrence; the caller re-resolves variants via find_items on pick)."""
        master = self.get_master()
        by_name, names = {}, []
        for m in master:
            key = m["item"].lower()
            if key not in by_name:
                by_name[key] = m
                names.append(m["item"])
        matches = difflib.get_close_matches(name, names, n=n, cutoff=0.4)
        subs = [x for x in names if name.strip().lower() in x.lower() and x not in matches]
        chosen = (matches + subs)[:n]
        return [by_name[x.lower()] for x in chosen]

    # ---- PO numbering ----
    def next_po_no(self):
        ws = self._ws("POs", PO_HEADERS)
        nums = []
        for v in ws.col_values(1)[1:]:
            try:
                nums.append(int(str(v).strip()))
            except ValueError:
                pass
        base = max(nums) if nums else (Config.START_PO_NO - 1)
        return max(base + 1, Config.START_PO_NO)

    # ---- PO rows ----
    def create_po(self, po):
        ws = self._ws("POs", PO_HEADERS)
        self._ensure_capacity(ws, 1)
        res = ws.append_row([po.get(h, "") for h in PO_HEADERS],
                            value_input_option="USER_ENTERED")
        try:
            rng = res["updates"]["updatedRange"].split("!")[1]
            self._po_rows[str(po.get("po_no"))] = int(
                "".join(ch for ch in rng.split(":")[0] if ch.isdigit()))
        except Exception:
            pass

    def _find_po_row(self, ws, po_no):
        cached = self._po_rows.get(str(po_no))
        if cached:
            return cached
        for i, v in enumerate(ws.col_values(1), start=1):
            if str(v).strip() == str(po_no):
                self._po_rows[str(po_no)] = i
                return i
        return None

    def _verified_po_row(self, ws, po_no):
        """Row number, re-checked against column A before it is used to WRITE.

        The cache survives edits made in the browser. Delete a row in POs and
        every cached row number below it is off by one -- so an approval would
        be written onto a neighbouring PO, silently and with a real name and
        timestamp on it. get_po already re-checked; writes did not.
        """
        rownum = self._find_po_row(ws, po_no)
        if not rownum:
            return None
        try:
            actual = str(ws.cell(rownum, 1).value or "").strip()
        except Exception:
            return rownum
        if actual == str(po_no):
            return rownum
        self._po_rows.pop(str(po_no), None)
        rownum = self._find_po_row(ws, po_no)
        return rownum

    def get_po(self, po_no):
        """One column scan (usually cached) plus one row fetch. The old version
        pulled the entire POs tab through get_all_records on every call, and
        update_po then scanned column A a second time."""
        ws = self._ws("POs", PO_HEADERS)
        rownum = self._find_po_row(ws, po_no)
        if not rownum:
            return None
        vals = ws.row_values(rownum)
        if str(vals[0]).strip() != str(po_no):   # rows shifted underneath us
            self._po_rows.pop(str(po_no), None)
            rownum = self._find_po_row(ws, po_no)
            if not rownum:
                return None
            vals = ws.row_values(rownum)
        vals += [""] * (len(PO_HEADERS) - len(vals))
        return {h: vals[i] for i, h in enumerate(PO_HEADERS)}

    def get_pos_by_requester(self, requester_id):
        ws = self._ws("POs", PO_HEADERS)
        out = []
        for r in ws.get_all_records(expected_headers=PO_HEADERS):
            if str(r.get("requester_id")).strip() == str(requester_id):
                out.append({k: r.get(k, "") for k in PO_HEADERS})
        return out

    def update_po(self, po_no, **fields):
        ws = self._ws("POs", PO_HEADERS)
        rownum = self._verified_po_row(ws, po_no)
        if not rownum:
            return False
        reqs = [{"range": gspread.utils.rowcol_to_a1(rownum, PO_HEADERS.index(k) + 1),
                 "values": [[v]]}
                for k, v in fields.items() if k in PO_HEADERS]
        if reqs:
            ws.batch_update(reqs, value_input_option="USER_ENTERED")
        return True

    # ---- line items ----
    def add_line_items(self, po_no, items):
        ws = self._ws("Line_Items", LINE_HEADERS)
        rows = [[po_no, f"{po_no}-{i}", it.get("material_code", ""),
                 it.get("supplier_code", ""), it["item"],
                 it.get("supplier_reagent", ""), it.get("pack", ""),
                 it["qty"], it["unit_price"], it["line_total"],
                 it.get("ref_price", it["unit_price"]),
                 it.get("variant_reason", ""), it.get("on_hand", ""),
                 "", "", "", ""]
                for i, it in enumerate(items, 1)]
        if rows:
            self._ensure_capacity(ws, len(rows))
            ws.append_rows(rows, value_input_option="USER_ENTERED")

    def get_line_items(self, po_no):
        ws = self._ws("Line_Items", LINE_HEADERS)
        out = []
        for r in ws.get_all_records(expected_headers=LINE_HEADERS):
            if str(r.get("po_no")).strip() == str(po_no):
                unit = _to_float(r.get("unit_price"))
                ref_raw = str(r.get("ref_price", "")).strip()
                out.append({
                    "line_id": str(r.get("line_id", "")),
                    "material_code": str(r.get("material_code", "")),
                    "supplier_code": str(r.get("supplier_code", "")),
                    "item": str(r.get("item", "")),
                    "supplier_reagent": str(r.get("supplier_reagent", "")),
                    "pack": str(r.get("pack", "")),
                    "qty": int(_to_float(r.get("qty"))),
                    "unit_price": unit,
                    "line_total": _to_float(r.get("line_total")),
                    "ref_price": _to_float(ref_raw) if ref_raw else unit,
                    "variant_reason": str(r.get("variant_reason", "")).strip(),
                    "on_hand": str(r.get("on_hand", "")).strip(),
                    "cancelled_qty": int(_to_float(r.get("cancelled_qty"))),
                    "cancel_reason": str(r.get("cancel_reason", "")).strip(),
                })
        return out

    def replace_line_items(self, po_no, items):
        """Delete only this PO's rows. The old implementation cleared the whole
        tab and rewrote it, so a failure between the two lost every PO's line
        items -- and upload-driven resubmit puts this on the hot path."""
        ws = self._ws("Line_Items", LINE_HEADERS)
        doomed = [i for i, v in enumerate(ws.col_values(1), start=1)
                  if i > 1 and str(v).strip() == str(po_no)]
        for rownum in reversed(doomed):
            ws.delete_rows(rownum)
        self.add_line_items(po_no, items)

    # ---- goods receipt ----
    def get_receipts(self, po_no):
        """ONE request, whatever the size of the tab.

        The previous version looked the PO up with findall and then fetched
        each hit with its own row_values call: a 12-line PO delivered in two
        lots cost 25 API reads, and /receive calls this twice. Google allows
        about 60 reads a minute, so the receiving path was one busy delivery
        away from failing. A single get_all_values and a filter in Python is
        one request no matter how far Receipts grows.
        """
        ws = self._ws("Receipts", RECEIPT_HEADERS)
        rows = ws.get_all_values()
        out = []
        for vals in rows[1:]:
            if len(vals) < 2 or str(vals[1]).strip() != str(po_no):
                continue
            vals = list(vals) + [""] * (len(RECEIPT_HEADERS) - len(vals))
            r = {h: vals[i] for i, h in enumerate(RECEIPT_HEADERS)}
            r["qty_received"] = int(_to_float(r.get("qty_received")))
            out.append(r)
        return out

    def add_receipts(self, po_no, entries, invoice_no, invoice_date, by, at):
        """One batched write per delivery, never one call per lot."""
        ws = self._ws("Receipts", RECEIPT_HEADERS)
        stamp = int(time.time() * 1000)
        rows = [[f"R{stamp}-{i}", po_no, e.get("line_id", ""),
                 e.get("code", ""), e.get("item", ""), e.get("qty_received", 0),
                 e.get("invoice_qty", ""), e.get("lot_no", ""),
                 e.get("expiry", ""), invoice_no, invoice_date, by, at,
                 e.get("note", "")]
                for i, e in enumerate(entries, 1)]
        if rows:
            self._ensure_capacity(ws, len(rows))
            ws.append_rows(rows, value_input_option="USER_ENTERED")
        return len(rows)

    def add_stock_counts(self, po_no, counts, by, at):
        ws = self._ws("Stock_Counts", STOCK_COUNT_HEADERS)
        rows = [[po_no, c.get("line_id", ""), c.get("code", ""),
                 c.get("item", ""), c.get("on_hand", ""), c.get("ordered", ""),
                 by, at] for c in counts]
        if rows:
            self._ensure_capacity(ws, len(rows))
            ws.append_rows(rows, value_input_option="USER_ENTERED")
        return len(rows)

    # ---- line-item updates (price confirmation, stock count, cancellation) ----
    def _line_rows(self, po_no):
        ws = self._ws("Line_Items", LINE_HEADERS)
        return ws, [i for i, v in enumerate(ws.col_values(1), start=1)
                    if i > 1 and str(v).strip() == str(po_no)]

    def update_lines(self, po_no, by_line_id):
        """by_line_id: {line_id: {column: value}}. One batch_update for the
        whole PO rather than a call per cell.

        The line_id column is fetched once, not cell by cell: a 12-line PO was
        costing 12 reads here on top of the column scan, and stock count,
        price confirmation and cancellation all come through this path.
        """
        ws, rownums = self._line_rows(po_no)
        if not rownums:
            return 0
        lid_col = LINE_HEADERS.index("line_id") + 1
        lid_col_values = ws.col_values(lid_col)
        reqs = []
        for rownum in rownums:
            lid = (str(lid_col_values[rownum - 1]).strip()
                   if rownum <= len(lid_col_values) else "")
            fields = by_line_id.get(lid)
            if not fields:
                continue
            for k, v in fields.items():
                if k in LINE_HEADERS:
                    reqs.append({
                        "range": gspread.utils.rowcol_to_a1(
                            rownum, LINE_HEADERS.index(k) + 1),
                        "values": [[v]]})
        if reqs:
            ws.batch_update(reqs, value_input_option="USER_ENTERED")
        return len(reqs)

    def open_lines(self):
        """Every line still outstanding across all active POs -- feeds the
        monthly cancellation review."""
        ws = self._ws("Line_Items", LINE_HEADERS)
        pos = {p["po_no"]: p for p in self.active_pos()}
        if not pos:
            return []
        got = {}
        rws = self._ws("Receipts", RECEIPT_HEADERS)
        for r in rws.get_all_records(expected_headers=RECEIPT_HEADERS):
            if str(r.get("po_no")).strip() in pos:
                lid = str(r.get("line_id", ""))
                got[lid] = got.get(lid, 0) + int(_to_float(r.get("qty_received")))
        out = []
        for r in ws.get_all_records(expected_headers=LINE_HEADERS):
            po_no = str(r.get("po_no")).strip()
            if po_no not in pos:
                continue
            lid = str(r.get("line_id", ""))
            ordered = int(_to_float(r.get("qty")))
            cancelled = int(_to_float(r.get("cancelled_qty")))
            received = got.get(lid, 0)
            if received + cancelled >= ordered:
                continue
            out.append({
                "po_no": po_no, "line_id": lid,
                "material_code": str(r.get("material_code", "")),
                "item": str(r.get("item", "")),
                "supplier": pos[po_no].get("supplier", ""),
                "created_at": pos[po_no].get("created_at", ""),
                "stage": pos[po_no].get("stage", ""),
                "ordered": ordered, "received": received,
                "cancelled": cancelled,
                "outstanding": ordered - received - cancelled,
            })
        return out

    def active_pos(self):
        ws = self._ws("POs", PO_HEADERS)
        return [{k: r.get(k, "") for k in PO_HEADERS}
                for r in ws.get_all_records(expected_headers=PO_HEADERS)
                if str(r.get("status", "")).strip() == "active"]

    # ---- upload audit trail ----
    def log_upload_start(self, user, meta, supersedes=None):
        """Written BEFORE parsing decides anything. Returns the upload_id."""
        ws = self._ws("Uploads", UPLOAD_HEADERS)
        upload_id = f"U{int(time.time() * 1000)}"
        row = {"upload_id": upload_id,
               "uploaded_at": clock.now_str("%d-%b-%Y %H:%M:%S"),
               "uploaded_by": getattr(user, "full_name", "") or "",
               "uploaded_by_id": getattr(user, "id", ""),
               "result": "reading", "supersedes": supersedes or ""}
        for k in ("file_name", "file_id", "sha256", "size", "sheet_count",
                  "sheet_names", "max_row", "rows_in_range",
                  "populated_below_range", "template_version",
                  "template_supplier", "template_category"):
            row[k] = meta.get(k, "") or ""
        self._ensure_capacity(ws, 1)
        ws.append_row([row.get(h, "") for h in UPLOAD_HEADERS],
                      value_input_option="USER_ENTERED")
        return upload_id

    def log_upload_rows(self, upload_id, report):
        """Every row the reader saw, raw values verbatim, in sheet order."""
        ws = self._ws("Upload_Rows", UPLOAD_ROW_HEADERS)
        rows = [[upload_id] + [r.get(h, "") for h in UPLOAD_ROW_HEADERS[1:]]
                for r in report]
        if rows:
            self._ensure_capacity(ws, len(rows))
            ws.append_rows(rows, value_input_option="USER_ENTERED")

    def log_upload_result(self, upload_id, result, summary=None, po_no=""):
        """result: reading | blocked | checked | cancelled | submitted."""
        ws = self._ws("Uploads", UPLOAD_HEADERS)
        rownum = next((i for i, v in enumerate(ws.col_values(1), start=1)
                       if str(v).strip() == str(upload_id)), None)
        if not rownum:
            return False
        s = summary or {}
        vals = {"result": result}
        if summary:
            vals.update({
                "rows_populated": s.get("rows_populated", ""),
                "rows_ok": s.get("rows_ok", ""),
                "rows_blocked": s.get("rows_blocked", ""),
                "derived_supplier": s.get("supplier", ""),
                "derived_category": s.get("category", ""),
                "template_matches": "yes" if s.get("template_matches", True) else "NO",
            })
        if po_no:
            vals["po_no"] = po_no
        ws.batch_update(
            [{"range": gspread.utils.rowcol_to_a1(rownum, UPLOAD_HEADERS.index(k) + 1),
              "values": [[v]]} for k, v in vals.items() if v != ""],
            value_input_option="USER_ENTERED")
        return True


sheets = Sheets()
