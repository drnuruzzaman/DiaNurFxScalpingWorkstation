"""
server/forecast/cond_model.py - T2 regime, T3 direction, T4 trade odds (Phase C).

Each is a LIFT table over its baseline: the forecast is the baseline plus how
much outcomes in similar conditions differed from what the baseline said for
them at the time, shrunk towards the parent condition and finally towards
"no lift". The keys were chosen on 2018-2023 only (fit 2018-21, checked
2022-23, before any scoring year was looked at):

    regime     regime now -> x momentum over the horizon -> x volatility state
               (the current regime alone: +9.7% Brier on 5m, +15% on 15m)
    direction  regime now -> x momentum -> x leg / pullback position
               (no key moved direction by more than 0.1% - expected to stay
               at the baseline, and labelled so if it does)
    trade      volatility state -> x session -> x regime, per template, over
               the baseline replayed at the trade's own cost (vol and session
               moved target-first by ~0.3-0.6% on 5m)

k and the half-life are tuned walk-forward on 2020-2023 by Brier score; the
batch then scores 2024-2026 and a promotion gate decides what the lab shows.
"""
from __future__ import annotations

import numpy as np

from . import HORIZON, TEMPLATES, TRAIN_START, ms_of
from . import baseline as bl
from .tables import Months, lift_table

MODEL_VERSION = 'c1'
K_GRID = (3, 10, 30, 100, 300)
HALF_LIVES = (None, 24, 12)
TUNE = ('2020-01-01', '2024-01-01')
P_MIN = 0.003

REGIMES = ('uptrend', 'downtrend', 'range', 'transition', 'squeeze', 'unknown')
MOMS = ('falling hard', 'falling', 'flat', 'rising', 'rising hard')
LEGPULL = ('up leg, near its high', 'up leg, 0.5-1 ATR back', 'up leg, 1+ ATR back',
           'down leg, near its low', 'down leg, 0.5-1 ATR back', 'down leg, 1+ ATR back', 'no leg')
VOLS = ('very low vol', 'low vol', 'normal vol', 'high vol', 'extreme vol', 'vol unknown')
SESSIONS = ('daily break', 'Sydney', 'Tokyo', 'London', 'New York', 'London/NY overlap')


# --------------------------------------------------------------------------- #
# keys                                                                        #
# --------------------------------------------------------------------------- #
def _regime(state):
    s = np.asarray(state, dtype=np.int64)
    return np.where((s < 0) | (s > 4), 5, s)


def _mom(mom_h):
    m = np.asarray(mom_h, dtype=np.float64)
    return np.digitize(np.where(np.isfinite(m), m, 0.0), [-1.5, -0.5, 0.5, 1.5]).astype(np.int64)


def _legpull(leg_dir, leg_pull):
    d = np.asarray(leg_dir, dtype=np.int64)
    p = np.asarray(leg_pull, dtype=np.float64)
    b = np.digitize(np.where(np.isfinite(p), p, 0.0), [0.5, 1.0])
    return np.where(d == 0, 6, np.where(d > 0, 0, 3) + b).astype(np.int64)


def _vol(vol):
    v = np.asarray(vol, dtype=np.int64)
    return np.where((v < 0) | (v > 4), 5, v)


# name -> (parts, sizes, part names)
SPECS = {
    'direction': (('regime', 'mom', 'legpull'), (6, 5, 7), (REGIMES, MOMS, LEGPULL)),
    'state': (('regime', 'mom', 'vol'), (6, 5, 6), (REGIMES, MOMS, VOLS)),
    'trade': (('vol', 'session', 'regime'), (6, 6, 6), (VOLS, SESSIONS, REGIMES)),
}


def parts(F: dict) -> dict:
    """The key parts for every row of a feature table."""
    return {'regime': _regime(F['state']), 'mom': _mom(F['mom_h']),
            'legpull': _legpull(F['leg_dir'], F['leg_pull']), 'vol': _vol(F['vol']),
            'session': np.asarray(F['session'], dtype=np.int64)}


def cells(name: str, P: dict) -> np.ndarray:
    keys, sizes, _ = SPECS[name]
    c = P[keys[0]]
    for k, s in zip(keys[1:], sizes[1:]):
        c = c * s + P[k]
    return c.astype(np.int64)


def lineage(name: str) -> np.ndarray:
    _, sizes, _ = SPECS[name]
    C = int(np.prod(sizes))
    c = np.arange(C)
    rows = [np.zeros_like(c)]
    for i in range(1, len(sizes) + 1):
        rows.append(c // int(np.prod(sizes[i:])) if i < len(sizes) else c)
    return np.stack(rows)


def describe(name: str, cell: int) -> list:
    """The cell's conditions, coarse to fine, in words."""
    keys, sizes, names = SPECS[name]
    out, c = [], int(cell)
    vals = []
    for s in reversed(sizes):
        vals.append(c % s)
        c //= s
    vals.reverse()
    for k, v, nm in zip(keys, vals, names):
        out.append(nm[v])
    return out


# --------------------------------------------------------------------------- #
# fitting                                                                     #
# --------------------------------------------------------------------------- #
def _apply(pbase: np.ndarray, lift: np.ndarray) -> np.ndarray:
    """Baseline + lift, kept a probability (renormalised for several classes)."""
    p = np.clip(pbase + lift, P_MIN, 1 - P_MIN)
    if p.ndim >= 2 and p.shape[-1] > 1:
        p = p / p.sum(axis=-1, keepdims=True)
    return p


def _brier(p, y) -> float:
    return float(np.mean(np.sum((p - y) ** 2, axis=-1))) if p.ndim == 2 else float(np.mean((p - y) ** 2))


def _fit(months, known, lin, fine, resid, H, row_m, tune_mask, pbase_rows, y_rows, log, tag):
    """Tune (k, half-life) on the tuning rows, return the chosen settings and loss table."""
    losses = {}
    for hl in HALF_LIVES:
        for k in K_GRID:
            t = lift_table(months, known, lin, fine, resid, k, H, hl)
            lift = t['lift'][row_m[tune_mask], fine[tune_mask]]
            if resid.ndim == 1:
                lift = lift[:, 0]
            losses[(k, hl)] = _brier(_apply(pbase_rows, lift), y_rows)
    best = min(losses, key=losses.get)
    if log:
        base = _brier(np.clip(pbase_rows, P_MIN, 1 - P_MIN), y_rows)
        log(f'    {tag}: tuned on 2020-2023 -> k {best[0]}, half-life {best[1] or "none"}  '
            f'(Brier {losses[best]:.5f} vs baseline {base:.5f})')
    return best, {f'k={k} hl={hl or "none"}': v for (k, hl), v in losses.items()}


def fit_market(F: dict, M: dict, B: dict, days: bl.Days, tf: str, start_ms: int = None,
               log=None) -> dict:
    """Direction (every step) and regime-at-the-horizon lift tables for one timeframe."""
    start_ms = ms_of(TRAIN_START) if start_ms is None else start_ms
    H = HORIZON[tf]
    months = Months(int(F['close_ms'][0]), int(F['close_ms'][-1]))
    P = parts(F)
    di = days.of(F['close_ms'])
    row_m = months.of(F['close_ms'])
    use = M['complete'] & (F['t'] >= start_ms) & np.isfinite(F['atr'])
    known = np.where(use, months.known_from(M['resolve_ms']), months.n)
    tune = use & (F['close_ms'] >= ms_of(TUNE[0])) & (F['close_ms'] < ms_of(TUNE[1]))
    out = {'version': MODEL_VERSION, 'tf': tf, 'horizon': H, 'months': months.starts}
    # ---- direction, per step
    dfine = cells('direction', P)
    dlin = lineage('direction')
    r = M['ret'].astype(np.float64)
    y_all = np.where(np.isfinite(r), (r > 0) * 1.0, np.nan)
    pb_all = B['p_up'][di]                                     # (N, H)
    resid_H = y_all[:, H - 1] - pb_all[:, H - 1]
    tm = tune & np.isfinite(resid_H)
    (dk, dhl), dtune = _fit(months, known, dlin, dfine, resid_H, H, row_m, tm, pb_all[tm, H - 1],
                            y_all[tm, H - 1], log, f'{tf} direction')
    dl = np.zeros((months.n, dlin.shape[1], H), dtype=np.float32)
    dneff = dw = None
    for k in range(H):
        t = lift_table(months, known, dlin, dfine, y_all[:, k] - pb_all[:, k], dk, H, dhl)
        dl[:, :, k] = t['lift'][..., 0]
        if k == H - 1:
            dneff, dw = t['neff'], t['w']
    out.update({'dir_lift': dl, 'dir_neff': dneff, 'dir_w': dw, 'dir_k': dk, 'dir_hl': dhl,
                'dir_tuning': dtune})
    # ---- regime at the horizon
    sfine = cells('state', P)
    slin = lineage('state')
    sh = M['state_h'].astype(np.int64)
    onehot = np.zeros((sh.size, bl.N_STATES))
    okh = sh >= 0
    onehot[np.nonzero(okh)[0], sh[okh]] = 1.0
    pbs = B['p_state'][di]
    sres = np.where(okh[:, None], onehot - pbs, np.nan)
    tm = tune & okh & np.isfinite(pbs).all(axis=1)
    (sk, shl), stune = _fit(months, known, slin, sfine, sres, H, row_m, tm, pbs[tm], onehot[tm],
                            log, f'{tf} regime')
    t = lift_table(months, known, slin, sfine, sres, sk, H, shl)
    out.update({'st_lift': t['lift'], 'st_neff': t['neff'], 'st_w': t['w'], 'st_k': sk,
                'st_hl': shl, 'st_tuning': stune})
    return out


def fit_trade(F: dict, T: dict, BT: dict, days: bl.Days, tf: str, start_ms: int = None,
              log=None) -> dict:
    """Per-template target / stop / neither lift over the cost-replayed baseline."""
    start_ms = ms_of(TRAIN_START) if start_ms is None else start_ms
    horizon = 72 if tf == '5m' else 24
    pos = np.searchsorted(F['t'], T['t'])
    P = {k: v[pos] for k, v in parts(F).items()}
    months = Months(int(T['close_ms'][0]), int(T['close_ms'][-1]))
    row_m = months.of(T['close_ms'])
    di = days.of(T['close_ms'])
    use = T['t'] >= start_ms
    known = np.where(use, months.known_from(T['resolve_ms']), months.n)
    fine = cells('trade', P)
    lin = lineage('trade')
    pb = bl.pick_tpl(BT, di, T['spread_atr'])                    # (N, templates, 3)
    tune = use & (T['close_ms'] >= ms_of(TUNE[0])) & (T['close_ms'] < ms_of(TUNE[1]))
    nT = len(TEMPLATES)
    onehot = np.zeros((T['t'].size, nT, 3))
    for ti in range(nT):
        y = T['tpl'][:, ti]
        cls = np.select([y == 1, y == -1], [0, 1], 2)
        onehot[np.arange(y.size), ti, cls] = 1.0
    resid = onehot - pb
    # Tune on the two 1 ATR / 1R templates together.
    tm = tune & np.isfinite(pb[:, 0]).all(axis=1) & np.isfinite(pb[:, 6]).all(axis=1)
    both = np.concatenate([resid[:, 0], resid[:, 6]], axis=0)
    (k_, hl_), ttune = _fit(
        months, np.concatenate([known, known]), lin, np.concatenate([fine, fine]), both, horizon,
        np.concatenate([row_m, row_m]), np.concatenate([tm, tm]),
        np.concatenate([pb[tm, 0], pb[tm, 6]]), np.concatenate([onehot[tm, 0], onehot[tm, 6]]),
        log, f'{tf} trade')
    lift = np.zeros((months.n, lin.shape[1], nT, 3), dtype=np.float32)
    neff = w = None
    for ti in range(nT):
        t = lift_table(months, known, lin, fine, resid[:, ti], k_, horizon, hl_)
        lift[:, :, ti] = t['lift']
        if ti == 0:
            neff, w = t['neff'], t['w']
    return {'tr_months': months.starts, 'tr_lift': lift, 'tr_neff': neff, 'tr_w': w,
            'tr_k': k_, 'tr_hl': hl_, 'tr_tuning': ttune}


def month_of(starts: np.ndarray, close_ms) -> np.ndarray:
    return np.clip(np.searchsorted(starts, np.asarray(close_ms, dtype=np.int64), 'right') - 1,
                   0, starts.size - 1)


def interval(p, neff, k_prior, z: float = 1.645):
    """90% interval for a table probability: binomial error on the cell's own
    effective samples plus the prior's pseudo-samples."""
    n = np.maximum(np.asarray(neff, dtype=np.float64) + float(k_prior), 1.0)
    se = np.sqrt(np.clip(p * (1 - p), 1e-9, None) / n)
    return np.clip(p - z * se, 0, 1), np.clip(p + z * se, 0, 1)


__all__ = ['MODEL_VERSION', 'SPECS', 'REGIMES', 'MOMS', 'LEGPULL', 'VOLS', 'SESSIONS', 'parts',
           'cells', 'lineage', 'describe', 'fit_market', 'fit_trade', 'month_of', 'interval', '_apply']
