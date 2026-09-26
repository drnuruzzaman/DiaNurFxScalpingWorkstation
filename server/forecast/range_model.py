"""
server/forecast/range_model.py - T1, the range forecast (Phase B).

How far price is likely to travel UP and DOWN over the next k bars, as
P20 / P50 / P80 in ATR - a conditional table over the three things that
measurably moved the forward range in 2018-2023 (never on the years it is
scored on):

    hour   the broker-time hour of the close. Broker time is New York + 7h, so
           the 08:30 ET releases and the London open sit on the same broker hour
           all year, where in UTC they shift with the clock changes. On 5m the
           range ran 0.73x the norm in the late New York hours and 1.50x at the
           open.
    news   a tier-one US release scheduled inside the horizon: 2.2x on 5m,
           1.3x on 4h.
    vol    how compressed ATR is against its weekly level (mean true range over
           5 days / ATR, in quintile buckets set on 2018-2023): compressed
           volatility tends to expand - 0.78x to 1.27x.

    levels   all rows -> hour -> hour x news -> hour x news x vol
             each shrunk to its parent with n_eff / (n_eff + k)

k is tuned walk-forward on 2020-2023 (each month's forecast from its own
monthly snapshot), by pinball loss at the horizon; 2024-2026 stay untouched
for the scorecard. The model is a table of predictions per (month, cell), so
a forecast is a lookup and every number can be traced to its cell.
"""
from __future__ import annotations

import numpy as np

from . import HORIZON, TRAIN_START, ms_of
from . import score as sc
from .tables import Months, quantile_table

QS = (0.2, 0.5, 0.8)
# rr_w quintile edges per timeframe, from 2018-2023 feature values only.
VOL_EDGES = {'5m': (0.78, 1.01, 1.23, 1.51), '15m': (0.80, 1.00, 1.17, 1.36),
             '1h': (0.88, 0.98, 1.07, 1.17), '4h': (0.95, 0.98, 1.02, 1.05)}
# Bucket 0 = lowest TR/ATR, i.e. ATR stretched ABOVE its weekly level (ranges
# in ATR tend to shrink back); bucket 4 = ATR compressed well below it (they
# tend to expand).
VOL_NAMES = ('ATR well above its week', 'ATR above its week', 'ATR near its week',
             'ATR below its week', 'ATR well below its week')
LEVEL_NAMES = ('all rows', 'hour', 'hour x news', 'hour x news x volatility')
N_CELLS = 24 * 2 * 5
K_GRID = (3, 10, 30, 100, 300)
HALF_LIVES = (None, 24, 12)           # months; None = the plain expanding window
TUNE = ('2020-01-01', '2024-01-01')
MODEL_VERSION = 'r1'


def cells(close_ms, news_in_h, rr_w, tf: str) -> np.ndarray:
    """The finest cell of each row: (hour x 2 + news) x 5 + vol bucket."""
    hour = (np.asarray(close_ms, dtype=np.int64) // 3_600_000) % 24
    news = (np.asarray(news_in_h) > 0).astype(np.int64)
    r = np.asarray(rr_w, dtype=np.float64)
    mid = float(np.median(VOL_EDGES[tf]))                   # missing -> middle bucket
    vol = np.searchsorted(np.asarray(VOL_EDGES[tf]), np.where(np.isfinite(r), r, mid), 'right')
    return ((hour * 2 + news) * 5 + vol).astype(np.int64)


def lineage() -> np.ndarray:
    """(levels, cells): each finest cell's cell at every level."""
    c = np.arange(N_CELLS)
    return np.stack([np.zeros_like(c), c // 10, c // 5, c])


def decode(cell: int) -> dict:
    cell = int(cell)
    return {'hour': cell // 10, 'news': (cell // 5) % 2, 'vol': cell % 5,
            'vol_name': VOL_NAMES[cell % 5]}


def _loss(q_up, q_dn, x_up, x_dn) -> float:
    tot, n = 0.0, 0
    for q, x in ((q_up, x_up), (q_dn, x_dn)):
        ok = np.isfinite(x) & np.isfinite(q).all(axis=1)
        for i, tau in enumerate(QS):
            tot += float(sc.pinball(q[ok, i], x[ok], tau).sum())
        n += int(ok.sum()) * len(QS)
    return tot / max(n, 1)


def fit(F: dict, M: dict, tf: str, start_ms: int = None, k_prior: float = None,
        half_life: float = 'tune', log=None, fine: np.ndarray = None, lin: np.ndarray = None,
        n_cells: int = N_CELLS, steps=None) -> dict:
    """
    The range model for one timeframe: predictions for every (month, cell,
    side, step k, quantile), from outcomes resolved before each month.
    F and M are the feature rows and market labels (same rows).

    fine / lin / n_cells swap in another cell layout with the same fitting
    (Phase E's news model); steps limits the fitted steps (e.g. [H - 1] to
    score the horizon only). The defaults are this module's own model.
    """
    start_ms = ms_of(TRAIN_START) if start_ms is None else start_ms
    H = HORIZON[tf]
    months = Months(int(F['close_ms'][0]), int(F['close_ms'][-1]))
    fine = cells(F['close_ms'], F['news_in_h'], F['rr_w'], tf) if fine is None else fine
    use = M['complete'] & (F['t'] >= start_ms) & np.isfinite(F['atr'])
    known = np.where(use, months.known_from(M['resolve_ms']), months.n)
    lin = lineage() if lin is None else lin
    row_m = months.of(F['close_ms'])
    tuning = {}
    if k_prior is None or half_life == 'tune':
        tr = (F['close_ms'] >= ms_of(TUNE[0])) & (F['close_ms'] < ms_of(TUNE[1])) & use
        ks = K_GRID if k_prior is None else (k_prior,)
        hls = HALF_LIVES if half_life == 'tune' else (half_life,)
        up_h, dn_h = M['up'][:, H - 1].astype(np.float64), M['dn'][:, H - 1].astype(np.float64)
        for hl in hls:
            for k in ks:
                qu = quantile_table(months, known, lin, fine, up_h, k, H, QS, hl)['q']
                qd = quantile_table(months, known, lin, fine, dn_h, k, H, QS, hl)['q']
                tuning[(k, hl)] = _loss(qu[row_m[tr], fine[tr]], qd[row_m[tr], fine[tr]],
                                        M['up'][tr, H - 1], M['dn'][tr, H - 1])
        k_prior, half_life = min(tuning, key=tuning.get)
        if log:
            log(f'    {tf}: tuned on 2020-2023 -> k {k_prior}, half-life '
                f'{half_life or "none"}  (best loss {tuning[(k_prior, half_life)]:.4f}, '
                f'worst {max(tuning.values()):.4f})')
    pred = np.full((months.n, n_cells, 2, H, len(QS)), np.nan, dtype=np.float32)
    neff = w = None
    for si, side in enumerate(('up', 'dn')):
        for k in (range(H) if steps is None else steps):
            t = quantile_table(months, known, lin, fine, M[side][:, k].astype(np.float64),
                               k_prior, H, QS, half_life)
            pred[:, :, si, k] = t['q']
            if si == 0 and k == H - 1:
                neff, w = t['neff'], t['w']
    return {'version': MODEL_VERSION, 'tf': tf, 'horizon': H, 'months': months.starts,
            'pred': pred, 'neff': neff, 'w': w, 'k_prior': float(k_prior),
            'half_life_months': half_life,
            'tuning': {f'k={k} hl={hl or "none"}': v for (k, hl), v in tuning.items()},
            'vol_edges': list(VOL_EDGES[tf])}


def predict(model: dict, close_ms, news_in_h, rr_w) -> tuple:
    """(up_q (N, H, 3), dn_q (N, H, 3), cell (N,), month index (N,)) by lookup."""
    starts = model['months']
    m = np.clip(np.searchsorted(starts, np.asarray(close_ms, dtype=np.int64), 'right') - 1,
                0, starts.size - 1)
    cell = cells(close_ms, news_in_h, rr_w, model['tf'])
    q = model['pred'][m, cell]
    return q[:, 0], q[:, 1], cell, m


__all__ = ['QS', 'VOL_EDGES', 'VOL_NAMES', 'LEVEL_NAMES', 'N_CELLS', 'K_GRID', 'MODEL_VERSION',
           'cells', 'lineage', 'decode', 'fit', 'predict']
