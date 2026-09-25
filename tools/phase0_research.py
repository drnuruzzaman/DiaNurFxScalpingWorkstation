#!/usr/bin/env python
"""
tools/phase0_research.py - Phase 0: measure before redesigning TP and SL.

Read-only. Nothing live is touched and no setting changes.

The premise it is built around: XAUUSD is not a one-direction market. A 1H
uptrend is itself a sequence of impulses and corrections, and on 3m/5m/15m
those corrections are moves in their own right - often large enough to trade
in both directions. So before asking "where should TP go", this measures what
the price paths actually look like.

PART A - market anatomy (impulses and corrections)
    Price on 3m, 5m, 15m and 1H is cut into legs with an ATR zigzag (a leg ends
    when price reverses by LEG_ATR x ATR from its extreme). Each leg is labelled
    against the next timeframe up - 1H for the lower three, 4H for 1H:
        WITH     moving the same way as the higher-timeframe trend (impulse)
        COUNTER  moving against it (correction)
        RANGE    the higher timeframe has no trend
    For each: how many per day, how big (ATR and points), how long, how deep
    corrections run relative to the impulse before them, and how many are big
    enough to trade (>= TRADEABLE_ATR, roughly a 1 ATR stop plus 1R).

PART B - signal paths
    Every qualified signal in the three captured years (2026 in-sample, 2025
    out-of-sample, 2024 hold-out), walked forward from its real fill on actual
    bars, resolving the entry bar and every stop bar on 1-minute data:
        MFE     how far it ran in R before the stop (or the horizon)
        MAE     for trades that reached +1R, how far against they went first
        rescue  stopped out, then went on to +1R from entry anyway - the
                signature of a stop that sat inside the noise
        EV(k)   net expectancy of a fixed target at k R, fixed stop, time exit
                at the horizon, after spread + commission + slippage
    Broken down by setup, by direction vs the 1H trend, by session and regime.
    "Qualified" deliberately IGNORES the reward gate: that gate depends on TP2,
    which is the thing being redesigned, so it cannot be allowed to pick the
    sample.

Output: a full report at order_ledger/phase0_report.txt, one row per signal at
order_ledger/phase0_signals.csv (for Phase 1), and a summary on screen.

    python tools/phase0_research.py
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import exp_exits as X                                              # noqa: E402
from server.config import CONFIG, TF_SECONDS, session_quality      # noqa: E402
from server.datafeed import load_disk                              # noqa: E402
from server.engine.analysis import quick_trend                     # noqa: E402
from server.engine.indicators import atr as atr_fn                 # noqa: E402

SYMBOL = 'XAUUSD.a'
LABELS = [('is2026', '2026'), ('oos2025', '2025'), ('hold2024', '2024')]

LEG_ATR = 1.5            # a leg ends on a reversal of this many ATR
TRADEABLE_ATR = 2.0      # a leg this big fits a ~1 ATR stop plus 1R
HORIZON = 72             # bars (6h on 5m) a signal is followed for
DEDUP_BARS = 5           # one signal per setup+side per 5 bars, so a single
                         # move firing on consecutive bars counts once
R_LEVELS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0)
EV_LEVELS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
MIN_N = 30               # cells smaller than this are not reported
DAY_MS = 86_400_000

OUT_TXT = X.OUT / 'phase0_report.txt'
OUT_CSV = X.OUT / 'phase0_signals.csv'


# =========================================================================== #
# shared                                                                      #
# =========================================================================== #
def trend_track(series, window: int = 300):
    """
    The engine's own trend read (quick_trend) at every closed bar of `series`.

    Returns (close_times, states) with states +1 up / -1 down / 0 ranging, so
    any moment can be mapped to the trend of the last bar that had CLOSED by
    then - never the one still forming.
    """
    dur = TF_SECONDS_OF(series)
    closes, states = [], []
    for j in range(60, len(series)):
        w = series.slice(max(0, j + 1 - window), j + 1)
        s = (quick_trend(w) or {}).get('state', '')
        states.append(1 if 'up' in s else -1 if 'down' in s else 0)
        closes.append(int(series.t[j]) + dur)
    return np.array(closes, dtype=np.int64), np.array(states, dtype=np.int8)


def TF_SECONDS_OF(series) -> int:
    return int(TF_SECONDS[series.tf] * 1000)


def state_at(track, ts: int) -> int:
    closes, states = track
    k = int(np.searchsorted(closes, ts, 'right')) - 1
    return int(states[k]) if k >= 0 else 0


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float('nan')


# =========================================================================== #
# PART A - impulses and corrections                                           #
# =========================================================================== #
def zigzag(h, l, a, k: float) -> list:
    """Confirmed pivots (index, price, 'H'|'L') of an ATR-scaled zigzag."""
    n = len(h)
    if n < 20:
        return []
    piv = []
    hi, hi_i, lo, lo_i = h[0], 0, l[0], 0
    trend = 0
    for i in range(1, n):
        th = k * a[i] if a[i] > 0 else np.inf
        if trend == 0:
            if h[i] > hi:
                hi, hi_i = h[i], i
            if l[i] < lo:
                lo, lo_i = l[i], i
            if hi - lo >= th:
                if hi_i > lo_i:
                    trend = 1
                    piv.append((lo_i, lo, 'L'))
                else:
                    trend = -1
                    piv.append((hi_i, hi, 'H'))
        elif trend == 1:
            if h[i] > hi:
                hi, hi_i = h[i], i
            elif hi - l[i] >= th:
                piv.append((hi_i, hi, 'H'))
                trend, lo, lo_i = -1, l[i], i
        else:
            if l[i] < lo:
                lo, lo_i = l[i], i
            elif h[i] - lo >= th:
                piv.append((lo_i, lo, 'L'))
                trend, hi, hi_i = 1, h[i], i
    return piv


def anatomy(series, htf_track) -> dict:
    """Legs of `series`, each labelled WITH / COUNTER / RANGE against the HTF."""
    h, l, c, t = series.h, series.l, series.c, series.t
    a = atr_fn(h, l, c, 14)
    piv = zigzag(h, l, a, LEG_ATR)
    dur = TF_SECONDS_OF(series)
    legs = []
    for (i0, p0, k0), (i1, p1, _) in zip(piv, piv[1:]):
        up = k0 == 'L'
        ref = float(a[i0]) if a[i0] > 0 else float('nan')
        # Context is the HTF trend when the leg STARTED - what a trader
        # standing at that moment would have seen.
        st = state_at(htf_track, int(t[i0]) + dur)
        side = 1 if up else -1
        kind = 'RANGE' if st == 0 else ('WITH' if st == side else 'COUNTER')
        legs.append({'up': up, 'kind': kind, 'pts': abs(p1 - p0),
                     'atr': abs(p1 - p0) / ref if ref == ref else float('nan'),
                     'bars': i1 - i0, 'i0': i0})
    # Correction depth: a COUNTER leg against the WITH leg immediately before.
    for prev, cur in zip(legs, legs[1:]):
        if cur['kind'] == 'COUNTER' and prev['kind'] == 'WITH':
            cur['retrace'] = cur['pts'] / prev['pts'] if prev['pts'] > 0 else float('nan')
    days = max(1.0, (float(t[-1]) - float(t[0])) / DAY_MS * 5 / 7)   # trading days
    return {'legs': legs, 'days': days}


def anatomy_block(name, res) -> list:
    legs, days = res['legs'], res['days']
    lines = [f'  {name}   {len(legs)} legs over {days:.0f} trading days'
             f' ({len(legs) / days:.1f}/day)   leg = reversal of {LEG_ATR} ATR']
    lines.append(f'    {"":8} {"share":>6} {"per day":>8} {"size ATR p50/p75/p90":>22} '
                 f'{"points p50/p75":>15} {"bars p50":>9} {"tradeable":>10} {"retrace p50":>12} '
                 f'{"in 38-79%":>10} {">100%":>6}')
    for kind in ('WITH', 'COUNTER', 'RANGE'):
        g = [x for x in legs if x['kind'] == kind]
        if not g:
            continue
        s_atr = np.array([x['atr'] for x in g if x['atr'] == x['atr']])
        s_pts = np.array([x['pts'] for x in g])
        bars = np.array([x['bars'] for x in g])
        trad = float(np.mean(s_atr >= TRADEABLE_ATR)) * 100 if len(s_atr) else 0.0
        rr = np.array([x['retrace'] for x in g if x.get('retrace', float('nan')) == x.get('retrace')])
        rtxt = (f'{pct(rr, 50):>11.0%} {np.mean((rr >= .382) & (rr <= .786)):>10.0%} '
                f'{np.mean(rr > 1.0):>6.0%}') if len(rr) >= MIN_N else f'{"":>11} {"":>10} {"":>6}'
        lines.append(
            f'    {kind:<8} {len(g) / len(legs):>6.0%} {len(g) / days:>8.1f} '
            f'{pct(s_atr, 50):>7.1f} /{pct(s_atr, 75):>5.1f} /{pct(s_atr, 90):>5.1f}    '
            f'{pct(s_pts, 50):>6.1f} /{pct(s_pts, 75):>6.1f} {np.median(bars):>9.0f} '
            f'{trad:>9.0f}% {rtxt}')
    return lines


# =========================================================================== #
# PART B - signal paths                                                       #
# =========================================================================== #
def qualifies(c, spec) -> bool:
    """
    Would this signal have been acted on - WITHOUT the reward gate.

    The reward gate compares net R:R to TP2, and TP2 is what is being
    redesigned. Letting it choose the sample would bake the current target
    logic into the evidence used to replace it.
    """
    skip = {'reward', 'daily', 'exposure'}
    if any(v == 'BLOCK' for (nm, v, _) in c['gates'] if nm not in skip):
        return False
    pen = sum(p for (nm, v, p) in c['gates'] if v == 'WARN' and nm not in skip)
    return max(0, min(100, c['raw_conf'] - pen)) >= CONFIG.gates.min_confidence


def m1_range(m1, t0: int, t1: int):
    return int(np.searchsorted(m1.t, t0, 'left')), int(np.searchsorted(m1.t, t1, 'left'))


def walk(c, series, m1, spec):
    """Follow one signal from its fill. Returns a record, or None if unfilled."""
    i = int(c['i'])
    n = len(series)
    if i >= n - 2:
        return None
    long = c['side'] == 'buy'
    sign = 1.0 if long else -1.0
    point = float(spec.get('point') or 0.01)
    slip = float(CONFIG.instrument.default_slippage_points) * point
    step = TF_SECONDS[X.TF] * 1000

    # ---- the fill, exactly as the backtest takes it ----------------------- #
    first_m1 = None
    if c['entry_type'] == 'stop':
        reached = (series.h[i] >= c['trigger']) if long else (series.l[i] <= c['trigger'])
        if not reached:
            return None
        fill = c['trigger'] + sign * slip
        # The entry bar only counts from the minute the trigger printed.
        a, b = m1_range(m1, int(series.t[i]), int(series.t[i]) + step)
        for j in range(a, b):
            if (m1.h[j] >= c['trigger']) if long else (m1.l[j] <= c['trigger']):
                first_m1 = j
                break
    else:
        fill = float(series.o[i]) + sign * slip
    stop = float(c['stop'])
    risk = abs(fill - stop)
    if risk <= 0 or (long and stop >= fill) or (not long and stop <= fill):
        return None

    def fav(hh, ll):
        return ((hh - fill) if long else (fill - ll)) / risk

    def adv(hh, ll):
        return ((fill - ll) if long else (hh - fill)) / risk

    def hit(hh, ll):
        return (ll <= stop) if long else (hh >= stop)

    mfe, mae, mae_before_1r = 0.0, 0.0, None
    stopped, stop_bar = False, None
    reach_bar = {k: None for k in R_LEVELS}
    last = min(n, i + HORIZON)

    def feed_step(hh, ll, bar_no):
        """Advance one price step; True when the stop is taken."""
        nonlocal mfe, mae, mae_before_1r
        if hit(hh, ll):
            return True
        mae = max(mae, adv(hh, ll))
        mfe = max(mfe, fav(hh, ll))
        for k in R_LEVELS:
            if reach_bar[k] is None and mfe >= k:
                reach_bar[k] = bar_no
                if k == 1.0:
                    mae_before_1r = mae
        return False

    for bi in range(i, last):
        t0 = int(series.t[bi])
        use_m1 = bi == i or hit(series.h[bi], series.l[bi])
        if use_m1:
            # Entry bar, or a bar that takes the stop: replay it minute by
            # minute so a new high that came BEFORE the stop is credited and
            # one that came after is not. A minute that touches both counts
            # as the stop - the conservative reading.
            a, b = m1_range(m1, t0, t0 + step)
            if bi == i and first_m1 is not None:
                a = first_m1
            done = False
            for j in range(a, b):
                if feed_step(float(m1.h[j]), float(m1.l[j]), bi - i):
                    done = True
                    break
            if b <= a:                                   # no 1m data: use the bar
                done = feed_step(float(series.h[bi]), float(series.l[bi]), bi - i)
            if done:
                stopped, stop_bar = True, bi
                break
        else:
            feed_step(float(series.h[bi]), float(series.l[bi]), bi - i)

    end_bar = stop_bar if stopped else last - 1
    horizon_r = -1.0 - slip / risk if stopped else sign * (float(series.c[end_bar]) - fill) / risk

    # After a stop: did price go on to +1R from the original entry anyway?
    rescued = False
    if stopped:
        target = fill + sign * risk
        for bi in range(stop_bar + 1, last):
            if (series.h[bi] >= target) if long else (series.l[bi] <= target):
                rescued = True
                break

    vpu = (1.0 / float(spec.get('tick_size') or 0.01)) * float(spec.get('tick_value') or 1.0)
    commission = float(spec.get('commission_per_lot_side')
                       or CONFIG.instrument.commission_per_lot_side) * 2.0
    cost_r = commission / (risk * vpu) + c['spread_price'] / risk

    tp2_r = abs(float(c['tp2']) - fill) / risk
    return {
        'bar_t': int(c['bar_t']), 'playbook': c['playbook'], 'side': c['side'],
        'regime': c.get('regime') or '', 'risk_pts': risk,
        'risk_atr': risk / c['atr'] if c['atr'] else float('nan'),
        'mfe': mfe, 'mae': mae, 'mae_1r': mae_before_1r,
        'stopped': stopped, 'rescued': rescued, 'horizon_r': horizon_r,
        'bars_to_1r': reach_bar[1.0], 'reach': {k: reach_bar[k] is not None for k in R_LEVELS},
        'cost_r': cost_r, 'tp2_r': tp2_r, 'tp2_hit': mfe >= tp2_r,
    }


def ev_at(recs, k) -> float:
    """Net R per trade of 'fixed target at k R, fixed stop, exit at horizon'."""
    out = []
    for r in recs:
        if r['mfe'] >= k:
            g = k
        elif r['stopped']:
            g = r['horizon_r']                      # the stop, with slippage
        else:
            g = min(r['horizon_r'], k)             # still open: marked at the horizon
        out.append(g - r['cost_r'])
    return float(np.mean(out)) if out else float('nan')


HEAD_B = (f'    {"":34} {"n":>5} ' + ' '.join(f'{f"P{k:g}R":>6}' for k in (0.5, 1.0, 1.5, 2.0, 3.0))
          + f' {"MFE50":>6} {"MAE@1R p50/p80":>15} {"rescue":>7} '
          + ' '.join(f'{f"EV{k:g}":>7}' for k in (0.75, 1.0, 1.5, 2.0))
          + f' {"best k":>7} {"EVbest":>7}')


def row_b(label, recs) -> str:
    if len(recs) < MIN_N:
        return f'    {label:<34} {len(recs):>5}   (too few)'
    p = {k: np.mean([r['reach'][k] for r in recs]) for k in R_LEVELS}
    mfe = np.array([r['mfe'] for r in recs])
    m1r = np.array([r['mae_1r'] for r in recs if r['mae_1r'] is not None])
    stopped = [r for r in recs if r['stopped']]
    resc = np.mean([r['rescued'] for r in stopped]) if stopped else 0.0
    evs = {k: ev_at(recs, k) for k in EV_LEVELS}
    best = max(evs, key=evs.get)
    maetxt = f'{pct(m1r, 50):>6.2f} /{pct(m1r, 80):>5.2f}' if len(m1r) >= 10 else f'{"":>13}'
    return (f'    {label:<34} {len(recs):>5} '
            + ' '.join(f'{p[k]:>6.0%}' for k in (0.5, 1.0, 1.5, 2.0, 3.0))
            + f' {np.median(mfe):>6.2f}  {maetxt} {resc:>7.0%} '
            + ' '.join(f'{evs[k]:>+7.3f}' for k in (0.75, 1.0, 1.5, 2.0))
            + f' {best:>6g}R {evs[best]:>+7.3f}')


def grouped(recs, keyfn, order=None) -> list:
    g = defaultdict(list)
    for r in recs:
        g[keyfn(r)].append(r)
    keys = order or sorted(g, key=lambda k: -len(g[k]))
    return [(k, g[k]) for k in keys if k in g]


def session_of(ts_ms: int) -> str:
    hour = dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).hour
    return session_quality(hour)[0]


# =========================================================================== #
# driver                                                                      #
# =========================================================================== #
def main() -> int:
    buf = io.StringIO()

    def out(line=''):
        print(line, flush=True)
        buf.write(line + '\n')

    out('PHASE 0 - measure before redesigning TP / SL        XAUUSD, read-only')
    out(f'leg = reversal of {LEG_ATR} ATR   tradeable leg >= {TRADEABLE_ATR} ATR   '
        f'signal horizon {HORIZON} x 5m   costs: spread + commission + slippage')

    all_recs = {}
    csv_rows = []
    for label, year in LABELS:
        cap = pickle.loads((X.OUT / f'exp_exits_{label}.pkl').read_bytes())
        a, b = cap['start_ms'], cap['end_ms']
        warm = a - 60 * DAY_MS                    # history for the trend reads
        s = {tf: load_disk(SYMBOL, tf, None, warm, b) for tf in ('3m', '5m', '15m', '1h', '4h')}
        m1 = load_disk(SYMBOL, '1m', None, a, b + 2 * DAY_MS)
        track_1h = trend_track(s['1h'])
        track_4h = trend_track(s['4h'])

        out(f'\n{"=" * 124}\n  {year}   '
            f'{dt.datetime.fromtimestamp(a / 1000, dt.UTC):%Y-%m-%d} -> '
            f'{dt.datetime.fromtimestamp(b / 1000, dt.UTC):%Y-%m-%d}\n{"=" * 124}')

        # ---------------------------------------------------------- PART A
        out('\n  PART A - impulses and corrections   (WITH = impulse along the HTF trend, '
            'COUNTER = correction against it)')
        for tf, htf, track in (('3m', '1H', track_1h), ('5m', '1H', track_1h),
                               ('15m', '1H', track_1h), ('1h', '4H', track_4h)):
            ser = s[tf]
            k0 = int(np.searchsorted(ser.t, a, 'left'))
            res = anatomy(ser.slice(k0, len(ser)), track)
            for line in anatomy_block(f'{tf:>3} vs {htf} trend', res):
                out(line)

        # ---------------------------------------------------------- PART B
        spec = cap['spec']
        series, _ = X.load_period(cap)
        last_seen = {}
        recs = []
        for c in cap['candidates']:
            if not qualifies(c, spec):
                continue
            key = (c['playbook'], c['side'])
            if c['i'] - last_seen.get(key, -10**9) < DEDUP_BARS:
                continue
            last_seen[key] = c['i']
            r = walk(c, series, m1, spec)
            if r is None:
                continue
            h1 = state_at(track_1h, int(c['bar_t']) + TF_SECONDS[X.TF] * 1000)
            sd = 1 if c['side'] == 'buy' else -1
            r['vs1h'] = 'range' if h1 == 0 else ('with 1H' if h1 == sd else 'counter 1H')
            r['session'] = session_of(int(c['bar_t']))
            r['year'] = year
            recs.append(r)
        all_recs[year] = recs

        out(f'\n  PART B - signal paths   {len(recs)} qualified signals (reward gate ignored, '
            f'deduped per setup+side over {DEDUP_BARS} bars)')
        out('    P kR = reached k R before the stop   MFE50 = median best excursion (R)   '
            'MAE@1R = how deep trades that reached 1R went against first (R)')
        out('    rescue = stopped trades that then reached +1R from entry   '
            'EVk = net R/trade with a fixed target at k R   best k = the target with the highest EV')
        out(HEAD_B)
        out(row_b('ALL SIGNALS', recs))
        out('    -- by setup')
        for k, g in grouped(recs, lambda r: r['playbook']):
            out(row_b(k, g))
        out('    -- by direction vs the 1H trend')
        for k, g in grouped(recs, lambda r: r['vs1h'], ['with 1H', 'counter 1H', 'range']):
            out(row_b(k, g))
        out('    -- by setup x 1H trend')
        for k, g in grouped(recs, lambda r: f'{r["playbook"]} / {r["vs1h"]}'):
            out(row_b(k, g))
        out('    -- by session')
        for k, g in grouped(recs, lambda r: r['session']):
            out(row_b(k, g))
        out('    -- by regime')
        for k, g in grouped(recs, lambda r: r['regime'] or '-'):
            out(row_b(k, g))
        out('    -- stop size (stop distance in ATR)')
        for k, g in grouped(recs, lambda r: ('<1.0' if r['risk_atr'] < 1.0 else '1.0-1.5'
                                             if r['risk_atr'] < 1.5 else '1.5-2.5'
                                             if r['risk_atr'] < 2.5 else '>=2.5'),
                            ['<1.0', '1.0-1.5', '1.5-2.5', '>=2.5']):
            out(row_b(f'stop {k} ATR', g))
        out('    -- structural TP2: how far away, and how often reached before the stop')
        for k, g in grouped(recs, lambda r: ('<1.5R' if r['tp2_r'] < 1.5 else '1.5-2R'
                                             if r['tp2_r'] < 2 else '2-3R' if r['tp2_r'] < 3
                                             else '>=3R'), ['<1.5R', '1.5-2R', '2-3R', '>=3R']):
            hit = np.mean([r['tp2_hit'] for r in g]) if g else 0
            out(f'    TP2 {k:<8} n={len(g):>5}   reached before stop {hit:>5.0%}')

        for r in recs:
            csv_rows.append({
                'year': year, 'bar_utc': dt.datetime.fromtimestamp(r['bar_t'] / 1000, dt.UTC)
                .strftime('%Y-%m-%d %H:%M'), 'playbook': r['playbook'], 'side': r['side'],
                'vs1h': r['vs1h'], 'session': r['session'], 'regime': r['regime'],
                'risk_pts': round(r['risk_pts'], 3), 'risk_atr': round(r['risk_atr'], 3),
                'mfe_r': round(r['mfe'], 3), 'mae_r': round(r['mae'], 3),
                'mae_before_1r': '' if r['mae_1r'] is None else round(r['mae_1r'], 3),
                'stopped': int(r['stopped']), 'rescued': int(r['rescued']),
                'horizon_r': round(r['horizon_r'], 3), 'cost_r': round(r['cost_r'], 4),
                'tp2_r': round(r['tp2_r'], 3), 'tp2_hit': int(r['tp2_hit'])})

    # -------------------------------------------------------------- summary
    out(f'\n{"=" * 124}\n  CONSISTENCY - best fixed-target EV per setup, per year '
        f'(a setup only has an edge if it holds in all three)\n{"=" * 124}')
    books = sorted({r['playbook'] for rs in all_recs.values() for r in rs})
    out(f'    {"setup":<22}' + ''.join(f'{y:>22}' for y in all_recs) + f'{"verdict":>14}')
    for pb in books:
        cells, signs = [], []
        for y, rs in all_recs.items():
            g = [r for r in rs if r['playbook'] == pb]
            if len(g) < MIN_N:
                cells.append(f'{"n<" + str(MIN_N):>22}')
                continue
            evs = {k: ev_at(g, k) for k in EV_LEVELS}
            best = max(evs, key=evs.get)
            signs.append(evs[best] > 0)
            cells.append(f'{evs[best]:>+10.3f} @ {best:g}R (n{len(g):>4})')
        verdict = ('edge x3' if len(signs) == 3 and all(signs) else
                   'no edge' if signs and not any(signs) else 'mixed')
        out(f'    {pb:<22}' + ''.join(cells) + f'{verdict:>14}')
    for side in ('with 1H', 'counter 1H', 'range'):
        cells = []
        for y, rs in all_recs.items():
            g = [r for r in rs if r['vs1h'] == side]
            evs = {k: ev_at(g, k) for k in EV_LEVELS}
            best = max(evs, key=evs.get)
            cells.append(f'{evs[best]:>+10.3f} @ {best:g}R (n{len(g):>4})')
        out(f'    {"[" + side + "]":<22}' + ''.join(cells))

    OUT_TXT.write_text(buf.getvalue(), encoding='utf-8')
    with open(OUT_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
        w.writeheader()
        w.writerows(csv_rows)
    print(f'\nreport  -> {OUT_TXT}\nsignals -> {OUT_CSV}  ({len(csv_rows)} rows)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
