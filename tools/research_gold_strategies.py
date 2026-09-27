#!/usr/bin/env python
"""
tools/research_gold_strategies.py - does any classic strategy have an edge on gold, before costs?

BACKTEST ONLY. Read-only: it reads data/ and writes runs/research/gold_strategies/<run>/.
Nothing here is wired into the live system.

The question: with costs at ZERO, is there a strategy whose timing makes money
on gold on any timeframe - more than gold's own rise would give anything that
is mostly long? Four families with the most support in the trading literature,
at their textbook parameters, fixed before any result was seen:

  TREND      always in: long above the 100-bar simple average, short below.
  BREAKOUT   Donchian (turtle): long on a close above the 20-bar high, out on
             a close below the 10-bar low; short the mirror; flat between.
  REVERSION  Connors RSI(2): long when RSI(2) < 10 above the 200-bar average,
             out on a close above the 5-bar average; short the mirror
             (RSI(2) > 90 below the 200 average, out below the 5 average).
  SESSION    long from 22:00 to 07:00 UTC (after New York to London), flat
             otherwise - the overnight drift reported for gold. One test, on
             1h bars; it does not depend on the chart's timeframe.

HOW IT IS SCORED, also fixed in advance
  - Every gold timeframe 1m-1d, 2018 to the last bar on disk, per year. A
    signal at a bar's close trades at the NEXT bar's open; returns run open
    to open, weekends included. Log returns, in percent.
  - ZERO COST, as asked. The last column is the cost per round trip (points)
    at which each strategy's gross profit is used up - the real account
    pays about 20-30 (spread ~10-20, commission ~4, slippage ~6).
  - The benchmark is RANDOM TIMING WITH THE SAME EXPOSURE: the strategy's own
    positions shifted in time by a random offset (200 shifts). That keeps how
    often it is long or short and how long it holds, and breaks only the
    timing - so gold's rise, which every mostly-long strategy rides, is in the
    benchmark too.
  - PASSES only if all three hold: 2018-2026 total beats the 95th percentile
    of the shifted benchmark; positive in each of 2024, 2025 and 2026; and
    positive in at least 7 of the 9 years.
  - 28 tests (3 families x 9 timeframes + SESSION). At a 5% bar, one or two
    could pass by luck alone; a pass is a candidate for a costed test, not a
    strategy.

    python tools/research_gold_strategies.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.datafeed import load_disk                                   # noqa: E402
from server.engine.indicators import rsi                                 # noqa: E402
from server.forecast.timebase import broker_to_utc                       # noqa: E402

SYMBOL = 'XAUUSD.a'
TFS = ('1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '1d')
FIRST_YEAR, WARMUP_YEAR = 2018, 2017
SHIFTS = 200
POINT = 0.01
DAY, HOUR = 86_400_000, 3_600_000
OUT = ROOT / 'runs' / 'research' / 'gold_strategies'


# --------------------------------------------------------------------------- #
# the four families: a position (+1 / 0 / -1) decided at each bar's close     #
# --------------------------------------------------------------------------- #
def trend(c: np.ndarray) -> np.ndarray:
    ma = pd.Series(c).rolling(100).mean().to_numpy()
    return np.where(np.isfinite(ma), np.where(c > ma, 1, -1), 0).astype(np.int8)


def breakout(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    hi20 = pd.Series(h).rolling(20).max().shift(1).to_numpy()      # the 20 bars BEFORE this one
    lo20 = pd.Series(l).rolling(20).min().shift(1).to_numpy()
    lo10 = pd.Series(l).rolling(10).min().shift(1).to_numpy()
    hi10 = pd.Series(h).rolling(10).max().shift(1).to_numpy()
    pos = np.zeros(c.size, dtype=np.int8)
    p = 0
    for i in range(c.size):
        if p > 0 and c[i] < lo10[i]:
            p = 0
        elif p < 0 and c[i] > hi10[i]:
            p = 0
        if p <= 0 and c[i] > hi20[i]:
            p = 1
        elif p >= 0 and c[i] < lo20[i]:
            p = -1
        pos[i] = p
    return pos


def reversion(c: np.ndarray) -> np.ndarray:
    r2 = rsi(c, 2)
    ma200 = pd.Series(c).rolling(200).mean().to_numpy()
    ma5 = pd.Series(c).rolling(5).mean().to_numpy()
    pos = np.zeros(c.size, dtype=np.int8)
    p = 0
    for i in range(c.size):
        if not (np.isfinite(ma200[i]) and np.isfinite(r2[i])):
            pos[i] = 0
            continue
        if p > 0 and c[i] > ma5[i]:
            p = 0
        elif p < 0 and c[i] < ma5[i]:
            p = 0
        if p == 0:
            if r2[i] < 10 and c[i] > ma200[i]:
                p = 1
            elif r2[i] > 90 and c[i] < ma200[i]:
                p = -1
        pos[i] = p
    return pos


def session(t: np.ndarray) -> np.ndarray:
    """Long for every bar that OPENS 22:00-06:59 UTC: decided at the close before it."""
    hour = (broker_to_utc(t) % DAY) // HOUR
    want = ((hour >= 22) | (hour < 7)).astype(np.int8)
    pos = np.zeros(t.size, dtype=np.int8)
    pos[:-1] = want[1:]
    return pos


# --------------------------------------------------------------------------- #
# scoring                                                                     #
# --------------------------------------------------------------------------- #
class Series:
    """Open-to-open returns: a position decided at bar i's close earns bar i+1's open to i+2's."""

    def __init__(self, s):
        self.s = s
        o = s.o.astype(np.float64)
        n = o.size
        self.lr = np.zeros(n)                   # earned by the position decided at bar i
        self.dp = np.zeros(n)                   # the same, in price
        self.lr[:n - 2] = np.log(o[2:] / o[1:n - 1])
        self.dp[:n - 2] = o[2:] - o[1:n - 1]
        self.year = s.t.astype('datetime64[ms]').astype('datetime64[Y]').astype(int) + 1970
        self.live = self.year >= FIRST_YEAR
        self.years = sorted(set(self.year[self.live].tolist()))


def score(ser: Series, pos: np.ndarray, seed: int = 11) -> dict:
    live = ser.live
    p = pos.astype(np.float64)
    r = p * ser.lr
    by_year = {y: 100 * float(r[ser.year == y].sum()) for y in ser.years}
    total = 100 * float(r[live].sum())
    # round trips: every change INTO a position; the price earned over them
    entries = (pos != 0) & (np.r_[0, pos[:-1]] != pos)
    n_trades = int(entries[live].sum())
    gross_pts = float((p * ser.dp)[live].sum()) / POINT
    # random timing, same exposure: the positions shifted within the scored span
    lp = p[live]
    lr = ser.lr[live]
    g = np.random.default_rng(seed)
    n = lp.size
    lo = max(1, n // 50)
    shifted = np.array([100 * float((np.roll(lp, int(k)) * lr).sum())
                        for k in g.integers(lo, n - lo, SHIFTS)])
    p95 = float(np.percentile(shifted, 95))
    last3 = [by_year.get(y, 0.0) for y in (2024, 2025, 2026)]
    positive = sum(v > 0 for v in by_year.values())
    passed = total > p95 and all(v > 0 for v in last3) and positive >= 7
    return {'total': total, 'by_year': by_year, 'p95': p95, 'p50': float(np.median(shifted)),
            'trades': n_trades, 'per_year': n_trades / max(1, len(ser.years)),
            'in_market': float(np.mean(pos[live] != 0)), 'long': float(np.mean(pos[live] > 0)),
            'breakeven_pts': gross_pts / n_trades if n_trades else float('nan'),
            'positive_years': positive, 'passed': passed,
            'buy_hold': {y: 100 * float(ser.lr[ser.year == y].sum()) for y in ser.years}}


# --------------------------------------------------------------------------- #
# stage 2: the one pass whose edge exceeds real costs, with the costs         #
# --------------------------------------------------------------------------- #
def session_costed(start_h: int = 22, end_h: int = 7) -> dict:
    """
    SESSION replayed on 1m bars with real costs: buy at the first minute at or
    after start_h UTC (the ask: that minute's recorded spread, floored at 10
    points, plus 3 points of slippage), sell at the first minute at or after
    end_h UTC (the bid, less 3 points), commission $3 a lot a side. One trade
    per night; a night the market does not open is skipped.
    """
    m = load_disk(SYMBOL, '1m', list(range(FIRST_YEAR, datetime.now(timezone.utc).year + 1)))
    utc = broker_to_utc(m.t)
    hour = (utc % DAY) // HOUR
    inside = (hour >= start_h) | (hour < end_h)
    night = (utc - start_h * HOUR) // DAY
    idx = np.nonzero(inside)[0]
    first = {}
    last = {}
    for i in idx:                     # first and last in-window minute of each night
        k = int(night[i])
        first.setdefault(k, i)
        last[k] = i
    spread = np.maximum(m.spread.astype(np.float64), 10.0) * POINT
    slip, comm = 3 * POINT, 2 * 3.0 / 1.4231836618515619 * POINT     # $3/lot/side in price
    rows = []
    for k, i0 in first.items():
        i1 = last[k] + 1
        if i1 >= m.t.size or (utc[i1] % DAY) // HOUR != end_h:       # no clean exit at end_h
            continue
        buy = float(m.o[i0]) + float(spread[i0]) + slip
        sell = float(m.o[i1]) - slip
        rows.append((int(m.t[i0]), float(m.o[i1] - m.o[i0]) / POINT,
                     (sell - buy - comm) / POINT, 100 * np.log((sell - comm) / buy),
                     float(spread[i0]) / POINT))
    a = np.array(rows)
    yr = a[:, 0].astype('datetime64[ms]').astype('datetime64[Y]').astype(int) + 1970
    out = {'window': f'{start_h:02d}-{end_h:02d} UTC', 'nights': int(a.shape[0]),
           'gross_pts': float(a[:, 1].mean()), 'net_pts': float(a[:, 2].mean()),
           'net_pct': float(a[:, 3].sum()), 'entry_spread_med': float(np.median(a[:, 4])),
           'by_year': {int(y): float(a[yr == y, 3].sum()) for y in sorted(set(yr.tolist()))}}
    return out


# --------------------------------------------------------------------------- #
# report                                                                      #
# --------------------------------------------------------------------------- #
def main() -> int:
    t0 = time.perf_counter()
    rows = []
    years = None
    for tf in TFS:
        s = load_disk(SYMBOL, tf, list(range(WARMUP_YEAR, datetime.now(timezone.utc).year + 1)))
        ser = Series(s)
        years = ser.years
        c, h, l = s.c.astype(np.float64), s.h.astype(np.float64), s.l.astype(np.float64)
        for name, pos in (('TREND', trend(c)), ('BREAKOUT', breakout(h, l, c)),
                          ('REVERSION', reversion(c))):
            rows.append((name, tf, score(ser, pos)))
        if tf == '1h':
            rows.append(('SESSION', tf, score(ser, session(s.t))))
            bh = score(ser, np.ones(s.t.size, dtype=np.int8))['by_year']
        print(f'{tf}: {s.t.size:,} bars scored ({time.perf_counter() - t0:.0f}s)', flush=True)

    run_id = datetime.now().strftime('%Y%m%d-%H%M%S')
    d = OUT / run_id
    d.mkdir(parents=True, exist_ok=True)
    out = []
    w = out.append
    w(f'GOLD - CLASSIC STRATEGIES AT ZERO COST, EVERY TIMEFRAME        {SYMBOL}   run {run_id}')
    w('Backtest only. Signal at a bar close, trade at the next open; log returns in %, 2018 to the')
    w('last bar on disk. Benchmark: the same positions shifted in time (same exposure, random timing).')
    w('PASS = total above the benchmark 95th percentile, positive 2024, 2025 and 2026, >= 7 of 9 years.')
    w('break-even = gross points per round trip: the cost at which the edge is gone (real: ~20-30).')
    w('')
    w('gold itself (buy and hold), % per year: '
      + '  '.join(f'{y} {bh[y]:+.0f}' for y in years))
    w('')
    ycols = ''.join(f'{str(y)[2:]:>6}' for y in years)
    w(f"{'strategy':<10}{'tf':>4}{'total%':>8}{'bench p50/p95':>15}{ycols}{'+yrs':>5}{'in mkt':>7}"
      f"{'long':>6}{'trades/yr':>10}{'b/e pts':>8}  verdict")
    for name, tf, r in rows:
        yv = ''.join(f"{r['by_year'].get(y, 0):>+6.0f}" for y in years)
        w(f"{name:<10}{tf:>4}{r['total']:>+8.0f}{r['p50']:>+8.0f}/{r['p95']:<+6.0f}{yv}"
          f"{r['positive_years']:>5}{100 * r['in_market']:>6.0f}%{100 * r['long']:>5.0f}%"
          f"{r['per_year']:>10.0f}{r['breakeven_pts']:>8.1f}  {'PASS' if r['passed'] else '-'}")
    passed = [(n, tf) for n, tf, r in rows if r['passed']]
    w('')
    w(f"{len(passed)} of {len(rows)} passed" + (': ' + ', '.join(f'{n} {tf}' for n, tf in passed)
                                                 if passed else '.'))
    # -- stage 2: SESSION with real costs, and the window moved an hour each way (a
    # robustness check only - the 22-07 window was fixed before any result).
    w('')
    w('STAGE 2 - SESSION with real costs, replayed on 1m bars (the only pass whose zero-cost edge')
    w('exceeds real costs). Windows moved an hour each way show whether it is a knife edge; the')
    w('22-07 window stays the test - the others are not candidates to pick from.')
    w(f"{'window':<13}{'nights':>7}{'gross pts':>10}{'net pts':>9}{'entry spread':>13}{'net %':>7}"
      + ''.join(f'{str(y)[2:]:>6}' for y in years))
    for sh, eh in ((22, 7), (21, 7), (23, 7), (22, 6), (22, 8)):
        c = session_costed(sh, eh)
        w(f"{c['window']:<13}{c['nights']:>7}{c['gross_pts']:>+10.1f}{c['net_pts']:>+9.1f}"
          f"{c['entry_spread_med']:>13.0f}{c['net_pct']:>+7.0f}"
          + ''.join(f"{c['by_year'].get(y, 0):>+6.0f}" for y in years))
    w('')
    w('Measurements on history - not trading advice, and not a live strategy.')
    txt = '\n'.join(out)
    (d / 'report.txt').write_text(txt, encoding='utf-8')
    (d / 'results.json').write_text(json.dumps([{'strategy': n, 'tf': tf, **{k: v for k, v in r.items()}}
                                                for n, tf, r in rows], indent=1, default=float),
                                    encoding='utf-8')
    print(txt)
    print(f'\n-> {d.relative_to(ROOT)}  ({time.perf_counter() - t0:.0f}s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
