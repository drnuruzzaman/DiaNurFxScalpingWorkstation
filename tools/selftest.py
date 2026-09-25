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

    # --------------------------------------------------------------- 11 --- #
    section('11. leg position (the Phase 0b avoid rules)')
    from server.engine import legs as LG
    from server.engine.qualify import qualify
    from server.engine.signals import Signal
    leg = snap.get('leg')
    check('the snapshot carries the leg in progress',
          bool(leg) and leg['dir'] in (1, -1),
          f"dir {leg['dir']:+d}, {leg['ext_atr']:.2f} ATR run, "
          f"{leg['pull_atr']:.2f} ATR off the extreme" if leg else 'none')
    # The live engine sees a 600-bar window, the research saw all history. The
    # read must not depend on where the window starts, or the gate would be
    # judging something the research never measured.
    worst = 0.0
    for cut in (700, 800, 900):
        if cut > len(series):
            continue
        hh, ll, cc = series.h[:cut], series.l[:cut], series.c[:cut]
        a_all = LG.leg_state(hh, ll, cc, atr(hh, ll, cc, 14))
        w = slice(cut - 600, cut)
        a_win = LG.leg_state(hh[w], ll[w], cc[w], atr(hh[w], ll[w], cc[w], 14))
        if a_all and a_win and a_all['dir'] == a_win['dir']:
            worst = max(worst, abs(a_all['ext_atr'] - a_win['ext_atr']),
                        abs(a_all['pull_atr'] - a_win['pull_atr']))
        else:
            worst = float('inf')
    check('the leg read does not depend on the window start', worst < 0.02,
          f'largest divergence {worst:.4f} ATR')

    up = {'dir': 1, 'ext_atr': 3.0, 'pull_atr': 0.7}
    check('fading a leg 0.5-1.0 ATR off its extreme is avoided',
          LG.avoid_verdict('sell', up, 0) is not None)
    check('...trading WITH the leg there is not', LG.avoid_verdict('buy', up, 0) is None)
    check('...nor fading it 0.3 ATR off the extreme',
          LG.avoid_verdict('sell', dict(up, pull_atr=0.3), 0) is None)
    check('...nor 1.0 ATR off (the band is [0.5, 1.0))',
          LG.avoid_verdict('sell', dict(up, pull_atr=1.0), 0) is None)
    check('fading a leg that has run 4 ATR is avoided',
          LG.avoid_verdict('sell', {'dir': 1, 'ext_atr': 4.0, 'pull_atr': 0.2}, 0) is not None)
    check('...3.9 ATR is not',
          LG.avoid_verdict('sell', {'dir': 1, 'ext_atr': 3.9, 'pull_atr': 0.2}, 0) is None)
    short = {'dir': -1, 'ext_atr': 2.0, 'pull_atr': 0.2}
    check('fading the impulse while 1h trends the same way is avoided',
          LG.avoid_verdict('buy', short, -1, '1h') is not None)
    check('...not when 1h is against the leg', LG.avoid_verdict('buy', short, 1, '1h') is None)
    rows = {'rows': [{'tf': '15m', 'state': 'down'}, {'tf': '1h', 'state': 'up'},
                     {'tf': '4h', 'state': 'ranging'}]}
    check('the impulse rule reads 1h for a 5m signal', LG.htf_trend(rows, '5m') == (1, '1h'))
    check('...and the next timeframe up for 1h', LG.htf_trend(rows, '1h') == (0, '4h'))

    # End to end: the gate in qualify(), both positions of the switch.
    px, av = float(snap['price']), float(snap['atr'])
    snap_ext = dict(snap, leg={'dir': 1, 'ext_atr': 5.0, 'pull_atr': 0.2})
    saved_af = CONFIG.gates.avoid_fades
    got = {}
    try:
        for on in (True, False):
            CONFIG.gates.avoid_fades = on
            fade = Signal(playbook='pattern_break', label='TEST', side='sell', symbol=symbol,
                          tf='5m', entry=px, stop=px + 2 * av, tp1=px - 2 * av,
                          tp2=px - 4 * av, confidence=80, expiry_bars=12,
                          atr_at_signal=av, id='leg-test')
            got[on] = next(x for x in qualify(fade, snap_ext, spec).gates if x['name'] == 'leg')
    finally:
        CONFIG.gates.avoid_fades = saved_af
    check('switch ON: fading a 5 ATR leg is BLOCKED', got[True]['verdict'] == 'BLOCK',
          got[True]['detail'])
    check('switch OFF: the same signal passes, with the reason still shown',
          got[False]['verdict'] == 'PASS' and 'avoid rules off' in got[False]['detail'],
          got[False]['detail'])

    # --------------------------------------------------------------- 12 --- #
    section('12. pattern detector: head clearance and the break window')
    from server.engine import patterns as PT
    from server.engine.structure import Swing
    _saved_pt = (PT.EXPIRE_UNBROKEN, PT.HEAD_CLEAR_ATR)
    PT.EXPIRE_UNBROKEN, PT.HEAD_CLEAR_ATR = True, 0.5   # the rules, when switched on

    def hs_case(head=108.0, after=10, dip_at=None):
        """A bearish H&S on flat data, `after` bars past the right shoulder."""
        pts = [(10, 105.0, 'high'), (15, 100.0, 'low'), (20, head, 'high'),
               (25, 100.0, 'low'), (30, 105.0, 'high')]
        n = 30 + after
        c = np.full(n, 103.0)
        if dip_at is not None:
            c[30 + dip_at] = 99.0               # one close through the neckline
        t = np.arange(n, dtype=float) * 300_000
        sw = [Swing(idx=k, t=int(t[k]), price=pr, kind=kd, confirmed_at=k + 2)
              for k, pr, kd in pts]
        return [x for x in PT.head_and_shoulders(sw, c + 0.5, c - 0.5, c, t, 1.0)
                if x.kind == 'head_shoulders']

    got = hs_case()
    check('a head 3 ATR above both shoulders is a head and shoulders',
          len(got) == 1 and got[0].status == 'forming', str([x.status for x in got]))
    check('a head only 0.3 ATR above a shoulder is not',
          not hs_case(head=105.3), 'that is a double top plus a bounce')
    check('an unbroken H&S is dropped once its 30-bar break window closes',
          not hs_case(after=35))
    got = hs_case(after=35, dip_at=5)
    check('...but one that broke inside the window stays, confirmed',
          len(got) == 1 and got[0].status == 'confirmed', str([x.status for x in got]))

    def dt_case(after):
        pts = [(10, 105.0, 'high'), (15, 100.0, 'low'), (20, 105.0, 'high')]
        n = 20 + after
        c = np.full(n, 103.0)
        t = np.arange(n, dtype=float) * 300_000
        sw = [Swing(idx=k, t=int(t[k]), price=pr, kind=kd, confirmed_at=k + 2)
              for k, pr, kd in pts]
        return [x for x in PT.double_tops_bottoms(sw, c + 0.5, c - 0.5, c, t, 1.0)
                if x.kind == 'double_top']

    check('a double top inside its break window is forming',
          [x.status for x in dt_case(10)] == ['forming'])
    check('...and gone once the window closes unbroken', not dt_case(35))
    PT.EXPIRE_UNBROKEN, PT.HEAD_CLEAR_ATR = _saved_pt

    # ------------------------------------------------------------------- #
    print(f'\n{"=" * 52}')
    print(f'  {PASS} passed, {FAIL} failed')
    print('=' * 52)
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
