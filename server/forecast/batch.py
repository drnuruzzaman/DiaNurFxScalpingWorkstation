"""
server/forecast/batch.py - the whole-period run that turns forecasts into evidence.

An interactive replay produces a few hundred forecasts; evidence needs every
closed bar of 2024, 2025 and 2026. This runs the forecasters over all of it,
with no UI, and scores each one against the baseline on the SAME rows:

    per forecaster x output x step k x year          Brier / log loss / ECE /
                                                     AUC, or pinball / hit
                                                     rates / coverage, skill
                                                     with a day-block interval
    per condition (regime, session, volatility,      where the unconditional
    MTF alignment, leg, news)                        baseline is wrong, and by
                                                     how much, year by year

Phase A forecasters - all of them baselines or checks, none a product:
    climatology       the empirical baseline itself (expanding from 2018);
                      for trade outputs every path is replayed at the row's
                      own cost (the spread in ATR is known at decision time)
    recency           the same with a 365-day half-life (decision 1's option)
    state_transition  P(state later | state now), a REFERENCE check: regime
                      persistence is real, so a working scorecard must show it
    pooled            trade outputs: the baseline at the costs history paid,
                      a reference for what ignoring the spread costs
    range_v1          Phase B: the range model (hour x news x volatility
                      tables, shrunk), scored against the climatology; its
                      promotion gate decides whether the lab draws its cone
    cond_v1           Phase C: direction, regime and trade-odds LIFT tables
                      over their baselines, each with its own gate

Writes runs/forecast/batch/<run_id>/: manifest.json (versions, data keys),
scorecard.parquet (every aggregate), forecasts_<tf>.parquet (every forecast
row with its baseline and outcome) and report.txt.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone

import numpy as np

from . import (BASELINE_VERSION, BATCH_DIR, COST_LEVELS, FEATURE_VERSION, HORIZON, LABEL_VERSION,
               MAE_AT, MFE_AT, ROOT, SYMBOL, TEMPLATES, TFS, TRADE_TFS, TRAIN_START, engine_hash,
               ms_of)
from . import baseline as bl
from . import cond_model as cm
from . import features as ft
from . import labels as lb
from . import range_model as rm
from . import reads
from . import score as sc
from . import store
from .timebase import SESSION_NAMES

MIN_N = 5000                 # a baseline day needs this many resolved samples to be scored
EVAL_YEARS = (2024, 2025, 2026)
HALF_LIFE = 365


# --------------------------------------------------------------------------- #
# conditions                                                                  #
# --------------------------------------------------------------------------- #
def _mtf_bucket(s: np.ndarray) -> np.ndarray:
    return np.select([s <= -60, s <= -20, s < 20, s < 60], [0, 1, 2, 3], 4)


MTF_NAMES = ('strong down', 'down', 'conflicted', 'up', 'strong up')


def groups(F: dict) -> dict:
    """Condition -> (codes per row, names). Codes < 0 are 'unknown'."""
    return {
        'regime': (F['state'].astype(np.int64), reads.STATES),
        'session': (F['session'].astype(np.int64), SESSION_NAMES),
        'vol': (F['vol'].astype(np.int64), reads.VOLS),
        'mtf': (_mtf_bucket(F['mtf_score']).astype(np.int64), MTF_NAMES),
        'leg': (np.where(F['leg_dir'] > 0, 0, np.where(F['leg_dir'] < 0, 1, 2)).astype(np.int64),
                ('up leg', 'down leg', 'no leg')),
        'news': (F['news_in_h'].astype(np.int64), ('no release due', 'release due')),
    }


# --------------------------------------------------------------------------- #
# one scorecard row                                                           #
# --------------------------------------------------------------------------- #
def prob_row(meta: dict, p, y, pb, day, horizon: int, reps: int, with_ci: bool) -> dict:
    """Scores for a probability forecast (binary p (N,) or multiclass (N, C))."""
    p, pb = np.asarray(p, dtype=np.float64), np.asarray(pb, dtype=np.float64)
    n = int(len(y))
    row = dict(meta, n=n, n_eff=sc.n_eff(n, horizon))
    if n == 0:
        return row
    bm, bb = sc.brier(p, y), sc.brier(pb, y)
    row.update(brier=float(bm.mean()), brier_base=float(bb.mean()),
               logloss=float(sc.log_loss(p, y).mean()),
               logloss_base=float(sc.log_loss(pb, y).mean()),
               ece=sc.ece(p, y), ece_base=sc.ece(pb, y), skill=sc.skill(bm, bb))
    if p.ndim == 1:
        row.update(mean_p=float(p.mean()), mean_p_base=float(pb.mean()),
                   freq=float(np.mean(y)), auc=sc.auc(p, y))
    else:
        yy = np.asarray(y, dtype=np.int64)
        for c in range(p.shape[1]):                 # said vs happened, class by class
            row[f'mean_p_c{c}'] = float(p[:, c].mean())
            row[f'freq_c{c}'] = float(np.mean(yy == c))
    if with_ci:
        row['ci_lo'], row['ci_hi'] = sc.skill_ci(bm, bb, day, reps)
    return row


def quant_row(meta: dict, q, y, qb, day, horizon: int, reps: int, with_ci: bool) -> dict:
    """Scores for a P20/P50/P80 forecast of a non-negative excursion."""
    n = int(len(y))
    row = dict(meta, n=n, n_eff=sc.n_eff(n, horizon))
    if n == 0:
        return row
    lm = sum(sc.pinball(q[:, i], y, tau) for i, tau in enumerate(bl.QS)) / len(bl.QS)
    lb_ = sum(sc.pinball(qb[:, i], y, tau) for i, tau in enumerate(bl.QS)) / len(bl.QS)
    row.update(pinball=float(lm.mean()), pinball_base=float(lb_.mean()), skill=sc.skill(lm, lb_),
               hit20=float(np.mean(y <= q[:, 0])), hit50=float(np.mean(y <= q[:, 1])),
               hit80=float(np.mean(y <= q[:, 2])), cov=sc.coverage(q[:, 0], q[:, 2], y),
               cov_base=sc.coverage(qb[:, 0], qb[:, 2], y), q50=float(np.median(q[:, 1])))
    if with_ci:
        row['ci_lo'], row['ci_hi'] = sc.skill_ci(lm, lb_, day, reps)
    return row


def diff_ci(p, y, day, reps: int = 400, seed: int = 11) -> tuple:
    """Day-block bootstrap interval for (realised frequency - mean forecast)."""
    _, inv = np.unique(np.asarray(day), return_inverse=True)
    nd = int(inv.max()) + 1 if inv.size else 0
    if nd < 2:
        return float('nan'), float('nan')
    d = np.asarray(y, dtype=np.float64) - np.asarray(p, dtype=np.float64)
    s = np.bincount(inv, weights=d, minlength=nd)
    c = np.bincount(inv, minlength=nd).astype(np.float64)
    draws = np.random.default_rng(seed).integers(0, nd, size=(reps, nd))
    v = s[draws].sum(axis=1) / c[draws].sum(axis=1)
    return float(np.quantile(v, 0.05)), float(np.quantile(v, 0.95))


# --------------------------------------------------------------------------- #
# the run                                                                     #
# --------------------------------------------------------------------------- #
def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    raise TypeError(type(o))


def _git() -> str:
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:                                                  # noqa: BLE001
        return ''


class Run:
    def __init__(self, symbol: str = SYMBOL, tfs=TFS, trade_tfs=TRADE_TFS,
                 eval_years=EVAL_YEARS, half_life: int = HALF_LIFE, reps: int = 400,
                 run_id: str = None, log=print):
        self.symbol, self.tfs, self.trade_tfs = symbol, tuple(tfs), tuple(trade_tfs)
        self.eval_years = tuple(int(y) for y in eval_years)
        self.half_life, self.reps, self.log = half_life, reps, log
        self.run_id = run_id or datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        self.dir = BATCH_DIR / self.run_id
        self.start = ms_of(TRAIN_START)
        self.rows: list = []
        self.cond: list = []
        self.gates: dict = {}
        self.cgates: dict = {}
        self.info: dict = {'tfs': {}, 'trade': {}}

    # ------------------------------------------------------------- helpers
    def _years(self, close_ms):
        return ft.years_of(close_ms)

    def _emit_prob(self, meta, p, y, pb, day, yrs, horizon, with_ci=True):
        for yr in list(self.eval_years) + ['all']:
            m = (yrs == yr) if yr != 'all' else np.isin(yrs, self.eval_years)
            self.rows.append(prob_row(dict(meta, year=str(yr)), p[m], y[m], pb[m], day[m],
                                      horizon, self.reps, with_ci))

    def _emit_quant(self, meta, q, y, qb, day, yrs, horizon, with_ci=True):
        for yr in list(self.eval_years) + ['all']:
            m = (yrs == yr) if yr != 'all' else np.isin(yrs, self.eval_years)
            self.rows.append(quant_row(dict(meta, year=str(yr)), q[m], y[m], qb[m], day[m],
                                       horizon, self.reps, with_ci))

    def _conditional(self, meta, p, y, day, yrs, G):
        """
        Per condition: the baseline's MISS (realised - baseline) and its LIFT -
        the miss minus that year's overall miss. A drift that moves every group
        at once (a bull year, cheaper costs) shows in the miss of every group;
        only the lift is what the condition itself adds.
        """
        d = np.asarray(y, dtype=np.float64) - np.asarray(p, dtype=np.float64)
        yr_miss = np.zeros(d.size)
        for yr in self.eval_years:
            m = yrs == yr
            if m.any():
                yr_miss[m] = d[m].mean()
        for col, (codes, names) in G.items():
            for gi, name in enumerate(names):
                g = codes == gi
                per_year = {}
                for yr in self.eval_years:
                    m = g & (yrs == yr)
                    if m.sum() >= 50:
                        per_year[str(yr)] = float(np.mean(d[m] - yr_miss[m]))
                m = g & np.isin(yrs, self.eval_years)
                if m.sum() < 50:
                    continue
                lo, hi = diff_ci(p[m] + yr_miss[m], y[m], day[m], self.reps)
                self.cond.append(dict(meta, kind='lift', cond=col, group=name, n=int(m.sum()),
                                      n_eff=sc.n_eff(int(m.sum()), meta['horizon']),
                                      mean_p=float(np.mean(p[m])), freq=float(np.mean(y[m])),
                                      miss=float(np.mean(d[m])),
                                      lift=float(np.mean(d[m] - yr_miss[m])),
                                      ci_lo=lo, ci_hi=hi, per_year=per_year))

    def _conditional_skill(self, meta, lm, lbase, day, yrs, G):
        """Where a forecast beats its baseline: skill per condition, pooled and by year."""
        for col, (codes, names) in G.items():
            for gi, name in enumerate(names):
                g = codes == gi
                m = g & np.isin(yrs, self.eval_years)
                if m.sum() < 100:
                    continue
                per_year = {}
                for yr in self.eval_years:
                    my = g & (yrs == yr)
                    if my.sum() >= 50:
                        per_year[str(yr)] = sc.skill(lm[my], lbase[my])
                lo, hi = sc.skill_ci(lm[m], lbase[m], day[m], self.reps)
                self.cond.append(dict(meta, kind='skill', cond=col, group=name, n=int(m.sum()),
                                      n_eff=sc.n_eff(int(m.sum()), meta['horizon']),
                                      skill=sc.skill(lm[m], lbase[m]), ci_lo=lo, ci_hi=hi,
                                      per_year=per_year))

    def _range_model(self, tf, F, M, B, idx, d, day, y_, G):
        """The Phase B range model, scored against the climatology, and its gate."""
        H = HORIZON[tf]
        st = store.load(self.symbol, tf)
        if st is None:
            raise RuntimeError(f'{tf}: the range model is not built or is stale - run '
                               'tools/forecast_build.py --only store')
        mdl = {'months': st.a['m_starts'], 'pred': st.a['pred'], 'tf': tf}
        up_m, dn_m, _, _ = rm.predict(mdl, F['close_ms'][idx], F['news_in_h'][idx],
                                      F['rr_w'][idx])
        base = {'tf': tf, 'horizon': H, 'kind': 'market', 'forecaster': 'range_v1'}
        gate = {'k_prior': st.info.get('k_prior'),
                'half_life_months': st.info.get('half_life_months'),
                'years': {}, 'decay': {'up': [], 'dn': []}}
        for side, Q in (('up', up_m), ('dn', dn_m)):
            for k in range(H):
                x = M[side][idx, k].astype(np.float64)
                ok = np.isfinite(x) & np.isfinite(Q[:, k]).all(axis=1)
                qb = B[f'{side}_q'][d[ok], k]
                meta = dict(base, output=f'range_{side}', k=k + 1)
                self._emit_quant(meta, Q[ok, k], x[ok], qb, day[ok], y_[ok], H)
                emitted = self.rows[-(len(self.eval_years) + 1):]
                gate['decay'][side].append(emitted[-1].get('skill'))
                if k == H - 1:
                    for r in emitted[:-1]:
                        gate['years'].setdefault(r['year'], {})[side] = {
                            'skill': r.get('skill'), 'lo': r.get('ci_lo'), 'hi': r.get('ci_hi'),
                            'cov': r.get('cov'), 'cov_base': r.get('cov_base')}
                    lm = sum(sc.pinball(Q[ok, k, i], x[ok], tau)
                             for i, tau in enumerate(rm.QS)) / len(rm.QS)
                    lbase = sum(sc.pinball(qb[:, i], x[ok], tau)
                                for i, tau in enumerate(rm.QS)) / len(rm.QS)
                    self._conditional_skill(meta, lm, lbase, day[ok], y_[ok],
                                            {c: (v[0][ok], v[1]) for c, v in G.items()})
        # Promoted only if it beat the baseline in EVERY scored year, both sides,
        # with the interval clear of zero, and stayed calibrated (P20-P80 ~60%).
        checks = []
        for yr in self.eval_years:
            for side in ('up', 'dn'):
                r = gate['years'].get(str(yr), {}).get(side) or {}
                checks.append((r.get('lo') or -1) > 0 and 0.55 <= (r.get('cov') or 0) <= 0.65)
        gate['promoted'] = bool(checks) and all(checks)
        self.gates[tf] = gate

    def _gate_prob(self, rows_by_year: dict, ece_max: float = 0.03) -> dict:
        """Promoted only if the skill interval clears zero in EVERY year and it stays calibrated."""
        years = {}
        ok = []
        for yr in self.eval_years:
            r = rows_by_year.get(str(yr)) or {}
            years[str(yr)] = {'skill': r.get('skill'), 'lo': r.get('ci_lo'), 'hi': r.get('ci_hi'),
                              'ece': r.get('ece'), 'ece_base': r.get('ece_base')}
            ok.append((r.get('ci_lo') or -1) > 0 and (r.get('ece') if r.get('ece') is not None
                                                      else 1) <= ece_max)
        return {'promoted': bool(ok) and all(ok), 'years': years}

    def _cond_market(self, tf, F, M, B, idx, d, day, y_, G):
        """Phase C: direction and regime lift tables, scored against their baselines."""
        H = HORIZON[tf]
        st = store.load(self.symbol, tf)
        a = st.a
        P = cm.parts({k: F[k][idx] for k in ('state', 'mom_h', 'leg_dir', 'leg_pull', 'vol',
                                             'session')})
        m = cm.month_of(a['c_months'], F['close_ms'][idx])
        dcell, scell = cm.cells('direction', P), cm.cells('state', P)
        base = {'tf': tf, 'horizon': H, 'kind': 'market', 'forecaster': 'cond_v1'}
        gate = {'k': st.info.get('cond', {}).get('dir_k'), 'hl': st.info.get('cond', {}).get('dir_hl')}
        decay = []
        for k in range(H):
            r = M['ret'][idx, k]
            ok = np.isfinite(r)
            y = (r[ok] > 0) * 1.0
            pb = B['p_up'][d[ok], k]
            pm = cm._apply(pb, a['dir_lift'][m[ok], dcell[ok], k].astype(np.float64))
            meta = dict(base, output='direction', k=k + 1)
            self._emit_prob(meta, pm, y, pb, day[ok], y_[ok], H)
            emitted = self.rows[-(len(self.eval_years) + 1):]
            decay.append(emitted[-1].get('skill'))
            if k == H - 1:
                gate.update(self._gate_prob({r_['year']: r_ for r_ in emitted[:-1]}))
                bm, bb = sc.brier(pm, y), sc.brier(pb, y)
                self._conditional_skill(meta, bm, bb, day[ok], y_[ok],
                                        {c: (v[0][ok], v[1]) for c, v in G.items()})
        gate['decay'] = decay
        sh = M['state_h'][idx]
        ok = sh >= 0
        Pb = B['p_state'][d[ok]]
        Pm = cm._apply(Pb, a['st_lift'][m[ok], scell[ok]].astype(np.float64))
        meta = dict(base, output='state', k=H)
        self._emit_prob(meta, Pm, sh[ok].astype(np.int64), Pb, day[ok], y_[ok], H)
        emitted = self.rows[-(len(self.eval_years) + 1):]
        sgate = dict(self._gate_prob({r_['year']: r_ for r_ in emitted[:-1]}),
                     k=st.info.get('cond', {}).get('st_k'), hl=st.info.get('cond', {}).get('st_hl'))
        bm, bb = sc.brier(Pm, sh[ok].astype(np.int64)), sc.brier(Pb, sh[ok].astype(np.int64))
        self._conditional_skill(meta, bm, bb, day[ok], y_[ok],
                                {c: (v[0][ok], v[1]) for c, v in G.items()})
        self.cgates.setdefault(tf, {}).update({'direction': gate, 'state': sgate})

    def _cond_trade(self, tf, F, T, BT, idx, d, day, y_, horizon, pos):
        """Phase C: template odds lift over the cost-replayed baseline."""
        st = store.load(self.symbol, tf)
        a = st.a
        P = cm.parts({k: F[k][pos[idx]] for k in ('state', 'mom_h', 'leg_dir', 'leg_pull', 'vol',
                                                  'session')})
        m = cm.month_of(a['tr_months'], T['close_ms'][idx])
        cell = cm.cells('trade', P)
        pb_all = bl.pick_tpl(BT, d, T['spread_atr'][idx])
        base = {'tf': tf, 'horizon': horizon, 'kind': 'trade', 'forecaster': 'cond_v1', 'k': 0}
        gates = {}
        for ti, (side, sl, tp) in enumerate(TEMPLATES):
            y = T['tpl'][idx, ti].astype(np.int64)
            cls = np.select([y == 1, y == -1], [0, 1], 2)
            Pb = pb_all[:, ti]
            Pm = cm._apply(Pb, a['tr_lift'][m, cell, ti].astype(np.float64))
            name = f'tpl_{side}_{sl:g}atr_{tp:g}r'
            self._emit_prob(dict(base, output=name), Pm, cls, Pb, day, y_, horizon)
            emitted = self.rows[-(len(self.eval_years) + 1):]
            gates[name] = self._gate_prob({r_['year']: r_ for r_ in emitted[:-1]})
        core = [f'tpl_{sd}_1atr_{t}r' for sd in ('buy', 'sell') for t in ('1', '2')]
        self.cgates.setdefault(tf, {})['trade'] = {
            'promoted': all(gates[n]['promoted'] for n in core), 'templates': gates,
            'k': st.info.get('cond', {}).get('tr_k'), 'hl': st.info.get('cond', {}).get('tr_hl')}

    # ------------------------------------------------------------- market
    def market_tf(self, tf: str, keep_rows: bool = True):
        t0 = time.perf_counter()
        H = HORIZON[tf]
        F = ft.assemble(self.symbol, tf)
        M = lb.market(self.symbol, tf)
        if not np.array_equal(F['t'], M['t']):
            raise RuntimeError(f'{tf}: feature rows and label rows differ')
        days = bl.Days(int(F['close_ms'][0]), int(F['close_ms'][-1]))
        B = bl.market(days, M, self.start)
        R = bl.market(days, M, self.start, self.half_life)
        di = days.of(F['close_ms'])
        yrs = self._years(F['close_ms'])
        ev = (M['complete'] & np.isfinite(F['atr']) & (B['n'][di] >= MIN_N)
              & np.isin(yrs, self.eval_years))
        idx = np.nonzero(ev)[0]
        d, y_ = di[idx], yrs[idx]
        day = F['day'][idx]
        G = {k: (v[0][idx], v[1]) for k, v in groups(F).items()}
        base = {'tf': tf, 'horizon': H, 'kind': 'market'}
        # ---- direction, every step (the decay curve)
        for k in range(H):
            r = M['ret'][idx, k]
            ok = np.isfinite(r)
            y = (r[ok] > 0) * 1.0
            pb = B['p_up'][d[ok], k]
            meta = dict(base, output='direction', k=k + 1)
            self._emit_prob(dict(meta, forecaster='climatology'), pb, y, pb, day[ok], y_[ok], H,
                            with_ci=False)
            self._emit_prob(dict(meta, forecaster='recency'), R['p_up'][d[ok], k], y, pb,
                            day[ok], y_[ok], H)
            if k == H - 1:
                self._conditional(dict(meta, forecaster='climatology'), pb, y, day[ok], y_[ok],
                                  {c: (v[0][ok], v[1]) for c, v in G.items()})
        # ---- state at the horizon
        s_h = M['state_h'][idx]
        ok = s_h >= 0
        yb = s_h[ok].astype(np.int64)
        Pb = B['p_state'][d[ok]]
        meta = dict(base, output='state', k=H)
        self._emit_prob(dict(meta, forecaster='climatology'), Pb, yb, Pb, day[ok], y_[ok], H,
                        with_ci=False)
        self._emit_prob(dict(meta, forecaster='recency'), R['p_state'][d[ok]], yb, Pb, day[ok],
                        y_[ok], H)
        ST = bl.state_transition(days, M, F['state'].astype(np.int64), self.start, B['p_state'])
        now = F['state'][idx][ok].astype(np.int64)
        Pst = np.where((now >= 0)[:, None], ST[d[ok], np.maximum(now, 0)], Pb)
        self._emit_prob(dict(meta, forecaster='state_transition'), Pst, yb, Pb, day[ok], y_[ok], H)
        # ---- range, every step
        for side in ('up', 'dn'):
            for k in range(H):
                x = M[side][idx, k].astype(np.float64)
                ok2 = np.isfinite(x)
                qb = B[f'{side}_q'][d[ok2], k]
                meta = dict(base, output=f'range_{side}', k=k + 1)
                self._emit_quant(dict(meta, forecaster='climatology'), qb, x[ok2], qb, day[ok2],
                                 y_[ok2], H, with_ci=False)
                self._emit_quant(dict(meta, forecaster='recency'), R[f'{side}_q'][d[ok2], k],
                                 x[ok2], qb, day[ok2], y_[ok2], H)
        self._range_model(tf, F, M, B, idx, d, day, y_, G)
        self._cond_market(tf, F, M, B, idx, d, day, y_, G)
        self.info['tfs'][tf] = {'rows': int(F['t'].size), 'scored': int(idx.size),
                                'first_ms': int(F['close_ms'][0]),
                                'last_ms': int(F['close_ms'][-1]),
                                'seconds': round(time.perf_counter() - t0, 1)}
        if keep_rows:
            self._write_rows(tf, F, M, B, R, idx, d)
        self.log(f'  market {tf:>3}: {idx.size:>7} forecasts scored '
                 f'({time.perf_counter() - t0:.0f}s)')

    def _write_rows(self, tf, F, M, B, R, idx, d):
        """Every scored forecast: its ID, time, baseline, recency forecast and outcome."""
        import pandas as pd
        H = HORIZON[tf]
        df = pd.DataFrame({
            'id': [f'{self.symbol}|{tf}|{int(c)}|{self.run_id}' for c in F['close_ms'][idx]],
            'close_ms': F['close_ms'][idx], 'close_utc': F['close_utc'][idx],
            'year': ft.years_of(F['close_ms'][idx]),
            'regime': F['state'][idx], 'session': F['session'][idx], 'vol': F['vol'][idx],
            'mtf_score': F['mtf_score'][idx], 'news_in_h': F['news_in_h'][idx],
            'base_p_up': B['p_up'][d, H - 1], 'recency_p_up': R['p_up'][d, H - 1],
            'ret_h': M['ret'][idx, H - 1], 'state_h': M['state_h'][idx],
            'base_up_q20': B['up_q'][d, H - 1, 0], 'base_up_q50': B['up_q'][d, H - 1, 1],
            'base_up_q80': B['up_q'][d, H - 1, 2], 'up_h': M['up'][idx, H - 1],
            'base_dn_q20': B['dn_q'][d, H - 1, 0], 'base_dn_q50': B['dn_q'][d, H - 1, 1],
            'base_dn_q80': B['dn_q'][d, H - 1, 2], 'dn_h': M['dn'][idx, H - 1],
            'base_n_eff': B['n_eff'][d],
        })
        for s in range(bl.N_STATES):
            df[f'base_p_state_{reads.STATES[s].lower()}'] = B['p_state'][d, s]
        self.dir.mkdir(parents=True, exist_ok=True)
        df.to_parquet(self.dir / f'forecasts_{tf}.parquet', compression='zstd', index=False)

    # ------------------------------------------------------------- trade
    def trade_tf(self, tf: str):
        t0 = time.perf_counter()
        T = lb.trade(self.symbol, tf)
        F = ft.assemble(self.symbol, tf)
        pos = np.searchsorted(F['t'], T['t'])
        if not np.array_equal(F['t'][pos], T['t']):
            raise RuntimeError(f'{tf}: trade rows missing from the feature rows')
        days = bl.Days(int(T['close_ms'][0]), int(T['close_ms'][-1]))
        B = bl.trade(days, T, self.start)
        R = bl.trade(days, T, self.start, self.half_life)
        di = days.of(T['close_ms'])
        yrs = self._years(T['close_ms'])
        ev = (B['n'][di] >= MIN_N) & np.isin(yrs, self.eval_years)
        idx = np.nonzero(ev)[0]
        d, y_, day = di[idx], yrs[idx], F['day'][pos[idx]]
        G = {k: (v[0][pos[idx]], v[1]) for k, v in groups(F).items()}
        horizon = 72 if tf == '5m' else 24          # decision bars the 360-minute path spans
        base = {'tf': tf, 'horizon': horizon, 'kind': 'trade'}
        # The baseline is REPLAYED AT THE ROW'S OWN COST: the spread in ATR is
        # known when a trade is decided, and it moves the odds (baseline.trade).
        # The table at the costs history actually paid is kept as the 'pooled'
        # reference forecaster, to show what ignoring the spread costs.
        sp_atr, slip = T['spread_atr'][idx], T['slip_atr'][idx]
        cm = {'p_tpl': bl.pick_tpl(B, d, sp_atr)}
        cm['p_mfe'], cm['p_mae'] = bl.pick_excursion(B, d, sp_atr, slip)
        cr = {'p_tpl': bl.pick_tpl(R, d, sp_atr)}
        cr['p_mfe'], cr['p_mae'] = bl.pick_excursion(R, d, sp_atr, slip)
        for ti, (side, sl, tp) in enumerate(TEMPLATES):
            y = T['tpl'][idx, ti].astype(np.int64)
            cls = np.select([y == 1, y == -1], [0, 1], 2)            # TPL_CLASSES order
            Pb = cm['p_tpl'][:, ti]
            meta = dict(base, output=f'tpl_{side}_{sl:g}atr_{tp:g}r', k=0)
            self._emit_prob(dict(meta, forecaster='climatology'), Pb, cls, Pb, day, y_, horizon,
                            with_ci=False)
            self._emit_prob(dict(meta, forecaster='recency'), cr['p_tpl'][:, ti], cls, Pb, day,
                            y_, horizon)
            self._emit_prob(dict(meta, forecaster='pooled'), B['p_tpl'][d, ti], cls, Pb, day,
                            y_, horizon)
            if sl == 1.0 and tp in (1.0, 2.0):
                tp_first = (y == 1) * 1.0
                self._conditional(dict(meta, output=meta['output'] + '_tp_first',
                                       forecaster='climatology'),
                                  Pb[:, 0], tp_first, day, y_, G)
        for si, side in enumerate(('buy', 'sell')):
            for key, grid, name in (('p_mfe', MFE_AT, 'mfe'), ('p_mae', MAE_AT, 'mae')):
                vals = T[f'{side}_{name}'][idx]
                for xi, x in enumerate(grid):
                    y = (vals >= x) * 1.0
                    pb = cm[key][:, si, xi]
                    meta = dict(base, output=f'{name}_{side}_{x:g}atr', k=0)
                    self._emit_prob(dict(meta, forecaster='climatology'), pb, y, pb, day, y_,
                                    horizon, with_ci=False)
                    self._emit_prob(dict(meta, forecaster='recency'), cr[key][:, si, xi], y, pb,
                                    day, y_, horizon)
                    self._emit_prob(dict(meta, forecaster='pooled'), B[key][d, si, xi], y, pb,
                                    day, y_, horizon)
        self._cond_trade(tf, F, T, B, idx, d, day, y_, horizon, pos)
        yrs_all = self._years(T['close_ms'])
        cost_by_year = {str(yv): round(float(np.median(T['spread_atr'][yrs_all == yv])), 4)
                        for yv in np.unique(yrs_all)}
        self.info['trade'][tf] = {'rows': int(T['t'].size), 'scored': int(idx.size),
                                  'median_spread_atr_by_year': cost_by_year,
                                  'seconds': round(time.perf_counter() - t0, 1)}
        self.log(f'  trade  {tf:>3}: {idx.size:>7} forecasts scored '
                 f'({time.perf_counter() - t0:.0f}s)')

    # ------------------------------------------------------------- output
    def manifest(self) -> dict:
        from .news import events
        ev = events()
        return {
            'run_id': self.run_id, 'symbol': self.symbol, 'created_utc':
                datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'versions': {'feature': FEATURE_VERSION, 'label': LABEL_VERSION,
                         'baseline': BASELINE_VERSION, 'engine': engine_hash(), 'git': _git()},
            'train_start': TRAIN_START, 'eval_years': list(self.eval_years),
            'half_life_days': self.half_life, 'min_baseline_n': MIN_N,
            'bootstrap_reps': self.reps, 'horizons': dict(HORIZON),
            'templates': [list(x) for x in TEMPLATES], 'news_events': ev.get('n'),
            'forecasters': {
                'climatology': 'empirical baseline, expanding from TRAIN_START, resolved-only, '
                               'refreshed each broker day',
                'recency': f'climatology with a {self.half_life}-day half-life',
                'state_transition': 'REFERENCE check: P(state later | state now), shrunk to '
                                    'climatology (50 pseudo-counts)',
                'pooled': 'trade outputs only - the climatology at the costs history actually '
                          'paid, kept to show what ignoring the spread costs'},
            'trade_baseline': 'replayed at the row\'s own cost: templates from outcomes re-run '
                              f'at spread levels {list(COST_LEVELS)} ATR (interpolated), MFE/MAE '
                              'from raw excursions at cost-shifted thresholds',
            'data': self.info,
        }

    def save(self) -> None:
        import pandas as pd
        self.dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(self.rows).to_parquet(self.dir / 'scorecard.parquet', compression='zstd',
                                           index=False)
        cond = pd.DataFrame([dict(r, per_year=json.dumps(r['per_year'])) for r in self.cond])
        cond.to_parquet(self.dir / 'conditional.parquet', compression='zstd', index=False)
        (self.dir / 'manifest.json').write_text(json.dumps(self.manifest(), indent=1),
                                                encoding='utf-8')
        (self.dir / 'ui_summary.json').write_text(json.dumps(self.ui_summary(), indent=1,
                                                             default=_json_default),
                                                  encoding='utf-8')

    def ui_summary(self) -> dict:
        """What the lab shows next to a forecast: gates, decay, references, trade calibration."""
        def pick(**kw):
            return [r for r in self.rows if all(r.get(k) == v for k, v in kw.items())]
        ref = {tf: {r['year']: [r.get('skill'), r.get('ci_lo'), r.get('ci_hi')]
                    for r in pick(tf=tf, output='state', forecaster='state_transition')}
               for tf in self.info['tfs']}
        trade = {}
        for tf in self.info['trade']:
            trade[tf] = {}
            for side, sl, tp in TEMPLATES:
                name = f'tpl_{side}_{sl:g}atr_{tp:g}r'
                trade[tf][name] = {r['year']: [r.get('mean_p_c0'), r.get('freq_c0'), r.get('ece')]
                                   for r in pick(tf=tf, output=name, forecaster='climatology')}
        cond = {}
        ccond = {}
        for r in self.cond:
            if r.get('kind') == 'skill' and r.get('forecaster') == 'cond_v1':
                ccond.setdefault(r['tf'], []).append(
                    {'output': r['output'], 'cond': r['cond'], 'group': r['group'],
                     'skill': r['skill'], 'lo': r['ci_lo'], 'hi': r['ci_hi'],
                     'per_year': r['per_year'], 'n_eff': r['n_eff']})
            if r.get('kind') == 'skill' and r.get('forecaster') == 'range_v1':
                cond.setdefault(r['tf'], []).append(
                    {'side': r['output'][6:], 'cond': r['cond'], 'group': r['group'],
                     'skill': r['skill'], 'lo': r['ci_lo'], 'hi': r['ci_hi'],
                     'per_year': r['per_year'], 'n_eff': r['n_eff']})
        return {'created_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'versions': self.manifest()['versions'], 'eval_years': list(self.eval_years),
                'range': self.gates, 'range_conditions': cond, 'reference': ref,
                'trade': trade, 'cond': self.cgates, 'cond_conditions': ccond}

    def run(self, keep_rows: bool = True) -> 'Run':
        for tf in self.tfs:
            self.market_tf(tf, keep_rows)
        for tf in self.trade_tfs:
            self.trade_tf(tf)
        self.save()
        return self


__all__ = ['EVAL_YEARS', 'MIN_N', 'HALF_LIFE', 'groups', 'prob_row', 'quant_row', 'diff_ci', 'Run']
