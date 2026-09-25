#!/usr/bin/env python
"""
tools/phase1_tpsl.py - Phase 1: test the TP / SL / selection changes Phase 0 pointed to.

Read-only. Nothing live is touched.

Baseline is the account as it trades TODAY: configs/settings.json applied
(every setup on, reward floor, cooldown, daily limits), today's exit (TP1 at
1R arms a +0.5R lock and a 1 ATR trail, MT5 holds TP2), FIXED 0.01 lots on
gold. Results are reported in R and in dollars at 0.01 lots, because with a
fixed lot a wider stop means more money per R - comparing R alone would hide
exactly the trade-off being tested.

STAGE A - one change at a time, against the baseline, all three years
    S3      only the three setups that held up in all three years of Phase 0
            (pattern_break, flag_continuation, false_break_fade). NOT out of
            sample: that list was chosen with all three years in view.
    S26     the fair version: setups chosen from 2026 alone (positive net R
            per trade under the baseline), then judged on 2025 and 2024.
    AVOID   Phase 0b's three avoid rules: no fading a leg after a 0.5-1.0 ATR
            pullback, no fading a leg that has run >= 4 ATR, no fading the 1H
            impulse while it is in progress. Leg state is read causally.
    FLOOR   stop floor 1.5 / 2.0 / 2.5 ATR instead of today's 1.0. Targets are
            re-derived from the new R exactly as the live engine does (TP1 at
            1R, a structural TP2 kept when it still sits 1.2-6R away) and the
            room-to-TP1 gate is re-judged against the new TP1.
    EXIT    per setup, the better of today's trail or an all-out fixed target
            (0.75-3R) - chosen on 2026 only.

STAGE B - one combination, chosen on 2026 only
    setups (all / S26) x avoid (off / on) x floor (1.0 / 1.5 / 2.0 / 2.5),
    best 2026 dollar result; then the per-setup exit chosen under it on 2026.
    That single configuration is then run on 2025 and 2024, which it never saw.

    python tools/phase1_tpsl.py
"""
from __future__ import annotations

import io
import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import exp_exits as X                                              # noqa: E402
import phase0_research as P0                                       # noqa: E402
import phase0b_leg_position as B                                   # noqa: E402
from exp_be_reversal import apply_live_settings                    # noqa: E402
from server.config import CONFIG, TF_SECONDS                       # noqa: E402
from server.datafeed import load_disk                              # noqa: E402
from server.engine.indicators import atr as atr_fn                 # noqa: E402
from server.engine.levels import Level, room_to_target             # noqa: E402

OUT_TXT = X.OUT / 'phase1_report.txt'
DAY_MS = 86_400_000
GOLD_LOTS = 0.01
TOP3 = ('pattern_break', 'flag_continuation', 'false_break_fade')
FLOORS = (1.0, 1.5, 2.0, 2.5)
K_GRID = (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
YEARS = [('is2026', '2026'), ('oos2025', '2025'), ('hold2024', '2024')]


def live_exit():
    return X.Exit('live trail', partial=0.0, first_r=1.0, final='tp2',
                  be_lock_r=float(CONFIG.risk.trail_lock_r),
                  trail_atr=float(CONFIG.risk.trail_atr))


# =========================================================================== #
# data                                                                        #
# =========================================================================== #
class Year:
    def __init__(self, label, name):
        self.name = name
        self.cap = pickle.loads((X.OUT / f'exp_exits_{label}.pkl').read_bytes())
        self.series, self.m1 = X.load_period(self.cap)
        a0, b0 = self.cap['start_ms'], self.cap['end_ms']
        s = self.series
        self.atr5 = atr_fn(s.h, s.l, s.c, 14)
        self.z = B.zigzag_states(s.h, s.l, self.atr5, P0.LEG_ATR)
        self.track_1h = P0.trend_track(load_disk(P0.SYMBOL, '1h', None, a0 - 60 * DAY_MS, b0))
        self.spec = self.cap['spec']
        self.vpu = (1.0 / float(self.spec.get('tick_size') or 0.01)) \
            * float(self.spec.get('tick_value') or 1.0)
        self._legs = {}
        self._floored = {}

    def leg(self, i):
        if i not in self._legs:
            self._legs[i] = B.leg_state(self.z, self.atr5, self.series.c, i - 1) if i >= 2 else None
        return self._legs[i]

    def candidates(self, floor: float) -> list:
        """The capture with fixed lots and, above 1.0, a wider stop floor."""
        if floor not in self._floored:
            self._floored[floor] = [with_floor(c, floor) for c in self.cap['candidates']]
        return self._floored[floor]


def with_floor(c: dict, floor: float) -> dict:
    """
    Fixed gold lots, and the stop pushed out to `floor` ATR if it is inside.

    Mirrors signals._widen_to_noise_floor() as it now is on disk: targets are
    re-derived from the new R, and a TP2 that was structural is offered back
    and kept if it still sits 1.2-6R away. Then the room gate, which judges
    the path to TP1, is re-judged because TP1 has moved.
    """
    c = dict(c)
    c['lots'] = GOLD_LOTS if c['lots'] > 0 else 0.0
    atr_v = float(c['atr'] or 0.0)
    risk = abs(c['entry'] - c['stop'])
    want = floor * atr_v
    if floor <= 1.0 or atr_v <= 0 or risk >= want:
        return c
    buy = c['side'] == 'buy'
    t = 1.0 if buy else -1.0
    fixed_old = c['entry'] + t * risk * CONFIG.risk.tp2_r
    structural = None if abs(c['tp2'] - fixed_old) <= 1e-6 * max(1.0, abs(fixed_old)) else c['tp2']
    c['stop'] = c['entry'] - t * want
    c['tp1'] = c['entry'] + t * want * CONFIG.risk.tp1_r
    tp2 = c['entry'] + t * want * CONFIG.risk.tp2_r
    if structural is not None and want * 1.2 <= (structural - c['entry']) * t <= want * 6.0:
        tp2 = structural
    c['tp2'] = tp2
    levels = [Level(**x) for x in c['levels']]
    room = room_to_target(levels, c['entry'], c['tp1'], c['side'])
    gates = [g for g in c['gates'] if g[0] != 'room']
    if not room['clear'] and room.get('obstacle'):
        gates.append(('room', 'BLOCK', 0.0) if room['fraction'] < 0.55 else ('room', 'WARN', 8.0))
    else:
        gates.append(('room', 'PASS', 0.0))
    c['gates'] = gates
    return c


# =========================================================================== #
# one run                                                                     #
# =========================================================================== #
def avoid_admit(y: Year):
    """Phase 0b's three avoid rules, read from the causal leg state."""
    step = TF_SECONDS[X.TF] * 1000

    def admit(c, conf):
        st = y.leg(int(c['i']))
        if st is None:
            return True
        sd = 1 if c['side'] == 'buy' else -1
        fade = sd != st['dir']
        if fade and 0.5 <= st['pull_atr'] < 1.0:
            return False
        if fade and st['ext_atr'] >= 4.0:
            return False
        h1 = P0.state_at(y.track_1h, int(c['bar_t']) + step)
        if h1 != 0 and st['dir'] == h1 and sd != h1:
            return False
        return True
    return admit


def run(y: Year, setups=None, avoid=False, floor=1.0, exits=None, fixed_k=None):
    """
    evaluate() with the arm's settings. `exits` maps setup -> fixed k (or
    'trail'); `fixed_k` applies one fixed target to every setup.
    """
    cands = y.candidates(floor)
    if setups is not None:
        cands = [c for c in cands if c['playbook'] in setups]
    cap = dict(y.cap, candidates=cands)
    base_exit = live_exit()
    original = X.simulate

    def patched(c, ex, series_, m1_, spec_, fill, start_idx):
        k = fixed_k if fixed_k is not None else (exits or {}).get(c['playbook'], 'trail')
        use = ex if k == 'trail' else X.Exit(f'fixed {k}R', partial=1.0, first_r=float(k))
        res = original(c, use, series_, m1_, spec_, fill, start_idx)
        if res is not None:
            res['risk'] = abs(fill - c['stop'])
        return res

    X.simulate = patched
    try:
        return X.evaluate(cap, y.series, y.m1, X.gate_current, base_exit,
                          admit=avoid_admit(y) if avoid else None)
    finally:
        X.simulate = original


def money(y: Year, trades) -> dict:
    """Dollars at fixed 0.01 lots, in trade order."""
    usd = np.array([t['net_r'] * t['risk'] * y.vpu * GOLD_LOTS for t in trades]) \
        if trades else np.array([0.0])
    cum = np.cumsum(usd)
    dd = float(np.max(np.maximum.accumulate(cum) - cum)) if len(cum) else 0.0
    return {'usd': float(usd.sum()), 'dd': dd,
            'risk': float(np.mean([t['risk'] for t in trades])) if trades else 0.0}


HEAD = (f'    {"":40} {"trades":>6} {"win":>6} {"PF":>5} {"E[R]":>7} {"sum R":>8} '
        f'{"$ @0.01":>9} {"$ maxDD":>8} {"avg stop":>9}')


def row(label, y, trades):
    if not trades:
        return f'    {label:<40} {"no trades":>6}'
    s = X.stats(trades)
    m = money(y, trades)
    pf = f'{s["pf"]:.2f}' if s['pf'] == s['pf'] else '  - '
    return (f'    {label:<40} {s["n"]:>6} {s["win"]:>5.1f}% {pf:>5} {s["exp"]:>+7.3f} '
            f'{s["sum"]:>+8.1f} {m["usd"]:>+9.0f} {m["dd"]:>8.0f} {m["risk"]:>7.2f}pt')


def choose_exits(y: Year, setups, avoid, floor) -> dict:
    """Per setup, the best of the trail or a fixed k - on THIS year only."""
    per = {}                                    # setup -> {policy: sum net R}
    for k in ('trail',) + K_GRID:
        trades = run(y, setups, avoid, floor, fixed_k=k)
        for t in trades:
            per.setdefault(t['playbook'], {}).setdefault(k, 0.0)
            per[t['playbook']][k] += t['net_r']
    return {pb: max(d, key=d.get) for pb, d in per.items()}


# =========================================================================== #
# driver                                                                      #
# =========================================================================== #
def main() -> int:
    buf = io.StringIO()

    def out(line=''):
        print(line, flush=True)
        buf.write(line + '\n')

    for line in apply_live_settings():
        out(f'live setting: {line}')
    out(f'\nPHASE 1 - TP / SL / selection   XAUUSD 5m   baseline = today\'s live settings '
        f'and exit   fixed {GOLD_LOTS} lots   costs included')

    years = [Year(lb, nm) for lb, nm in YEARS]
    y26 = years[0]

    # S26: setups picked from 2026 alone.
    base26 = run(y26)
    per = {}
    for t in base26:
        per.setdefault(t['playbook'], []).append(t['net_r'])
    s26 = tuple(sorted(pb for pb, r in per.items() if len(r) >= 30 and np.mean(r) > 0))
    out(f'S26 (chosen on 2026 alone): {", ".join(s26)}')
    exits_a = choose_exits(y26, None, False, 1.0)
    out('EXIT per setup (chosen on 2026, baseline otherwise): '
        + ', '.join(f'{pb} {k if k == "trail" else str(k) + "R"}' for pb, k in sorted(exits_a.items())))

    # ------------------------------------------------------------- STAGE A
    arms = [
        ('BASE  today, live', dict()),
        ('S3    top-3 setups (hindsight)', dict(setups=TOP3)),
        ('S26   setups chosen on 2026', dict(setups=s26)),
        ('AVOID the three avoid rules', dict(avoid=True)),
        ('FLOOR stop >= 1.5 ATR', dict(floor=1.5)),
        ('FLOOR stop >= 2.0 ATR', dict(floor=2.0)),
        ('FLOOR stop >= 2.5 ATR', dict(floor=2.5)),
        ('EXIT  per-setup exit (2026-chosen)', dict(exits=exits_a)),
    ]
    out(f'\n{"=" * 118}\n  STAGE A - one change at a time\n{"=" * 118}')
    for y in years:
        out(f'\n  {y.name}')
        out(HEAD)
        for name, kw in arms:
            out(row(name, y, run(y, **kw)))

    # ------------------------------------------------------------- STAGE B
    out(f'\n{"=" * 118}\n  STAGE B - one combination, chosen on 2026 only\n{"=" * 118}')
    grid = []
    for setups_name, setups in (('all', None), ('S26', s26)):
        for avoid in (False, True):
            for floor in FLOORS:
                tr = run(y26, setups, avoid, floor)
                m = money(y26, tr)
                grid.append((m['usd'], setups_name, setups, avoid, floor, tr))
    grid.sort(key=lambda g: -g[0])
    out('  2026 grid, best first (live exit):')
    out(HEAD)
    for usd, sn, _, av, fl, tr in grid:
        out(row(f'setups {sn:<3} avoid {"on " if av else "off"} floor {fl}', y26, tr))
    _, sn, setups, avoid, floor, _ = grid[0]
    exits_b = choose_exits(y26, setups, avoid, floor)
    out(f'\n  CHOSEN on 2026: setups {sn}, avoid {"on" if avoid else "off"}, floor {floor} ATR, '
        f'exits ' + ', '.join(f'{pb} {k if k == "trail" else str(k) + "R"}'
                              for pb, k in sorted(exits_b.items())))

    out(f'\n{"=" * 118}\n  VERDICT - the chosen configuration against today, in years it never saw\n{"=" * 118}')
    for y in years:
        tag = 'in-sample' if y is y26 else 'OUT OF SAMPLE'
        out(f'\n  {y.name}  ({tag})')
        out(HEAD)
        out(row('BASE  today, live', y, run(y)))
        out(row('CHOSEN combination', y, run(y, setups, avoid, floor, exits=exits_b)))
        out(row('  ...same, but today\'s exit', y, run(y, setups, avoid, floor)))

    OUT_TXT.write_text(buf.getvalue(), encoding='utf-8')
    print(f'\nreport -> {OUT_TXT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
