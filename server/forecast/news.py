"""
server/forecast/news.py - scheduled US releases as causal features.

At a bar's close the forecast may know WHEN tier-one releases are scheduled
(the calendars are published weeks ahead) and when the last one happened -
never what a release will print. So every feature here is a function of
scheduled times only:

    next_min    minutes from the close to the next major release (capped)
    next_kind   which release that is (KINDS code, -1 none)
    prev_min    minutes since the last major release (capped)
    in_h        a major release is due inside the market horizon
    in_trade    a major release is due inside the trade horizon (72 x 5m)
    minor_in_h  a minor one (claims, JOLTS) is due inside the market horizon

Source: data/_news/history.json, built by tools/news_backfill.py (FRED
release dates at their scheduled minute + the Federal Reserve's FOMC
calendar). Times are UTC ms; bars are converted with timebase first.
"""
from __future__ import annotations

import json

import numpy as np

from ..config import DATA_DIR

HISTORY = DATA_DIR / '_news' / 'history.json'
KINDS = ('NFP', 'CPI', 'FOMC', 'PPI', 'GDP', 'PCE', 'RETAIL', 'CLAIMS', 'JOLTS')
MAJOR = ('NFP', 'CPI', 'FOMC', 'PPI', 'GDP', 'PCE', 'RETAIL')
CAP_MIN = 1440
_CACHE: dict = {}


def events(path=HISTORY) -> dict:
    """{'major': (ts, kind_code), 'minor': (ts, kind_code)} sorted by time."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {'major': (np.zeros(0, np.int64), np.zeros(0, np.int8)),
                'minor': (np.zeros(0, np.int64), np.zeros(0, np.int8)), 'n': 0}
    hit = _CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    doc = json.loads(path.read_text(encoding='utf-8'))
    code = {k: i for i, k in enumerate(KINDS)}
    rows = [(int(e['ts']), code[e['kind']]) for e in doc.get('events') or []
            if e.get('kind') in code and e.get('scheduled', True) and e.get('time_known', True)]
    rows.sort()
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    kd = np.array([r[1] for r in rows], dtype=np.int8)
    major = np.isin(kd, [code[k] for k in MAJOR])
    out = {'major': (ts[major], kd[major]), 'minor': (ts[~major], kd[~major]),
           'n': int(ts.size), 'first_ms': int(ts[0]) if ts.size else None,
           'last_ms': int(ts[-1]) if ts.size else None}
    _CACHE[key] = (mtime, out)
    return out


def features(close_utc, horizon_ms: int, trade_ms: int, ev: dict = None) -> dict:
    """News features at each bar close (UTC ms), per the module doc."""
    ev = ev or events()
    c = np.asarray(close_utc, dtype=np.int64)
    ts, kd = ev['major']
    n = c.size
    out = {'news_next_min': np.full(n, CAP_MIN, np.float32),
           'news_next_kind': np.full(n, -1, np.int8),
           'news_prev_min': np.full(n, CAP_MIN, np.float32),
           'news_in_h': np.zeros(n, np.int8),
           'news_in_trade': np.zeros(n, np.int8),
           'news_minor_in_h': np.zeros(n, np.int8)}
    if ts.size:
        j = np.searchsorted(ts, c, 'right')              # first release strictly after
        has_next = j < ts.size
        nxt = ts[np.minimum(j, ts.size - 1)]
        mins = (nxt - c) / 60000.0
        out['news_next_min'] = np.where(has_next, np.minimum(mins, CAP_MIN), CAP_MIN) \
            .astype(np.float32)
        out['news_next_kind'] = np.where(has_next & (mins <= CAP_MIN),
                                         kd[np.minimum(j, ts.size - 1)], -1).astype(np.int8)
        has_prev = j > 0
        prv = ts[np.maximum(j - 1, 0)]
        out['news_prev_min'] = np.where(has_prev, np.minimum((c - prv) / 60000.0, CAP_MIN),
                                        CAP_MIN).astype(np.float32)
        out['news_in_h'] = (has_next & (nxt - c <= horizon_ms)).astype(np.int8)
        out['news_in_trade'] = (has_next & (nxt - c <= trade_ms)).astype(np.int8)
    mts, _ = ev['minor']
    if mts.size:
        j = np.searchsorted(mts, c, 'right')
        nxt = mts[np.minimum(j, mts.size - 1)]
        out['news_minor_in_h'] = ((j < mts.size) & (nxt - c <= horizon_ms)).astype(np.int8)
    return out


__all__ = ['HISTORY', 'KINDS', 'MAJOR', 'CAP_MIN', 'events', 'features']
