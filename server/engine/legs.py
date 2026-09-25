"""
server/engine/legs.py - where price is inside the leg in progress.

Price moves as a sequence of legs (impulses) and corrections. This module
reads, on CLOSED bars, which leg is in progress and how far along it is, in
ATR - the input to the 'leg' gate in qualify.py.

A leg is an ATR zigzag: it ends when price reverses K x ATR from its extreme.
The zigzag is replayed causally, bar by bar, so the state after the last bar
is exactly what was knowable at that close - a pivot confirmed later cannot
leak into it.

What Phase 0 / 0b measured with this read (XAUUSD 5m, 2024-2026,
tools/phase0_research.py, tools/phase0b_leg_position.py):

  - legs have no memory. P(+1 ATR more) is 52-58% however far the leg has
    already run, so "extended, due to turn" is not a reason to trade.
  - corrections retrace ~100% of the leg (median ~97%), so a pullback is not
    a "pocket" price reliably bounces from.
  - three ways of FADING the leg (trading against it) lost in all three years,
    and those are what the gate blocks:
        fading after a 0.5-1.0 ATR pullback from the extreme
        fading a leg that has already run >= 4 ATR
        fading the higher-timeframe impulse while it is in progress
"""

from __future__ import annotations

import numpy as np

from ..config import CONFIG, TF_SECONDS

LEG_ATR = 1.5            # a leg ends on a reversal of this many ATR (Phase 0)
LEG_ATR_PERIOD = 14      # the ATR Phase 0 measured with

# The avoid rules, exactly as tested in tools/phase1_tpsl.py avoid_admit().
FADE_PULL_LO = 0.5       # fading after a pullback of [0.5, 1.0) ATR
FADE_PULL_HI = 1.0
FADE_EXT_ATR = 4.0       # fading a leg that has run this far


def zigzag_last(h, l, a, k: float = LEG_ATR) -> dict | None:
    """
    The zigzag's state after the LAST bar: direction of the leg in progress,
    its starting pivot, the pivot before that, and its running extreme.

    Line for line the replay in tools/phase0b_leg_position.zigzag_states(),
    keeping only the final state - the live engine only ever asks about now.
    """
    n = len(h)
    if n < 2:
        return None
    pivots = []
    hi, hi_i, lo, lo_i = float(h[0]), 0, float(l[0]), 0
    tr = 0
    start_i = 0                          # bar the leg in progress started on
    for i in range(1, n):
        th = k * a[i] if a[i] > 0 else np.inf
        if tr == 0:
            if h[i] > hi:
                hi, hi_i = float(h[i]), i
            if l[i] < lo:
                lo, lo_i = float(l[i]), i
            if hi - lo >= th:
                if hi_i > lo_i:
                    tr = 1
                    pivots.append(lo)
                    start_i = lo_i
                else:
                    tr = -1
                    pivots.append(hi)
                    start_i = hi_i
        elif tr == 1:
            if h[i] > hi:
                hi, hi_i = float(h[i]), i
            elif hi - l[i] >= th:
                pivots.append(hi)
                start_i = hi_i
                tr, lo, lo_i = -1, float(l[i]), i
        else:
            if l[i] < lo:
                lo, lo_i = float(l[i]), i
            elif h[i] - lo >= th:
                pivots.append(lo)
                start_i = lo_i
                tr, hi, hi_i = 1, float(h[i]), i
    if tr == 0:
        return None
    return {
        'dir': tr,
        'piv': float(pivots[-1]),
        'prev': float(pivots[-2]) if len(pivots) >= 2 else float('nan'),
        'ext': hi if tr == 1 else lo,
        'ext_i': hi_i if tr == 1 else lo_i,
        'start_i': start_i,
    }


def leg_state(h, l, c, a, t=None, k: float = LEG_ATR) -> dict | None:
    """
    The leg in progress as of the close of the last bar, in ATR of that bar.

    `a` must be the ATR array for the same bars (period LEG_ATR_PERIOD).
    Returns None until the first leg is confirmed.
    """
    z = zigzag_last(h, l, a, k)
    atr_j = float(a[-1]) if len(a) else 0.0
    if z is None or not atr_j > 0:
        return None
    run = abs(z['ext'] - z['piv'])
    prev_run = abs(z['piv'] - z['prev']) if z['prev'] == z['prev'] else float('nan')
    # Not rounded: the avoid rules have hard edges (1.0, 4.0 ATR) and a
    # 0.998 rounded to 1.00 lands on the wrong side of one. Display rounds.
    out = {
        'dir': int(z['dir']),
        'ext_atr': run / atr_j,                              # how far it has run
        'pull_atr': abs(z['ext'] - float(c[-1])) / atr_j,    # back off the extreme
        'depth': (run / prev_run if prev_run and prev_run == prev_run else None),
        'start': round(z['piv'], 3),
        'extreme': round(z['ext'], 3),
        'close': float(c[-1]),
        'atr': atr_j,
        'k_atr': k,
    }
    if t is not None and len(t):
        out['start_ms'] = int(t[z['start_i']])
        out['extreme_ms'] = int(t[z['ext_i']])
        out['bar_ms'] = int(t[-1])
    return out


def annotate(leg: dict | None, mtf: dict, base_tf: str) -> dict | None:
    """
    The leg read plus what the gate would say about FADING it right now -
    the higher-timeframe trend it reads, which side a fade would be, and the
    reason that fade is blocked (None when it is not). For the chart and the
    analyst, so neither has to re-derive the rules.
    """
    if not leg:
        return None
    htf_dir, htf_tf = htf_trend(mtf, base_tf)
    fade_side = 'sell' if int(leg['dir']) == 1 else 'buy'
    return dict(leg, htf_dir=htf_dir, htf_tf=htf_tf, fade_side=fade_side,
                fade_block=avoid_verdict(fade_side, leg, htf_dir, htf_tf),
                rules_on=bool(CONFIG.gates.avoid_fades))


def htf_trend(mtf: dict, base_tf: str) -> tuple:
    """
    (direction, tf) of the higher-timeframe trend the impulse rule reads.

    1h for every base timeframe below it - what Phase 0b tested on 5m - and
    otherwise the first ladder timeframe above the base. Direction is +1 up,
    -1 down, 0 ranging / unavailable, read from the MTF rows exactly as the
    research read quick_trend(): 'up' or 'down' in the state.
    """
    base_s = TF_SECONDS.get(base_tf, 300)
    want_s = max(3600, base_s + 1)
    best = None
    for row in (mtf or {}).get('rows') or []:
        s = TF_SECONDS.get(row.get('tf'), 0)
        if s >= want_s and (best is None or s < TF_SECONDS.get(best.get('tf'), 0)):
            best = row
    if best is None:
        return 0, None
    state = str(best.get('state') or '')
    return (1 if 'up' in state else -1 if 'down' in state else 0), best.get('tf')


def avoid_verdict(side: str, leg: dict | None, htf_dir: int,
                  htf_tf: str | None = None) -> str | None:
    """
    Why this trade is one of the three fades Phase 0b says to avoid, or None.
    """
    if not leg:
        return None
    sd = 1 if side == 'buy' else -1
    d = int(leg['dir'])
    fade = sd != d
    word = 'up' if d == 1 else 'down'
    if fade and FADE_PULL_LO <= float(leg['pull_atr']) < FADE_PULL_HI:
        return (f"fades the {word} leg {leg['pull_atr']:.1f} ATR off its extreme - "
                f"a spot that lost in all three years tested")
    if fade and float(leg['ext_atr']) >= FADE_EXT_ATR:
        return (f"fades {'an' if d == 1 else 'a'} {word} leg that has run {leg['ext_atr']:.1f} ATR - long legs "
                f"are no likelier to turn, and this lost in all three years tested")
    if htf_dir != 0 and d == htf_dir and sd != htf_dir:
        return (f"fades the {word} impulse while {htf_tf or 'the higher timeframe'} "
                f"is trending {word} too - lost in all three years tested")
    return None


# What Phase 0 / 0b measured, in words. One copy, read by the narrator and by
# the LLM fact sheet, so the analyst reasons from the same numbers the gate
# rests on - and cannot drift back to "exhausted" legs and Fibonacci pockets.
MEASURED = (
    "Price moves in legs and corrections. Measured on XAUUSD 5m 2024-2026, a "
    "leg has no memory: the chance it runs another 1 ATR is 52-58% however far "
    "it has already gone, so a long leg is not 'exhausted' or 'due' to turn.",
    "Corrections retraced a median ~97% of the leg before them, so a pullback "
    "into a Fibonacci 'pocket' is not, by itself, a level price reliably turns from.",
    "Legs run both ways inside a 1h trend, and the 1h direction does not make a "
    "5m leg any longer - a lower-timeframe move against the 1h trend is a normal leg.",
    "Fading a leg (trading against it) lost in all three years in three spots: "
    "0.5-1.0 ATR off its extreme, once it has run 4 ATR or more, and against "
    "the 1h impulse while it is in progress.",
)


def describe(leg: dict, htf_dir: int = 0, htf_tf: str | None = None) -> str:
    """The leg in progress as one sentence, for the narrator and fact sheet."""
    up = int(leg['dir']) == 1
    word = 'up' if up else 'down'
    s = f"{'an' if up else 'a'} {word} leg"
    if leg.get('start') is not None:
        s += f" from {leg['start']:.2f}"
    s += f" that has run {float(leg['ext_atr']):.1f} ATR"
    if leg.get('extreme') is not None:
        s += f" to {leg['extreme']:.2f}"
    s += f"; price is {float(leg['pull_atr']):.1f} ATR off that extreme"
    if htf_tf:
        trend = {1: 'trending up', -1: 'trending down'}.get(htf_dir, 'not trending')
        s += f", with {htf_tf} {trend}"
    return s


__all__ = ['LEG_ATR', 'MEASURED', 'zigzag_last', 'leg_state', 'htf_trend',
           'avoid_verdict', 'annotate', 'describe']
