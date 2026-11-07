"""
server/chart_levels.py - the S/R levels the LIVE CHART draws.

Ported 2026-11-06 from ScalpingWorkstation (server/smc/classic.find_levels),
whose levels read better on the chart. Display only: the engine's own
snapshot['levels'] - which the playbooks, the room gate and the narrator read -
is untouched, and nothing in server/engine is edited (its files are
fingerprinted by the forward books).

Same method as the engine (cluster swing extremes into scored bands), with the
four differences that made ScalpingWorkstation's levels cleaner:

  * the band unit is the window's MEAN ATR, not the last bar's - a chart opened
    in a quiet hour otherwise cuts every level into slivers;
  * "broken" means price closed THROUGH the band (above a high it capped, below
    a low it held) and is still through - not merely that price moved away;
  * a weaker band within two widths of a stronger one is the same level found
    twice, and is dropped;
  * swings use ScalpingWorkstation's fractal width per timeframe (PIVOT_K).
"""
from __future__ import annotations

import numpy as np

from .engine import structure as stm
from .engine.indicators import atr as atr_fn
from .engine.levels import _round_number_pull
from .engine.precision import rp, using

# ScalpingWorkstation server/smc/__init__.py: bars either side of a fractal pivot.
PIVOT_K = {'1m': 3, '3m': 2, '5m': 2, '15m': 2, '30m': 2, '1h': 3, '2h': 3, '4h': 3,
           '1d': 2, '1w': 2}
SWING_ATR_MULT = 0.55       # a leg must clear this * ATR to count (both projects)
MAX_SWINGS = 60
TOLERANCE_ATR = 0.35
MIN_TOUCHES = 2
MAX_LEVELS = 14


def _reaction(h, l, idx: int, price: float, atr_val: float, forward: int = 12) -> float:
    """How far price travelled away from `price` after touching it, in ATR."""
    end = min(h.size, idx + forward + 1)
    if end <= idx + 1 or atr_val <= 0:
        return 0.0
    return float(max(h[idx + 1:end].max() - price, price - l[idx + 1:end].min()) / atr_val)


def chart_levels(series, tf: str, digits=None) -> list:
    """Levels for the live chart from the CLOSED bars of `series`, as engine
    Level dicts (price, kind, touches, score, width, first_t, last_t, ...)."""
    n = len(series) - 1                       # closed bars only
    if n < 30:
        return []
    h, l, c, t = series.h[:n], series.l[:n], series.c[:n], series.t[:n]
    a = atr_fn(h, l, c, 14)
    finite = a[np.isfinite(a) & (a > 0)]
    atr_now = float(finite.mean()) if finite.size else max(float(np.mean(h - l)), 1e-9)
    tol = TOLERANCE_ATR * atr_now
    price_now = float(c[-1])
    first_t, last_t = int(t[0]), int(t[-1])
    span_t = max(1, last_t - first_t)

    swings = stm.find_swings(h, l, c, t, PIVOT_K.get(tf, 2), SWING_ATR_MULT, a, MAX_SWINGS)
    seeds = sorted(((s.idx, float(s.price), s.kind) for s in swings), key=lambda x: x[1])
    clusters: list = []
    for s in seeds:
        if clusters and abs(s[1] - clusters[-1]['mean']) <= tol:
            g = clusters[-1]
            g['members'].append(s)
            g['mean'] = float(np.mean([m[1] for m in g['members']]))
        else:
            clusters.append({'mean': s[1], 'members': [s]})

    out: list = []
    with using(digits):
        for g in clusters:
            members = g['members']
            if len(members) < MIN_TOUCHES and _round_number_pull(members[0][1]) < 0.85:
                continue                          # a lone touch only counts at a big figure
            price = float(np.mean([m[1] for m in members]))
            touches = len(members)
            idxs = [m[0] for m in members]
            member_t = [int(t[i]) for i in idxs]
            reaction = max((_reaction(h, l, i, price, atr_now) for i in idxs), default=0.0)

            last_i = max(idxs)
            last_kind = next(m[2] for m in members if m[0] == last_i)
            after = c[last_i + 1:]
            through = (after > price + 1.5 * tol) if last_kind == 'high' else (after < price - 1.5 * tol)
            broken = bool(after.size >= 3 and through.sum() >= 3 and through[-1])

            n_high = sum(1 for m in members if m[2] == 'high')
            if n_high >= 1 and touches - n_high >= 1 and touches >= 3:
                kind = 'flip'
            elif price > price_now:
                kind = 'resistance'
            else:
                kind = 'support'

            recency = (max(member_t) - first_t) / span_t
            pull = _round_number_pull(price)
            score = (min(touches, 6) * 11 + recency * 18 + min(reaction, 3.0) * 6 + pull * 10
                     + (6 if kind == 'flip' else 0))
            score = int(max(0, min(100, round(score))))

            sources = []
            if touches >= 2:
                sources.append(f'{touches} swing touches')
            if pull >= 0.85:
                sources.append('major round number')
            elif pull >= 0.4:
                sources.append('round number')
            if kind == 'flip':
                sources.append('flipped polarity')
            out.append({
                'price': rp(price), 'kind': kind, 'touches': touches, 'score': score,
                'width': rp(tol), 'first_t': min(member_t), 'last_t': max(member_t),
                'reaction_atr': round(reaction, 2), 'is_round': pull >= 0.4, 'broken': broken,
                'distance_atr': round((price - price_now) / atr_now, 2), 'sources': sources,
            })

    # One band per area of price: a weaker band within two widths of a stronger
    # one is the same level found twice, and stacking them buries the candles.
    out.sort(key=lambda x: -x['score'])
    kept: list = []
    for lv in out:
        if all(abs(lv['price'] - k['price']) > 2 * tol for k in kept):
            kept.append(lv)
        if len(kept) >= MAX_LEVELS:
            break
    return _drawn(kept, price_now)


# What ChartEngine.drawLevels draws: score >= 50, best 8.
DRAWN = 8
DRAWN_MIN_SCORE = 50


def _drawn(levels: list, price_now: float) -> list:
    """
    The levels the chart will draw, best first. The nearest level on EACH side
    of price always gets a place: on 4h (2026-11-06) the support price sat on,
    4069.95, scored 9th and the plain top-8 cut it - the one level that
    mattered most at that moment. The rest of the places go by score.
    """
    eligible = [lv for lv in levels if lv['score'] >= DRAWN_MIN_SCORE]
    below = [lv for lv in eligible if lv['price'] <= price_now]
    above = [lv for lv in eligible if lv['price'] > price_now]
    must = ([max(below, key=lambda x: x['price'])] if below else []) + \
           ([min(above, key=lambda x: x['price'])] if above else [])
    rest = [lv for lv in eligible if lv not in must][:DRAWN - len(must)]
    return sorted(must + rest, key=lambda x: -x['score'])


__all__ = ['chart_levels']
