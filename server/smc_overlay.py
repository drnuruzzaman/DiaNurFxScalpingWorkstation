"""
server/smc_overlay.py - liquidity pools for the charts.

Ported 2026-11-06 from ScalpingWorkstation (server/smc/liquidity.py, zones.py,
structure.py, core.py) for the live chart and the backtest lab chart. Display
only: no playbook, gate or regime reads any of it, and nothing in server/engine
or the lab's replay (both fingerprinted by the forward books) is touched.

LIQUIDITY POOLS - where resting stops sit, and how they got taken:
    PDH/PDL    previous broker day's high/low            (from 1d bars)
    PWH/PWL    previous week's high/low                  (from 1w bars)
    ASIA_H/L   19:00-24:00 New York                      (from 15m bars)
    LON_H/L    London killzone, 02:00-05:00 New York     (from 15m bars)
    SWING_H/L  unswept 1h swing points                   (from 1h bars)
    EQH/EQL    two 1h swings at the same price
  A pool RESTS from when it is known until 5m price trades half a pip beyond
  it: a SWEEP if that bar closes back inside, a RUN if it closes beyond.

ORDER BLOCKS are not here: since 2026-11-07 both charts draw the engine's own
(server/engine/zones.order_blocks, snapshot['order_blocks']) - the ones that
score the signals.

Everything is causal: callers pass the time `now`, and only bars CLOSED by then
are used. Times are milliseconds on whatever clock the caller's bars use - UTC
on the live chart, the broker's clock in the lab - with `fx` mapping that clock
onto the FX day (New York + 7h, where the broker day starts at 17:00 New York)
so session windows and Fridays are found the same way on both.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from .engine.indicators import atr as atr_fn


NEVER = 2 ** 62
DAY = 86_400_000
H = 3_600_000

# ScalpingWorkstation server/smc/__init__.py: bars either side of a fractal pivot.
PIVOT_K = {'1m': 3, '3m': 2, '5m': 2, '15m': 2, '30m': 2, '1h': 3, '2h': 3, '4h': 3,
           '1d': 2, '1w': 2}
RANK = {'PWH': 5, 'PWL': 5, 'PDH': 4, 'PDL': 4, 'LON_H': 3, 'LON_L': 3,
        'ASIA_H': 2, 'ASIA_L': 2, 'EQH': 2, 'EQL': 2, 'SWING_H': 1, 'SWING_L': 1}

# How much history each pool source needs, in bars of its own timeframe.
POOL_BARS = {'1d': 30, '1w': 8, '15m': 96 * 12, '1h': 400, '5m': 288 * 12}


# --------------------------------------------------------------------------- #
# clocks                                                                       #
# --------------------------------------------------------------------------- #
def fx_from_utc(t: np.ndarray) -> np.ndarray:
    """UTC ms -> FX-day ms (New York wall clock + 7h), DST-aware per hour."""
    from zoneinfo import ZoneInfo
    ny = ZoneInfo('America/New_York')
    t = np.asarray(t, dtype=np.int64)
    if not t.size:
        return t
    hours = t // H
    uniq, inv = np.unique(hours, return_inverse=True)
    off = np.array([int(datetime.fromtimestamp(int(u) * 3600, tz=timezone.utc)
                        .astimezone(ny).utcoffset().total_seconds() * 1000) for u in uniq],
                   dtype=np.int64)
    return t + off[inv] + 7 * H


def fx_identity(t: np.ndarray) -> np.ndarray:
    """The broker clock already is the FX day (ScalpingWorkstation's assumption)."""
    return np.asarray(t, dtype=np.int64)


def closed(series, tf_ms: int, now_ms: int, count: int = None):
    """(t, o, h, l, c) of the bars of `series` closed by `now_ms`, the last `count`."""
    t = np.asarray(series.t, dtype=np.int64)
    j = int(np.searchsorted(t + tf_ms, now_ms, side='right'))
    i = 0 if count is None else max(0, j - count)
    return (t[i:j], np.asarray(series.o[i:j], float), np.asarray(series.h[i:j], float),
            np.asarray(series.l[i:j], float), np.asarray(series.c[i:j], float))


# --------------------------------------------------------------------------- #
# pivots                                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class Pivot:
    i: int
    t: int
    price: float
    kind: str          # 'H' | 'L'
    known_at: int


def pivots(t, h, l, k: int, tf_ms: int) -> list:
    """Fractal pivots: strictly beyond the k bars before, not exceeded by the k
    after. Known once bar i+k has closed."""
    n = len(t)
    out: list = []
    for i in range(k, n - k):
        hh, ll = h[i - k:i + k + 1], l[i - k:i + k + 1]
        known = int(t[i + k]) + tf_ms
        if h[i] > hh[:k].max() and h[i] >= hh[k + 1:].max():
            out.append(Pivot(i, int(t[i]), float(h[i]), 'H', known))
        if l[i] < ll[:k].min() and l[i] <= ll[k + 1:].min():
            out.append(Pivot(i, int(t[i]), float(l[i]), 'L', known))
    return out


# --------------------------------------------------------------------------- #
# liquidity pools                                                              #
# --------------------------------------------------------------------------- #
@dataclass
class Pool:
    kind: str
    side: int             # +1 buy-side (above highs), -1 sell-side (below lows)
    price: float
    t_from: int
    known_at: int
    valid_until: int
    consumed_at: int = NEVER
    consumed_by: str = ''          # 'sweep' | 'run'

    @property
    def rank(self) -> int:
        return RANK.get(self.kind, 0)

    def resting_at(self, now: int) -> bool:
        return self.known_at <= now < min(self.consumed_at, self.valid_until)

    def to_dict(self) -> dict:
        used = self.consumed_at != NEVER
        return {'kind': self.kind, 'side': self.side, 'price': self.price,
                'from_t': self.t_from, 'known_t': self.known_at, 'end_t': self.valid_until,
                'consumed_t': self.consumed_at if used else None,
                'consumed_by': self.consumed_by if used else None}


def _weekday(fx_t: int) -> int:
    return int((fx_t // DAY + 3) % 7)          # Monday = 0


def _daily(t, h, l, fx) -> list:
    out = []
    ft = fx(t)
    for i in range(len(t)):
        t0 = int(t[i])
        gap = 3 if _weekday(int(ft[i]) + H) == 4 else 1      # Friday -> next session Monday
        out.append(Pool('PDH', 1, float(h[i]), t0, t0 + DAY, t0 + gap * DAY + DAY))
        out.append(Pool('PDL', -1, float(l[i]), t0, t0 + DAY, t0 + gap * DAY + DAY))
    return out


def _weekly(t, h, l) -> list:
    out = []
    for i in range(len(t)):
        t0 = int(t[i])
        known = t0 + 7 * DAY
        out.append(Pool('PWH', 1, float(h[i]), t0, known, known + 5 * DAY))
        out.append(Pool('PWL', -1, float(l[i]), t0, known, known + 5 * DAY))
    return out


def _session(t, h, l, tf_ms, fx, s_h: float, e_h: float, name: str,
             min_cover: float = 0.8, valid_h: float = 19.0) -> list:
    """High/low of an FX-day window [s_h, e_h) each day (hours of the FX day,
    i.e. New York + 7h), known at its end and resting until `valid_h`."""
    if not len(t):
        return []
    ft = fx(t)
    tod = ft % DAY
    mask = (tod >= int(s_h * H)) & (tod < int(e_h * H))
    if not mask.any():
        return []
    days = ft[mask] // DAY
    ts, fts, hs, ls = t[mask], ft[mask], h[mask], l[mask]
    expected = (e_h - s_h) * H / tf_ms
    starts = np.flatnonzero(np.r_[True, days[1:] != days[:-1]])
    ends = np.r_[starts[1:], len(days)]
    out = []
    for a, z in zip(starts, ends):
        if (z - a) < min_cover * expected:
            continue
        off = int(fts[a]) - int(ts[a])                 # this day's clock -> FX-day shift
        base = int(days[a]) * DAY
        known, valid = base + int(e_h * H) - off, base + int(valid_h * H) - off
        out.append(Pool(f'{name}_H', 1, float(hs[a:z].max()), int(ts[a]), known, valid))
        out.append(Pool(f'{name}_L', -1, float(ls[a:z].min()), int(ts[a]), known, valid))
    return out


def _swings(t, h, l, c, tf_ms, pip, life_bars: int = 120) -> list:
    """Unswept 1h swing points, plus equal highs/lows where two swings align."""
    a = np.nan_to_num(atr_fn(h, l, c, 14), nan=0.0)
    life = life_bars * tf_ms
    out, by_kind = [], {'H': [], 'L': []}
    for p in pivots(t, h, l, PIVOT_K['1h'], tf_ms):
        side = 1 if p.kind == 'H' else -1
        out.append(Pool('SWING_H' if side == 1 else 'SWING_L', side, p.price, p.t,
                        p.known_at, p.known_at + life))
        prev = by_kind[p.kind]
        for q in reversed(prev[-3:]):
            if p.i - q.i > life_bars:
                continue
            if abs(p.price - q.price) > max(pip, 0.1 * float(a[p.i])):
                continue
            lvl = max(p.price, q.price) if side == 1 else min(p.price, q.price)
            between = c[q.i + 1:p.i]
            if (side == 1 and (between > lvl).any()) or (side == -1 and (between < lvl).any()):
                continue
            out.append(Pool('EQH' if side == 1 else 'EQL', side, lvl, q.t, p.known_at,
                            p.known_at + life))
            break
        prev.append(p)
    return out


def _consume(pools: list, t, h, l, c, tf_ms, min_pen: float) -> None:
    """Stamp when each pool was swept or run, on the detection (5m) bars."""
    for p in pools:
        a = int(np.searchsorted(t, p.known_at, side='left'))
        z = int(np.searchsorted(t, p.valid_until, side='left'))
        if a >= z:
            continue
        hit = h[a:z] >= p.price + min_pen if p.side == 1 else l[a:z] <= p.price - min_pen
        if not hit.any():
            continue
        x = a + int(np.argmax(hit))
        p.consumed_at = int(t[x]) + tf_ms
        inside = c[x] < p.price if p.side == 1 else c[x] > p.price
        p.consumed_by = 'sweep' if inside else 'run'


def build_pools(get, now_ms: int, fx, pip: float) -> list:
    """Every pool known by `now_ms`, consumption stamped. `get(tf)` returns a
    series of that timeframe; only its bars closed by `now_ms` are used."""
    from .config import TF_SECONDS
    ms = {k: TF_SECONDS[k] * 1000 for k in POOL_BARS}
    src = {k: closed(get(k), ms[k], now_ms, n) for k, n in POOL_BARS.items()}
    d1, w1, m15, h1, m5 = src['1d'], src['1w'], src['15m'], src['1h'], src['5m']
    pools = _daily(d1[0], d1[2], d1[3], fx) + _weekly(w1[0], w1[2], w1[3])
    pools += _session(m15[0], m15[2], m15[3], ms['15m'], fx, 2.0, 7.0, 'ASIA')    # 19-24 NY
    pools += _session(m15[0], m15[2], m15[3], ms['15m'], fx, 9.0, 12.0, 'LON')    # 02-05 NY
    pools += _swings(h1[0], h1[2], h1[3], h1[4], ms['1h'], pip)
    pools = [p for p in pools if p.known_at <= now_ms]
    pools.sort(key=lambda p: p.known_at)
    _consume(pools, m5[0], m5[2], m5[3], m5[4], ms['5m'], 0.5 * pip)
    return pools


def pick_pools(pools: list, now_ms: int, t_start: int, last_price: float, pip: float) -> list:
    """What the chart draws (ScalpingWorkstation's choice): every day, week and
    session pool in view; of the swing/EQ pools, the 3 nearest still resting on
    each side of price and the 4 most recent sweeps. Pools at one price merge
    into the highest-ranked."""
    swingish = ('SWING', 'EQ')
    visible = [p for p in pools if p.known_at <= now_ms
               and min(p.consumed_at, p.valid_until) > t_start]
    key = [p for p in visible if not p.kind.startswith(swingish)]
    rest = [p for p in visible if p.kind.startswith(swingish) and p.resting_at(now_ms)]
    above = sorted((p for p in rest if p.price >= last_price), key=lambda p: p.price)[:3]
    below = sorted((p for p in rest if p.price < last_price), key=lambda p: -p.price)[:3]
    swept = [p for p in visible if p.kind.startswith(swingish) and p.consumed_by == 'sweep'
             and p.consumed_at <= now_ms][-4:]
    merged: dict = {}
    for p in key + above + below + swept:
        k = (round(p.price / (pip / 10)), p.consumed_by or 'rest')
        if k not in merged or p.rank > merged[k].rank:
            merged[k] = p
    return [p.to_dict() for p in sorted(merged.values(), key=lambda p: p.known_at)]


__all__ = ['fx_from_utc', 'fx_identity', 'closed', 'order_blocks', 'build_pools', 'pick_pools']
