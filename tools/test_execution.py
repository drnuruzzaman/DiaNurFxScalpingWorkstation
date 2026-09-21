#!/usr/bin/env python
"""
tools/test_execution.py - the execution layer against a FAKE broker.

Nothing here can reach MT5: the executor is handed a FakeBridge that keeps
orders and positions in lists. Run standalone, or from tools/selftest.py.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import CONFIG                                     # noqa: E402
from server.engine.signals import Signal                             # noqa: E402
from server.executor import Executor                                 # noqa: E402
from server.order_ledger import OrderLedger, tag_for                 # noqa: E402
from server.signal_store import (CLOSED, EXPIRED, FILLED, FINAL,      # noqa: E402
                                 REVERSED, SENT, SignalStore)

TF = '5m'
TF_MS = 300_000
SPEC = {'symbol': 'XAUUSD.a', 'digits': 2, 'point': 0.01, 'tick_size': 0.01,
        'stops_level_points': 0, 'volume_step': 0.01}


class FakeBridge:
    def __init__(self):
        self.positions_ = []
        self.orders_ = []
        self.deals_ = []
        self.calls = []
        self.next_ticket = 5000
        self.down = False            # bridge unreachable
        self.lose_reply = False      # order placed but the answer never arrives
        self.quote = {'bid': 0.0, 'ask': 0.0}

    def positions(self, strict=False):
        return None if self.down else [dict(p) for p in self.positions_]

    def orders(self, strict=False):
        return None if self.down else [dict(o) for o in self.orders_]

    def deals(self, days=3):
        return None if self.down else {'deals': [dict(d) for d in self.deals_]}

    def trade(self, path, **p):
        self.calls.append((path, p))
        if path == '/order/send':
            self.next_ticket += 1
            t = self.next_ticket
            if p['kind'] == 'market':
                px = self.quote['ask'] if p['side'] == 'buy' else self.quote['bid']
                self.positions_.append({'ticket': t, 'identifier': t, 'symbol': p['symbol'],
                                        'side': p['side'], 'price_open': px, 'sl': p['sl'],
                                        'tp': p['tp'], 'comment': p['comment'],
                                        'time_ms': int(time.time() * 1000),
                                        'price_current': px, 'magic': 778899})
            else:
                self.orders_.append({'ticket': t, 'symbol': p['symbol'], 'comment': p['comment'],
                                     'price_open': p['price'], 'sl': p['sl']})
            if self.lose_reply:
                return 0, {'error': 'timed out'}
            return 200, {'ok': True, 'ticket': t, 'price': self.quote['ask']}
        if path == '/order/cancel':
            self.orders_ = [o for o in self.orders_ if o['ticket'] != p['ticket']]
            return 200, {'ok': True}
        if path == '/order/modify':
            for pos in self.positions_:
                if pos['ticket'] == p['ticket']:
                    pos['sl'] = p['sl']
            return 200, {'ok': True}
        return 404, {'error': 'no route'}


class FakeFeed:
    def __init__(self, bridge):
        self.b = bridge
        self.series = None

    def quote(self, symbol):
        return dict(self.b.quote)

    def spec(self, symbol):
        return dict(SPEC)

    def bars(self, symbol, tf, n, live=True):
        return self.series


class _S:
    """Bars: (high, low) per row, one timeframe apart starting at t0."""
    def __init__(self, t0, rows):
        self.t = np.array([t0 + i * TF_MS for i in range(len(rows))], dtype=np.int64)
        self.h = np.array([r[0] for r in rows], dtype=float)
        self.l = np.array([r[1] for r in rows], dtype=float)

    def __len__(self):
        return len(self.t)


def signal(side='buy', entry=4000.0, stop=3990.0, playbook='pattern_break', atr=10.0):
    d = 1 if side == 'buy' else -1
    return Signal(playbook=playbook, label='TEST', side=side, symbol='XAUUSD.a', tf=TF,
                  entry=entry, stop=stop, tp1=entry + 10 * d, tp2=entry + 25 * d,
                  confidence=70, expiry_bars=12, atr_at_signal=atr, id='x')


def rig(auto=True, armed=True, verdict='qualified', lots=0.02):
    tmp = Path(tempfile.mkdtemp())
    store = SignalStore(tmp / 'store.json')
    ledger = OrderLedger(tmp / 'ledger.json')
    bridge = FakeBridge()
    feed = FakeFeed(bridge)
    state = {'auto': auto, 'armed': armed, 'verdict': verdict, 'lots': lots}

    def requalify(rec):
        return SimpleNamespace(status=state['verdict'], reason='test',
                               sizing={'lots': state['lots']})

    ex = Executor(store, ledger, bridge, feed, CONFIG, requalify=requalify,
                  auto=lambda: state['auto'], trading_enabled=lambda: state['armed'],
                  tf_ms=lambda tf: TF_MS, log=lambda *a: None)
    return SimpleNamespace(tmp=tmp, store=store, ledger=ledger, bridge=bridge, feed=feed,
                           ex=ex, state=state)


def run(check) -> None:
    now = int(time.time() * 1000)
    bar = (now // TF_MS) * TF_MS - TF_MS          # the bar that just closed

    # ------------------------------------------- the REAL quote, real shape
    # The fakes below hand the executor a flat quote. The real FEED.quote once
    # returned the bridge's {"quotes": {sym: ...}} envelope unopened, so the
    # executor saw no bid/ask and the Place button silently did nothing. Test
    # the real function against the bridge's real reply shape.
    import copy as _copy
    from server.datafeed import FEED as _FEED
    feed = _copy.copy(_FEED)
    feed.bridge = SimpleNamespace(quotes=lambda syms: {'quotes': {
        'XAUUSD.a': {'symbol': 'XAUUSD.a', 'bid': 4000.1, 'ask': 4000.3}}})
    q = feed.quote('XAUUSD.a') or {}
    check('FEED.quote unwraps the bridge envelope', q.get('bid') == 4000.1 and q.get('ask') == 4000.3,
          str(q)[:80])

    # ------------------------------------------------------------ the store
    r = rig()
    ids = r.store.finalize('XAUUSD.a', TF, [signal(entry=4000.004, stop=3990.006)], SPEC,
                           bar, TF_MS)
    rec = r.store.get(ids[0]) if ids else None
    check('bar close finalises a signal as FINAL', bool(rec) and rec['stage'] == FINAL,
          ids[0] if ids else 'none')
    check('FINAL levels are rounded to the tick',
          bool(rec) and rec['signal']['entry'] == 4000.0 and rec['signal']['stop'] == 3990.01,
          f"entry {rec['signal']['entry'] if rec else '-'} stop {rec['signal']['stop'] if rec else '-'}")
    again = r.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)
    check('the same bar never finalises twice', again == [])
    later = r.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar + TF_MS, TF_MS)
    check('a live side is not re-issued on the next bar', later == [])
    reloaded = SignalStore(r.tmp / 'store.json')
    check('the store survives a restart', ids and reloaded.get(ids[0]) is not None)
    check('bar_closed fires once per new bar',
          r.store.bar_closed('XAUUSD.a', TF, bar) and not r.store.bar_closed('XAUUSD.a', TF, bar)
          and r.store.bar_closed('XAUUSD.a', TF, bar + TF_MS))
    rev = r.store.finalize('XAUUSD.a', TF, [signal('sell', 4000.0, 4010.0)], SPEC,
                           bar + 2 * TF_MS, TF_MS)
    check('an opposite FINAL reverses an unsent one',
          r.store.get(ids[0])['stage'] == REVERSED and len(rev) == 1)

    # ------------------------------------------------------- sending rules
    r = rig(auto=False)
    fid = r.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)[0]
    r.bridge.quote = {'bid': 4000.5, 'ask': 4000.8}
    r.ex.step()
    check('auto off: nothing is sent', r.bridge.calls == [])

    r.state['auto'], r.state['armed'] = True, False
    r.ex.step()
    check('bridge not armed: nothing is sent', r.bridge.calls == [])

    r.state['armed'], r.state['verdict'] = True, 'watch'
    r.ex.step()
    check('not qualified at send time: nothing is sent', r.bridge.calls == [],
          (r.store.get(fid) or {}).get('note', ''))

    r.state['verdict'] = 'qualified'
    r.ex.step()
    sent = [c for c in r.bridge.calls if c[0] == '/order/send']
    check('within tolerance: a MARKET order is sent',
          len(sent) == 1 and sent[0][1]['kind'] == 'market', str(sent[0][1] if sent else ''))
    check('the order carries the take-profit the setting asks for',
          bool(sent) and sent[0][1]['tp'] == (4025.0 if CONFIG.execution.broker_tp == 'tp2'
                                             else sent[0][1]['tp']))
    check('the order carries the signal tag', bool(sent) and sent[0][1]['comment'] == tag_for(fid))
    r.ex.step()
    check('the position is found: SENT -> FILLED', r.store.get(fid)['stage'] == FILLED)
    r.ex.step()
    check('one order per signal, ever',
          len([c for c in r.bridge.calls if c[0] == '/order/send']) == 1)

    # auto only on the timeframes switched on for it; the Place button ignores that
    ra = rig()
    fa = ra.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)[0]
    ra.bridge.quote = {'bid': 4000.5, 'ask': 4000.8}
    saved = CONFIG.execution.auto_timeframes
    try:
        CONFIG.execution.auto_timeframes = ('1h', '4h')
        ra.ex.step()
        check('auto skips a timeframe that is not enabled for it', ra.bridge.calls == [],
              (ra.store.get(fa) or {}).get('note', ''))
        ra.ex.send_now(fa)
        check('...but the Place button still sends it',
              len([c for c in ra.bridge.calls if c[0] == '/order/send']) == 1)
    finally:
        CONFIG.execution.auto_timeframes = saved

    # the take-profit MT5 holds follows the setting
    saved_tp = CONFIG.execution.broker_tp
    try:
        for mode, want in (('none', 0.0), ('tp1', 4010.0), ('tp2', 4025.0), ('cap', 4030.0)):
            CONFIG.execution.broker_tp = mode
            rt = rig()
            rt.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)
            rt.bridge.quote = {'bid': 4000.5, 'ask': 4000.8}
            rt.ex.step()
            sent_tp = [c[1]['tp'] for c in rt.bridge.calls if c[0] == '/order/send']
            check(f'broker TP "{mode}" sends tp={want}', sent_tp == [want], f'sent {sent_tp}')
    finally:
        CONFIG.execution.broker_tp = saved_tp

    # lots clamp
    for raw, want in ((0.5, 0.03), (0.001, 0.01), (0.02, 0.02)):
        r2 = rig(lots=raw)
        f2 = r2.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)[0]
        r2.bridge.quote = {'bid': 4000.5, 'ask': 4000.8}
        r2.ex.step()
        got = (r2.ledger.get(f2) or {}).get('lots')
        check(f'lots {raw} is clamped to {want}', got == want, f'got {got}')

    # pending when price has moved
    for ask, kind in ((4006.0, 'limit'), (3994.5, 'stop')):
        r3 = rig()
        r3.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)
        r3.bridge.quote = {'bid': ask - 0.3, 'ask': ask}
        r3.ex.step()
        s3 = [c for c in r3.bridge.calls if c[0] == '/order/send']
        check(f'buy with ask {ask}: pending {kind.upper()} at the frozen entry',
              len(s3) == 1 and s3[0][1]['kind'] == kind and s3[0][1]['price'] == 4000.0,
              str(s3[0][1] if s3 else 'nothing sent'))

    # decision C: one per symbol and timeframe
    r4 = rig()
    r4.store.finalize('XAUUSD.a', TF, [signal(playbook='pattern_break')], SPEC, bar, TF_MS)
    r4.bridge.quote = {'bid': 4000.5, 'ask': 4000.8}
    r4.ex.step()
    r4.ex.step()                                   # now FILLED
    # force a second live FINAL on the same symbol+tf, other side not involved
    r4.store.recs['XAUUSD.a:5m:flag:buy:1'] = dict(
        r4.store.active()[0], id='XAUUSD.a:5m:flag:buy:1', stage=FINAL)
    r4.ex.step()
    check('one position per symbol and timeframe',
          len([c for c in r4.bridge.calls if c[0] == '/order/send']) == 1,
          (r4.store.get('XAUUSD.a:5m:flag:buy:1') or {}).get('note', ''))

    # a lost reply is never re-sent until the broker is searched
    r5 = rig()
    f5 = r5.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)[0]
    r5.bridge.quote = {'bid': 4000.5, 'ask': 4000.8}
    r5.bridge.lose_reply = True
    r5.ex.step()
    check('a lost reply is recorded as UNKNOWN', r5.ledger.get(f5)['state'] == 'unknown')
    r5.ex.step()
    check('...and is not sent again', len([c for c in r5.bridge.calls
                                           if c[0] == '/order/send']) == 1)
    r5.ledger.rows[f5]['updated_ms'] -= 10_000
    r5.ex.step()
    check('...until the broker is searched and the order found by its tag',
          r5.ledger.get(f5)['state'] in ('placed', 'filled')
          and r5.store.get(f5)['stage'] in (SENT, FILLED))

    # an unreachable bridge changes nothing
    r6 = rig()
    f6 = r6.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)[0]
    r6.bridge.quote = {'bid': 4000.5, 'ask': 4000.8}
    r6.ex.step()
    r6.ex.step()
    r6.bridge.down = True
    r6.ex.step()
    check('an unreachable bridge is never read as "position closed"',
          r6.store.get(f6)['stage'] == FILLED)

    # a pending order is cancelled when the signal expires
    r7 = rig()
    f7 = r7.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)[0]
    r7.bridge.quote = {'bid': 4005.7, 'ask': 4006.0}
    r7.ex.step()
    r7.store.recs[f7]['expires_ms'] = now - 1
    r7.ex.step()
    check('an expired pending order is cancelled: EXPIRED',
          r7.store.get(f7)['stage'] == EXPIRED and r7.bridge.orders_ == []
          and any(c[0] == '/order/cancel' for c in r7.bridge.calls))

    # ------------------------------------------------------------ trailing
    r8 = rig()
    f8 = r8.store.finalize('XAUUSD.a', TF, [signal(atr=4.0)], SPEC, bar, TF_MS)[0]
    r8.bridge.quote = {'bid': 3999.9, 'ask': 4000.0}      # fill exactly at entry
    r8.ex.step()
    r8.ex.step()
    fill_ms = r8.store.get(f8)['fill_ms']
    fb = (fill_ms // TF_MS) * TF_MS
    # fill bar, then a bar through TP1 (4010) to 4012, then a bar to 4020, then forming
    r8.feed.series = _S(fb, [(4003, 3998), (4012, 4002), (4020, 4011), (4019, 4017)])
    r8.bridge.quote = {'bid': 4018.0, 'ask': 4018.3}
    r8.ex.step()
    pos = r8.bridge.positions_[0]
    # lock = 4000 + 0.5 * 10 = 4005; trail from 4020 - 4 = 4016; both closed bars
    check('at TP1 the stop locks +0.5R, then trails 1 ATR on closed bars',
          abs(pos['sl'] - 4016.0) < 1e-6, f"stop {pos['sl']}, want 4016.0")
    r8.feed.series = _S(fb, [(4003, 3998), (4012, 4002), (4020, 4011), (4019, 4017),
                              (4018, 4016.5)])
    r8.ex.step()
    check('the stop never loosens', abs(r8.bridge.positions_[0]['sl'] - 4016.0) < 1e-6,
          f"stop {r8.bridge.positions_[0]['sl']}")

    # --------------------------------------------------------------- close
    pid = r8.bridge.positions_[0]['ticket']
    r8.bridge.positions_ = []
    r8.bridge.deals_ = [{'position_id': pid, 'entry': 0, 'price': 4000.0, 'profit': 0},
                        {'position_id': pid, 'entry': 1, 'price': 4016.0, 'profit': 48.0,
                         'commission': -0.18, 'reason': 4}]
    r8.ex.step()
    c8 = r8.store.get(f8)
    check('closed by the trailed stop: CLOSED, outcome trail, +1.6R',
          c8['stage'] == CLOSED and c8['outcome'] == 'trail' and abs(c8['r'] - 1.6) < 1e-6,
          f"{c8['stage']} {c8.get('outcome')} {c8.get('r')}")


def main() -> int:
    passed = failed = 0

    def check(name, ok, detail=''):
        nonlocal passed, failed
        passed += bool(ok)
        failed += not ok
        print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f' - {detail}' if detail else ''))

    run(check)
    print(f'\n  {passed} passed, {failed} failed')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
