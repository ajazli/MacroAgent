"""
Timezone utilities. All bot dates/times are expressed in GMT+8 (Singapore Time).
"""
from datetime import datetime, date, timezone, timedelta

SGT = timezone(timedelta(hours=8))


def now_sgt() -> datetime:
    """Return the current datetime in GMT+8."""
    return datetime.now(SGT)


def today_sgt() -> date:
    """Return today's date in GMT+8."""
    return datetime.now(SGT).date()
