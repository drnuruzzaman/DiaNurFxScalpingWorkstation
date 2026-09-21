#!/usr/bin/env python
"""
tools/exp_entries.py - which playbooks earn their place.

Runs on the candidates tools/exp_exits.py already captured, with the adopted
exit (lock +0.3R at 1R, trail 1.0 ATR) and today's reward gate. Dropping a
playbook here filters signals AFTER resolve(), so confluence bonuses and
conflicts it took part in are not re-computed - a screen, not a verdict. The
winner gets a real backtest.run(playbooks=...) before anything is adopted, and
a year it was not selected on (2024).

Usage:
    python tools/exp_entries.py --labels is2026 oos2025 [hold2024]
"""
from __future__ import annotations

import argparse
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.exp_exits import (EXITS, HEAD, OUT, _row, evaluate,  # noqa: E402
                             gate_current, load_period, stats)

ALL = ['breakout_retest', 'false_break_fade', 'flag_continuation', 'mtf_pullback',
       'pattern_break', 'range_fade', 'sweep_reversal']


def hour(c):
    return time.gmtime(c['bar_t'] / 1000).tm_hour


def drop(*names):
    return lambda c, conf: c['playbook'] not in names


def only(*names):
    return lambda c, conf: c['playbook'] in names


def sweep_min_conf(x):
    return lambda c, conf: c['playbook'] != 'sweep_reversal' or conf >= x


def sweep_hours(a, b):
    return lambda c, conf: c['playbook'] != 'sweep_reversal' or a <= hour(c) < b


ARMS = [
    ('all playbooks (today)', None),
    ('- sweep_reversal', drop('sweep_reversal')),
    ('- breakout_retest', drop('breakout_retest')),
    ('- range_fade', drop('range_fade')),
    ('- sweep, breakout, range_fade', drop('sweep_reversal', 'breakout_retest', 'range_fade')),
    ('- breakout, range_fade', drop('breakout_retest', 'range_fade')),
    ('sweep_reversal conf >= 70', sweep_min_conf(70)),
    ('sweep_reversal conf >= 75', sweep_min_conf(75)),
    ('sweep_reversal 07-17 UTC only', sweep_hours(7, 17)),
    ('sweep conf>=70, - breakout, range_fade',
     lambda c, conf: drop('breakout_retest', 'range_fade')(c, conf)
     and sweep_min_conf(70)(c, conf)),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--labels', nargs='+', required=True)
    a = ap.parse_args()
    ex = EXITS['T4']
    for label in a.labels:
        cap = pickle.loads((OUT / f'exp_exits_{label}.pkl').read_bytes())
        series, m1 = load_period(cap)
        print(f'\n=== {label} ===   exit: {ex.name}   gate: TP2 >= 1.5')
        print(HEAD)
        for name, rule in ARMS:
            print(_row(name, stats(evaluate(cap, series, m1, gate_current, ex, admit=rule))))
        # where sweep_reversal loses: regime and session
        sw = evaluate(cap, series, m1, gate_current, ex, admit=only('sweep_reversal'))
        idx = {(c['i'], c['side']): c for c in cap['candidates'] if c['playbook'] == 'sweep_reversal'}
        by_reg, by_hr = {}, {}
        for t in sw:
            i = int(series.t.searchsorted(t['entry_t']))
            c = idx.get((i, t['side']))
            if c is None:
                continue
            by_reg.setdefault(c['regime'] or '?', []).append(t)
            by_hr.setdefault(hour(c) // 4 * 4, []).append(t)
        print('  sweep_reversal by regime:',
              '  '.join(f'{k} {len(v)}/{sum(x["net_r"] for x in v):+.1f}R' for k, v in sorted(by_reg.items())))
        print('  sweep_reversal by 4h UTC block:',
              '  '.join(f'{k:02d}h {len(v)}/{sum(x["net_r"] for x in v):+.1f}R' for k, v in sorted(by_hr.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
