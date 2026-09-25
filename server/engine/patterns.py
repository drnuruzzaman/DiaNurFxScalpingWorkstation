"""
server/engine/patterns.py - classical chart patterns, detected off the swing
sequence rather than off raw bars.

Why swings and not bars: a head-and-shoulders is a statement about five turning
points. Trying to find it with a sliding window over closes finds it everywhere.
Working from the ATR-filtered swing list means a pattern can only form out of
moves the market actually made.

Every pattern returns the same contract, because the UI and the signal engine
both consume them generically:

    kind        machine name, e.g. 'inverse_head_shoulders'
    label       human name for the badge
    direction   'bullish' | 'bearish'
    status      'forming' | 'confirmed' | 'failed'
    quality     0..100, how textbook it is
    points      the polyline the chart draws
    break_level the price that confirms it
    target      the measured move
    invalidation where the idea is wrong
    rr          target vs invalidation from the break level
    notes       the specific measurements, for the narrator to quote

QUALITY IS NOT CONFIDENCE. Quality says "this is a clean example of the shape".
Whether to trade it is decided later, in signals.py and qualify.py, where
context, session and risk get a vote.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np


@dataclass
class Pattern:
    kind: str
    label: str
    direction: str
    status: str
    quality: int
    points: list = field(default_factory=list)     # [{t, price, role}]
    break_level: float = 0.0
    target: float = 0.0
    invalidation: float = 0.0
    rr: float = 0.0
    start_t: int = 0
    end_t: int = 0
    start_idx: int = 0
    end_idx: int = 0
    notes: list = field(default_factory=list)
    zone: dict = field(default_factory=dict)       # shaded box for the chart
    # Filled in by detect_patterns once it knows where price is now.
    distance_to_break_atr: float = 0.0
    age_bars: int = 0
    actionable: bool = False
    relevance: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _q(value: float) -> int:
    return int(max(0, min(100, round(value))))


def _rr(break_level: float, target: float, invalidation: float) -> float:
    risk = abs(break_level - invalidation)
    reward = abs(target - break_level)
    return round(reward / risk, 2) if risk > 1e-9 else 0.0


def _status(broke: bool, failed: bool) -> str:
    if failed:
        return 'failed'
    return 'confirmed' if broke else 'forming'


# How long a completed pattern has to trigger before it stops being a plan and
# becomes a history lesson. Scanning "did price EVER break this" over a 600-bar
# window marked 89% of patterns confirmed, including ones whose right shoulder
# was 300 bars back - technically true, useless to trade.
CONFIRM_WINDOW = 30

# A pattern judged by that window whose window has CLOSED without a break is
# dropped, not left as "forming". Before, the status simply never timed out:
# live 15m gold showed a Head & Shoulders "forming" 81 bars after its right
# shoulder, its steep neckline projected ever lower (break 4 ATR under price),
# and pattern_break could still arm a stop order on it. Worse, a neckline
# broken on bar 31 was reported "forming" forever, because the scan stops at
# bar 30. Applies to the two detectors that use the window - head and
# shoulders, double/triple tops and bottoms; the others test "ever broken".
# OFF: tested 2024-2026 (tools/exp_patterns.py, order_ledger/
# exp_patterns_report.txt) and it cost money in every year - pattern_break
# E[R] 2026 +0.016 -> +0.001, 2025 -0.008 -> -0.027, 2024 -0.015 -> -0.025.
# The stale "forming" patterns' extended necklines were acting as breakout
# triggers that paid. Kept switchable for a display-only use.
EXPIRE_UNBROKEN = False

# The head of a head and shoulders must clear BOTH shoulders by this much.
# Any margin used to do: a "head" 0.10 above its left shoulder (0.01 ATR)
# passed, which is a double top - already reported as one - plus a lower
# bounce read as a right shoulder, counted a second time under another name.
# 0.0 = the old rule (any margin). 0.5 tested worse alongside the expiry.
HEAD_CLEAR_ATR = 0.0

# How much two same-kind patterns must share before one is a duplicate.
#
# A third of the shorter span. Low enough to catch the same shape found at two
# window sizes, which typically share far more than that, and high enough that
# two formations merely touching at the edges - the tail of one being the head
# of the next - are still reported as the two setups they are.
DUP_OVERLAP = 0.34


def _overlap(a, b) -> float:
    """How much of the SHORTER pattern the two share, 0..1."""
    lo = max(a.start_idx, b.start_idx)
    hi = min(a.end_idx, b.end_idx)
    shared = max(0, hi - lo)
    shortest = min(a.end_idx - a.start_idx, b.end_idx - b.start_idx)
    return shared / shortest if shortest > 0 else 0.0


def _expired(n_bars: int, start: int, broke: bool) -> bool:
    """The confirmation window after `start` is over, and nothing broke."""
    return EXPIRE_UNBROKEN and not broke and n_bars - start >= CONFIRM_WINDOW


def _broke_within(series: np.ndarray, start: int, level, above: bool,
                  window: int = CONFIRM_WINDOW) -> bool:
    """Did price close through `level` within `window` bars of `start`?"""
    seg = series[start:start + window]
    if seg.size == 0:
        return False
    lvl = level[start:start + window] if isinstance(level, np.ndarray) else level
    return bool((seg > lvl).any() if above else (seg < lvl).any())


def _project(anchor_price: float, slope: float, anchor_idx: int,
             target_idx: int, max_extrapolate: int = 40) -> float:
    """
    A sloped boundary evaluated at `target_idx`, but never extrapolated more
    than `max_extrapolate` bars past its anchor.

    Running a neckline 300 bars forward produced break levels nowhere near the
    pattern and RR values in the hundreds. A line is only meaningful near the
    structure that defined it.
    """
    capped = min(target_idx, anchor_idx + max_extrapolate)
    return anchor_price + slope * (capped - anchor_idx)


# --------------------------------------------------------------------------- #
# head and shoulders                                                          #
# --------------------------------------------------------------------------- #
def head_and_shoulders(swings, h, l, c, t, atr_val) -> list:
    """
    Five alternating swings: shoulder, head, shoulder, with a neckline through
    the two intervening turns.

    Symmetry is scored, not required. Real H&S rarely has matching shoulders,
    and demanding it finds nothing; a lopsided one is simply worth less.
    """
    out = []
    if len(swings) < 5 or atr_val <= 0:
        return out

    for i in range(len(swings) - 4):
        p = swings[i:i + 5]
        kinds = [s.kind for s in p]

        # Two valid arrangements:
        #   bearish  H&S : ls(high) t1(low) head(high) t2(low) rs(high)
        #   bullish iH&S : ls(low)  t1(high) head(low) t2(high) rs(low)
        clear = HEAD_CLEAR_ATR * atr_val
        if kinds == ['high', 'low', 'high', 'low', 'high']:
            ls, t1, head, t2, rs = p
            direction = 'bearish'
            if not head.price - max(ls.price, rs.price) > clear:
                continue
        elif kinds == ['low', 'high', 'low', 'high', 'low']:
            ls, t1, head, t2, rs = p
            direction = 'bullish'
            if not min(ls.price, rs.price) - head.price > clear:
                continue
        else:
            continue

        # --- measurements ------------------------------------------------- #
        shoulder_gap = abs(ls.price - rs.price)
        shoulder_avg = (abs(ls.price) + abs(rs.price)) / 2 or 1.0
        symmetry = max(0.0, 1.0 - shoulder_gap / max(atr_val * 2.5, 1e-9))
        head_depth = abs(head.price - (t1.price + t2.price) / 2) / atr_val
        if head_depth < 1.2:
            continue                    # head barely clears the troughs

        # time symmetry: the two halves should take comparable time
        left_bars = head.idx - ls.idx
        right_bars = rs.idx - head.idx
        if left_bars <= 0 or right_bars <= 0:
            continue
        time_sym = min(left_bars, right_bars) / max(left_bars, right_bars)

        # --- neckline ------------------------------------------------------ #
        nslope = (t2.price - t1.price) / max(t2.idx - t1.idx, 1)
        last_x = c.size - 1
        neck_now = _project(t1.price, nslope, t1.idx, last_x)
        neck_at_rs = t1.price + nslope * (rs.idx - t1.idx)

        # A pattern whose trigger sits on top of its own invalidation is not a
        # trade at any quality score - it has nowhere to put a stop.
        if abs(neck_now - head.price) < atr_val * 0.6:
            continue

        height = abs(head.price - neck_at_rs)
        neck_line = t1.price + nslope * (np.arange(c.size) - t1.idx)
        if direction == 'bearish':
            target = neck_now - height
            invalidation = float(head.price)
            broke = _broke_within(c, rs.idx, neck_line, above=False)
            failed = bool(h[rs.idx:].size and h[rs.idx:].max() > head.price)
        else:
            target = neck_now + height
            invalidation = float(head.price)
            broke = _broke_within(c, rs.idx, neck_line, above=True)
            failed = bool(l[rs.idx:].size and l[rs.idx:].min() < head.price)
        if _expired(c.size, rs.idx, broke):
            continue

        quality = _q(
            symmetry * 34 +
            min(head_depth / 4.0, 1.0) * 26 +
            time_sym * 22 +
            min(abs(nslope) / max(atr_val * 0.2, 1e-9), 1.0) * -8 +   # flat neck is better
            18
        )

        out.append(Pattern(
            kind='inverse_head_shoulders' if direction == 'bullish' else 'head_shoulders',
            label='Inverse Head & Shoulders' if direction == 'bullish' else 'Head & Shoulders',
            direction=direction,
            status=_status(broke, failed),
            quality=quality,
            points=[
                {'t': int(ls.t), 'price': float(ls.price), 'role': 'left shoulder'},
                {'t': int(t1.t), 'price': float(t1.price), 'role': 'neckline'},
                {'t': int(head.t), 'price': float(head.price), 'role': 'head'},
                {'t': int(t2.t), 'price': float(t2.price), 'role': 'neckline'},
                {'t': int(rs.t), 'price': float(rs.price), 'role': 'right shoulder'},
            ],
            break_level=round(float(neck_now), 3),
            target=round(float(target), 3),
            invalidation=invalidation,
            rr=_rr(neck_now, target, invalidation),
            start_t=int(ls.t), end_t=int(rs.t),
            start_idx=int(ls.idx), end_idx=int(rs.idx),
            notes=[
                f'shoulder symmetry {symmetry * 100:.0f}%',
                f'head depth {head_depth:.1f} ATR',
                f'time symmetry {time_sym * 100:.0f}%',
                f'neckline slope {nslope / atr_val:+.3f} ATR/bar',
            ],
        ))
    return out


# --------------------------------------------------------------------------- #
# double / triple tops and bottoms                                            #
# --------------------------------------------------------------------------- #
def double_tops_bottoms(swings, h, l, c, t, atr_val) -> list:
    out = []
    if len(swings) < 3 or atr_val <= 0:
        return out
    tol = atr_val * 0.9

    for i in range(len(swings) - 2):
        a, mid, b = swings[i], swings[i + 1], swings[i + 2]
        if a.kind != b.kind or mid.kind == a.kind:
            continue
        gap = abs(a.price - b.price)
        if gap > tol:
            continue
        depth = abs(mid.price - (a.price + b.price) / 2)
        if depth < atr_val * 1.3:
            continue                     # no meaningful trough between the peaks

        bearish = a.kind == 'high'
        # A third touch upgrades it to a triple.
        triple = False
        if i + 4 < len(swings):
            c4 = swings[i + 4]
            if c4.kind == a.kind and abs(c4.price - b.price) <= tol:
                triple = True
                b = c4

        neck = float(mid.price)
        peak = (a.price + b.price) / 2
        height = abs(peak - neck)
        if bearish:
            target = neck - height
            invalidation = float(max(a.price, b.price) + atr_val * 0.25)
            broke = _broke_within(c, b.idx, neck, above=False)
            failed = bool(c[b.idx:].size and (c[b.idx:] > invalidation).any())
        else:
            target = neck + height
            invalidation = float(min(a.price, b.price) - atr_val * 0.25)
            broke = _broke_within(c, b.idx, neck, above=True)
            failed = bool(c[b.idx:].size and (c[b.idx:] < invalidation).any())
        if _expired(c.size, b.idx, broke):
            continue

        match = max(0.0, 1.0 - gap / tol)
        quality = _q(match * 42 + min(depth / (atr_val * 4), 1.0) * 30 +
                     (12 if triple else 0) + 22)

        kind = ('triple_top' if triple else 'double_top') if bearish else \
               ('triple_bottom' if triple else 'double_bottom')
        label = kind.replace('_', ' ').title()

        pts = [{'t': int(a.t), 'price': float(a.price), 'role': 'peak 1'},
               {'t': int(mid.t), 'price': float(mid.price), 'role': 'neckline'},
               {'t': int(b.t), 'price': float(b.price), 'role': 'peak 2'}]

        out.append(Pattern(
            kind=kind, label=label,
            direction='bearish' if bearish else 'bullish',
            status=_status(broke, failed), quality=quality, points=pts,
            break_level=round(neck, 3), target=round(float(target), 3),
            invalidation=round(invalidation, 3),
            rr=_rr(neck, target, invalidation),
            start_t=int(a.t), end_t=int(b.t),
            start_idx=int(a.idx), end_idx=int(b.idx),
            notes=[
                f'peaks within {gap / atr_val:.2f} ATR',
                f'depth {depth / atr_val:.1f} ATR',
                f'measured move {height:.2f}',
            ],
            zone={'price_top': float(max(a.price, b.price)),
                  'price_bottom': float(min(a.price, b.price)),
                  't1': int(a.t), 't2': int(b.t)},
        ))
    return out


# --------------------------------------------------------------------------- #
# triangles and wedges                                                        #
# --------------------------------------------------------------------------- #
def triangles_wedges(swings, h, l, c, t, atr_val) -> list:
    """
    Two converging (or same-direction) boundary lines over the last four to six
    swings. Ascending / descending / symmetrical triangles and rising / falling
    wedges all fall out of the slope pair.
    """
    out = []
    if len(swings) < 5 or atr_val <= 0:
        return out
    last_x = c.size - 1

    for window in (6, 5):
        if len(swings) < window:
            continue
        seg = swings[-window:]
        highs = [s for s in seg if s.kind == 'high']
        lows = [s for s in seg if s.kind == 'low']
        if len(highs) < 2 or len(lows) < 2:
            continue

        hs = (highs[-1].price - highs[0].price) / max(highs[-1].idx - highs[0].idx, 1)
        ls = (lows[-1].price - lows[0].price) / max(lows[-1].idx - lows[0].idx, 1)
        hs_n, ls_n = hs / atr_val, ls / atr_val          # ATR per bar

        start_idx = min(seg[0].idx, highs[0].idx, lows[0].idx)
        width_start = ((highs[0].price + highs[-1].price) / 2 -
                       (lows[0].price + lows[-1].price) / 2)
        upper_now = highs[-1].price + hs * (last_x - highs[-1].idx)
        lower_now = lows[-1].price + ls * (last_x - lows[-1].idx)
        width_now = upper_now - lower_now
        if width_now <= atr_val * 0.4:
            continue
        converging = width_now < width_start * 0.85

        flat = 0.02
        kind = label = None
        direction = 'neutral'
        if converging and abs(hs_n) < flat and ls_n > flat:
            kind, label, direction = 'ascending_triangle', 'Ascending Triangle', 'bullish'
        elif converging and abs(ls_n) < flat and hs_n < -flat:
            kind, label, direction = 'descending_triangle', 'Descending Triangle', 'bearish'
        elif converging and hs_n < -flat and ls_n > flat:
            kind, label, direction = 'symmetrical_triangle', 'Symmetrical Triangle', 'neutral'
        elif converging and hs_n > flat and ls_n > flat and ls_n > hs_n:
            kind, label, direction = 'rising_wedge', 'Rising Wedge', 'bearish'
        elif converging and hs_n < -flat and ls_n < -flat and hs_n > ls_n:
            kind, label, direction = 'falling_wedge', 'Falling Wedge', 'bullish'
        if kind is None:
            continue

        apex_gap = width_now / max(width_start, 1e-9)
        height = abs(width_start)
        if direction == 'bullish':
            break_level, target = upper_now, upper_now + height
            invalidation = lower_now
            broke = bool((c[seg[-1].idx:] > upper_now).any())
            failed = bool((c[seg[-1].idx:] < lower_now - atr_val * 0.3).any())
        elif direction == 'bearish':
            break_level, target = lower_now, lower_now - height
            invalidation = upper_now
            broke = bool((c[seg[-1].idx:] < lower_now).any())
            failed = bool((c[seg[-1].idx:] > upper_now + atr_val * 0.3).any())
        else:
            # Symmetrical: no directional bias, break decides. Report the nearer
            # boundary as the trigger.
            px = float(c[-1])
            up_closer = abs(upper_now - px) <= abs(px - lower_now)
            break_level = upper_now if up_closer else lower_now
            target = break_level + (height if up_closer else -height)
            invalidation = lower_now if up_closer else upper_now
            broke = bool((c[seg[-1].idx:] > upper_now).any() or
                         (c[seg[-1].idx:] < lower_now).any())
            failed = False

        quality = _q((1.0 - apex_gap) * 40 + min(len(seg) / 6.0, 1.0) * 20 +
                     min(height / (atr_val * 5), 1.0) * 20 + 20)

        out.append(Pattern(
            kind=kind, label=label, direction=direction,
            status=_status(broke, failed), quality=quality,
            points=[{'t': int(s.t), 'price': float(s.price), 'role': s.kind}
                    for s in seg],
            break_level=round(float(break_level), 3),
            target=round(float(target), 3),
            invalidation=round(float(invalidation), 3),
            rr=_rr(break_level, target, invalidation),
            start_t=int(seg[0].t), end_t=int(seg[-1].t),
            start_idx=int(start_idx), end_idx=int(seg[-1].idx),
            notes=[
                f'upper slope {hs_n:+.3f} ATR/bar',
                f'lower slope {ls_n:+.3f} ATR/bar',
                f'compressed to {apex_gap * 100:.0f}% of starting width',
            ],
            zone={'upper_now': float(upper_now), 'lower_now': float(lower_now),
                  't1': int(seg[0].t), 't2': int(t[last_x])},
        ))
        if out:
            break            # one triangle read is enough; prefer the wider window
    return out


# --------------------------------------------------------------------------- #
# flags and pennants                                                          #
# --------------------------------------------------------------------------- #
def flags(swings, h, l, c, t, atr_val) -> list:
    """
    A sharp impulse (the pole) followed by a shallow counter-trend drift.

    The test that matters is PROPORTION: a flag whose consolidation retraces
    more than about 60% of the pole is not a flag, it is a reversal in progress.
    """
    out = []
    if len(swings) < 4 or atr_val <= 0 or c.size < 20:
        return out

    for i in range(max(0, len(swings) - 8), len(swings) - 2):
        a, b = swings[i], swings[i + 1]
        pole = b.price - a.price
        pole_bars = b.idx - a.idx
        if pole_bars < 2 or abs(pole) < atr_val * 3.0:
            continue
        # The pole must be steep: a slow grind is a trend, not a pole.
        if abs(pole) / pole_bars < atr_val * 0.35:
            continue

        rest = swings[i + 2:]
        if len(rest) < 2:
            continue
        seg_prices = [s.price for s in rest]
        flag_hi, flag_lo = max(seg_prices), min(seg_prices)
        flag_depth = flag_hi - flag_lo
        if flag_depth > abs(pole) * 0.62:
            continue
        retr = (b.price - flag_lo) / pole if pole > 0 else (flag_hi - b.price) / abs(pole)
        if retr > 0.62 or retr < 0.05:
            continue

        bullish = pole > 0
        last_x = c.size - 1
        if bullish:
            break_level = flag_hi
            target = break_level + abs(pole)
            invalidation = flag_lo
            broke = bool((c[rest[0].idx:] > flag_hi).any())
            failed = bool((c[rest[0].idx:] < flag_lo - atr_val * 0.3).any())
        else:
            break_level = flag_lo
            target = break_level - abs(pole)
            invalidation = flag_hi
            broke = bool((c[rest[0].idx:] < flag_lo).any())
            failed = bool((c[rest[0].idx:] > flag_hi + atr_val * 0.3).any())

        pennant = flag_depth < abs(pole) * 0.3 and len(rest) >= 4
        quality = _q((1 - retr / 0.62) * 34 +
                     min(abs(pole) / (atr_val * 8), 1.0) * 30 +
                     min(len(rest) / 5.0, 1.0) * 16 + 20)

        out.append(Pattern(
            kind='bull_pennant' if (pennant and bullish) else
                 'bear_pennant' if pennant else
                 'bull_flag' if bullish else 'bear_flag',
            label=('Bull Pennant' if (pennant and bullish) else
                   'Bear Pennant' if pennant else
                   'Bull Flag' if bullish else 'Bear Flag'),
            direction='bullish' if bullish else 'bearish',
            status=_status(broke, failed), quality=quality,
            points=[{'t': int(a.t), 'price': float(a.price), 'role': 'pole start'},
                    {'t': int(b.t), 'price': float(b.price), 'role': 'pole end'}] +
                   [{'t': int(s.t), 'price': float(s.price), 'role': 'flag'} for s in rest],
            break_level=round(float(break_level), 3),
            target=round(float(target), 3),
            invalidation=round(float(invalidation), 3),
            rr=_rr(break_level, target, invalidation),
            start_t=int(a.t), end_t=int(rest[-1].t),
            start_idx=int(a.idx), end_idx=int(rest[-1].idx),
            notes=[
                f'pole {abs(pole) / atr_val:.1f} ATR in {pole_bars} bars',
                f'flag retraces {retr * 100:.0f}% of pole',
                f'flag depth {flag_depth / atr_val:.1f} ATR',
            ],
            zone={'price_top': float(flag_hi), 'price_bottom': float(flag_lo),
                  't1': int(rest[0].t), 't2': int(rest[-1].t)},
        ))
    return out


# --------------------------------------------------------------------------- #
# rectangles / ranges                                                         #
# --------------------------------------------------------------------------- #
def rectangles(swings, h, l, c, t, atr_val) -> list:
    out = []
    if len(swings) < 4 or atr_val <= 0:
        return out
    seg = swings[-6:]
    highs = [s.price for s in seg if s.kind == 'high']
    lows = [s.price for s in seg if s.kind == 'low']
    if len(highs) < 2 or len(lows) < 2:
        return out

    top, bot = float(np.mean(highs)), float(np.mean(lows))
    height = top - bot
    if height < atr_val * 1.5:
        return out
    spread_h = (max(highs) - min(highs)) / atr_val
    spread_l = (max(lows) - min(lows)) / atr_val
    if spread_h > 1.2 or spread_l > 1.2:
        return out                     # edges too ragged to be a rectangle

    px = float(c[-1])
    up_closer = abs(top - px) <= abs(px - bot)
    break_level = top if up_closer else bot
    target = break_level + (height if up_closer else -height)
    invalidation = bot if up_closer else top
    broke = bool((c[seg[-1].idx:] > top).any() or (c[seg[-1].idx:] < bot).any())

    quality = _q((2.4 - spread_h - spread_l) / 2.4 * 45 +
                 min(len(seg) / 6.0, 1.0) * 25 + 25)
    out.append(Pattern(
        kind='rectangle', label='Rectangle / Range', direction='neutral',
        status=_status(broke, False), quality=quality,
        points=[{'t': int(s.t), 'price': float(s.price), 'role': s.kind} for s in seg],
        break_level=round(break_level, 3), target=round(float(target), 3),
        invalidation=round(invalidation, 3),
        rr=_rr(break_level, target, invalidation),
        start_t=int(seg[0].t), end_t=int(seg[-1].t),
        start_idx=int(seg[0].idx), end_idx=int(seg[-1].idx),
        notes=[f'height {height / atr_val:.1f} ATR',
               f'top edge scatter {spread_h:.2f} ATR',
               f'bottom edge scatter {spread_l:.2f} ATR'],
        zone={'price_top': top, 'price_bottom': bot,
              't1': int(seg[0].t), 't2': int(t[-1])},
    ))
    return out


# --------------------------------------------------------------------------- #
# orchestration                                                               #
# --------------------------------------------------------------------------- #
def detect_patterns(swings, h, l, c, t, atr_val, min_quality: int = 48,
                    max_patterns: int = 8, max_age_bars: int = 150) -> list:
    """
    Every detector, filtered down to what is still actionable.

    Three filters beyond quality, all learned from scanning a year of M5:
      * AGE       a pattern that completed 300 bars ago is not a plan.
      * RISK      break and invalidation must be far enough apart to place a
                  stop between them.
      * SANITY    RR is capped; anything claiming 50R is a measurement bug, not
                  an opportunity, and it must not be allowed to outrank a real
                  2R setup.
    """
    found: list = []
    for fn in (head_and_shoulders, double_tops_bottoms, triangles_wedges,
               flags, rectangles):
        try:
            found.extend(fn(swings, h, l, c, t, atr_val))
        except Exception:                       # noqa: BLE001
            # A detector throwing must never take the whole analysis down -
            # the other patterns and the signal engine are still useful.
            continue

    last_idx = c.size - 1
    keep: list = []
    for p in found:
        if p.quality < min_quality or p.status == 'failed':
            continue
        if last_idx - p.end_idx > max_age_bars:
            continue
        if abs(p.break_level - p.invalidation) < atr_val * 0.5:
            continue
        if not (0.15 <= p.rr <= 12.0):
            continue
        keep.append(p)
    found = keep

    # Deduplicate: the same shape often gets found at two window sizes.
    #
    # On OVERLAP, not on where the two happen to end.
    #
    # The old test dropped a pattern only when another of the same kind ended
    # within four bars of it. Two scans of one shape rarely agree that closely:
    # a wider window picks up a later swing and finishes five or ten bars on,
    # so both survived. Measured on live 4h gold that put three Triple Tops in
    # a list of eight, two of them sharing 67% of their candles - one shape,
    # counted twice, crowding out the patterns that were actually different.
    #
    # Comparing the SPANS answers the real question. Two formations that share
    # most of their candles are one formation; two that share none are two,
    # however close their end bars happen to fall. That second half matters as
    # much as the first - a downtrend legitimately prints the same shape again
    # at a lower level, and merging those would hide a real setup.
    found.sort(key=lambda p: (-p.quality, -p.end_idx))
    kept: list = []
    for p in found:
        if any(k.kind == p.kind and _overlap(k, p) >= DUP_OVERLAP for k in kept):
            continue
        kept.append(p)

    # Rank by what a trader can still DO with it, not by how pretty it is.
    # A 90-quality pattern whose trigger is 6 ATR away is worth less right now
    # than a 60-quality one sitting on its break level.
    price_now = float(c[-1])
    for p in kept:
        p.distance_to_break_atr = round(
            (p.break_level - price_now) / atr_val, 2) if atr_val > 0 else 0.0
        p.age_bars = int(last_idx - p.end_idx)
        near = max(0.0, 1.0 - abs(p.distance_to_break_atr) / 4.0)
        fresh = max(0.0, 1.0 - p.age_bars / float(max_age_bars))
        p.actionable = bool(p.status == 'forming' or p.age_bars <= CONFIRM_WINDOW)
        p.relevance = _q(p.quality * 0.45 + near * 30 + fresh * 25)

    kept.sort(key=lambda p: -p.relevance)
    return kept[:max_patterns]


__all__ = ['Pattern', 'detect_patterns', 'head_and_shoulders',
           'double_tops_bottoms', 'triangles_wedges', 'flags', 'rectangles']
