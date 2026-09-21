"""
server/engine/trendlines.py - trendlines and the channels they anchor.

An honest trendline has to earn its place, because any two points define a line
and a 600-bar chart offers hundreds of pairs. A candidate here survives only if:

    * it connects two confirmed swings of the SAME kind (two lows, or two highs)
    * at least one further swing TOUCHES it within tolerance
    * price never closed through it by more than `max_violation` between the
      anchors - a line price has already sliced through is history
    * it has a sane slope - near-vertical lines are artefacts of a gap, not
      structure

Scoring rewards touches, length and how recently price respected it, and
penalises age since the last touch. The result is the handful of lines a human
analyst would actually draw.

Channels are built by taking a validated line and sweeping a parallel copy out
to the furthest opposing extreme, then measuring CONTAINMENT: the fraction of
bars that actually sat inside. A "channel" containing 55% of the bars is not a
channel, and is dropped.

Everything is computed in (bar index, price) space and converted to timestamps
only on the way out, so slope is comparable across timeframes.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from itertools import combinations

import numpy as np


@dataclass
class Trendline:
    kind: str                # 'support' | 'resistance'
    x1: int                  # bar index of first anchor
    y1: float
    x2: int                  # bar index of second anchor
    y2: float
    t1: int                  # epoch ms
    t2: int
    slope: float             # price per bar
    touches: int
    score: int               # 0..100
    broken: bool
    price_now: float         # where the line sits at the last bar
    distance_atr: float
    touch_idx: list
    age_bars: int

    def value_at(self, x: float) -> float:
        return self.y1 + self.slope * (x - self.x1)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['angle'] = round(float(np.degrees(np.arctan(self.slope))), 2)
        return d


@dataclass
class Channel:
    kind: str                # 'ascending' | 'descending' | 'horizontal'
    upper: dict              # {x1,y1,x2,y2,slope}
    lower: dict
    slope: float
    containment: float       # 0..1, fraction of bars inside
    touches_upper: int
    touches_lower: int
    width: float             # average vertical height, in price
    width_atr: float
    score: int
    position: float          # where price sits inside it, 0 = lower, 1 = upper
    t1: int
    t2: int

    def to_dict(self) -> dict:
        return asdict(self)


def _line_through(x1, y1, x2, y2):
    if x2 == x1:
        return None
    return (y2 - y1) / (x2 - x1)


def find_trendlines(h, l, c, t, swings: list, atr_val: float,
                    min_touches: int = 3, max_violation_atr: float = 0.45,
                    max_lines: int = 8) -> list:
    """
    Validated support and resistance trendlines, best first.

    `min_touches` counts the two anchors, so 3 means "two anchors plus one
    confirming touch".
    """
    if len(swings) < 3 or atr_val <= 0 or c.size == 0:
        return []
    n = c.size
    tol = 0.35 * atr_val
    max_violation = max_violation_atr * atr_val
    last_x = n - 1
    candidates: list = []

    for kind, want in (('support', 'low'), ('resistance', 'high')):
        pool = [s for s in swings if s.kind == want]
        if len(pool) < 2:
            continue
        for a, b in combinations(pool, 2):
            if b.idx - a.idx < 5:
                continue                     # anchors too close to mean anything
            slope = _line_through(a.idx, a.price, b.idx, b.price)
            if slope is None:
                continue
            # Reject near-vertical lines: more than ~1 ATR of drift per bar is a
            # gap artefact, not a trendline anyone could trade.
            if abs(slope) > atr_val:
                continue

            def line_at(x):
                return a.price + slope * (x - a.idx)

            # --- violation test over the anchor span ---------------------- #
            xs = np.arange(a.idx, min(b.idx + 1, n))
            vals = a.price + slope * (xs - a.idx)
            if kind == 'support':
                excursion = vals - l[xs]
            else:
                excursion = h[xs] - vals
            if excursion.size and float(excursion.max()) > max_violation:
                continue

            # --- count touches across the whole pool ---------------------- #
            touch_idx = []
            for s in pool:
                if s.idx < a.idx or s.idx > last_x:
                    continue
                if abs(s.price - line_at(s.idx)) <= tol:
                    touch_idx.append(s.idx)
            if len(touch_idx) < min_touches:
                continue

            # --- has price closed through it since? ----------------------- #
            tail = np.arange(b.idx, n)
            tail_vals = a.price + slope * (tail - a.idx)
            if kind == 'support':
                through = c[tail] < tail_vals - max_violation
            else:
                through = c[tail] > tail_vals + max_violation
            broken = bool(through.sum() >= 2)

            here = float(line_at(last_x))
            span = b.idx - a.idx
            age = last_x - max(touch_idx)

            score = (
                min(len(touch_idx), 6) * 13 +                 # up to 78
                min(span / n, 1.0) * 14 +                     # length
                max(0.0, 1.0 - age / 60.0) * 14 +             # recency of respect
                (-25 if broken else 0)
            )
            candidates.append(Trendline(
                kind=kind, x1=a.idx, y1=float(a.price), x2=b.idx, y2=float(b.price),
                t1=int(t[a.idx]), t2=int(t[b.idx]), slope=float(slope),
                touches=len(touch_idx),
                score=int(max(0, min(100, round(score)))),
                broken=broken, price_now=round(here, 3),
                distance_atr=round((here - float(c[-1])) / atr_val, 2),
                touch_idx=sorted(set(touch_idx)), age_bars=int(age),
            ))

    # Deduplicate: many anchor pairs describe the same line. Keep the best of
    # any group whose value at the last bar is within tolerance and whose slope
    # broadly agrees.
    candidates.sort(key=lambda tl: -tl.score)
    kept: list = []
    for cand in candidates:
        dup = False
        for k in kept:
            if k.kind != cand.kind:
                continue
            if (abs(k.price_now - cand.price_now) < tol * 1.5 and
                    abs(k.slope - cand.slope) < atr_val * 0.08):
                dup = True
                break
        if not dup:
            kept.append(cand)
        if len(kept) >= max_lines:
            break
    return kept


def find_channels(h, l, c, t, trendlines: list, atr_val: float,
                  min_containment: float = 0.72, max_channels: int = 3) -> list:
    """
    Turn validated trendlines into channels by projecting a parallel boundary.

    The parallel is placed at the furthest opposing excursion over the line's
    active span, then containment is measured. Anything that does not actually
    contain price is discarded rather than drawn.
    """
    if not trendlines or atr_val <= 0 or c.size == 0:
        return []
    n = c.size
    out: list = []

    for tl in trendlines:
        if tl.broken:
            continue
        start = tl.x1
        xs = np.arange(start, n)
        if xs.size < 12:
            continue
        base = tl.y1 + tl.slope * (xs - tl.x1)

        # Place the parallel at the 93rd percentile of excursion, not the max.
        # Anchoring on the max puts the boundary beyond every bar by definition,
        # so containment came back 1.00 for everything and measured nothing. A
        # percentile lets a genuine channel score high and a loose one score low,
        # at the cost of allowing a few spike wicks outside - which is exactly
        # how a human would draw it.
        if tl.kind == 'support':
            offset = float(np.percentile(h[xs] - base, 93))
            if offset <= 0:
                continue
            lower_b, upper_b = base, base + offset
        else:
            offset = float(np.percentile(base - l[xs], 93))
            if offset <= 0:
                continue
            upper_b, lower_b = base, base - offset

        width = float(np.mean(upper_b - lower_b))
        # Too thin to trade, or so wide it is just "the chart" rather than a
        # channel a scalp can use.
        if width <= atr_val * 0.8 or width > atr_val * 12.0:
            continue

        inside = ((c[xs] >= lower_b - atr_val * 0.25) &
                  (c[xs] <= upper_b + atr_val * 0.25))
        containment = float(inside.mean())
        if containment < min_containment:
            continue

        tol = atr_val * 0.35
        touches_u = int((np.abs(h[xs] - upper_b) <= tol).sum())
        touches_l = int((np.abs(l[xs] - lower_b) <= tol).sum())
        if touches_u < 2 or touches_l < 2:
            continue

        slope_per_atr = tl.slope / atr_val
        if slope_per_atr > 0.05:
            kind = 'ascending'
        elif slope_per_atr < -0.05:
            kind = 'descending'
        else:
            kind = 'horizontal'

        pos_span = upper_b[-1] - lower_b[-1]
        position = 0.5 if pos_span <= 0 else float((c[-1] - lower_b[-1]) / pos_span)

        score = int(max(0, min(100, round(
            containment * 55 +
            min(touches_u + touches_l, 10) * 3.5 +
            min(xs.size / n, 1.0) * 10
        ))))

        out.append(Channel(
            kind=kind,
            upper={'x1': int(xs[0]), 'y1': float(upper_b[0]),
                   'x2': int(xs[-1]), 'y2': float(upper_b[-1]),
                   't1': int(t[xs[0]]), 't2': int(t[xs[-1]]), 'slope': float(tl.slope)},
            lower={'x1': int(xs[0]), 'y1': float(lower_b[0]),
                   'x2': int(xs[-1]), 'y2': float(lower_b[-1]),
                   't1': int(t[xs[0]]), 't2': int(t[xs[-1]]), 'slope': float(tl.slope)},
            slope=float(tl.slope),
            containment=round(containment, 3),
            touches_upper=touches_u, touches_lower=touches_l,
            width=round(width, 3), width_atr=round(width / atr_val, 2),
            score=score, position=round(max(-0.2, min(1.2, position)), 3),
            t1=int(t[xs[0]]), t2=int(t[xs[-1]]),
        ))

    # Drop near-duplicate channels built from parallel lines.
    out.sort(key=lambda ch: -ch.score)
    kept: list = []
    for ch in out:
        if any(abs(k.upper['y2'] - ch.upper['y2']) < atr_val and
               abs(k.lower['y2'] - ch.lower['y2']) < atr_val for k in kept):
            continue
        kept.append(ch)
        if len(kept) >= max_channels:
            break
    return kept


def regression_channel(c: np.ndarray, t: np.ndarray, lookback: int,
                       atr_val: float, devs: float = 2.0) -> dict:
    """
    A least-squares channel over the last `lookback` bars.

    Always defined, unlike the swing-anchored channels above, so the UI has a
    baseline slope to draw even when no clean structural channel exists.
    """
    n = c.size
    if n < 20 or atr_val <= 0:
        return {}
    lb = int(min(lookback, n))
    y = c[-lb:]
    x = np.arange(lb, dtype=np.float64)
    slope, intercept = np.polyfit(x, y, 1)
    fit = slope * x + intercept
    resid = y - fit
    sd = float(np.std(resid)) or atr_val * 0.5
    return {
        'slope': float(slope),
        'slope_atr_per_bar': round(float(slope / atr_val), 4),
        'mid': {'y1': float(fit[0]), 'y2': float(fit[-1])},
        'upper': {'y1': float(fit[0] + devs * sd), 'y2': float(fit[-1] + devs * sd)},
        'lower': {'y1': float(fit[0] - devs * sd), 'y2': float(fit[-1] - devs * sd)},
        't1': int(t[-lb]), 't2': int(t[-1]),
        'x1': int(n - lb), 'x2': int(n - 1),
        'sd': sd,
        'r2': round(float(1 - np.var(resid) / max(np.var(y), 1e-12)), 3),
        'position': round(float((c[-1] - (fit[-1] - devs * sd)) /
                                max(2 * devs * sd, 1e-9)), 3),
    }


__all__ = ['Trendline', 'Channel', 'find_trendlines', 'find_channels',
           'regression_channel']
