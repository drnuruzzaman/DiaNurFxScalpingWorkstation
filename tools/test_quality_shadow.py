#!/usr/bin/env python
"""
tools/test_quality_shadow.py - the C1 shadow log says what the engine's C1 rule would do.

    python tools/test_quality_shadow.py

1. On real XAUUSD 5m bars, server/quality_shadow.c1_sides() agrees with
   pb_pattern_break itself: the sides it lists are the sides pb_pattern_break
   trades with C1 off, and the sides it passes are the ones it still trades
   with CONFIG.quality.pattern_classic_only on.
2. The hooks: a pattern_break send is logged pass / veto / abstain on the
   signal's own bar, anything else is ignored, and bad input never raises.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import CONFIG, MTF_LADDER, QualitySettings      # noqa: E402
from server.datafeed import load_disk                             # noqa: E402
from server.engine.analysis import analyse, quick_trend           # noqa: E402
from server.engine.signals import pb_pattern_break                # noqa: E402
from server.lab.data import spec as lab_spec                      # noqa: E402
from server import quality_shadow as qs                           # noqa: E402

PASSED, FAILED = [], []


def check(name, ok, detail=''):
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def agreement(n_bars: int = 400) -> None:
    sym, tf = 'XAUUSD.a', '5m'
    spec = lab_spec(sym)
    s = load_disk(sym, tf, [2026])
    htf = {x: load_disk(sym, x, [2025, 2026]) for x in dict.fromkeys(MTF_LADDER[tf]) if x != tf}
    seen = agree = with_pattern = vetoed = 0
    for i in range(len(s) - n_bars, len(s)):
        w = s.slice(i - 600, i)
        bt = float(s.t[i - 1])
        mtf = {}
        for name, h in htf.items():
            j = int(np.searchsorted(h.t, bt, 'right'))
            hw = h.slice(max(0, j - 300), j)
            if len(hw) >= 60:
                mtf[name] = quick_trend(hw)
        snap = analyse(w, mtf, spec)
        if not snap.get('ok'):
            continue
        seen += 1
        sides = qs.c1_sides(snap)
        CONFIG.quality = QualitySettings()
        off = {x.side for x in pb_pattern_break(snap, w)}
        CONFIG.quality = QualitySettings(pattern_classic_only=True)
        on = {x.side for x in pb_pattern_break(snap, w)}
        CONFIG.quality = QualitySettings()
        mine_off = set(sides)
        mine_on = {k for k, v in sides.items() if v['pass']}
        agree += (mine_off == off and mine_on == on)
        with_pattern += bool(off)
        vetoed += len(off - on)
    check('c1_sides matches pb_pattern_break with C1 off and on', agree == seen,
          f'{agree}/{seen} bars, {with_pattern} with a pattern_break, {vetoed} sides vetoed by C1')
    check('the sample exercises both verdicts', with_pattern > 0 and vetoed > 0)


def hooks() -> None:
    tmp = Path(tempfile.mkdtemp()) / 'shadow_quality.jsonl'
    qs.LOG = tmp
    pat = lambda kind, q, d='bearish': {                                   # noqa: E731
        'kind': kind, 'quality': q, 'direction': d, 'actionable': True, 'status': 'forming',
        'break_level': 100.0, 'invalidation': 102.0 if d == 'bearish' else 98.0}
    snap = {'ok': True, 'atr': 1.0, 'price': 100.5, 'bar_time_ms': 1000,
            'patterns': [pat('rising_wedge', 80), pat('double_top', 70)]}
    qs.remember_closed('XAUUSD.a', '5m', snap)
    snap2 = dict(snap, bar_time_ms=2000, patterns=[pat('double_top', 60), pat('rising_wedge', 90)])
    qs.remember_closed('XAUUSD.a', '5m', snap2)
    base = {'symbol': 'XAUUSD.a', 'tf': '5m', 'side': 'sell', 'playbook': 'pattern_break'}
    qs.note_send(dict(base, id='a', final_bar_ms=1000))
    qs.note_send(dict(base, id='b', final_bar_ms=2000))
    qs.note_send(dict(base, id='c', final_bar_ms=3000))
    qs.note_send(dict(base, id='d', final_bar_ms=1000, side='buy'))
    qs.note_send(dict(base, id='e', final_bar_ms=1000, playbook='mtf_pullback'))
    qs.note_send(dict(base, id='f', final_bar_ms=1000, tf='15m'))
    qs.note_send({'playbook': 'pattern_break', 'tf': '5m', 'final_bar_ms': 'junk'})
    qs.remember_closed('XAUUSD.a', '5m', {'ok': True, 'patterns': 'junk', 'atr': 1})
    rows = [json.loads(x) for x in tmp.read_text(encoding='utf-8').splitlines()]
    got = {r['id']: r['verdict'] for r in rows}
    check('classic pattern >= 65 behind the signal passes', got.get('a') == 'pass')
    check('only a classic pattern under 65 is vetoed', got.get('b') == 'veto')
    check('a bar with no verdict kept abstains', got.get('c') == 'abstain')
    check('a side with no pattern abstains', got.get('d') == 'abstain')
    check('other playbooks and timeframes are not logged', 'e' not in got and 'f' not in got)
    check('bad input never raises and writes nothing', len(rows) == 4, f'{len(rows)} rows')


def main() -> int:
    print('C1 shadow log')
    hooks()
    agreement()
    print(f'\n  {len(PASSED)} passed, {len(FAILED)} failed')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
