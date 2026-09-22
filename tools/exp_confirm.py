#!/usr/bin/env python
"""
tools/exp_confirm.py - the real backtest for an entry rule the screen liked.

tools/exp_entries.py filters signals after resolve(); this runs
backtest.run(playbooks=...) so a disabled playbook is never generated at all,
and confluence and conflict resolution happen as they would live. Same
accounting as the Backtest tab (commission, no spread) - compare runs of this
script with each other, not with exp_exits.py's spread-charged numbers.

Usage:
    python tools/exp_confirm.py --start 2025-01-01 --end 2025-08-31 --set all
    python tools/exp_confirm.py --start 2025-01-01 --end 2025-08-31 --set kept
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import CONFIG                                # noqa: E402
from server.engine import backtest as bt                        # noqa: E402

SETS = {
    # Named explicitly: None would now mean "all ENABLED playbooks".
    'all': ['mtf_pullback', 'sweep_reversal', 'breakout_retest', 'false_break_fade',
            'pattern_break', 'range_fade', 'flag_continuation'],
    # the screen's winner: drop sweep_reversal, breakout_retest, range_fade
    'kept': ['mtf_pullback', 'flag_continuation', 'pattern_break', 'false_break_fade'],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--set', choices=list(SETS), required=True)
    a = ap.parse_args()
    ms = lambda s: int(dt.datetime.fromisoformat(s).timestamp() * 1000)  # noqa: E731
    r = bt.run('XAUUSD.a', '5m', start_ms=ms(a.start), end_ms=ms(a.end),
               equity=25000, step=1, playbooks=SETS[a.set])
    if not r.get('ok'):
        print('failed:', r.get('error'))
        return 1
    s = r['summary']
    out = {'set': a.set, 'start': a.start, 'end': a.end, 'exit_mode': CONFIG.risk.exit_mode,
           **{k: s[k] for k in ('trades', 'win_rate', 'profit_factor', 'expectancy_r',
                                 'net_pnl', 'return_pct', 'max_drawdown_pct')},
           'by_playbook': {k: {'n': v['trades'], 'exp': v['expectancy_r']}
                           for k, v in r['by_playbook'].items()}}
    Path(ROOT / 'order_ledger').mkdir(exist_ok=True)
    (ROOT / 'order_ledger' / f'confirm_{a.set}_{a.start[:4]}.json').write_text(json.dumps(out, indent=1))
    print(json.dumps(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
