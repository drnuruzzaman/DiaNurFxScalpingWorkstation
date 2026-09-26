"""
server/forecast/store.py - what the lab reads: forecasts by lookup.

Built by tools/forecast_build.py after the reads and labels, one file per
forecast timeframe under runs/forecast/cache/<symbol>/<tf>/forecast.npz:

    rows      every closed bar since TRAIN_START: time, close, ATR, its range
              cell, and the context the panel explains it with (regime, next
              release, volatility against its week)
    model     the range model's predictions per (month, cell, side, step,
              quantile), with each cell's n_eff and shrinkage weight
    baseline  the climatology per broker day: range quantiles per step, P(up)
              per step, P(state at the horizon)
    barrier   (5m, 15m) monthly stop/target grid odds - barrier.py

A forecast for a bar is then a few array lookups - no model runs in the lab,
and nothing is computed from bars after the one on screen: every table was
built from outcomes resolved before its month or day began.

The symbol's latest batch run's promotion gate (ui_summary.json) says, per
timeframe, whether the model beat the baseline in every scored year; when it
did not, the lab draws the baseline cone and says so.
"""
from __future__ import annotations

import json
import threading

import numpy as np

from ..config import TF_SECONDS
from ..datafeed import load_disk
from . import (CACHE_DIR, FEATURE_VERSION, HORIZON, LABEL_VERSION, SYMBOL, TRADE_TFS,
               TRAIN_START, engine_hash, latest_run, ms_of)
from . import baseline as bl
from . import barrier as br
from . import cond_model as cm
from . import features as ft
from . import labels as lb
from . import range_model as rm
from .news import HISTORY, KINDS
from .tables import Months
from .timebase import SESSION_NAMES

STORE_VERSION = 's3'
_LOCK = threading.Lock()
_CACHE: dict = {}


def _paths(symbol: str, tf: str):
    d = CACHE_DIR / symbol / tf
    return d / 'forecast.npz', d / 'forecast.json'


def _key(symbol: str, tf: str) -> dict:
    try:
        news_m = int(HISTORY.stat().st_mtime)
    except OSError:
        news_m = 0
    return {'v': STORE_VERSION, 'model': rm.MODEL_VERSION, 'cond': cm.MODEL_VERSION,
            'feature': FEATURE_VERSION,
            'label': LABEL_VERSION, 'engine': engine_hash(), 'symbol': symbol, 'tf': tf,
            'files': lb._files(symbol, tf), 'news': news_m,
            'm1': lb._files(symbol, '1m') if tf in TRADE_TFS else None}


def is_fresh(symbol: str, tf: str) -> bool:
    npz, meta = _paths(symbol, tf)
    if not (npz.exists() and meta.exists()):
        return False
    try:
        return json.loads(meta.read_text(encoding='utf-8')).get('key') == _key(symbol, tf)
    except (OSError, ValueError):
        return False


def build(symbol: str, tf: str, log=print) -> dict:
    """Fit and save everything the lab needs for one forecast timeframe."""
    start = ms_of(TRAIN_START)
    F = ft.assemble(symbol, tf)
    M = lb.market(symbol, tf)
    if not np.array_equal(F['t'], M['t']):
        raise RuntimeError(f'{tf}: feature rows and label rows differ')
    model = rm.fit(F, M, tf, start, log=log)
    days = bl.Days(int(F['close_ms'][0]), int(F['close_ms'][-1]))
    B = bl.market(days, M, start)
    bars = load_disk(symbol, tf, lb._years(symbol, tf))
    close = bars.c[np.searchsorted(bars.t.astype(np.int64), F['t'])]
    out = {
        't': F['t'], 'close_ms': F['close_ms'], 'close': close.astype(np.float64),
        'atr': F['atr'].astype(np.float32),
        'cell': rm.cells(F['close_ms'], F['news_in_h'], F['rr_w'], tf).astype(np.int16),
        'state': F['state'].astype(np.int8), 'vol': F['vol'].astype(np.int8),
        'session': F['session'].astype(np.int8),
        'rr_w': F['rr_w'].astype(np.float32),
        'news_next_min': F['news_next_min'].astype(np.float32),
        'news_next_kind': F['news_next_kind'].astype(np.int8),
        'm_starts': model['months'], 'pred': model['pred'],
        'neff': model['neff'], 'w': model['w'],
        'd0': np.int64(days.d0), 'b_up': B['up_q'].astype(np.float32),
        'b_dn': B['dn_q'].astype(np.float32), 'b_pup': B['p_up'].astype(np.float32),
        'b_state': B['p_state'].astype(np.float32), 'b_n': B['n'].astype(np.float32),
    }
    # ---- Phase C: direction and regime lift tables, and what the analog
    # list shows (outcomes, known only once resolved - the lookup checks that).
    mk = cm.fit_market(F, M, B, days, tf, start, log=log)
    P = cm.parts(F)
    H = HORIZON[tf]
    r = M['ret'][:, H - 1]
    out.update({
        'c_months': mk['months'], 'dir_lift': mk['dir_lift'], 'dir_neff': mk['dir_neff'],
        'dir_w': mk['dir_w'], 'st_lift': mk['st_lift'], 'st_neff': mk['st_neff'],
        'st_w': mk['st_w'],
        'dir_cell': cm.cells('direction', P).astype(np.int16),
        'st_cell': cm.cells('state', P).astype(np.int16),
        'tr_cell': cm.cells('trade', P).astype(np.int16),
        'an_key': (cm.cells('state', P) * 6 + P['session']).astype(np.int16),
        'resolve_ms': M['resolve_ms'].astype(np.int64),
        'ret_up': np.where(M['complete'] & np.isfinite(r), (r > 0).astype(np.int8), -1).astype(np.int8),
        'up_h': M['up'][:, H - 1].astype(np.float32), 'dn_h': M['dn'][:, H - 1].astype(np.float32),
        'state_h': M['state_h'].astype(np.int8),
    })
    cond_info = {'dir_k': mk['dir_k'], 'dir_hl': mk['dir_hl'], 'st_k': mk['st_k'],
                 'st_hl': mk['st_hl']}
    if tf in TRADE_TFS:
        T = lb.trade(symbol, tf)
        tm = Months(int(F['close_ms'][0]), int(F['close_ms'][-1]))
        bt = br.tables(T, tm, start)
        out.update({'bar_m_starts': tm.starts, 'bar_U': bt['U'], 'bar_D': bt['D'],
                    'bar_n': bt['n']})
        dT = bl.Days(int(T['close_ms'][0]), int(T['close_ms'][-1]))
        BT = bl.trade(dT, T, start)
        tr = cm.fit_trade(F, T, BT, dT, tf, start, log=log)
        pos = np.searchsorted(F['t'], T['t'])
        tb = np.full(F['t'].size, -2, dtype=np.int8)
        ts_ = np.full(F['t'].size, -2, dtype=np.int8)
        tb[pos], ts_[pos] = T['tpl'][:, 0], T['tpl'][:, 6]       # 1 ATR / 1R, buy and sell
        out.update({'tr_months': tr['tr_months'], 'tr_lift': tr['tr_lift'],
                    'tr_neff': tr['tr_neff'], 'tr_w': tr['tr_w'], 'tpl_buy': tb, 'tpl_sell': ts_})
        cond_info.update({'tr_k': tr['tr_k'], 'tr_hl': tr['tr_hl']})
    npz, meta = _paths(symbol, tf)
    npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(npz, **out)
    info = {'key': _key(symbol, tf), 'horizon': HORIZON[tf], 'k_prior': model['k_prior'],
            'half_life_months': model['half_life_months'], 'tuning': model['tuning'],
            'vol_edges': model['vol_edges'], 'rows': int(F['t'].size), 'cond': cond_info,
            'first_ms': int(F['close_ms'][0]), 'last_ms': int(F['close_ms'][-1])}
    meta.write_text(json.dumps(info, indent=1), encoding='utf-8')
    with _LOCK:
        _CACHE.pop((symbol, tf), None)
    return info


def latest_summary(symbol: str = SYMBOL) -> dict:
    """The symbol's newest batch run's ui_summary.json ({} when it has none)."""
    run = latest_run(symbol)
    if run is None:
        return {}
    try:
        doc = json.loads((run / 'ui_summary.json').read_text(encoding='utf-8'))
        doc['run_id'] = run.name
        doc.setdefault('symbol', symbol)
        return doc
    except (OSError, ValueError):
        return {}


class Store:
    """One timeframe's forecasts, loaded once per process."""

    def __init__(self, symbol: str, tf: str, arrays: dict, info: dict, stale: bool = False):
        self.symbol, self.tf, self.a, self.info = symbol, tf, arrays, info
        # Built before the latest data or engine change: every forecast it holds
        # is still what was knowable at its bar, it just may not reach the newest
        # bars. The lab says so rather than refusing.
        self.stale = stale
        self.H = HORIZON[tf]
        self.tf_ms = TF_SECONDS[tf] * 1000
        self.t = arrays['t']
        self.close_ms = arrays['close_ms']

    # ------------------------------------------------------------- lookup
    def row_at(self, bar_t: int):
        """Row of the bar that OPENED at bar_t, or None."""
        i = int(np.searchsorted(self.t, int(bar_t)))
        return i if i < self.t.size and int(self.t[i]) == int(bar_t) else None

    def row_closed_by(self, close_ms: int):
        """Row of the last bar CLOSED at or before close_ms, or None."""
        i = int(np.searchsorted(self.close_ms, int(close_ms), 'right')) - 1
        return i if i >= 0 else None

    def _month(self, close_ms: int) -> int:
        s = self.a['m_starts']
        return int(np.clip(np.searchsorted(s, int(close_ms), 'right') - 1, 0, s.size - 1))

    def _day(self, close_ms: int) -> int:
        d = (int(close_ms) - int(self.a['d0'])) // bl.DAY
        return int(np.clip(d, 0, self.a['b_up'].shape[0] - 1))

    def cone(self, i: int) -> dict | None:
        """The range forecast at row i: model and baseline quantiles, per step."""
        a = self.a
        c_ms = int(self.close_ms[i])
        m, d, cell = self._month(c_ms), self._day(c_ms), int(a['cell'][i])
        q = a['pred'][m, cell]                               # (2, H, 3)
        base_up, base_dn = a['b_up'][d], a['b_dn'][d]
        if not (np.isfinite(q).all() and np.isfinite(base_up).all()):
            return None
        info = rm.decode(cell)
        nk = int(a['news_next_kind'][i])
        return {
            'up': np.round(q[0], 4).tolist(), 'dn': np.round(q[1], 4).tolist(),
            'base_up': np.round(base_up, 4).tolist(), 'base_dn': np.round(base_dn, 4).tolist(),
            'ratio': float((q[0, -1, 1] + q[1, -1, 1]) / max(base_up[-1, 1] + base_dn[-1, 1], 1e-9)),
            'cell': dict(info, neff=float(a['neff'][m, cell]), w=float(a['w'][m, cell]),
                         news_next_min=float(a['news_next_min'][i]),
                         news_next=KINDS[nk] if 0 <= nk < len(KINDS) else None,
                         session=SESSION_NAMES[int(a['session'][i])],
                         rr_w=float(a['rr_w'][i]) if np.isfinite(a['rr_w'][i]) else None),
        }

    # ------------------------------------------------------------- Phase C
    def _cmonth(self, close_ms: int, key: str = 'c_months') -> int:
        s = self.a[key]
        return int(np.clip(np.searchsorted(s, int(close_ms), 'right') - 1, 0, s.size - 1))

    def direction(self, i: int) -> dict | None:
        """P(close higher after k bars) - the lift table's and the baseline's, per step."""
        a = self.a
        if 'dir_lift' not in a:
            return None
        c_ms = int(self.close_ms[i])
        m, d, cell = self._cmonth(c_ms), self._day(c_ms), int(a['dir_cell'][i])
        base = a['b_pup'][d].astype(np.float64)
        p = cm._apply(base, a['dir_lift'][m, cell].astype(np.float64))
        k = float(self.info.get('cond', {}).get('dir_k') or 30)
        lo, hi = cm.interval(p[-1], a['dir_neff'][m, cell], k)
        return {'p': np.round(p, 4).tolist(), 'base': np.round(base, 4).tolist(),
                'lo': float(lo), 'hi': float(hi), 'neff': float(a['dir_neff'][m, cell]),
                'w': float(a['dir_w'][m, cell]), 'why': cm.describe('direction', cell)}

    def regime(self, i: int) -> dict | None:
        """P(the chart's regime label after H bars) - lift table and baseline."""
        a = self.a
        if 'st_lift' not in a:
            return None
        c_ms = int(self.close_ms[i])
        m, d, cell = self._cmonth(c_ms), self._day(c_ms), int(a['st_cell'][i])
        base = a['b_state'][d].astype(np.float64)
        p = cm._apply(base, a['st_lift'][m, cell].astype(np.float64))
        k = float(self.info.get('cond', {}).get('st_k') or 10)
        top = int(np.argmax(p))
        lo, hi = cm.interval(p[top], a['st_neff'][m, cell], k)
        return {'p': np.round(p, 4).tolist(), 'base': np.round(base, 4).tolist(), 'top': top,
                'lo': float(lo), 'hi': float(hi), 'neff': float(a['st_neff'][m, cell]),
                'w': float(a['st_w'][m, cell]), 'why': cm.describe('state', cell)}

    def trade_lift(self, i: int, side: str, stop_atr: float, target_atr: float) -> dict | None:
        """The conditions' lift for the template nearest this trade (target, stop, neither)."""
        a = self.a
        if 'tr_lift' not in a:
            return None
        sl = 1.0 if stop_atr < 1.25 else 1.5
        r = target_atr / max(stop_atr, 1e-9)
        tp = min((1.0, 1.5, 2.0), key=lambda x: abs(x - r))
        from . import TEMPLATES
        ti = TEMPLATES.index((side, sl, tp))
        m = self._cmonth(int(self.close_ms[i]), 'tr_months')
        cell = int(a['tr_cell'][i])
        return {'lift': a['tr_lift'][m, cell, ti].astype(float).tolist(),
                'template': f'{side} {sl:g} ATR stop, {tp:g}R', 'neff': float(a['tr_neff'][m, cell]),
                'why': cm.describe('trade', cell)}

    def analogs(self, i: int, limit: int = 12) -> dict | None:
        """
        Similar past moments: bars in the same regime x momentum x volatility x
        session, whose outcome had RESOLVED by this bar's close. Neighbouring
        bars share their outcome, so matches are thinned to one per horizon -
        the count that is left is the honest sample size.
        """
        a = self.a
        if 'an_key' not in a:
            return None
        c_ms = int(self.close_ms[i])
        key = int(a['an_key'][i])
        cand = np.nonzero((a['an_key'][:i] == key) & (a['resolve_ms'][:i] <= c_ms)
                          & (a['ret_up'][:i] >= 0))[0]
        picked, last = [], None
        for j in cand[::-1]:                                     # newest first
            if last is None or last - j >= self.H:
                picked.append(int(j))
                last = j
        if not picked:
            return {'n': int(cand.size), 'n_eff': 0, 'rows': []}
        pk = np.array(picked)
        out = {'n': int(cand.size), 'n_eff': int(pk.size),
               'p_up': float(a['ret_up'][pk].mean()),
               'up_med': float(np.median(a['up_h'][pk])), 'dn_med': float(np.median(a['dn_h'][pk])),
               'first_ms': int(self.t[pk[-1]]), 'why': cm.describe('state', int(a['st_cell'][i]))
               + [SESSION_NAMES[int(a['session'][i])]]}
        if 'tpl_buy' in a:
            tb, ts_ = a['tpl_buy'][pk], a['tpl_sell'][pk]
            out['tp_buy'] = float(np.mean(tb[tb > -2] == 1)) if (tb > -2).any() else None
            out['tp_sell'] = float(np.mean(ts_[ts_ > -2] == 1)) if (ts_ > -2).any() else None
        out['rows'] = [{'t': int(self.t[j]), 'close_ms': int(self.close_ms[j]),
                        'up': round(float(a['up_h'][j]), 2), 'dn': round(float(a['dn_h'][j]), 2),
                        'ret_up': int(a['ret_up'][j]), 'state_h': int(a['state_h'][j])}
                       for j in picked[:limit]]
        return out

    def baseline_odds(self, i: int) -> dict:
        a = self.a
        d = self._day(int(self.close_ms[i]))
        return {'p_up': np.round(a['b_pup'][d], 4).tolist(),
                'p_state': np.round(a['b_state'][d], 4).tolist(),
                'state_now': int(a['state'][i])}

    def barrier(self, close_ms: int, side: str, stop_atr: float, target_atr: float,
                cost_atr: float, slip_atr: float) -> dict | None:
        if 'bar_U' not in self.a:
            return None
        s = self.a['bar_m_starts']
        m = int(np.clip(np.searchsorted(s, int(close_ms), 'right') - 1, 0, s.size - 1))
        tabs = {'U': self.a['bar_U'], 'D': self.a['bar_D']}
        return br.odds(tabs, m, side, stop_atr, target_atr, cost_atr, slip_atr)

    def at(self, bar_t: int) -> dict:
        """The full forecast payload for the bar that opened at bar_t."""
        i = self.row_at(bar_t)
        if i is None:
            return {'available': False, 'tf': self.tf,
                    'reason': 'no forecast for this bar - it is outside the built period '
                              '(run tools/forecast_build.py)'}
        c = self.cone(i)
        if c is None:
            return {'available': False, 'tf': self.tf,
                    'reason': 'not enough resolved history before this bar yet'}
        return dict({'available': True, 'tf': self.tf, 'horizon': self.H, 'row': i,
                     't': int(self.t[i]), 'close_ms': int(self.close_ms[i]),
                     'close': float(self.a['close'][i]), 'atr': float(self.a['atr'][i]),
                     'direction': self.direction(i), 'regime': self.regime(i),
                     'analogs': self.analogs(i)},
                    **c, **self.baseline_odds(i))

    def horizon_row(self, i: int) -> dict | None:
        """The compact MTF row: horizon-step quantiles only."""
        c = self.cone(i)
        if c is None:
            return None
        dr = self.direction(i)
        return {'tf': self.tf, 'horizon': self.H, 't': int(self.t[i]),
                'close': float(self.a['close'][i]), 'atr': float(self.a['atr'][i]),
                'up': c['up'][-1], 'dn': c['dn'][-1], 'base_up': c['base_up'][-1],
                'base_dn': c['base_dn'][-1], 'ratio': c['ratio'],
                'p_up': self.baseline_odds(i)['p_up'][-1],
                'p_up_model': dr['p'][-1] if dr else None}


def load(symbol: str, tf: str, allow_stale: bool = False) -> Store | None:
    """
    The store for a timeframe (cached per process). None when it was never
    built - or when it is stale and allow_stale is False (the batch insists on
    fresh; the lab accepts stale and flags it).
    """
    npz, meta = _paths(symbol, tf)
    with _LOCK:
        hit = _CACHE.get((symbol, tf))
    # A rebuilt store (the monthly update) replaces the file: reload it rather
    # than keep serving the old tables for the life of the process.
    try:
        mtime = npz.stat().st_mtime
    except OSError:
        mtime = None
    if hit is not None and getattr(hit, 'mtime', None) == mtime and (allow_stale or not hit.stale):
        return hit
    if not (npz.exists() and meta.exists()):
        return None
    fresh = is_fresh(symbol, tf)
    if not fresh and not allow_stale:
        return None
    with np.load(npz) as z:
        arrays = {k: z[k] for k in z.files}
    st = Store(symbol, tf, arrays, json.loads(meta.read_text(encoding='utf-8')), stale=not fresh)
    st.mtime = mtime
    with _LOCK:
        _CACHE[(symbol, tf)] = st
    return st


__all__ = ['STORE_VERSION', 'is_fresh', 'build', 'latest_summary', 'Store', 'load']
