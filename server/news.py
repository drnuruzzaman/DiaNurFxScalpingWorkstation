"""
news.py - one calendar, assembled from several providers.

No single provider gives a complete picture, and each fails differently:

  ForexFactory  this week only, every currency, has times and impact. The
                weekly file is replaced in place, so last week is simply gone.
  QuantGist     ~4 weeks AHEAD, richest fields (forecast/previous/actual,
                affected symbols, impact score). No history: its earliest row
                is always in the future.
  FRED          the only source with real HISTORY - thousands of past release
                dates - but DATE ONLY, no time of day, and no impact rating.
  Finnhub       headlines, not a calendar. The economic-calendar endpoint is
                not on this account's tier (verified: "You don't have access
                to this resource"), so it is used for news text only.

So the calendar is merged, and - because two of the three forward sources
forget the past - everything seen is PERSISTED. The archive is what makes
historical marks possible at full time resolution from today onward; FRED
backfills older dates at day resolution, flagged as such so nothing draws a
precise vertical line through a time it does not actually know.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DATA_DIR

# Underscore-prefixed on purpose: available_symbols() treats every directory
# under data/ as a tradeable instrument, so a plain 'news' folder turned up in
# the watchlist as a symbol with no price.
NEWS_DIR = Path(DATA_DIR) / '_news'
STORE_PATH = NEWS_DIR / 'calendar.json'

IMPACT_RANK = {'holiday': 0, 'low': 1, 'medium': 2, 'high': 3}

_LOCK = threading.Lock()
# key -> event. Loaded once, then kept in memory and flushed after each refresh.
_STORE: dict = {}
_LOADED = False
_LAST_REFRESH = 0.0
REFRESH_EVERY_S = 900.0

# FRED lists every release it carries, including things like "Coinbase
# Cryptocurrencies". These are the ones that actually move gold; anything else
# would be thousands of rows of noise in the archive.
FRED_KEEP = (
    'employment situation',
    'consumer price index',
    'producer price index',
    'gross domestic product',
    'personal income and outlays',
    'advance monthly sales for retail',
    'retail sales',
    'job openings',
    'unemployment insurance weekly claims',
)

# FOMC is deliberately ABSENT from that list. FRED returns a date for
# "FOMC Press Release" on every calendar day - 39 of them in a 120-day window,
# Saturdays and Sundays included - because the release is attached to a daily
# series rather than to the eight actual meetings. Marking all of them would
# put an FOMC line on every candle on the chart. ForexFactory and QuantGist
# both carry real FOMC dates with real times, so nothing is lost.


def _env(name: str) -> str:
    return (os.environ.get(name) or '').strip()


# QuantGist 403s the default Python-urllib agent outright - the same request
# with any real User-Agent succeeds. Sent to every provider for consistency.
_UA = 'DiaNurFx/1.0 (+local trading workstation)'


def _get(url: str, headers: dict = None, timeout: float = 15.0):
    h = {'User-Agent': _UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.loads(fh.read().decode('utf-8', 'replace'))


def _norm_title(t: str) -> str:
    """Loose key for matching the same release across providers."""
    return ''.join(ch for ch in (t or '').lower() if ch.isalnum())[:24]


def event_key(ts: int, currency: str, title: str) -> str:
    """
    Identity for a release.

    Times are rounded to 5 minutes: providers disagree by a minute or two on
    the same event, and without the rounding ForexFactory's 12:30 and
    QuantGist's 12:31 become two marks on the chart for one release.
    """
    slot = int(ts // 300_000) if ts else 0
    return f'{slot}:{(currency or "").upper()}:{_norm_title(title)}'


def _blank(**kw) -> dict:
    e = {
        'ts': None, 'currency': '', 'title': '', 'impact': 'low',
        'forecast': '', 'previous': '', 'actual': '',
        'source': '', 'time_known': True,
        # True when the minute came from the published release schedule rather
        # than from a provider reporting it.
        'time_inferred': False,
    }
    e.update(kw)
    return e


# --------------------------------------------------------------------------- #
# providers                                                                   #
# --------------------------------------------------------------------------- #
def from_forexfactory(bridge_payload: dict) -> list:
    """The bridge already fetches and normalises this one; just re-tag it."""
    out = []
    for e in (bridge_payload or {}).get('events') or []:
        if not e.get('ts'):
            continue
        out.append(_blank(
            ts=int(e['ts']), currency=e.get('currency') or '',
            title=e.get('title') or '', impact=(e.get('impact') or 'low').lower(),
            forecast=e.get('forecast') or '', previous=e.get('previous') or '',
            source='forexfactory'))
    return out


def from_quantgist(timeout: float = 15.0) -> list:
    """
    QuantGist's forward calendar, all pages.

    Its date filters are ignored by the API (verified: passing from/to or
    start_date/end_date returns the same 83 rows), so the whole set is pulled
    and filtered here instead.
    """
    key = _env('QUANTGIST_API_KEY')
    if not key:
        return []
    headers = {'X-API-Key': key}
    out, page = [], 1
    while page <= 10:
        url = f'https://api.quantgist.com/v1/calendar?page={page}&per_page=100'
        try:
            payload = _get(url, headers, timeout)
        except Exception:                                  # noqa: BLE001
            break
        rows = payload.get('data') or []
        for r in rows:
            ts = _parse_iso(r.get('release_time'))
            if ts is None:
                continue
            out.append(_blank(
                ts=ts, currency=r.get('currency') or '',
                title=r.get('title') or '',
                impact=(r.get('impact') or 'low').lower(),
                forecast=_s(r.get('forecast')), previous=_s(r.get('previous')),
                actual=_s(r.get('actual')), source='quantgist'))
        if not payload.get('has_more'):
            break
        page += 1
    return out


# US releases run to a published schedule, in Eastern time. FRED reports the
# DATE a release happened but not the minute, so the minute is taken from that
# schedule rather than left blank - an 08:30 ET CPI print is not a guess.
#
# Anything not on this list keeps `time_known: False` and stays off the chart.
FRED_SCHEDULE_ET = (
    ('employment situation', (8, 30)),
    ('consumer price index', (8, 30)),
    ('producer price index', (8, 30)),
    ('gross domestic product', (8, 30)),
    ('personal income and outlays', (8, 30)),
    ('advance monthly sales for retail', (8, 30)),
    ('retail sales', (8, 30)),
    ('unemployment insurance weekly claims', (8, 30)),
    ('job openings', (10, 0)),
    ('fomc', (14, 0)),
)


def _us_eastern_offset(d: datetime) -> int:
    """
    Hours behind UTC for US Eastern on this date: 4 under DST, else 5.

    DST runs from the second Sunday in March to the first Sunday in November.
    Worth getting right rather than hardcoding -4: an hour of error puts a CPI
    print on the wrong candle, which is the whole reason for marking it.
    """
    year = d.year
    march = datetime(year, 3, 8, tzinfo=timezone.utc)
    start = march + timedelta(days=(6 - march.weekday()) % 7)      # 2nd Sunday
    nov = datetime(year, 11, 1, tzinfo=timezone.utc)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7)            # 1st Sunday
    return 4 if start <= d < end else 5


def _fred_release_time(name: str, day: datetime):
    """(utc_datetime, inferred) for a release on `day`, or (day, False)."""
    low = name.lower()
    for key, (hh, mm) in FRED_SCHEDULE_ET:
        if key in low:
            utc_h = hh + _us_eastern_offset(day)
            return day + timedelta(hours=utc_h, minutes=mm), True
    return day, False


def from_fred(days_back: int = 120, timeout: float = 15.0) -> list:
    """
    Historical US release DATES.

    FRED publishes the date a release happened, not the minute. These are
    therefore marked `time_known: False` and pinned to 00:00 UTC - a caller
    that draws precise vertical lines is expected to skip them rather than
    imply a time nobody reported.
    """
    key = _env('FRED_API_KEY')
    if not key:
        return []
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days_back)).strftime('%Y-%m-%d')
    end = now.strftime('%Y-%m-%d')
    # realtime_END matters as much as start. Without it FRED happily returns
    # SCHEDULED future dates, and descending order then walks back from the
    # far end of next year - the first page came back full of dates in
    # December, none of which had happened.
    url = ('https://api.stlouisfed.org/fred/releases/dates'
           f'?api_key={urllib.parse.quote(key)}&file_type=json'
           f'&realtime_start={start}&realtime_end={end}'
           '&sort_order=desc&limit=1000')
    # NOT include_release_dates_with_no_data. With it on, FRED returns every
    # date a release COULD have carried data, which put an "FOMC Press
    # Release" on the chart every single day including Saturday and Sunday.
    try:
        payload = _get(url, None, timeout)
    except Exception:                                      # noqa: BLE001
        return []

    out = []
    for r in payload.get('release_dates') or []:
        name = r.get('release_name') or ''
        if not any(k in name.lower() for k in FRED_KEEP):
            continue
        try:
            d = datetime.strptime(r['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        # No US macro release lands at a weekend. A date that does is FRED
        # padding its series, not a release.
        if d.weekday() >= 5:
            continue
        when, timed = _fred_release_time(name, d)
        out.append(_blank(
            ts=int(when.timestamp() * 1000), currency='USD', title=name,
            time_inferred=timed,
            # FRED carries no impact rating. Everything on the keep-list is a
            # tier-one US release, so "high" is the honest default here rather
            # than a guess spread across three levels.
            impact='high', source='fred', time_known=timed))
    return out


def headlines(limit: int = 30, timeout: float = 12.0) -> list:
    """
    Finnhub general market news. Not a calendar - headlines only.

    Kept separate from the calendar merge on purpose: a headline has no
    scheduled time to gate a trade against.
    """
    key = _env('FINNHUB_API_KEY')
    if not key:
        return []
    url = f'https://finnhub.io/api/v1/news?category=general&token={urllib.parse.quote(key)}'
    try:
        rows = _get(url, None, timeout)
    except Exception:                                      # noqa: BLE001
        return []
    out = []
    for r in rows[:limit]:
        out.append({
            'ts': int(r.get('datetime') or 0) * 1000,
            'headline': r.get('headline') or '',
            'source': r.get('source') or '',
            'summary': (r.get('summary') or '')[:400],
            'url': r.get('url') or '',
        })
    out.sort(key=lambda x: -x['ts'])
    return out


def _s(v) -> str:
    return '' if v is None else str(v)


def _parse_iso(stamp) -> int:
    if not stamp:
        return None
    s = str(stamp).strip().replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# --------------------------------------------------------------------------- #
# the archive                                                                 #
# --------------------------------------------------------------------------- #
def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        with open(STORE_PATH, 'r', encoding='utf-8') as fh:
            for e in json.load(fh).get('events') or []:
                if e.get('ts'):
                    _STORE[event_key(e['ts'], e.get('currency'), e.get('title'))] = e
    except (OSError, ValueError):
        pass


def _flush() -> None:
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix('.json.tmp')
    events = sorted(_STORE.values(), key=lambda e: e['ts'])
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'events': events, 'saved_ms': int(time.time() * 1000)}, fh)
    os.replace(tmp, STORE_PATH)


# Fields a later fetch is allowed to fill in. A release that has since printed
# gains an `actual`, and a forecast firms up as the date approaches - but a
# provider returning an empty string must never blank a value another one
# already supplied.
_MERGEABLE = ('forecast', 'previous', 'actual')


def _absorb(events: list) -> int:
    added = 0
    for e in events:
        k = event_key(e['ts'], e['currency'], e['title'])
        cur = _STORE.get(k)
        if cur is None:
            _STORE[k] = e
            added += 1
            continue
        for f in _MERGEABLE:
            if not cur.get(f) and e.get(f):
                cur[f] = e[f]
        # A source that knows the time outranks one that does not, and a
        # higher impact rating wins - providers under-rate more often than
        # they over-rate, and missing a release is the worse error.
        if e.get('time_known') and not cur.get('time_known'):
            cur['ts'] = e['ts']
            cur['time_known'] = True
        if IMPACT_RANK.get(e.get('impact'), 0) > IMPACT_RANK.get(cur.get('impact'), 0):
            cur['impact'] = e['impact']
        if e['source'] not in cur['source']:
            cur['source'] = f"{cur['source']}+{e['source']}"
    return added


def refresh(bridge_payload: dict = None, force: bool = False) -> dict:
    """Pull every provider, merge into the archive, persist. Cheap to call."""
    global _LAST_REFRESH
    with _LOCK:
        _load()
        if not force and time.time() - _LAST_REFRESH < REFRESH_EVERY_S:
            return {'cached': True, 'events': len(_STORE)}
        _LAST_REFRESH = time.time()

        counts = {}
        for name, rows in (
            ('forexfactory', from_forexfactory(bridge_payload)),
            ('quantgist', from_quantgist()),
            ('fred', from_fred()),
        ):
            counts[name] = len(rows)
            _absorb(rows)
        try:
            _flush()
        except OSError:
            pass
        return {'cached': False, 'events': len(_STORE), 'fetched': counts}


def window(from_ms: int, to_ms: int, impact: str = 'all',
           time_known_only: bool = False) -> list:
    """Archive slice, oldest first."""
    with _LOCK:
        _load()
        floor = 0 if impact == 'all' else IMPACT_RANK.get(impact, 0)
        out = [
            e for e in _STORE.values()
            if e['ts'] and from_ms <= e['ts'] <= to_ms
            and IMPACT_RANK.get(e.get('impact'), 0) >= floor
            and (not time_known_only or e.get('time_known'))
        ]
    out.sort(key=lambda e: e['ts'])
    return out


def upcoming(within_ms: int, impact: str = 'high') -> list:
    now = int(time.time() * 1000)
    return [e for e in window(now, now + within_ms, impact, time_known_only=True)]


def sources_status() -> dict:
    """Which providers are configured - for the Settings page."""
    return {
        'forexfactory': True,
        'quantgist': bool(_env('QUANTGIST_API_KEY')),
        'fred': bool(_env('FRED_API_KEY')),
        'finnhub': bool(_env('FINNHUB_API_KEY')),
    }
