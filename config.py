"""All configuration is read from environment variables (set these in Railway)."""
import os


def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_float(value, default):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _id_list(value):
    if not value:
        return []
    out = []
    for part in str(value).replace(";", ",").split(","):
        n = _to_int(part)
        if n is not None:
            out.append(n)
    return out


class Config:
    # ---- Core (required) ----
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
    GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

    # Master list location. Leave MASTER_SPREADSHEET_ID blank to use the same spreadsheet,
    # or set it (+ MASTER_TAB) to read your existing "Supplier MasterList" sheet directly.
    MASTER_SPREADSHEET_ID = os.environ.get("MASTER_SPREADSHEET_ID", "")
    MASTER_TAB = os.environ.get("MASTER_TAB", "Table1")

    # ---- Behaviour ----
    TIMEZONE = os.environ.get("TIMEZONE", "Asia/Phnom_Penh")
    START_PO_NO = _to_int(os.environ.get("START_PO_NO")) or 1
    CURRENCY = os.environ.get("CURRENCY", "$")
    COMPANY_NAME = os.environ.get("COMPANY_NAME", "CAMMED LAB")
    # "Other" category: if a reused item's new price differs from the reference by at least
    # this percentage, the requester must confirm and approvers see a warning flag.
    OTHER_PRICE_TOLERANCE_PCT = _to_float(os.environ.get("OTHER_PRICE_TOLERANCE_PCT"), 20.0)

    # ---- One group chat ID per stage ----
    CHAT_IDS = {
        "stock": _to_int(os.environ.get("STOCK_CHAT_ID")),
        "book": _to_int(os.environ.get("BOOKKEEPING_CHAT_ID")),
        "fin": _to_int(os.environ.get("FINANCE_CHAT_ID")),
        "gm": _to_int(os.environ.get("GM_CHAT_ID")),
        "board": _to_int(os.environ.get("BOARD_CHAT_ID")),
        "approved": _to_int(os.environ.get("APPROVED_PO_CHAT_ID")),
        "cash": _to_int(os.environ.get("CASH_ADVANCE_CHAT_ID")),
    }

    # ---- Optional approver locks (comma-separated user IDs; empty = anyone in the group) ----
    APPROVERS = {
        "stock": _id_list(os.environ.get("STOCK_APPROVERS")),
        "book": _id_list(os.environ.get("BOOKKEEPING_APPROVERS")),
        "fin": _id_list(os.environ.get("FINANCE_APPROVERS")),
        "gm": _id_list(os.environ.get("GM_APPROVERS")),
        "board": _id_list(os.environ.get("BOARD_APPROVERS")),
    }

    @classmethod
    def missing_core(cls):
        missing = []
        if not cls.BOT_TOKEN:
            missing.append("BOT_TOKEN")
        if not cls.SPREADSHEET_ID:
            missing.append("SPREADSHEET_ID")
        if not cls.GOOGLE_CREDENTIALS_JSON:
            missing.append("GOOGLE_CREDENTIALS_JSON")
        return missing
