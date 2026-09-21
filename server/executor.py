"""
The executor: walks every live signal through its lifecycle and manages the
trailing stop once a position is open.

    FINAL  -> SENT      auto mode on, bridge armed, the signal still QUALIFIED
                        right now (not when it was detected), its symbol and
                        timeframe slot free (decision C), under the global cap.
                        Entry policy (decision B): market if price is within
                        entry_tolerance_atr of the frozen entry, otherwise a
                        pending limit/stop AT the frozen entry, expiring with
                        the signal.
    SENT   -> FILLED    a position carrying this signal's tag appears.
    SENT   -> EXPIRED / REVERSED / CANCELLED   the pending order is cancelled.
    FILLED -> CLOSED    the position is gone; the closing deal says how.

Trailing, per the exit plan: until TP1 the original stop stands. Once price
has touched TP1 the stop moves to entry + trail_lock_r x R, then trails
trail_atr x ATR behind the best price of CLOSED bars. It only ever tightens -
and the bridge refuses anything else independently.

Two rules keep this from doing damage on a bad day:

  - A bridge that does not answer changes NOTHING. positions() returning None
    is never read as "every position closed".
  - An order whose outcome is unknown is never re-sent until the broker has
    been searched for it (see order_ledger.py).

Everything it talks to is injected, so the tests drive it against a fake
broker and no test can ever place an order.
"""
from __future__ import annotations

import time

from .order_ledger import tag_for
from .signal_store import (CANCELLED, CLOSED, EXPIRED, FILLED, FINAL, REVERSED, SENT,
                           round_to_tick)

RETRY_AFTER_REFUSAL_MS = 30_000
UNKNOWN_GIVE_UP_MS = 60_000


def _now() -> int:
    return int(time.time() * 1000)


class Executor:
    def __init__(self, store, ledger, bridge, feed, config, *, requalify, auto,
                 trading_enabled, on_sent=None, tf_ms=None, log=print):
        self.store, self.ledger, self.bridge, self.feed = store, ledger, bridge, feed
        self.cfg = config
        self.requalify = requalify              # rec -> Signal (fresh verdict) or None
        self.auto = auto                        # () -> bool
        self.trading_enabled = trading_enabled  # () -> bool (bridge armed)
        self.on_sent = on_sent or (lambda rec: None)
        self.tf_ms = tf_ms or (lambda tf: 300_000)
        self.log = log

    # ----------------------------------------------------------------- tick
    def step(self) -> None:
        positions = self.bridge.positions(strict=True)
        orders = self.bridge.orders(strict=True)
        if positions is None or orders is None:
            return                      # no answer: change nothing
        self._deals = None
        self._reconcile_unknown(positions, orders)
        for rec in self.store.active():
            try:
                if rec['stage'] == FINAL:
                    self._final(rec)
                elif rec['stage'] == SENT:
                    self._sent(rec, positions, orders)
                elif rec['stage'] == FILLED:
                    self._filled(rec, positions)
            except Exception as exc:                      # noqa: BLE001
                self.log(f'! executor {rec["id"]}: {type(exc).__name__}: {exc}')

    def _deals_list(self):
        if self._deals is None:
            payload = self.bridge.deals(3)
            self._deals = (payload or {}).get('deals') if isinstance(payload, dict) else None
        return self._deals

    # ---------------------------------------------------------------- FINAL
    def send_now(self, fid: str) -> str:
        """
        The Place button: the same path as auto mode, minus the auto switch.
        Every other check still applies. Returns what happened, in words.
        """
        rec = self.store.get(fid)
        if rec is None:
            return 'unknown signal'
        if rec['stage'] != FINAL:
            return f'signal is {rec["stage"]}, only a FINAL signal can be sent'
        self._final(rec, manual=True)
        after = self.store.get(fid) or {}
        row = self.ledger.get(fid) or {}
        if after.get('stage') == SENT:
            return f'sent: {row.get("kind")} {row.get("lots")} lots, ticket {row.get("ticket")}'
        return after.get('note') or row.get('events', [[0, '', 'not sent']])[-1][2] or 'not sent'

    def _final(self, rec, manual: bool = False) -> None:
        fid, sig, now = rec['id'], rec['signal'], _now()
        if now > rec['expires_ms']:
            self.store.move(fid, EXPIRED, 'expired before it was sent')
            return
        if not self.trading_enabled():
            if manual:
                self._wait(fid, 'not sent: the bridge is not armed (--enable-trading)')
            return
        if not (manual or self.auto()):
            return
        if not manual and rec['tf'] not in tuple(self.cfg.execution.auto_timeframes):
            self._wait(fid, f'waiting: {rec["tf"]} is not enabled for auto trading')
            return
        if manual:
            rec = dict(rec, retry_after_ms=0)
        if not self.ledger.may_send(fid):
            self._wait(fid, f'not sent: this signal already has an order '
                            f'({(self.ledger.get(fid) or {}).get("state")})')
            return
        if now < rec.get('retry_after_ms', 0):
            return                  # a refusal is being waited out; its note stands
        if self.cfg.execution.one_per_symbol_tf and self.ledger.live_for(rec['symbol'], rec['tf']):
            self._wait(fid, f'waiting: {rec["symbol"]} {rec["tf"]} already has a live order')
            return
        if self.ledger.live_count() >= self.cfg.risk.max_concurrent:
            self._wait(fid, f'waiting: {self.cfg.risk.max_concurrent} orders already live')
            return

        # Decision 6: qualified at SEND time, judged on the latest market.
        q = self.requalify(rec)
        if q is None or q.status != 'qualified':
            self._wait(fid, f'not sent: {getattr(q, "status", "no fresh analysis")} - '
                            f'{getattr(q, "reason", "")}')
            return

        quote = self.feed.quote(rec['symbol']) or {}
        bid, ask = float(quote.get('bid') or 0), float(quote.get('ask') or 0)
        if not (bid and ask):
            # Never silent: this exact return, fed a mis-shaped quote, is what
            # made the Place button appear to do nothing at all.
            self._wait(fid, f'not sent: no live bid/ask for {rec["symbol"]}')
            return
        buy = rec['side'] == 'buy'
        entry, stop = float(sig['entry']), float(sig['stop'])
        if (buy and bid <= stop) or (not buy and ask >= stop):
            self.store.move(fid, CANCELLED, 'price is already through the stop')
            return

        px = ask if buy else bid
        tol = self.cfg.execution.entry_tolerance_atr * float(sig.get('atr_at_signal') or 0)
        if abs(px - entry) <= tol:
            kind, price = 'market', 0.0
        else:
            # Price has moved: wait for it at the frozen entry instead of chasing.
            kind = ('limit' if px > entry else 'stop') if buy else \
                   ('limit' if px < entry else 'stop')
            price = entry

        lots = self._lots(q)
        trail = self.cfg.risk.exit_mode == 'trail'
        request = {
            'symbol': rec['symbol'], 'tf': rec['tf'], 'side': rec['side'], 'kind': kind,
            'lots': lots, 'price': price, 'sl': stop,
            # Trail mode has no fixed target: the stop does the exiting.
            'tp': self._broker_tp(sig, trail),
            'expiration_ms': rec['expires_ms'] if kind != 'market' else 0,
        }
        self.ledger.intent(fid, request)
        code, body = self.bridge.trade(
            '/order/send', symbol=rec['symbol'], side=rec['side'], lots=lots,
            sl=stop, tp=request['tp'], kind=kind, price=price,
            expiration_ms=request['expiration_ms'], comment=tag_for(fid),
            signal_id=fid, confirm=1)
        if code == 0:
            self.ledger.mark(fid, 'unknown', body.get('error', 'no answer'))
            return
        if body.get('ok'):
            self.ledger.mark(fid, 'placed', f'{kind} ticket {body.get("ticket")}',
                             ticket=body.get('ticket'),
                             fill_price=body.get('price') if kind == 'market' else None)
            self.store.move(fid, SENT, f'{kind} {lots} lots, ticket {body.get("ticket")}',
                            lots=lots, kind=kind)
            self.on_sent(rec)
        else:
            self.ledger.mark(fid, 'refused', body.get('error', f'HTTP {code}'))
            self.store.update(fid, retry_after_ms=now + RETRY_AFTER_REFUSAL_MS,
                              note=f'refused: {body.get("error", "")}')

    def _broker_tp(self, sig, trail: bool) -> float:
        """The take-profit MT5 holds for this order (see execution.broker_tp)."""
        if not trail:
            return float(sig['tp2'])            # partial plan: TP2 is the target
        mode = getattr(self.cfg.execution, 'broker_tp', 'none')
        if mode == 'tp1':
            # All out at TP1. Note the trailing stop never gets to act: the
            # position is closed by MT5 the moment TP1 prints.
            return float(sig['tp1'])
        if mode == 'tp2':
            return float(sig['tp2'])
        if mode == 'cap':
            entry, stop = float(sig['entry']), float(sig['stop'])
            sign = 1.0 if sig['side'] == 'buy' else -1.0
            spec = self.feed.spec(sig.get('symbol')) or {}
            tick = float(spec.get('tick_size') or 0.01)
            digits = int(spec.get('digits') or 2)
            return round_to_tick(entry + sign * float(self.cfg.execution.broker_tp_r)
                                 * abs(entry - stop), tick, digits)
        return 0.0

    def _lots(self, q) -> float:
        e = self.cfg.execution
        raw = float((getattr(q, 'sizing', None) or {}).get('lots') or e.min_lots)
        clamped = min(e.max_lots, max(e.min_lots, raw))
        return round(round(clamped / 0.01) * 0.01, 2)

    def _wait(self, fid, note) -> None:
        rec = self.store.get(fid)
        if rec and rec.get('note') != note:
            self.store.update(fid, note=note)

    # ----------------------------------------------------------------- SENT
    def _find_position(self, positions, ticket, tag):
        for p in positions:
            if ticket and (p.get('identifier') == ticket or p.get('ticket') == ticket):
                return p
            if tag and str(p.get('comment') or '').startswith(tag):
                return p
        return None

    def _find_order(self, orders, ticket, tag):
        for o in orders:
            if ticket and o.get('ticket') == ticket:
                return o
            if tag and str(o.get('comment') or '').startswith(tag):
                return o
        return None

    def _sent(self, rec, positions, orders) -> None:
        fid, now = rec['id'], _now()
        row = self.ledger.get(fid) or {}
        ticket, tag = row.get('ticket'), row.get('tag') or tag_for(fid)
        pos = self._find_position(positions, ticket, tag)
        if pos:
            self.ledger.mark(fid, 'filled', f'position {pos["ticket"]}',
                             position=pos['ticket'], fill_price=pos['price_open'])
            self.store.move(fid, FILLED, f'filled at {pos["price_open"]}',
                            position=pos['ticket'], fill_price=pos['price_open'],
                            fill_ms=pos.get('time_ms') or now)
            return
        pend = self._find_order(orders, ticket, tag)
        if pend:
            why = None
            if now > rec['expires_ms']:
                why = (EXPIRED, 'expired unfilled - pending order cancelled')
            elif rec.get('reverse'):
                why = (REVERSED, f'opposite FINAL {rec["reverse"]} - pending order cancelled')
            else:
                q = self.feed.quote(rec['symbol']) or {}
                bid, ask = float(q.get('bid') or 0), float(q.get('ask') or 0)
                stop = float(rec['signal']['stop'])
                if bid and ask and ((rec['side'] == 'buy' and bid <= stop)
                                    or (rec['side'] == 'sell' and ask >= stop)):
                    why = (CANCELLED, 'price went through the stop before the entry filled')
            if why:
                code, body = self.bridge.trade('/order/cancel', ticket=pend['ticket'], confirm=1)
                if body.get('ok') or 'no pending order' in str(body.get('error', '')):
                    self.ledger.mark(fid, why[0].lower(), why[1])
                    self.store.move(fid, why[0], why[1])
            return
        # Neither an order nor a position. Give a fresh order a moment to show.
        if now - row.get('updated_ms', 0) < 5_000:
            return
        deals = self._deals_list()
        if deals is None:
            return
        mine = [d for d in deals if ticket and (d.get('order') == ticket
                                                 or d.get('position_id') == ticket)]
        if any(d.get('entry') in (1, 3) for d in mine):
            self.store.move(fid, FILLED, 'filled and closed between checks',
                            position=ticket, fill_price=next(
                                (d['price'] for d in mine if d.get('entry') == 0), None))
            return
        stage = EXPIRED if now > rec['expires_ms'] else CANCELLED
        note = 'no longer at the broker' + (' (expired)' if stage == EXPIRED else '')
        self.ledger.mark(fid, stage.lower(), note)
        self.store.move(fid, stage, note)

    # --------------------------------------------------------------- FILLED
    def _filled(self, rec, positions) -> None:
        fid = rec['id']
        row = self.ledger.get(fid) or {}
        pos = self._find_position(positions, rec.get('position'), row.get('tag') or tag_for(fid))
        if pos is None:
            self._closed(rec)
            return
        if self.cfg.risk.exit_mode == 'trail':
            self._trail(rec, pos)

    def _closed(self, rec) -> None:
        fid = rec['id']
        deals = self._deals_list()
        if deals is None:
            return                      # cannot tell how it closed yet; ask again
        pid = rec.get('position')
        out = [d for d in deals if d.get('position_id') == pid and d.get('entry') in (1, 3)]
        if not out:
            return                      # history not written yet
        d = out[-1]
        close = float(d['price'])
        fill = float(rec.get('fill_price') or rec['signal']['entry'])
        risk = abs(fill - float(rec['signal']['stop'])) or 1e-9
        sign = 1.0 if rec['side'] == 'buy' else -1.0
        reason = d.get('reason')
        outcome = ('target' if reason == 5 else
                   ('trail' if rec.get('trail', {}).get('armed') else 'stop') if reason == 4
                   else 'manual')
        r = round(sign * (close - fill) / risk, 3)
        profit = round(sum(float(x.get('profit') or 0) + float(x.get('commission') or 0)
                           + float(x.get('swap') or 0) for x in out), 2)
        self.ledger.mark(fid, 'closed', f'{outcome} at {close}', close_price=close,
                         outcome=outcome, r=r, profit=profit)
        self.store.move(fid, CLOSED, f'{outcome} at {close} ({r:+.2f}R)',
                        close_price=close, outcome=outcome, r=r)

    def _trail(self, rec, pos) -> None:
        fid, sig = rec['id'], rec['signal']
        buy = rec['side'] == 'buy'
        sign = 1.0 if buy else -1.0
        fill = float(rec.get('fill_price') or pos['price_open'])
        risk = abs(fill - float(sig['stop']))
        atr = float(sig.get('atr_at_signal') or 0)
        tp1 = float(sig['tp1'])
        spec = self.feed.spec(rec['symbol']) or {}
        tick = float(spec.get('tick_size') or 0.01)
        digits = int(spec.get('digits') or 2)
        tf_ms = self.tf_ms(rec['tf'])

        series = self.feed.bars(rec['symbol'], rec['tf'], 300, live=True)
        if series is None or len(series) < 2:
            return
        t, h, l = series.t, series.h, series.l
        fill_bar = (int(rec.get('fill_ms') or 0) // tf_ms) * tf_ms
        since = [i for i in range(len(t)) if int(t[i]) >= fill_bar]
        trail = dict(rec.get('trail') or {})

        if not trail.get('armed'):
            for i in since:
                if (h[i] >= tp1) if buy else (l[i] <= tp1):
                    trail = {'armed': True, 'armed_bar_t': int(t[i])}
                    break
            cur = float(pos.get('price_current') or 0)
            if not trail.get('armed') and cur and ((cur >= tp1) if buy else (cur <= tp1)):
                trail = {'armed': True, 'armed_bar_t': int(t[-1])}
        if not trail.get('armed'):
            return

        target = fill + sign * self.cfg.risk.trail_lock_r * risk
        closed = [i for i in since if int(t[i]) >= trail['armed_bar_t'] and i < len(t) - 1]
        if closed and atr > 0:
            ref = max(float(h[i]) for i in closed) if buy else min(float(l[i]) for i in closed)
            cand = ref - sign * self.cfg.risk.trail_atr * atr
            target = max(target, cand) if buy else min(target, cand)
        # Never at or through the market, and outside the broker's minimum.
        q = self.feed.quote(rec['symbol']) or {}
        floor = max(int(spec.get('stops_level_points') or 0) * float(spec.get('point') or tick), tick)
        if buy and q.get('bid'):
            target = min(target, float(q['bid']) - floor)
        if not buy and q.get('ask'):
            target = max(target, float(q['ask']) + floor)
        target = round_to_tick(target, tick, digits)

        current = float(pos.get('sl') or 0)
        tightens = (target > current + tick / 2) if buy else \
                   (current == 0 or target < current - tick / 2)
        if tightens:
            code, body = self.bridge.trade('/order/modify', ticket=pos['ticket'],
                                           sl=target, confirm=1)
            if body.get('ok'):
                trail['last_sl'] = target
                self.ledger.mark(fid, 'filled', f'stop -> {target}')
        self.store.update(fid, trail=trail)

    # ------------------------------------------------------- crash recovery
    def _reconcile_unknown(self, positions, orders) -> None:
        """Resolve orders whose outcome was never learned: search the broker by tag."""
        now = _now()
        for row in self.ledger.listing(500):
            if row['state'] not in ('sending', 'unknown'):
                continue
            if now - row['updated_ms'] < 5_000:
                continue
            fid, tag = row['id'], row['tag']
            pos = self._find_position(positions, None, tag)
            pend = self._find_order(orders, None, tag)
            if pos or pend:
                ticket = (pend or {}).get('ticket') or (pos or {}).get('identifier')
                self.ledger.mark(fid, 'placed', 'found at the broker by its tag', ticket=ticket)
                self.store.move(fid, SENT, 'order found at the broker after an unknown send')
            elif now - row['updated_ms'] > UNKNOWN_GIVE_UP_MS:
                # Searched for a minute and it is not there: nothing was placed.
                self.ledger.mark(fid, 'refused', 'not found at the broker - nothing was placed')


__all__ = ['Executor']
