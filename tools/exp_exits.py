#!/usr/bin/env python
"""
tools/exp_exits.py - which exit, and which reward gate, actually pays.

Question being settled: the TP2 floor (net 1.5R) rejects signals whose first
target is fine. Should we (a) relax the floor, (b) exit everything at TP1, or
(c) trail the stop - and does the answer survive a year it was not tuned on?

Two stages, because the expensive part is the same for every arm:

  capture   Replay the engine once over a period, exactly as backtest.run()
            does, and record EVERY non-conflicted signal with what each arm
            needs: raw confidence, every gate verdict, levels for the room
            check, sizing, the bar's spread. ~2.5 min per month of 5m data.

  report    For each arm, re-decide which of those signals qualify under the
            arm's reward gate, then simulate the arm's exit bar by bar. Seconds.

Faithfulness:
  - Entry, cooldown, pending-order fills, the one-bar fill delay, expiry and
    ambiguous-bar resolution on M1 all mirror backtest.run() line for line.
    `verify` proves it: arm A with backtest.run()'s own accounting must match
    run()'s trade count and total R on the same window.
  - Costs: backtest.run() charges commission but NOT the spread. Every arm
    here charges both (the bar's recorded spread, once per round trip), so
    the numbers are lower than run()'s and honest. The gap is printed.
  - Trailing uses the ATR at signal time and only COMPLETED bars, so a trail
    can never be set from a price the bar has not printed yet.

Usage:
    python tools/exp_exits.py capture --label is2026  --start 2026-01-01 --end 2026-08-21
    python tools/exp_exits.py capture --label oos2025 --start 2025-01-01 --end 2025-08-31
    python tools/exp_exits.py verify  --start 2026-01-01 --end 2026-02-01
    python tools/exp_exits.py report  --labels is2026 oos2025
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import pickle
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import CONFIG, MTF_LADDER, TF_SECONDS           # noqa: E402
from server.datafeed import FEED, load_disk                        # noqa: E402
from server.engine.analysis import analyse, quick_trend            # noqa: E402
from server.engine.levels import Level, room_to_target             # noqa: E402
from server.engine.qualify import _net_rr, qualify_all             # noqa: E402
from server.engine.signals import generate                         # noqa: E402

SYMBOL = 'XAUUSD.a'
TF = '5m'
WINDOW = 600
OUT = ROOT / 'order_ledger'


def _ms(s: str) -> int:
    return int(dt.datetime.fromisoformat(s).timestamp() * 1000)


# =========================================================================== #
# stage 1: capture                                                            #
# =========================================================================== #
def capture(start_ms: int, end_ms: int, step: int = 1, max_bars: int = 0) -> dict:
    """Mirror backtest.run()'s signal loop; record candidates instead of trading."""
    spec = FEED.spec(SYMBOL)
    series = load_disk(SYMBOL, TF, None, start_ms, end_ms)
    ladder = [x for x in MTF_LADDER.get(TF, [TF]) if x != TF]
    htf = {}
    for name in dict.fromkeys(ladder):
        s = load_disk(SYMBOL, name, None, start_ms, end_ms)
        if len(s) >= 60:
            htf[name] = s

    def mtf_at(ts: float) -> dict:
        out = {}
        for name, s in htf.items():
            i = int(np.searchsorted(s.t, ts, 'right'))
            w = s.slice(max(0, i - 300), i)
            if len(w) >= 60:
                out[name] = quick_trend(w)
        return out

    total = len(series)
    stop_at = min(total, WINDOW + max_bars) if max_bars else total
    point = float(spec.get('point') or 0.01)
    cands = []
    t0 = time.time()
    for i in range(WINDOW, stop_at, step):
        w = series.slice(i - WINDOW, i)
        bar_t = float(series.t[i - 1])
        snap = analyse(w, mtf_at(bar_t), spec)
        if not snap.get('ok'):
            continue
        # Daily limits and exposure are re-applied per arm in stage 2, because
        # they depend on which trades THAT arm took. Zero here keeps those two
        # gates from ever deciding at capture time.
        ctx = {'equity': 25000.0, 'open_positions': 0,
               'trades_today': 0, 'daily_pnl_pct': 0.0}
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
        if (i - WINDOW) % 2000 == 0:
            done = (i - WINDOW) / max(1, stop_at - WINDOW)
            print(f'  {done * 100:5.1f}%  {len(cands)} candidates  '
                  f'{time.time() - t0:6.0f}s', flush=True)
    return {'start_ms': start_ms, 'end_ms': end_ms, 'step': step,
            'spec': spec, 'candidates': cands}


# =========================================================================== #
# stage 2: gates                                                              #
# =========================================================================== #
def _nrr(c, spec, target):
    return _net_rr(c['entry'], c['stop'], target, spec, c['lots'], c['spread_price'])


def _tp_at(c, r_mult):
    risk = abs(c['entry'] - c['stop'])
    sign = 1.0 if c['side'] == 'buy' else -1.0
    return c['entry'] + sign * risk * r_mult


def gate_current(c, spec):
    """Today's rule: net R:R to TP2 >= min_rr (1.5), else BLOCK."""
    n1, n2 = _nrr(c, spec, c['tp1']), _nrr(c, spec, c['tp2'])
    if n2 < CONFIG.risk.min_rr:
        return False, 0
    return True, (8 if n1 < 0.6 else 0)


def gate_band(c, spec):
    """BLOCK below 1.2; 1.2-1.5 is a WARN (-8 confidence) instead of a block."""
    n1, n2 = _nrr(c, spec, c['tp1']), _nrr(c, spec, c['tp2'])
    if n2 < 1.2:
        return False, 0
    pen = 8 if n2 < CONFIG.risk.min_rr else 0
    return True, pen + (8 if n1 < 0.6 else 0)


def gate_blended(c, spec):
    """What the partial-exit plan pays: 0.5 x TP1 + 0.5 x TP2 >= 1.1 net."""
    n1, n2 = _nrr(c, spec, c['tp1']), _nrr(c, spec, c['tp2'])
    if 0.5 * n1 + 0.5 * n2 < 1.1:
        return False, 0
    return True, (8 if n1 < 0.6 else 0)


def gate_none(c, spec):
    """No reward floor. The room gate (path to TP1) still applies."""
    return True, 0


def gate_tp1(floor):
    def g(c, spec):
        return (_nrr(c, spec, c['tp1_eff']) >= floor), 0
    g.__doc__ = f'net R:R to the (moved) TP1 >= {floor}'
    return g


# =========================================================================== #
# stage 2: exits                                                              #
# =========================================================================== #
class Exit:
    """
    One exit policy.

      partial     fraction closed at the first target (0 = none)
      first_r     first target in R from the SIGNAL entry (1.0 = today's TP1)
      final       'tp2' (today's structural target), a number of R, or None
      be_lock_r   when the first target prints, stop moves to entry + this R
      trail_atr   trail distance in ATR once the first target prints (None = off)
    """

    def __init__(self, name, partial=0.5, first_r=1.0, final='tp2',
                 be_lock_r=0.0, trail_atr=None):
        self.name, self.partial, self.first_r = name, partial, first_r
        self.final, self.be_lock_r, self.trail_atr = final, be_lock_r, trail_atr


def simulate(c, ex: Exit, series, m1, spec, fill, start_idx):
    """Walk forward bar by bar - the same state machine as _resolve_exit()."""
    n = len(series)
    long = c['side'] == 'buy'
    risk = abs(fill - c['stop'])
    if risk <= 0:
        return None
    sign = 1.0 if long else -1.0
    point = float(spec.get('point') or 0.01)
    slip = float(CONFIG.instrument.default_slippage_points) * point

    first_px = _tp_at(c, ex.first_r) if ex.first_r != 1.0 else c['tp1']
    if ex.final == 'tp2':
        final_px = c['tp2']
    elif ex.final is None:
        final_px = None
    else:
        final_px = _tp_at(c, float(ex.final))
    # A full exit at the first target: the "first" level IS the final one.
    if ex.partial >= 1.0:
        final_px, first_px = first_px, None

    stop = c['stop']
    took = False
    open_frac = 1.0
    r = 0.0
    trail_ref = None          # best price since the trail armed, completed bars
    mfe = 0.0
    outcome = 'expired'
    bars = 0
    last = min(n, start_idx + c['expiry'] + 1)

    def hits(h, l, stop_now):
        if long:
            return (l <= stop_now,
                    first_px is not None and not took and h >= first_px,
                    final_px is not None and h >= final_px)
        return (h >= stop_now,
                first_px is not None and not took and l <= first_px,
                final_px is not None and l <= final_px)

    for i in range(start_idx, last):
        h, l = float(series.h[i]), float(series.l[i])
        mfe = max(mfe, ((h - fill) if long else (fill - l)) / risk)
        hs, h1, h2 = hits(h, l, stop)

        if hs and (h1 or h2):
            # Settle on M1 exactly as _resolve_on_m1 does: first event wins.
            res = None
            if m1 is not None:
                t0 = int(series.t[i])
                lo_i = int(np.searchsorted(m1.t, t0, 'left'))
                hi_i = int(np.searchsorted(m1.t, t0 + TF_SECONDS[TF] * 1000, 'left'))
                for j in range(lo_i, hi_i):
                    a, b, cc = hits(float(m1.h[j]), float(m1.l[j]), stop)
                    if a:
                        res = (True, False, False)
                        break
                    if cc:
                        res = (False, False, True)
                        break
                    if b:
                        res = (False, True, False)
                        break
            hs, h1, h2 = res if res is not None else (True, False, False)

        if hs:
            px = stop - slip if long else stop + slip
            r += open_frac * sign * (px - fill) / risk
            outcome = ('trail' if trail_ref is not None and stop != c['stop']
                       and ex.trail_atr else ('first' if took else 'stop'))
            bars = i - start_idx + 1
            open_frac = 0.0
            break
        if h2:
            r += open_frac * sign * (final_px - fill) / risk
            outcome = 'target'
            bars = i - start_idx + 1
            open_frac = 0.0
            break
        if h1:
            r += ex.partial * sign * (first_px - fill) / risk
            open_frac -= ex.partial
            took = True
            stop = fill + sign * ex.be_lock_r * risk
            if ex.trail_atr:
                trail_ref = h if long else l
            # no `continue`: this bar has closed, so the trail below may set
            # from it immediately rather than one bar late

        # Trail on COMPLETED bars only: this bar has closed, so it may move the
        # stop for the next one - never the bar that is being judged.
        if trail_ref is not None and ex.trail_atr:
            trail_ref = max(trail_ref, h) if long else min(trail_ref, l)
            cand = trail_ref - sign * ex.trail_atr * c['atr']
            stop = max(stop, cand) if long else min(stop, cand)

    if open_frac > 0:
        i = min(n - 1, start_idx + c['expiry'])
        if i >= start_idx:
            px = float(series.c[i])
            r += open_frac * sign * (px - fill) / risk
            bars = i - start_idx + 1

    vpu = (1.0 / float(spec.get('tick_size') or 0.01)) * float(spec.get('tick_value') or 1.0)
    commission = float(spec.get('commission_per_lot_side')
                       or CONFIG.instrument.commission_per_lot_side) * 2.0
    comm_r = commission / (risk * vpu)                 # per lot, so lots cancel
    spread_r = c['spread_price'] / risk
    return {'gross_r': r, 'bt_r': r - comm_r, 'net_r': r - comm_r - spread_r,
            'outcome': outcome, 'bars': bars, 'mfe': mfe}


# =========================================================================== #
# stage 2: the replay                                                         #
# =========================================================================== #
def evaluate(cap, series, m1, gate, ex: Exit, tp1_r=1.0, risk_pct=0.5, trace=None,
             admit=None):
    spec = cap['spec']
    point = float(spec.get('point') or 0.01)
    slip = float(CONFIG.instrument.default_slippage_points) * point
    total = len(series)
    last_key: dict = {}
    day = None
    trades_today = 0
    day_pnl = 0.0
    # Money, not R: run() measures the daily-loss limit in account currency
    # against current equity, and lots are rounded DOWN, so a -1R trade costs
    # a little under risk_per_trade_pct. Counting in R at exactly 0.5% tripped
    # the limit early and dropped six trades on one losing day in verify.
    equity = 25000.0
    vpu = (1.0 / float(spec.get('tick_size') or 0.01)) * float(spec.get('tick_value') or 1.0)
    out = []

    for c in cap['candidates']:
        c = dict(c)
        c['tp1_eff'] = _tp_at(c, tp1_r) if tp1_r != 1.0 else c['tp1']
        i = c['i']
        d = int(c['bar_t'] // 86_400_000)
        if d != day:
            day, trades_today, day_pnl = d, 0, 0.0

        recheck_room = tp1_r != 1.0
        skip = {'reward', 'daily', 'exposure'} | ({'room'} if recheck_room else set())
        if any(v == 'BLOCK' for (nm, v, _) in c['gates'] if nm not in skip):
            continue
        pen = sum(p for (nm, v, p) in c['gates'] if v == 'WARN' and nm not in skip)
        ok, rpen = gate(c, spec)
        if not ok:
            continue
        if recheck_room:
            lv = [Level(**x) for x in c['levels']]
            room = room_to_target(lv, c['entry'], c['tp1_eff'], c['side'])
            if not room['clear'] and room.get('obstacle'):
                if room['fraction'] < 0.55:
                    continue
                pen += 8
        conf = max(0, min(100, c['raw_conf'] - pen - rpen))
        if conf < CONFIG.gates.min_confidence:
            continue
        # Optional entry rule under test (tools/exp_entries.py). Sees the
        # candidate and its final confidence; False drops the signal.
        if admit is not None and not admit(c, conf):
            continue
        # daily limits, as qualify.py would have applied them with this arm's
        # own running totals
        if trades_today >= CONFIG.risk.max_daily_trades:
            if trace is not None:
                trace.append((i, 'daily trades', trades_today, day_pnl, equity))
            continue
        if day_pnl / max(equity, 1.0) * 100.0 <= -CONFIG.risk.max_daily_loss_pct:
            if trace is not None:
                trace.append((i, 'daily loss', trades_today, day_pnl, equity))
            continue

        key = (c['playbook'], c['side'])
        if i - last_key.get(key, -999) < CONFIG.gates.cooldown_bars:
            continue
        last_key[key] = i
        if c['lots'] <= 0 or i >= total:
            continue

        fill = float(series.o[i]) + (slip if c['side'] == 'buy' else -slip)
        if c['entry_type'] == 'stop':
            reached = (float(series.h[i]) >= c['trigger'] if c['side'] == 'buy'
                       else float(series.l[i]) <= c['trigger'])
            if not reached:
                continue
            fill = c['trigger'] + (slip if c['side'] == 'buy' else -slip)

        res = simulate(c, ex, series, m1, spec, fill, i + 1)
        if res is None:
            continue
        res['playbook'] = c['playbook']
        res['entry_t'] = int(series.t[i])
        res['side'] = c['side']
        out.append(res)
        trades_today += 1
        # run()'s daily-loss limit counts commission but not spread; mirror it
        # so the limit trips on the same day it would in the real backtest.
        # run() sizes from CURRENT equity; capture sized at a fixed 25,000.
        # Rounded to the 0.01 lot step the way size_position() does. R is
        # unaffected - this only matters for when the daily-loss limit trips,
        # which verify showed can be a knife edge (-1.96% vs -2.00%).
        lots_now = max(0.01, int(c['lots'] * equity / 25000.0 * 100) / 100.0)
        pnl = res['bt_r'] * abs(fill - c['stop']) * vpu * lots_now
        day_pnl += pnl
        equity += pnl
    return out


def stats(trades, key='net_r', risk_pct=0.5):
    if not trades:
        return None
    r = np.array([t[key] for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float('nan')
    eq = np.cumprod(1.0 + r * risk_pct / 100.0)
    peak = np.maximum.accumulate(eq)
    dd = float(((peak - eq) / peak).max() * 100)
    reached1 = np.mean([t['mfe'] >= 1.0 for t in trades]) * 100
    return {'n': len(r), 'win': len(wins) / len(r) * 100, 'pf': pf,
            'exp': r.mean(), 'sum': r.sum(), 'ret': (eq[-1] - 1) * 100,
            'dd': dd, 'bars': np.mean([t['bars'] for t in trades]),
            'reach1': reached1}


def load_period(cap):
    series = load_disk(SYMBOL, TF, None, cap['start_ms'], cap['end_ms'])
    m1 = load_disk(SYMBOL, '1m', None, cap['start_ms'], cap['end_ms'])
    return series, (None if m1.empty() else m1)


EXITS = {
    'X0': Exit('50% at TP1 1R, BE, rest to TP2   (today)'),
    'X1': Exit('all out at TP1 = 1.0R', partial=1.0, first_r=1.0),
    'X2': Exit('all out at TP1 = 1.3R', partial=1.0, first_r=1.3),
    'X3': Exit('all out at TP1 = 1.5R', partial=1.0, first_r=1.5),
    'T1': Exit('at 1R: BE, trail 1.0 ATR, no target', partial=0.0, final=None, trail_atr=1.0),
    'T2': Exit('50% at 1R, BE, runner trails 1.0 ATR', partial=0.5, final=None, trail_atr=1.0),
    'T3': Exit('at 1R: BE, trail 1.5 ATR, no target', partial=0.0, final=None, trail_atr=1.5),
    'T4': Exit('at 1R: lock +0.3R, trail 1.0 ATR', partial=0.0, final=None,
               be_lock_r=0.3, trail_atr=1.0),
    'T5': Exit('50% at 1R, lock +0.3R, trail 0.7 ATR', partial=0.5, final=None,
               be_lock_r=0.3, trail_atr=0.7),
}

GATES = {
    'current  TP2 >= 1.5': gate_current,
    'band     1.2-1.5 warns': gate_band,
    'blended  >= 1.1': gate_blended,
    'none     (room only)': gate_none,
}


def _row(label, s):
    if s is None:
        return f'  {label:<44} {"no trades":>10}'
    pf = f'{s["pf"]:.2f}' if s['pf'] == s['pf'] else '  - '
    return (f'  {label:<44} {s["n"]:>5} {s["win"]:>5.1f}% {pf:>5} '
            f'{s["exp"]:>+7.3f} {s["sum"]:>+8.1f} {s["ret"]:>+7.1f}% '
            f'{s["dd"]:>5.1f}% {s["reach1"]:>5.1f}% {s["bars"]:>5.1f}')


HEAD = (f'  {"":<44} {"trades":>5} {"win":>6} {"PF":>5} {"E[R]":>7} '
        f'{"sum R":>8} {"return":>8} {"maxDD":>6} {">=1R":>6} {"bars":>5}')


def report(labels):
    for label in labels:
        cap = pickle.loads((OUT / f'exp_exits_{label}.pkl').read_bytes())
        series, m1 = load_period(cap)
        a = dt.datetime.fromtimestamp(cap['start_ms'] / 1000)
        b = dt.datetime.fromtimestamp(cap['end_ms'] / 1000)
        print(f'\n{"=" * 118}\n  {label}:  {a:%Y-%m-%d} -> {b:%Y-%m-%d}   '
              f'{len(cap["candidates"])} candidate signals   costs: commission + spread + slippage'
              f'   risk 0.5%/trade compounding\n{"=" * 118}')

        print('\n  1. EXIT ONLY - entries fixed by today\'s gate, exit varies')
        print(HEAD)
        for k, ex in EXITS.items():
            print(_row(f'{k}  {ex.name}', stats(evaluate(cap, series, m1, gate_current, ex))))

        print('\n  2. GATE ONLY - today\'s exit, reward gate varies')
        print(HEAD)
        for gname, g in GATES.items():
            print(_row(gname, stats(evaluate(cap, series, m1, g, EXITS['X0']))))

        print('\n  3. TRADE-TO-TP1 PLANS - TP2 is irrelevant, so no TP2 floor')
        print(HEAD)
        plans = [
            ('X1  all out 1.0R | room only', gate_none, EXITS['X1'], 1.0),
            ('X2  all out 1.3R | TP1 net >= 1.10', gate_tp1(1.10), EXITS['X2'], 1.3),
            ('X3  all out 1.5R | TP1 net >= 1.25', gate_tp1(1.25), EXITS['X3'], 1.5),
            ('T1  BE + trail 1.0 ATR | room only', gate_none, EXITS['T1'], 1.0),
            ('T2  50% + trail 1.0 ATR | room only', gate_none, EXITS['T2'], 1.0),
            ('T3  BE + trail 1.5 ATR | room only', gate_none, EXITS['T3'], 1.0),
            ('T4  +0.3R + trail 1.0 ATR | room only', gate_none, EXITS['T4'], 1.0),
            ('T5  50% +0.3R trail 0.7 | room only', gate_none, EXITS['T5'], 1.0),
        ]
        for name, g, ex, tp1_r in plans:
            print(_row(name, stats(evaluate(cap, series, m1, g, ex, tp1_r=tp1_r))))


def verify(start_ms, end_ms):
    """Arm A with run()'s own accounting must reproduce backtest.run()."""
    from server.engine import backtest as bt
    t0 = time.time()
    ref = bt.run(SYMBOL, TF, start_ms=start_ms, end_ms=end_ms, equity=25000, step=1)
    print(f'backtest.run: {ref["summary"]["trades"]} trades in {time.time() - t0:.0f}s')
    cap = capture(start_ms, end_ms)
    series, m1 = load_period(cap)
    got = evaluate(cap, series, m1, gate_current, EXITS['X0'])
    ref_r = sum(t['r_multiple'] for t in ref['trades'])
    my_r = sum(t['gross_r'] for t in got)
    print(f'harness     : {len(got)} trades')
    print(f'total gross R  run() {ref_r:+.2f}   harness {my_r:+.2f}')
    ref_keys = {(t['entry_t'], t['side'], t['playbook']) for t in ref['trades']}
    my_keys = {(t['entry_t'], t['side'], t['playbook']) for t in got}
    only_ref = sorted(ref_keys - my_keys)
    only_me = sorted(my_keys - ref_keys)
    print(f'trades only in run(): {len(only_ref)}   only in harness: {len(only_me)}')
    for k in only_ref[:12]:
        print('   run() only :', dt.datetime.utcfromtimestamp(k[0] / 1000), k[1], k[2])
    for k in only_me[:12]:
        print('   harness only:', dt.datetime.utcfromtimestamp(k[0] / 1000), k[1], k[2])
    OUT.mkdir(exist_ok=True)
    pickle.dump({'ref': ref['trades'], 'got': got, 'cap': cap},
                open(OUT / 'exp_exits_verify.pkl', 'wb'))
    spread_cost = sum(t['bt_r'] - t['net_r'] for t in got)
    print(f'spread run() never charges: {spread_cost:.2f}R over {len(got)} trades '
          f'({spread_cost / max(1, len(got)):.3f}R per trade)')


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('capture')
    c.add_argument('--label', required=True)
    c.add_argument('--start', required=True)
    c.add_argument('--end', required=True)
    c.add_argument('--step', type=int, default=1)
    v = sub.add_parser('verify')
    v.add_argument('--start', required=True)
    v.add_argument('--end', required=True)
    r = sub.add_parser('report')
    r.add_argument('--labels', nargs='+', required=True)
    a = ap.parse_args()

    if a.cmd == 'capture':
        t0 = time.time()
        cap = capture(_ms(a.start), _ms(a.end), a.step)
        OUT.mkdir(exist_ok=True)
        (OUT / f'exp_exits_{a.label}.pkl').write_bytes(pickle.dumps(cap))
        print(f'captured {len(cap["candidates"])} candidates in {time.time() - t0:.0f}s')
    elif a.cmd == 'verify':
        verify(_ms(a.start), _ms(a.end))
    else:
        report(a.labels)
    return 0


if __name__ == '__main__':
    sys.exit(main())
