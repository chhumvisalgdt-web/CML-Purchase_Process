"""Google Sheets storage layer (gspread + service account).

- POs and Line_Items live in SPREADSHEET_ID.
- The master list is read from MASTER_SPREADSHEET_ID / MASTER_TAB (defaults to the same
  spreadsheet). Column headers are matched by name, so your existing "Supplier MasterList"
  (No. / Material Code / CML Reagent / Supplier / Supplier Reagent / Price / Pack) works as-is.
"""
import json
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
]
LINE_HEADERS = ["po_no", "line_id", "material_code", "item", "supplier_reagent", "pack",
                "qty", "unit_price", "line_total"]
# Reusable reference list for the "Other" category (bot reads and appends to it)
OTHER_HEADERS = ["item", "unit_price", "supplier"]


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
        self._ws("Other_Items", OTHER_HEADERS)
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

    # ---- master list ----
    def get_master(self, force=False):
        if (not force) and self._master_cache is not None and (time.time() - self._master_cache_at) < 60:
            return self._master_cache
        try:
            ws = self._open_master().worksheet(Config.MASTER_TAB)
        except gspread.WorksheetNotFound:
            self._master_cache, self._master_cache_at = [], time.time()
            return []
        rows = ws.get_all_values()
        if not rows:
            self._master_cache, self._master_cache_at = [], time.time()
            return []
        norm = [_norm(h) for h in rows[0]]

        def idx(*keys):
            for k in keys:
                if k in norm:
                    return norm.index(k)
            return -1

        ci = {
            "name": idx("cml reagent", "reagent", "item"),
            "code": idx("material code", "code"),
            "supplier": idx("supplier"),
            "sreagent": idx("supplier reagent"),
            "price": idx("price", "unit price"),
            "pack": idx("pack"),
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
            })
        self._master_cache, self._master_cache_at = out, time.time()
        return out

    def find_item(self, name):
        name_l = name.strip().lower()
        for m in self.get_master():
            if m["item"].lower() == name_l:
                return m
        return None

    def suggest_items(self, name, n=3):
        master = self.get_master()
        names = [m["item"] for m in master]
        matches = difflib.get_close_matches(name, names, n=n, cutoff=0.4)
        subs = [x for x in names if name.strip().lower() in x.lower() and x not in matches]
        chosen = (matches + subs)[:n]
        by_name = {m["item"]: m for m in master}
        return [by_name[x] for x in chosen]

    # ---- "Other" reusable list ----
    def get_other_master(self, force=False):
        if (not force) and self._other_cache is not None and (time.time() - self._other_cache_at) < 300:
            return self._other_cache
        ws = self._ws("Other_Items", OTHER_HEADERS)
        out = []
        for r in ws.get_all_records(expected_headers=OTHER_HEADERS):
            name = str(r.get("item", "")).strip()
            if not name:
                continue
            out.append({
                "item": name,
                "unit_price": _to_float(r.get("unit_price")),
                "supplier": str(r.get("supplier", "")).strip(),
                "material_code": "", "supplier_reagent": "", "pack": "",
            })
        self._other_cache, self._other_cache_at = out, time.time()
        return out

    def find_other_item(self, name):
        name_l = name.strip().lower()
        for m in self.get_other_master():
            if m["item"].lower() == name_l:
                return m
        return None

    def suggest_other_items(self, name, n=6):
        master = self.get_other_master()
        names = [m["item"] for m in master]
        matches = difflib.get_close_matches(name, names, n=n, cutoff=0.4)
        subs = [x for x in names if name.strip().lower() in x.lower() and x not in matches]
        chosen = (matches + subs)[:n]
        by_name = {m["item"]: m for m in master}
        return [by_name[x] for x in chosen]

    def add_other_item(self, item, unit_price, supplier):
        if self.find_other_item(item):
            return
        ws = self._ws("Other_Items", OTHER_HEADERS)
        ws.append_row([item, unit_price, supplier], value_input_option="USER_ENTERED")
        self._other_cache = None

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
        for key, val in fields.items():
            if key in PO_HEADERS:
                ws.update_cell(rownum, PO_HEADERS.index(key) + 1, val)
        return True

    # ---- line items ----
    def add_line_items(self, po_no, items):
        ws = self._ws("Line_Items", LINE_HEADERS)
        rows = [[po_no, f"{po_no}-{i}", it.get("material_code", ""), it["item"],
                 it.get("supplier_reagent", ""), it.get("pack", ""),
                 it["qty"], it["unit_price"], it["line_total"]]
                for i, it in enumerate(items, 1)]
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")

    def get_line_items(self, po_no):
        ws = self._ws("Line_Items", LINE_HEADERS)
        out = []
        for r in ws.get_all_records(expected_headers=LINE_HEADERS):
            if str(r.get("po_no")).strip() == str(po_no):
                out.append({
                    "line_id": str(r.get("line_id", "")),
                    "material_code": str(r.get("material_code", "")),
                    "item": str(r.get("item", "")),
                    "supplier_reagent": str(r.get("supplier_reagent", "")),
                    "pack": str(r.get("pack", "")),
                    "qty": int(_to_float(r.get("qty"))),
                    "unit_price": _to_float(r.get("unit_price")),
                    "line_total": _to_float(r.get("line_total")),
                })
        return out

    def replace_line_items(self, po_no, items):
        ws = self._ws("Line_Items", LINE_HEADERS)
        values = ws.get_all_values()
        header = values[0] if values else LINE_HEADERS
        body = values[1:] if len(values) > 1 else []
        keep = [header] + [r for r in body if r and str(r[0]).strip() != str(po_no)]
        ws.clear()
        ws.update(values=keep, range_name="A1")
        self.add_line_items(po_no, items)


sheets = Sheets()
