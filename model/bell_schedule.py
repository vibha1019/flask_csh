"""Del Norte High School bell schedule (from
https://delnorte.powayusd.com/apps/bell_schedules/), used to automatically
determine the current class period and its real start/end time so tardy is
computed against the actual school day, not a manually typed duration.

Only the two schedules that repeat every week are modeled: Regular
(Mon/Tue/Thu/Fri) and Wednesday Late Start. Special-day schedules (minimum
days, assemblies, finals, parade day, etc.) are not handled and fall back to
"no bell period right now" on those days. Break, lunch, and office hours are
intentionally excluded since attendance is not taken during them.

Times are interpreted in the school's timezone (SCHOOL_TIMEZONE env var,
default America/Los_Angeles), not the server's, since the server may run
in UTC (e.g. in Docker).
"""

import os
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

SCHOOL_TZ = ZoneInfo(os.environ.get("SCHOOL_TIMEZONE", "America/Los_Angeles"))

REGULAR_WEEKDAYS = {0, 1, 3, 4}  # Mon, Tue, Thu, Fri
WEDNESDAY = 2

REGULAR_SCHEDULE = [
    ("Period 1", time(8, 35), time(9, 41)),
    ("Period 2", time(9, 46), time(10, 55)),
    ("Period 3", time(11, 37), time(12, 43)),
    ("Period 4", time(13, 18), time(14, 24)),
    ("Period 5", time(14, 29), time(15, 35)),
]

WEDNESDAY_SCHEDULE = [
    ("Period 1", time(9, 35), time(10, 34)),
    ("Period 2", time(10, 39), time(11, 38)),
    ("Period 3", time(11, 53), time(12, 57)),
    ("Period 4", time(13, 32), time(14, 31)),
    ("Period 5", time(14, 36), time(15, 35)),
]

# Not an official district figure, just a reasonable default grace window.
# Adjust to whatever the real tardy policy is.
DEFAULT_GRACE_SECONDS = 300


def _schedule_for_weekday(weekday):
    if weekday in REGULAR_WEEKDAYS:
        return REGULAR_SCHEDULE
    if weekday == WEDNESDAY:
        return WEDNESDAY_SCHEDULE
    return None


def get_bell_period(utc_dt):
    """Returns (name, start_utc, end_utc) for whichever class period
    contains utc_dt (a naive UTC datetime), or None if it's a weekend,
    before/after school, or a break/lunch/passing period. start_utc/end_utc
    are naive UTC datetimes, matching how timestamps are stored."""
    local_dt = utc_dt.replace(tzinfo=timezone.utc).astimezone(SCHOOL_TZ)
    schedule = _schedule_for_weekday(local_dt.weekday())
    if not schedule:
        return None

    for name, start, end in schedule:
        if start <= local_dt.time() < end:
            start_local = local_dt.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            end_local = local_dt.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
            start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
            end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
            return name, start_utc, end_utc

    return None
