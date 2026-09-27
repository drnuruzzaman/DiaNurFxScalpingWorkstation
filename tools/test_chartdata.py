#!/usr/bin/env python
"""
tools/test_chartdata.py - the live chart's bars, checked against MT5.

    1. one clock: disk bars (broker time) are moved onto the live bars' clock
       (UTC) by the bridge's measured offset - or, while MT5 does not answer,
       by the last one it stored in data/manifest.json
    2. against the running API and bridge (skipped when they are not up), for
       every symbol on disk and every timeframe: the live bars ARE MT5's, in
       order, on one grid; the scroll-back history and the disk fallback used
       while MT5 is down sit on that same grid and overlap it bar for bar

A bar off its series' grid is another timeframe's or another clock's - the
4H chart once held 1-minute bars, and the daily one every day twice.

Run:  python tools/test_chartdata.py      (section 2 needs the API on :8770 and the bridge on :8765)
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = 0, 0
API, BRIDGE_URL = 'http://127.0.0.1:8770', 'http://127.0.0.1:8765'
TF_SECONDS = {'1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600,
              '2h': 7200, '4h': 14400, '1d': 86400, '1w': 604800}


def check(name: str, ok: bool, detail: str = '') -> None:
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}]   {name}" + (f' - {detail}' if detail else ''))


def section(t: str) -> None:
    print(f'\n{t}\n' + '-' * len(t))


def get(url: str):
    return json.load(urllib.request.urlopen(url, timeout=120))


def on_grid(ts, step_ms: int, ref: int) -> bool:
    return all((ref - int(t)) % step_ms == 0 for t in ts)


class Offline:
    """A bridge that does not answer: what the API sees before MT5 is up."""

    def bars(self, symbol, tf, count):
        from server.datafeed import empty_series
        return empty_series(symbol, tf)

    def health(self):
        return None


class Up:
    def __init__(self, offset_ms):
        self.offset_ms = offset_ms

    def health(self):
        return {'connected': True, 'time_offset_ms': self.offset_ms}


def main() -> int:
    from server import datafeed as dfm

    # ------------------------------------------------------------------ 1
    section('1. one clock for disk and live bars')
    s = dfm.Series(symbol='X', tf='1h', t=np.array([3_600_000.0, 7_200_000.0]),
                   o=np.ones(2), h=np.ones(2), l=np.ones(2), c=np.ones(2), v=np.ones(2))
    moved = dfm.on_live_clock(s, 3 * 3_600_000)
    check('disk bars move back by the offset, and say so',
          list(moved.t) == [3_600_000.0 - 10_800_000, 7_200_000.0 - 10_800_000]
          and moved.tz_offset_ms == 10_800_000 and list(s.t) == [3_600_000.0, 7_200_000.0])
    check('...and are left alone when there is no offset', dfm.on_live_clock(s, 0) is s)
    dfm._CLOCK.update(offset_ms=None, at=0.0)
    check("the bridge's measurement is the offset while it answers",
          dfm.broker_offset_ms(Up(7_200_000)) == 7_200_000)
    from sim.clock import manifest_offset
    stored, _ = manifest_offset(str(dfm.DATA_DIR))
    dfm._CLOCK.update(offset_ms=None, at=0.0)
    got = dfm.broker_offset_ms(Offline())
    check('...and the one it stored in data/manifest.json while it does not',
          got == (stored or 0), f'{got} ms (stored {stored})')

    # ------------------------------------------------------------------ 2
    section('2. every symbol and timeframe, against MT5')
    try:
        get(f'{API}/api/data/job')
        get(f'{BRIDGE_URL}/health')
    except Exception as exc:                                          # noqa: BLE001
        print(f'  skipped - the API or the bridge is not up ({exc.__class__.__name__})')
        print(f'\n{PASS} passed, {FAIL} failed')
        return 1 if FAIL else 0
    symbols = sorted(get(f'{API}/api/data/status')['coverage'])
    for sym in symbols:
        for tf, sec in TF_SECONDS.items():
            step = sec * 1000
            live = get(f'{API}/api/bars?symbol={sym}&tf={tf}&count=600')['bars']
            if not live:
                check(f'{sym} {tf}: live bars', False, 'none')
                continue
            lt = [b[0] for b in live]
            mt5 = get(f'{BRIDGE_URL}/bars?symbol={sym}&tf={tf}&count=600')['bars']
            same = len(mt5) == len(live) and all(
                b[0] == m['t'] and b[1:5] == [m['o'], m['h'], m['l'], m['c']]
                for b, m in zip(live, mt5))
            ordered = all(b > a for a, b in zip(lt, lt[1:]))
            hist = get(f'{API}/api/history?symbol={sym}&tf={tf}'
                       f'&from_ms={lt[0] - 300 * step}&to_ms={lt[-1]}')['bars']
            h_t = [b[0] for b in hist]
            dfm._CLOCK.update(offset_ms=None, at=0.0)
            off = [int(x) for x in dfm.Feed(Offline()).bars(sym, tf, 600, live=True).t]
            ok = (same and ordered and on_grid(lt, step, lt[-1]) and on_grid(h_t, step, lt[-1])
                  and on_grid(off, step, lt[-1]) and bool(set(h_t) & set(lt))
                  and bool(set(off) & set(lt)))
            check(f'{sym} {tf}: live = MT5, ordered, and scroll-back and offline bars on its grid',
                  ok, '' if ok else f'MT5 {same} ordered {ordered} grid {on_grid(lt, step, lt[-1])} '
                  f'history {on_grid(h_t, step, lt[-1])} offline {on_grid(off, step, lt[-1])}')
    print(f'\n{PASS} passed, {FAIL} failed')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
