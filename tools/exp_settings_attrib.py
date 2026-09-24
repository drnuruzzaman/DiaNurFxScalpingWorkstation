#!/usr/bin/env python
"""
tools/exp_settings_attrib.py - which live setting changes the backtest?

The code defaults and configs/settings.json differ in five settings that
decide which signals become trades. Together they turn a roughly flat system
into one that loses ~80% in two of three tested years. This attributes it.

Both directions, because settings interact:
  ADD     start from the defaults, switch ON one live setting at a time
  REVERT  start from live, switch OFF one live setting at a time (back to default)

Same captured signals (tools/exp_exits.py captures), same exit (today's live
trail), same costs. Only the setting varies. Nothing live is touched.

    python tools/exp_settings_attrib.py
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import exp_exits as X                                              # noqa: E402
from server.config import CONFIG                                   # noqa: E402

LABELS = [('is2026', '2026'), ('oos2025', '2025'), ('hold2024', '2024')]

DEFAULTS = {
    'disabled_playbooks': ('sweep_reversal', 'breakout_retest', 'range_fade'),
    'min_rr': 1.5,
    'cooldown_bars': 5,
    'max_daily_trades': 12,
    'max_daily_loss_pct': 2.0,
}
LIVE = {
    'disabled_playbooks': (),
    'min_rr': 1.0,
    'cooldown_bars': 2,
    'max_daily_trades': 500,
    'max_daily_loss_pct': 5.0,
}
NAMES = {
    'disabled_playbooks': 'playbooks: 3 re-enabled',
    'min_rr': 'reward floor 1.5 -> 1.0',
    'cooldown_bars': 'cooldown 5 -> 2 bars',
    'max_daily_trades': 'daily trades 12 -> 500',
    'max_daily_loss_pct': 'daily loss 2% -> 5%',
}

EXIT = X.Exit('live trail', partial=0.0, first_r=1.0, final='tp2',
              be_lock_r=0.5, trail_atr=1.0)


def apply(cfg: dict) -> None:
    CONFIG.gates.disabled_playbooks = tuple(cfg['disabled_playbooks'])
    CONFIG.risk.min_rr = cfg['min_rr']
    CONFIG.gates.cooldown_bars = cfg['cooldown_bars']
    CONFIG.risk.max_daily_trades = cfg['max_daily_trades']
    CONFIG.risk.max_daily_loss_pct = cfg['max_daily_loss_pct']


def run(cap, series, m1, cfg):
    apply(cfg)
    off = set(cfg['disabled_playbooks'])
    c2 = dict(cap, candidates=[c for c in cap['candidates'] if c['playbook'] not in off])
    return X.stats(X.evaluate(c2, series, m1, X.gate_current, EXIT))


def cell(s):
    if s is None:
        return f'{"-":>24}'
    return f'{s["n"]:>5} {s["sum"]:>+7.0f}R {s["ret"]:>+6.0f}% {s["dd"]:>3.0f}%'


def main() -> int:
    data = {}
    for label, year in LABELS:
        cap = pickle.loads((X.OUT / f'exp_exits_{label}.pkl').read_bytes())
        data[year] = (cap, *X.load_period(cap))

    rows = [('DEFAULTS (all off)', dict(DEFAULTS))]
    for k in DEFAULTS:
        rows.append((f'ADD     {NAMES[k]}', {**DEFAULTS, k: LIVE[k]}))
    rows.append(('LIVE (all on)', dict(LIVE)))
    for k in LIVE:
        rows.append((f'REVERT  {NAMES[k]}', {**LIVE, k: DEFAULTS[k]}))

    head = '  '.join(f'{y:^24}' for y in data)
    sub = '  '.join(f'{"trades":>5} {"sum R":>8} {"ret":>7} {"DD":>3} ' for _ in data)
    print(f'{"":<36}{head}\n{"":<36}{sub}')
    for name, cfg in rows:
        cells = '  '.join(cell(run(*data[y], cfg)) for y in data)
        if name.startswith('LIVE'):
            print()
        print(f'{name:<36}{cells}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
