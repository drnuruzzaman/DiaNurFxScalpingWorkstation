"""
server/engine/events.py - the things that HAPPEN at a level.

Levels and patterns describe the board. This module describes the moves: a
sweep of resting liquidity, a rejection wick, a genuine breakout, the retest
that follows it, and the false break that catches everyone leaning the wrong
way.

The distinction that matters most, and the one most systems get wrong:

    BREAKOUT      price CLOSED through the level and stayed
    SWEEP         price WICKED through and closed back inside
    FALSE BREAK   price closed through, then closed back inside within a few bars

These look identical for one bar. The only thing separating them is what
happens next, so every detector here reports how many bars it waited and
whether the verdict is still provisional. A signal built on an unconfirmed
break is a signal built on a guess, and the qualification stage is told as much.

All events carry `idx`/`t` of the bar that completed them, so nothing can be
used before it existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np

from .indicators import candle_stats


@dataclass
class Event:
    kind: str            # sweep | rejection | breakout | retest | false_break | reversal
    direction: str       # 'up' | 'down'  (the direction of the MOVE)
    bias: str            # 'bullish' | 'bearish' (what it implies for the next move)
    idx: int
    t: int
    price: float         # the level involved
    extreme: float       # how far price actually went
    strength: int        # 0..100
    confirmed: bool
    bars_since: int
    label: str = ''
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _s(value: float) -> int:
    return int(max(0, min(100, round(value))))


# --------------------------------------------------------------------------- #
# liquidity sweeps                                                            #
# --------------------------------------------------------------------------- #
def detect_sweeps(h, l, c, o, t, swings, atr_val, lookback: int = 60,
                  reclaim_bars: int = 4) -> list:
    """
    A wick through a prior swing extreme that closes back inside.

    This is the single most useful event on gold intraday: it marks where stops
    were taken and, crucially, that the move through the level had no follow
    through. The strength score rewards a deep wick, a fast reclaim, and a level
    that had real liquidity behind it (an equal-highs cluster beats a lone
    swing).
    """
    out: list = []
    n = c.size
    if n < 10 or atr_val <= 0 or not swings:
        return out
    start = max(1, n - lookback)

    for i in range(start, n):
        for s in swings:
            if s.idx >= i or i - s.idx > lookback:
                continue
            if s.kind == 'high':
                # wicked above the swing high but closed back below it
                if not (h[i] > s.price and c[i] < s.price):
                    continue
                penetration = (h[i] - s.price) / atr_val
                direction, bias = 'up', 'bearish'
                extreme = float(h[i])
            else:
                if not (l[i] < s.price and c[i] > s.price):
                    continue
                penetration = (s.price - l[i]) / atr_val
                direction, bias = 'down', 'bullish'
                extreme = float(l[i])

            if penetration < 0.08:
                continue                 # grazed it; not a sweep

            # Did the reclaim hold for a few bars, or did price go straight back?
            fwd = c[i + 1:i + 1 + reclaim_bars]
            if fwd.size:
                held = bool((fwd < s.price).all()) if s.kind == 'high' \
                    else bool((fwd > s.price).all())
            else:
                held = False
            confirmed = held and fwd.size >= min(2, reclaim_bars)

            rng = max(h[i] - l[i], 1e-9)
            wick = (h[i] - max(o[i], c[i])) if s.kind == 'high' else \
                   (min(o[i], c[i]) - l[i])
            wick_ratio = wick / rng

            strength = _s(
                min(penetration / 1.2, 1.0) * 30 +
                wick_ratio * 34 +
                (20 if confirmed else 0) +
                (16 if getattr(s, 'label', '') in ('HH', 'LL') else 8)
            )

            out.append(Event(
                kind='sweep', direction=direction, bias=bias, idx=int(i),
                t=int(t[i]), price=float(s.price), extreme=extreme,
                strength=strength, confirmed=confirmed, bars_since=int(n - 1 - i),
                label=f"swept {s.kind} {s.price:.2f}",
                notes=[
                    f'penetrated {penetration:.2f} ATR beyond the level',
                    f'wick was {wick_ratio * 100:.0f}% of the bar range',
                    'reclaimed and held' if confirmed else 'reclaim not yet confirmed',
                ],
            ))
            break              # one sweep per bar - the nearest swing wins

    out.sort(key=lambda e: -e.idx)
    return out[:8]


# --------------------------------------------------------------------------- #
# rejection candles at a level                                                #
# --------------------------------------------------------------------------- #
def detect_rejections(o, h, l, c, t, levels, atr_val,
                      wick_ratio: float = 0.55, lookback: int = 40) -> list:
    """Long-wick bars that touched a scored level and were pushed away."""
    out: list = []
    n = c.size
    if n < 5 or atr_val <= 0 or not levels:
        return out
    cs = candle_stats(o, h, l, c)
    start = max(0, n - lookback)

    for i in range(start, n):
        for lv in levels:
            band = max(lv.width, atr_val * 0.3)
            touched_up = h[i] >= lv.price - band and h[i] <= lv.price + band * 3
            touched_dn = l[i] <= lv.price + band and l[i] >= lv.price - band * 3
            if cs['upper_pct'][i] >= wick_ratio and touched_up and c[i] < lv.price:
                out.append(Event(
                    kind='rejection', direction='up', bias='bearish', idx=int(i),
                    t=int(t[i]), price=float(lv.price), extreme=float(h[i]),
                    strength=_s(cs['upper_pct'][i] * 55 + lv.score * 0.35 +
                                min(cs['range'][i] / atr_val, 2.0) * 8),
                    confirmed=True, bars_since=int(n - 1 - i),
                    label=f'rejected {lv.kind} {lv.price:.2f}',
                    notes=[f"upper wick {cs['upper_pct'][i] * 100:.0f}% of range",
                           f'level score {lv.score}',
                           f"closed {(lv.price - c[i]) / atr_val:.2f} ATR below it"],
                ))
                break
            if cs['lower_pct'][i] >= wick_ratio and touched_dn and c[i] > lv.price:
                out.append(Event(
                    kind='rejection', direction='down', bias='bullish', idx=int(i),
                    t=int(t[i]), price=float(lv.price), extreme=float(l[i]),
                    strength=_s(cs['lower_pct'][i] * 55 + lv.score * 0.35 +
                                min(cs['range'][i] / atr_val, 2.0) * 8),
                    confirmed=True, bars_since=int(n - 1 - i),
                    label=f'rejected {lv.kind} {lv.price:.2f}',
                    notes=[f"lower wick {cs['lower_pct'][i] * 100:.0f}% of range",
                           f'level score {lv.score}',
                           f"closed {(c[i] - lv.price) / atr_val:.2f} ATR above it"],
                ))
                break

    out.sort(key=lambda e: -e.idx)
    return out[:8]


# --------------------------------------------------------------------------- #
# breakouts, retests and false breaks                                         #
# --------------------------------------------------------------------------- #
def detect_breaks(o, h, l, c, v, t, levels, atr_val,
                  false_break_bars: int = 5, retest_bars: int = 12,
                  lookback: int = 60) -> list:
    """
    Closes through a level, then classify what followed.

    Expansion matters: a break on a bar half the size of recent average range is
    a drift through, and drifts get retraced. The range ratio feeds the score,
    so a limp break scores low and the qualification stage can refuse it.
    """
    out: list = []
    n = c.size
    if n < 12 or atr_val <= 0 or not levels:
        return out
    rng = h - l
    avg_rng = float(np.mean(rng[-40:])) if n >= 40 else float(np.mean(rng))
    avg_vol = float(np.mean(v[-40:])) if (v is not None and n >= 40 and v.any()) else 0.0
    start = max(1, n - lookback)

    for lv in levels:
        band = max(lv.width, atr_val * 0.25)
        for i in range(start, n):
            broke_up = c[i] > lv.price + band and c[i - 1] <= lv.price + band
            broke_dn = c[i] < lv.price - band and c[i - 1] >= lv.price - band
            if not (broke_up or broke_dn):
                continue

            direction = 'up' if broke_up else 'down'
            expansion = rng[i] / avg_rng if avg_rng > 0 else 1.0
            vol_ratio = (v[i] / avg_vol) if avg_vol > 0 else 1.0

            # --- did it stick? -------------------------------------------- #
            window = c[i + 1:i + 1 + false_break_bars]
            back_inside = False
            if window.size:
                back_inside = bool((window < lv.price).any()) if broke_up \
                    else bool((window > lv.price).any())
            decided = window.size >= false_break_bars

            if back_inside:
                out.append(Event(
                    kind='false_break', direction=direction,
                    bias='bearish' if broke_up else 'bullish',
                    idx=int(i), t=int(t[i]), price=float(lv.price),
                    extreme=float(h[i] if broke_up else l[i]),
                    strength=_s(50 + lv.score * 0.3 + (12 if expansion < 0.9 else 0)),
                    confirmed=True, bars_since=int(n - 1 - i),
                    label=f'false break {direction} at {lv.price:.2f}',
                    notes=[f'closed back inside within {false_break_bars} bars',
                           f'break bar was {expansion:.1f}x average range',
                           f'level score {lv.score}'],
                ))
                continue

            # --- a real break: look for the retest ------------------------ #
            retest_idx = None
            seg = slice(i + 1, min(n, i + 1 + retest_bars))
            if broke_up:
                touched = np.flatnonzero(l[seg] <= lv.price + band)
            else:
                touched = np.flatnonzero(h[seg] >= lv.price - band)
            if touched.size:
                retest_idx = int(i + 1 + touched[0])

            strength = _s(
                min(expansion / 1.8, 1.0) * 30 +
                min(vol_ratio / 2.0, 1.0) * 14 +
                lv.score * 0.30 +
                (16 if decided else 0) +
                (10 if retest_idx is not None else 0)
            )
            out.append(Event(
                kind='breakout', direction=direction,
                bias='bullish' if broke_up else 'bearish',
                idx=int(i), t=int(t[i]), price=float(lv.price),
                extreme=float(h[i] if broke_up else l[i]),
                strength=strength, confirmed=decided, bars_since=int(n - 1 - i),
                label=f'broke {lv.kind} {lv.price:.2f}',
                notes=[f'break bar {expansion:.1f}x average range',
                       f'volume {vol_ratio:.1f}x average' if avg_vol > 0 else 'volume n/a',
                       f'level score {lv.score}',
                       'holding, not yet {} bars old'.format(false_break_bars)
                       if not decided else 'held beyond the false-break window'],
            ))

            if retest_idx is not None and retest_idx < n:
                # Did the retest HOLD? That is the entry the playbook wants.
                after = c[retest_idx:retest_idx + 4]
                held = bool((after > lv.price).all()) if broke_up else \
                       bool((after < lv.price).all())
                out.append(Event(
                    kind='retest', direction=direction,
                    bias='bullish' if broke_up else 'bearish',
                    idx=retest_idx, t=int(t[retest_idx]), price=float(lv.price),
                    extreme=float(l[retest_idx] if broke_up else h[retest_idx]),
                    strength=_s(40 + lv.score * 0.35 + (25 if held else 0)),
                    confirmed=held, bars_since=int(n - 1 - retest_idx),
                    label=f'retest of {lv.price:.2f}',
                    notes=[f'retested {retest_idx - i} bars after the break',
                           'held as support' if (held and broke_up) else
                           'held as resistance' if held else 'retest not yet held'],
                ))
            break             # first break of this level is the one that counts

    out.sort(key=lambda e: -e.idx)
    return out[:10]


# --------------------------------------------------------------------------- #
# reversal: the composite                                                     #
# --------------------------------------------------------------------------- #
def detect_reversal(events: list, breaks: list, c, t, atr_val,
                    rsi_arr=None) -> dict:
    """
    A reversal is not one thing, it is a coincidence of things.

    The text-book sequence is: sweep the liquidity, break structure against the
    old trend (CHoCH), momentum flips. Each is weak alone and strong together,
    so this returns a graded read rather than a boolean - the narrator quotes
    the components and the qualifier weighs the total.
    """
    n = c.size
    recent = [e for e in events if e.bars_since <= 12]
    sweep = next((e for e in recent if e.kind == 'sweep'), None)
    rejection = next((e for e in recent if e.kind == 'rejection'), None)
    false_break = next((e for e in recent if e.kind == 'false_break'), None)
    choch = next((b for b in reversed(breaks or [])
                  if b.kind == 'CHoCH' and (n - 1 - b.idx) <= 20), None)

    components, score, bias_votes = [], 0, []
    if sweep:
        score += sweep.strength * 0.30
        components.append(f'liquidity sweep of {sweep.price:.2f}')
        bias_votes.append(sweep.bias)
    if rejection:
        score += rejection.strength * 0.22
        components.append(f'rejection wick at {rejection.price:.2f}')
        bias_votes.append(rejection.bias)
    if false_break:
        score += false_break.strength * 0.22
        components.append(f'false break at {false_break.price:.2f}')
        bias_votes.append(false_break.bias)
    if choch:
        score += 26
        components.append(f'CHoCH {choch.direction} through {choch.price:.2f}')
        bias_votes.append('bullish' if choch.direction == 'up' else 'bearish')

    momentum_note = None
    if rsi_arr is not None and rsi_arr.size >= 3:
        tail = rsi_arr[~np.isnan(rsi_arr)]
        if tail.size >= 3:
            turn = tail[-1] - tail[-3]
            if abs(turn) >= 4:
                score += 10
                momentum_note = f'RSI turning {"up" if turn > 0 else "down"} ({turn:+.1f} over 3 bars)'
                bias_votes.append('bullish' if turn > 0 else 'bearish')

    if not bias_votes:
        return {'detected': False, 'score': 0, 'bias': None, 'components': []}

    bull = bias_votes.count('bullish')
    bear = bias_votes.count('bearish')
    bias = 'bullish' if bull > bear else 'bearish' if bear > bull else None
    agreement = max(bull, bear) / len(bias_votes)
    score = score * (0.5 + 0.5 * agreement)

    return {
        'detected': score >= 45 and bias is not None,
        'score': _s(score),
        'bias': bias,
        'agreement': round(agreement, 2),
        'components': components + ([momentum_note] if momentum_note else []),
        'has_sweep': sweep is not None,
        'has_choch': choch is not None,
        'confirmed': bool(score >= 60 and choch is not None and
                          (sweep is not None or false_break is not None)),
    }


__all__ = ['Event', 'detect_sweeps', 'detect_rejections', 'detect_breaks',
           'detect_reversal']
