#!/usr/bin/env python
"""
tools/research_mtf_liquidity.py - a multi-timeframe liquidity + structure framework, backtested.

The practical price-action framework, as fixed rules: the higher timeframe
sets the direction, liquidity resting against that direction gets swept, the
lower timeframe's structure turns back, and the trade rides from the sweep.

THE RULES (MTF-LS v1), fixed before any result was seen. Nothing here was
tuned; every number is a common default of this style of trading.

  1 BIAS        1H structure. Fractal pivots, k = 3 (a pivot high is above
                the 3 bars before it and not below the 3 after; it is known
                at the close of the third bar after). BULL after a 1H close
                above the last confirmed pivot high, BEAR after a close below
                the last confirmed pivot low. Longs only in BULL, shorts only
                in BEAR.
  2 LIQUIDITY   15m pools against the bias. For longs, sell-side pools:
                15m pivot lows (k = 3) confirmed in the last 5 days and not
                traded through since; the previous broker day's low; today's
                Asia-session low (00:00-07:00 UTC), from 07:00 UTC. Mirror
                for shorts. A pool traded through is gone either way.
  3 SWEEP       a 15m bar trades through one or more pools and CLOSES back
                inside all of them - the stops are taken, the move rejected.
  4 CONFIRM     5m change of character. The reference is the last confirmed
                5m pivot high (k = 2) above the sweep bar's close. Within 12
                5m bars (one hour) of the sweep, a 5m close above it is the
                signal; a 5m close below the sweep low first cancels it.
  5 FILTERS     the signal bar closes 07:00-20:00 UTC (London and New York);
                the 1H bias agrees; the stop is 0.5-3.0 x ATR14(5m) from the
                fill; one position at a time; one trade per sweep.
  6 ORDERS      market at the next minute's open after the signal bar: buys
                at the ask, both sides +3 points slippage. Stop beyond the
                lowest low from the sweep to the signal, 0.1 ATR(5m) further
                (for shorts the highest high, plus that minute's spread, as
                the stop rides the ask). Target 2R. Time stop after 8 hours.
  7 COSTS       gold: the spread recorded on each 1m bar, floored at 10
                points; USDJPY: 5 points flat (its recorded spreads are
                unusable - a flat 50 in 2018-2020, then mostly zeros). The
                broker's commission, $3 per lot per side, in price. 3 points
                of slippage on the entry and on stop and time exits; the
                target is a limit and does not slip. Fills walk the 1m path
                by the lab broker's own rule (server/forecast/labels.first_
                touch); a stop and a target in one minute go to the stop
                when the path cannot tell them apart.

VARIANT v2, decided AFTER v1's result was seen - weaker evidence for that.
                v1's stop band (0.5-3.0 x ATR14 of the 5m) removed 636 of the
                921 signals that passed every other rule: the stop sits beyond
                a 15m sweep wick, several 5m ATRs away by construction. v2
                measures the same band in ATR14 of the 15m, the sweep's own
                timeframe. Nothing else changes.

VARIANT v3, asked for after v1 and v2 - the entry at the retracement.
                v1's rules with one change: instead of buying the signal
                close, a limit order halfway back from the signal close to
                the stop anchor (the 50% retracement of the move off the
                sweep), valid for 60 minutes; unfilled, it lapses. Stop,
                target (2R from the fill), time stop and costs unchanged; the
                limit fill pays the spread but no slippage. The stop band is
                v1's, the one fixed in advance - the halved stop distance of a
                retracement entry answers what v2 changed it for.

VARIANT v4, asked for after v3 - v3 plus a required displacement. After the
                sweep, a 5m candle whose body is at least 1.0 x ATR14(5m) in
                the trade's direction, leaving a fair value gap (the next bar
                clear of the bar before it). The signal is the first 5m close,
                within the hour, by which both the change of character and
                the displacement have happened. Everything else is v3's.

LADDERS (added 2026-09-27 for the forward test): the same rules one rung
                up or down. Entry timeframe -> sweep (pool) timeframe -> bias:
                1m -> 5m -> 15m, 3m -> 15m -> 1h, 5m -> 15m -> 1h (the one
                tested above), 15m -> 1h -> 4h, 30m -> 2h -> 4h, 1h -> 4h ->
                1d. Every time rule is counted in the ENTRY timeframe's bars,
                as on 5m: confirmation 12 bars, the limit order 12 bars, the
                time stop 96 bars; pools are kept 480 of their own bars. The
                5m ladder is exactly the rules above.

THE TEST, also fixed in advance
  - 2018 to 2026 (to the last bar on disk), per year, gold and USDJPY. No
    year was used to choose anything, so every year is out of sample.
  - Results in R net of every cost: R = the distance from the fill to the
    stop.
  - PROFITABLE only if all three hold, per symbol: net R per trade > 0 in
    each of 2024, 2025 and 2026; the 2018-2026 mean's 90% interval (day-
    block bootstrap) is above 0; and it beats the 95th percentile of a
    random-entry baseline.
  - The BASELINE keeps everything but the liquidity and structure: random
    5m closes in the same sessions, on the side the 1H bias gives, with the
    framework's own stop sizes (in ATR), the same 2R target and 8-hour stop,
    one position at a time, as many trades per year - 50 seeds. It asks
    whether the sweep and the change of character add anything to the
    trend bias alone.
  - A cost stress: 10 more points of spread on gold, 3 on USDJPY.

Read-only: it reads data/ and writes runs/research/mtf_liquidity/<run>/.

    python tools/research_mtf_liquidity.py                      v1, gold and USDJPY
    python tools/research_mtf_liquidity.py --variant v2
    python tools/research_mtf_liquidity.py --symbols XAUUSD.a
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.datafeed import load_disk                                   # noqa: E402
from server.engine.indicators import atr                                 # noqa: E402
from server.forecast.labels import NEVER, first_touch                    # noqa: E402
from server.forecast.timebase import broker_to_utc                       # noqa: E402

MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
TF_MS = {'1m': MIN, '3m': 3 * MIN, '5m': 5 * MIN, '15m': 15 * MIN, '30m': 30 * MIN, '1h': HOUR,
         '2h': 2 * HOUR, '4h': 4 * HOUR, '1d': DAY}
# entry timeframe -> (pool / sweep timeframe, bias timeframe)
LADDERS = {'1m': ('5m', '15m'), '3m': ('15m', '1h'), '5m': ('15m', '1h'), '15m': ('1h', '4h'),
           '30m': ('2h', '4h'), '1h': ('4h', '1d')}
RULES = {
    'bias_tf': '1h', 'bias_k': 3,
    'pool_tf': '15m', 'pool_k': 3, 'pool_days': 5, 'asia_utc': (0, 7),
    'entry_tf': '5m', 'entry_k': 2, 'confirm_bars': 12,
    'session_utc': (7, 20),
    'stop_buffer_atr': 0.1, 'stop_atr_min': 0.5, 'stop_atr_max': 3.0, 'stop_band_tf': '5m',
    'entry': 'market', 'limit_frac': 0.5, 'limit_valid_min': 60,
    'displacement': False, 'disp_body_atr': 1.0,
    'target_r': 2.0, 'max_hold_min': 480, 'slip_points': 3.0,
}
RULES_DEFAULT = dict(RULES)        # the base rules, before a variant or a ladder changes them
COSTS = {'XAUUSD.a': {'mode': 'recorded', 'floor': 10.0, 'stress': 10.0},
         'USDJPY.a': {'mode': 'fixed', 'points': 5.0, 'stress': 3.0}}
FIRST_YEAR, WARMUP_YEAR = 2018, 2017
BASELINE_SEEDS = 50
# stop_band_tf: 'entry' = the entry timeframe's ATR, 'pool' = the sweep timeframe's
VARIANTS = {'v1': {'stop_band_tf': 'entry'}, 'v2': {'stop_band_tf': 'pool'},
            'v3': {'stop_band_tf': 'entry', 'entry': 'limit'},
            'v4': {'stop_band_tf': 'entry', 'entry': 'limit', 'displacement': True}}


def use_ladder(entry_tf: str) -> None:
    """Set the timeframes and scale every time rule to the entry timeframe's bars."""
    pool_tf, bias_tf = LADDERS[entry_tf]
    em, pm = TF_MS[entry_tf] // MIN, TF_MS[pool_tf] // MIN
    RULES.update(entry_tf=entry_tf, pool_tf=pool_tf, bias_tf=bias_tf,
                 limit_valid_min=12 * em, max_hold_min=96 * em, pool_days=480 * pm / 1440)
OUT = ROOT / 'runs' / 'research' / 'mtf_liquidity'


# --------------------------------------------------------------------------- #
# market structure                                                            #
# --------------------------------------------------------------------------- #
def pivots(x: np.ndarray, k: int, high: bool) -> np.ndarray:
    """Fractal pivots: beyond the k bars before (strictly) and not beyond by the k after. Known at bar i+k."""
    n = x.size
    if n < 2 * k + 1:
        return np.zeros(0, dtype=np.int64)
    core = np.arange(k, n - k)
    left = np.stack([x[core - j] for j in range(1, k + 1)])
    right = np.stack([x[core + j] for j in range(1, k + 1)])
    if high:
        sel = (x[core] > left.max(0)) & (x[core] >= right.max(0))
    else:
        sel = (x[core] < left.min(0)) & (x[core] <= right.min(0))
    return core[sel]


def bias_1h(s) -> np.ndarray:
    """+1 / -1 / 0 at the close of each 1H bar: the side of the last structure break (rule 1)."""
    k = RULES['bias_k']
    ph, pl = pivots(s.h, k, True), pivots(s.l, k, False)
    known_h = {int(i) + k: float(s.h[i]) for i in ph}
    known_l = {int(i) + k: float(s.l[i]) for i in pl}
    out = np.zeros(s.c.size, dtype=np.int8)
    last_h = last_l = np.nan
    st = 0
    for j in range(s.c.size):
        if j in known_h:
            last_h = known_h[j]
        if j in known_l:
            last_l = known_l[j]
        if s.c[j] > last_h:
            st = 1
        elif s.c[j] < last_l:
            st = -1
        out[j] = st
    return out


# --------------------------------------------------------------------------- #
# liquidity and sweeps (15m)                                                  #
# --------------------------------------------------------------------------- #
def sweeps(s15, utc_open: np.ndarray) -> list:
    """
    Every 15m sweep (rules 2-3): (side, close_ms, extreme, close, bar index).
    Pools are checked against a bar only if they were known before it opened.
    """
    k, keep = RULES['pool_k'], int(RULES['pool_days'] * DAY)
    tf = TF_MS[RULES['pool_tf']]
    n = s15.c.size
    ph, pl = pivots(s15.h, k, True), pivots(s15.l, k, False)
    add_h, add_l = {}, {}
    for i in ph:
        add_h.setdefault(int(i) + k, []).append(float(s15.h[i]))
    for i in pl:
        add_l.setdefault(int(i) + k, []).append(float(s15.l[i]))
    bday = s15.t // DAY                                       # broker day (FX day)
    uday = utc_open // DAY
    uhour = (utc_open % DAY) // HOUR
    a0, a1 = RULES['asia_utc']
    lows, highs = [], []                                      # [level, expires_ms]
    out = []
    day_lo = day_hi = None
    prev_lo = prev_hi = None
    asia_lo = asia_hi = None
    asia_day = None
    asia_done = False
    for j in range(n):
        t_open = int(s15.t[j])
        # -- pools that became known at the previous bar's close or at this open
        if j == 0 or bday[j] != bday[j - 1]:
            if day_lo is not None:
                prev_lo, prev_hi = day_lo, day_hi
                end = (int(bday[j]) + 1) * DAY
                lows.append([prev_lo, end])
                highs.append([prev_hi, end])
            day_lo, day_hi = float(s15.l[j]), float(s15.h[j])
        if uday[j] != asia_day:
            asia_day, asia_lo, asia_hi, asia_done = uday[j], None, None, False
        if not asia_done and uhour[j] >= a1 and asia_lo is not None:
            end = int(t_open - (utc_open[j] - (uday[j] * DAY + RULES['session_utc'][1] * HOUR)))
            lows.append([asia_lo, end])
            highs.append([asia_hi, end])
            asia_done = True
        lows = [p for p in lows if p[1] > t_open]
        highs = [p for p in highs if p[1] > t_open]
        lo, hi, cl = float(s15.l[j]), float(s15.h[j]), float(s15.c[j])
        # -- sell-side taken: a long setup if the close is back above all of them
        took = [p for p in lows if lo < p[0]]
        if took:
            lows = [p for p in lows if not lo < p[0]]
            if cl > max(p[0] for p in took):
                out.append((1, t_open + tf, lo, cl, j))
        took = [p for p in highs if hi > p[0]]
        if took:
            highs = [p for p in highs if not hi > p[0]]
            if cl < min(p[0] for p in took):
                out.append((-1, t_open + tf, hi, cl, j))
        # -- known at this bar's close: pivots confirmed now, the day's range, Asia
        close_ms = t_open + tf
        for lv in add_l.get(j, ()):
            lows.append([lv, close_ms + keep])
        for lv in add_h.get(j, ()):
            highs.append([lv, close_ms + keep])
        day_lo, day_hi = min(day_lo, lo), max(day_hi, hi)
        if a0 <= uhour[j] < a1:
            asia_lo = lo if asia_lo is None else min(asia_lo, lo)
            asia_hi = hi if asia_hi is None else max(asia_hi, hi)
    return out


# --------------------------------------------------------------------------- #
# the 5m confirmation (rule 4) and filters (rule 5)                           #
# --------------------------------------------------------------------------- #
class Frame:
    """The 5m series with what the rules read from it, and the 1H bias laid onto its closes."""

    def __init__(self, s5, s1h, s15):
        # (named for the 5m ladder: s5 is the entry series, s15 the pool, s1h the bias)
        self.s = s5
        self.step = TF_MS[RULES['entry_tf']]
        self.close_ms = s5.t + self.step
        self.atr = atr(s5.h, s5.l, s5.c, 14)
        # The last closed pool bar's ATR at each entry close, for v2's stop band.
        a15 = atr(s15.h, s15.l, s15.c, 14)
        i15 = np.searchsorted(s15.t + TF_MS[RULES['pool_tf']], self.close_ms, 'right') - 1
        atr15 = np.where(i15 >= 0, a15[np.clip(i15, 0, None)], np.nan)
        self.band = atr15 if RULES['stop_band_tf'] == 'pool' else self.atr
        k = RULES['entry_k']
        ph, pl = pivots(s5.h, k, True), pivots(s5.l, k, False)
        self.ph_known = self.close_ms[ph + k]                  # when each pivot became known
        self.ph_price = s5.h[ph]
        self.pl_known = self.close_ms[pl + k]
        self.pl_price = s5.l[pl]
        b = bias_1h(s1h)
        idx = np.searchsorted(s1h.t + TF_MS[RULES['bias_tf']], self.close_ms, 'right') - 1
        self.bias = np.where(idx >= 0, b[np.clip(idx, 0, None)], 0).astype(np.int8)
        self.utc_hour = (broker_to_utc(self.close_ms) % DAY) // HOUR
        self.year = self.close_ms.astype('datetime64[ms]').astype('datetime64[Y]').astype(int) + 1970

    def reference(self, side: int, at_ms: int, beyond: float):
        """The last confirmed 5m pivot beyond `beyond` known by at_ms, within 2 days (rule 4)."""
        known, price = (self.ph_known, self.ph_price) if side > 0 else (self.pl_known, self.pl_price)
        j = int(np.searchsorted(known, at_ms, 'right')) - 1
        while j >= 0 and known[j] >= at_ms - 2 * DAY:
            p = float(price[j])
            if (p > beyond) if side > 0 else (p < beyond):
                return p
            j -= 1
        return None

    def signal(self, ev) -> dict | None:
        """The change of character after one sweep, or None (cancelled, expired, no reference)."""
        side, t_sweep, extreme, close15, _ = ev
        ref = self.reference(side, t_sweep, close15)
        if ref is None:
            return None
        s = self.s
        i = int(np.searchsorted(s.t, t_sweep, 'left'))
        worst = extreme
        horizon = t_sweep + RULES['confirm_bars'] * self.step      # TIME, not bars (an hour on 5m)
        need_disp = RULES['displacement']
        choch = disp = False
        for j in range(i, min(i + RULES['confirm_bars'], s.t.size)):
            if self.close_ms[j] > horizon:                          # the market shut in between
                return None
            if side > 0:
                if s.c[j] < extreme:
                    return None
                worst = min(worst, float(s.l[j]))
                choch = choch or s.c[j] > ref
            else:
                if s.c[j] > extreme:
                    return None
                worst = max(worst, float(s.h[j]))
                choch = choch or s.c[j] < ref
            # v4: a displacement candle after the sweep - bar j-1, its body at least
            # disp_body_atr x ATR in the trade's direction - that left a fair value
            # gap: bar j clear of bar j-2 (above it for longs, below for shorts).
            if need_disp and not disp and j - 1 >= i and j - 2 >= 0:
                d = j - 1
                body = (s.c[d] - s.o[d]) * side
                gap = (s.l[j] > s.h[j - 2]) if side > 0 else (s.h[j] < s.l[j - 2])
                disp = bool(body >= RULES['disp_body_atr'] * self.atr[d] and gap)
            if choch and (disp or not need_disp):
                return {'side': side, 'i': j, 'close_ms': int(self.close_ms[j]), 'anchor': worst,
                        'ref': ref, 'sweep_ms': t_sweep}
        return None

    def passes(self, sig: dict) -> bool:
        j = sig['i']
        h0, h1 = RULES['session_utc']
        return h0 <= int(self.utc_hour[j]) < h1 and int(self.bias[j]) == sig['side'] \
            and np.isfinite(self.atr[j]) and self.atr[j] > 0 and np.isfinite(self.band[j])


# --------------------------------------------------------------------------- #
# fills on the 1m path (rules 6-7)                                            #
# --------------------------------------------------------------------------- #
class Book:
    """Trades walked on the 1m path, with every cost, by the lab broker's path rule."""

    def __init__(self, symbol: str, m1, spec: dict, extra_spread: float = 0.0):
        self.m1 = m1
        self.point = float(spec.get('point') or 0.01)
        self.digits = int(spec.get('digits') or 2)
        c = COSTS[symbol]
        if c['mode'] == 'recorded':
            sp = m1.spread.astype(np.float64) if m1.spread is not None else np.zeros(m1.t.size)
            sp = np.maximum(sp, c['floor'])
        else:
            sp = np.full(m1.t.size, c['points'])
        self.spread = (sp + extra_spread) * self.point              # price units, per minute
        self.slip = RULES['slip_points'] * self.point
        tick_value = float(spec.get('tick_value') or 1.0)
        tick_size = float(spec.get('tick_size') or self.point)
        per_side = float(spec.get('commission_per_lot_side') or 0.0) / tick_value * tick_size
        self.commission = 2 * per_side                               # round trip, price units
        self.orders = self.unfilled = self.rejected = 0             # v3's limit orders

    def _walk(self, side: int, i0: int, fill: float, stop: float, target: float):
        """Exits from minute i0 for up to max_hold_min: (last minute index, exit price, how)."""
        m = self.m1
        i1 = int(np.searchsorted(m.t, m.t[i0] + RULES['max_hold_min'] * MIN, 'left'))
        sl_ = slice(i0, max(i1, i0 + 1))
        O, H, L, C = (a[sl_][None, :].astype(np.float64) for a in (m.o, m.h, m.l, m.c))
        sp = self.spread[sl_][None, :]
        if side > 0:                                                 # exits on the bid
            t_tp = first_touch(O, H, L, C, np.array([[target]]), True)[0]
            t_sl = first_touch(O, H, L, C, np.array([[stop]]), False)[0]
        else:                                                        # exits on the ask
            t_tp = first_touch(O, H, L, C, target - sp, False)[0]
            t_sl = first_touch(O, H, L, C, stop - sp, True)[0]
        if t_sl != NEVER and t_sl <= t_tp:                           # a tie goes to the stop
            j, gap = int(t_sl) // 4, int(t_sl) % 4 == 0
            px = float(O[0, j]) if gap else stop - (float(sp[0, j]) if side < 0 else 0.0)
            exit_px = (px - self.slip) if side > 0 else (px + float(sp[0, j]) + self.slip)
            return i0 + j, exit_px, 'stop'
        if t_tp != NEVER:
            return i0 + int(t_tp) // 4, target, 'target'
        j = O.shape[1] - 1
        exit_px = (float(C[0, j]) - self.slip) if side > 0 else \
            (float(C[0, j]) + float(sp[0, j]) + self.slip)
        return i0 + j, exit_px, 'time'

    def _result(self, side, entry_i, exit_i, fill, stop, target, exit_px, how, risk, atr_now):
        gross = (exit_px - fill) * side
        net = gross - self.commission
        return {'side': side, 'entry_ms': int(self.m1.t[entry_i]),
                'exit_ms': int(self.m1.t[exit_i]) + MIN,
                'fill': fill, 'stop': stop, 'target': target, 'exit': exit_px, 'how': how,
                'risk': risk, 'stop_atr': risk / atr_now, 'r_gross': gross / risk,
                'r_net': net / risk}

    def trade(self, side: int, at_ms: int, anchor: float, atr_now: float,
              band: float = None) -> dict | None:
        """Market entry at the next minute's open (v1, v2)."""
        m = self.m1
        i0 = int(np.searchsorted(m.t, at_ms, 'left'))
        if i0 >= m.t.size or m.t[i0] - at_ms > 15 * MIN:             # no market to fill in
            return None
        o, sp0 = float(m.o[i0]), float(self.spread[i0])
        buf = RULES['stop_buffer_atr'] * atr_now
        if side > 0:
            fill, stop = o + sp0 + self.slip, anchor - buf           # buy at the ask; stop on the bid
        else:
            fill, stop = o - self.slip, anchor + sp0 + buf           # sell at the bid; stop on the ask
        risk = (fill - stop) * side
        band = atr_now if band is None else band               # v1: the 5m ATR; v2: the 15m
        if not (RULES['stop_atr_min'] * band <= risk <= RULES['stop_atr_max'] * band):
            return None
        target = fill + side * RULES['target_r'] * risk
        k, exit_px, how = self._walk(side, i0, fill, stop, target)
        return self._result(side, i0, k, fill, stop, target, exit_px, how, risk, atr_now)

    def trade_limit(self, side: int, at_ms: int, anchor: float, close_sig: float,
                    atr_now: float, band: float = None) -> dict | None:
        """
        v3: a limit order at the retracement - limit_frac of the way back from the
        signal close to the stop anchor - valid limit_valid_min minutes. It fills
        when the ask (buys) or bid (sells) trades at it, at the limit, or at the
        open if the minute gaps through. The fill minute is judged pessimistically:
        a stop inside it counts; a target inside it does not.
        """
        m = self.m1
        self.orders += 1
        i0 = int(np.searchsorted(m.t, at_ms, 'left'))
        if i0 >= m.t.size or m.t[i0] - at_ms > 15 * MIN:
            return None
        limit = close_sig + RULES['limit_frac'] * (anchor - close_sig)
        iw = int(np.searchsorted(m.t, m.t[i0] + RULES['limit_valid_min'] * MIN, 'left'))
        sl_ = slice(i0, max(iw, i0 + 1))
        O, H, L, C = (a[sl_][None, :].astype(np.float64) for a in (m.o, m.h, m.l, m.c))
        sp = self.spread[sl_][None, :]
        if side > 0:
            t_f = first_touch(O, H, L, C, limit - sp, False)[0]      # ask <= limit
        else:
            t_f = first_touch(O, H, L, C, np.array([[limit]]), True)[0]   # bid >= limit
        if t_f == NEVER:
            self.unfilled += 1
            return None
        j, gap = int(t_f) // 4, int(t_f) % 4 == 0
        k = i0 + j
        buf = RULES['stop_buffer_atr'] * atr_now
        spk = float(self.spread[k])
        if side > 0:
            fill = (float(m.o[k]) + spk) if gap else limit
            stop = anchor - buf
        else:
            fill = float(m.o[k]) if gap else limit
            stop = anchor + spk + buf
        risk = (fill - stop) * side
        band = atr_now if band is None else band
        if not (RULES['stop_atr_min'] * band <= risk <= RULES['stop_atr_max'] * band):
            self.rejected += 1
            return None
        target = fill + side * RULES['target_r'] * risk
        stopped_in_fill_minute = (float(m.l[k]) <= stop) if side > 0 else \
            (float(m.h[k]) + spk >= stop)
        if stopped_in_fill_minute:
            exit_px = (stop - self.slip) if side > 0 else (stop + self.slip)
            return self._result(side, k, k, fill, stop, target, exit_px, 'stop', risk, atr_now)
        if k + 1 >= m.t.size:
            return None
        e, exit_px, how = self._walk(side, k + 1, fill, stop, target)
        return self._result(side, k, e, fill, stop, target, exit_px, how, risk, atr_now)


# --------------------------------------------------------------------------- #
# the runs                                                                    #
# --------------------------------------------------------------------------- #
def load(symbol: str, tf: str):
    years = list(range(WARMUP_YEAR if tf != '1m' else FIRST_YEAR, datetime.now(timezone.utc).year + 1))
    return load_disk(symbol, tf, years)


def framework(fr: Frame, s15, book: Book, start_ms: int) -> list:
    """Every signal, then one position at a time in the order they fired."""
    utc15 = broker_to_utc(s15.t)
    sigs = []
    for ev in sweeps(s15, utc15):
        if ev[1] < start_ms:
            continue
        sig = fr.signal(ev)
        if sig and fr.passes(sig):
            sigs.append(sig)
    sigs.sort(key=lambda x: x['close_ms'])
    trades, free_at, used = [], 0, set()
    for sig in sigs:
        if sig['close_ms'] < free_at or sig['sweep_ms'] in used:
            continue
        if RULES['entry'] == 'limit':
            tr = book.trade_limit(sig['side'], sig['close_ms'], sig['anchor'],
                                  float(fr.s.c[sig['i']]), float(fr.atr[sig['i']]),
                                  float(fr.band[sig['i']]))
        else:
            tr = book.trade(sig['side'], sig['close_ms'], sig['anchor'], float(fr.atr[sig['i']]),
                            float(fr.band[sig['i']]))
        if tr is None:
            continue
        used.add(sig['sweep_ms'])
        tr['sweep_ms'] = sig['sweep_ms']
        trades.append(tr)
        free_at = tr['exit_ms']
    return trades


def baseline(fr: Frame, book: Book, trades: list, start_ms: int, seed: int) -> list:
    """Random 5m closes on the bias side, the framework's stop sizes, as many trades per year."""
    g = np.random.default_rng(seed)
    h0, h1 = RULES['session_utc']
    ok = (fr.close_ms >= start_ms) & (fr.utc_hour >= h0) & (fr.utc_hour < h1) & \
        (fr.bias != 0) & np.isfinite(fr.atr) & (fr.atr > 0) & np.isfinite(fr.band)
    years = fr.year
    sizes = np.array([t['stop_atr'] for t in trades]) if trades else np.array([1.0])
    want = {}
    for t in trades:
        y = datetime.fromtimestamp(t['entry_ms'] / 1000, timezone.utc).year
        want[y] = want.get(y, 0) + 1
    out = []
    for y, n in sorted(want.items()):
        cand = np.nonzero(ok & (years == y))[0]
        if not cand.size:
            continue
        free_at, got, tries = 0, 0, 0
        for j in np.sort(g.choice(cand, size=min(cand.size, n * 6), replace=False)):
            if got >= n or tries > n * 20:
                break
            tries += 1
            if fr.close_ms[j] < free_at:
                continue
            side = int(fr.bias[j])
            a = float(fr.atr[j])
            k = float(g.choice(sizes))
            # the stop anchor that gives this stop size, measured from the bar's close
            anchor = float(fr.s.c[j]) - side * (k - RULES['stop_buffer_atr']) * a
            tr = book.trade(side, int(fr.close_ms[j]), anchor, a, float(fr.band[j]))
            if tr is None:
                continue
            out.append(tr)
            got += 1
            free_at = tr['exit_ms']
    return out


# --------------------------------------------------------------------------- #
# statistics                                                                  #
# --------------------------------------------------------------------------- #
def year_of(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).year


def block_ci(trades: list, reps: int = 2000, seed: int = 7) -> tuple:
    """90% interval of the mean net R, resampling whole broker days."""
    if len(trades) < 10:
        return (np.nan, np.nan)
    days = {}
    for t in trades:
        days.setdefault(t['entry_ms'] // DAY, []).append(t['r_net'])
    groups = list(days.values())
    g = np.random.default_rng(seed)
    means = []
    for _ in range(reps):
        pick = g.integers(0, len(groups), len(groups))
        vals = [v for i in pick for v in groups[i]]
        means.append(np.mean(vals))
    return float(np.percentile(means, 5)), float(np.percentile(means, 95))


def stats(trades: list) -> dict:
    if not trades:
        return {'n': 0}
    r = np.array([t['r_net'] for t in trades])
    rg = np.array([t['r_gross'] for t in trades])
    eq = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(np.r_[0, eq])[1:] - eq)) if eq.size else 0.0
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    how = [t['how'] for t in trades]
    return {'n': len(trades), 'mean': float(r.mean()), 'gross': float(rg.mean()),
            'total': float(r.sum()), 'win': float(np.mean([h == 'target' for h in how])),
            'timed': float(np.mean([h == 'time' for h in how])),
            'pf': float(wins / losses) if losses > 0 else float('inf'), 'dd': dd}


# --------------------------------------------------------------------------- #
# report                                                                      #
# --------------------------------------------------------------------------- #
def run_symbol(symbol: str, log) -> dict:
    t0 = time.perf_counter()
    from server.lab.data import spec as lab_spec
    spec = lab_spec(symbol)
    s1h, s15, s5, m1 = (load(symbol, tf) for tf in (RULES['bias_tf'], RULES['pool_tf'],
                                                    RULES['entry_tf'], '1m'))
    log(f"{symbol} {RULES['entry_tf']} ladder: loaded bias {s1h.t.size:,}  pool {s15.t.size:,}  "
        f'entry {s5.t.size:,}  1m {m1.t.size:,} bars ({time.perf_counter() - t0:.0f}s)')
    start_ms = int(datetime(FIRST_YEAR, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    fr = Frame(s5, s1h, s15)
    book = Book(symbol, m1, spec)
    trades = framework(fr, s15, book, start_ms)
    log(f'{symbol}: {len(trades):,} framework trades ({time.perf_counter() - t0:.0f}s)')
    fills = (book.orders, book.unfilled, book.rejected)
    stressed = framework(fr, s15, Book(symbol, m1, spec, COSTS[symbol]['stress']), start_ms)
    base = [baseline(fr, book, trades, start_ms, seed) for seed in range(BASELINE_SEEDS)]
    log(f'{symbol}: baseline {BASELINE_SEEDS} seeds done ({time.perf_counter() - t0:.0f}s)')
    years = sorted({year_of(t['entry_ms']) for t in trades})
    by_year = {y: stats([t for t in trades if year_of(t['entry_ms']) == y]) for y in years}
    base_mean = np.array([np.mean([t['r_net'] for t in b]) if b else np.nan for b in base])
    base_year = {y: np.array([np.mean([t['r_net'] for t in b if year_of(t['entry_ms']) == y])
                              for b in base if any(year_of(t['entry_ms']) == y for t in b)])
                 for y in years}
    ci = block_ci(trades)
    last3 = [y for y in (2024, 2025, 2026) if y in by_year]
    every = all(by_year[y]['mean'] > 0 for y in last3) and len(last3) == 3
    p95 = float(np.nanpercentile(base_mean, 95)) if base_mean.size else np.nan
    whole = stats(trades)
    verdict = every and ci[0] > 0 and whole.get('mean', -1) > p95
    why = []
    if not every:
        why.append('not positive in every one of 2024-2026')
    if not ci[0] > 0:
        why.append('the 2018-2026 interval includes zero or less')
    if not whole.get('mean', -1) > p95:
        why.append('no better than random entries on the bias side')
    return {'symbol': symbol, 'trades': trades, 'stressed': stressed, 'by_year': by_year,
            'whole': whole, 'ci': ci, 'base_mean': base_mean, 'base_year': base_year,
            'p95': p95, 'verdict': verdict, 'why': why, 'fills': fills,
            'stress': stats(stressed), 'stress_year': {y: stats([t for t in stressed
                                                                  if year_of(t['entry_ms']) == y])
                                                       for y in years}}


def fmt_report(res: list, run_id: str, variant: str) -> str:
    out = []
    w = out.append
    w(f'MTF LIQUIDITY + STRUCTURE (MTF-LS {variant}) - fixed rules, backtested        run {run_id}')
    if variant.startswith('v3'):
        w('VARIANT v3: limit entry at the 50% retracement of the move off the sweep, valid 60 min; '
          "otherwise v1's rules.")
    elif variant.startswith('v4'):
        w('VARIANT v4: v3 plus a required displacement - a 5m body >= 1.0 ATR after the sweep that '
          'left a fair value gap.')
    elif not variant.startswith('v1'):
        w(f"VARIANT {variant}: stop band 0.5-3.0 x ATR14({RULES['stop_band_tf']}) - decided after v1's "
          'result was seen (see the tool header).')
    w('Bias 1H structure; sell-/buy-side 15m liquidity swept and closed back inside; 5m change of')
    w('character within an hour; market entry, stop beyond the sweep, 2R target, 8-hour time stop.')
    w('Every cost charged: spread, commission, slippage; fills on the 1m path. R = fill to stop.')
    w('Rules fixed before any result - see the tool header. Nothing tuned; every year is out of sample.')
    for r in res:
        w('')
        w('=' * 96)
        w(f"{r['symbol']}")
        w('=' * 96)
        w(f"{'year':<6}{'trades':>7}{'win%':>7}{'gross R':>9}{'net R/tr':>10}{'total R':>9}{'PF':>6}"
          f"{'maxDD R':>9}{'timed%':>8}{'random p5/p50/p95':>22}{'+stress':>9}")
        for y, s in r['by_year'].items():
            b = r['base_year'].get(y)
            bb = (f"{np.nanpercentile(b, 5):+.3f}/{np.nanpercentile(b, 50):+.3f}/"
                  f"{np.nanpercentile(b, 95):+.3f}") if b is not None and b.size else '-'
            st = r['stress_year'].get(y, {})
            w(f"{y:<6}{s['n']:>7}{100 * s['win']:>6.1f}%{s['gross']:>+9.3f}{s['mean']:>+10.3f}"
              f"{s['total']:>+9.1f}{s['pf']:>6.2f}{s['dd']:>9.1f}{100 * s['timed']:>7.1f}%{bb:>22}"
              f"{st.get('mean', float('nan')):>+9.3f}")
        s = r['whole']
        b = r['base_mean']
        w(f"{'all':<6}{s['n']:>7}{100 * s['win']:>6.1f}%{s['gross']:>+9.3f}{s['mean']:>+10.3f}"
          f"{s['total']:>+9.1f}{s['pf']:>6.2f}{s['dd']:>9.1f}{100 * s['timed']:>7.1f}%"
          f"{np.nanpercentile(b, 5):>+8.3f}/{np.nanpercentile(b, 50):+.3f}/{np.nanpercentile(b, 95):+.3f}"
          f"{r['stress'].get('mean', float('nan')):>+9.3f}")
        lo, hi = r['ci']
        w(f"2018-2026 net R per trade {s['mean']:+.3f}, 90% interval [{lo:+.3f}, {hi:+.3f}] (day-block bootstrap)")
        w(f"random entries on the bias side, same stops and exits: 95th percentile {r['p95']:+.3f} R per trade")
        if RULES['entry'] == 'limit':
            o, u, rj = r['fills']
            w(f"limit orders {o:,}: unfilled in the hour {u:,}, stop outside the band {rj:,}, "
              f"traded {s['n']:,} (one position at a time)")
        w(f"VERDICT: {'PROFITABLE' if r['verdict'] else 'NOT PROFITABLE'}"
          + ('' if r['verdict'] else ' - ' + '; '.join(r['why'])))
    w('')
    w('Columns: win% = target reached; gross R before costs; net R per trade after every cost;')
    w('PF = profit factor; maxDD in R; timed% = closed by the 8-hour stop; random = the same')
    w('year from 50 random-entry runs (p5/p50/p95); +stress = net R with 10 more points of spread')
    w('on gold, 3 on USDJPY. Measurements on history, not trading advice.')
    return '\n'.join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--symbols', default='XAUUSD.a,USDJPY.a')
    ap.add_argument('--variant', default='v1', choices=sorted(VARIANTS))
    ap.add_argument('--ladder', default='5m', choices=list(LADDERS),
                    help='the entry timeframe; the sweep and bias timeframes follow (LADDERS)')
    a = ap.parse_args()
    RULES.update(VARIANTS[a.variant])
    use_ladder(a.ladder)
    run_id = datetime.now().strftime('%Y%m%d-%H%M%S') + f'-{a.variant}' + \
        ('' if a.ladder == '5m' else f'-{a.ladder}')
    d = OUT / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / 'rules.json').write_text(json.dumps({'rules': RULES, 'costs': COSTS,
                                              'first_year': FIRST_YEAR, 'variant': a.variant,
                                              'baseline_seeds': BASELINE_SEEDS}, indent=1),
                                  encoding='utf-8')
    res = [run_symbol(s, lambda m: print(m, flush=True)) for s in a.symbols.split(',') if s]
    for r in res:
        with open(d / f"trades_{r['symbol']}.csv", 'w', newline='', encoding='utf-8') as fh:
            cols = ['side', 'entry_ms', 'exit_ms', 'fill', 'stop', 'target', 'exit', 'how', 'risk',
                    'stop_atr', 'r_gross', 'r_net', 'sweep_ms']
            wr = csv.DictWriter(fh, fieldnames=cols)
            wr.writeheader()
            for t in r['trades']:
                wr.writerow({k: t.get(k) for k in cols})
    txt = fmt_report(res, run_id, a.variant + ('' if a.ladder == '5m' else
                                                f" {a.ladder}/{RULES['pool_tf']}/{RULES['bias_tf']}"))
    (d / 'report.txt').write_text(txt, encoding='utf-8')
    print(txt)
    print(f'\n-> {d.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
