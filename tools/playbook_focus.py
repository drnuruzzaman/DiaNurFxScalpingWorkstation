#!/usr/bin/env python
"""
tools/playbook_focus.py - which playbook, on which timeframe, is worth focusing on? BACKTEST ONLY.

    python tools/playbook_focus.py [--prefix all7]

RUNS     headless backtest-lab runs, one per timeframe-year
         (tools/trade_journal_backfill.py window --tag <prefix>-<year>
         --filter no_transition --all-playbooks): XAUUSD.a, 2018-2026,
         today's live settings EXCEPT that all seven playbooks are on (live
         disables sweep_reversal, breakout_retest, range_fade), forecast filter
         no_transition (it can act only on 5m, 15m, 1h, 4h - the timeframes
         with a forecast store), news blackout off, the live executor and exit,
         minute-by-minute simulated broker.

For every playbook x timeframe:

  RESULT   trades, win rate, R before costs, cost R, net R (the lab's P&L in
           R), net R in 2018-2023 and in 2024-2026 with its 90% day-block
           interval, profit at 0.01 lots.
  GATES    each signal's FIRST verdict (its gate ledger on the bar it was
           made): how many qualified / watch / rejected, which gates blocked
           them (and how often a gate was the only block), how many went on to
           an order anyway (a verdict is re-taken every bar until the signal
           expires), and how many sends the forecast filter vetoed.

READING, fixed 2026-09-27 before any of these runs was read
    A playbook x timeframe is a CANDIDATE only if it has >= 30 trades in each
    period, its mean net R is above 0 in 2018-2023, and the 90% interval of
    its 2024-2026 mean is above 0. 7 playbooks x 7 timeframes = 49 cells, so
    about two would pass by chance alone: a candidate earns a pre-registered
    forward test (the shadow log), never a switch in live on this evidence.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from timeframe_costs import (SRC, SYMBOL, YEARS, DEV, CONFIRM, _spec, ci, enrich,  # noqa: E402
                             max_dd, tag_pp)

TFS = ['3m', '5m', '15m', '30m', '1h', '2h', '4h']
PLAYBOOKS = ['mtf_pullback', 'sweep_reversal', 'breakout_retest', 'false_break_fade',
             'pattern_break', 'range_fade', 'flag_continuation']
MIN_N = 30


def load(prefix: str, tf: str) -> tuple:
    trades, verdicts, years = [], [], []
    for y in YEARS:
        f = SRC / f'win_{prefix}-{y}_{tf}.json'
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding='utf-8'))
        years.append(y)
        trades += [x for x in tag_pp(d['journal'], _spec()) if x.get('outcome') != 'manual'
                   and x.get('fill_ms') and x.get('profit') is not None]
        verdicts += d.get('verdicts') or []
    return trades, verdicts, years


def gate_table(verdicts: list) -> dict:
    from server.lab.stats import gates
    sent = {v['id'] for v in verdicts if v.get('sent')}
    rows = [{'id': v['id'], 'playbook': v['playbook'],
             'first_qual': {'status': v.get('status'), 'confidence': v.get('confidence'),
                            'gates': v.get('gates') or []}} for v in verdicts]
    from server.config import CONFIG
    return gates(rows, sent, int(CONFIG.gates.min_confidence))


def main() -> int:
    prefix = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == '--prefix' else 'all7'
    from server.datafeed import load_disk
    from server.lab.data import spec as lab_spec
    from server.lab import settings as lab_settings
    lab_settings.activate(lab_settings.effective(None))
    spec = lab_spec(SYMBOL)
    m1 = load_disk(SYMBOL, '1m', list(range(2017, datetime.now(timezone.utc).year + 1)))

    L = []
    w = L.append
    w(f'PLAYBOOK FOCUS - {SYMBOL}, backtest lab, runs "{prefix}", all 7 playbooks on, '
      f'forecast filter no_transition (acts on 5m/15m/1h/4h only), news blackout off   '
      f'{datetime.now():%Y-%m-%d %H:%M}')
    w('Net R = the lab\'s P&L in R (costs in). CANDIDATE = n >= 30 in each period, net > 0 in '
      '2018-23, 2024-26 90% interval above 0 (fixed in advance; ~2 of 49 pass by chance).')

    cells, gate_rows, coverage = [], {}, {}
    for tf in TFS:
        trades, verdicts, years = load(prefix, tf)
        coverage[tf] = years
        if not years:
            continue
        rows = enrich(trades, m1, spec)
        gate_rows[tf] = (verdicts, gate_table(verdicts))
        for pb in PLAYBOOKS:
            r = [x for x in rows if x['playbook'] == pb]
            dev = [x for x in r if x['year'] in DEV]
            cf = [x for x in r if x['year'] in CONFIRM]
            lo, hi = ci(cf)
            md = float(np.mean([x['net'] for x in dev])) if dev else float('nan')
            mc = float(np.mean([x['net'] for x in cf])) if cf else float('nan')
            cand = len(dev) >= MIN_N and len(cf) >= MIN_N and md > 0 and lo > 0
            cells.append({'tf': tf, 'pb': pb, 'n': len(r), 'nd': len(dev), 'nc': len(cf),
                          'win': float(np.mean([x['net'] > 0 for x in r]) * 100) if r else float('nan'),
                          'gross': float(np.mean([x['gross'] for x in r])) if r else float('nan'),
                          'cost': float(np.mean([x['cost'] for x in r])) if r else float('nan'),
                          'net': float(np.mean([x['net'] for x in r])) if r else float('nan'),
                          'md': md, 'mc': mc, 'lo': lo, 'hi': hi,
                          'profit': sum(x['profit'] for x in r), 'dd': max_dd(r) if r else 0.0,
                          'cand': cand})

    w('\nCoverage: ' + '  '.join(f"{tf} {len(y)} yrs" for tf, y in coverage.items()))

    # 1. the ranking
    w('\n1. Every playbook x timeframe, best 2024-26 first (n >= 10 in 2024-26)')
    w(f"  {'tf':<4}{'playbook':<19}{'trades':>7}{'win%':>6}{'gross':>8}{'cost':>7}{'net R':>8}"
      f"{'2018-23':>9}{'2024-26':>9}{'90% interval':>19}{'profit':>10}{'max DD':>9}")
    for c in sorted([c for c in cells if c['nc'] >= 10], key=lambda c: -c['mc']):
        w(f"  {c['tf']:<4}{c['pb']:<19}{c['n']:>7}{c['win']:>6.1f}{c['gross']:>+8.3f}{c['cost']:>7.3f}"
          f"{c['net']:>+8.3f}{c['md']:>+9.3f}{c['mc']:>+9.3f}   [{c['lo']:+.3f}, {c['hi']:+.3f}]"
          f"{c['profit']:>+10.0f}{c['dd']:>9.0f}{'   CANDIDATE' if c['cand'] else ''}")
    thin = [c for c in cells if 0 < c['nc'] < 10]
    if thin:
        w('  fewer than 10 trades in 2024-26 (not ranked): '
          + ', '.join(f"{c['tf']} {c['pb']} ({c['nc']})" for c in thin))
    cands = [c for c in cells if c['cand']]
    w(f"\n  CANDIDATES: {', '.join(c['tf'] + ' ' + c['pb'] for c in cands) if cands else 'none'}")

    # 2. net R grid
    w('\n2. Net R per trade, 2018-23 | 2024-26 (n)')
    w(f"  {'':<19}" + ''.join(f'{tf:>22}' for tf in coverage if coverage[tf]))
    for pb in PLAYBOOKS:
        line = f'  {pb:<19}'
        for tf in coverage:
            if not coverage[tf]:
                continue
            c = next((c for c in cells if c['tf'] == tf and c['pb'] == pb), None)
            txt_c = f"{c['md']:+.2f}|{c['mc']:+.2f}({c['n']})" if c and c['n'] else '-'
            line += f'{txt_c:>22}'
        w(line)

    # 3. gates
    w('\n3. Why signals stopped - first verdicts, per timeframe')
    for tf, (verdicts, g) in gate_rows.items():
        st = g['status']
        filt = sum(1 for v in verdicts if v.get('filtered'))
        w(f"\n  {tf}: {g['n']} signals - qualified {st.get('qualified', 0)}, watch {st.get('watch', 0)}, "
          f"rejected {st.get('rejected', 0)}; {g['sent']} went on to an order; "
          f"forecast filter vetoed sends of {filt}")
        w(f"    {'gate':<11}{'blocked':>8}{'only':>7}{'sent later':>11}{'warned':>8}{'-conf':>6}{'decided':>9}")
        for name, x in list(g['gates'].items())[:10]:
            pen = f"{x['pen_avg']:.0f}" if x['pen_avg'] is not None else '-'
            w(f"    {name:<11}{x['block']:>8}{x['sole']:>7}{x['sent_after_block']:>11}{x['warn']:>8}"
              f"{pen:>6}{x['decided']:>9}")
        w(f"    {'playbook':<19}{'signals':>8}{'qual':>6}{'watch':>7}{'rej':>6}{'sent':>6}{'filt':>6}   top blocks")
        for pb in PLAYBOOKS:
            x = g['playbooks'].get(pb)
            if not x:
                continue
            f_n = sum(1 for v in verdicts if v['playbook'] == pb and v.get('filtered'))
            top = ', '.join(f'{k} {n}' for k, n in sorted(x['blocks'].items(), key=lambda kv: -kv[1])[:4])
            w(f"    {pb:<19}{x['n']:>8}{x.get('qualified', 0):>6}{x.get('watch', 0):>7}"
              f"{x.get('rejected', 0):>6}{x['sent']:>6}{f_n:>6}   {top}")

    # 4. gates across all timeframes, per playbook
    w('\n4. All timeframes together - share of each playbook\'s signals each gate blocked')
    allv = [v for (vs, _) in gate_rows.values() for v in vs]
    names = sorted({gname for v in allv for gname, verdict, _ in (v.get('gates') or [])
                    if verdict == 'BLOCK'})
    w(f"  {'playbook':<19}{'signals':>8}{'sent%':>7}" + ''.join(f'{n:>11}' for n in names))
    for pb in PLAYBOOKS:
        vs = [v for v in allv if v['playbook'] == pb]
        if not vs:
            continue
        sent = sum(1 for v in vs if v.get('sent')) / len(vs) * 100
        cellsg = []
        for n in names:
            k = sum(1 for v in vs if any(gn == n and vd == 'BLOCK' for gn, vd, _ in (v.get('gates') or [])))
            cellsg.append(f'{k / len(vs) * 100:>10.0f}%')
        w(f'  {pb:<19}{len(vs):>8}{sent:>6.0f}%' + ''.join(cellsg))

    txt = '\n'.join(L)
    (SRC / f'playbook_focus_{prefix}.txt').write_text(txt, encoding='utf-8')
    print(txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
