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

from config import Config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Default headers used only if we have to auto-create a master tab in the main spreadsheet.
MASTER_DEFAULT_HEADERS = ["No.", "Material Code", "CML Reagent", "Supplier",
                          "Supplier Reagent", "Price", "Pack"]

PO_HEADERS = [
    "po_no", "created_at", "requester_id", "requester_name", "supplier",
    "total", "urgent", "stage", "status",
    "stock_by", "stock_at", "book_by", "book_at", "fin_by", "fin_at",
    "gm_by", "gm_at", "board_by", "board_at",
    "reject_stage", "reject_reason", "updated_at", "reason", "category", "payment_type",
    "upload_id", "supplier_reason",
]
LINE_HEADERS = ["po_no", "line_id", "material_code", "item", "supplier_reagent", "pack",
                "qty", "unit_price", "line_total", "ref_price", "variant_reason"]
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

    def _ws(self, name, headers):
        sh = self._open()
        try:
            ws = sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=200, cols=max(10, len(headers)))
            ws.update(values=[headers], range_name="A1")
            return ws
        first = [h.strip().lower() for h in ws.row_values(1)]
        if first != headers:
            ws.update(values=[headers], range_name="A1")
        return ws

    def ensure_tabs(self):
        self._ws("POs", PO_HEADERS)
        self._ws("Line_Items", LINE_HEADERS)
        self._ws("Uploads", UPLOAD_HEADERS)
        self._ws("Upload_Rows", UPLOAD_ROW_HEADERS)
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
        ws.append_row([po.get(h, "") for h in PO_HEADERS], value_input_option="USER_ENTERED")

    def _find_po_row(self, ws, po_no):
        for i, v in enumerate(ws.col_values(1), start=1):
            if str(v).strip() == str(po_no):
                return i
        return None

    def get_po(self, po_no):
        ws = self._ws("POs", PO_HEADERS)
        for r in ws.get_all_records(expected_headers=PO_HEADERS):
            if str(r.get("po_no")).strip() == str(po_no):
                return {k: r.get(k, "") for k in PO_HEADERS}
        return None

    def get_pos_by_requester(self, requester_id):
        ws = self._ws("POs", PO_HEADERS)
        out = []
        for r in ws.get_all_records(expected_headers=PO_HEADERS):
            if str(r.get("requester_id")).strip() == str(requester_id):
                out.append({k: r.get(k, "") for k in PO_HEADERS})
        return out

    def update_po(self, po_no, **fields):
        ws = self._ws("POs", PO_HEADERS)
        rownum = self._find_po_row(ws, po_no)
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
        rows = [[po_no, f"{po_no}-{i}", it.get("material_code", ""), it["item"],
                 it.get("supplier_reagent", ""), it.get("pack", ""),
                 it["qty"], it["unit_price"], it["line_total"],
                 it.get("ref_price", it["unit_price"]), it.get("variant_reason", "")]
                for i, it in enumerate(items, 1)]
        if rows:
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
                    "item": str(r.get("item", "")),
                    "supplier_reagent": str(r.get("supplier_reagent", "")),
                    "pack": str(r.get("pack", "")),
                    "qty": int(_to_float(r.get("qty"))),
                    "unit_price": unit,
                    "line_total": _to_float(r.get("line_total")),
                    "ref_price": _to_float(ref_raw) if ref_raw else unit,
                    "variant_reason": str(r.get("variant_reason", "")).strip(),
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

    # ---- upload audit trail ----
    def log_upload_start(self, user, meta, supersedes=None):
        """Written BEFORE parsing decides anything. Returns the upload_id."""
        ws = self._ws("Uploads", UPLOAD_HEADERS)
        upload_id = f"U{int(time.time() * 1000)}"
        row = {"upload_id": upload_id,
               "uploaded_at": time.strftime("%d-%b-%Y %H:%M:%S"),
               "uploaded_by": getattr(user, "full_name", "") or "",
               "uploaded_by_id": getattr(user, "id", ""),
               "result": "reading", "supersedes": supersedes or ""}
        for k in ("file_name", "file_id", "sha256", "size", "sheet_count",
                  "sheet_names", "max_row", "rows_in_range",
                  "populated_below_range", "template_version",
                  "template_supplier", "template_category"):
            row[k] = meta.get(k, "") or ""
        ws.append_row([row.get(h, "") for h in UPLOAD_HEADERS],
                      value_input_option="USER_ENTERED")
        return upload_id

    def log_upload_rows(self, upload_id, report):
        """Every row the reader saw, raw values verbatim, in sheet order."""
        ws = self._ws("Upload_Rows", UPLOAD_ROW_HEADERS)
        rows = [[upload_id] + [r.get(h, "") for h in UPLOAD_ROW_HEADERS[1:]]
                for r in report]
        if rows:
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
