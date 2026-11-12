"""
Range detection from swings - the Python port of web/src/chart/ranges.ts.

Same rules as the chart (agreed 2026-11-08), so the engine and the chart can
mean the same thing by "range":

  * edges come from swing highs and swing lows that CLUSTER (at least two
    swing highs near the top and two swing lows near the bottom), never from
    the highest high / lowest low over a lookback
  * height 1.2-6 x ATR; the EDGES (touching swings) tilt at most 0.15 x ATR per 20 bars;
    at least 15 bars (8 for a range still forming)
  * a breakout is TWO closes in a row more than 0.3 x ATR past an edge - a
    single close outside never ends a range; the range then stops extending
  * tiers: major / minor / developing

STATUS (2026-11-08): referenced by nothing in the engine. It exists for the
parity test (tools/test_ranges.py) and the shadow report
(tools/range_shadow.py). Wiring it into regime / signals / patterns is a
separate, flagged step - see docs/proposals/2026-11-08-range-engine-port.md.

Point in time: find_ranges(bars[:i+1]) uses nothing after bar i. A swing
needs PIVOT_K bars after it, so the newest swings appear PIVOT_K bars late,
exactly as they would live.

Written with plain Python floats and the same operation order as the TS, so
both return identical boxes (numpy's pairwise sums would round differently).
Keep the two files in step: change one, change the other, rerun the parity
test.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

# --- tuning: identical to ranges.ts ---------------------------------------- #
PIVOT_K = 3
SWING_MIN_ATR = 0.6
MIN_H_ATR = 1.2
MAX_H_ATR = 6
MAX_SLOPE_ATR = 0.15
SLOPE_BARS = 20
MIN_BARS = 15
FORMING_MIN_BARS = 8
EDGE_ATR = 0.8               # a swing is at an edge within max(EDGE_ATR x ATR,
EDGE_H = 0.15                # EDGE_H x height) - see ranges.ts
LOOK_SWINGS = 8
BREAK_ATR = 0.3
RECLAIM_BARS = 3
STRONG_BARS = 12
STRONG_MOVE = 1.0
STRONG_ATR = 2.5
MAJOR_BARS = 48
MAJOR_TOUCHES = 7
SIMILAR = 0.25
NESTED = 0.7


@dataclass
class RangeBox:
    i0: int
    i1: int
    t0: int
    t1: int
    top: float
    bot: float
    tier: str               # 'major' | 'minor' | 'developing'
    active: bool            # no confirmed breakout yet
    breakout: str | None    # 'up' | 'down' | None
    strong: bool
    touchesTop: int
    touchesBot: int

    def to_dict(self) -> dict:
        return asdict(self)


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def atr_series(h, l, c) -> list:
    n = len(c)
    out = [0.0] * n
    q: list = []
    s = 0.0
    for i in range(n):
        if i == 0:
            v = h[0] - l[0]
        else:
            v = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        q.append(v)
        s += v
        if len(q) > 14:
            s -= q.pop(0)
        out[i] = s / len(q)
    return out


def swings(h, l, atr) -> list:
    """Fractal pivots, cleaned into an alternating zigzag with an ATR floor.
    Each swing is (bar index, price, is_high)."""
    n = len(h)
    raw = []
    for i in range(PIVOT_K, n - PIVOT_K):
        is_h = is_l = True
        for k in range(i - PIVOT_K, i + PIVOT_K + 1):
            if k == i:
                continue
            if h[k] > h[i]:
                is_h = False
            if l[k] < l[i]:
                is_l = False
        if is_h:
            raw.append((i, h[i], True))
        if is_l:
            raw.append((i, l[i], False))
    zz: list = []
    for s in raw:
        if not zz:
            zz.append(s)
            continue
        last = zz[-1]
        if last[2] == s[2]:
            if (s[1] >= last[1]) if s[2] else (s[1] <= last[1]):
                zz[-1] = s
            continue
        if abs(s[1] - last[1]) < atr[s[0]] * SWING_MIN_ATR:
            continue
        zz.append(s)
    return zz


def _edge_slope(sw, i0: int, i1: int, top: float, bot: float, tol: float) -> float:
    """Tilt of the range's edges, price per bar: one least-squares slope over
    the swing highs touching the top and the swing lows touching the bottom,
    each side about its own mean. A side with < 2 touches adds nothing."""
    num = den = 0.0
    for hi in (True, False):
        pts = [x for x in sw if x[2] == hi and i0 <= x[0] <= i1
               and abs(x[1] - (top if hi else bot)) <= tol]
        if len(pts) < 2:
            continue
        xm = ym = 0.0
        for x in pts:
            xm += x[0]
            ym += x[1]
        xm /= len(pts)
        ym /= len(pts)
        for x in pts:
            num += (x[0] - xm) * (x[1] - ym)
            den += (x[0] - xm) * (x[0] - xm)
    return num / den if den else 0.0


def find_ranges(t, h, l, c, trace: list = None) -> list:
    """Every range in the bars, oldest first. Inputs are sequences of equal
    length (times in ms, highs, lows, closes); numpy arrays are accepted.

    `trace`, when a list, receives one dict per swing tried as a range start
    - its box when one was found and why it was not kept - to answer "why is
    this not a range?" (Python only; the chart has no equivalent)."""
    t = [int(x) for x in t]
    h = [float(x) for x in h]
    l = [float(x) for x in l]
    c = [float(x) for x in c]
    n = len(c)
    if n < MIN_BARS + 20:
        return []
    atr = atr_series(h, l, c)
    sw = swings(h, l, atr)
    cands: list = []

    def note(s, why, **kw):
        if trace is not None:
            trace.append({'i0': sw[s][0], 't0': t[sw[s][0]], 'swing': sw[s], 'why': why, **kw})

    for s in range(len(sw) - 2):
        a = atr[sw[s][0]] or atr[n - 1]
        if not a > 0:
            continue
        found = None
        tried = []
        ln = 3
        while ln <= LOOK_SWINGS and s + ln <= len(sw):
            win = sw[s:s + ln]
            hs = [x for x in win if x[2]]
            ls = [x for x in win if not x[2]]
            if hs and ls:
                top = max(x[1] for x in hs)
                bot = min(x[1] for x in ls)
                hh = top - bot
                if hh < a * MIN_H_ATR or hh > a * MAX_H_ATR:
                    tried.append(f'{ln} swings: height {hh / a:.1f} ATR')
                else:
                    channel = False
                    if len(hs) >= 2 and len(ls) >= 2:
                        dh = hs[-1][1] - hs[0][1]
                        dl = ls[-1][1] - ls[0][1]
                        channel = (_sign(dh) == _sign(dl) and abs(dh) > hh * 0.5
                                   and abs(dl) > hh * 0.5)
                    if channel:
                        tried.append(f'{ln} swings: channel (highs {dh:+.1f}, lows {dl:+.1f})')
                    else:
                        tol = max(a * EDGE_ATR, hh * EDGE_H)
                        nt = [x for x in hs if top - x[1] <= tol]
                        nb = [x for x in ls if x[1] - bot <= tol]
                        def_end = win[-1][0]
                        # Edges must interleave (see ranges.ts): a staircase
                        # of two ranges is not one range.
                        interleaved = bool(nt and nb and nt[0][0] < nb[-1][0]
                                           and nb[0][0] < nt[-1][0])
                        if len(nt) >= 2 and len(nb) >= 2 and interleaved:
                            found = (top, bot, def_end, False)
                            break
                        if s + ln == len(sw) and len(nt) + len(nb) >= 3:
                            found = (top, bot, def_end, True)
                        tried.append(f'{ln} swings: {bot:.1f}-{top:.1f} edge tests top {len(nt)} '
                                     f'bottom {len(nb)} (need 2+2, within {tol:.1f})'
                                     + ('' if interleaved or len(nt) < 2 or len(nb) < 2
                                        else '') + ('; not interleaved' if len(nt) >= 2
                                                    and len(nb) >= 2 and not interleaved else ''))
            ln += 1
        if found is None:
            note(s, 'no box', tried=tried)
            continue
        top, bot, def_end, forming = found
        hh = top - bot
        i0 = sw[s][0]
        box = {'bot': bot, 'top': top, 'atr': a}
        if any(c[k] > top + a * BREAK_ATR or c[k] < bot - a * BREAK_ATR
               for k in range(i0, def_end + 1)):
            note(s, 'a close outside while the edges were forming', **box)
            continue

        i1 = n - 1
        breakout = None
        k = def_end + 1
        while k < n:
            ak = atr[k] or a
            up_by = c[k] - top
            dn_by = bot - c[k]
            prev_up = k > 0 and c[k - 1] - top > ak * BREAK_ATR
            prev_dn = k > 0 and bot - c[k - 1] > ak * BREAK_ATR
            d = ('up' if up_by > ak * BREAK_ATR and prev_up
                 else 'down' if dn_by > ak * BREAK_ATR and prev_dn else None)
            if d is None:
                k += 1
                continue
            # A failed breakout (a close back inside within RECLAIM_BARS) is a
            # false break: the range carries on. See ranges.ts.
            back = next((q for q in range(k + 1, min(n - 1, k + RECLAIM_BARS) + 1)
                         if bot - (atr[q] or a) * BREAK_ATR <= c[q] <= top + (atr[q] or a) * BREAK_ATR), -1)
            if back >= 0:
                k = back + 1
                continue
            breakout, i1 = d, k - 2
            break
        box.update(i1=i1, t1=t[i1], bars=i1 - i0 + 1, breakout=breakout)
        if forming and breakout:
            note(s, 'forming range broke before it was confirmed', **box)
            continue
        if i1 - i0 + 1 < (FORMING_MIN_BARS if forming else MIN_BARS):
            note(s, f'only {i1 - i0 + 1} bars', **box)
            continue
        t2 = max(a * EDGE_ATR, hh * EDGE_H)
        es = abs(_edge_slope(sw, i0, i1, top, bot, t2)) * SLOPE_BARS / a
        if es > MAX_SLOPE_ATR:
            note(s, f'edge slope {es:.2f} ATR/{SLOPE_BARS} bars > {MAX_SLOPE_ATR}', **box)
            continue

        strong = False
        if breakout:
            lim = min(n - 1, i1 + STRONG_BARS)
            best = 0.0
            for k in range(i1 + 1, lim + 1):
                best = max(best, c[k] - top if breakout == 'up' else bot - c[k])
            strong = best >= min(hh * STRONG_MOVE * 2, max(hh * STRONG_MOVE, a * STRONG_ATR))

        tt = tb = 0
        for (i, p, hi) in sw:
            if i < i0 or i > i1:
                continue
            if hi and abs(p - top) <= t2:
                tt += 1
            if not hi and abs(p - bot) <= t2:
                tb += 1

        cands.append(RangeBox(i0, i1, t[i0], t[i1], top, bot, 'minor',
                              breakout is None, breakout, strong, tt, tb))
        note(s, 'candidate', **box)

    # --- merge / nest ------------------------------------------------------ #
    def score(r):
        return (r.touchesTop + r.touchesBot) * 10 + (r.i1 - r.i0)
    cands.sort(key=lambda r: -score(r))          # stable, like Array.sort
    kept: list = []
    for r in cands:
        absorbed = False
        for k in kept:
            ov = min(k.i1, r.i1) - max(k.i0, r.i0)
            if ov <= 0:
                continue
            hk, hr = k.top - k.bot, r.top - r.bot
            avg = (hk + hr) / 2
            similar = abs(k.top - r.top) <= avg * SIMILAR and abs(k.bot - r.bot) <= avg * SIMILAR
            why = None
            if similar:
                if r.i0 < k.i0:
                    k.i0, k.t0 = r.i0, r.t0
                absorbed, why = True, 'merged: same edges'
                break
            nested = hr <= hk * NESTED and r.top <= k.top + hk * 0.1 and r.bot >= k.bot - hk * 0.1
            contains = hk <= hr * NESTED and k.top <= r.top + hr * 0.1 and k.bot >= r.bot - hr * 0.1
            same = ov >= 0.8 * max(k.i1 - k.i0, r.i1 - r.i0)
            if (nested or contains) and not same:
                continue
            if same:
                absorbed, why = True, 'merged: same bars'
                break
            share = ov / min(k.i1 - k.i0, r.i1 - r.i0)
            if share > 0.3:
                absorbed, why = True, f'overlaps a better range by {share * 100:.0f}%'
                break
            if r.i0 >= k.i0:
                r.i0 = k.i1 + 1
            else:
                r.i1 = k.i0 - 1
            if r.i1 - r.i0 + 1 < MIN_BARS:
                absorbed, why = True, 'trimmed below the minimum by a neighbour'
                break
            r.t0, r.t1 = t[r.i0], t[r.i1]
        if not absorbed:
            kept.append(r)
        elif trace is not None:
            trace.append({'i0': r.i0, 't0': r.t0, 'why': why, 'bot': r.bot, 'top': r.top,
                          'i1': r.i1, 't1': r.t1, 'into': (k.t0, k.t1, k.bot, k.top)})

    # --- tiers and keep rules ---------------------------------------------- #
    def inside(r, m):
        hm = m.top - m.bot
        return (m is not r and r.i0 >= m.i0 and r.i1 <= m.i1 and (r.top - r.bot) <= hm * NESTED
                and r.top <= m.top + hm * 0.1 and r.bot >= m.bot - hm * 0.1)

    for r in kept:
        ln = r.i1 - r.i0 + 1
        touches = r.touchesTop + r.touchesBot
        nested_in = any(inside(r, m) for m in kept)
        major = (not nested_in) and ((ln >= MAJOR_BARS and touches >= MAJOR_TOUCHES)
                                     or (r.strong and ln >= MIN_BARS * 2))
        r.tier = 'major' if major else ('developing' if r.active else 'minor')
    # Every range that passed is kept; `strong` only labels the breakout.
    out = list(kept)
    out.sort(key=lambda r: r.i0)
    return out


def active_range(t, h, l, c, lookback: int = 600):
    """The range price is in NOW (no confirmed breakout), or None. Major beats
    developing; among equals, the tighter (innermost) box wins - it is the
    structure price is actually trading."""
    if lookback and len(c) > lookback:
        t, h, l, c = t[-lookback:], h[-lookback:], l[-lookback:], c[-lookback:]
    live = [r for r in find_ranges(t, h, l, c) if r.active]
    if not live:
        return None
    rank = {'major': 0, 'minor': 1, 'developing': 2}
    live.sort(key=lambda r: (rank[r.tier], r.top - r.bot))
    return live[0]


# --------------------------------------------------------------------------- #
# range TYPES (2026-11-08) - for range_edge_fade                              #
# --------------------------------------------------------------------------- #
# find_ranges() only finds FLAT boxes (slope <= MAX_SLOPE_ATR). A trader also
# fades the edges of ranges that lean or that are narrowing / widening. This
# fits a top line through the swing highs and a bottom line through the swing
# lows of the latest run of swings price has stayed between, and names it:
#
#   horizontal   both lines flat (|slope| <= TYPE_FLAT_ATR per SLOPE_BARS)
#   ascending    both rising, roughly parallel
#   descending   both falling, roughly parallel
#   contracting  the gap narrows by >= TYPE_WIDTH_CHANGE (top falling and/or
#                bottom rising faster) - triangles and wedges
#   expanding    the gap widens by >= TYPE_WIDTH_CHANGE - broadening ranges
#
# Point in time like the rest of this file: swings need PIVOT_K bars after
# them, so a run is only known PIVOT_K bars after its last swing.
TYPE_FLAT_ATR = 0.15            # same as MAX_SLOPE_ATR: flat is flat
TYPE_WIDTH_CHANGE = 0.25        # the gap changing by a quarter = contracting / expanding
TYPE_MIN_SWINGS = 5             # two highs and two lows, and a third test of one side
TYPE_MAX_SLOPE_ATR = 1.5        # steeper than this is a trend leg, not a range
TYPE_FIT_ATR = 0.6              # every swing within max(this x ATR, TYPE_FIT_W x width) of its line
TYPE_FIT_W = 0.35
TYPE_MAX_SWINGS = 10
TYPE_MIN_W_ATR = 1.5            # narrower than this is noise, not a range to fade
TYPE_MAX_W_ATR = 8.0
TYPE_CONTAIN_ATR = 0.3          # closes may sit this far outside a line


def _line(pts):
    """Least-squares line through [(i, p)], as (slope, intercept)."""
    m = len(pts)
    xm = sum(x for x, _ in pts) / m
    ym = sum(y for _, y in pts) / m
    den = sum((x - xm) ** 2 for x, _ in pts)
    slope = sum((x - xm) * (y - ym) for x, y in pts) / den if den else 0.0
    return slope, ym - slope * xm


def range_structure(t, h, l, c, lookback: int = 600):
    """The range structure price is trading in NOW, of any of the five types,
    or None. Prefers the LONGEST run of swings that still contains price.

    Returns a dict: type, top/bottom line values at the last bar and at the
    run's start, slopes (x ATR per SLOPE_BARS), width now (price and ATR),
    where price sits between the lines (0 = bottom, 1 = top), start bar and
    swing count. Indices are relative to the bars passed in."""
    if lookback and len(c) > lookback:
        t, h, l, c = t[-lookback:], h[-lookback:], l[-lookback:], c[-lookback:]
    h = [float(x) for x in h]
    l = [float(x) for x in l]
    c = [float(x) for x in c]
    n = len(c)
    if n < MIN_BARS + 20:
        return None
    # A flat box the chart draws IS the horizontal range - same edges as the
    # trader sees, not a re-fit of them.
    box = active_range(t, h, l, c, lookback=0)
    if box is not None:
        a_box = atr_series(h, l, c)[n - 1] or 1.0
        w = box.top - box.bot
        return {
            'type': 'horizontal', 'top': box.top, 'bot': box.bot,
            'top_start': box.top, 'bot_start': box.bot,
            'slope_top_atr': 0.0, 'slope_bot_atr': 0.0,
            'width': w, 'width_atr': round(w / a_box, 2), 'width_change': 0.0,
            'position': round((c[-1] - box.bot) / w, 3) if w > 0 else 0.5,
            'i0': box.i0, 't0': box.t0, 'swings': box.touchesTop + box.touchesBot,
            'tier': box.tier, 'source': 'box',
        }
    atr = atr_series(h, l, c)
    sw = swings(h, l, atr)
    a = atr[n - 1]
    if len(sw) < TYPE_MIN_SWINGS or not a > 0:
        return None
    last = n - 1
    best = None
    for k in range(TYPE_MIN_SWINGS, min(TYPE_MAX_SWINGS, len(sw)) + 1):
        run = sw[-k:]
        hs = [(i, p) for i, p, hi in run if hi]
        ls = [(i, p) for i, p, hi in run if not hi]
        if len(hs) < 2 or len(ls) < 2:
            continue
        sh, ih = _line(hs)
        sl, il = _line(ls)
        i0 = run[0][0]
        top0, bot0 = sh * i0 + ih, sl * i0 + il
        top1, bot1 = sh * last + ih, sl * last + il
        w0, w1 = top0 - bot0, top1 - bot1
        if w1 < a * TYPE_MIN_W_ATR or w1 > a * TYPE_MAX_W_ATR or w0 < a * TYPE_MIN_W_ATR:
            continue
        if last - i0 + 1 < MIN_BARS:
            continue
        if max(abs(sh), abs(sl)) * SLOPE_BARS > a * TYPE_MAX_SLOPE_ATR:
            continue
        # The lines must FIT: a line through scattered swings is not an edge.
        fit = max(a * TYPE_FIT_ATR, min(w0, w1) * TYPE_FIT_W)
        if any(abs(p - (sh * i + ih)) > fit for i, p in hs) or                 any(abs(p - (sl * i + il)) > fit for i, p in ls):
            continue
        # Every close since the run began stays between the lines.
        tol = a * TYPE_CONTAIN_ATR
        if any(c[j] > sh * j + ih + tol or c[j] < sl * j + il - tol for j in range(i0, n)):
            continue
        best = (k, sh, ih, sl, il, i0, top0, bot0, top1, bot1, w0, w1)
    if best is None:
        return None
    k, sh, ih, sl, il, i0, top0, bot0, top1, bot1, w0, w1 = best
    s_top = sh * SLOPE_BARS / a
    s_bot = sl * SLOPE_BARS / a
    change = (w1 - w0) / w0
    if change <= -TYPE_WIDTH_CHANGE:
        kind = 'contracting'
    elif change >= TYPE_WIDTH_CHANGE:
        kind = 'expanding'
    elif abs(s_top) <= TYPE_FLAT_ATR and abs(s_bot) <= TYPE_FLAT_ATR:
        kind = 'horizontal'
    elif s_top > 0 and s_bot > 0:
        kind = 'ascending'
    elif s_top < 0 and s_bot < 0:
        kind = 'descending'
    else:
        kind = 'horizontal' if abs(s_top + s_bot) / 2 <= TYPE_FLAT_ATR else (
            'ascending' if s_top + s_bot > 0 else 'descending')
    pos = (c[-1] - bot1) / w1 if w1 > 0 else 0.5
    return {
        'type': kind, 'top': top1, 'bot': bot1, 'top_start': top0, 'bot_start': bot0,
        'slope_top_atr': round(s_top, 3), 'slope_bot_atr': round(s_bot, 3),
        'width': w1, 'width_atr': round(w1 / a, 2), 'width_change': round(change, 3),
        'position': round(pos, 3), 'i0': i0, 't0': int(t[i0]), 'swings': k,
        'tier': None, 'source': 'lines',
    }


def failed_break(t, h, l, c, level: float, lookback: int = 12, within: int = 1):
    """A FALSE BREAK of `level` that has just completed, by the chart's own
    breakout rule: two closes in a row more than BREAK_ATR x ATR beyond the
    level, then a close back inside (within the same buffer) no more than
    RECLAIM_BARS bars later. Returns None, or a dict for the most recent one
    whose reclaim close is among the last `within` bars:
        side       'above' (it broke up and failed) or 'below'
        extreme    the furthest high / low of the excursion
        break_i, back_i, bars_since (of the reclaim)"""
    h = [float(x) for x in h]
    l = [float(x) for x in l]
    c = [float(x) for x in c]
    n = len(c)
    if n < 20:
        return None
    atr = atr_series(h, l, c)
    lo = max(1, n - lookback)
    for back in range(n - 1, max(lo + 1, n - within) - 1, -1):
        for side in ('above', 'below'):
            out_ = (lambda j: c[j] - level > atr[j] * BREAK_ATR) if side == 'above' \
                else (lambda j: level - c[j] > atr[j] * BREAK_ATR)
            if out_(back) or not out_(back - 1):
                continue                      # `back` must be the first close back
            # Walk back over the closes outside: the break needs two in a row,
            # and the reclaim must come within RECLAIM_BARS of the second.
            j = back - 1
            while j - 1 >= lo and out_(j - 1):
                j -= 1
            first = j
            if back - 1 - first + 1 < 2:
                continue
            second = first + 1
            if back - second > RECLAIM_BARS:
                continue
            ext = max(h[first:back + 1]) if side == 'above' else min(l[first:back + 1])
            return {'side': side, 'level': level, 'extreme': ext, 'break_i': second,
                    'back_i': back, 'bars_since': n - 1 - back,
                    'outside_bars': back - first}
    return None
