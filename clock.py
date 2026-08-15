"""One clock for the whole bot.

Every timestamp the bot writes -- approvals, receipts, stock counts, price
confirmations, cancellations, upload log rows, generated-file stamps -- must
come from here.

Before this module there were two clocks: bot.py stamped Config.TIMEZONE while
post_handlers.py, sheets.py and the Excel layers used a naive datetime.now(),
which on Railway is UTC. Phnom Penh is UTC+7, so a delivery recorded three
hours after its approval was written into the sheet as four hours BEFORE it.
An approval trail whose events do not run in order is not an approval trail.
"""
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:      # pragma: no cover - stdlib since 3.9
    ZoneInfo = None

from config import Config

# The one stamp format written to the sheet. Kept here so a change lands
# everywhere at once -- group_handlers parses these back to age a PO.
STAMP = "%d-%b-%Y %H:%M"


def local_now():
    """Naive datetime in Config.TIMEZONE. Naive on purpose: the sheet stores
    wall-clock strings, and mixing offsets into them would be a second format."""
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(Config.TIMEZONE)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.now()


def now_str(fmt=STAMP):
    return local_now().strftime(fmt)


def today():
    """Local calendar date -- what 'expired' and 'short dated' are judged against."""
    return local_now().date()
