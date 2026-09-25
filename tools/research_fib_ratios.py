#!/usr/bin/env python
"""
tools/research_fib_ratios.py - do gold's swings respect Fibonacci ratios?

The question behind harmonic XABCD patterns (Gartley, Bat, Butterfly, Crab),
ABCD measured moves and Elliott wave targets: do swings END at particular
ratios of the swing before them? Every one of those patterns is a set of
ratio rules on the same zigzag, so if reversals do not favour those ratios,
none of the patterns has anything to trade. Measured before building any of
them.

Read-only. XAUUSD 5m, 2024-01-01 .. 2026-08-21, ATR(14) zigzag at 1.0 / 1.5 /
2.5 ATR (the leg definition Phase 0 used), each year checked separately.

  A. RETRACEMENTS   r = correction / leg before it. For each Fibonacci level
                    F: of the corrections that REACHED F, what share ENDED
                    within [F, F+0.05)? That is the probability of turning at
                    F given price got there - exactly the harmonic "potential
                    reversal zone" claim. Compared with the same probability
                    at non-Fibonacci levels just either side (F +/- 0.06,
                    0.09, 0.12), which cancels the smooth trend of the curve.
  B. MEASURED MOVE  m = CD / AB (same-direction legs either side of one
                    correction): the same test at 1.0, 1.272, 1.618 - ABCD.
  C. ELLIOTT        5 alternating legs obeying the three hard rules (wave 2
                    never beyond the start of 1, wave 3 not the shortest, wave
                    4 never into wave 1's range) with wave 5 making a new
                    extreme. (1) How often they occur in real gold vs the
                    same legs shuffled into random order. (2) Whether the move
                    after a completed "wave 5" is any larger than after any
                    other leg of the same direction.

    python tools/research_fib_ratios.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import phase0_research as P0                                        # noqa: E402
from server.datafeed import load_disk                               # noqa: E402
from server.engine.indicators import atr as atr_fn                  # noqa: E402

SYMBOL, TF = 'XAUUSD.a', '5m'
KS = (1.0, 1.5, 2.5)
FIB_R = (0.382, 0.5, 0.618, 0.786, 0.886, 1.0, 1.272, 1.618)
FIB_M = (1.0, 1.272, 1.618)
W = 0.05                           # "ended at F" = ended within [F, F + W)
CONTROLS = (-0.12, -0.09, -0.06, 0.06, 0.09, 0.12)
OUT = ROOT / 'order_ledger' / 'research_fib_report.txt'


def hazard(x: np.ndarray, q: float) -> tuple:
    """(reached, ended there): how many got to q, and how many stopped in [q, q+W)."""
    reached = int((x >= q).sum())
    ended = int(((x >= q) & (x < q + W)).sum())
    return reached, ended


def level_test(x: np.ndarray, level: float) -> dict:
    n, e = hazard(x, level)
    h = e / n if n else float('nan')
    ctrl = []
    for d in CONTROLS:
        cn, ce = hazard(x, level + d)
        if cn >= 30:
            ctrl.append(ce / cn)
    hc = float(np.mean(ctrl)) if ctrl else float('nan')
    se = np.sqrt(hc * (1 - hc) / n) if n and hc == hc else float('nan')
    z = (h - hc) / se if se and se == se and se > 0 else float('nan')
    return {'n': n, 'h': h, 'hc': hc, 'z': z}


def legs_of(piv: list, atr: np.ndarray):
    """Leg sizes (price) and their size in ATR at the leg's start pivot."""
    p = np.array([x[1] for x in piv], dtype=float)
    idx = np.array([x[0] for x in piv], dtype=int)
    size = np.abs(np.diff(p))
    in_atr = size / np.maximum(atr[idx[:-1]], 1e-9)
    return p, idx, size, in_atr


def elliott_windows(p: np.ndarray) -> np.ndarray:
    """Start indexes i where pivots p[i..i+5] form a rule-valid 5-wave impulse."""
    out = []
    for i in range(len(p) - 5):
        a = p[i:i + 6]
        up = a[1] > a[0]
        s = 1.0 if up else -1.0
        b = a * s                          # mirror a down impulse into an up one
        w1, w3, w5 = b[1] - b[0], b[3] - b[2], b[5] - b[4]
        ok = (w1 > 0 and b[2] > b[0]       # wave 2 never beyond the start of 1
              and b[3] > b[1]              # wave 3 beyond wave 1
              and b[4] > b[1]              # wave 4 never into wave 1's range
              and b[5] > b[3]              # wave 5 makes the new extreme
              and w3 > 0 and w5 > 0
              and not (w3 < w1 and w3 < w5))   # wave 3 not the shortest
        if ok:
            out.append(i)
    return np.array(out, dtype=int)


def shuffled_pivots(p: np.ndarray, rng) -> np.ndarray:
    """Same leg sizes, random order, same alternation - a structureless twin."""
    size = np.abs(np.diff(p))
    rng.shuffle(size)
    sign = np.sign(p[1] - p[0])
    steps = size * np.array([sign * (1 if k % 2 == 0 else -1) for k in range(len(size))])
    return np.concatenate(([p[0]], p[0] + np.cumsum(steps)))


def main() -> int:
    buf = io.StringIO()

    def out(line=''):
        print(line)
        buf.write(line + '\n')

    s = load_disk(SYMBOL, TF, [2024, 2025, 2026])
    a = atr_fn(s.h, s.l, s.c, 14)
    years = np.array([int(np.datetime64(int(t), 'ms').astype('datetime64[Y]').astype(int) + 1970)
                      for t in s.t])
    out(f'FIBONACCI RATIOS AT GOLD REVERSALS   {SYMBOL} {TF}   {len(s):,} bars '
        f'{str(np.datetime64(int(s.t[0]), "ms"))[:10]} .. {str(np.datetime64(int(s.t[-1]), "ms"))[:10]}')
    out('h = share of swings that reached the level and ENDED within +0.05 of it; '
        'ctrl = the same at non-Fib levels either side; z = difference in standard errors')

    rng = np.random.default_rng(7)
    for k in KS:
        piv = P0.zigzag(s.h, s.l, a, k)
        p, idx, size, in_atr = legs_of(piv, a)
        r = size[1:] / np.maximum(size[:-1], 1e-9)             # correction / prior leg
        m = size[2:] / np.maximum(size[:-2], 1e-9)             # CD / AB
        r_year = years[idx[1:-1]]
        out()
        out(f'=== zigzag {k} ATR: {len(piv):,} pivots, median leg {np.nanmedian(in_atr):.2f} ATR, '
            f'median retracement {np.median(r):.3f}')
        out(f'  A. retracement r = correction / prior leg')
        out(f'     {"level":>6} {"reached":>8} {"h":>7} {"ctrl":>7} {"diff":>7} {"z":>6}   '
            f'{"z 2024":>7} {"z 2025":>7} {"z 2026":>7}')
        for F in FIB_R:
            t = level_test(r, F)
            zy = [level_test(r[r_year == y], F)['z'] for y in (2024, 2025, 2026)]
            out(f'     {F:>6.3f} {t["n"]:>8,} {t["h"]:>7.3f} {t["hc"]:>7.3f} '
                f'{(t["h"] - t["hc"]) * 100:>+6.1f}pp {t["z"]:>+6.2f}   '
                + ' '.join(f'{z:>+7.2f}' for z in zy))
        out(f'  B. measured move m = CD / AB')
        for F in FIB_M:
            t = level_test(m, F)
            out(f'     {F:>6.3f} {t["n"]:>8,} {t["h"]:>7.3f} {t["hc"]:>7.3f} '
                f'{(t["h"] - t["hc"]) * 100:>+6.1f}pp {t["z"]:>+6.2f}')

        # ----------------------------------------------------------- Elliott
        imp = elliott_windows(p)
        real = len(imp) / max(1, len(p) - 5)
        twins = [len(elliott_windows(shuffled_pivots(p, rng))) / max(1, len(p) - 5)
                 for _ in range(30)]
        out(f'  C. Elliott: rule-valid 5-wave impulses in {real * 100:.2f}% of 6-pivot windows; '
            f'shuffled legs {np.mean(twins) * 100:.2f}% +/- {np.std(twins) * 100:.2f}%')
        # The move AFTER wave 5 (leg p5 -> p6) vs after any leg of the same
        # direction, in ATR. Both are at least k ATR by construction.
        after = np.array([in_atr[i + 5] for i in imp if i + 5 < len(in_atr)])
        after = after[np.isfinite(after)]
        base = in_atr[1:]
        base = base[np.isfinite(base)]
        big = 3.0 if k <= 1.5 else 5.0
        out(f'     leg after a completed wave 5: mean {np.mean(after):.2f} ATR, '
            f'P(>= {big:g} ATR) {np.mean(np.array(after) >= big) * 100:.1f}%  (n={len(after):,})   '
            f'| after any leg: mean {np.mean(base):.2f} ATR, '
            f'P(>= {big:g} ATR) {np.mean(base >= big) * 100:.1f}%')

    OUT.write_text(buf.getvalue(), encoding='utf-8')
    print(f'\nwritten {OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
