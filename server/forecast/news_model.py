"""
server/forecast/news_model.py - Phase E: the range model with the news spelled out.

The Phase B range model (range_model.py) knows one thing about news: is a
major US release due inside the horizon, yes or no. That treats NFP like
PCE, ignores the weekly claims print, and ignores WHEN in the horizon the
release lands - a CPI five minutes out puts its whole reaction inside a
5m x 12 horizon, one fifty-five minutes out puts almost none of it there.

This model keeps everything else identical - the broker hour, the volatility
bucket, the monthly resolved-only tables, the shrinkage to parents, k tuned
on 2020-2023 only - and replaces the yes/no with a news STATE, still built
from scheduled times only (a calendar is known weeks ahead; the print never):

    0  no release          nothing due inside the horizon, none just out
    1  minor ahead         only a claims / JOLTS print inside the horizon
    2  second tier, early  PPI / GDP / PCE / retail sales in the first half
    3  second tier, late   ... in the second half
    4  NFP / CPI, early    in the first half of the horizon
    5  NFP / CPI, late     in the second half
    6  FOMC ahead          the 14:00 statement (and the press conference)
    7  just after          a major release printed within the last horizon,
                           nothing major ahead - the reaction still running

    levels   all rows -> hour -> hour x major-ahead (Phase B's own split)
             -> hour x state -> hour x state x vol

So every news cell shrinks first to the Phase B cell it refines. Where the
refinement has no support the forecast IS the Phase B forecast; where it has,
the evidence decides. It is promoted only by the Phase B gate, run against
what the lab shows today (tools/forecast_news.py).
"""
from __future__ import annotations

import numpy as np

from ..config import TF_SECONDS
from . import HORIZON
from . import news as nw
from . import range_model as rm

MODEL_VERSION = 'n1'
STATES = ('no release', 'minor release ahead', 'second-tier release early',
          'second-tier release late', 'NFP/CPI early', 'NFP/CPI late', 'FOMC ahead',
          'just after a major release')
SHORT = ('none', 'minor', 'tier2 early', 'tier2 late', 'NFP/CPI early', 'NFP/CPI late',
         'FOMC', 'after')
N_STATES = len(STATES)
N_CELLS = 24 * N_STATES * 5
TOP = ('NFP', 'CPI')
TIER2 = ('PPI', 'GDP', 'PCE', 'RETAIL')
LEVEL_NAMES = ('all rows', 'hour', 'hour x major release ahead', 'hour x news state',
               'hour x news state x volatility')


def horizon_min(tf: str) -> float:
    return float(HORIZON[tf] * TF_SECONDS[tf] / 60)


def state(F: dict, tf: str) -> np.ndarray:
    """The news state of every row (module doc), from the scheduled-time features."""
    hmin = horizon_min(tf)
    kind = np.asarray(F['news_next_kind'], dtype=np.int64)
    nxt = np.asarray(F['news_next_min'], dtype=np.float64)
    prv = np.asarray(F['news_prev_min'], dtype=np.float64)
    ahead = np.asarray(F['news_in_h']) > 0
    minor = np.asarray(F['news_minor_in_h']) > 0
    code = {k: i for i, k in enumerate(nw.KINDS)}
    early = nxt <= hmin / 2
    top = np.isin(kind, [code[k] for k in TOP])
    tier2 = np.isin(kind, [code[k] for k in TIER2])
    fomc = kind == code['FOMC']
    s = np.zeros(kind.shape, dtype=np.int64)
    s[minor & ~ahead] = 1
    s[(prv <= hmin) & ~ahead & ~minor] = 7
    s[ahead & tier2 & early] = 2
    s[ahead & tier2 & ~early] = 3
    s[ahead & top & early] = 4
    s[ahead & top & ~early] = 5
    s[ahead & fomc] = 6
    return s


def cells(close_ms, st, rr_w, tf: str) -> np.ndarray:
    """The finest cell of each row: (hour x N_STATES + state) x 5 + vol bucket."""
    hour = (np.asarray(close_ms, dtype=np.int64) // 3_600_000) % 24
    r = np.asarray(rr_w, dtype=np.float64)
    mid = float(np.median(rm.VOL_EDGES[tf]))
    vol = np.searchsorted(np.asarray(rm.VOL_EDGES[tf]), np.where(np.isfinite(r), r, mid), 'right')
    return ((hour * N_STATES + np.asarray(st, dtype=np.int64)) * 5 + vol).astype(np.int64)


def lineage() -> np.ndarray:
    """(levels, cells): each finest cell's cell at every level (module doc)."""
    c = np.arange(N_CELLS)
    hour, st = c // (N_STATES * 5), (c // 5) % N_STATES
    major = ((st >= 2) & (st <= 6)).astype(np.int64)        # Phase B's news flag
    return np.stack([np.zeros_like(c), hour, hour * 2 + major, c // 5, c])


def decode(cell: int) -> dict:
    cell = int(cell)
    st = (cell // 5) % N_STATES
    return {'hour': cell // (N_STATES * 5), 'state': st, 'state_name': STATES[st],
            'vol': cell % 5, 'vol_name': rm.VOL_NAMES[cell % 5]}


def fit(F: dict, M: dict, tf: str, log=None, **kw) -> dict:
    """The news model for one timeframe - range_model.fit on the news cells."""
    fine = cells(F['close_ms'], state(F, tf), F['rr_w'], tf)
    out = rm.fit(F, M, tf, log=log, fine=fine, lin=lineage(), n_cells=N_CELLS, **kw)
    out['version'] = MODEL_VERSION
    return out


def predict(model: dict, close_ms, st, rr_w) -> tuple:
    """(up_q (N, H, 3), dn_q (N, H, 3), cell (N,), month index (N,)) by lookup."""
    starts = model['months']
    m = np.clip(np.searchsorted(starts, np.asarray(close_ms, dtype=np.int64), 'right') - 1,
                0, starts.size - 1)
    cell = cells(close_ms, st, rr_w, model['tf'])
    q = model['pred'][m, cell]
    return q[:, 0], q[:, 1], cell, m


def quiet_twin(cell) -> np.ndarray:
    """The same hour and volatility bucket with no release - what the news is measured against."""
    c = np.asarray(cell, dtype=np.int64)
    return (c // (N_STATES * 5)) * N_STATES * 5 + c % 5


__all__ = ['MODEL_VERSION', 'STATES', 'SHORT', 'N_STATES', 'N_CELLS', 'LEVEL_NAMES', 'state',
           'cells', 'lineage', 'decode', 'fit', 'predict', 'quiet_twin', 'horizon_min']
