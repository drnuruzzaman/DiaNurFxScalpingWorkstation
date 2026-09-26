"""
server/forecast/tables.py - conditional tables with hierarchical shrinkage.

A table answers "in conditions like these, what happened next?" - built so a
sparse cell cannot claim more than it knows:

  levels     a hierarchy of cells, coarse to fine (e.g. hour -> hour x news ->
             hour x news x volatility). Every finer cell is blended with its
             parent: weight n_eff / (n_eff + k) on the cell, the rest on the
             parent, recursively down to all rows. A cell seen 17 times says
             almost nothing of its own; one seen 5,000 times speaks for itself.
  snapshots  rebuilt at the start of every (broker-time) month from outcomes
             RESOLVED before it. A forecast made during a month reads that
             month's snapshot - exactly what a system refreshed monthly would
             have known - so nothing learns from an outcome before it is known.
  outputs    the prediction for every (month, finest cell) pair, computed once.
             A forecast is then a lookup, which is what lets the lab serve it
             per bar without shipping per-row data.

Everything is vectorised over (month, cell): histograms are accumulated per
month and cumulated, so a table covering 2018-2026 builds in about a second.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

QBIN = 0.05                   # ATR per histogram bin
QNBINS = 240                  # 0 .. 12 ATR; wider outcomes land in the last bin


class Months:
    """Broker-time month starts spanning a period, and index maths on them."""

    def __init__(self, first_ms: int, last_ms: int):
        a = datetime.fromtimestamp(first_ms / 1000, timezone.utc)
        b = datetime.fromtimestamp(last_ms / 1000, timezone.utc)
        starts = []
        y, m = a.year, a.month
        while (y, m) <= (b.year, b.month):
            starts.append(int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        self.starts = np.array(starts, dtype=np.int64)
        self.n = self.starts.size

    def of(self, ms) -> np.ndarray:
        """Index of the month each instant falls in (clipped to the span)."""
        i = np.searchsorted(self.starts, np.asarray(ms, dtype=np.int64), 'right') - 1
        return np.clip(i, 0, self.n - 1)

    def known_from(self, resolve_ms) -> np.ndarray:
        """First month whose START is at or after each resolve time (n if never)."""
        return np.searchsorted(self.starts, np.asarray(resolve_ms, dtype=np.int64), 'left')


def _hist(months: Months, known: np.ndarray, codes: np.ndarray, n_cells: int,
          x: np.ndarray, half_life_months: float = None) -> np.ndarray:
    """
    (months, cells, bins) histogram of x, cumulated over months (resolved-only).
    With a half-life, older months are down-weighted: acc = f * acc + new.
    """
    ok = (known < months.n) & np.isfinite(x) & (codes >= 0)
    b = np.clip(np.floor(np.where(ok, x, 0.0) / QBIN).astype(np.int64), 0, QNBINS - 1)
    h = np.zeros((months.n, n_cells, QNBINS), dtype=np.float64)
    np.add.at(h, (known[ok], codes[ok], b[ok]), 1.0)
    if not half_life_months:
        return np.cumsum(h, axis=0)
    f = 0.5 ** (1.0 / float(half_life_months))
    for m in range(1, months.n):
        h[m] += f * h[m - 1]
    return h


def _quantiles_from_pmf(pmf: np.ndarray, qs) -> np.ndarray:
    """Quantiles (linear within a bin) of each row of a (rows, bins) pmf."""
    cdf = np.cumsum(pmf, axis=1)
    out = np.empty((pmf.shape[0], len(qs)))
    r = np.arange(pmf.shape[0])
    for qi, q in enumerate(qs):
        idx = np.argmax(cdf >= q - 1e-12, axis=1)
        below = np.where(idx > 0, cdf[r, np.maximum(idx - 1, 0)], 0.0)
        inbin = pmf[r, idx]
        with np.errstate(invalid='ignore', divide='ignore'):
            frac = np.where(inbin > 0, (q - below) / inbin, 0.0)
        out[:, qi] = (idx + np.clip(frac, 0.0, 1.0)) * QBIN
    return out


def quantile_table(months: Months, known: np.ndarray, lineage: np.ndarray, fine_codes: np.ndarray,
                   x: np.ndarray, k_prior: float, horizon: int, qs,
                   half_life_months: float = None) -> dict:
    """
    Blended conditional quantiles for every (month, finest cell).

    fine_codes holds each sample's finest cell (-1 = none). lineage[l, c] is
    finest cell c's cell at level l - level 0 is one cell for everything and
    the last level is the finest cell itself - so the hierarchy is NESTED by
    construction. Samples join the tables from their month `known` on.

    Returns {'q': (months, cells, len(qs)), 'neff': (months, cells) effective
    samples in the finest cell, 'w': (months, cells) the finest cell's own weight}.
    With half_life_months, older outcomes count less (n_eff counts the weights).
    """
    lineage = np.asarray(lineage, dtype=np.int64)
    L, C = lineage.shape
    fc = np.asarray(fine_codes, dtype=np.int64)
    hists = []
    for l in range(L):
        size = int(lineage[l].max()) + 1
        codes = np.where(fc >= 0, lineage[l][np.maximum(fc, 0)], -1)
        hists.append(_hist(months, known, codes, size, x, half_life_months))
    code_at = lineage
    pmf = None
    neff = w = None
    for l in range(L):
        h = hists[l][:, code_at[l]]                       # (months, C, bins)
        n = h.sum(axis=2)
        with np.errstate(invalid='ignore', divide='ignore'):
            own = np.where(n[..., None] > 0, h / np.where(n > 0, n, 1)[..., None], 0.0)
        if pmf is None:                                   # all rows: no parent
            pmf = own
            if L == 1:
                neff, w = n / horizon, np.ones_like(n)
            continue
        ne = n / horizon
        wl = ne / (ne + k_prior)
        pmf = wl[..., None] * own + (1.0 - wl[..., None]) * pmf
        neff, w = ne, wl
    M = months.n
    flat = pmf.reshape(M * C, QNBINS)
    empty = flat.sum(axis=1) <= 0
    q = _quantiles_from_pmf(np.where(empty[:, None], 0.0, flat), qs)
    q[empty] = np.nan
    return {'q': q.reshape(M, C, len(qs)).astype(np.float32),
            'neff': neff.astype(np.float32), 'w': w.astype(np.float32)}


def lift_table(months: Months, known: np.ndarray, lineage: np.ndarray, fine_codes: np.ndarray,
               resid: np.ndarray, k_prior: float, horizon: int,
               half_life_months: float = None) -> dict:
    """
    Conditional LIFT over a baseline, for every (month, finest cell).

    resid holds each sample's outcome minus what its baseline said for it at
    the time - (N,) for a yes/no outcome, (N, classes) for a categorical one
    (one-hot minus the baseline's probabilities); rows with a NaN are skipped.
    A cell's lift is its mean residual, blended with its parent's lift by
    n_eff / (n_eff + k) and so on up to all rows. The forecast is then the
    baseline plus the lift: a cell with little history says little beyond
    what the baseline already says, and drift the baseline carries (a bull
    year, cheaper costs) is not re-learned as skill.

    Returns {'lift': (months, cells, classes), 'neff': (months, cells), 'w': ...}.
    """
    lineage = np.asarray(lineage, dtype=np.int64)
    L, C = lineage.shape
    R = np.asarray(resid, dtype=np.float64)
    R = R[:, None] if R.ndim == 1 else R
    K = R.shape[1]
    fc = np.asarray(fine_codes, dtype=np.int64)
    ok = (known < months.n) & (fc >= 0) & np.isfinite(R).all(axis=1)
    f = 0.5 ** (1.0 / float(half_life_months)) if half_life_months else 1.0
    lift = neff = w = None
    for l in range(L):
        size = int(lineage[l].max()) + 1
        codes = lineage[l][np.maximum(fc, 0)]
        s = np.zeros((months.n, size, K))
        n = np.zeros((months.n, size))
        np.add.at(s, (known[ok], codes[ok]), R[ok])
        np.add.at(n, (known[ok], codes[ok]), 1.0)
        if f >= 1.0:
            s, n = np.cumsum(s, axis=0), np.cumsum(n, axis=0)
        else:
            for m in range(1, months.n):
                s[m] += f * s[m - 1]
                n[m] += f * n[m - 1]
        s, n = s[:, lineage[l]], n[:, lineage[l]]              # onto the finest cells
        with np.errstate(invalid='ignore', divide='ignore'):
            own = np.where(n[..., None] > 0, s / np.where(n > 0, n, 1)[..., None], 0.0)
        ne = n / horizon
        if lift is None:                                        # all rows
            lift = own
            neff, w = ne, (n > 0).astype(float)
            continue
        wl = ne / (ne + k_prior)
        lift = wl[..., None] * own + (1.0 - wl[..., None]) * lift
        neff, w = ne, wl
    return {'lift': lift.astype(np.float32), 'neff': neff.astype(np.float32),
            'w': w.astype(np.float32)}


__all__ = ['QBIN', 'QNBINS', 'Months', 'quantile_table', 'lift_table']
