"""
server/forecast/baseline.py - the empirical, resolved-only baseline.

The number every forecast has to beat. It is unconditional - it knows the
horizon, the barrier distances in ATR and the costs, but nothing about the
market's current state - and it is honest about time:

  - it is refreshed at the start of each broker day from every sample whose
    outcome had RESOLVED by then (resolve_ms <= day start), and nothing else;
    that is exactly what a live system could have counted that morning, so
    no purge window has to be guessed - the resolve time IS the purge
  - the window expands from TRAIN_START (decision 1); optionally each sample
    is down-weighted by its age with a half-life in days (the recency option)
  - distances are in ATR of the forecast bar, so the range baseline is the
    strong one: "the usual multiple of the current ATR"
  - trade baselines are REPLAYED AT TODAY'S COST: the spread in ATR is known
    when a trade is decided and it moves the odds, so every historical path
    is counted as it would have played at the row's own cost (see trade())

Everything is computed for all days at once: each sample is added on the day
it becomes known, and a (decayed) running sum over days gives each morning's
table. Tables carry their weight n and effective sample size n_eff.
"""
from __future__ import annotations

import numpy as np

from . import COST_LEVELS, MAE_AT, MFE_AT, TEMPLATES

DAY = 86_400_000
BIN = 0.02                   # ATR per histogram bin for the range quantiles
NBINS = 1000                 # 0 .. 20 ATR; anything wider lands in the last bin
QS = (0.2, 0.5, 0.8)
N_STATES = 5
TPL_CLASSES = (1, -1, 0)     # target first, stop first, neither


class Days:
    """The broker days a set of forecasts spans, and the index maths on them."""

    def __init__(self, first_ms: int, last_ms: int):
        self.d0 = int(first_ms) - int(first_ms) % DAY
        self.n = int((int(last_ms) - self.d0) // DAY) + 1
        self.starts = self.d0 + DAY * np.arange(self.n, dtype=np.int64)

    def of(self, ms) -> np.ndarray:
        """Index of the day each instant falls in (clipped to the span)."""
        i = (np.asarray(ms, dtype=np.int64) - self.d0) // DAY
        return np.clip(i, 0, self.n - 1)

    def known_from(self, resolve_ms) -> np.ndarray:
        """First day whose START is at or after each resolve time (n if never)."""
        return np.searchsorted(self.starts, np.asarray(resolve_ms, dtype=np.int64), 'left')


def decay_factor(half_life_days) -> float:
    return 1.0 if not half_life_days else float(0.5 ** (1.0 / float(half_life_days)))


def running(contrib: np.ndarray, f: float) -> np.ndarray:
    """acc[d] = f * acc[d-1] + contrib[d], along axis 0."""
    if f >= 1.0:
        return np.cumsum(contrib, axis=0)
    out = np.empty_like(contrib, dtype=np.float64)
    acc = np.zeros(contrib.shape[1:], dtype=np.float64)
    for d in range(contrib.shape[0]):
        acc = acc * f + contrib[d]
        out[d] = acc
    return out


def _add(days: Days, known: np.ndarray, weights=None, extra_shape=(), index=None) -> np.ndarray:
    """Per-day contribution array: samples added on the day they become known."""
    ok = known < days.n
    contrib = np.zeros((days.n,) + tuple(extra_shape), dtype=np.float64)
    w = np.ones(known.size) if weights is None else np.asarray(weights, dtype=np.float64)
    if index is None:
        np.add.at(contrib, known[ok], w[ok])
    else:
        np.add.at(contrib, (known[ok],) + tuple(ix[ok] for ix in index), w[ok])
    return contrib


def counts(days: Days, known: np.ndarray, f: float) -> dict:
    """Running weight n and effective sample size n_eff = (sum w)^2 / sum w^2."""
    c = _add(days, known)
    n = running(c, f)
    n2 = running(c, f * f)
    with np.errstate(invalid='ignore', divide='ignore'):
        n_eff = np.where(n2 > 0, n * n / n2, 0.0)
    return {'n': n, 'n_eff': n_eff}


def binary(days: Days, known: np.ndarray, y: np.ndarray, f: float) -> np.ndarray:
    """P(y) per day from resolved samples; y is 0/1 (or NaN to skip)."""
    ok = np.isfinite(y)
    k = np.where(ok, known, days.n)
    num = running(_add(days, k, np.where(ok, y, 0.0)), f)
    den = running(_add(days, k), f)
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(den > 0, num / den, np.nan)


def multiclass(days: Days, known: np.ndarray, y: np.ndarray, n_classes: int, f: float,
               classes=None) -> np.ndarray:
    """P(class) per day, (days, C). y holds class values; unknown ones are skipped."""
    classes = list(range(n_classes)) if classes is None else list(classes)
    idx = np.full(y.size, -1, dtype=np.int64)
    for i, c in enumerate(classes):
        idx[y == c] = i
    ok = idx >= 0
    k = np.where(ok, known, days.n)
    cnt = running(_add(days, k, None, (len(classes),), (np.maximum(idx, 0),)), f)
    tot = cnt.sum(axis=1, keepdims=True)
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(tot > 0, cnt / tot, np.nan)


def quantiles(days: Days, known: np.ndarray, x: np.ndarray, f: float, qs=QS) -> np.ndarray:
    """Weighted quantiles of x per day from a running histogram, (days, len(qs))."""
    ok = np.isfinite(x) & (x >= 0)
    k = np.where(ok, known, days.n)
    b = np.clip(np.floor(np.where(ok, x, 0.0) / BIN).astype(np.int64), 0, NBINS - 1)
    hist = running(_add(days, k, None, (NBINS,), (b,)), f)
    cdf = np.cumsum(hist, axis=1)
    total = cdf[:, -1]
    out = np.full((days.n, len(qs)), np.nan)
    for qi, q in enumerate(qs):
        target = q * total
        idx = np.argmax(cdf >= target[:, None] - 1e-12, axis=1)
        below = np.where(idx > 0, cdf[np.arange(days.n), np.maximum(idx - 1, 0)], 0.0)
        inbin = hist[np.arange(days.n), idx]
        with np.errstate(invalid='ignore', divide='ignore'):
            frac = np.where(inbin > 0, (target - below) / inbin, 0.0)
        out[:, qi] = np.where(total > 0, (idx + np.clip(frac, 0, 1)) * BIN, np.nan)
    return out


# --------------------------------------------------------------------------- #
# the baselines the batch and the lab use                                     #
# --------------------------------------------------------------------------- #
def market(days: Days, lab: dict, start_ms: int, half_life_days=None) -> dict:
    """
    Climatology for the market labels: per-step range quantiles (up and down
    excursion), per-step P(up), and P(state at the horizon), by day.
    """
    f = decay_factor(half_life_days)
    use = lab['complete'] & (lab['t'] >= start_ms)
    known = np.where(use, days.known_from(lab['resolve_ms']), days.n)
    H = lab['up'].shape[1]
    out = {'up_q': np.full((days.n, H, len(QS)), np.nan),
           'dn_q': np.full((days.n, H, len(QS)), np.nan),
           'p_up': np.full((days.n, H), np.nan)}
    for k in range(H):
        out['up_q'][:, k] = quantiles(days, known, lab['up'][:, k].astype(np.float64), f)
        out['dn_q'][:, k] = quantiles(days, known, lab['dn'][:, k].astype(np.float64), f)
        r = lab['ret'][:, k].astype(np.float64)
        out['p_up'][:, k] = binary(days, known, np.where(np.isfinite(r), (r > 0) * 1.0, np.nan), f)
    out['p_state'] = multiclass(days, known, lab['state_h'].astype(np.int64), N_STATES, f)
    out.update(counts(days, known, f))
    return out


def _trade_tables(days: Days, lab: dict, known: np.ndarray, f: float) -> dict:
    """Template outcomes and MFE/MAE exceedance at the costs each sample actually paid."""
    T = len(TEMPLATES)
    out = {'p_tpl': np.full((days.n, T, len(TPL_CLASSES)), np.nan),
           'p_mfe': np.full((days.n, 2, len(MFE_AT)), np.nan),
           'p_mae': np.full((days.n, 2, len(MAE_AT)), np.nan)}
    for ti in range(T):
        out['p_tpl'][:, ti] = multiclass(days, known, lab['tpl'][:, ti].astype(np.int64),
                                         len(TPL_CLASSES), f, TPL_CLASSES)
    for si, side in enumerate(('buy', 'sell')):
        mfe, mae = lab[f'{side}_mfe'].astype(np.float64), lab[f'{side}_mae'].astype(np.float64)
        for xi, x in enumerate(MFE_AT):
            out['p_mfe'][:, si, xi] = binary(days, known, (mfe >= x) * 1.0, f)
        for xi, x in enumerate(MAE_AT):
            out['p_mae'][:, si, xi] = binary(days, known, (mae >= x) * 1.0, f)
    return out


def _hist(days: Days, known: np.ndarray, x: np.ndarray, f: float) -> np.ndarray:
    """Running histogram of x per day (everything finite counts; clipped into range)."""
    ok = np.isfinite(x)
    k = np.where(ok, known, days.n)
    bins = np.clip(np.floor(np.where(ok, x, 0.0) / BIN).astype(np.int64), 0, NBINS - 1)
    return running(_add(days, k, None, (NBINS,), (bins,)), f)


def trade(days: Days, lab: dict, start_ms: int, half_life_days=None) -> dict:
    """
    Climatology for the trade labels, REPLAYED AT TODAY'S COST.

    The spread is known when a trade is decided, and it moves the odds a lot:
    on XAUUSD 5m it fell from ~0.10 ATR (2018) to ~0.02 ATR (2026) as gold's
    ATR grew and the spread did not, and P(target first) for a 1:1 trade rose
    from ~40% to ~48% on both sides. So the baseline for a trade decided at
    cost c counts every historical path as it would have played AT cost c:
      templates   from tpl_cf, the outcome replayed at each COST_LEVELS level,
                  interpolated to the row's cost (pick_tpl)
      MFE / MAE   from the raw excursion histograms, read at thresholds moved
                  by the row's cost and slippage (pick_excursion)
    Matching on cost instead would also match on the volatility spikes that
    made cost look cheap in the early years - tried, and it miscalibrated 15m.

    The tables at the costs each sample actually paid are kept too ('p_tpl',
    'p_mfe', 'p_mae') - the 'pooled' reference in the scorecard.
    """
    f = decay_factor(half_life_days)
    use = lab['t'] >= start_ms
    known = np.where(use, days.known_from(lab['resolve_ms']), days.n)
    out = _trade_tables(days, lab, known, f)
    T, K = len(TEMPLATES), len(COST_LEVELS)
    out['p_tpl_cf'] = np.full((days.n, K, T, len(TPL_CLASSES)), np.nan)
    for ti in range(T):
        for ci in range(K):
            out['p_tpl_cf'][:, ci, ti] = multiclass(
                days, known, lab['tpl_cf'][:, ti, ci].astype(np.int64), len(TPL_CLASSES), f,
                TPL_CLASSES)
    for side in ('up', 'dn'):
        h = _hist(days, known, lab[f'raw_{side}'].astype(np.float64), f)
        out[f'h_{side}'] = h
        out[f'c_{side}'] = np.cumsum(h, axis=1)
    out.update(counts(days, known, f))
    return out


def pick_tpl(tables: dict, day_idx: np.ndarray, cost: np.ndarray) -> np.ndarray:
    """(N, T, 3): each row's template odds replayed at its own cost (linear between levels)."""
    lv = np.asarray(COST_LEVELS, dtype=np.float64)
    c = np.clip(np.asarray(cost, dtype=np.float64), lv[0], lv[-1])
    hi = np.clip(np.searchsorted(lv, c, 'right'), 1, lv.size - 1)
    lo = hi - 1
    w = ((c - lv[lo]) / (lv[hi] - lv[lo]))[:, None, None]
    P = tables['p_tpl_cf']
    return P[day_idx, lo] * (1.0 - w) + P[day_idx, hi] * w


def survival(tables: dict, side: str, day_idx: np.ndarray, thr: np.ndarray) -> np.ndarray:
    """P(raw excursion >= thr) per row, from that row's day histogram (linear within a bin)."""
    h, c = tables[f'h_{side}'], tables[f'c_{side}']
    total = c[day_idx, -1]
    t = np.clip(np.asarray(thr, dtype=np.float64), 0.0, NBINS * BIN - 1e-9)
    b = np.floor(t / BIN).astype(np.int64)
    frac = t / BIN - b
    below = np.where(b > 0, c[day_idx, np.maximum(b - 1, 0)], 0.0) + frac * h[day_idx, b]
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(total > 0, 1.0 - below / total, np.nan)


def pick_excursion(tables: dict, day_idx: np.ndarray, cost: np.ndarray, slip: np.ndarray):
    """
    P(MFE >= x) and P(MAE >= x) for buys and sells at each row's own cost:
    arrays (N, 2, len(MFE_AT)) and (N, 2, len(MAE_AT)), side 0 = buy.

    With c the spread and s the slippage in ATR, a buy enters c + s above the
    bid, a sell s below it and exits on the ask (c above the bid), so
        buy  MFE = up - c - s    buy  MAE = down + c + s
        sell MFE = down - c - s  sell MAE = up + c + s
    where up / down are the raw bid excursions from the entry bid.
    """
    cs = np.asarray(cost, dtype=np.float64) + np.asarray(slip, dtype=np.float64)
    n = day_idx.size
    mfe = np.empty((n, 2, len(MFE_AT)))
    mae = np.empty((n, 2, len(MAE_AT)))
    for xi, x in enumerate(MFE_AT):
        mfe[:, 0, xi] = survival(tables, 'up', day_idx, x + cs)
        mfe[:, 1, xi] = survival(tables, 'dn', day_idx, x + cs)
    for xi, x in enumerate(MAE_AT):
        mae[:, 0, xi] = survival(tables, 'dn', day_idx, x - cs)
        mae[:, 1, xi] = survival(tables, 'up', day_idx, x - cs)
    return mfe, mae


def state_transition(days: Days, lab: dict, state_now: np.ndarray, start_ms: int,
                     clim: np.ndarray, k_prior: float = 50.0, half_life_days=None) -> np.ndarray:
    """
    REFERENCE forecaster, not a product: P(state at the horizon | state now),
    from resolved samples, shrunk towards the climatology with k_prior
    pseudo-counts. Regime persistence is a known effect, so the scorecard
    should show it as skill - a check that the scorecard can see skill at all.
    Returns (days, 5 current states, 5 future states).
    """
    f = decay_factor(half_life_days)
    use = lab['complete'] & (lab['t'] >= start_ms) & (state_now >= 0) & (lab['state_h'] >= 0)
    known = np.where(use, days.known_from(lab['resolve_ms']), days.n)
    cnt = running(_add(days, known, None, (N_STATES, N_STATES),
                       (np.maximum(state_now, 0).astype(np.int64),
                        np.maximum(lab['state_h'], 0).astype(np.int64))), f)
    prior = np.nan_to_num(clim)[:, None, :] * k_prior
    tot = cnt.sum(axis=2, keepdims=True)
    return (cnt + prior) / (tot + k_prior)


__all__ = ['DAY', 'BIN', 'NBINS', 'QS', 'N_STATES', 'TPL_CLASSES', 'Days', 'decay_factor',
           'running', 'counts', 'binary', 'multiclass', 'quantiles', 'market', 'trade', 'pick_tpl',
           'survival', 'pick_excursion', 'state_transition']
