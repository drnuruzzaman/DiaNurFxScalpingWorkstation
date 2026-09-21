"""
server/engine/levels.py - support, resistance and the liquidity that sits at them.

A level is not a line, it is a BAND. Price does not turn at 4523.71; it turns
somewhere in the 4522-4526 area, and a system that insists on the exact number
will miss the trade and then claim the level failed. Every level here therefore
carries a width, scaled to ATR.

Levels are scored, not just found, because on a 600-bar gold chart there are
forty plausible levels and maybe five that matter. The score combines:

    touches   how many times price reacted there
    recency   a level from 400 bars ago is history, not a plan
    reaction  how hard price moved away from it, in ATR
    flip      broken support that now caps rallies is worth more than either
    round     gold respects .00 and .50 far more than a random price
    volume    a high-volume node is where positions actually live

Nothing here looks forward: a level at bar i is built only from bars <= i.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field

import numpy as np

from .indicators import volume_profile


@dataclass
class Level:
    price: float
    kind: str                  # 'support' | 'resistance' | 'flip'
    touches: int
    score: int                 # 0..100
    width: float               # half-width of the band, in price
    first_t: int
    last_t: int
    reaction_atr: float = 0.0
    is_round: bool = False
    broken: bool = False
    distance_atr: float = 0.0  # from current price, signed: +above, -below
    sources: list = field(default_factory=list)   # what built it

    def to_dict(self) -> dict:
        return asdict(self)


def _round_number_pull(price: float) -> float:
    """
    How close is this to a level gold actually respects? 0..1.

    Whole dollars matter, halves matter less, and the big figures (x00) matter
    most - that is where option strikes and resting orders cluster.
    """
    for step, weight in ((100.0, 1.0), (50.0, 0.85), (10.0, 0.6), (5.0, 0.4), (1.0, 0.2)):
        if abs(price - round(price / step) * step) < step * 0.02:
            return weight
    return 0.0


def _reaction_strength(h, l, c, idx: int, price: float, atr_val: float,
                       forward: int = 12) -> float:
    """How far price travelled away from `price` after touching it, in ATR."""
    end = min(c.size, idx + forward + 1)
    if end <= idx + 1 or atr_val <= 0:
        return 0.0
    seg_h = h[idx + 1:end].max()
    seg_l = l[idx + 1:end].min()
    return float(max(seg_h - price, price - seg_l) / atr_val)


def find_levels(h, l, c, t, swings: list, atr_arr: np.ndarray,
                tolerance_atr: float = 0.35, min_touches: int = 2,
                max_levels: int = 14, volume: np.ndarray = None) -> list:
    """
    Cluster swing extremes into scored S/R bands.

    Swings are the seed rather than every bar's high/low: a level that nothing
    ever reversed at is not a level, it is a price.
    """
    if not swings or c.size == 0:
        return []
    atr_now = float(atr_arr[~np.isnan(atr_arr)][-1]) if np.isfinite(atr_arr).any() else 1.0
    if atr_now <= 0:
        atr_now = max(float(np.mean(h - l)), 1e-6)
    tol = tolerance_atr * atr_now
    price_now = float(c[-1])
    last_t = int(t[-1])
    first_t = int(t[0])
    span_t = max(1, last_t - first_t)

    # --- cluster ----------------------------------------------------------- #
    seeds = [(s.idx, s.price, s.kind) for s in swings]
    seeds.sort(key=lambda x: x[1])
    clusters: list = []
    for idx, price, kind in seeds:
        if clusters and abs(price - clusters[-1]['mean']) <= tol:
            g = clusters[-1]
            g['members'].append((idx, price, kind))
            g['mean'] = float(np.mean([m[1] for m in g['members']]))
        else:
            clusters.append({'mean': float(price), 'members': [(idx, price, kind)]})

    # --- add the volume profile's high-volume node as its own seed ---------- #
    vp_levels = {}
    if volume is not None and volume.size == c.size:
        vp = volume_profile(h, l, c, volume, bins=48)
        for key in ('poc', 'vah', 'val'):
            if vp.get(key) is not None:
                vp_levels[key] = float(vp[key])

    out: list = []
    for g in clusters:
        members = g['members']
        if len(members) < min_touches:
            # A single touch can still matter if it is a big round number or the
            # volume POC - otherwise drop it.
            solo = members[0][1]
            near_vp = any(abs(solo - v) <= tol for v in vp_levels.values())
            if _round_number_pull(solo) < 0.85 and not near_vp:
                continue

        price = float(np.mean([m[1] for m in members]))
        touches = len(members)
        idxs = [m[0] for m in members]
        kinds = [m[2] for m in members]
        member_t = [int(t[i]) for i in idxs]

        # reaction: best move away from the band after any touch
        reaction = max((_reaction_strength(h, l, c, i, price, atr_now) for i in idxs),
                       default=0.0)

        # "Broken" means price left decisively and stayed out for a while, not
        # that it oscillated across the band at some point in 600 bars - over a
        # window that long, almost every level is crossed and the loose test
        # labelled the entire book as broken.
        after = c[max(idxs) + 1:]
        broken = False
        if after.size >= 3:
            beyond = np.abs(after - price) > 1.5 * tol
            broken = bool(beyond.sum() >= 3 and np.abs(after[-1] - price) > 1.5 * tol)

        # Polarity flip has a specific meaning: the band held as resistance AND
        # as support at different times. That shows up as a cluster containing
        # both swing highs and swing lows - not as a crossing.
        n_high = sum(1 for x in kinds if x == 'high')
        n_low = touches - n_high
        above = price > price_now
        if n_high >= 1 and n_low >= 1 and touches >= 3:
            kind = 'flip'
        elif above:
            kind = 'resistance'
        else:
            kind = 'support'

        recency = (max(member_t) - first_t) / span_t          # 0..1
        round_pull = _round_number_pull(price)
        near_vp = any(abs(price - v) <= tol for v in vp_levels.values())

        score = (
            min(touches, 6) * 11 +            # up to 66
            recency * 18 +                    # up to 18
            min(reaction, 3.0) * 6 +          # up to 18
            round_pull * 10 +                 # up to 10
            (8 if near_vp else 0) +
            (6 if kind == 'flip' else 0)
        )
        score = int(max(0, min(100, round(score))))

        sources = []
        if touches >= 2:
            sources.append(f'{touches} swing touches')
        if round_pull >= 0.85:
            sources.append('major round number')
        elif round_pull >= 0.4:
            sources.append('round number')
        if near_vp:
            sources.append('high-volume node')
        if kind == 'flip':
            sources.append('flipped polarity')

        out.append(Level(
            price=round(price, 3), kind=kind, touches=touches, score=score,
            width=round(tol, 3), first_t=min(member_t), last_t=max(member_t),
            reaction_atr=round(reaction, 2),
            is_round=round_pull >= 0.4, broken=broken,
            distance_atr=round((price - price_now) / atr_now, 2),
            sources=sources,
        ))

    out.sort(key=lambda lv: -lv.score)
    return out[:max_levels]


def nearest_levels(levels: list, price: float):
    """(nearest level above, nearest below) - the walls a trade has to clear."""
    above = [lv for lv in levels if lv.price > price]
    below = [lv for lv in levels if lv.price < price]
    above.sort(key=lambda lv: lv.price)
    below.sort(key=lambda lv: -lv.price)
    return (above[0] if above else None, below[0] if below else None)


def room_to_target(levels: list, entry: float, target: float, side: str,
                   min_score: int = 45):
    """
    Is the path from entry to target actually clear?

    A 2R target with a 90-score resistance sitting at 1.2R is not a 2R target,
    it is a 1.2R target with extra steps. Returns the first obstacle and how far
    into the move it sits, as a fraction.
    """
    lo, hi = (entry, target) if target > entry else (target, entry)
    span = abs(target - entry)
    if span <= 0:
        return {'clear': False, 'obstacle': None, 'fraction': 0.0}
    blockers = [lv for lv in levels
                if lv.score >= min_score and lo < lv.price < hi and not lv.broken]
    if side == 'buy':
        blockers = [lv for lv in blockers if lv.kind in ('resistance', 'flip')]
    else:
        blockers = [lv for lv in blockers if lv.kind in ('support', 'flip')]
    if not blockers:
        return {'clear': True, 'obstacle': None, 'fraction': 1.0}
    blockers.sort(key=lambda lv: abs(lv.price - entry))
    first = blockers[0]
    frac = abs(first.price - entry) / span
    return {
        'clear': frac >= 0.95,
        'obstacle': first.to_dict(),
        'fraction': round(float(frac), 3),
        'count': len(blockers),
    }


def session_levels(t: np.ndarray, h: np.ndarray, l: np.ndarray,
                   c: np.ndarray) -> dict:
    """
    Prior day / prior week high-low, and the current day's range so far.

    These are the reference points every desk watches, which is precisely why
    they work: PDH and PDL are where the stops are.
    """
    if t.size == 0:
        return {}
    days = (t // 86_400_000).astype(np.int64)
    today = days[-1]
    out: dict = {}

    cur = days == today
    if cur.any():
        out['day_high'] = float(h[cur].max())
        out['day_low'] = float(l[cur].min())
        out['day_open'] = float(c[cur][0])

    prev_days = np.unique(days[days < today])
    if prev_days.size:
        pd_mask = days == prev_days[-1]
        out['pdh'] = float(h[pd_mask].max())
        out['pdl'] = float(l[pd_mask].min())
        out['pdc'] = float(c[pd_mask][-1])

    weeks = ((t // 86_400_000 + 4) // 7).astype(np.int64)   # epoch Thu -> Mon weeks
    this_week = weeks[-1]
    prev_weeks = np.unique(weeks[weeks < this_week])
    if prev_weeks.size:
        pw = weeks == prev_weeks[-1]
        out['pwh'] = float(h[pw].max())
        out['pwl'] = float(l[pw].min())
    return out


def liquidity_pools(swings: list, equal_highs: list, equal_lows: list,
                    price: float, atr_val: float) -> list:
    """
    Where resting stop orders are likely parked.

    Equal highs/lows are the obvious pools. Unswept single swings count too, at
    lower weight. Each pool gets a `pull` score - how strong a magnet it is for
    a liquidity-sweep move, combining size of the cluster and closeness.
    """
    pools: list = []

    def add(price_lvl, count, side, swept, label, base):
        if atr_val <= 0:
            return
        dist = abs(price_lvl - price) / atr_val
        # Magnets stop pulling once they are far away, and stop existing once
        # they are taken. 12 ATR is the horizon a scalp can plausibly reach;
        # 8 decayed so fast that a good pool 5 ATR out scored below a poor one
        # at arm's length.
        pull = base * max(0.0, 1.0 - dist / 12.0) * (0.25 if swept else 1.0)
        pools.append({
            'price': round(float(price_lvl), 3),
            'side': side,                    # 'buyside' above, 'sellside' below
            'count': int(count),
            'swept': bool(swept),
            'label': label,
            'distance_atr': round(float(dist), 2),
            'pull': int(max(0, min(100, round(pull)))),
        })

    for g in equal_highs:
        add(g['price'], g['count'], 'buyside', g['swept'],
            f"equal highs x{g['count']}", 55 + 12 * min(g['count'], 4))
    for g in equal_lows:
        add(g['price'], g['count'], 'sellside', g['swept'],
            f"equal lows x{g['count']}", 55 + 12 * min(g['count'], 4))

    covered = [p['price'] for p in pools]
    for s in swings[-14:]:
        if s.swept:
            continue
        if any(abs(s.price - p) <= atr_val * 0.3 for p in covered):
            continue
        add(s.price, 1, 'buyside' if s.kind == 'high' else 'sellside', False,
            f'unswept swing {s.label or s.kind}', 38)

    pools.sort(key=lambda p: -p['pull'])
    return pools[:12]


__all__ = ['Level', 'find_levels', 'nearest_levels', 'room_to_target',
           'session_levels', 'liquidity_pools']
