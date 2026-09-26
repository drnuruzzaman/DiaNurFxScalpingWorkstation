#!/usr/bin/env python
"""
tools/forecast_batch.py - Phase A batch run: score the baselines on 2024-2026.

    python tools/forecast_batch.py                  all forecast and trade timeframes
    python tools/forecast_batch.py --symbol USDJPY.a   another instrument (default gold)
    python tools/forecast_batch.py --tfs 15m --trade-tfs 15m
    python tools/forecast_batch.py --report runs/forecast/batch/<run_id>

Needs the caches from tools/forecast_build.py. Writes runs/forecast/batch/<run_id>/
(manifest, scorecard, conditional table, per-forecast rows) and report.txt.

Everything here is a MEASUREMENT - of the market and of how well an
unconditional baseline describes it - not trading advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _pct(x, d=1):
    return '   -  ' if x != x else f'{100 * x:{5 + d}.{d}f}%'


def _f(x, w=7, d=3):
    return ' ' * (w - 1) + '-' if x != x else f'{x:{w}.{d}f}'


def report(run_dir: Path) -> str:
    import numpy as np
    import pandas as pd

    man = json.loads((run_dir / 'manifest.json').read_text(encoding='utf-8'))
    sc = pd.read_parquet(run_dir / 'scorecard.parquet')
    cond = pd.read_parquet(run_dir / 'conditional.parquet')
    years = [str(y) for y in man['eval_years']]
    H = man['horizons']
    out = []
    w = out.append
    v = man['versions']
    w(f"FORECAST ENGINE - PHASE A: THE BASELINES, SCORED        {man['symbol']}   run {man['run_id']}")
    w(f"versions  feature {v['feature']} / label {v['label']} / baseline {v['baseline']} / "
      f"engine {v['engine']} / git {v['git'] or '-'}")
    w(f"training  expanding from {man['train_start']}, resolved-only, refreshed at each broker "
      f"day start; recency option half-life {man['half_life_days']} days")
    w(f"evaluated {', '.join(years)} (broker-time years); a baseline day needs "
      f"{man['min_baseline_n']} resolved samples; intervals are 90%, day-block bootstrap "
      f"({man['bootstrap_reps']} reps)")
    w('n_eff = rows / horizon: outcomes that share bars are counted once.')
    w('These are measurements of the market and of the baseline, not trading advice.')

    def sel(**kw):
        m = np.ones(len(sc), dtype=bool)
        for k, val in kw.items():
            m &= (sc[k] == val).to_numpy()
        return sc[m]

    # ------------------------------------------------------------------ 1
    w('\n1. WHAT WAS SCORED')
    w(f"   {'':6}{'horizon':>10}{'rows since 2018':>17}{'scored':>9}")
    for tf, d in man['data']['tfs'].items():
        w(f"   {tf:<6}{H[tf]:>6} bars{d['rows']:>17,}{d['scored']:>9,}")
    for tf, d in man['data']['trade'].items():
        w(f"   {tf + ' tr':<6}{'360 min':>10}{d['rows']:>17,}{d['scored']:>9,}   "
          '(trade decisions, walked on M1)')

    # ------------------------------------------------------------------ 2
    w('\n2. IS THE UNCONDITIONAL BASELINE CALIBRATED OUT OF SAMPLE?   (climatology)')
    w('   2a. direction - P(close higher at the horizon)')
    w(f"   {'tf':<5}{'year':<6}{'said':>8}{'happened':>10}{'Brier':>8}{'ECE':>7}{'n_eff':>8}")
    for tf in H:
        for yr in years + ['all']:
            r = sel(tf=tf, output='direction', k=H[tf], forecaster='climatology', year=yr)
            if len(r):
                r = r.iloc[0]
                w(f"   {tf:<5}{yr:<6}{_pct(r['mean_p'])}{_pct(r['freq']):>10}{_f(r['brier'], 8)}"
                  f"{_pct(r['ece']):>7}{int(r['n_eff']):>8,}")
    w('\n   2b. state at the horizon (the chart\'s regime label) - calibration error, 5 classes')
    w(f"   {'tf':<5}" + ''.join(f'{y:>9}' for y in years + ['all']))
    for tf in H:
        cells = []
        for yr in years + ['all']:
            r = sel(tf=tf, output='state', forecaster='climatology', year=yr)
            cells.append(f"{_pct(r.iloc[0]['ece']):>9}" if len(r) else f"{'-':>9}")
        w(f"   {tf:<5}" + ''.join(cells))
    w('\n   2c. range at the horizon - excursion from the close, in ATR')
    w('       calibrated = 20 / 50 / 80% of outcomes at or below P20 / P50 / P80, '
      '60% inside P20-P80')
    w(f"   {'tf':<5}{'side':<5}{'year':<6}{'<=P20':>7}{'<=P50':>7}{'<=P80':>7}{'P20-80':>8}"
      f"{'P50 ATR':>9}{'pinball':>9}")
    for tf in H:
        for side in ('up', 'dn'):
            for yr in years + ['all']:
                r = sel(tf=tf, output=f'range_{side}', k=H[tf], forecaster='climatology', year=yr)
                if len(r):
                    r = r.iloc[0]
                    w(f"   {tf:<5}{side:<5}{yr:<6}{_pct(r['hit20'], 0):>7}{_pct(r['hit50'], 0):>7}"
                      f"{_pct(r['hit80'], 0):>7}{_pct(r['cov'], 0):>8}{_f(r['q50'], 9, 2)}"
                      f"{_f(r['pinball'], 9)}")
    w('\n   2d. trade templates (spread, slippage and the lab broker\'s path rule included)')
    w('       P(target first) said by the baseline (history replayed at each trade\'s own cost)')
    w('       vs what happened; ECE over the 3 outcomes')
    for tf in man['data']['trade']:
        w(f"   {tf} decisions, 360-minute horizon")
        w(f"   {'template':<26}" + ''.join(f"{y + ' said/got':>18}" for y in years) + f"{'ECE all':>9}")
        for side, sl, tp in man['templates']:
            name = f'tpl_{side}_{sl:g}atr_{tp:g}r'
            ra = sel(tf=tf, output=name, forecaster='climatology', year='all')
            w(f"   {side + f' {sl:g} ATR stop, {tp:g}R':<26}" + _tpl_cells(sel, tf, name, years)
              + (f"{_pct(ra.iloc[0]['ece']):>9}" if len(ra) else ''))
        w("   MFE / MAE exceedance (costs included): said -> got, all years")
        for side in ('buy', 'sell'):
            cells = []
            for name, grid in (('mfe', (0.5, 1.0, 1.5, 2.0)), ('mae', (0.5, 1.0, 1.5))):
                for x in grid:
                    r = sel(tf=tf, output=f'{name}_{side}_{x:g}atr', forecaster='climatology',
                            year='all')
                    if len(r):
                        r = r.iloc[0]
                        cells.append(f"{name.upper()}>={x:g}: {_pct(r['mean_p'], 0).strip()}->"
                                     f"{_pct(r['freq'], 0).strip()}")
            w(f"     {side:<5}" + '  '.join(cells))

    w('\n   2e. why the trade baseline replays history at today\'s cost: spread in ATR by year')
    for tf, d in man['data']['trade'].items():
        by = d.get('median_spread_atr_by_year') or {}
        w(f"   {tf:<5}" + '  '.join(f"{y} {v:.3f}" for y, v in sorted(by.items())))
    if man['symbol'] == 'XAUUSD.a':
        w('   Gold\'s ATR grew with its price and the spread did not, so a trade got ~4x cheaper in')
        w('   ATR terms and P(target first) rose on BOTH sides. Pooling all years would call that')
        w('   skill.')
    else:
        w('   When the spread moves against the ATR over the years, a trade\'s cost moves with it,')
        w('   and so does P(target first) on BOTH sides. Pooling all years would call that skill.')
    w('   The table at the costs history paid, scored against the replayed one')
    w('   (skill of the "pooled" table; < 0 = replaying at today\'s cost is the better baseline):')
    w(f"   {'tf':<5}{'template':<16}" + ''.join(f'{y:>22}' for y in years))
    for tf in man['data']['trade']:
        for side in ('buy', 'sell'):
            name = f'tpl_{side}_1atr_1r'
            cells = [_skill_cell(sel(tf=tf, output=name, forecaster='pooled', year=yr))
                     for yr in years]
            w(f"   {tf:<5}{side + ' 1ATR 1R':<16}" + ''.join(cells))

    # ------------------------------------------------------------------ 3
    w(f"\n3. RECENCY WEIGHTING - {man['half_life_days']}-day half-life vs the expanding window")
    w('   skill = 1 - score(recency) / score(expanding); > 0 means recency helps. '
      '[90% interval]')
    rows = [('direction', 'direction @H'), ('state', 'state @H'), ('range_up', 'range up @H'),
            ('range_dn', 'range down @H')]
    w(f"   {'tf':<5}{'output':<16}" + ''.join(f'{y:>22}' for y in years))
    for tf in H:
        for out_name, label in rows:
            cells = []
            for yr in years:
                kk = {'k': H[tf]} if out_name != 'state' else {}
                r = sel(tf=tf, output=out_name, forecaster='recency', year=yr, **kk)
                cells.append(_skill_cell(r))
            w(f"   {tf:<5}{label:<16}" + ''.join(cells))
    for tf in man['data']['trade']:
        for side in ('buy', 'sell'):
            name = f'tpl_{side}_1atr_1r'
            cells = [_skill_cell(sel(tf=tf, output=name, forecaster='recency', year=yr))
                     for yr in years]
            w(f"   {tf:<5}{side + ' 1ATR 1R':<16}" + ''.join(cells))

    # ------------------------------------------------------------------ 4
    w('\n4. REFERENCE CHECK - can the scorecard see skill that is known to exist?')
    w('   state_transition = P(state later | state now); regime persistence is real, '
      'so this must score > 0')
    w(f"   {'tf':<5}" + ''.join(f'{y:>22}' for y in years))
    for tf in H:
        cells = [_skill_cell(sel(tf=tf, output='state', forecaster='state_transition', year=yr))
                 for yr in years]
        w(f"   {tf:<5}" + ''.join(cells))

    # ------------------------------------------------------------------ 5
    w('\n5. WHERE CONDITIONS MOVE THE ODDS - a preview of the conditional tables (Phase C)')
    w('   LIFT = (realised - baseline) in the group, minus the same miss over ALL rows of that')
    w('   year - so a drift that lifts every group at once (a bull year, cheaper trades) is')
    w('   taken out and only what the condition adds is left. Percentage points. Listed only')
    w('   when the lift has the SAME sign in all three years AND its 90% interval excludes 0.')
    w('   A lift is a candidate, not a forecast: Phase C has to turn it into one and beat the')
    w('   baseline out of sample, with a penalty for how many candidates were looked at.')
    lifts = cond[cond['kind'] == 'lift'] if 'kind' in cond.columns else cond
    if len(lifts):
        c = lifts.copy()
        c['py'] = c['per_year'].apply(json.loads)

        def consistent(row):
            vals = [row['py'].get(y) for y in years]
            if any(x is None for x in vals):
                return False
            return (all(x > 0 for x in vals) or all(x < 0 for x in vals)) and \
                (row['ci_lo'] > 0 or row['ci_hi'] < 0)
        tested = len(c)
        c = c[c.apply(consistent, axis=1)]
        # How many would pass if no condition mattered at all: three independent
        # yearly lifts around zero, the same two tests. Groups overlap, so the
        # real null spread is wider than this - read it as a floor.
        g = np.random.default_rng(1)
        z = g.standard_normal((100_000, 3))
        p_null = float(np.mean(((z > 0).all(1) | (z < 0).all(1)) &
                               (np.abs(z.sum(1) / np.sqrt(3)) > 1.645)))
        draws = g.binomial(tested, p_null, 20_000)
        w(f'   {len(c)} of {tested} condition groups met both tests. By chance alone about '
          f'{tested * p_null:.0f} would')
        w(f'   (90% range {np.percentile(draws, 5):.0f}-{np.percentile(draws, 95):.0f}); more '
          'than that suggests some structure is real, but not WHICH rows.')
        c = c.assign(absd=c['lift'].abs()).sort_values(['tf', 'output', 'absd'],
                                                        ascending=[True, True, False])
        for (tf, outn), g in c.groupby(['tf', 'output'], sort=False):
            w(f"   {tf} {outn}")
            for _, r in g.head(8).iterrows():
                yrs = ' '.join(f"{100 * r['py'][y]:+5.1f}" for y in years)
                w(f"     {r['cond'] + ': ' + r['group']:<34} said {_pct(r['mean_p']).strip():>6} "
                  f"got {_pct(r['freq']).strip():>6}  lift {100 * r['lift']:+5.1f}pp "
                  f"[{100 * r['ci_lo']:+.1f}, {100 * r['ci_hi']:+.1f}]  by year {yrs}  "
                  f"n_eff {int(r['n_eff']):,}")
        if not len(c):
            w('   none met both conditions')
    # ------------------------------------------------------------------ 6
    ui = json.loads((run_dir / 'ui_summary.json').read_text(encoding='utf-8')) \
        if (run_dir / 'ui_summary.json').exists() else {}
    gates = ui.get('range') or {}
    if gates:
        w('\n6. THE RANGE MODEL (Phase B) - hour x news x volatility tables, each cell shrunk to')
        w('   its parent. k and the half-life were tuned on 2020-2023 only; 2024-2026 are scored')
        w('   against the climatology at the horizon. PROMOTED = the interval clears zero in every')
        w('   year on both sides AND the P20-P80 band held 55-65% of outcomes - only then does the')
        w('   lab draw this cone; otherwise it draws the baseline and says why.')
        w(f"   {'tf':<5}{'side':<6}" + ''.join(f'{y:>22}' for y in years)
          + f"{'P20-80 held':>16}  verdict")
        for tf, g in gates.items():
            for side in ('up', 'dn'):
                cells, covs = [], []
                for yr in years:
                    r = (g['years'].get(yr) or {}).get(side) or {}
                    sk, lo, hi = r.get('skill'), r.get('lo'), r.get('hi')
                    cells.append(f'{100 * sk:>+8.2f}% [{100 * lo:+.2f},{100 * hi:+.2f}]'.rjust(22)
                                 if sk is not None and lo is not None else f"{'-':>22}")
                    covs.append(f"{100 * r['cov']:.0f}%" if r.get('cov') is not None else '-')
                verdict = ('PROMOTED' if g.get('promoted') else 'baseline kept') if side == 'up' else ''
                w(f"   {tf:<5}{side:<6}" + ''.join(cells) + f"{' / '.join(covs):>16}  {verdict}")
            hl = g.get('half_life_months')
            w(f"   {'':5}tuned: k {g.get('k_prior'):g}, half-life {str(hl) + ' months' if hl else 'none'}"
              f"   decay (skill by step, all years): up "
              + ' '.join(f'{100 * x:+.1f}' if x is not None else '-' for x in g['decay']['up'])
              + '  | down ' + ' '.join(f'{100 * x:+.1f}' if x is not None else '-'
                                       for x in g['decay']['dn']))
        rc = ui.get('range_conditions') or {}
        if rc:
            w('\n   where the range model helps most and least (skill at the horizon, both sides pooled')
            w('   by condition group; interval 90%):')
            for tf, rows in rc.items():
                good = sorted((r for r in rows if r['lo'] is not None),
                              key=lambda r: -r['skill'])
                if not good:
                    continue
                best = ', '.join(f"{r['cond']} {r['group']} ({r['side']}) {100 * r['skill']:+.1f}%"
                                 for r in good[:3])
                worst = ', '.join(f"{r['cond']} {r['group']} ({r['side']}) {100 * r['skill']:+.1f}%"
                                  for r in good[-2:])
                w(f'   {tf:<5}most: {best}')
                w(f'   {"":5}least: {worst}')
    # ------------------------------------------------------------------ 7
    cg = ui.get('cond') or {}
    if cg:
        w('\n7. THE CONDITIONAL TABLES (Phase C) - lift over each baseline, keys chosen on 2018-2023')
        w('   direction: regime x momentum x leg position | regime: regime x momentum x volatility')
        w('   trade odds: volatility x session x regime, over the baseline replayed at each trade\'s cost')
        w('   Brier skill vs the baseline; PROMOTED = interval clears zero every year AND calibration')
        w('   error <= 3pp. What is not promoted is shown in the lab as "~ baseline", numbers visible.')
        w(f"   {'tf':<5}{'output':<22}" + ''.join(f'{y:>22}' for y in years) + '  verdict')
        for tf, g in cg.items():
            for key, label in (('direction', f"direction @{H.get(tf)}"), ('state', 'regime @H')):
                gg = g.get(key)
                if not gg:
                    continue
                cells = []
                for yr in years:
                    r = gg['years'].get(yr) or {}
                    sk, lo, hi = r.get('skill'), r.get('lo'), r.get('hi')
                    cells.append(f'{100 * sk:>+8.2f}% [{100 * lo:+.2f},{100 * hi:+.2f}]'.rjust(22)
                                 if sk is not None and lo is not None else f"{'-':>22}")
                w(f"   {tf:<5}{label:<22}" + ''.join(cells)
                  + f"  {'PROMOTED' if gg.get('promoted') else 'baseline kept'}")
            tg = g.get('trade')
            if tg:
                for name in ('tpl_buy_1atr_1r', 'tpl_sell_1atr_1r', 'tpl_buy_1atr_2r', 'tpl_sell_1atr_2r'):
                    gg = tg['templates'].get(name) or {}
                    cells = []
                    for yr in years:
                        r = (gg.get('years') or {}).get(yr) or {}
                        sk, lo, hi = r.get('skill'), r.get('lo'), r.get('hi')
                        cells.append(f'{100 * sk:>+8.2f}% [{100 * lo:+.2f},{100 * hi:+.2f}]'.rjust(22)
                                     if sk is not None and lo is not None else f"{'-':>22}")
                    w(f"   {tf:<5}{name[4:]:<22}" + ''.join(cells)
                      + f"  {'PROMOTED' if gg.get('promoted') else 'baseline kept'}")
    w('\nFiles: manifest.json, ui_summary.json (what the lab shows), scorecard.parquet (every '
      'output, every step k - the decay curves), conditional.parquet, forecasts_<tf>.parquet '
      '(one row per forecast).')
    return '\n'.join(out)


def _tpl_cells(sel, tf, name, years) -> str:
    cells = []
    for yr in years:
        r = sel(tf=tf, output=name, forecaster='climatology', year=yr)
        if len(r):
            r = r.iloc[0]
            # class 0 of the 3-class outcome (target, stop, neither) is "target first"
            cells.append(f"{_pct(r['mean_p_c0']).strip():>9} /{_pct(r['freq_c0']).strip():>7}")
        else:
            cells.append(f"{'-':>18}")
    return ''.join(cells)


def _skill_cell(r) -> str:
    if not len(r):
        return f"{'-':>22}"
    r = r.iloc[0]
    s, lo, hi = r.get('skill'), r.get('ci_lo'), r.get('ci_hi')
    if s is None or s != s:
        return f"{'-':>22}"
    if lo is None or lo != lo:
        return f'{100 * s:>+8.2f}%{"":>13}'
    return f'{100 * s:>+8.2f}% [{100 * lo:+.2f},{100 * hi:+.2f}]'.rjust(22)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--symbol', default=None, help='the instrument, default the forecast\'s own (gold)')
    ap.add_argument('--tfs', default=None, help='comma list, default all forecast timeframes')
    ap.add_argument('--trade-tfs', default=None, help='comma list, default 5m,15m')
    ap.add_argument('--years', default=None, help='comma list, default 2024,2025,2026')
    ap.add_argument('--reps', type=int, default=400)
    ap.add_argument('--no-rows', action='store_true', help='skip the per-forecast parquet files')
    ap.add_argument('--report', default=None, help='only (re)write the report of a run dir')
    a = ap.parse_args()

    if a.report:
        d = Path(a.report)
        txt = report(d)
        (d / 'report.txt').write_text(txt, encoding='utf-8')
        print(txt)
        return 0

    from tools.forecast_build import _below_normal
    _below_normal()                  # the live API and the bridge keep first call on the CPU
    from server.forecast import SYMBOL, TFS, TRADE_TFS
    from server.forecast.batch import EVAL_YEARS, Run
    symbol = a.symbol or SYMBOL
    tfs = a.tfs.split(',') if a.tfs else TFS
    trade_tfs = a.trade_tfs.split(',') if a.trade_tfs is not None else TRADE_TFS
    trade_tfs = [x for x in trade_tfs if x]
    years = [int(y) for y in a.years.split(',')] if a.years else EVAL_YEARS
    t0 = datetime.now(timezone.utc)
    print(f'batch: {symbol} {",".join(tfs)} market, {",".join(trade_tfs) or "no"} trade, '
          f'years {",".join(map(str, years))}', flush=True)
    run = Run(symbol=symbol, tfs=tfs, trade_tfs=trade_tfs, eval_years=years, reps=a.reps,
              log=lambda m: print(m, flush=True)).run(keep_rows=not a.no_rows)
    txt = report(run.dir)
    (run.dir / 'report.txt').write_text(txt, encoding='utf-8')
    print(txt)
    print(f'\n-> {run.dir.relative_to(ROOT)}  ({(datetime.now(timezone.utc) - t0).seconds}s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
