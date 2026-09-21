#!/usr/bin/env python
"""
tools/exp_oos.py - out-of-sample check for the pattern_break regime gate.

The gating rule (allow pattern_break only in trend and range) was chosen from
2026's own cross-tab. That is in-sample selection: pick the buckets that
happened to win and you will always "improve" the result. The rule is only
worth adopting if it also holds on data it was not derived from.

This runs the same three arms on a DIFFERENT year, with the gate fixed to what
2026 suggested rather than re-derived. If the ordering survives, the rule is
picking up something real about the regimes. If it inverts, it was noise.

Usage:
    python tools/exp_oos.py --year 2025
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.engine import backtest as bt                      # noqa: E402
from tools.exp_pattern_break import (cross_tab, install_arm,  # noqa: E402
                                     pf, restore, summarise)

# Fixed, NOT re-derived on this year's data. That is the whole point.
GATE_FROM_2026 = ['trend', 'range']


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--year', type=int, default=2025)
    ap.add_argument('--months', type=int, default=8)
    ap.add_argument('--step', type=int, default=3)
    ap.add_argument('--tf', default='5m')
    args = ap.parse_args()

    start = dt.datetime(args.year, 1, 1)
    end = start + dt.timedelta(days=int(args.months * 30.44))
    window = dict(symbol='XAUUSD.a', tf=args.tf,
                  start_ms=int(start.timestamp() * 1000),
                  end_ms=int(end.timestamp() * 1000),
                  equity=25000, step=args.step)

    print(f'OUT-OF-SAMPLE  {start:%Y-%m-%d} -> {end:%Y-%m-%d}  {args.tf}')
    print(f'gate fixed from 2026: pattern_break allowed in {GATE_FROM_2026}\n')

    rows = []
    install_arm('baseline')
    ra = bt.run(**window)
    if not ra.get('ok'):
        print('baseline failed:', ra.get('error'))
        return 1
    rows.append(('A base', summarise('A  BASELINE (exempt everywhere)', ra)))
    cross_tab(ra, 'pattern_break')

    install_arm('dropped')
    rows.append(('B dropped', summarise('B  DROPPED', bt.run(**window))))

    install_arm('gated', GATE_FROM_2026)
    rc = bt.run(**window)
    rows.append(('C gated', summarise(f'C  GATED ({", ".join(GATE_FROM_2026)})', rc)))
    cross_tab(rc, 'pattern_break')
    restore()

    print('\n' + '=' * 72)
    print(f'  OUT-OF-SAMPLE VERDICT ({args.year})')
    print('=' * 72)
    print(f"  {'arm':10s} {'trades':>7s} {'win%':>7s} {'PF':>7s} {'expR':>9s} "
          f"{'return%':>9s} {'maxDD%':>8s}")
    for name, s in rows:
        print(f"  {name:10s} {s['trades']:7d} {s['win_rate']:7.1f} "
              f"{pf(s['profit_factor']):>7s} {s['expectancy_r']:+9.3f} "
              f"{s['return_pct']:+9.2f} {s['max_drawdown_pct']:8.2f}")

    best = max(rows, key=lambda r: r[1]['expectancy_r'])[0]
    print(f'\n  best by expectancy: {best}')
    print('  the 2026 rule holds' if best == 'C gated'
          else '  the 2026 rule does NOT reproduce here - treat it as in-sample noise')
    return 0


if __name__ == '__main__':
    sys.exit(main())
