#!/usr/bin/env python
"""
tools/phase0b_leg_position.py - Phase 0b: where in the leg does a signal fire?

Read-only. Nothing live is touched.

Phase 0 showed that on 3m-1H gold is a sequence of impulses and corrections of
about equal size, and that the 1H trend says almost nothing about how the next
lower-timeframe leg will go. The question that framing actually asks is
positional: when a signal fires, where is price INSIDE the current leg - and
does that predict how the trade goes?

For every qualified signal (same sample as Phase 0) this records the state of
the 5m ATR zigzag at the moment the signal became known - the close of the
signal bar - using only pivots that had been CONFIRMED by then. The zigzag is
replayed causally, bar by bar, so nothing here can see a pivot the market had
not yet printed.

    JOIN / FADE   the signal trades in the direction of the leg in progress,
                  or against it
    extension     how far that leg has run so far, in ATR (median leg: 2.6)
    pullback      how far price has come back from the leg's extreme, in ATR
                  (a leg is confirmed over at LEG_ATR, so this is < LEG_ATR)
    depth         the leg in progress as a fraction of the leg before it -
                  for a correction, how much of the impulse it has retraced

Views, each broken down per year so a pattern only counts if it holds in all
three:
    1. join vs fade x extension
    2. join vs fade x pullback
    3. impulse / correction framing against the 1H trend:
         buy-the-dip       correction leg in progress, signal WITH the 1H trend
                           - by how much of the impulse has been retraced
         join the impulse  impulse leg in progress, signal with it
         fade the impulse  impulse leg in progress, signal against it
    4. leg survival: once a 5m leg has run X ATR, how often does it go further

    python tools/phase0b_leg_position.py
"""
from __future__ import annotations

import io
import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import exp_exits as X                                              # noqa: E402
import phase0_research as P0                                       # noqa: E402
from server.config import CONFIG, TF_SECONDS                       # noqa: E402
from server.datafeed import load_disk                              # noqa: E402
from server.engine.indicators import atr as atr_fn                 # noqa: E402

OUT_TXT = X.OUT / 'phase0b_report.txt'
DAY_MS = 86_400_000
MEDIAN_LEG = 2.6          # Phase 0: median 5m leg, all three years


# =========================================================================== #
# the zigzag, replayed causally                                               #
# =========================================================================== #
def zigzag_states(h, l, a, k: float) -> dict:
    """
    P0.zigzag(), line for line, but recording its state after EVERY bar.

    After bar j: the direction of the leg in progress, its confirmed starting
    pivot, the pivot before that, and the running extreme. A signal decided at
    the close of bar j reads index j - it sees exactly what the zigzag knew
    then, and a pivot confirmed later is invisible to it.
    """
    n = len(h)
    trend = np.zeros(n, dtype=np.int8)
    piv = np.full(n, np.nan)            # start of the leg in progress
    prev = np.full(n, np.nan)           # start of the leg before it
    ext = np.full(n, np.nan)            # running extreme of the leg in progress
    pivots = []
    hi, hi_i, lo, lo_i = h[0], 0, l[0], 0
    tr = 0
    for i in range(1, n):
        th = k * a[i] if a[i] > 0 else np.inf
        if tr == 0:
            if h[i] > hi:
                hi, hi_i = h[i], i
            if l[i] < lo:
                lo, lo_i = l[i], i
            if hi - lo >= th:
                if hi_i > lo_i:
                    tr = 1
                    pivots.append(lo)
                else:
                    tr = -1
                    pivots.append(hi)
        elif tr == 1:
            if h[i] > hi:
                hi, hi_i = h[i], i
            elif hi - l[i] >= th:
                pivots.append(hi)
                tr, lo, lo_i = -1, l[i], i
        else:
            if l[i] < lo:
                lo, lo_i = l[i], i
            elif h[i] - lo >= th:
                pivots.append(lo)
                tr, hi, hi_i = 1, h[i], i
        trend[i] = tr
        if tr != 0:
            piv[i] = pivots[-1]
            prev[i] = pivots[-2] if len(pivots) >= 2 else np.nan
            ext[i] = hi if tr == 1 else lo
    return {'trend': trend, 'piv': piv, 'prev': prev, 'ext': ext}


def leg_state(z, a, c, j: int) -> dict | None:
    """The leg in progress as of the close of bar j, in ATR of that bar."""
    tr = int(z['trend'][j])
    atr_j = float(a[j])
    if tr == 0 or not atr_j > 0 or np.isnan(z['piv'][j]):
        return None
    piv, ext, prev = float(z['piv'][j]), float(z['ext'][j]), float(z['prev'][j])
    run = abs(ext - piv)
    prev_run = abs(piv - prev) if prev == prev else float('nan')
    return {
        'dir': tr,
        'ext_atr': run / atr_j,
        'pull_atr': abs(ext - float(c[j])) / atr_j,
        'depth': run / prev_run if prev_run and prev_run == prev_run else float('nan'),
    }


# =========================================================================== #
# buckets                                                                     #
# =========================================================================== #
def ext_bucket(x: float) -> str:
    if x < 1.5:
        return 'a) run < 1.5 ATR'
    if x < MEDIAN_LEG:
        return f'b) run 1.5-{MEDIAN_LEG}'
    if x < 4.0:
        return f'c) run {MEDIAN_LEG}-4.0'
    return 'd) run >= 4.0 ATR'


def pull_bucket(x: float) -> str:
    if x < 0.5:
        return 'a) pulled back < 0.5 ATR'
    if x < 1.0:
        return 'b) pulled back 0.5-1.0'
    return 'c) pulled back >= 1.0'


def depth_bucket(x: float) -> str:
    if x != x:
        return 'z) unknown'
    if x < 0.5:
        return 'a) retraced < 50%'
    if x < 0.8:
        return 'b) retraced 50-80%'
    if x < 1.2:
        return 'c) retraced 80-120%'
    return 'd) retraced >= 120%'


# =========================================================================== #
# driver                                                                      #
# =========================================================================== #
def main() -> int:
    buf = io.StringIO()

    def out(line=''):
        print(line, flush=True)
        buf.write(line + '\n')

    out('PHASE 0b - where in the leg does a signal fire?      XAUUSD 5m, read-only')
    out(f'leg = 5m ATR zigzag, reversal of {P0.LEG_ATR} ATR, replayed causally   '
        f'median leg {MEDIAN_LEG} ATR   horizon {P0.HORIZON} x 5m   costs included')
    out('columns as Phase 0:  P kR = reached k R before the stop   '
        'EVk = net R/trade with a fixed target at k R')

    per_year = {}
    for label, year in P0.LABELS:
        cap = pickle.loads((X.OUT / f'exp_exits_{label}.pkl').read_bytes())
        a0, b0 = cap['start_ms'], cap['end_ms']
        spec = cap['spec']
        series, _ = X.load_period(cap)
        m1 = load_disk(P0.SYMBOL, '1m', None, a0, b0 + 2 * DAY_MS)
        track_1h = P0.trend_track(load_disk(P0.SYMBOL, '1h', None, a0 - 60 * DAY_MS, b0))

        h, l, c = series.h, series.l, series.c
        atr5 = atr_fn(h, l, c, 14)
        z = zigzag_states(h, l, atr5, P0.LEG_ATR)

        last_seen, recs = {}, []
        for cand in cap['candidates']:
            if not P0.qualifies(cand, spec):
                continue
            key = (cand['playbook'], cand['side'])
            if cand['i'] - last_seen.get(key, -10 ** 9) < P0.DEDUP_BARS:
                continue
            last_seen[key] = cand['i']
            j = int(cand['i']) - 1                     # the signal bar: decided at its close
            if j < 1:
                continue
            st = leg_state(z, atr5, c, j)
            if st is None:
                continue
            r = P0.walk(cand, series, m1, spec)
            if r is None:
                continue
            sd = 1 if cand['side'] == 'buy' else -1
            h1 = P0.state_at(track_1h, int(cand['bar_t']) + TF_SECONDS[X.TF] * 1000)
            r.update(st)
            r['rel'] = 'JOIN' if sd == st['dir'] else 'FADE'
            # The leg in progress, read against the 1H trend.
            if h1 == 0:
                r['frame'] = 'n/a (1H ranging)'
            elif st['dir'] == h1:
                r['frame'] = 'join the impulse' if sd == h1 else 'fade the impulse'
            else:
                r['frame'] = 'buy-the-dip (enter after correction)' if sd == h1 \
                    else 'join the correction'
            recs.append(r)
        per_year[year] = recs

        out(f'\n{"=" * 124}\n  {year}   {len(recs)} signals\n{"=" * 124}')
        out(P0.HEAD_B)
        out(P0.row_b('ALL SIGNALS', recs))

        out('    -- 1. join vs fade the leg in progress, by how far that leg has run')
        for rel in ('JOIN', 'FADE'):
            g = [r for r in recs if r['rel'] == rel]
            out(P0.row_b(f'{rel} (all)', g))
            for k, gg in P0.grouped(g, lambda r: ext_bucket(r['ext_atr']),
                                    sorted({ext_bucket(r['ext_atr']) for r in g})):
                out(P0.row_b(f'  {rel} {k}', gg))

        out('    -- 2. join vs fade, by how far price has already pulled back from the extreme')
        for rel in ('JOIN', 'FADE'):
            g = [r for r in recs if r['rel'] == rel]
            for k, gg in P0.grouped(g, lambda r: pull_bucket(r['pull_atr']),
                                    sorted({pull_bucket(r['pull_atr']) for r in g})):
                out(P0.row_b(f'  {rel} {k}', gg))

        out('    -- 3. impulses and corrections (leg in progress vs the 1H trend)')
        for fr in ('buy-the-dip (enter after correction)', 'join the impulse',
                   'fade the impulse', 'join the correction', 'n/a (1H ranging)'):
            g = [r for r in recs if r['frame'] == fr]
            out(P0.row_b(fr, g))
            if fr.startswith('buy-the-dip'):
                for k, gg in P0.grouped(g, lambda r: depth_bucket(r['depth']),
                                        sorted({depth_bucket(r['depth']) for r in g})):
                    out(P0.row_b(f'  {k} of the impulse', gg))
            elif fr in ('join the impulse', 'fade the impulse'):
                for k, gg in P0.grouped(g, lambda r: ext_bucket(r['ext_atr']),
                                        sorted({ext_bucket(r['ext_atr']) for r in g})):
                    out(P0.row_b(f'  {k}', gg))

        # 4. leg survival, from the full (non-causal) leg list - this one is a
        # property of the market, not of any signal, so hindsight is fine.
        res = P0.anatomy(series, track_1h)
        sizes = np.array([x['atr'] for x in res['legs'] if x['atr'] == x['atr']])
        out('    -- 4. leg survival: once a 5m leg has run X ATR ...')
        out(f'    {"":14} {"legs":>6} {"+0.5 ATR more":>14} {"+1 ATR more":>12} '
            f'{"+2 ATR more":>12} {"median left":>12}')
        for x in (1.5, 2.0, 2.6, 3.0, 4.0, 5.0):
            s = sizes[sizes >= x]
            if len(s) < P0.MIN_N:
                continue
            out(f'    run >= {x:<4} ATR {len(s):>6} {np.mean(s >= x + .5):>14.0%} '
                f'{np.mean(s >= x + 1):>12.0%} {np.mean(s >= x + 2):>12.0%} '
                f'{np.median(s - x):>10.2f} ATR')

    # ---------------------------------------------------------------- summary
    out(f'\n{"=" * 124}\n  CONSISTENCY at FIXED targets (no picking the best target after the fact)'
        f'   EV1 / EV2 = net R per trade at 1R / 2R\n{"=" * 124}')
    years = list(per_year)

    def summary(label, pick):
        cells, signs1, signs2 = [], [], []
        for y in years:
            g = [r for r in per_year[y] if pick(r)]
            if len(g) < P0.MIN_N:
                cells.append(f'{"n<" + str(P0.MIN_N):>26}')
                continue
            e1, e2 = P0.ev_at(g, 1.0), P0.ev_at(g, 2.0)
            signs1.append(e1 > 0)
            signs2.append(e2 > 0)
            p1 = np.mean([r['reach'][1.0] for r in g])
            cells.append(f'{e1:>+7.3f} {e2:>+7.3f} {p1:>4.0%} n{len(g):>4}')
        v = []
        if len(signs1) == 3 and all(signs1):
            v.append('1R x3')
        if len(signs2) == 3 and all(signs2):
            v.append('2R x3')
        verdict = ', '.join(v) if v else ('negative x3' if len(signs1) == 3 and not any(signs1)
                                           and not any(signs2) else '-')
        out(f'    {label:<44}' + ''.join(cells) + f'  {verdict}')

    out(f'    {"":<44}' + ''.join(f'{y + "  EV1    EV2  P1R":>26}' for y in years) + '  verdict')
    summary('ALL SIGNALS', lambda r: True)
    for rel in ('JOIN', 'FADE'):
        for b in ('a) run < 1.5 ATR', f'b) run 1.5-{MEDIAN_LEG}', f'c) run {MEDIAN_LEG}-4.0',
                  'd) run >= 4.0 ATR'):
            summary(f'{rel} {b}', lambda r, rel=rel, b=b: r['rel'] == rel and ext_bucket(r['ext_atr']) == b)
    for rel in ('JOIN', 'FADE'):
        for b in ('a) pulled back < 0.5 ATR', 'b) pulled back 0.5-1.0', 'c) pulled back >= 1.0'):
            summary(f'{rel} {b}', lambda r, rel=rel, b=b: r['rel'] == rel and pull_bucket(r['pull_atr']) == b)
    for b in ('a) retraced < 50%', 'b) retraced 50-80%', 'c) retraced 80-120%', 'd) retraced >= 120%'):
        summary(f'buy-the-dip, {b}',
                lambda r, b=b: r['frame'].startswith('buy-the-dip') and depth_bucket(r['depth']) == b)
    for fr in ('join the impulse', 'fade the impulse', 'join the correction'):
        summary(fr, lambda r, fr=fr: r['frame'] == fr)

    OUT_TXT.write_text(buf.getvalue(), encoding='utf-8')
    print(f'\nreport -> {OUT_TXT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
