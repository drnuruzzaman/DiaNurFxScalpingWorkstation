"""
server/lab/forecast.py - the forecast engine inside a replay (Phase B).

For the bar on screen, every frame carries:

  - the RANGE CONE for the session's timeframe: the model's quantiles and the
    baseline's, per step. The session symbol's latest batch run's promotion
    gate says which one to draw - the model only if it beat the baseline in
    every scored year. One symbol's gates never stand in for another's.
  - the MTF rows: each forecast timeframe at its latest CLOSED bar.
  - BASELINE ODDS for the signals and open positions on screen - target first,
    stop first, neither inside 72 x 5m - replayed at this bar's cost.
  - how THIS SESSION's forecasts have done: each one is settled when the replay
    passes its horizon (the bars after it are simulated history by then) and
    scored against the baseline on the same bars.

All of it is lookups into runs/forecast/cache (server/forecast/store.py);
nothing here reads a bar the replay has not reached.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from ..forecast import SYMBOL, TFS, TRADE_TFS
from ..forecast import cond_model as cm
from ..forecast import score as sc
from ..forecast import store as fstore
from ..forecast.range_model import QS

_SUMMARY: dict = {}             # symbol -> {'at', 'doc'}
_NEWS: dict = {}
NEWS_DIR = Path(__file__).resolve().parents[2] / 'runs' / 'forecast' / 'news'


def news_summary(symbol: str = SYMBOL) -> dict:
    """The symbol's newest Phase E news-layer run (tools/forecast_news.py), re-read at most every 30 s."""
    hit = _NEWS.get(symbol)
    if hit is None or time.time() - hit['at'] > 30:
        doc = {}
        try:
            for p in sorted((p for p in NEWS_DIR.iterdir() if (p / 'summary.json').exists()),
                            reverse=True):
                d = json.loads((p / 'summary.json').read_text(encoding='utf-8'))
                if (d.get('symbol') or SYMBOL) == symbol:
                    doc = d
                    break
        except (OSError, ValueError):
            doc = {}
        hit = _NEWS[symbol] = {'at': time.time(), 'doc': doc}
    return hit['doc']


def news_reaction(tf: str, kind: str, as_of_ms: int, symbol: str = SYMBOL) -> dict | None:
    """
    How much wider the horizon's range ran around this kind of release than the same
    hour without one - from releases whose horizon had CLOSED by as_of_ms (broker time),
    each against the quiet hours of the year before it. Never a release after the bar.
    """
    r = (((news_summary(symbol).get('tfs') or {}).get(tf) or {}).get('reactions') or {}).get(kind)
    if not r:
        return None
    ev = [e for e in r.get('events') or [] if int(e[1]) <= int(as_of_ms)]
    if len(ev) < 5:
        return None
    xs = np.array([float(e[2]) for e in ev])
    return {'kind': kind, 'n': len(ev), 'x': float(np.median(xs)),
            'x_p25': float(np.quantile(xs, 0.25)), 'x_p75': float(np.quantile(xs, 0.75)),
            'p_up': float(np.mean([int(e[3]) for e in ev])), 'first_ms': int(ev[0][0])}


def summary(symbol: str = SYMBOL) -> dict:
    """The symbol's latest batch run's ui_summary.json, re-read at most every 30 s."""
    hit = _SUMMARY.get(symbol)
    if hit is None or time.time() - hit['at'] > 30:
        hit = _SUMMARY[symbol] = {'at': time.time(), 'doc': fstore.latest_summary(symbol)}
    return hit['doc']


def _gate(tf: str, symbol: str = SYMBOL) -> dict | None:
    g = (summary(symbol).get('range') or {}).get(tf)
    if not g:
        return None
    return {'promoted': bool(g.get('promoted')), 'years': g.get('years') or {},
            'k_prior': g.get('k_prior'), 'half_life_months': g.get('half_life_months')}


def _cgate(tf: str, key: str, symbol: str = SYMBOL) -> dict | None:
    """The Phase C gate for one output ('direction', 'state', 'trade') on a timeframe."""
    g = ((summary(symbol).get('cond') or {}).get(tf) or {}).get(key)
    if not g:
        return None
    return {'promoted': bool(g.get('promoted')), 'years': g.get('years') or {}}


def confidence(p: float, lo: float, hi: float, base: float, neff: float, promoted: bool) -> str:
    """
    The fixed rules (never judgement):
      high      the 90% interval excludes the baseline, the cell has >= 500
                effective samples, and this output passed its promotion gate
      moderate  the interval excludes the baseline but one of the other two fails
      baseline  the interval includes the baseline - no edge worth reading
    """
    if lo <= base <= hi:
        return 'baseline'
    return 'high' if (neff >= 500 and promoted) else 'moderate'


def _loss(q, x) -> float:
    return float(sum(sc.pinball(q[i], x, tau) for i, tau in enumerate(QS)) / len(QS))


class SessionForecast:
    """One replay session's forecasts: the payload per bar and the settled record."""

    def __init__(self, symbol: str, tf: str):
        self.symbol, self.tf = symbol, tf
        self.settled: list = []
        self._sum = {'n': 0, 'in_up': 0, 'in_dn': 0, 'b_in_up': 0, 'b_in_dn': 0,
                     'loss_m': 0.0, 'loss_b': 0.0}
        # Load every timeframe's store now (once per process) so the first frame
        # of a replay does not stall on reading them from disk.
        for t in TFS:
            self.store(t)

    def store(self, tf: str = None):
        tf = tf or self.tf
        return fstore.load(self.symbol, tf, allow_stale=True) if tf in TFS else None

    # ------------------------------------------------------------ payload
    def payload(self, sess, v: int, frame: dict) -> dict:
        t_v = int(sess.series.t[v])
        close_ms = t_v + sess.tf_ms
        st = self.store()
        out = {'tf': self.tf, 'symbol': self.symbol, 'available': False}
        if st is None:
            out['reason'] = ('forecasts are built for 5m, 15m, 1h and 4h sessions'
                             if self.tf not in TFS else
                             f'the forecast tables for {self.symbol} are not built - switch its '
                             'forecast engine on in Settings > Market data, then update it')
        else:
            out.update(st.at(t_v))
            out['stale'] = st.stale
        out['gate'] = _gate(self.tf, self.symbol)
        out['promoted'] = bool(out['gate'] and out['gate']['promoted'])
        out['run_id'] = summary(self.symbol).get('run_id')
        out['mtf'] = self._mtf(close_ms)
        # Phase E: the next release's measured reaction, from releases resolved by now.
        cell = out.get('cell') or {}
        if cell.get('news_next') and (cell.get('news_next_min') or 1e9) < 1440:
            out['news'] = news_reaction(self.tf, cell['news_next'], close_ms, self.symbol)
        self._phase_c(sess, out)
        out['trades'] = self._trades(sess, v, frame, st, out) if out.get('available') else []
        out['session'] = self.stats()
        return out

    def _phase_c(self, sess, out: dict) -> None:
        """Direction and regime with their gate and confidence, analogs, MTF agreement."""
        dg, sg = _cgate(self.tf, 'direction', self.symbol), _cgate(self.tf, 'state', self.symbol)
        d = out.get('direction')
        if d:
            d['promoted'] = bool(dg and dg['promoted'])
            d['gate'] = dg
            d['confidence'] = confidence(d['p'][-1], d['lo'], d['hi'], d['base'][-1], d['neff'],
                                         d['promoted'])
        r = out.get('regime')
        if r:
            r['promoted'] = bool(sg and sg['promoted'])
            r['gate'] = sg
            top = r['top']
            r['confidence'] = confidence(r['p'][top], r['lo'], r['hi'], r['base'][top], r['neff'],
                                         r['promoted'])
        an = out.get('analogs')
        if an and an.get('rows'):
            lo_t, hi_t = int(sess.series.t[sess.i0]), int(sess.series.t[sess.k])
            for row in an['rows']:
                row['in_session'] = lo_t <= row['t'] <= hi_t
            if d:
                # Do the similar past moments point the same way as the table?
                ap, mp, bp = an.get('p_up'), d['p'][-1], d['base'][-1]
                if ap is None or an['n_eff'] < 20:
                    an['agreement'] = 'too few'
                elif (ap - bp) * (mp - bp) > 0 and abs(ap - bp) > 0.02:
                    an['agreement'] = 'agree'
                elif abs(ap - bp) <= 0.02:
                    an['agreement'] = 'neutral'
                else:
                    an['agreement'] = 'disagree'
        # Timeframe agreement: the direction read on every forecast timeframe.
        ps = [(r_['tf'], r_.get('p_up_model')) for r_ in out.get('mtf') or []
              if r_.get('p_up_model') is not None]
        if ps:
            ups = sum(1 for _, pv in ps if pv > 0.5)
            spread = max(pv for _, pv in ps) - min(pv for _, pv in ps)
            label = ('all up' if ups == len(ps) else 'all down' if ups == 0 else 'mixed')
            any_promoted = any(bool((_cgate(tf, 'direction', self.symbol) or {}).get('promoted'))
                               for tf, _ in ps)
            out['agreement'] = {'label': label, 'spread': spread, 'rows': ps,
                                'promoted': any_promoted}

    def _mtf(self, close_ms: int) -> list:
        rows = []
        for tf in TFS:
            s2 = self.store(tf)
            if s2 is None:
                continue
            i = s2.row_closed_by(close_ms)
            r = s2.horizon_row(i) if i is not None else None
            if r:
                g = _gate(tf, self.symbol)
                r['promoted'] = bool(g and g['promoted'])
                rows.append(r)
        return rows

    def _trades(self, sess, v: int, frame: dict, st, fc: dict) -> list:
        if self.tf not in TRADE_TFS or st is None:
            return []
        atr = float(fc['atr'])
        point = float(sess.spec.get('point') or 0.01)
        spread = float(sess._spread(v)) * point
        slip = float(sess.eff['instrument'].get('default_slippage_points', 3.0)) * point
        cost, slip_atr = spread / atr, slip / atr
        close_ms = int(fc['close_ms'])
        bid = float(sess.series.c[v])
        out = []
        for s in frame.get('signals') or []:
            if not (s.get('status') == 'qualified' or s.get('stage') in ('SENT', 'FILLED')):
                continue
            entry, stop = float(s['entry']), float(s['stop'])
            a = abs(entry - stop) / atr
            row = {'kind': 'signal', 'id': s['id'], 'side': s['side'],
                   'label': s.get('label') or s.get('playbook'), 'stop_atr': a}
            for key in ('tp1', 'tp2'):
                b = abs(float(s[key]) - entry) / atr
                row[key] = self._with_model(st, fc, s['side'], a, b,
                                            st.barrier(close_ms, s['side'], a, b, cost, slip_atr))
            out.append(row)
        for p in frame.get('positions') or []:
            sl, tp = float(p.get('sl') or 0), float(p.get('tp') or 0)
            if not (sl and tp):
                continue
            # A filled trade from where price is NOW: entry costs are sunk, a buy
            # exits on the bid and a sell on the ask (bid + spread).
            if p['side'] == 'buy':
                d_tp, d_sl = tp - bid, bid - sl
            else:
                d_tp, d_sl = bid - tp + spread, sl - spread - bid
            if d_tp <= 0 or d_sl <= 0:
                continue
            o = st.barrier(close_ms, p['side'], d_sl / atr, d_tp / atr, 0.0, 0.0)
            out.append({'kind': 'position', 'id': f"pos-{p['ticket']}", 'side': p['side'],
                        'stop_atr': d_sl / atr,
                        'tp': self._with_model(st, fc, p['side'], d_sl / atr, d_tp / atr, o)})
        return out

    def _with_model(self, st, fc: dict, side: str, stop_atr: float, target_atr: float, base) -> dict:
        """The baseline odds, plus the conditions' lift for the nearest template."""
        out = dict(base or {}, target_atr=target_atr)
        if not base or base.get('target') is None:
            return out
        lift = st.trade_lift(int(fc['row']), side, stop_atr, target_atr)
        if not lift:
            return out
        b3 = np.array([base['target'], base['stop'], base['neither']])
        m3 = cm._apply(b3, np.array(lift['lift']))
        tg = _cgate(self.tf, 'trade', self.symbol)
        out['model'] = {'target': float(m3[0]), 'stop': float(m3[1]), 'neither': float(m3[2]),
                        'template': lift['template'], 'why': lift['why'],
                        'neff': lift['neff'], 'promoted': bool(tg and tg['promoted'])}
        return out

    # ------------------------------------------------------------ settling
    def settle(self, sess, k1: int) -> None:
        """The forecast made H bars before bar k1 has now played out: score it."""
        st = self.store()
        if st is None:
            return
        H = st.H
        j = k1 - H
        if j < sess.i0:
            return
        i = st.row_at(int(sess.series.t[j]))
        c = st.cone(i) if i is not None else None
        if c is None:
            return
        close_j = float(sess.series.c[j])
        atr = float(st.a['atr'][i])
        if not atr > 0:
            return
        up = (float(sess.series.h[j + 1:k1 + 1].max()) - close_j) / atr
        dn = (close_j - float(sess.series.l[j + 1:k1 + 1].min())) / atr
        qu, qd, bu, bd = c['up'][-1], c['dn'][-1], c['base_up'][-1], c['base_dn'][-1]
        rec = {'i': j, 't': int(sess.series.t[j]), 'up': round(up, 3), 'dn': round(dn, 3),
               'q_up': qu, 'q_dn': qd, 'b_up': bu, 'b_dn': bd,
               'in_up': bool(qu[0] <= up <= qu[2]), 'in_dn': bool(qd[0] <= dn <= qd[2]),
               'b_in_up': bool(bu[0] <= up <= bu[2]), 'b_in_dn': bool(bd[0] <= dn <= bd[2]),
               'loss_m': _loss(qu, up) + _loss(qd, dn), 'loss_b': _loss(bu, up) + _loss(bd, dn)}
        self.settled.append(rec)
        s = self._sum
        s['n'] += 1
        for key in ('in_up', 'in_dn', 'b_in_up', 'b_in_dn'):
            s[key] += int(rec[key])
        s['loss_m'] += rec['loss_m']
        s['loss_b'] += rec['loss_b']

    def stats(self) -> dict:
        s = self._sum
        n = s['n']
        if not n:
            return {'n': 0}
        return {'n': n, 'cov_up': s['in_up'] / n, 'cov_dn': s['in_dn'] / n,
                'b_cov_up': s['b_in_up'] / n, 'b_cov_dn': s['b_in_dn'] / n,
                'skill': (1.0 - s['loss_m'] / s['loss_b']) if s['loss_b'] > 0 else None}

    def rows(self, limit: int = 300) -> list:
        return self.settled[-int(limit):]


__all__ = ['summary', 'SessionForecast']
