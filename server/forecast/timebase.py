"""
server/forecast/timebase.py - broker server time <-> UTC, and sessions.

Disk bars are stamped in broker server time (see server/datafeed.py). This
broker runs New York + 7h: the week opens Monday 01:00 and the daily break
sits at 23:55-01:00 all year round, straight through both the US and the EU
clock changes (checked on 2019, 2024 and 2025 history). So broker time is
UTC+2 while New York is on standard time and UTC+3 while it is on daylight
time - switching with the US, not with Europe.

Sessions and news are defined in UTC, so everything here converts exactly
rather than assuming the fixed +3h the live clock happens to show today.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from ..config import session_quality

H = 3_600_000
DAY = 86_400_000
BROKER_MINUS_NY = 7 * H

SESSION_NAMES = ('closed', 'sydney', 'tokyo', 'london', 'newyork', 'overlap')
_QUALITY_TO_CODE = {'closed': 0, 'sydney': 1, 'tokyo': 2, 'london': 3, 'newyork': 4,
                    'london/newyork overlap': 5}
# utc hour -> session code, from the engine's own session_quality().
_HOUR_TO_SESSION = np.array([_QUALITY_TO_CODE[session_quality(h)[0]] for h in range(24)],
                            dtype=np.int8)


def _sunday_on_or_after(d: datetime) -> datetime:
    return d + timedelta(days=(6 - d.weekday()) % 7)


def _last_sunday(year: int, month: int) -> datetime:
    nxt = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def us_dst_wall(year: int) -> tuple:
    """
    (start, end) of US daylight time as New York WALL-clock epoch ms.
    Both switches happen at 02:00 local. 2007 onwards: second Sunday in March
    to first Sunday in November; before that, first Sunday in April to last
    Sunday in October.
    """
    if year >= 2007:
        start = _sunday_on_or_after(datetime(year, 3, 8, tzinfo=timezone.utc))
        end = _sunday_on_or_after(datetime(year, 11, 1, tzinfo=timezone.utc))
    else:
        start = _sunday_on_or_after(datetime(year, 4, 1, tzinfo=timezone.utc))
        end = _last_sunday(year, 10)
    return (int(start.timestamp() * 1000) + 2 * H, int(end.timestamp() * 1000) + 2 * H)


def _years(ms: np.ndarray) -> range:
    lo = datetime.fromtimestamp(float(ms.min()) / 1000 - 86400, timezone.utc).year
    hi = datetime.fromtimestamp(float(ms.max()) / 1000 + 86400, timezone.utc).year
    return range(lo, hi + 1)


def broker_to_utc(ms) -> np.ndarray:
    """Broker server epoch ms -> true UTC epoch ms (vectorised)."""
    b = np.asarray(ms, dtype=np.int64)
    if b.size == 0:
        return b.copy()
    wall = b - BROKER_MINUS_NY
    dst = np.zeros(b.shape, dtype=bool)
    for y in _years(wall):
        s, e = us_dst_wall(y)
        dst |= (wall >= s) & (wall < e)
    return wall + np.where(dst, 4 * H, 5 * H)


def utc_to_broker(ms) -> np.ndarray:
    """True UTC epoch ms -> broker server epoch ms (vectorised)."""
    u = np.asarray(ms, dtype=np.int64)
    if u.size == 0:
        return u.copy()
    dst = np.zeros(u.shape, dtype=bool)
    for y in _years(u):
        s, e = us_dst_wall(y)
        dst |= (u >= s + 5 * H) & (u < e + 4 * H)      # switch instants in UTC
    return u + np.where(dst, 3 * H, 2 * H)


def session_code(utc_ms) -> np.ndarray:
    """Engine session at each UTC instant, as a code into SESSION_NAMES."""
    u = np.asarray(utc_ms, dtype=np.int64)
    return _HOUR_TO_SESSION[((u // H) % 24).astype(np.int64)]


def utc_hour(utc_ms) -> np.ndarray:
    """Fractional UTC hour of day, 0..24."""
    u = np.asarray(utc_ms, dtype=np.int64)
    return ((u % DAY) / H).astype(np.float32)


def weekday(utc_ms) -> np.ndarray:
    """0 = Monday .. 6 = Sunday, in UTC."""
    u = np.asarray(utc_ms, dtype=np.int64)
    return (((u // DAY) + 3) % 7).astype(np.int8)          # 1970-01-01 was a Thursday


def broker_day_start(ms) -> np.ndarray:
    """Start of the broker trading day (broker midnight = 17:00 New York)."""
    b = np.asarray(ms, dtype=np.int64)
    return b - (b % DAY)


__all__ = ['SESSION_NAMES', 'us_dst_wall', 'broker_to_utc', 'utc_to_broker', 'session_code',
           'utc_hour', 'weekday', 'broker_day_start']
