"""
server/forecast/score.py - how good was a forecast, and was it better than the baseline.

Proper scoring rules only - a forecast cannot improve them by hedging:

    probabilities (direction, state, trade outcome, MFE/MAE exceedance)
        Brier score, log loss, expected calibration error (ECE), AUC,
        and a reliability table (said X%, happened Y%)
    quantiles (range)
        pinball (quantile) loss per level, the hit rate below each quantile
        (20 / 50 / 80% is calibrated), and P20-P80 coverage (60% is right)

Skill is always against the baseline scored on the SAME rows:
    skill = 1 - score(forecast) / score(baseline)
> 0 better than the baseline, 0 no better, < 0 worse.

Labels overlap - a 12-bar horizon means neighbouring rows share 11 bars of
outcome - so row counts overstate the evidence. Every result carries n_eff
(rows / horizon, the overlap-adjusted count) and a day-block bootstrap
interval for the skill, resampling whole days so the overlap stays inside
each block.
"""
from __future__ import annotations

import numpy as np

EPS = 1e-6


# --------------------------------------------------------------------------- #
# probabilities                                                               #
# --------------------------------------------------------------------------- #
def brier(p, y) -> np.ndarray:
    """Per-row Brier score. p (N,) with y 0/1, or p (N, C) with y class index."""
    p = np.asarray(p, dtype=np.float64)
    if p.ndim == 1:
        return (p - np.asarray(y, dtype=np.float64)) ** 2
    onehot = np.zeros_like(p)
    onehot[np.arange(p.shape[0]), np.asarray(y, dtype=np.int64)] = 1.0
    return ((p - onehot) ** 2).sum(axis=1)


def log_loss(p, y) -> np.ndarray:
    """Per-row log loss (natural log), probabilities clipped to [EPS, 1-EPS]."""
    p = np.asarray(p, dtype=np.float64)
    if p.ndim == 1:
        q = np.clip(p, EPS, 1 - EPS)
        yy = np.asarray(y, dtype=np.float64)
        return -(yy * np.log(q) + (1 - yy) * np.log(1 - q))
    q = np.clip(p[np.arange(p.shape[0]), np.asarray(y, dtype=np.int64)], EPS, 1.0)
    return -np.log(q)


def reliability(p, y, bins: int = 10) -> list:
    """[(bin_lo, bin_hi, n, mean_p, freq)] for a binary forecast."""
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    rows = []
    for b in range(bins):
        m = idx == b
        if m.any():
            rows.append((float(edges[b]), float(edges[b + 1]), int(m.sum()),
                         float(p[m].mean()), float(y[m].mean())))
    return rows


def ece(p, y, bins: int = 10) -> float:
    """Expected calibration error. Multiclass: the mean over classes (classwise)."""
    p = np.asarray(p, dtype=np.float64)
    if p.ndim == 2:
        yy = np.asarray(y, dtype=np.int64)
        return float(np.mean([ece(p[:, c], (yy == c) * 1.0, bins) for c in range(p.shape[1])]))
    n = p.size
    if n == 0:
        return float('nan')
    return float(sum(k * abs(mp - fr) for _, _, k, mp, fr in reliability(p, y, bins)) / n)


def auc(p, y) -> float:
    """Area under the ROC curve (Mann-Whitney, ties averaged). 0.5 = no ranking."""
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64) > 0.5
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 == 0 or n0 == 0:
        return float('nan')
    order = np.argsort(p, kind='mergesort')
    _, first, cnt = np.unique(p[order], return_index=True, return_counts=True)
    ranks = np.empty(p.size)
    ranks[order] = np.repeat(first + (cnt - 1) / 2.0 + 1.0, cnt)     # ties share a rank
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


# --------------------------------------------------------------------------- #
# quantiles                                                                   #
# --------------------------------------------------------------------------- #
def pinball(q, y, tau: float) -> np.ndarray:
    """Per-row quantile loss of prediction q at level tau."""
    d = np.asarray(y, dtype=np.float64) - np.asarray(q, dtype=np.float64)
    return np.maximum(tau * d, (tau - 1.0) * d)


def coverage(lo, hi, y) -> float:
    y = np.asarray(y, dtype=np.float64)
    return float(np.mean((y >= lo) & (y <= hi))) if y.size else float('nan')


# --------------------------------------------------------------------------- #
# skill with an honest interval                                               #
# --------------------------------------------------------------------------- #
def skill(model_loss: np.ndarray, base_loss: np.ndarray) -> float:
    b = float(np.mean(base_loss))
    return float(1.0 - np.mean(model_loss) / b) if b > 0 else float('nan')


def skill_ci(model_loss, base_loss, day, reps: int = 400, level: float = 0.90,
             seed: int = 7) -> tuple:
    """Day-block bootstrap interval for skill: whole days are resampled."""
    model_loss = np.asarray(model_loss, dtype=np.float64)
    base_loss = np.asarray(base_loss, dtype=np.float64)
    if model_loss.size == 0:
        return float('nan'), float('nan')
    _, inv = np.unique(np.asarray(day), return_inverse=True)
    nd = int(inv.max()) + 1
    sm = np.bincount(inv, weights=model_loss, minlength=nd)
    sb = np.bincount(inv, weights=base_loss, minlength=nd)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, nd, size=(reps, nd))
    num, den = sm[draws].sum(axis=1), sb[draws].sum(axis=1)
    with np.errstate(invalid='ignore', divide='ignore'):
        sk = 1.0 - num / den
    a = (1.0 - level) / 2.0
    return float(np.nanquantile(sk, a)), float(np.nanquantile(sk, 1 - a))


def n_eff(n_rows: int, horizon_bars: int) -> int:
    """Overlap-adjusted sample size: rows whose outcomes share bars count once."""
    return int(n_rows // max(1, horizon_bars))


__all__ = ['brier', 'log_loss', 'reliability', 'ece', 'auc', 'pinball', 'coverage', 'skill',
           'skill_ci', 'n_eff']
