"""
server/engine/structure.py - swing points, market structure, trend state.

This is the backbone: levels, trendlines, patterns and most signals are all
expressed in terms of the swing sequence this module produces. Get the swings
wrong and everything downstream is confidently wrong.

Two-stage swing detection, because one stage is never enough:

  1. FRACTAL   a bar whose high is the highest of the k bars either side.
               Cheap and local, but on M1 gold it fires constantly - every
               two-bar wiggle becomes a "swing".
  2. ATR FILTER a candidate only survives if the leg from the previous
               confirmed swing is worth at least `swing_atr_mult * ATR`.
               This is what separates structure from noise, and it adapts:
               the same code works on a quiet Tokyo range and a CPI spike.

Alternation is enforced last - highs and lows must interleave. Two highs in a
row means the second replaced the first (a higher high extends the leg); it is
not a new swing.

CONFIRMATION LAG IS REAL AND IS NOT HIDDEN. A fractal at index i cannot be
known until bar i+k has closed. `Swing.confirmed_at` records that, and the
backtester only allows a swing to be used from that bar onward. Skipping this
is the single easiest way to build a backtest that prints money and a live
system that does not.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

from .indicators import atr, last_valid


@dataclass
class Swing:
    idx: int            # bar index of the extreme
    t: int              # epoch ms of that bar
    price: float
    kind: str           # 'high' | 'low'
    confirmed_at: int   # bar index from which this swing was knowable
    strength: float = 0.0   # leg size in ATR multiples
    label: str = ''     # HH | HL | LH | LL
    swept: bool = False     # has price since traded through it?

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StructureBreak:
    idx: int
    t: int
    price: float           # the swing level that broke
    kind: str              # 'BOS' | 'CHoCH'
    direction: str         # 'up' | 'down'
    from_swing: int        # index of the swing that was broken

    def to_dict(self) -> dict:
        return asdict(self)


def structural_pool(swings: list, n_bars: int, want_swings: int = 14,
                    min_bars: int = 120, max_bars: int = 300) -> tuple:
    """
    The swings that describe CURRENT structure, plus the bar the window starts at.

    Trendlines and channels should be anchored in structure a trader can still
    see and still cares about. Handing the geometry search every swing in the
    buffer produced lines pinned to highs from two days ago - technically
    valid, drawn across the whole screen, and anchored off the left edge where
    nobody could check them.

    The window is measured in SWINGS rather than bars so its structural content
    stays roughly constant however fast the market happens to be moving, then
    clamped in bars so it cannot collapse to nothing in a dead session or run
    away in a violent one.

    Returns (pool, start_idx). `pool` keeps the ORIGINAL bar indices - callers
    pass the full price arrays alongside it, because the indices on the
    resulting trendlines are what the chart draws with.
    """
    if not swings:
        return [], max(0, n_bars - max_bars)

    recent = swings[-want_swings:] if len(swings) > want_swings else swings
    start = recent[0].idx
    # Never reach further back than max_bars, and always cover at least
    # min_bars - a quiet stretch must not shrink the window to a handful of
    # candles just because price stopped making swings.
    floor_idx = max(0, n_bars - max_bars)
    ceil_idx = max(0, n_bars - min_bars)
    start = int(min(max(start, floor_idx), ceil_idx))
    return [s for s in swings if s.idx >= start], start


def find_swings(h: np.ndarray, l: np.ndarray, c: np.ndarray, t: np.ndarray,
                k: int = 2, atr_mult: float = 0.55,
                atr_arr: np.ndarray = None, max_swings: int = 60) -> list:
    """
    Alternating, ATR-filtered swing points, oldest first.

    `atr_arr` may be passed in to avoid recomputing it; when omitted a 14-period
    ATR is used.
    """
    n = c.size
    if n < 2 * k + 3:
        return []
    a = atr_arr if atr_arr is not None else atr(h, l, c, 14)
    median_atr = float(np.nanmedian(a[~np.isnan(a)])) if np.isfinite(a).any() else 0.0
    if median_atr <= 0:
        median_atr = float(np.mean(h - l)) or 1e-6

    # --- stage 1: raw fractals -------------------------------------------- #
    raw = []
    for i in range(k, n - k):
        window_h = h[i - k:i + k + 1]
        window_l = l[i - k:i + k + 1]
        if h[i] == window_h.max() and (window_h.argmax() == k):
            raw.append((i, float(h[i]), 'high'))
        if l[i] == window_l.min() and (window_l.argmin() == k):
            raw.append((i, float(l[i]), 'low'))
    raw.sort(key=lambda r: (r[0], r[2]))

    # --- stage 2: ATR filter + alternation -------------------------------- #
    swings: list = []
    for idx, price, kind in raw:
        local_atr = a[idx] if idx < a.size and not np.isnan(a[idx]) else median_atr
        if local_atr <= 0:
            local_atr = median_atr
        if not swings:
            swings.append(Swing(idx=idx, t=int(t[idx]), price=price, kind=kind,
                                confirmed_at=idx + k))
            continue

        prev = swings[-1]
        if prev.kind == kind:
            # Same side twice: keep the more extreme one, it extends the leg.
            better = (price > prev.price) if kind == 'high' else (price < prev.price)
            if better:
                swings[-1] = Swing(idx=idx, t=int(t[idx]), price=price, kind=kind,
                                   confirmed_at=idx + k)
            continue

        leg = abs(price - prev.price)
        if leg < atr_mult * local_atr:
            # Too small to be structure. If this candidate is more extreme than
            # the swing BEFORE last, it still matters - it means the earlier
            # leg was the noise. Otherwise drop it.
            if len(swings) >= 2:
                before = swings[-2]
                extends = ((kind == 'high' and price > before.price) or
                           (kind == 'low' and price < before.price))
                if extends:
                    # Re-measure the leg against whatever now precedes it, so a
                    # replaced swing keeps a real strength rather than 0.
                    anchor = swings[-3] if len(swings) >= 3 else before
                    swings[-2] = Swing(
                        idx=idx, t=int(t[idx]), price=price, kind=kind,
                        confirmed_at=idx + k,
                        strength=round(abs(price - anchor.price) / local_atr, 2))
                    swings.pop()
            continue

        swings.append(Swing(idx=idx, t=int(t[idx]), price=price, kind=kind,
                            confirmed_at=idx + k,
                            strength=round(leg / local_atr, 2)))

    # --- label HH/HL/LH/LL and mark swept levels --------------------------- #
    highs = [s for s in swings if s.kind == 'high']
    lows = [s for s in swings if s.kind == 'low']
    for seq in (highs, lows):
        for i, s in enumerate(seq):
            if i == 0:
                continue
            prev = seq[i - 1]
            if s.kind == 'high':
                s.label = 'HH' if s.price > prev.price else 'LH'
            else:
                s.label = 'HL' if s.price > prev.price else 'LL'

    for s in swings:
        after_h = h[s.idx + 1:]
        after_l = l[s.idx + 1:]
        if s.kind == 'high':
            s.swept = bool(after_h.size and after_h.max() > s.price)
        else:
            s.swept = bool(after_l.size and after_l.min() < s.price)

    return swings[-max_swings:]


def structure_breaks(swings: list, h: np.ndarray, l: np.ndarray,
                     c: np.ndarray, t: np.ndarray) -> list:
    """
    BOS and CHoCH, detected on CLOSES.

    Closes, not wicks, deliberately. A wick through a swing high is a liquidity
    sweep - often the opposite signal - and treating it as a break is how a
    system ends up buying every top. Sweeps are handled in events.py.

    BOS   = a break that continues the existing direction.
    CHoCH = the first break against it, i.e. the character changed.
    """
    if len(swings) < 3:
        return []
    breaks: list = []
    direction = None            # current structural direction

    for si, sw in enumerate(swings):
        if si < 1:
            continue
        # Only bars after this swing was CONFIRMED may break it.
        start = max(sw.confirmed_at, sw.idx + 1)
        if start >= c.size:
            continue
        # A swing is broken once, by the first close through it.
        if sw.kind == 'high':
            hit = np.flatnonzero(c[start:] > sw.price)
        else:
            hit = np.flatnonzero(c[start:] < sw.price)
        if hit.size == 0:
            continue
        bar = int(start + hit[0])

        # Don't report a break of a swing that a later swing already superseded.
        later = [s for s in swings[si + 1:] if s.kind == sw.kind and s.idx < bar]
        if later:
            continue

        new_dir = 'up' if sw.kind == 'high' else 'down'
        kind = 'BOS' if (direction is None or direction == new_dir) else 'CHoCH'
        direction = new_dir
        breaks.append(StructureBreak(idx=bar, t=int(t[bar]), price=float(sw.price),
                                     kind=kind, direction=new_dir, from_swing=sw.idx))

    breaks.sort(key=lambda b: b.idx)
    # Re-derive BOS/CHoCH in time order; the per-swing walk above can emit them
    # out of sequence when two swings break on the same bar.
    direction = None
    for b in breaks:
        b.kind = 'BOS' if (direction is None or direction == b.direction) else 'CHoCH'
        direction = b.direction
    return breaks


def trend_state(swings: list, breaks: list, c: np.ndarray,
                ema_fast: np.ndarray = None, ema_slow: np.ndarray = None) -> dict:
    """
    The structural read, as a dict the narrator can speak from.

    Trend is decided by the SWING SEQUENCE first and confirmed by EMAs second.
    Doing it the other way round produces a system that calls a trend two legs
    after it started and gets chopped out of every range.
    """
    highs = [s for s in swings if s.kind == 'high'][-3:]
    lows = [s for s in swings if s.kind == 'low'][-3:]

    seq_score = 0
    pattern = []
    for s in ([x for x in swings if x.label][-4:]):
        pattern.append(s.label)
        if s.label in ('HH', 'HL'):
            seq_score += 1
        elif s.label in ('LH', 'LL'):
            seq_score -= 1

    last_break = breaks[-1] if breaks else None
    recent_choch = next((b for b in reversed(breaks) if b.kind == 'CHoCH'), None)

    # EMA agreement is a confirmation vote, worth one notch either way.
    ema_vote = 0
    if ema_fast is not None and ema_slow is not None:
        f, s_ = last_valid(ema_fast), last_valid(ema_slow)
        if f and s_:
            ema_vote = 1 if f > s_ else -1

    score = seq_score * 2 + ema_vote
    if last_break:
        score += 2 if last_break.direction == 'up' else -2

    if score >= 4:
        state, strength = 'trending up', 'HIGH' if score >= 6 else 'NORMAL'
    elif score <= -4:
        state, strength = 'trending down', 'HIGH' if score <= -6 else 'NORMAL'
    elif score >= 2:
        state, strength = 'trending up', 'WEAK'
    elif score <= -2:
        state, strength = 'trending down', 'WEAK'
    else:
        state, strength = 'ranging', 'NORMAL'

    # Invalidation: the level that, if closed through, kills the current read.
    invalidation = None
    if 'up' in state and lows:
        invalidation = float(lows[-1].price)
    elif 'down' in state and highs:
        invalidation = float(highs[-1].price)

    structure_label = ' + '.join(dict.fromkeys(pattern[-2:])) if pattern else '-'

    return {
        'state': state,
        'strength': strength,
        'score': int(score),
        'structure': structure_label,
        'sequence': pattern,
        'invalidation': invalidation,
        'last_break': last_break.to_dict() if last_break else None,
        'last_choch': recent_choch.to_dict() if recent_choch else None,
        'swing_high': float(highs[-1].price) if highs else None,
        'swing_low': float(lows[-1].price) if lows else None,
        'ema_agrees': (ema_vote > 0 and 'up' in state) or (ema_vote < 0 and 'down' in state),
    }


def leg_retracement(swings: list, price: float) -> dict:
    """
    Where is price inside the most recent impulse leg?

    Returns the Fibonacci read the pullback playbook is built on: the 0.5-0.786
    band is the "pocket" a continuation entry wants, and anything past 1.0 means
    the leg is dead, not retracing.
    """
    if len(swings) < 2:
        return {'valid': False}
    a, b = swings[-2], swings[-1]
    lo, hi = (a.price, b.price) if b.price > a.price else (b.price, a.price)
    span = hi - lo
    if span <= 0:
        return {'valid': False}
    up_leg = b.price > a.price
    # Retracement measured from the END of the leg back toward its start.
    pct = (hi - price) / span if up_leg else (price - lo) / span
    levels = {f'{r:g}': (hi - span * r if up_leg else lo + span * r)
              for r in (0.236, 0.382, 0.5, 0.618, 0.705, 0.786, 1.0)}
    if pct < 0:
        zone = 'extended'
    elif pct < 0.236:
        zone = 'shallow'
    elif pct < 0.5:
        zone = 'normal'
    elif pct <= 0.786:
        zone = 'pocket'
    elif pct <= 1.0:
        zone = 'deep'
    else:
        zone = 'broken'
    return {
        'valid': True,
        'direction': 'up' if up_leg else 'down',
        'leg_low': float(lo), 'leg_high': float(hi), 'span': float(span),
        'retracement': round(float(pct), 3),
        'zone': zone,
        'in_pocket': zone == 'pocket',
        'levels': {k: float(v) for k, v in levels.items()},
        'from_t': int(a.t), 'to_t': int(b.t),
    }


def impulse_retracement(swings: list, price: float, direction: str,
                        min_span: float = 0.0) -> dict:
    """
    Retracement of the last IMPULSE leg running in `direction`.

    This is the frame a pullback entry actually needs, and it is not the same as
    `leg_retracement`. In an uptrend the most recent leg is the pullback itself
    (high -> low), so measuring against it asks "how far has the dip retraced",
    which is the wrong question. What a buy-the-dip wants is the last up leg
    (low -> high) and how deeply price has come back into it.

    direction: 'up' looks for a low->high leg, 'down' for a high->low leg.
    """
    if len(swings) < 2:
        return {'valid': False}

    want_end = 'high' if direction == 'up' else 'low'
    # Walk back for the most recent completed impulse in this direction.
    for i in range(len(swings) - 1, 0, -1):
        end, start = swings[i], swings[i - 1]
        if end.kind != want_end or start.kind == end.kind:
            continue
        if direction == 'up' and end.price <= start.price:
            continue
        if direction == 'down' and end.price >= start.price:
            continue

        lo, hi = min(start.price, end.price), max(start.price, end.price)
        span = hi - lo
        # Skip insignificant impulses. Without this the search latches onto the
        # nearest two-swing wiggle - an 8-point leg on gold - and every pullback
        # reads as "broken" because price is naturally several times that away.
        if span <= 0 or span < min_span:
            continue
        # Retrace measured from the END of the impulse back toward its start.
        pct = (hi - price) / span if direction == 'up' else (price - lo) / span
        levels = {f'{r:g}': (hi - span * r if direction == 'up' else lo + span * r)
                  for r in (0.236, 0.382, 0.5, 0.618, 0.705, 0.786, 1.0)}
        if pct < 0:
            zone = 'extended'          # still making new highs/lows, no pullback
        elif pct < 0.236:
            zone = 'shallow'
        elif pct < 0.5:
            zone = 'normal'
        elif pct <= 0.786:
            zone = 'pocket'
        elif pct <= 1.0:
            zone = 'deep'
        else:
            zone = 'broken'            # retraced past the impulse origin
        return {
            'valid': True,
            'direction': direction,
            'impulse_start': float(start.price),
            'impulse_end': float(end.price),
            'leg_low': float(lo), 'leg_high': float(hi), 'span': float(span),
            'retracement': round(float(pct), 3),
            'zone': zone,
            'in_pocket': zone == 'pocket',
            'levels': {k: float(v) for k, v in levels.items()},
            'from_t': int(start.t), 'to_t': int(end.t),
            'bars_since_impulse': None,
        }
    return {'valid': False}


def equal_levels(swings: list, tolerance: float, kind: str = 'high') -> list:
    """
    Clusters of swings at effectively the same price - equal highs / equal lows.

    These are where resting stops pile up, so they are the magnets a sweep goes
    hunting for. Returns the richest clusters first.
    """
    pool = [s for s in swings if s.kind == kind]
    if len(pool) < 2:
        return []
    used, groups = set(), []
    for i, s in enumerate(pool):
        if i in used:
            continue
        members = [s]
        used.add(i)
        for j in range(i + 1, len(pool)):
            if j in used:
                continue
            if abs(pool[j].price - s.price) <= tolerance:
                members.append(pool[j])
                used.add(j)
        if len(members) >= 2:
            prices = [m.price for m in members]
            groups.append({
                'price': float(np.mean(prices)),
                'count': len(members),
                'kind': kind,
                'swept': all(m.swept for m in members),
                'from_t': int(min(m.t for m in members)),
                'to_t': int(max(m.t for m in members)),
                'indices': [m.idx for m in members],
            })
    groups.sort(key=lambda g: (-g['count'], g['from_t']))
    return groups


__all__ = ['Swing', 'StructureBreak', 'find_swings', 'structure_breaks',
           'trend_state', 'leg_retracement', 'equal_levels']
