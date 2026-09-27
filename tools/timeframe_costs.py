#!/usr/bin/env python
"""
tools/timeframe_costs.py - what costs take, and what is left, on each timeframe. BACKTEST ONLY.

The exit study (tools/exit_replay.py) found the 5m entries beat random by only
~0.02R while costs take ~0.05-0.08R a trade. On a higher timeframe the stop is
wider, so the same spread and commission are a smaller share of it - this
measures whether that leaves the playbooks anything.

    python tools/timeframe_costs.py

TRADES   headless backtest-lab runs (tools/trade_journal_backfill.py lab
         --tf <tf>): XAUUSD.a, 2018-2026, today's live settings, the live
         executor and exit, minute-by-minute simulated broker. The same code
         and settings on every timeframe - nothing is tuned per timeframe.

PER TRADE
    risk      |fill - initial stop|, the 1R
    net R     profit / money at risk (the lab's own P&L: spread, slippage,
              commission all in)
    cost R    what trading it cost, in R: round-trip commission + the spread
              at the fill minute + slippage (3 points on a market or stop
              entry, 3 on a stop-loss exit), all over the risk
    gross R   net R + cost R - the entries' result before costs

READING, fixed 2026-09-27 before any higher-timeframe result was read
    A timeframe is WORTH A CLOSER LOOK only if its mean net R is positive in
    2018-2023 AND the 90% day-block interval of its 2024-2026 mean is above 0.
    Seven timeframes are compared, so one passing is a reason for a
    pre-registered follow-up (per playbook, then the shadow log), not a switch.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = ROOT / 'runs' / 'research' / 'journal'
OUT = SRC / 'timeframe_costs.txt'
SYMBOL = 'XAUUSD.a'
TFS = ['1m', '3m', '5m', '15m', '30m', '1h', '4h']
YEARS = list(range(2018, 2027))
DEV, CONFIRM = range(2018, 2024), range(2024, 2027)
SLIP_POINTS = 3.0


def load(tf: str, prefix: str = None) -> list:
    """The year-by-year lab runs of one timeframe: the `lab` runs, or with
    `prefix` the `window` runs tagged <prefix>-<year> (win_<prefix>-<year>_<tf>.json)."""
    rows = []
    for y in YEARS:
        f = (SRC / f'win_{prefix}-{y}_{tf}.json') if prefix else (SRC / f'lab_{SYMBOL}_{tf}_{y}.json')
        if f.exists():
            doc = json.loads(f.read_text(encoding='utf-8'))
            rows += [x for x in doc['journal'] if x.get('outcome') != 'manual'
                     and x.get('fill_ms') and x.get('profit') is not None]
    return rows


def enrich(rows: list, m1, spec) -> list:
    point = float(spec.get('point') or 0.01)
    tick = float(spec.get('tick_size') or point)
    per_price = float(spec.get('tick_value') or 1.0) / tick
    comm = float(spec.get('commission_per_lot_side') or 0.0)
    sp = m1.spread
    out = []
    for x in rows:
        risk = abs(float(x['fill_price']) - float(x['stop']))
        lots = float(x.get('lots') or 0)
        if not risk > 0 or not lots > 0:
            continue
        j = min(int(np.searchsorted(m1.t, int(x['fill_ms']), 'right')) - 1, len(m1) - 1)
        spread = float(sp[j]) * point if sp is not None and j >= 0 else 20 * point
        slips = (x.get('kind') in ('market', 'stop')) + (x.get('outcome') in ('stop', 'trail'))
        cost = 2 * comm / (risk * per_price) + spread / risk + slips * SLIP_POINTS * point / risk
        net = float(x['profit']) / (risk * per_price * lots)
        out.append({'year': datetime.fromtimestamp(int(x['fill_ms']) / 1000, timezone.utc).year,
                    'day': int(x['fill_ms']) // 86_400_000, 'playbook': x['playbook'],
                    'net': net, 'cost': cost, 'gross': net + cost,
                    'profit': float(x['profit']), 'exit_ms': int(x.get('exit_ms') or x['fill_ms']),
                    'risk_atr': risk / float(x['atr']) if x.get('atr') else np.nan,
                    'risk_pts': risk / point})
    return out


def ci(rows: list, key='net', reps=2000, seed=3) -> tuple:
    days: dict = {}
    for r in rows:
        days.setdefault(r['day'], []).append(r[key])
    if len(days) < 10:
        return float('nan'), float('nan')
    grp = [np.array(v) for v in days.values()]
    s = np.array([g.sum() for g in grp])
    c = np.array([g.size for g in grp])
    pick = np.random.default_rng(seed).integers(0, len(grp), (reps, len(grp)))
    m = s[pick].sum(1) / c[pick].sum(1)
    return float(np.percentile(m, 5)), float(np.percentile(m, 95))


def max_dd(rows: list) -> float:
    p = np.array([r['profit'] for r in sorted(rows, key=lambda r: r['exit_ms'])])
    if not p.size:
        return 0.0
    eq = np.cumsum(p)
    return float((np.maximum.accumulate(np.r_[0.0, eq])[1:] - eq).max())


WINDOW_TFS = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h']


def window_report(tag: str) -> int:
    """
    One window's runs (tools/trade_journal_backfill.py window --tag <tag>),
    every timeframe side by side. A window this short is descriptive: the
    90% interval says how much of any number is sample size.
    """
    from server.datafeed import load_disk
    from server.lab.data import spec as lab_spec
    spec = lab_spec(SYMBOL)
    m1 = load_disk(SYMBOL, '1m', [datetime.now(timezone.utc).year - 1,
                                  datetime.now(timezone.utc).year])
    L = []
    w = L.append
    meta = {}
    data = {}
    for tf in WINDOW_TFS:
        files = sorted(SRC.glob(f'win_{tag}*_{tf}.json'))
        if not files:
            continue
        docs = [json.loads(f.read_text(encoding='utf-8')) for f in files]
        rows = [x for d in docs for x in d['journal']
                if x.get('outcome') != 'manual' and x.get('fill_ms') and x.get('profit') is not None]
        data[tf] = enrich(rows, m1, spec)
        meta[tf] = {'start': min(d['start'] for d in docs), 'end': max(d['end'] for d in docs),
                    'filter': docs[0].get('forecast_filter'),
                    'active': all(d.get('filter_active') for d in docs),
                    'bars': sum(d.get('bars', 0) for d in docs)}
    if not data:
        raise SystemExit(f'no runs tagged {tag} in {SRC}')
    any_meta = next(iter(meta.values()))
    w(f"COST SHARE, PROFIT AND EXPECTANCY BY TIMEFRAME - {SYMBOL}, backtest lab, live settings, "
      f"{any_meta['start']} to {any_meta['end']}, forecast filter {any_meta['filter'] or 'off'}, "
      f"news blackout off   {datetime.now():%Y-%m-%d %H:%M}")
    w('Per trade, in R (1R = fill to initial stop). gross = before costs; cost = commission + '
      'spread + slippage; net = the lab\'s P&L. Profit at the lab\'s lot size (0.01 gold).')
    w('Three months is a small sample: read the interval before the mean.')
    w(f"\n{'tf':<5}{'filter':>9}{'trades':>8}{'win%':>6}{'stop':>11}{'gross R':>9}{'cost R':>8}"
      f"{'cost/stop':>10}{'net R':>8}{'90% interval':>19}{'profit':>9}{'max DD':>9}{'PF':>6}")
    for tf, rows in data.items():
        lo, hi = ci(rows)
        cost = np.array([r['cost'] for r in rows]) if rows else np.array([np.nan])
        p = np.array([r['profit'] for r in rows])
        pf = p[p > 0].sum() / -p[p < 0].sum() if (p < 0).any() else float('nan')
        filt = 'on' if meta[tf]['active'] else ('n/a' if meta[tf]['filter'] else 'off')
        if not rows:
            w(f'{tf:<5}{filt:>9}{0:>8}')
            continue
        w(f"{tf:<5}{filt:>9}{len(rows):>8}{np.mean([r['net'] > 0 for r in rows]) * 100:>6.1f}"
          f"{np.nanmedian([r['risk_pts'] for r in rows]):>7.0f}pts"
          f"{np.mean([r['gross'] for r in rows]):>+9.3f}{np.nanmean(cost):>8.3f}"
          f"{np.nanmean(cost) * 100:>9.1f}%{np.mean([r['net'] for r in rows]):>+8.3f}"
          f"   [{lo:+.3f}, {hi:+.3f}]{p.sum():>+9.2f}{max_dd(rows):>9.2f}{pf:>6.2f}")
    w("\nfilter: on = the forecast store exists for this timeframe and the filter could veto; "
      "n/a = asked for, but no forecast store - these ran UNFILTERED.")
    w('\nPer playbook, net R per trade (n):')
    pbs = sorted({r['playbook'] for rows in data.values() for r in rows})
    w(f"  {'':<18}" + ''.join(f'{tf:>14}' for tf in data))
    for pb in pbs:
        cells = []
        for tf, rows in data.items():
            v = [r['net'] for r in rows if r['playbook'] == pb]
            cells.append(f"{(f'{np.mean(v):+.3f}({len(v)})' if v else '-'):>14}")
        w(f'  {pb:<18}' + ''.join(cells))
    txt = '\n'.join(L)
    out = SRC / f'timeframe_costs_{tag}.txt'
    out.write_text(txt, encoding='utf-8')
    print(txt)
    return 0


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == '--window':
        return window_report(sys.argv[2])
    prefix = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == '--prefix' else None
    from server.datafeed import load_disk
    from server.lab.data import spec as lab_spec
    spec = lab_spec(SYMBOL)
    m1 = load_disk(SYMBOL, '1m', list(range(2017, datetime.now(timezone.utc).year + 1)))
    data = {tf: enrich(load(tf, prefix), m1, spec) for tf in (WINDOW_TFS if prefix else TFS)}
    data = {tf: v for tf, v in data.items() if v}

    L = []
    w = L.append
    w(f'COST SHARE, PROFIT AND EXPECTANCY BY TIMEFRAME - {SYMBOL}, backtest lab, live settings, '
      f'2018-2026   {datetime.now():%Y-%m-%d %H:%M}')
    w('Per trade, in R (1R = fill to initial stop). cost = commission + spread + slippage. '
      'gross = before costs. Profit at the lab\'s lot size (0.01 gold).')
    if prefix:
        w(f'Runs tagged {prefix}: forecast filter no_transition (it acts only where a forecast '
          'store exists - 5m, 15m, 1h, 4h; the others ran unfiltered), news blackout off.')
    w('Reading fixed in advance: worth a closer look = net R > 0 in 2018-23 AND 2024-26 '
      'interval above 0.')
    w(f"\n{'tf':<5}{'trades':>7}{'/yr':>6}{'win%':>6}{'stop':>12}{'gross R':>9}{'cost R':>8}"
      f"{'cost/stop':>10}{'net R':>8}{'  2018-23':>10}{'  2024-26 [90%]':>25}{'profit/yr':>11}"
      f"{'max DD':>9}   verdict")
    for tf, rows in data.items():
        yrs = sorted({r['year'] for r in rows})
        dev = [r for r in rows if r['year'] in DEV]
        cf = [r for r in rows if r['year'] in CONFIRM]
        lo, hi = ci(cf)
        mdev = np.mean([r['net'] for r in dev]) if dev else np.nan
        mcf = np.mean([r['net'] for r in cf]) if cf else np.nan
        worth = mdev > 0 and lo > 0
        cost = np.array([r['cost'] for r in rows])
        w(f"{tf:<5}{len(rows):>7}{len(rows) / max(1, len(yrs)):>6.0f}"
          f"{np.mean([r['net'] > 0 for r in rows]) * 100:>6.1f}"
          f"{np.nanmedian([r['risk_pts'] for r in rows]):>8.0f}pts"
          f"{np.mean([r['gross'] for r in rows]):>+9.3f}{cost.mean():>8.3f}"
          f"{cost.mean() * 100:>9.1f}%{np.mean([r['net'] for r in rows]):>+8.3f}"
          f"{mdev:>+10.3f}{mcf:>+9.3f} [{lo:+.3f}, {hi:+.3f}]"
          f"{sum(r['profit'] for r in rows) / max(1, len(yrs)):>+11.2f}{max_dd(rows):>9.2f}"
          f"   {'WORTH A CLOSER LOOK' if worth else '-'}")

    w('\nNet R per trade by year:')
    w('      ' + ''.join(f'{tf:>9}' for tf in data))
    for y in YEARS:
        cells = []
        for tf, rows in data.items():
            v = [r['net'] for r in rows if r['year'] == y]
            cells.append(f'{np.mean(v):>+9.3f}' if v else f'{"-":>9}')
        w(f'{y}  ' + ''.join(cells))
    w('\nGross R (before costs) by year - the entries\' own edge:')
    w('      ' + ''.join(f'{tf:>9}' for tf in data))
    for y in YEARS:
        cells = []
        for tf, rows in data.items():
            v = [r['gross'] for r in rows if r['year'] == y]
            cells.append(f'{np.mean(v):>+9.3f}' if v else f'{"-":>9}')
        w(f'{y}  ' + ''.join(cells))
    w('\nProfit per year at 0.01 lots:')
    w('      ' + ''.join(f'{tf:>9}' for tf in data))
    for y in YEARS:
        cells = []
        for tf, rows in data.items():
            v = [r['profit'] for r in rows if r['year'] == y]
            cells.append(f'{sum(v):>+9.0f}' if v else f'{"-":>9}')
        w(f'{y}  ' + ''.join(cells))
    w('\nPer playbook, net R per trade (n) - 2018-23 | 2024-26:')
    pbs = sorted({r['playbook'] for rows in data.values() for r in rows})
    for pb in pbs:
        cells = []
        for tf, rows in data.items():
            d = [r['net'] for r in rows if r['playbook'] == pb and r['year'] in DEV]
            c = [r['net'] for r in rows if r['playbook'] == pb and r['year'] in CONFIRM]
            f = lambda v: f'{np.mean(v):+.3f}({len(v)})' if v else '-'      # noqa: E731
            cells.append(f'{tf}: {f(d)} | {f(c)}')
        w(f'  {pb:<18} ' + '   '.join(cells))
    txt = '\n'.join(L)
    (SRC / f'timeframe_costs{"_" + prefix if prefix else ""}.txt').write_text(txt, encoding='utf-8')
    print(txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
