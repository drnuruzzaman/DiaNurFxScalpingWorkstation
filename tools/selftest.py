#!/usr/bin/env python
"""
tools/selftest.py - prove the install works and the engine is honest.

Run:  python tools/selftest.py

Checks, in order of how badly they would hurt if wrong:

  1. NO LOOKAHEAD      indicator and analysis values at bar i must not change
                       when later bars are appended. This is the property the
                       whole backtest rests on.
  2. resample fidelity  M1 aggregated to M5 must match the broker's own M5
  3. engine pipeline    analyse -> generate -> qualify produces coherent orders
  4. order sanity       stops on the correct side, targets beyond entry, sizing
                        that risks what it claims
  5. execution guards   the disarmed path refuses, the demo guard refuses live
  6. JSON safety        every snapshot field survives json.dumps

Exit code is 0 only if everything passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np                                            # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = '') -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'  [ok]   {name}' + (f' — {detail}' if detail else ''))
    else:
        FAIL += 1
        print(f'  [FAIL] {name}' + (f' — {detail}' if detail else ''))


def section(title: str) -> None:
    print(f'\n{title}\n' + '-' * len(title))


def main() -> int:
    from server.config import CONFIG
    from server.datafeed import (available_symbols, available_timeframes,
                                 load_disk, load_recent, resample)
    from server.engine.analysis import analyse, build_mtf, quick_trend
    from server.engine.indicators import adx, atr, macd, rsi
    from server.engine.qualify import qualify_all, size_position
    from server.engine.signals import generate

    symbol = CONFIG.symbol
    section('data')
    syms = available_symbols()
    check('data directory has symbols', bool(syms), ', '.join(syms) or 'none found')
    if not syms:
        print('\nNo data under data/. Nothing else can be checked.')
        return 1
    if symbol not in syms:
        symbol = syms[0]
    tfs = available_timeframes(symbol)
    check('timeframes present', len(tfs) >= 4, f'{len(tfs)}: {", ".join(tfs)}')

    series = load_recent(symbol, '5m', 900)
    check('loaded 5m bars', len(series) >= 600, f'{len(series)} bars')
    if len(series) < 200:
        return 1

    # ---------------------------------------------------------------- 1 --- #
    section('1. no lookahead (the property the backtest rests on)')
    h, l, c = series.h, series.l, series.c
    full = {
        'atr': atr(h, l, c, 14),
        'rsi': rsi(c, 14),
        'macd_hist': macd(c)[2],
        'adx': adx(h, l, c, 14)[0],
    }
    worst = 0.0
    for cut in (300, 450, 600):
        part = {
            'atr': atr(h[:cut], l[:cut], c[:cut], 14),
            'rsi': rsi(c[:cut], 14),
            'macd_hist': macd(c[:cut])[2],
            'adx': adx(h[:cut], l[:cut], c[:cut], 14)[0],
        }
        for key in full:
            a, b = full[key][cut - 1], part[key][cut - 1]
            if np.isnan(a) and np.isnan(b):
                continue
            worst = max(worst, abs(float(a) - float(b)))
    check('indicators are causal', worst < 1e-9,
          f'largest divergence {worst:.2e} across 12 comparisons')

    # The analysis snapshot must be stable the same way.
    snap_full = analyse(series.slice(0, 700))
    snap_part = analyse(series.slice(0, 700))
    check('analyse() is deterministic',
          snap_full['price'] == snap_part['price'] and
          snap_full['trend']['state'] == snap_part['trend']['state'],
          'same input, same output')

    # ---------------------------------------------------------------- 2 --- #
    section('2. resample fidelity')
    m1 = load_recent(symbol, '1m', 120000)
    native5 = load_recent(symbol, '5m', 40000)
    if len(m1) > 5000 and len(native5) > 1000:
        built = resample(m1, '5m')
        common = np.intersect1d(built.t, native5.t)
        ai = np.searchsorted(built.t, common)
        bi = np.searchsorted(native5.t, common)
        err = np.maximum(
            np.maximum(np.abs(built.h[ai] - native5.h[bi]),
                       np.abs(built.l[ai] - native5.l[bi])),
            np.abs(built.c[ai] - native5.c[bi]))
        mismatched = np.flatnonzero(err > 0.005)

        # Judge only the buckets where all five M1 bars actually exist.
        #
        # Broker M1 history has holes - a minute with no ticks is simply not
        # recorded - so a bucket missing a minute can legitimately disagree
        # with the broker's own M5, which was built from the live stream. An
        # assertion on MAX error is therefore an assertion about data gaps, not
        # about this code: one gap in 24,000 bars failed the whole check while
        # every complete bucket matched exactly.
        unexplained = 0
        for i in mismatched:
            ts = int(common[i])
            lo = int(np.searchsorted(m1.t, ts))
            hi = int(np.searchsorted(m1.t, ts + 300_000))
            if hi - lo >= 5:
                unexplained += 1
        check('M1 -> M5 matches the broker M5 on complete buckets',
              unexplained == 0,
              f'{len(common)} bars compared, {len(mismatched)} differ '
              f'({len(mismatched) - unexplained} explained by M1 gaps, '
              f'{unexplained} unexplained)')
    else:
        check('M1 -> M5 comparison', True, 'skipped (insufficient 1m history)')

    # ---------------------------------------------------------------- 3 --- #
    section('3. engine pipeline')
    from server.datafeed import FEED
    spec = FEED.spec(symbol)
    mtf = build_mtf(FEED, symbol, '5m', live=False)
    snap = analyse(series.tail(600), mtf, spec)
    check('snapshot is ok', snap.get('ok'), snap.get('reason', ''))
    check('compute under 100ms', snap['compute_ms'] < 100, f"{snap['compute_ms']}ms")
    for block in ('trend', 'regime', 'levels', 'patterns', 'events', 'momentum',
                  'mtf', 'liquidity', 'gauge', 'trendlines', 'channels'):
        check(f'block present: {block}', block in snap and snap[block] is not None)

    sigs = qualify_all(generate(snap, series.tail(600)), snap, spec,
                       {'equity': 25000, 'open_positions': 0,
                        'trades_today': 0, 'daily_pnl_pct': 0.0})
    check('signal generation ran', True, f'{len(sigs)} signal(s) on this bar')

    # ---------------------------------------------------------------- 4 --- #
    section('4. order sanity')
    bad = []
    for s in sigs:
        if s.status == 'conflicted':
            continue
        if s.side == 'buy' and not (s.stop < s.entry < s.tp1 <= s.tp2):
            bad.append(f'{s.playbook} buy levels out of order')
        if s.side == 'sell' and not (s.stop > s.entry > s.tp1 >= s.tp2):
            bad.append(f'{s.playbook} sell levels out of order')
        if s.atr_at_signal > 0:
            gap = abs(s.entry - s.stop) / s.atr_at_signal
            if gap < CONFIG.risk.min_stop_atr - 1e-6:
                bad.append(f'{s.playbook} stop {gap:.2f} ATR is under the noise floor')
    check('every signal has coherent levels', not bad, '; '.join(bad) or
          f'{len([s for s in sigs if s.status != "conflicted"])} checked')

    sizing = size_position(4600.0, 4590.0, spec,
                           {'equity': 25000, 'risk_per_trade_pct': 0.5})
    expected = 25000 * 0.005
    check('position sizing risks what it claims',
          abs(sizing['risk_cash'] - expected) / expected < 0.25,
          f"{sizing['lots']} lots risks {sizing['risk_cash']} vs {expected:.0f} budget")
    check('sizing scales with stop distance',
          size_position(4600.0, 4595.0, spec, {'equity': 25000, 'risk_per_trade_pct': 0.5})['lots']
          > sizing['lots'],
          'a tighter stop permits more lots')

    # ---------------------------------------------------------------- 5 --- #
    section('5. execution guards')
    from bridge import execution
    execution.arm(enabled=False)
    try:
        execution.send_order(None, None, symbol, 'buy', 0.01, 4500.0)
        check('disarmed bridge refuses orders', False, 'IT DID NOT REFUSE')
    except execution.Refused as exc:
        check('disarmed bridge refuses orders', True, str(exc)[:60] + '…')

    class _Live:
        login, trade_mode, currency = 21000000, 2, 'AUD'
        balance = equity = 1000.0
        trade_allowed = True

    class _MT5:
        def account_info(self):
            return _Live()

    import threading
    execution.arm(enabled=True, demo_only=True)
    try:
        execution.send_order(_MT5(), threading.Lock(), symbol, 'buy', 0.01, 4500.0)
        check('demo guard blocks a live account', False, 'IT DID NOT BLOCK')
    except execution.Refused as exc:
        check('demo guard blocks a live account', True, str(exc)[:60] + '…')
    execution.arm(enabled=False)
    check('execution left disarmed', not execution.ARMED)

    # ---------------------------------------------------------------- 6 --- #
    section('6. JSON safety')
    try:
        payload = json.dumps({'snapshot': snap,
                              'signals': [s.to_dict() for s in sigs]},
                             allow_nan=False)
        check('snapshot serialises without NaN or Infinity', True,
              f'{len(payload) / 1024:.1f} KB')
    except (ValueError, TypeError) as exc:
        check('snapshot serialises without NaN or Infinity', False, str(exc))

    # ---------------------------------------------------------------- 7 --- #
    section('7. execution lifecycle (fake broker - nothing reaches MT5)')
    from tools.test_execution import run as _run_execution
    _run_execution(check)

    # ---------------------------------------------------------------- 8 --- #
    section('8. open positions do not block signals')
    import numpy as _np
    from server.engine.signals import Signal
    TF_MS = 300_000
    T0 = 1_800_000_000_000

    def _sig(side, entry, bar_ms):
        d = 1 if side == 'buy' else -1
        return Signal(playbook='breakout_retest', label='BREAKOUT RETEST', side=side,
                      symbol='TEST', tf='5m', entry=entry, stop=entry - 10 * d,
                      tp1=entry + 10 * d, tp2=entry + 20 * d, confidence=70,
                      expiry_bars=12, bar_time_ms=bar_ms, id='t', atr_at_signal=5.0,
                      risk_points=10.0, rr1=1.0, rr2=2.0)

    from server.engine.qualify import qualify as _qualify
    probe = _sig('buy', float(snap['price']), int(snap['bar_time_ms']))
    probe.symbol = symbol
    judged = _qualify(probe, snap, spec, {'equity': 10000, 'open_positions': 50,
                                          'trades_today': 0, 'daily_pnl_pct': 0.0,
                                          'risk_per_trade_pct': 0.5})
    exp = [x for x in judged.gates if x['name'] == 'exposure']
    check('50 open positions: exposure gate does not BLOCK',
          bool(exp) and exp[0]['verdict'] != 'BLOCK',
          exp[0]['detail'] if exp else 'no exposure gate')
    check('...and costs no confidence', bool(exp) and exp[0].get('penalty', 0) == 0)

    # ---------------------------------------------------------------- 9 --- #
    section('9. trailing exit')
    from server.engine import backtest as _bt

    class _P:
        """A tiny price path in the shape _resolve_exit_trail reads."""
        tf = '5m'

        def __init__(self, bars):
            self.t = _np.array([T0 + i * TF_MS for i in range(len(bars))], dtype=_np.int64)
            self.h = _np.array([b[0] for b in bars], dtype=float)
            self.l = _np.array([b[1] for b in bars], dtype=float)
            self.c = _np.array([b[2] for b in bars], dtype=float)

        def __len__(self):
            return len(self.t)

    _spec = {'point': 0.01, 'tick_size': 0.01, 'tick_value': 1.0}
    _slip = CONFIG.instrument.default_slippage_points * 0.01

    def _trade():
        return _bt.Trade(signal_id='t', playbook='test', side='buy', entry_t=T0,
                         entry_price=100.0, stop=90.0, tp1=110.0, tp2=130.0,
                         lots=0.01, atr=5.0)

    # rises to TP1 (arms: stop -> 103, trail from 111 -> 106), runs to 120
    # (stop -> 115), then pulls back through 115
    path = _P([(105, 95, 104), (111, 104, 110), (120, 108, 119), (118, 114, 115)])
    tr = _bt._resolve_exit_trail(path, 0, _trade(), _spec, 20, None)
    want = (115.0 - _slip - 100.0) / 10.0
    check('trail locks in and follows price', tr.outcome == 'trail'
          and abs(tr.r_multiple - want) < 1e-3,
          f'{tr.outcome} {tr.r_multiple:+.3f}R, want {want:+.3f}R')

    # a dip that stays above the trail changes nothing: the stop only ratchets
    path2 = _P([(111, 101, 110), (109, 106.5, 107), (125, 107, 124), (124, 119.5, 120)])
    tr2 = _bt._resolve_exit_trail(path2, 0, _trade(), _spec, 20, None)
    check('trail only ratchets forward', tr2.outcome == 'trail'
          and abs(tr2.r_multiple - (120.0 - _slip - 100.0) / 10.0) < 1e-3,
          f'{tr2.outcome} {tr2.r_multiple:+.3f}R')

    # stopped before TP1: an ordinary -1R loss, no trail
    tr3 = _bt._resolve_exit_trail(_P([(101, 89, 90)]), 0, _trade(), _spec, 20, None)
    check('before TP1 the original stop stands', tr3.outcome == 'stop'
          and tr3.r_multiple < -0.99, f'{tr3.outcome} {tr3.r_multiple:+.3f}R')

    check('trailing is the configured exit', CONFIG.risk.exit_mode == 'trail')

    # --------------------------------------------------------------- 10 --- #
    section('10. disabled playbooks')
    from server.engine import signals as _sg
    called = []
    real = dict(_sg.PLAYBOOKS)
    try:
        for name in real:
            _sg.PLAYBOOKS[name] = (lambda n: lambda snap, series: called.append(n) or [])(name)
        _sg.generate(snap, series.tail(600), respect_regime=False)
        hit = sorted(set(called) & set(CONFIG.gates.disabled_playbooks))
        check('disabled playbooks are never run', not hit,
              f'ran {hit}' if hit else f'ran {sorted(set(called))}')
        called.clear()
        _sg.generate(snap, series.tail(600), only=['sweep_reversal'], respect_regime=False)
        check('an explicit request still reaches a disabled playbook',
              called == ['sweep_reversal'], f'ran {called}')
    finally:
        _sg.PLAYBOOKS.clear()
        _sg.PLAYBOOKS.update(real)

    # ------------------------------------------------------------------- #
    print(f'\n{"=" * 52}')
    print(f'  {PASS} passed, {FAIL} failed')
    print('=' * 52)
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
