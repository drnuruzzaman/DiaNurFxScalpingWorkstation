"""
server/forecast/barrier.py - baseline odds for any stop / target pair.

The trade templates in labels.py cover fixed distances. A real signal's stop
and targets are wherever the setup put them, so the lab needs the baseline for
ANY pair: P(target first), P(stop first), P(neither inside 72 x 5m).

From the trade labels' first-touch grid (the bid path from the entry bid, in
ATR of the decision bar, no costs), for every pair of grid levels (stop a,
target b) and every month:

    U[a, b]   price rises b before it falls a   (a buy's target first)
    D[a, b]   price falls b before it rises a   (a sell's target first)

counted from outcomes RESOLVED before the month started, like every baseline.

Costs are applied by moving the levels, which is exact for a constant spread
c and slippage s (both in ATR): a buy enters c + s above the bid, so its
target needs the bid to rise b + c + s and its stop a fall of only a - c - s;
a sell enters s below the bid and exits on the ask, which gives the same two
shifts. The shifted pair is read off the grid by bilinear interpolation - the
grid is 0.25 ATR apart and the odds move smoothly with distance.
"""
from __future__ import annotations

import numpy as np

from . import LEVELS
from .labels import NEVER
from .tables import Months

GRID = np.asarray(LEVELS, dtype=np.float64)


def tables(lab: dict, months: Months, start_ms: int) -> dict:
    """
    Monthly U and D tables from trade labels: (months, stop level, target
    level, 3) probabilities of (target first, stop first, neither).
    """
    L = GRID.size
    use = lab['t'] >= start_ms
    known = np.where(use, months.known_from(lab['resolve_ms']), months.n)
    ok = known < months.n
    km = known[ok]
    up, dn = lab['up_tau'][ok], lab['dn_tau'][ok]
    out = {}
    for name, tgt, stp in (('U', up, dn), ('D', dn, up)):
        cnt = np.zeros(months.n * L * L * 3, dtype=np.float64)
        for i in range(L):                                   # stop level a_i
            s = stp[:, i][:, None]                           # (n, 1)
            cls = np.where(tgt < s, 0, np.where(s < tgt, 1, 2))   # (n, L targets)
            idx = ((km[:, None] * L + i) * L + np.arange(L)[None, :]) * 3 + cls
            cnt += np.bincount(idx.ravel(), minlength=cnt.size)
        c = np.cumsum(cnt.reshape(months.n, L, L, 3), axis=0)
        tot = c.sum(axis=3, keepdims=True)
        with np.errstate(invalid='ignore', divide='ignore'):
            out[name] = np.where(tot > 0, c / tot, np.nan).astype(np.float32)
    out['n'] = np.cumsum(np.bincount(km, minlength=months.n)).astype(np.float64)
    return out


def _bilinear(T: np.ndarray, a: float, b: float) -> tuple:
    """T (L, L, 3) at stop a, target b (ATR). Returns (probs, inside_grid)."""
    inside = GRID[0] <= a <= GRID[-1] and GRID[0] <= b <= GRID[-1]
    a = float(np.clip(a, GRID[0], GRID[-1]))
    b = float(np.clip(b, GRID[0], GRID[-1]))
    i = int(np.clip(np.searchsorted(GRID, a, 'right') - 1, 0, GRID.size - 2))
    j = int(np.clip(np.searchsorted(GRID, b, 'right') - 1, 0, GRID.size - 2))
    fa = (a - GRID[i]) / (GRID[i + 1] - GRID[i])
    fb = (b - GRID[j]) / (GRID[j + 1] - GRID[j])
    p = ((1 - fa) * (1 - fb) * T[i, j] + fa * (1 - fb) * T[i + 1, j]
         + (1 - fa) * fb * T[i, j + 1] + fa * fb * T[i + 1, j + 1])
    return p, inside


def odds(tabs: dict, month: int, side: str, stop_atr: float, target_atr: float,
         cost_atr: float, slip_atr: float) -> dict:
    """
    Baseline odds for a trade: {'target': p, 'stop': p, 'neither': p, 'inside': bool}
    - replayed at this trade's own cost (see the module doc).
    """
    shift = float(cost_atr) + float(slip_atr)
    a, b = float(stop_atr) - shift, float(target_atr) + shift
    T = tabs['U' if side == 'buy' else 'D'][month]
    p, inside = _bilinear(T, a, b)
    if not np.isfinite(p).all():
        return {'target': None, 'stop': None, 'neither': None, 'inside': False}
    s = float(p.sum()) or 1.0
    return {'target': float(p[0] / s), 'stop': float(p[1] / s), 'neither': float(p[2] / s),
            'inside': bool(inside and a > 0)}


def grid_outcome(up_tau: np.ndarray, dn_tau: np.ndarray, side: str, i: int, j: int) -> np.ndarray:
    """Per-sample outcome at grid stop i, target j: 0 target, 1 stop, 2 neither."""
    tgt, stp = (up_tau[:, j], dn_tau[:, i]) if side == 'buy' else (dn_tau[:, j], up_tau[:, i])
    return np.where(tgt < stp, 0, np.where(stp < tgt, 1, 2))


__all__ = ['GRID', 'NEVER', 'tables', 'odds', 'grid_outcome']
