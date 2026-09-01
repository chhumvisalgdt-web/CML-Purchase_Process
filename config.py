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
    MASTER_TAB = os.environ.get("MASTER_TAB", "Reagent Master")
    # "Other" catalogue: pre-registered items with code, supplier and price.
    # The bot READS this tab only -- it must never write to a priced catalogue.
    OTHER_MASTER_TAB = os.environ.get("OTHER_MASTER_TAB", "Other Master")

    # ---- Behaviour ----
    TIMEZONE = os.environ.get("TIMEZONE", "Asia/Phnom_Penh")
    # Working hours used to decide when an approved order is actually sendable.
    # WORK_DAYS is Mon=0 .. Sun=6; the default is Mon-Sat.
    WORK_DAYS = [int(d) for d in
                 os.environ.get("WORK_DAYS", "0,1,2,3,4,5").split(",") if d.strip()]
    WORK_START_HOUR = _to_int(os.environ.get("WORK_START_HOUR")) or 8
    WORK_END_HOUR = _to_int(os.environ.get("WORK_END_HOUR")) or 17
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

    # ---- Excel upload path ----
    MAX_LINES = _to_int(os.environ.get("MAX_LINES")) or 12
    MAX_UPLOAD_BYTES = (_to_int(os.environ.get("MAX_UPLOAD_MB")) or 2) * 1024 * 1024

    # ---- Optional approver locks (comma-separated user IDs; empty = anyone in the group) ----
    APPROVERS = {
        "stock": _id_list(os.environ.get("STOCK_APPROVERS")),
        "book": _id_list(os.environ.get("BOOKKEEPING_APPROVERS")),
        "fin": _id_list(os.environ.get("FINANCE_APPROVERS")),
        "gm": _id_list(os.environ.get("GM_APPROVERS")),
        "board": _id_list(os.environ.get("BOARD_APPROVERS")),
    }


    # ---- Supporting attachments (management review) ----
    # Files the requester may attach to a PO so management can see the evidence
    # behind it -- a quotation, a spec sheet, a photo of the broken thing.
    #
    # ATTACH_STAGES is deliberately not a free hand. Three stages can never
    # receive an attachment however this is set (see Config.attach_stages):
    # the stock controller is price-blind and a quotation is a price; the
    # approved-PO group exists to forward documents to the supplier; the cash
    # advance group has no review role. A leak there is one careless env var,
    # so the ban lives in code rather than in a comment.
    ATTACH_CATEGORIES = [c.strip().lower() for c in
                         os.environ.get("ATTACH_CATEGORIES", "other").split(",")
                         if c.strip()]
    ATTACH_STAGES_RAW = [s.strip().lower() for s in
                         os.environ.get("ATTACH_STAGES", "book,fin,gm,board").split(",")
                         if s.strip()]
    ATTACH_NEVER = ("stock", "approved", "cash")
    ATTACH_MAX_COUNT = _to_int(os.environ.get("ATTACH_MAX_COUNT")) or 5
    ATTACH_MAX_BYTES = (_to_int(os.environ.get("ATTACH_MAX_MB")) or 10) * 1024 * 1024
    # Google Drive folder that the service account has Editor on. Blank = no
    # archive; attachments still work, they just live only in Telegram.
    ATTACHMENTS_FOLDER_ID = os.environ.get("ATTACHMENTS_FOLDER_ID", "")

    @classmethod
    def attach_stages(cls):
        return [s for s in cls.ATTACH_STAGES_RAW if s not in cls.ATTACH_NEVER]

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
