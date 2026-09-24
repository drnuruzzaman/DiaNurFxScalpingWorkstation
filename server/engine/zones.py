"""
Price AREAS, as opposed to price levels.

levels.py finds horizontal lines - a price several swings have touched. This
module finds the two things a trader reads as a band rather than a line:

  FAIR VALUE GAPS   a three-bar imbalance where the market moved so fast it
                    left a range no trade happened in.
  SUPPLY / DEMAND   the base a strong move departed from - the candles sitting
                    where a large order is presumed to have been worked.

Both are drawn as rectangles, both are consumed the same way (price returns,
reacts or does not), and both go stale once price has traded back through
them. They are kept together so that "has this been eaten into yet" is
answered the same way in both cases.

Nothing here invents a new idea. What it does do is refuse to report one
without a size test against ATR, because the naive definitions fire on every
other bar: a one-tick imbalance is arithmetic, not a gap a trader would wait
for, and a pause before a two-tick move is not a zone.
"""

from __future__ import annotations

import numpy as np


# A gap or base smaller than this fraction of ATR is noise. Chosen so that on
# gold M5 a typical session yields a handful of each rather than dozens: the
# point of drawing a zone is that there are few enough to look at.
MIN_GAP_ATR = 0.25
MIN_IMPULSE_ATR = 1.5
MAX_BASE_BARS = 3

# A base is a PAUSE, and a pause is narrow. Two independent caps, because
# either one alone lets through boxes that swallow the chart:
#
#   ABSOLUTE   wider than this much ATR and it is a range, not a base. The
#              first version had no absolute cap at all, so on a 4.5-ATR
#              impulse it would happily box a 4-ATR "base" - a band three
#              times the height of a candle, drawn over the price action it
#              was supposed to annotate.
#   RELATIVE   and it must be small against the move that left it. A base
#              half the size of its own impulse is not where the move came
#              from, it is most of the move.
MAX_BASE_ATR = 0.9
MAX_BASE_FRACTION = 0.5


def _mitigation(lo: float, hi: float, low: np.ndarray, high: np.ndarray,
                start: int) -> float:
    """
    How far price has eaten into a zone since it formed, 0..1.

    0 is untouched, 1 is fully traded through. Returned as a fraction rather
    than a boolean because "tapped the edge" and "closed clean through" are
    different facts and a caller may want to draw them differently - the
    common mistake is to discard a zone the moment a wick clips it.
    """
    span = hi - lo
    if span <= 0 or start >= len(low):
        return 1.0
    seg_lo = float(np.min(low[start:]))
    seg_hi = float(np.max(high[start:]))
    if seg_lo >= hi or seg_hi <= lo:
        return 0.0
    # The deepest penetration from whichever side the zone is approached.
    inside = min(hi, seg_hi) - max(lo, seg_lo)
    return float(min(1.0, max(0.0, inside / span)))


def _touches(lo: float, hi: float, low: np.ndarray, high: np.ndarray,
             start: int) -> int:
    """
    How many separate times price has come back to a band since `start`.

    A touch is a RUN of bars intersecting the band, not a count of bars: a
    four-hour consolidation inside a zone is one test, and counting each bar
    would report it as sixteen. Price has to leave and return for the count
    to advance, which is what a person means when they say a level has been
    tested twice.
    """
    n = min(len(low), len(high))
    if start >= n:
        return 0
    count, inside = 0, False
    for k in range(start, n):
        hit = float(low[k]) <= hi and float(high[k]) >= lo
        if hit and not inside:
            count += 1
        inside = hit
    return count


def fair_value_gaps(o: np.ndarray, h: np.ndarray, l: np.ndarray,
                    c: np.ndarray, t: np.ndarray, atr: np.ndarray,
                    lookback: int = 300, limit: int = 12) -> list:
    """
    Three-bar imbalances that price has not yet filled.

    The definition is the standard one: with bars A, B, C, an UP gap exists
    when C's low is above A's high - B ran so hard that the two bars either
    side of it do not overlap, and the range between them never traded.

    Reported newest first, and only while something is left to fill. A gap
    price has closed through is history; drawing it puts a box on the chart
    that no longer says anything about what price might do next.
    """
    n = len(c)
    if n < 10:
        return []
    out = []
    start = max(2, n - lookback)
    for i in range(n - 1, start - 1, -1):
        a_hi, a_lo = float(h[i - 2]), float(l[i - 2])
        c_hi, c_lo = float(h[i]), float(l[i])
        ref = float(atr[i]) if i < len(atr) and atr[i] > 0 else 0.0
        if ref <= 0:
            continue
        if c_lo > a_hi:
            lo, hi, side = a_hi, c_lo, 'bullish'
        elif c_hi < a_lo:
            lo, hi, side = c_hi, a_lo, 'bearish'
        else:
            continue
        if (hi - lo) < ref * MIN_GAP_ATR:
            continue
        filled = _mitigation(lo, hi, l, h, i + 1)
        if filled >= 0.98:
            continue
        out.append({
            'kind': 'fvg',
            'side': side,
            'low': round(lo, 5),
            'high': round(hi, 5),
            'mid': round((lo + hi) / 2, 5),
            'idx': int(i),
            't': int(t[i]),
            # The gap in ATR: a 2-ATR imbalance and a 0.3-ATR one behave
            # nothing alike, and the number is cheaper to read than the box.
            'size_atr': round((hi - lo) / ref, 2),
            'filled': round(filled, 2),
        })
        if len(out) >= limit:
            break
    return out


def supply_demand(o: np.ndarray, h: np.ndarray, l: np.ndarray,
                  c: np.ndarray, t: np.ndarray, atr: np.ndarray,
                  lookback: int = 300, limit: int = 8) -> list:
    """
    The bases that strong moves departed from.

    A demand zone is the last DOWN candle before a run up; supply is the last
    UP candle before a run down. That candle - plus up to two before it that
    are also part of the same pause - is taken as the area, on the reading
    that whatever absorbed price there may still be working.

    Two tests keep this honest, and both matter:

      THE DEPARTURE must be worth at least MIN_IMPULSE_ATR of range. Without
      it every hesitation in a drift becomes a zone.
      THE BASE must be smaller than the departure. A "base" wider than the
      move that left it is not a base, it is just a big bar, and boxing it
      tells you nothing you could not see.
    """
    n = len(c)
    if n < 20:
        return []
    out = []
    start = max(MAX_BASE_BARS, n - lookback)
    i = n - 2
    while i > start and len(out) < limit:
        ref = float(atr[i]) if i < len(atr) and atr[i] > 0 else 0.0
        if ref <= 0:
            i -= 1
            continue

        # Measure the move AWAY from bar i, over the few bars that follow.
        lo_w, hi_w = i + 1, min(n, i + 6)
        if hi_w <= lo_w:
            i -= 1
            continue
        seg_hi = float(np.max(h[lo_w:hi_w]))
        seg_lo = float(np.min(l[lo_w:hi_w]))
        up_move = seg_hi - float(c[i])
        down_move = float(c[i]) - seg_lo

        if up_move >= ref * MIN_IMPULSE_ATR and up_move > down_move:
            side, impulse = 'demand', up_move
            base_ok = c[i] <= o[i]          # last down candle before the run
            # Where the departure ran out of steam. Mitigation is measured
            # from HERE, not from a fixed number of bars after the base.
            peak = lo_w + int(np.argmax(h[lo_w:hi_w]))
        elif down_move >= ref * MIN_IMPULSE_ATR and down_move > up_move:
            side, impulse = 'supply', down_move
            base_ok = c[i] >= o[i]          # last up candle before the drop
            peak = lo_w + int(np.argmin(l[lo_w:hi_w]))
        else:
            i -= 1
            continue
        if not base_ok:
            i -= 1
            continue

        # Widen backwards over bars that belong to the same pause: small
        # bodies, and only while the band stays within the absolute cap.
        #
        # The width test is applied BEFORE each bar is absorbed rather than
        # after the loop. Testing at the end meant one long wick three bars
        # back could double the band, and the only choices left were to keep
        # it or throw away a base that was fine until that bar.
        lo, hi = float(l[i]), float(h[i])
        j = i
        for k in range(1, MAX_BASE_BARS):
            p = i - k
            if p < 0:
                break
            if abs(float(c[p]) - float(o[p])) > ref * 0.8:
                break
            nlo, nhi = min(lo, float(l[p])), max(hi, float(h[p]))
            if (nhi - nlo) > ref * MAX_BASE_ATR:
                break
            lo, hi, j = nlo, nhi, p

        width = hi - lo
        if width <= 0 or width > ref * MAX_BASE_ATR:
            i -= 1
            continue
        if width > impulse * MAX_BASE_FRACTION:
            i -= 1
            continue

        # From the end of the impulse, NOT a fixed six bars on.
        #
        # The fixed offset skipped the whole departure window, which is right
        # for ignoring the move out of the zone - but it also skipped anything
        # that happened inside it. A base price left and then fell straight
        # back through, all within those six bars, was reported untouched
        # forever: a 1h demand zone at 4405 sat on the chart as fresh while
        # price traded 120 points below it. Starting at the impulse extreme
        # keeps the departure excluded and counts everything after it.
        filled = _mitigation(lo, hi, l, h, peak + 1)
        touches = _touches(lo, hi, l, h, peak + 1)
        if filled >= 0.98:
            i = j - 1
            continue

        # One area of price, one box.
        #
        # Bases cluster: a pause, a push, another pause a few points away, and
        # the scan reports three boxes stacked on the same band of price. They
        # are drawn translucent, so overlapping them compounds the shading
        # until the candles underneath are unreadable - and three boxes over
        # one area does not mean three times the interest. The newest wins,
        # because it is the one price left most recently.
        if any(lo < z['high'] and hi > z['low'] for z in out):
            i = j - 1
            continue

        out.append({
            'kind': 'zone',
            'side': side,
            'low': round(lo, 5),
            'high': round(hi, 5),
            'mid': round((lo + hi) / 2, 5),
            'idx': int(j),
            'to_idx': int(i),
            't': int(t[j]),
            'impulse_atr': round(impulse / ref, 2),
            # How many times price has come back to test it. The impulse is
            # kept - it is why the zone exists at all - but the count is the
            # number worth putting on the chart: it says how well known this
            # band is, which the size of one old move does not.
            'touches': touches,
            'filled': round(filled, 2),
        })
        # Skip past the base just claimed, so one pause cannot report three
        # overlapping zones.
        i = j - 1
    return out
