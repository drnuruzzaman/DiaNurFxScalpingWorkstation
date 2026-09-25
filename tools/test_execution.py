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
from server.signal_store import (CANCELLED, CLOSED, EXPIRED, FILLED,  # noqa: E402
                                 FINAL, REVERSED, SENT, SignalStore)

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
        if state.get('verdict') is None:
            return None                     # no fresh closed-bar analysis
        return SimpleNamespace(status=state['verdict'], reason='test',
                               gates=state.get('gates') or [],
                               # Which closed bar the verdict came from. The
                               # executor judges a pending order once per bar,
                               # so a test that wants a second verdict has to
                               # advance this the way a real close would.
                               judged_bar_ms=state.get('judged_bar_ms', 0),
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
    # The CONTRACT is that the tag leads, not that it is the whole comment -
    # reconciliation matches with startswith(). Asserting equality made this
    # test fail the moment the playbook was appended, which is a change the
    # contract always allowed.
    comment = sent[0][1]['comment'] if sent else ''
    check('the order comment leads with the signal tag',
          comment.startswith(tag_for(fid)), comment)
    check('...and carries the timeframe and the playbook',
          f' {TF} ' in comment and comment.endswith('PATBREAK'), comment)
    check('...within the 31 characters MT5 keeps', len(comment) <= 31, f'{len(comment)}')
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

    # Fixed lots: every order goes out at the configured size for its class,
    # whatever the risk model sized it at - gold lots_gold, anything else
    # lots_non_gold. Checked end to end on a sent order, then on the one
    # function every order uses.
    saved = (CONFIG.execution.lots_gold, CONFIG.execution.lots_non_gold)
    try:
        CONFIG.execution.lots_gold, CONFIG.execution.lots_non_gold = 0.01, 0.05
        for raw in (0.5, 0.001, 0.02):
            r2 = rig(lots=raw)
            f2 = r2.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)[0]
            r2.bridge.quote = {'bid': 4000.5, 'ask': 4000.8}
            r2.ex.step()
            got = (r2.ledger.get(f2) or {}).get('lots')
            check(f'gold sized {raw} by risk is sent at the fixed 0.01', got == 0.01, f'got {got}')

        r3 = rig()
        check('a non-gold symbol trades the fixed non-gold size (0.05)',
              r3.ex._lots('USDJPY.a') == 0.05, str(r3.ex._lots('USDJPY.a')))
        check('...for any non-gold symbol', r3.ex._lots('EURUSD.a') == 0.05)
        check('gold trades lots_gold, not the non-gold size', r3.ex._lots('XAUUSD.a') == 0.01)
        CONFIG.execution.lots_gold = 0.02
        check('changing lots_gold changes the gold size', r3.ex._lots('XAUUSD.a') == 0.02)
        CONFIG.execution.lots_gold = 0.0149
        check('a size off the 0.01 step is rounded to it', r3.ex._lots('XAUUSD.a') == 0.01)
    finally:
        CONFIG.execution.lots_gold, CONFIG.execution.lots_non_gold = saved

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

    # -------------------- a pending order is re-judged on every CLOSED bar
    def gate(name, detail='gone'):
        return {'name': name, 'verdict': 'BLOCK', 'detail': detail, 'penalty': 0}

    def pending(**kw):
        """A rig with one order resting at its entry, unfilled."""
        r = rig(**kw)
        r.state['judged_bar_ms'] = bar
        fid = r.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)[0]
        r.bridge.quote = {'bid': 4005.7, 'ask': 4006.0}   # above entry -> LIMIT
        r.ex.step()
        return r, fid

    # invalidated on a closed bar: pulled
    r9, f9 = pending()
    r9.state['verdict'] = 'rejected'
    r9.state['gates'] = [gate('mtf', 'higher timeframes now disagree')]
    r9.state['judged_bar_ms'] = bar + TF_MS          # a bar closed
    r9.ex.step()
    check('a pending order is cancelled when the closed bar invalidates it',
          r9.store.get(f9)['stage'] == CANCELLED and r9.bridge.orders_ == [],
          r9.store.get(f9)['history'][-1][2])

    # the SAME bar is never judged twice, however often the executor ticks
    r10, f10 = pending()
    r10.state['verdict'] = 'qualified'
    r10.state['judged_bar_ms'] = bar + TF_MS
    r10.ex.step()                                    # judges that bar: fine
    r10.state['verdict'] = 'rejected'                # mid-bar wobble
    r10.state['gates'] = [gate('spread', 'spread 180 points')]
    r10.ex.step(); r10.ex.step(); r10.ex.step()
    check('a mid-bar change does not re-judge the same closed bar',
          r10.store.get(f10)['stage'] == SENT and len(r10.bridge.orders_) == 1)
    # ...until the next bar actually closes with it still blocked
    r10.state['judged_bar_ms'] = bar + 2 * TF_MS
    r10.ex.step()
    check('...and is cancelled when the NEXT bar closes still blocked',
          r10.store.get(f10)['stage'] == CANCELLED and r10.bridge.orders_ == [],
          r10.store.get(f10)['history'][-1][2])

    # a setup that recovers before the close is never pulled
    r11, f11 = pending()
    r11.state['verdict'] = 'rejected'
    r11.state['gates'] = [gate('spread')]
    r11.ex.step()                                    # same bar as the send
    r11.state['verdict'] = 'qualified'
    r11.state['gates'] = []
    r11.state['judged_bar_ms'] = bar + TF_MS
    r11.ex.step()
    check('a setup that recovers by the close keeps its order',
          r11.store.get(f11)['stage'] == SENT and len(r11.bridge.orders_) == 1)

    # silence is not invalidation
    r12, f12 = pending()
    r12.state['verdict'] = None          # no closed-bar snapshot at all
    r12.ex.step()
    r12.ex.step()
    check('no fresh analysis never cancels an order',
          r12.store.get(f12)['stage'] == SENT and len(r12.bridge.orders_) == 1)

    # once filled, the stop owns the trade
    r13 = rig()
    r13.state['judged_bar_ms'] = bar
    f13 = r13.store.finalize('XAUUSD.a', TF, [signal()], SPEC, bar, TF_MS)[0]
    r13.bridge.quote = {'bid': 3999.9, 'ask': 4000.0}     # fills at market
    r13.ex.step()
    r13.ex.step()
    r13.state['verdict'] = 'rejected'
    r13.state['gates'] = [gate('mtf')]
    r13.state['judged_bar_ms'] = bar + TF_MS
    r13.ex.step()
    check('a FILLED position is never closed by re-qualification',
          r13.store.get(f13)['stage'] == FILLED and len(r13.bridge.positions_) == 1)

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

    _daily(check)


def _ms(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() * 1000)


def _daily(check) -> None:
    """
    The daily limits (server/daily.py): the broker's day, the day's sends
    counted from the ledger, and the loss limit - the two gates that used to
    be dead (P&L never computed) or wrong (a counter that never reset).
    """
    from server import clock
    from server.daily import DailyLimits, day_start_ms, judging_context
    from server.engine.qualify import qualify

    OFF = 3 * 3_600_000                            # a UTC+3 broker clock

    # ------------------------------------------------------------ the day
    last_ms = _ms('2026-09-24T20:59:59.999Z')      # 23:59:59.999 broker time
    check("the broker's day ends at the broker's midnight, not UTC's",
          day_start_ms(last_ms, OFF) == _ms('2026-09-23T21:00:00Z')
          and day_start_ms(last_ms + 1, OFF) == _ms('2026-09-24T21:00:00Z'))
    check('...and at the stamp\'s own midnight with no offset (the lab, on broker-time history)',
          day_start_ms(_ms('2026-09-24T23:59:00Z'), 0) == _ms('2026-09-24T00:00:00Z'))

    # -------------------------------------- sends, from the ledger on disk
    tmp = Path(tempfile.mkdtemp())
    now = {'t': _ms('2026-09-24T18:00:00Z')}
    clock.set_source(lambda: now['t'])
    try:
        ledger = OrderLedger(tmp / 'ledger.json')
        for i, state in enumerate(('placed', 'placed', 'refused')):
            fid = f'XAUUSD.a:5m:pattern_break:buy:{i}'
            ledger.intent(fid, {'symbol': 'XAUUSD.a', 'tf': '5m', 'side': 'buy',
                                'kind': 'market', 'lots': 0.01})
            ledger.mark(fid, state, 'test')
            now['t'] += 10 * 60_000
        day0 = day_start_ms(now['t'], OFF)
        check("orders sent today come from the order ledger - a refusal is not a send",
              ledger.placed_since(day0) == 2, str(ledger.placed_since(day0)))
        check('...so an API restart (the file read again) keeps the count',
              OrderLedger(tmp / 'ledger.json').placed_since(day0) == 2)
        now['t'] = _ms('2026-09-24T21:05:00Z')     # 00:05 the next broker day
        check("the day's count resets at the broker's midnight",
              ledger.placed_since(day_start_ms(now['t'], OFF)) == 0)
    finally:
        clock.set_source(None)

    # ------------------------- DailyLimits, against a fake bridge and clock
    calls = []
    fake = {'s': _ms('2026-09-24T20:00:00Z') / 1000, 'today': -300.0, 'deals_up': True}

    def deals():
        calls.append('deals')
        return {'deals': []} if fake['deals_up'] else None

    def health():
        calls.append('health')
        return {'time_offset_ms': OFF}

    def summarise(_deals, off):
        return {'today': fake['today'], 'day_start_ms': day_start_ms(fake['s'] * 1000, off)}

    dl = DailyLimits(deals=deals, health=health, summarise=summarise,
                     sends=lambda since: 3, now=lambda: fake['s'], background=False)
    acct = {'balance': 19_700.0, 'equity': 19_550.0, 'profit': -150.0}
    calls.clear()
    trades, pct = dl.refresh(acct)
    check('the first refresh asks the bridge for its clock offset, nothing else',
          calls == ['health'] and trades == 3, str(calls))
    fake['s'] += 1
    calls.clear()
    trades, pct = dl.refresh(acct)
    check('the next reads realised P&L - one extra bridge call per refresh, never two',
          calls == ['deals'], str(calls))
    check('daily P&L = realised today + floating, over the balance the day opened with',
          pct == -2.25, f'{pct}% (-300 realised, -150 floating, opened at 20,000)')
    fake['s'] += 5
    calls.clear()
    dl.refresh(acct)
    check('...and the read is cached between refreshes', calls == [], str(calls))
    fake['s'] = _ms('2026-09-24T21:00:30Z') / 1000    # 00:00:30 the next broker day
    fake['deals_up'] = False
    calls.clear()
    trades, pct = dl.refresh({'balance': 19_700.0, 'equity': 19_700.0, 'profit': 0.0})
    check("past the broker's midnight yesterday's loss stops counting - even before a new read lands",
          pct == 0.0 and len(calls) == 1, f'{pct}% {calls}')
    fake['deals_up'], fake['today'] = True, -120.0
    fake['s'] += 1
    calls.clear()
    trades, pct = dl.refresh({'balance': 19_580.0, 'equity': 19_580.0, 'profit': 0.0})
    check("...and the new day's realised P&L is read at once, not 20s later",
          calls == ['deals'] and pct == round(-120 / 19_700 * 100, 3), f'{pct}% {calls}')

    # ------------------------------------------ the gate that acts on them
    saved = (CONFIG.risk.max_daily_loss_pct, CONFIG.risk.max_daily_trades)
    try:
        CONFIG.risk.max_daily_loss_pct, CONFIG.risk.max_daily_trades = 2.0, 24
        snap = {'atr': 10.0, 'tf': TF, 'price': 4000.0}
        base = {'equity': 19_550.0, 'open_positions': 0, 'spread_points': 10.0,
                'trades_today': 0, 'daily_pnl_pct': 0.0}

        def gate(ctx):
            q = qualify(signal(), snap, SPEC, ctx)
            return next(g for g in q.gates if g['name'] == 'daily')

        g = gate(dict(base, daily_pnl_pct=-2.25))
        check('down 2.25% against a 2% limit: the daily gate BLOCKS', g['verdict'] == 'BLOCK', g['detail'])
        g = gate(dict(base, daily_pnl_pct=-1.5))
        check('down 1.5%: it passes', g['verdict'] == 'PASS', g['detail'])
        full = dict(base, trades_today=24)
        g = gate(full)
        check("24 orders sent today: a 25th is blocked", g['verdict'] == 'BLOCK', g['detail'])
        g = gate(judging_context(full, 'SENT'))
        check('...but a pending order already sent is never cancelled by the cap it was placed under',
              g['verdict'] == 'PASS', g['detail'])
        g = gate(judging_context(dict(full, daily_pnl_pct=-2.5), 'SENT'))
        check('the loss limit still pulls a pending order', g['verdict'] == 'BLOCK', g['detail'])
    finally:
        CONFIG.risk.max_daily_loss_pct, CONFIG.risk.max_daily_trades = saved


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
