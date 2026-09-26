#!/usr/bin/env python
"""
tools/forecast_filtertest.py - Phase D: do the promoted forecasts improve the strategy?

    python tools/forecast_filtertest.py                      15m and 5m, 2024-2026, every filter
    python tools/forecast_filtertest.py --tfs 15m --workers 2
    python tools/forecast_filtertest.py --report runs/forecast/filtertest/<run_id>
    python tools/forecast_filtertest.py --run-id phase-d --tfs 5m --filters news_gate --add-arms

--add-arms runs only the arms a stored timeframe-year lacks. Its fresh baseline must
match the stored one trade for trade, or the new arms are NOT merged (they go to
<run>/unmerged/ and the run says why) - a new arm is only ever compared with the
baseline it was run against.

Each timeframe-year is replayed through the live engine, the live executor
and the simulated broker (server/lab/filtertest.py): once with no filter,
then once per forecast filter (server/lab/filters.py). The filters and their
thresholds were fixed before this ran; the verdict rule is fixed here:

    A filter PASSES on a timeframe only if, in EVERY year, it improves the
    expectancy (R per trade), the total R AND the net P&L after costs.

Four filters on two timeframes is eight tries, so a pass that is marginal in
one year is reported as marginal, not as an edge. The live settings are read
ONCE, when the run starts, and every replay of every timeframe-year uses that
copy - a save in the live app mid-run cannot put the baseline and a filter on
different settings. The report prints each result's settings fingerprint. Workers run at
below-normal priority; the default of two keeps memory use modest next to the
live system. Measurements on history - not trading advice.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'runs' / 'forecast' / 'filtertest'


def _task(symbol: str, tf: str, year: int, out_dir: str, filters=None, settings=None,
          merge: bool = False) -> dict:
    from tools.forecast_build import _below_normal
    _below_normal()
    from server.forecast import sync_engine_settings
    sync_engine_settings()
    from server.lab.filtertest import run_year
    from server.lab.filters import FILTERS
    t0 = time.perf_counter()
    logf = Path(out_dir) / f'{tf}_{year}.log'

    def log(m):
        with open(logf, 'a', encoding='utf-8') as fh:
            fh.write(time.strftime('%H:%M:%S') + ' ' + str(m) + '\n')
    r = run_year(symbol, tf, year, filters=tuple(filters) if filters else FILTERS, log=log,
                 settings=settings)
    path = Path(out_dir) / f'{tf}_{year}.json'
    merged = None
    if merge and path.exists():
        from server.lab.filtertest import _fingerprint
        old = json.loads(path.read_text(encoding='utf-8'))
        merged = _fingerprint(old['baseline']['trades']) == _fingerprint(r['baseline']['trades'])
        if merged:
            old['filters'].update(r['filters'])
            old.setdefault('arms_added', []).append({
                'arms': list(r['filters']), 'settings_hash': r.get('settings_hash'),
                'baseline_identical': True, 'at': time.strftime('%Y-%m-%d %H:%M:%S')})
            path.write_text(json.dumps(old, default=str), encoding='utf-8')
        else:
            side = Path(out_dir) / 'unmerged'
            side.mkdir(exist_ok=True)
            (side / f'{tf}-{year}.json').write_text(json.dumps(r, default=str), encoding='utf-8')
    else:
        path.write_text(json.dumps(r, default=str), encoding='utf-8')
    return {'tf': tf, 'year': year, 'seconds': round(time.perf_counter() - t0),
            'trades': len(r['baseline']['trades']), 'check': r['check']['same'], 'merged': merged}


# --------------------------------------------------------------------------- #
# comparison                                                                  #
# --------------------------------------------------------------------------- #
def _key(t: dict) -> tuple:
    return (int(t['entry_t']), t['side'])


def _boot_diff(a: list, b: list, reps: int = 1000, seed: int = 3) -> tuple:
    """90% day-block interval for mean R (b) - mean R (a); days resampled within each run."""
    def by_day(tr):
        d = {}
        for t in tr:
            d.setdefault(int(t['exit_t']) // 86_400_000, []).append(float(t['r']))
        return [np.array(v) for v in d.values()]
    da, db = by_day(a), by_day(b)
    if len(da) < 5 or len(db) < 5:
        return float('nan'), float('nan')
    g = np.random.default_rng(seed)
    out = []
    for _ in range(reps):
        sa = np.concatenate([da[i] for i in g.integers(0, len(da), len(da))])
        sb = np.concatenate([db[i] for i in g.integers(0, len(db), len(db))])
        out.append(sb.mean() - sa.mean())
    return float(np.quantile(out, 0.05)), float(np.quantile(out, 0.95))


def summarise(run_dir: Path) -> dict:
    res = {}
    for f in sorted(run_dir.glob('*_*.json')):
        if f.name in ('results.json',):
            continue
        r = json.loads(f.read_text(encoding='utf-8'))
        tf, year = r['tf'], r['year']
        base = r['baseline']
        bt = base['trades']
        bkeys = {_key(t): t for t in bt}
        row = {'baseline': _stats(base), 'check': r['check'], 'filters': {},
               'settings_hash': r.get('settings_hash')}
        for ff, fr in r['filters'].items():
            ft_ = fr['trades']
            fkeys = {_key(t): t for t in ft_}
            removed = [bkeys[k] for k in bkeys if k not in fkeys]
            added = [fkeys[k] for k in fkeys if k not in bkeys]
            lo, hi = _boot_diff(bt, ft_)
            s = _stats(fr)
            s.update({
                'removed_n': len(removed), 'removed_r': _mean_r(removed),
                'added_n': len(added), 'added_r': _mean_r(added),
                'd_exp_r': _d(s['exp_r'], row['baseline']['exp_r']), 'd_exp_lo': lo, 'd_exp_hi': hi,
                'd_sum_r': _d(s['sum_r'], row['baseline']['sum_r']),
                'd_net': _d(s['net'], row['baseline']['net']),
                'blocked_events': fr.get('blocked_events', 0)})
            row['filters'][ff] = s
        res.setdefault(tf, {})[str(year)] = row
    verdicts = {}
    for tf, years in res.items():
        verdicts[tf] = {}
        ffs = sorted({ff for y in years.values() for ff in y['filters']})
        for ff in ffs:
            ys = [years[y]['filters'].get(ff) for y in sorted(years)]
            ok = all(x and x['d_exp_r'] > 0 and x['d_sum_r'] > 0 and x['d_net'] > 0 for x in ys)
            verdicts[tf][ff] = 'PASS' if ok and len(ys) == 3 else 'fail'
    return {'results': res, 'verdicts': verdicts}


def _stats(run: dict) -> dict:
    st = run.get('stats') or {}
    return {'n': st.get('n', len(run['trades'])), 'win': st.get('win'), 'exp_r': st.get('exp_r'),
            'sum_r': st.get('sum_r'), 'net': st.get('net'), 'pf': st.get('pf'),
            'max_dd': st.get('max_dd')}


def _mean_r(tr: list):
    return float(np.mean([float(t['r']) for t in tr])) if tr else None


def _d(a, b):
    return (a - b) if (a is not None and b is not None) else float('nan')


def report(run_dir: Path) -> str:
    doc = summarise(run_dir)
    (run_dir / 'results.json').write_text(json.dumps(doc, indent=1, default=str), encoding='utf-8')
    out = []
    w = out.append
    w(f'FORECAST ENGINE - PHASE D: DO THE FORECASTS IMPROVE THE STRATEGY?     run {run_dir.name}')
    w('Each year replayed through the live engine, executor and simulated broker (auto mode,')
    w('live settings, 0.01 lots); each filter only VETOES sends the live gates had qualified.')
    w('PASS = better expectancy (R/trade), total R AND net P&L after costs in EVERY year.')
    w('Removed / added: trades the filter took out, and trades its freed capacity let in.')
    w('news_gate is not a forecast: the live news blackout, simulated from the release history.')
    w('These are measurements on history, not trading advice.')
    hashes = {years[y].get('settings_hash') for years in doc['results'].values() for y in years}
    w('Settings: ' + (f'one frozen copy for every result ({hashes.pop()})' if len(hashes) == 1
                      and None not in hashes else
                      'NOT the same for every result - compare within a timeframe-year only; '
                      'each year shows its fingerprint (- = not recorded, live when it ran)'))
    for tf, years in doc['results'].items():
        w(f'\n{tf}')
        for y in sorted(years):
            b = years[y]['baseline']
            chk = years[y]['check']
            w(f"  {y}  baseline: {b['n']} trades, win {_p(b['win'])}, {_f(b['exp_r'], 3)}R/trade, "
              f"total {_f(b['sum_r'], 1)}R, net {_f(b['net'], 2)}, PF {_f(b['pf'], 2)}, "
              f"max DD {_f(b['max_dd'], 2)}   (cache check: {'identical' if chk['same'] else 'DIFFERENT'}; "
              f"settings {years[y].get('settings_hash') or '-'})")
            for ff, s in years[y]['filters'].items():
                w(f"    {ff:<14} {s['n']:>5} trades  {_f(s['exp_r'], 3)}R/trade "
                  f"({s['d_exp_r']:+.3f} [{s['d_exp_lo']:+.3f},{s['d_exp_hi']:+.3f}])  "
                  f"total {s['d_sum_r']:+.1f}R  net {s['d_net']:+.2f}  | removed {s['removed_n']} at "
                  f"{_f(s['removed_r'], 3)}R, added {s['added_n']} at {_f(s['added_r'], 3)}R")
        w('  verdicts: ' + ', '.join(f'{ff} {v}' for ff, v in doc['verdicts'][tf].items()))
    return '\n'.join(out)


def _f(x, d):
    return '-' if x is None or x != x else f'{x:.{d}f}'


def _p(x):
    return '-' if x is None else f'{100 * x:.0f}%' if x <= 1 else f'{x:.0f}%'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--tfs', default='15m,5m')
    ap.add_argument('--years', default='2024,2025,2026')
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--symbol', default='XAUUSD.a')
    ap.add_argument('--run-id', default=None)
    ap.add_argument('--filters', default=None,
                    help='comma list of arms: the four forecast filters (default) and news_gate')
    ap.add_argument('--report', default=None)
    ap.add_argument('--add-arms', action='store_true',
                    help='add the --filters arms a stored timeframe-year lacks (see the doc)')
    a = ap.parse_args()
    if a.report:
        txt = report(Path(a.report))
        (Path(a.report) / 'report.txt').write_text(txt, encoding='utf-8')
        print(txt)
        return 0
    from tools.forecast_build import _below_normal
    _below_normal()
    from server.forecast import sync_engine_settings
    sync_engine_settings()
    from server.lab.filtertest import frozen_settings, settings_hash
    settings = frozen_settings()
    run_id = a.run_id or datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    d = OUT / run_id
    d.mkdir(parents=True, exist_ok=True)
    from server.lab.filtertest import ARMS
    flt = a.filters.split(',') if a.filters else None
    bad = [f for f in flt or [] if f not in ARMS]
    if bad:
        raise SystemExit(f'unknown arm(s) {bad}; choose from {ARMS}')
    if a.add_arms and not flt:
        raise SystemExit('--add-arms needs --filters: the arms to add')
    tasks = []
    for tf in a.tfs.split(','):
        for y in a.years.split(','):
            f = d / f'{tf}_{y}.json'
            if not f.exists():
                tasks.append((tf, int(y), flt, False))
            elif a.add_arms:
                have = json.loads(f.read_text(encoding='utf-8')).get('filters') or {}
                need = [x for x in flt if x not in have]
                if need:
                    tasks.append((tf, int(y), need, True))
    print(f'filter test {run_id}: {len(tasks)} timeframe-years on {a.workers} workers, '
          f'settings {settings_hash(settings)}' + (' (adding arms)' if a.add_arms else ''),
          flush=True)
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=max(1, a.workers)) as pool:
        futs = {pool.submit(_task, a.symbol, tf, y, str(d), arms, settings, merge): (tf, y)
                for tf, y, arms, merge in tasks}
        for f in as_completed(futs):
            tf, y = futs[f]
            try:
                r = f.result()
                note = {None: '', True: ', arms merged (baseline identical)',
                        False: ', baseline DIFFERS - arms NOT merged, see unmerged/'}[r['merged']]
                print(f"  {tf} {y}: {r['trades']} baseline trades, cache check "
                      f"{'identical' if r['check'] else 'DIFFERENT'}{note}, {r['seconds']}s "
                      f"(elapsed {time.perf_counter() - t0:.0f}s)", flush=True)
            except Exception as e:                                     # noqa: BLE001
                print(f'  [FAIL] {tf} {y}: {e!r}', flush=True)
    txt = report(d)
    (d / 'report.txt').write_text(txt, encoding='utf-8')
    print(txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
