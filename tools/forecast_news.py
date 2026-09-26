#!/usr/bin/env python
"""
tools/forecast_news.py - Phase E: does spelling out the news improve the range forecast?

    python tools/forecast_news.py                   every timeframe
    python tools/forecast_news.py --tfs 5m,15m
    python tools/forecast_news.py --report runs/forecast/news/<run_id>

For each timeframe:

  1. The news model (server/forecast/news_model.py) is fitted exactly as the
     Phase B range model was - k and the half-life tuned on 2020-2023 only,
     monthly tables of outcomes resolved before each month.
  2. It is scored at the horizon, 2024-2026, against WHAT THE LAB SHOWS NOW:
     the promoted Phase B model on timeframes where it was promoted, the
     climatology baseline where it was not. Pinball skill with a day-block
     bootstrap interval, per year and side, plus P20-P80 coverage.
  3. The gate is Phase B's: the interval clear of zero in EVERY year on both
     sides, and coverage 55-65%. Anything less and the lab keeps its cone.

Alongside, as measurement only: skill inside each news state, and a reaction
table per release type - how much wider the next horizon's range ran than
the same hour with no release, and how often it closed up (direction is
expected to be a coin flip: the calendar says when, never which way).
Workers run below normal priority. Measurements on history - not advice.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'runs' / 'forecast' / 'news'
EVAL_YEARS = (2024, 2025, 2026)
MIN_N = 5000


def _gate(years: dict) -> bool:
    """Phase B's gate: the interval clear of zero in EVERY year, both sides, P20-P80 55-65%."""
    checks = []
    for yr in EVAL_YEARS:
        for side in ('up', 'dn'):
            r = years.get(str(yr), {}).get(side) or {}
            lo = r.get('lo')
            checks.append(lo is not None and lo == lo and lo > 0
                          and 0.55 <= (r.get('cov') or 0) <= 0.65)
    return bool(checks) and all(checks)


def _years(ms) -> np.ndarray:
    return np.asarray(ms, dtype='datetime64[ms]').astype('datetime64[Y]').astype(np.int64) + 1970


def _loss(q, x):
    from server.forecast import range_model as rm
    from server.forecast import score as sc
    return sum(sc.pinball(q[:, i], x, tau) for i, tau in enumerate(rm.QS)) / len(rm.QS)


def run_tf(symbol: str, tf: str, log=print) -> dict:
    from server.forecast import HORIZON, TRAIN_START, ms_of
    from server.forecast import baseline as bl
    from server.forecast import features as ft
    from server.forecast import labels as lb
    from server.forecast import news as nw
    from server.forecast import news_model as nm
    from server.forecast import range_model as rm
    from server.forecast import score as sc
    from server.forecast import store
    from server.forecast import timebase as tb

    t0 = time.perf_counter()
    H = HORIZON[tf]
    F = ft.assemble(symbol, tf)
    M = lb.market(symbol, tf)
    days = bl.Days(int(F['close_ms'][0]), int(F['close_ms'][-1]))
    B = bl.market(days, M, ms_of(TRAIN_START))
    di = days.of(F['close_ms'])
    yrs = _years(F['close_ms'])
    promoted = bool(((store.latest_summary(symbol).get('range') or {}).get(tf) or {}).get('promoted'))
    st_r1 = store.load(symbol, tf)
    if promoted and st_r1 is None:
        raise RuntimeError(f'{tf}: the Phase B store is missing or stale - '
                           'run tools/forecast_build.py --only store')
    log(f'  {tf}: fitting the news model (reference: '
        f'{"Phase B model" if promoted else "climatology baseline"})')
    model = nm.fit(F, M, tf, log=log, steps=[H - 1])
    S = nm.state(F, tf)
    ev = (M['complete'] & np.isfinite(F['atr']) & (B['n'][di] >= MIN_N)
          & np.isin(yrs, EVAL_YEARS))
    idx = np.nonzero(ev)[0]
    d, y_, day, s_ = di[idx], yrs[idx], F['day'][idx], S[idx]
    up2, dn2, cell2, _ = nm.predict(model, F['close_ms'][idx], s_, F['rr_w'][idx])
    if promoted:
        mdl = {'months': st_r1.a['m_starts'], 'pred': st_r1.a['pred'], 'tf': tf}
        up1, dn1, _, _ = rm.predict(mdl, F['close_ms'][idx], F['news_in_h'][idx], F['rr_w'][idx])
    out = {'tf': tf, 'horizon': H, 'reference': 'range_v1' if promoted else 'climatology',
           'k_prior': model['k_prior'], 'half_life_months': model['half_life_months'],
           'years': {}, 'news_rows': {}, 'states': {}, 'rows': int(idx.size)}
    losses = {}
    for side, Q2, Q1 in (('up', up2, up1 if promoted else None), ('dn', dn2, dn1 if promoted else None)):
        x = M[side][idx, H - 1].astype(np.float64)
        qb = B[f'{side}_q'][d, H - 1]
        q2 = Q2[:, H - 1]
        ok = np.isfinite(x) & np.isfinite(q2).all(axis=1) & np.isfinite(qb).all(axis=1)
        ref = Q1[:, H - 1] if promoted else qb
        ok &= np.isfinite(ref).all(axis=1)
        l2, lr, lb_ = _loss(q2[ok], x[ok]), _loss(ref[ok], x[ok]), _loss(qb[ok], x[ok])
        losses[side] = (ok, l2, lr, x, q2)
        for yr in EVAL_YEARS:
            m = y_[ok] == yr
            if not m.any():
                continue
            lo, hi = sc.skill_ci(l2[m], lr[m], day[ok][m])
            out['years'].setdefault(str(yr), {})[side] = {
                'skill': sc.skill(l2[m], lr[m]), 'lo': lo, 'hi': hi,
                'skill_vs_base': sc.skill(l2[m], lb_[m]),
                'cov': sc.coverage(q2[ok][m, 0], q2[ok][m, 2], x[ok][m]),
                'n': int(m.sum()), 'n_eff': sc.n_eff(int(m.sum()), H)}
            # candidate B: the news model on news rows only, the reference elsewhere -
            # scored on the rows it changes (everywhere else it IS the reference)
            mn = m & (s_[ok] != 0)
            if mn.sum() >= 50:
                lo, hi = sc.skill_ci(l2[mn], lr[mn], day[ok][mn])
                out['news_rows'].setdefault(str(yr), {})[side] = {
                    'skill': sc.skill(l2[mn], lr[mn]), 'lo': lo, 'hi': hi,
                    'cov': sc.coverage(q2[ok][mn, 0], q2[ok][mn, 2], x[ok][mn]),
                    'cov_ref': sc.coverage(ref[ok][mn, 0], ref[ok][mn, 2], x[ok][mn]),
                    'n': int(mn.sum()), 'n_eff': sc.n_eff(int(mn.sum()), H)}
        for k in range(nm.N_STATES):
            m = s_[ok] == k
            if m.sum() < 50:
                continue
            row = out['states'].setdefault(nm.SHORT[k], {'state': k, 'name': nm.STATES[k]})
            row[side] = {'skill': sc.skill(l2[m], lr[m]), 'n': int(m.sum()),
                         'n_eff': sc.n_eff(int(m.sum()), H),
                         'years': {str(yr): sc.skill(l2[m & (y_[ok] == yr)], lr[m & (y_[ok] == yr)])
                                   for yr in EVAL_YEARS if (m & (y_[ok] == yr)).sum() >= 20}}
            if side == 'up':
                # how much wider the forecast runs than the same hour and vol with no release
                twin = nm.quiet_twin(cell2[ok][m])
                mi = np.clip(np.searchsorted(model['months'], F['close_ms'][idx][ok][m], 'right') - 1,
                             0, model['months'].size - 1)
                tw = model['pred'][mi, twin][:, :, H - 1, 1].sum(axis=1)
                me = (up2[ok][m][:, H - 1, 1] + dn2[ok][m][:, H - 1, 1])
                good = np.isfinite(tw) & np.isfinite(me) & (tw > 0)
                row['forecast_x'] = float(np.median(me[good] / tw[good])) if good.any() else None
    out['promoted'] = _gate(out['years'])
    out['promoted_news_rows'] = _gate(out['news_rows'])
    # Both candidates were fixed before any 5m-1h result was seen. If both pass, the
    # smaller change wins: news rows only.
    out['use'] = 'news_rows' if out['promoted_news_rows'] else 'all_rows' if out['promoted'] \
        else None
    out['reactions'] = reactions(F, M, S, tf, H, nw, tb)
    out['seconds'] = round(time.perf_counter() - t0)
    return out


def reactions(F, M, S, tf, H, nw, tb) -> dict:
    """
    Per release type: the realised range (up + down, ATR) over the horizon that starts
    at the last close before the release, against the median of the same broker hour's
    no-release rows (state 0) in the 365 days BEFORE it - so each multiplier uses only
    what was known when its horizon closed - and how often the horizon closed up.

    Every event is kept ([broker close ms, resolve ms, multiplier, closed up]) so the lab
    can quote the reaction from releases resolved before the bar on screen, never after.
    """
    from server.config import TF_SECONDS
    doc = json.loads(nw.HISTORY.read_text(encoding='utf-8'))
    close = np.asarray(F['close_ms'], dtype=np.int64)
    tf_ms = TF_SECONDS[tf] * 1000
    rng = (M['up'][:, H - 1] + M['dn'][:, H - 1]).astype(np.float64)
    ret = M['ret'][:, H - 1].astype(np.float64)
    hour = (close // 3_600_000) % 24
    yr = _years(close)
    quiet = (S == 0) & np.isfinite(rng)
    by_hour = {h: (close[quiet & (hour == h)], rng[quiet & (hour == h)]) for h in range(24)}
    year_ms = 365 * 86_400_000
    fin = np.isfinite(ret)
    out = {'_all': {'p_up': float(np.mean(ret[fin] > 0)) if fin.any() else None}}
    for kind in nw.KINDS:
        ts = np.array(sorted(int(e['ts']) for e in doc.get('events') or []
                             if e.get('kind') == kind and e.get('time_known', True)), dtype=np.int64)
        if not ts.size:
            continue
        b = tb.utc_to_broker(ts)
        j = np.searchsorted(close, b, 'right') - 1          # the last close at or before it
        ok = (j >= 0) & (j < close.size)
        j = j[ok]
        ok2 = (b[ok] - close[j] < tf_ms) & np.isfinite(rng[j])   # it prints in the first bar
        j = j[ok2]
        if not j.size:
            continue
        events, per_year = [], {}
        for jj in j:
            qc, qr = by_hour[int(hour[jj])]
            lo, hi = np.searchsorted(qc, close[jj] - year_ms, 'left'), np.searchsorted(qc, close[jj],
                                                                                        'left')
            if hi - lo < 30:
                continue
            base = float(np.median(qr[lo:hi]))
            if not base > 0:
                continue
            x = float(rng[jj] / base)
            resolve = int(M['resolve_ms'][jj]) if 'resolve_ms' in M else int(close[jj] + H * tf_ms)
            events.append([int(close[jj]), resolve, round(x, 4), int(ret[jj] > 0)])
            per_year.setdefault(int(yr[jj]), []).append(x)
        if not events:
            continue
        xs = np.array([e[2] for e in events])
        ups = np.array([e[3] for e in events])
        out[kind] = {'n': len(events), 'x_median': float(np.median(xs)),
                     'x_years': {str(y): float(np.median(v)) for y, v in sorted(per_year.items())
                                 if y >= 2024 and len(v) >= 3},
                     'p_up': float(ups.mean()), 'first': int(ts[0]), 'last': int(ts[-1]),
                     'events': events}
    return out


def report(doc: dict) -> str:
    out = []
    w = out.append
    w(f"FORECAST ENGINE - PHASE E: THE NEWS LAYER     run {doc['run_id']}")
    w('Range at the horizon, 2024-2026, scored against what the lab shows now (Phase B model where')
    w('promoted, else the climatology baseline). Gate = Phase B\'s: interval clear of zero in EVERY')
    w('year on both sides and P20-P80 coverage 55-65%. Two candidates, both fixed in advance:')
    w('A = the news model on every row; B = on news rows only, the reference elsewhere (scored on')
    w('the rows it changes). If both pass, B - the smaller change. Measurements, not trading advice.')
    for tf, r in doc['tfs'].items():
        verdict = {'news_rows': 'PROMOTED on news rows (the reference everywhere else)',
                   'all_rows': 'PROMOTED on all rows'}.get(r.get('use'),
                                                          'not promoted - the lab keeps its cone')
        w(f"\n{tf}  (vs {r['reference']}; k {r['k_prior']:g}, half-life "
          f"{r['half_life_months'] or 'none'}; {r['rows']} rows)  -> {verdict}")
        w('  A. news model on every row:')
        for yr, sides in sorted(r['years'].items()):
            w('  ' + yr + '  ' + '   '.join(
                f"{s}: skill {v['skill'] * 100:+.1f}% [{v['lo'] * 100:+.1f},{v['hi'] * 100:+.1f}] "
                f"cov {v['cov'] * 100:.0f}% (vs base {v['skill_vs_base'] * 100:+.1f}%)"
                for s, v in sides.items()))
        w('  B. news model on news rows only (scored on those rows; reference coverage in brackets):')
        for yr, sides in sorted(r.get('news_rows', {}).items()):
            w('  ' + yr + '  ' + '   '.join(
                f"{s}: skill {v['skill'] * 100:+.1f}% [{v['lo'] * 100:+.1f},{v['hi'] * 100:+.1f}] "
                f"cov {v['cov'] * 100:.0f}% ({v['cov_ref'] * 100:.0f}%) n_eff {v['n_eff']}"
                for s, v in sides.items()))
        w('  inside each news state (skill vs the reference, 2024-26 pooled; forecast width vs quiet hour):')
        for name, s in r['states'].items():
            u, dn = s.get('up') or {}, s.get('dn') or {}
            yrs = ' '.join(f"{y}:{v * 100:+.0f}%" for y, v in (u.get('years') or {}).items())
            w(f"    {name:<14} n_eff {u.get('n_eff', 0):>5}  up {u.get('skill', float('nan')) * 100:+5.1f}%  "
              f"dn {dn.get('skill', float('nan')) * 100:+5.1f}%  "
              f"width x{s.get('forecast_x') or float('nan'):.2f}   up by year {yrs}")
        allup = (r['reactions'].get('_all') or {}).get('p_up')
        w('  release reactions (range over the next horizon vs the same hour with no release; share '
          f"closing up - {100 * (allup or float('nan')):.0f}% of all horizons do):")
        for kind, v in r['reactions'].items():
            if kind.startswith('_'):
                continue
            yx = ' '.join(f'{y}:x{x:.2f}' for y, x in v['x_years'].items())
            w(f"    {kind:<7} {v['n']:>4} releases  x{(v['x_median'] or float('nan')):.2f}  "
              f"up {100 * (v['p_up'] or float('nan')):.0f}%   {yx}")
    return '\n'.join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--tfs', default='5m,15m,1h,4h')
    ap.add_argument('--symbol', default='XAUUSD.a')
    ap.add_argument('--report', default=None)
    ap.add_argument('--reactions', default=None,
                    help='recompute only the reaction tables of this run (the gate is untouched)')
    a = ap.parse_args()
    if a.report:
        doc = json.loads((Path(a.report) / 'summary.json').read_text(encoding='utf-8'))
        print(report(doc))
        return 0
    if a.reactions:
        from server.forecast import HORIZON
        from server.forecast import features as ft
        from server.forecast import labels as lb
        from server.forecast import news as nw
        from server.forecast import news_model as nm
        from server.forecast import timebase as tb
        d = Path(a.reactions)
        doc = json.loads((d / 'summary.json').read_text(encoding='utf-8'))
        for tf, r in doc['tfs'].items():
            F, M = ft.assemble(a.symbol, tf), lb.market(a.symbol, tf)
            r['reactions'] = reactions(F, M, nm.state(F, tf), tf, HORIZON[tf], nw, tb)
            print(f'  {tf}: reactions rebuilt', flush=True)
        (d / 'summary.json').write_text(json.dumps(doc, indent=1, default=float), encoding='utf-8')
        txt = report(doc)
        (d / 'report.txt').write_text(txt, encoding='utf-8')
        print(txt)
        return 0
    from tools.forecast_build import _below_normal
    _below_normal()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    d = OUT / run_id
    d.mkdir(parents=True, exist_ok=True)
    doc = {'run_id': run_id, 'created_utc': datetime.now(timezone.utc).isoformat(),
           'symbol': a.symbol, 'tfs': {}}
    for tf in a.tfs.split(','):
        doc['tfs'][tf] = run_tf(a.symbol, tf, log=lambda m: print(m, flush=True))
        (d / 'summary.json').write_text(json.dumps(doc, indent=1, default=float), encoding='utf-8')
        print(f"  {tf}: use {doc['tfs'][tf]['use']} "
              f"({doc['tfs'][tf]['seconds']}s)", flush=True)
    txt = report(doc)
    (d / 'report.txt').write_text(txt, encoding='utf-8')
    print(txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
