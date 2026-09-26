#!/usr/bin/env python
"""
tools/news_backfill.py - historical US release calendar for the forecast engine.

    python tools/news_backfill.py              2017 onwards -> data/_news/history.json
    python tools/news_backfill.py --probe      also check QuantGist / Finnhub history access

The forecast conditions on SCHEDULED information only: at 10:00 it may know
that CPI is due at 10:30, never what CPI will print. So this builds the list
of scheduled tier-one US releases, each at the minute it was due:

  FRED               real release history (dates) for a fixed whitelist of
                     release IDs; the minute comes from the published ET
                     schedule (08:30, JOLTS 10:00), converted to UTC with the
                     US clock change applied exactly
  federalreserve.gov the FOMC meeting calendar; statements at 14:00 ET on the
                     final day. Unscheduled, cancelled and notation-vote
                     entries are left out - nobody knew them in advance
  QuantGist/Finnhub  probed only (--probe). server/news.py found QuantGist has
                     no history (forward ~4 weeks) and Finnhub's economic
                     calendar is not on this account's tier; the probe says
                     whether that is still so

Known limit: a release DELAYED from its schedule (e.g. the 2025 government
shutdown) appears at its actual date, which was not known on the original
one. Rare, and flagged here rather than hidden.

The live calendar (data/_news/calendar.json, owned by the running API) is
never touched. Keys come from .env via server.config and are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UA = {'User-Agent': 'DiaNurFx/1.0 (+local trading workstation)'}

# FRED release id -> (kind, title, scheduled ET time). A whitelist on purpose:
# name matching also catches "Gross Domestic Product by County" and the daily
# "FOMC Press Release" series, neither of which is a market event.
FRED_RELEASES = {
    50: ('NFP', 'Employment Situation', (8, 30)),
    10: ('CPI', 'Consumer Price Index', (8, 30)),
    46: ('PPI', 'Producer Price Index', (8, 30)),
    53: ('GDP', 'Gross Domestic Product', (8, 30)),
    54: ('PCE', 'Personal Income and Outlays', (8, 30)),
    9: ('RETAIL', 'Advance Monthly Sales for Retail and Food Services', (8, 30)),
    180: ('CLAIMS', 'Unemployment Insurance Weekly Claims Report', (8, 30)),
    192: ('JOLTS', 'Job Openings and Labor Turnover Survey', (10, 0)),
}
FOMC_ET = (14, 0)
MONTHS = {m: i + 1 for i, m in enumerate(
    ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
     'september', 'october', 'november', 'december'))}


def _get(url: str, headers: dict = None, timeout: float = 25.0, raw: bool = False):
    h = dict(UA)
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as fh:
        body = fh.read().decode('utf-8', 'replace')
    return body if raw else json.loads(body)


def et_to_utc_ms(year: int, month: int, day: int, hh: int, mm: int) -> int:
    """A New York wall-clock time -> UTC epoch ms, with the US clock change applied."""
    from server.forecast.timebase import BROKER_MINUS_NY, broker_to_utc
    wall = int(datetime(year, month, day, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)
    return int(broker_to_utc([wall + BROKER_MINUS_NY])[0])


def _event(ts: int, kind: str, title: str, source: str) -> dict:
    return {'ts': ts, 'currency': 'USD', 'kind': kind, 'title': title, 'impact': 'high',
            'source': source, 'time_known': True, 'time_inferred': True, 'scheduled': True}


# --------------------------------------------------------------------------- #
# FRED                                                                        #
# --------------------------------------------------------------------------- #
def fred(start: str, end: str) -> tuple:
    key = (os.environ.get('FRED_API_KEY') or '').strip()
    if not key:
        return [], {'fred': 'no FRED_API_KEY in .env'}
    events, status = [], {}
    for rid, (kind, title, (hh, mm)) in FRED_RELEASES.items():
        url = ('https://api.stlouisfed.org/fred/release/dates'
               f'?api_key={urllib.parse.quote(key)}&file_type=json&release_id={rid}'
               f'&realtime_start={start}&realtime_end={end}'
               '&include_release_dates_with_no_data=false&sort_order=asc&limit=10000')
        try:
            rows = _get(url).get('release_dates') or []
        except (urllib.error.URLError, ValueError, OSError) as e:
            status[kind] = f'failed: {type(e).__name__}'
            continue
        kept = 0
        for r in rows:
            try:
                d = datetime.strptime(r['date'], '%Y-%m-%d')
            except (KeyError, ValueError):
                continue
            if d.weekday() >= 5:              # FRED padding, never a release
                continue
            events.append(_event(et_to_utc_ms(d.year, d.month, d.day, hh, mm), kind, title,
                                 'fred'))
            kept += 1
        status[kind] = kept
    return events, status


# --------------------------------------------------------------------------- #
# FOMC, from the Federal Reserve's own calendar                               #
# --------------------------------------------------------------------------- #
def _fomc_main(html: str) -> list:
    """(year, month, day) of each scheduled meeting's final day, 2021 onwards."""
    out = []
    heads = [(m.start(), int(m.group(1))) for m in re.finditer(r'(\d{4}) FOMC Meetings', html)]
    for i, (pos, year) in enumerate(heads):
        seg = html[pos: heads[i + 1][0] if i + 1 < len(heads) else len(html)]
        months = re.findall(r'fomc-meeting__month[^>]*>\s*(?:<[^>]+>\s*)*([^<]+?)\s*<', seg)
        dates = re.findall(r'fomc-meeting__date[^>]*>\s*(?:<[^>]+>\s*)*([^<]+?)\s*<', seg)
        for mon, dd in zip(months, dates):
            dd = dd.strip()
            if not re.fullmatch(r'\d{1,2}(-\d{1,2})?\*?', dd):
                continue                      # notation vote, unscheduled, cancelled
            names = [x for x in re.split(r'[/-]', mon.strip()) if x]
            month = _month(names[-1]) if names else None
            if month is None:
                continue
            day = int(dd.rstrip('*').split('-')[-1])
            out.append((year, month, day))
    return out


def _month(name: str):
    return next((v for k, v in MONTHS.items() if k.startswith(name.lower()[:3])), None)


def _fomc_historical(html: str, year: int) -> list:
    """
    Final days of scheduled meetings on a fomchistoricalYYYY page. Headings
    read "March 14-15 Meeting - 2017", and a meeting that spans two months
    "Jan/Feb 31-1 Meeting - 2017" (or "April 30-May 1").
    """
    txt = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html))
    pat = (r'([A-Z][a-z]+)(?:/([A-Z][a-z]+))? (\d{1,2})(?:-(?:([A-Z][a-z]+) )?(\d{1,2}))?'
           rf'( \((\w+)\))? (?:Meeting|Conference Call)s? - {year}')
    out = set()
    for m in re.finditer(pat, txt):
        if m.group(7):                        # (unscheduled) / (cancelled)
            continue
        month = _month(m.group(4) or m.group(2) or m.group(1))
        if month is None:
            continue
        out.add((year, month, int(m.group(5) or m.group(3))))
    return sorted(out)


def fomc(first_year: int, last_year: int) -> tuple:
    status = {}
    days = set()
    try:
        main = _get('https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm', raw=True)
        got = _fomc_main(main)
        days.update(got)
        status['main_page_years'] = sorted({y for y, _, _ in got})
    except (urllib.error.URLError, OSError) as e:
        status['main_page'] = f'failed: {type(e).__name__}'
    covered = {y for y, _, _ in days}
    for y in range(first_year, last_year + 1):
        if y in covered:
            continue
        try:
            page = _get(f'https://www.federalreserve.gov/monetarypolicy/fomchistorical{y}.htm',
                        raw=True)
            got = _fomc_historical(page, y)
            days.update(got)
            status[f'historical_{y}'] = len(got)
        except (urllib.error.URLError, OSError) as e:
            status[f'historical_{y}'] = f'failed: {type(e).__name__}'
    events = [_event(et_to_utc_ms(y, m, d, *FOMC_ET), 'FOMC', 'FOMC statement', 'federalreserve')
              for (y, m, d) in sorted(days) if first_year <= y <= last_year]
    return events, status


# --------------------------------------------------------------------------- #
# probes                                                                      #
# --------------------------------------------------------------------------- #
def probe() -> dict:
    out = {}
    qk = (os.environ.get('QUANTGIST_API_KEY') or '').strip()
    if qk:
        try:
            d = _get('https://api.quantgist.com/v1/calendar?page=1&per_page=100',
                     {'X-API-Key': qk})
            stamps = sorted(str(r.get('release_time') or '') for r in d.get('data') or []
                            if r.get('release_time'))
            out['quantgist'] = (f'{len(stamps)} rows, earliest {stamps[0][:10]}' if stamps
                                else 'no rows')
        except urllib.error.HTTPError as e:
            out['quantgist'] = f'HTTP {e.code}'
        except (urllib.error.URLError, ValueError, OSError) as e:
            out['quantgist'] = f'failed: {type(e).__name__}'
    else:
        out['quantgist'] = 'no QUANTGIST_API_KEY'
    fk = (os.environ.get('FINNHUB_API_KEY') or '').strip()
    if fk:
        try:
            d = _get('https://finnhub.io/api/v1/calendar/economic?from=2024-01-01&to=2024-01-31'
                     f'&token={urllib.parse.quote(fk)}')
            rows = (d.get('economicCalendar') or []) if isinstance(d, dict) else []
            out['finnhub'] = f'{len(rows)} rows for Jan 2024' if rows else f'no rows: {str(d)[:80]}'
        except urllib.error.HTTPError as e:
            out['finnhub'] = f'HTTP {e.code} (not on this account tier)' if e.code in (401, 403) \
                else f'HTTP {e.code}'
        except (urllib.error.URLError, ValueError, OSError) as e:
            out['finnhub'] = f'failed: {type(e).__name__}'
    else:
        out['finnhub'] = 'no FINNHUB_API_KEY'
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--start', default='2017-01-01')
    ap.add_argument('--probe', action='store_true')
    a = ap.parse_args()
    from server.config import DATA_DIR          # importing server.config loads .env
    out_path = DATA_DIR / '_news' / 'history.json'

    now = datetime.now(timezone.utc)
    end = now.strftime('%Y-%m-%d')
    fred_ev, fred_st = fred(a.start, end)
    fomc_ev, fomc_st = fomc(int(a.start[:4]), now.year)
    events = sorted(fred_ev + fomc_ev, key=lambda e: (e['ts'], e['kind']))
    # One event per (minute, kind): FRED lists GDP revisions etc. once each.
    seen, uniq = set(), []
    for e in events:
        k = (e['ts'], e['kind'])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    # Only what has happened: the live calendar owns the future.
    now_ms = int(now.timestamp() * 1000)
    uniq = [e for e in uniq if e['ts'] <= now_ms]
    status = {'fred': fred_st, 'fomc': fomc_st}
    if a.probe:
        status['probe'] = probe()
    doc = {'generated_ms': now_ms, 'start': a.start, 'status': status,
           'note': 'Scheduled US releases at their scheduled minute (UTC ms). '
                   'Built by tools/news_backfill.py; the live calendar is calendar.json.',
           'events': uniq}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix('.tmp')
    tmp.write_text(json.dumps(doc, indent=1), encoding='utf-8')
    tmp.replace(out_path)
    by_kind = {}
    for e in uniq:
        by_kind[e['kind']] = by_kind.get(e['kind'], 0) + 1
    print(f'{len(uniq)} events -> {out_path.relative_to(ROOT)}')
    print('  by kind:', ', '.join(f'{k} {v}' for k, v in sorted(by_kind.items())))
    print('  status :', json.dumps(status))
    return 0


if __name__ == '__main__':
    sys.exit(main())
