#!/usr/bin/env python
"""
tools/exp_tp2_widen.py - does keeping a structural TP2 on widened stops pay?

The bug: when a stop is too tight, signals._widen_to_noise_floor() pushes it
out to min_stop_atr and re-derives the targets - but used to pass no
structural objective, so every widened signal's TP2 fell back to a flat 2R.
The fix offers the old TP2 back when it was structural.

Only ~1% of signals are widened, so this does NOT recapture three years. It
re-runs the engine (with the fix, as it now is on disk) at exactly the bars
where a widened signal was captured, takes the corrected TP2 for the matching
signal, and swaps it into the existing capture. Every other candidate is left
byte-identical, so the two arms differ in nothing but those TP2s.

Both arms: live settings from configs/settings.json, live trail exit.

    python tools/exp_tp2_widen.py
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import exp_exits as X                                              # noqa: E402
from exp_be_reversal import apply_live_settings                    # noqa: E402
from server.config import CONFIG, MTF_LADDER                       # noqa: E402
from server.datafeed import load_disk                              # noqa: E402
from server.engine.analysis import analyse, quick_trend            # noqa: E402
from server.engine.signals import generate                         # noqa: E402

LABELS = [('is2026', '2026'), ('oos2025', '2025'), ('hold2024', '2024')]


def widened(c) -> bool:
    risk = abs(c['entry'] - c['stop'])
    return c['atr'] > 0 and abs(risk - CONFIG.risk.min_stop_atr * c['atr']) < 1e-6 * max(1.0, c['atr'])


def corrected(cap) -> tuple:
    """Return (patched capture, changed, unchanged, unmatched)."""
    spec = cap['spec']
    series = load_disk(X.SYMBOL, X.TF, None, cap['start_ms'], cap['end_ms'])
    htf = {}
    for name in dict.fromkeys(x for x in MTF_LADDER.get(X.TF, [X.TF]) if x != X.TF):
        s = load_disk(X.SYMBOL, name, None, cap['start_ms'], cap['end_ms'])
        if len(s) >= 60:
            htf[name] = s

    def mtf_at(ts):
        out = {}
        for name, s in htf.items():
            k = int(np.searchsorted(s.t, ts, 'right'))
            w = s.slice(max(0, k - 300), k)
            if len(w) >= 60:
                out[name] = quick_trend(w)
        return out

    targets = [j for j, c in enumerate(cap['candidates']) if widened(c)]
    by_bar = {}
    for j in targets:
        by_bar.setdefault(cap['candidates'][j]['i'], []).append(j)

    cands = [dict(c) for c in cap['candidates']]
    changed = unchanged = unmatched = 0
    for i, js in by_bar.items():
        w = series.slice(i - X.WINDOW, i)
        snap = analyse(w, mtf_at(float(series.t[i - 1])), spec)
        if not snap.get('ok'):
            unmatched += len(js)
            continue
        fresh = generate(snap, w, None, True)
        for j in js:
            c = cands[j]
            m = next((s for s in fresh if s.playbook == c['playbook'] and s.side == c['side']
                      and abs(s.entry - c['entry']) < 1e-6 and abs(s.stop - c['stop']) < 1e-6),
                     None)
            if m is None:
                unmatched += 1
                continue
            if abs(m.tp2 - c['tp2']) > 1e-9:
                c['tp2'] = float(m.tp2)
                changed += 1
            else:
                unchanged += 1
    return dict(cap, candidates=cands), changed, unchanged, unmatched


def main() -> int:
    for line in apply_live_settings():
        print(f'live setting: {line}')
    exit_ = X.Exit('live trail', partial=0.0, first_r=1.0, final='tp2',
                   be_lock_r=float(CONFIG.risk.trail_lock_r),
                   trail_atr=float(CONFIG.risk.trail_atr))
    off = set(CONFIG.gates.disabled_playbooks)
    print(X.HEAD)
    for label, year in LABELS:
        cap = pickle.loads((X.OUT / f'exp_exits_{label}.pkl').read_bytes())
        fixed, ch, same, miss = corrected(cap)
        series, m1 = X.load_period(cap)
        keep = lambda k: dict(k, candidates=[c for c in k['candidates'] if c['playbook'] not in off])
        a = X.evaluate(keep(cap), series, m1, X.gate_current, exit_)
        b = X.evaluate(keep(fixed), series, m1, X.gate_current, exit_)
        key = lambda t: (t['entry_t'], t['side'], t['playbook'])
        base = {key(t): t['net_r'] for t in a}
        d = np.array([t['net_r'] - base[key(t)] for t in b if key(t) in base])
        nz = d[np.abs(d) > 1e-12]
        print(f'\n{year}: widened signals re-run -> TP2 changed {ch}, unchanged {same}, '
              f'unmatched {miss}')
        print(X._row('  BEFORE  flat 2R on widened', X.stats(a)))
        print(X._row('  AFTER   structural kept', X.stats(b)))
        print(f'  trades whose result changed: {len(nz)}   net change {nz.sum():+.1f}R'
              f'   trades added/removed by the reward gate: {len(b) - len(a):+d}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
