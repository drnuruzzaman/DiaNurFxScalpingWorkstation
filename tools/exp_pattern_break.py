#!/usr/bin/env python
"""
tools/exp_pattern_break.py - is the pattern_break regime exemption earning its keep?

pattern_break is the only playbook allowed to fire in every regime. Every other
playbook is restricted to the regimes where it has a thesis. The exemption was
justified as "a completing head and shoulders is information in any regime",
which sounds reasonable and was never tested.

Because pattern_break appears in NO regime allowlist, simply deleting the
exemption does not gate it - it disables it. So three arms:

  A  BASELINE   exemption in place: pattern_break fires everywhere
  B  DROPPED    exemption removed: pattern_break never fires
  C  GATED      pattern_break allowed only in the regimes where arm A shows it
                actually works (chosen from A's cross-tab, not from a hunch)

Arms cannot be simulated by filtering arm A's trades. Playbooks compete for the
same concurrency and daily-limit budget, so removing one changes which of the
others get taken. Each arm is a real run.

Usage:
    python tools/exp_pattern_break.py --months 8 --step 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np                                            # noqa: E402

from server.engine import backtest as bt                      # noqa: E402
from server.engine import regime as regime_mod                # noqa: E402
from server.engine import signals as sig_mod                  # noqa: E402


# --------------------------------------------------------------------------- #
# arm control                                                                 #
# --------------------------------------------------------------------------- #
_ORIGINAL_GENERATE = sig_mod.generate


def install_arm(mode: str, gated_regimes=()) -> None:
    """
    Replace signals.generate with a variant that changes ONLY the pattern_break
    gating rule. Everything else - detection, scoring, qualification - is
    untouched, so any difference between arms is attributable to this one rule.
    """
    allowed_in = set(gated_regimes)

    def generate(snap, series, only=None, respect_regime=True):
        if not snap.get('ok'):
            return []
        state = (snap.get('regime') or {}).get('state', '')
        base = set((snap.get('regime') or {}).get('allowed_playbooks') or sig_mod.PLAYBOOKS)

        if mode == 'baseline':
            base.add('pattern_break')
        elif mode == 'dropped':
            base.discard('pattern_break')
        elif mode == 'gated':
            if state in allowed_in:
                base.add('pattern_break')
            else:
                base.discard('pattern_break')

        out = []
        for name, fn in sig_mod.PLAYBOOKS.items():
            if only and name not in only:
                continue
            if respect_regime and name not in base:
                continue
            try:
                out.extend(fn(snap, series) or [])
            except Exception:                                  # noqa: BLE001
                continue
        # Reuse the real resolution step so the only thing this arm
        # changes is the gating rule above.
        return sig_mod.resolve(out)

    sig_mod.generate = generate
    bt.generate = generate


def restore() -> None:
    sig_mod.generate = _ORIGINAL_GENERATE
    bt.generate = _ORIGINAL_GENERATE


# --------------------------------------------------------------------------- #
# reporting                                                                   #
# --------------------------------------------------------------------------- #
def pf(v):
    return '  inf' if v is None else f'{v:5.2f}'


def summarise(tag: str, r: dict) -> dict:
    s = r['summary']
    print(f'\n=== {tag} ===')
    print(f"  trades {s['trades']:4d}   win {s['win_rate']:5.1f}%   "
          f"PF {pf(s['profit_factor'])}   expectancy {s['expectancy_r']:+.3f}R")
    print(f"  return {s['return_pct']:+7.2f}%   maxDD {s['max_drawdown_pct']:5.2f}%   "
          f"net {s['net_pnl']:+9.2f}")
    return s


def cross_tab(r: dict, playbook: str) -> None:
    """Where does this playbook actually work?"""
    rows = []
    for key, st in (r.get('by_playbook_regime') or {}).items():
        if not key.startswith(playbook + '/'):
            continue
        rows.append((key.split('/', 1)[1], st))
    if not rows:
        print(f'  {playbook}: no trades')
        return
    print(f'\n  {playbook} by regime:')
    print(f'    {"regime":12s} {"n":>5s} {"win%":>7s} {"expR":>8s} {"PF":>7s} {"pnl":>10s}')
    for name, st in sorted(rows, key=lambda x: -x[1]['trades']):
        print(f"    {name:12s} {st['trades']:5d} {st['win_rate']:7.1f} "
              f"{st['expectancy_r']:+8.3f} {pf(st['profit_factor']):>7s} "
              f"{st['net_pnl']:+10.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--months', type=int, default=8)
    ap.add_argument('--step', type=int, default=3)
    ap.add_argument('--symbol', default='XAUUSD.a')
    ap.add_argument('--tf', default='5m')
    ap.add_argument('--equity', type=float, default=25000)
    args = ap.parse_args()

    start = dt.datetime(2026, 1, 1)
    end = start + dt.timedelta(days=int(args.months * 30.44))
    window = dict(symbol=args.symbol, tf=args.tf,
                  start_ms=int(start.timestamp() * 1000),
                  end_ms=int(end.timestamp() * 1000),
                  equity=args.equity, step=args.step)

    print(f'window {start:%Y-%m-%d} -> {end:%Y-%m-%d}  {args.tf}  step={args.step}')
    results = {}

    # --- arm A ------------------------------------------------------------- #
    install_arm('baseline')
    ra = bt.run(**window)
    if not ra.get('ok'):
        print('baseline failed:', ra.get('error'))
        return 1
    sa = summarise('A  BASELINE   (pattern_break exempt, fires everywhere)', ra)
    cross_tab(ra, 'pattern_break')
    results['baseline'] = ra

    # Choose arm C's regimes FROM THE DATA: keep only regimes where
    # pattern_break was positive on a non-trivial sample.
    keep = []
    for key, st in (ra.get('by_playbook_regime') or {}).items():
        if not key.startswith('pattern_break/'):
            continue
        name = key.split('/', 1)[1]
        if st['trades'] >= 15 and st['expectancy_r'] > 0:
            keep.append(name)
    print(f'\n  -> arm C will allow pattern_break in: {keep or "(none qualified)"}')

    # --- arm B ------------------------------------------------------------- #
    install_arm('dropped')
    rb = bt.run(**window)
    sb = summarise('B  DROPPED    (exemption removed => playbook never fires)', rb)
    results['dropped'] = rb

    # --- arm C ------------------------------------------------------------- #
    sc = None
    if keep:
        install_arm('gated', keep)
        rc = bt.run(**window)
        sc = summarise(f'C  GATED      (pattern_break only in {", ".join(keep)})', rc)
        cross_tab(rc, 'pattern_break')
        results['gated'] = rc
    else:
        print('\n=== C  GATED === skipped: no regime qualified on the evidence')

    restore()

    # --- verdict ------------------------------------------------------------ #
    print('\n' + '=' * 72)
    print('  VERDICT')
    print('=' * 72)
    hdr = f"  {'arm':10s} {'trades':>7s} {'win%':>7s} {'PF':>7s} {'expR':>9s} {'return%':>9s} {'maxDD%':>8s}"
    print(hdr)
    for name, s in (('A base', sa), ('B dropped', sb), ('C gated', sc)):
        if s is None:
            continue
        print(f"  {name:10s} {s['trades']:7d} {s['win_rate']:7.1f} {pf(s['profit_factor']):>7s} "
              f"{s['expectancy_r']:+9.3f} {s['return_pct']:+9.2f} {s['max_drawdown_pct']:8.2f}")

    out = ROOT / 'runs' / 'exp_pattern_break.json'
    out.write_text(json.dumps(
        {k: {kk: vv for kk, vv in v.items() if kk != 'trades'}
         for k, v in results.items()}, allow_nan=False), encoding='utf-8')
    print(f'\n  full results -> {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
