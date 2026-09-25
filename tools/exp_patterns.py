#!/usr/bin/env python
"""
tools/exp_patterns.py - does the pattern-detector fix change what trades?

The fix (server/engine/patterns.py, 2026-09-24):
    HEAD_CLEAR_ATR   a head and shoulders' head must clear BOTH shoulders by
                     0.5 ATR (was: any margin - a double top with a lower
                     bounce passed as H&S)
    EXPIRE_UNBROKEN  H&S and double/triple patterns whose 30-bar break window
                     closed without a break are dropped (was: "forming"
                     forever, still able to arm pattern_break)

Read-only. Nothing live is touched.

One engine replay per year records BOTH variants: analyse() runs once per
bar with the new rules, and the old rules' pattern list is computed from the
same swings in the same call (detect_patterns is wrapped), so the two
variants differ in nothing but the patterns. Where the two lists are equal
the candidates are shared.

Baseline is the account as it trades TODAY: configs/settings.json applied
(three playbooks off, 1.5 ATR stop floor, leg gate on), today's exit (TP1 at
1R arms a +0.5R lock and a 1 ATR trail, TP2 at the broker), fixed 0.01 lots.

Higher timeframes are read from CLOSED bars only - the bar that has closed by
the 5m bar's close - as the live engine now does for FINAL signals. The older
harness (exp_exits.capture, backtest.run) reads the HTF bar CONTAINING the
5m bar, which on disk is complete and so carries up to an hour (1h) or four
(4h) of future prices. Numbers here are therefore not comparable with the
Phase 1 report; old vs new here is.

    python tools/exp_patterns.py capture --label is2026   --start 2026-01-01 --end 2026-08-21
    python tools/exp_patterns.py capture --label oos2025  --start 2025-01-01 --end 2025-08-31
    python tools/exp_patterns.py capture --label hold2024 --start 2024-01-01 --end 2024-08-31
    python tools/exp_patterns.py report
"""
from __future__ import annotations

import argparse
import copy
import io
import pickle
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import exp_exits as X                                              # noqa: E402
from exp_be_reversal import apply_live_settings                    # noqa: E402
from server.config import CONFIG, MTF_LADDER, TF_SECONDS           # noqa: E402
from server.datafeed import FEED, load_disk                        # noqa: E402
from server.engine import patterns as P                            # noqa: E402
from server.engine.analysis import analyse, quick_trend            # noqa: E402
from server.engine.levels import Level                             # noqa: E402
from server.engine.qualify import qualify_all                      # noqa: E402
from server.engine.signals import generate                         # noqa: E402

OUT = X.OUT
YEARS = [('is2026', '2026'), ('oos2025', '2025'), ('hold2024', '2024')]
GOLD_LOTS = 0.01
PATTERN_PLAYBOOKS = ('pattern_break', 'flag_continuation')


def _path(label, variant):
    return OUT / f'exp_patterns_{label}_{variant}.pkl'


# =========================================================================== #
# capture                                                                     #
# =========================================================================== #
_STASH: dict = {}
_real_detect = P.detect_patterns


def _both(*a, **k):
    """New rules for the snapshot; the old rules' list stashed alongside."""
    P.EXPIRE_UNBROKEN, P.HEAD_CLEAR_ATR = False, 0.0
    try:
        _STASH['old'] = _real_detect(*a, **k)
    finally:
        P.EXPIRE_UNBROKEN, P.HEAD_CLEAR_ATR = True, 0.5
    return _real_detect(*a, **k)


def _record(snap, w, spec, point, i, bar_t, cands):
    ctx = {'equity': 25000.0, 'open_positions': 0, 'trades_today': 0, 'daily_pnl_pct': 0.0}
    raw = generate(snap, w, None, True)
    pre = copy.deepcopy(raw)
    post = qualify_all(raw, snap, spec, ctx)
    sp = snap.get('spread_points')
    spread_price = float(sp) * point if sp is not None else point * 20
    levels = [{k: v for k, v in lv.items() if k in Level.__annotations__}
              for lv in (snap.get('levels') or [])]
    for p, q in zip(pre, post):
        if q.status == 'conflicted':
            continue
        cands.append({
            'i': i, 'bar_t': bar_t, 'playbook': q.playbook, 'side': q.side,
            'entry_type': q.entry_type, 'trigger': q.trigger,
            'entry': q.entry, 'stop': q.stop, 'tp1': q.tp1, 'tp2': q.tp2,
            'atr': float(q.atr_at_signal or snap.get('atr') or 0.0),
            'expiry': int(q.expiry_bars), 'raw_conf': int(p.confidence),
            'status': q.status,
            'gates': [(g['name'], g['verdict'], float(g.get('penalty') or 0))
                      for g in q.gates],
            'lots': float((q.sizing or {}).get('lots') or 0.0),
            'spread_price': spread_price, 'levels': levels,
            'regime': (snap.get('regime') or {}).get('state', ''),
        })


def capture(label, start_ms, end_ms):
    apply_live_settings()
    P.detect_patterns = _both
    spec = FEED.spec(X.SYMBOL)
    series = load_disk(X.SYMBOL, X.TF, None, start_ms, end_ms)
    step_ms = TF_SECONDS[X.TF] * 1000
    htf = {}
    for name in dict.fromkeys(x for x in MTF_LADDER.get(X.TF, [X.TF]) if x != X.TF):
        s = load_disk(X.SYMBOL, name, None, start_ms, end_ms)
        if len(s) >= 60:
            htf[name] = (s, s.t + TF_SECONDS[name] * 1000)     # (series, close times)

    def mtf_at(bar_close: float) -> dict:
        out = {}
        for name, (s, closes) in htf.items():
            i = int(np.searchsorted(closes, bar_close, 'right'))    # CLOSED bars only
            w = s.slice(max(0, i - 300), i)
            if len(w) >= 60:
                out[name] = quick_trend(w)
        return out

    point = float(spec.get('point') or 0.01)
    old_c, new_c = [], []
    counts = {'bars': 0, 'differ': 0, 'hs_old': 0, 'hs_new': 0,
              'stale_old': 0, 'patterns_old': 0, 'patterns_new': 0}
    t0 = time.time()
    total = len(series)
    for i in range(X.WINDOW, total):
        w = series.slice(i - X.WINDOW, i)
        bar_t = float(series.t[i - 1])
        snap = analyse(w, mtf_at(bar_t + step_ms), spec)
        if not snap.get('ok'):
            continue
        old_list = [p.to_dict() for p in _STASH.get('old', [])]
        counts['bars'] += 1
        counts['patterns_old'] += len(old_list)
        counts['patterns_new'] += len(snap['patterns'])
        counts['hs_old'] += sum('head' in p['kind'] for p in old_list)
        counts['hs_new'] += sum('head' in p['kind'] for p in snap['patterns'])
        counts['stale_old'] += sum(p['status'] == 'forming' and p['age_bars'] >= P.CONFIRM_WINDOW
                                   and p['kind'].split('_')[-1] in ('shoulders', 'top', 'bottom')
                                   for p in old_list)
        n0 = len(new_c)
        _record(snap, w, spec, point, i, bar_t, new_c)
        if old_list == snap['patterns']:
            old_c.extend(new_c[n0:])            # identical input, identical candidates
        else:
            counts['differ'] += 1
            _record(dict(snap, patterns=old_list), w, spec, point, i, bar_t, old_c)
        if (i - X.WINDOW) % 2000 == 0:
            done = (i - X.WINDOW) / max(1, total - X.WINDOW)
            print(f'  {label} {done * 100:5.1f}%  new {len(new_c)} old {len(old_c)}  '
                  f'{time.time() - t0:6.0f}s', flush=True)
    for variant, cands in (('old', old_c), ('new', new_c)):
        _path(label, variant).write_bytes(pickle.dumps({
            'start_ms': start_ms, 'end_ms': end_ms, 'step': 1, 'spec': spec,
            'candidates': cands, 'counts': counts}))
    print(f'{label}: done in {time.time() - t0:.0f}s  {counts}', flush=True)


# =========================================================================== #
# report                                                                      #
# =========================================================================== #
def live_exit():
    return X.Exit('live trail', partial=0.0, first_r=1.0, final='tp2',
                  be_lock_r=float(CONFIG.risk.trail_lock_r),
                  trail_atr=float(CONFIG.risk.trail_atr))


def run(cap, series, m1):
    original = X.simulate

    def patched(c, ex, s_, m_, spec_, fill, start_idx):
        res = original(c, ex, s_, m_, spec_, fill, start_idx)
        if res is not None:
            res['risk'] = abs(fill - c['stop'])
        return res

    X.simulate = patched
    try:
        return X.evaluate(cap, series, m1, X.gate_current, live_exit())
    finally:
        X.simulate = original


def row(label, trades, vpu):
    if not trades:
        return f'    {label:<30} {"no trades":>6}'
    s = X.stats(trades)
    usd = np.array([t['net_r'] * t['risk'] * vpu * GOLD_LOTS for t in trades])
    cum = np.cumsum(usd)
    dd = float(np.max(np.maximum.accumulate(cum) - cum))
    pf = f'{s["pf"]:.2f}' if s['pf'] == s['pf'] else '  - '
    return (f'    {label:<30} {s["n"]:>6} {s["win"]:>5.1f}% {pf:>5} {s["exp"]:>+7.3f} '
            f'{s["sum"]:>+8.1f} {usd.sum():>+9.0f} {dd:>8.0f}')


HEAD = (f'    {"":30} {"trades":>6} {"win":>6} {"PF":>5} {"E[R]":>7} {"sum R":>8} '
        f'{"$ @0.01":>9} {"$ maxDD":>8}')


def report():
    apply_live_settings()
    buf = io.StringIO()

    def out(line=''):
        print(line)
        buf.write(line + '\n')

    out('PATTERN FIX - old vs new detector   XAUUSD 5m   today\'s live settings and exit   '
        'fixed 0.01 lots   costs included   HTF on closed bars')
    for label, name in YEARS:
        caps = {v: pickle.loads(_path(label, v).read_bytes()) for v in ('old', 'new')}
        series, m1 = X.load_period(caps['new'])
        spec = caps['new']['spec']
        vpu = (1.0 / float(spec.get('tick_size') or 0.01)) * float(spec.get('tick_value') or 1.0)
        k = caps['new']['counts']
        out()
        out(f'  {name}   bars {k["bars"]}, pattern lists differ on {k["differ"]} '
            f'({k["differ"] / max(1, k["bars"]) * 100:.1f}%)   H&S per bar old '
            f'{k["hs_old"] / max(1, k["bars"]):.3f} -> new {k["hs_new"] / max(1, k["bars"]):.3f}   '
            f'stale "forming" H&S/double/triple (old) {k["stale_old"] / max(1, k["bars"]):.3f}/bar')
        out(HEAD)
        tr = {v: run(caps[v], series, m1) for v in ('old', 'new')}
        for v in ('old', 'new'):
            out(row(f'ALL      {v}', tr[v], vpu))
        for pb in PATTERN_PLAYBOOKS:
            for v in ('old', 'new'):
                out(row(f'{pb[:17]:<17} {v}', [t for t in tr[v] if t['playbook'] == pb], vpu))
    (OUT / 'exp_patterns_report.txt').write_text(buf.getvalue(), encoding='utf-8')
    print(f'\nwritten {OUT / "exp_patterns_report.txt"}')


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('capture')
    c.add_argument('--label', required=True)
    c.add_argument('--start', required=True)
    c.add_argument('--end', required=True)
    sub.add_parser('report')
    a = ap.parse_args()
    if a.cmd == 'capture':
        capture(a.label, X._ms(a.start), X._ms(a.end))
    else:
        report()
    return 0


if __name__ == '__main__':
    sys.exit(main())
