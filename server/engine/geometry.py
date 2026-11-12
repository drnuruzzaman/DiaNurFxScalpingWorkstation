"""
server/engine/geometry.py - the big legs, their channels, and the trendlines a
trader draws on them. Read by analysis.py (so by the regime, the playbooks, the
forecast reads, the lab and the forward books) and drawn by both charts.

LEGS - a zigzag over the bars analysed: a turn counts once price reverses 30%
of their range (at least 4 ATR), so pullbacks inside a move do not split it.
The last two legs reaching into the default view (300 bars) are kept.

CHANNELS (2026-11-07 - until then the engine's channels were the structural
window's only, and these were display only):
  * per leg, the narrowest pair of parallel lines holding its highs and lows,
    all but the most extreme 1% of bars per side - so the edges run along the
    swings a hand-drawn channel connects, not along one spike;
  * channels fitted across the whole default view (every valid line tried);
  * ONE per kind - ascending, descending, horizontal: a leg channel takes its
    kind, the rest are filled from the engine's structural and full-view ones.
  The CURRENT leg's channel comes first: it is the one the regime reads.

TRENDLINES (2026-11-07), for each of those legs:
  * a RESISTANCE line through its swing highs and a SUPPORT line through its
    swing lows - edges of the hull of that side's swings, running the leg's way,
    chosen for length in the later part of the leg and for touches;
  * the PAUSES - the pullbacks inside the leg, as a trader boxes them - and,
    in the current leg, the range price is in now.
A finished leg's lines stop at its turn; the current leg's run to the last bar.
"""
from __future__ import annotations

from dataclasses import fields

import numpy as np


LEG_TURN_RANGE = 0.30       # a turn reverses this share of the bars' range...
LEG_TURN_ATR = 4.0          # ...and never less than this many ATR
LEG_MIN_BARS = 20
VIEW_BARS = 300             # the chart's default view (ChartEngine DEFAULT_SPAN)
PAUSES_PER_LEG = 2          # the longest pauses (ranges before a break) kept per leg
PAUSE_MIN_BARS = 8          # shorter is a breath, not a pause
PAUSE_TURN_ATR = 2.5        # a pullback inside a leg reverses at least this many ATR
LINE_LEAD = 0.25            # a line starts this share of its anchor span before its first swing


def zigzag(h, l, a: int, b: int, thr: float):
    """Turning points in [a, b], each confirmed by a reversal of `thr`; and
    the current leg's running extreme."""
    piv, trend, hi, lo = [], None, a, a
    for i in range(a, b + 1):
        if trend is None:
            if h[i] > h[hi]:
                hi = i
            if l[i] < l[lo]:
                lo = i
            if h[hi] - l[lo] >= thr:
                trend = 'up' if lo < hi else 'down'
                piv.append(lo if lo < hi else hi)
        elif trend == 'up':
            if h[i] >= h[hi]:
                hi = i
            elif h[hi] - l[i] >= thr:
                piv.append(hi)
                trend, lo = 'down', i
        else:
            if l[i] <= l[lo]:
                lo = i
            elif h[i] - l[lo] >= thr:
                piv.append(lo)
                trend, hi = 'up', i
    cur = hi if trend == 'up' else lo if trend == 'down' else None
    return piv, cur


def hull(xs, ys, upper: bool) -> list:
    """The upper (or lower) convex hull of points already sorted by x."""
    out: list = []
    for p in zip(xs, ys):
        while len(out) >= 2:
            (x1, y1), (x2, y2) = out[-2], out[-1]
            cross = (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1)
            if (cross >= 0) if upper else (cross <= 0):
                out.pop()
            else:
                break
        out.append(p)
    return out


def legs(h, l, atr_now: float, n: int, keep: int = 2) -> list:
    """[(start, end, live)] of the last `keep` legs reaching into the view,
    over bars [0, n). `live` marks the leg still running (end = last bar)."""
    if n < 60 or atr_now <= 0:
        return []
    b, view = n - 1, max(0, n - VIEW_BARS)
    span = float(h[:n].max() - l[:n].min())
    piv, cur = zigzag(h, l, 0, b, max(LEG_TURN_ATR * atr_now, LEG_TURN_RANGE * span))
    pts = piv + ([cur] if cur is not None and piv and cur > piv[-1] else [])
    out = []
    for k in range(len(pts) - 1):
        i0 = pts[k]
        live = k == len(pts) - 2 and pts[k + 1] == cur
        end = b if live else pts[k + 1]
        if end - i0 >= LEG_MIN_BARS and end > view:
            out.append((i0, end, live))
    return out[-keep:]


def _side_line(swings, h, l, i0: int, end: int, kind: str, up: bool, atr_now: float):
    """(x1, y1, x2, y2, touches) of the boundary through one side's swings."""
    pts = {(s.idx, float(s.price)) for s in swings if s.kind == kind and i0 <= s.idx <= end}
    if (kind == 'low') == up:                     # the leg starts on this side
        pts.add((i0, float(l[i0] if up else h[i0])))
    pts = sorted(pts)
    if len(pts) < 2:
        return None
    best = None
    edges = hull([p[0] for p in pts], [p[1] for p in pts], kind == 'high')
    for (x1, y1), (x2, y2) in zip(edges[:-1], edges[1:]):
        if x2 <= x1:
            continue
        k = (y2 - y1) / (x2 - x1)
        if (k > 0) != up:
            continue                              # must run the leg's way
        late = ((x2 - i0) / max(1, end - i0)) ** 2
        touches = sum(1 for x, y in pts if abs(y - (y1 + k * (x - x1))) <= 0.35 * atr_now)
        score = (x2 - x1) * late + touches * 3
        if best is None or score > best[0]:
            best = (score, x1, y1, x2, y2, touches)
    return best[1:] if best else None


def trendlines(t, h, l, c, swings: list, breaks: list, atr_now: float) -> dict:
    """{'lines': [...], 'levels': [...]} for the chart, in TIME - so the live
    chart and the lab's, whose bar indices differ, draw them the same way.

    lines:  {kind, t1, y1, t2, y2, slope_bar, end_t, live}  - resistance/support;
            drawn from the leg's start to `end_t`, or to the right edge if live.
    levels: {kind, price, from_t, to_t}  - to_t None runs to the right edge.
    """
    n = len(t)
    lines, levels = [], []
    for i0, end, live in legs(h, l, atr_now, n):
        up = c[end] > c[i0]
        for kind in ('high', 'low'):
            r = _side_line(swings, h, l, i0, end, kind, up, atr_now)
            if r is None:
                continue
            x1, y1, x2, y2, touches = r
            k = (y2 - y1) / (x2 - x1)
            # From a little before its first swing, not from the leg's start: a
            # steep line projected back over a flat base lands far off the
            # chart (a 15m support at 3572 under a 3870 low).
            a = max(i0, int(round(x1 - LINE_LEAD * (x2 - x1))))
            lines.append({'kind': 'resistance' if kind == 'high' else 'support',
                          't1': int(t[a]), 'y1': float(y1 + k * (a - x1)),
                          't2': int(t[end]), 'y2': float(y1 + k * (end - x1)),
                          'slope_bar': float(k), 'live': bool(live), 'touches': int(touches)})
        # The PAUSES - the pullbacks inside the leg, as a trader boxes them: in
        # a rally, a high, the low price pulled back to, and the bar that closed
        # back above the high (a decline mirrors it). Found by a small zigzag
        # (PAUSE_TURN_ATR) inside the leg. The longest few that end inside the
        # view are kept, one per area of price; a pullback nothing has closed
        # through yet is the range price is in now, drawn to the right edge.
        view = max(0, n - VIEW_BARS)
        piv, _ = zigzag(h, l, i0, end, PAUSE_TURN_ATR * atr_now)
        boxes, live_box = [], None
        for p0, p1 in zip(piv[:-1], piv[1:]):
            is_top = h[p0] >= h[p1] if up else l[p0] <= l[p1]
            if not is_top:
                continue                  # a pullback starts at the leg's-way extreme
            edge = float(h[p0] if up else l[p0])
            other = float(l[p1] if up else h[p1])
            after = c[p1 + 1:end + 1]
            hit = np.flatnonzero(after > edge) if up else np.flatnonzero(after < edge)
            if hit.size:
                j = p1 + 1 + int(hit[0])
                if j > view and j - p0 >= PAUSE_MIN_BARS:
                    boxes.append((j - p0, p0, j, edge, other))
            elif live:
                live_box = (p0, edge, other)
        kept = []
        for box in sorted(boxes, reverse=True):
            # One per pause: not at the same price, and not overlapping in time.
            if all(abs(box[3] - k[3]) > 0.5 * atr_now and (box[2] <= k[1] or box[1] >= k[2])
                   for k in kept):
                kept.append(box)
            if len(kept) >= PAUSES_PER_LEG:
                break
        for _, a, j, edge, other in sorted(kept, key=lambda b: b[2]):
            levels.append({'kind': 'resistance' if up else 'support', 'price': edge,
                           'from_t': int(t[a]), 'to_t': int(t[j])})
            levels.append({'kind': 'support' if up else 'resistance', 'price': other,
                           'from_t': int(t[a]), 'to_t': int(t[j])})
        if live_box is not None:
            a, edge, other = live_box
            levels.append({'kind': 'resistance' if up else 'support', 'price': edge,
                           'from_t': int(t[a]), 'to_t': None})
            levels.append({'kind': 'support' if up else 'resistance', 'price': other,
                           'from_t': int(t[a]), 'to_t': None})
    return {'lines': lines, 'levels': levels}


# --------------------------------------------------------------------------- #
# channels                                                                     #
# --------------------------------------------------------------------------- #
LEG_SPIKE_SHARE = 0.01      # of the leg's bars, per side, a channel edge may ignore
WIDE_MAX_LINES = 40         # every valid line is offered to the full-view channel finder
WIDE_MAX_CHANNELS = 6       # candidates; one_per_kind() picks


def narrowest(h, l, i0: int, end: int):
    """(slope, top, bottom) of the narrowest parallel strip holding the highs
    and lows in [i0, end] - all but the most extreme 1% of bars on each side.
    The optimum lies on a hull edge's slope, so only those are tried."""
    xs = np.arange(i0, end + 1, dtype=float)
    hh, ll = h[i0:end + 1], l[i0:end + 1]
    skip = int(len(xs) * LEG_SPIKE_SHARE)
    slopes = set()
    for edges in (hull(xs, hh, True), hull(xs, ll, False)):
        for (x1, y1), (x2, y2) in zip(edges[:-1], edges[1:]):
            if x2 > x1:
                slopes.add((y2 - y1) / (x2 - x1))
    best = None
    for k in slopes:
        top = float(np.sort(hh - k * xs)[::-1][skip])
        bot = float(np.sort(ll - k * xs)[skip])
        if best is None or top - bot < best[1] - best[2]:
            best = (k, top, bot)
    return best


def leg_channels(t, h, l, c, atr_now: float) -> list:
    """The last two legs' channels, in bar indices of the arrays given; the
    current leg's first."""
    n = len(t)
    out = []
    tol = 0.35 * atr_now
    for i0, end, live in legs(h, l, atr_now, n):
        fit = narrowest(h, l, i0, end)
        if fit is None:
            continue
        slope, top, bot = fit
        width = top - bot
        per_atr = slope / atr_now
        # Same flatness rule as trendlines.find_channels (0.15 ATR / 20 bars).
        from .trendlines import HORIZONTAL_ATR_PER_BAR as flat
        kind = ('ascending' if per_atr > flat else
                'descending' if per_atr < -flat else 'horizontal')
        xs = np.arange(i0, end + 1)
        tu = int((np.abs(h[i0:end + 1] - (top + slope * xs)) <= tol).sum())
        tlo = int((np.abs(l[i0:end + 1] - (bot + slope * xs)) <= tol).sum())
        inside = ((h[i0:end + 1] <= top + slope * xs + tol) &
                  (l[i0:end + 1] >= bot + slope * xs - tol))

        def edge(y0, i0=i0, end=end, slope=slope):
            return {'x1': int(i0), 'y1': float(y0 + slope * i0),
                    'x2': int(end), 'y2': float(y0 + slope * end),
                    't1': int(t[i0]), 't2': int(t[end]), 'slope': float(slope)}

        out.append({
            'kind': kind, 'upper': edge(top), 'lower': edge(bot), 'slope': float(slope),
            'containment': round(float(inside.mean()), 3),
            'touches_upper': tu, 'touches_lower': tlo,
            'width': round(width, 5), 'width_atr': round(width / atr_now, 2),
            'score': 100,
            'position': round((float(c[-1]) - (bot + slope * (n - 1))) / (width or 1.0), 3),
            't1': int(t[i0]), 't2': int(t[end]), 'source': 'leg', 'live': bool(live),
        })
    return sorted(out, key=lambda ch: not ch['live'])


def wide_channels(t, h, l, c, swings: list, atr_now: float, cfg) -> list:
    """Channels fitted over the default view, from every swing in it."""
    from . import trendlines as tlm
    n = len(t)
    start = max(0, n - VIEW_BARS)
    pool = [s for s in swings if s.idx >= start]
    if len(pool) < 3 or atr_now <= 0:
        return []
    lines = tlm.find_trendlines(h, l, c, t, pool, atr_now, cfg.tl_min_touches,
                                cfg.tl_max_violation_atr, WIDE_MAX_LINES)
    found = tlm.find_channels(h, l, c, t, lines, atr_now, cfg.channel_min_containment,
                              WIDE_MAX_CHANNELS)
    return [ch.to_dict() for ch in found]


def one_per_kind(channels: list) -> list:
    """The best channel of each kind by channel score; a tie goes to the longer."""
    best: dict = {}
    for ch in channels:
        k = ch.get('kind')
        rank = (int(ch.get('score') or 0), -int((ch.get('upper') or {}).get('x1') or 0))
        if k not in best or rank > best[k][0]:
            best[k] = (rank, ch)
    return [ch for _, ch in sorted(best.values(), key=lambda r: r[0], reverse=True)]


def channels(t, h, l, c, swings: list, atr_now: float, structural: list, cfg) -> list:
    """The channel set: one per kind, leg channels taking their kind (the
    current leg's first), the remaining kinds filled from `structural` (the
    engine's own, as dicts) and the full-view channels."""
    if atr_now <= 0 or len(t) < 60:
        return list(structural)
    mine: dict = {}
    for ch in leg_channels(t, h, l, c, atr_now):
        mine.setdefault(ch['kind'], ch)          # the current leg comes first
    others = list(structural) + wide_channels(t, h, l, c, swings, atr_now, cfg)
    rest = [ch for ch in one_per_kind(others) if ch.get('kind') not in mine]
    return list(mine.values()) + rest


def as_channel(d: dict):
    """A channel dict as trendlines.Channel, for regime.classify."""
    from .trendlines import Channel
    names = {f.name for f in fields(Channel)}
    return Channel(**{k: d[k] for k in names if k in d})


__all__ = ['zigzag', 'hull', 'legs', 'narrowest', 'leg_channels', 'wide_channels',
           'one_per_kind', 'channels', 'as_channel', 'trendlines']
