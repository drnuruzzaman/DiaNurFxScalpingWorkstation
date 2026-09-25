"""
server/lab/broker.py - the lab's broker: a simulation, and the only one here.

SimBroker answers the same calls the live Executor makes of the MT5 bridge -
positions(), orders(), deals(), trade('/order/send' | '/order/cancel' |
'/order/modify') - so the executor runs unmodified against it. Nothing in this
module, or anywhere in server/lab, can reach a real account.

How a minute is filled
----------------------
History is bid OHLC per minute. Within a minute the price is walked along the
usual path: open -> low -> high -> close for an up minute, open -> high -> low
-> close for a down one. Every pending order and every stop and target is a
threshold on that path; they trigger in the order the path reaches them, so a
stop hit before a target in the same minute is a stop, and an entry filled on
the way down can be stopped on the way back up within that minute.

  - The ask is the bid plus the minute's recorded spread. Buys fill and sell
    positions exit on the ask; sells fill and buy positions exit on the bid.
  - Limits and targets fill at their price (or better, when the minute OPENS
    beyond it). Stop entries and stop-losses fill at their price plus
    slippage, or at the open when price gaps through them.
  - Commission is charged per side, per lot, as the account pays it.
  - Pending orders expire at their expiration time, as MT5 expires them.

What it cannot know: the order of ticks inside a minute beyond that path, and
spread spikes shorter than a minute. Both are second-order for a strategy
whose stops are measured in ATR of 5m and above.
"""
from __future__ import annotations

REASON_CLIENT, REASON_EXPERT, REASON_SL, REASON_TP = 0, 3, 4, 5
MAGIC = 778899


class SimBroker:
    def __init__(self, symbol: str, spec: dict, balance: float, slippage_points: float = 3.0,
                 clock=None):
        self.symbol = symbol
        self.spec = dict(spec)
        self.point = float(spec.get('point') or 0.01)
        self.tick = float(spec.get('tick_size') or self.point)
        self.digits = int(spec.get('digits') or 2)
        # Account currency per 1.0 of price per lot: tick_value / tick_size.
        self.vpu = float(spec.get('tick_value') or 1.0) / self.tick
        self.comm_side = float(spec.get('commission_per_lot_side') or 0.0)
        self.slip = float(slippage_points or 0.0) * self.point
        self.balance = float(balance)
        self._pos: list = []
        self._ord: list = []
        self._deals: list = []
        self._ticket = 100_000
        self.bid = self.ask = 0.0
        self.spread_pts = 0.0
        self.now = clock or (lambda: 0)
        self.on_close = None            # (position, exit deal) -> None
        self.on_event = None            # (kind, text, data) -> None, manual trades only

    # ------------------------------------------------------------- helpers
    def _next(self) -> int:
        self._ticket += 1
        return self._ticket

    def _r(self, x: float) -> float:
        return round(float(x), self.digits)

    def _emit(self, kind: str, text: str, **data) -> None:
        if self.on_event:
            self.on_event(kind, text, data)

    @staticmethod
    def is_manual(comment: str) -> bool:
        return str(comment or '').startswith('LAB')

    def set_quote(self, bid: float, spread_pts: float) -> None:
        self.bid = float(bid)
        self.spread_pts = float(spread_pts or 0.0)
        self.ask = self.bid + self.spread_pts * self.point

    def _pl(self, p: dict, price: float) -> float:
        sign = 1.0 if p['side'] == 'buy' else -1.0
        return round((price - p['price_open']) * sign * self.vpu * p['volume'], 2)

    def floating(self) -> float:
        return round(sum(self._pl(p, self.bid if p['side'] == 'buy' else self.ask)
                         for p in self._pos), 2)

    def equity(self) -> float:
        return round(self.balance + self.floating(), 2)

    # ----------------------------------------------------- the bridge's API
    def positions(self, strict: bool = False) -> list:
        out = []
        for p in self._pos:
            cur = self.bid if p['side'] == 'buy' else self.ask
            q = dict(p)
            q['price_current'] = self._r(cur)
            q['profit'] = self._pl(p, cur)
            out.append(q)
        return out

    def orders(self, strict: bool = False) -> list:
        return [dict(o) for o in self._ord]

    def deals(self, days: int = 3) -> dict:
        return {'deals': [dict(d) for d in self._deals]}

    def trade(self, path: str, **p):
        if path == '/order/send':
            return self._send(p)
        if path == '/order/cancel':
            t = p.get('ticket')
            hit = next((o for o in self._ord if o['ticket'] == t), None)
            if hit is None:
                return 200, {'ok': False, 'error': 'no pending order with that ticket'}
            self._ord.remove(hit)
            if self.is_manual(hit['comment']):
                self._emit('cancel', f"pending {hit['side']} {hit['kind']} cancelled",
                           ticket=t)
            return 200, {'ok': True}
        if path == '/order/modify':
            t = p.get('ticket')
            pos = next((x for x in self._pos if x['ticket'] == t), None)
            if pos is None:
                return 200, {'ok': False, 'error': 'no such position'}
            sl = float(p.get('sl') or 0)
            tp = p.get('tp')
            buy = pos['side'] == 'buy'
            if sl and ((buy and sl >= self.bid) or (not buy and sl <= self.ask)):
                return 200, {'ok': False, 'error': 'invalid stops'}
            if sl:
                pos['sl'] = self._r(sl)
            if tp is not None:
                pos['tp'] = self._r(float(tp)) if float(tp) else 0.0
            if self.is_manual(pos['comment']):
                self._emit('stop', f"manual stop -> {pos['sl']}", ticket=t, sl=pos['sl'])
            return 200, {'ok': True}
        if path == '/position/close':
            t = p.get('ticket')
            pos = next((x for x in self._pos if x['ticket'] == t), None)
            if pos is None:
                return 200, {'ok': False, 'error': 'no such position'}
            px = self.bid if pos['side'] == 'buy' else self.ask
            self._close(pos, px, REASON_CLIENT)
            return 200, {'ok': True, 'price': self._r(px)}
        return 404, {'ok': False, 'error': 'no route'}

    def _send(self, p: dict):
        side = p.get('side')
        buy = side == 'buy'
        lots = round(float(p.get('lots') or 0), 2)
        kind = p.get('kind') or 'market'
        sl = float(p.get('sl') or 0)
        tp = float(p.get('tp') or 0)
        comment = str(p.get('comment') or '')
        if side not in ('buy', 'sell') or lots <= 0:
            return 200, {'ok': False, 'error': 'invalid request'}
        if not (self.bid and self.ask):
            return 200, {'ok': False, 'error': 'no price'}
        t = self._next()
        if kind == 'market':
            px = self.ask + self.slip if buy else self.bid - self.slip
            if sl and ((buy and sl >= self.bid) or (not buy and sl <= self.ask)):
                return 200, {'ok': False, 'error': 'invalid stops'}
            if tp and ((buy and tp <= self.ask) or (not buy and tp >= self.bid)):
                return 200, {'ok': False, 'error': 'invalid stops'}
            self._open(side, lots, px, sl, tp, comment, t)
            return 200, {'ok': True, 'ticket': t, 'price': self._r(px)}
        price = float(p.get('price') or 0)
        ok = (kind == 'limit' and ((buy and price < self.ask) or (not buy and price > self.bid))) \
            or (kind == 'stop' and ((buy and price > self.ask) or (not buy and price < self.bid)))
        if not ok:
            return 200, {'ok': False, 'error': 'invalid price'}
        self._ord.append({
            'ticket': t, 'symbol': self.symbol, 'side': side, 'kind': kind,
            'type': f'{side}_{kind}', 'volume': lots, 'price_open': self._r(price),
            'sl': self._r(sl) if sl else 0.0, 'tp': self._r(tp) if tp else 0.0,
            'comment': comment, 'magic': MAGIC,
            'expiration_ms': int(p.get('expiration_ms') or 0),
            'time_setup_ms': int(self.now()),
        })
        if self.is_manual(comment):
            self._emit('order', f'pending {side} {kind} {lots} at {self._r(price)}', ticket=t)
        return 200, {'ok': True, 'ticket': t, 'price': self._r(price)}

    # ------------------------------------------------------- fills & exits
    def _open(self, side, lots, price, sl, tp, comment, ticket) -> dict:
        comm = round(self.comm_side * lots, 2)
        self.balance -= comm
        now = int(self.now())
        pos = {
            'ticket': ticket, 'identifier': ticket, 'symbol': self.symbol, 'side': side,
            'type': 0 if side == 'buy' else 1, 'volume': lots,
            'price_open': self._r(price), 'sl': self._r(sl) if sl else 0.0,
            'tp': self._r(tp) if tp else 0.0, 'comment': comment, 'magic': MAGIC,
            'time_ms': now, 'swap': 0.0, 'initial_sl': self._r(sl) if sl else 0.0,
            'commission': -comm,
        }
        self._pos.append(pos)
        self._deals.append({
            'ticket': self._next(), 'order': ticket, 'position_id': ticket, 'entry': 0,
            'price': pos['price_open'], 'volume': lots, 'profit': 0.0, 'commission': -comm,
            'swap': 0.0, 'reason': REASON_EXPERT, 'time_ms': now, 'symbol': self.symbol,
            'side': side, 'comment': comment,
        })
        if self.is_manual(comment):
            self._emit('fill', f'{side} {lots} filled at {pos["price_open"]}', ticket=ticket)
        return pos

    def _close(self, pos: dict, price: float, reason: int) -> None:
        pl = self._pl(pos, price)
        comm = round(self.comm_side * pos['volume'], 2)
        self.balance += pl - comm
        deal = {
            'ticket': self._next(), 'order': 0, 'position_id': pos['ticket'], 'entry': 1,
            'price': self._r(price), 'volume': pos['volume'], 'profit': pl,
            'commission': -comm, 'swap': 0.0, 'reason': reason, 'time_ms': int(self.now()),
            'symbol': self.symbol, 'side': pos['side'], 'comment': pos['comment'],
        }
        self._deals.append(deal)
        self._pos.remove(pos)
        if self.on_close:
            self.on_close(pos, deal)

    def _triggers(self, sp: float) -> list:
        """
        Every live threshold, expressed on the BID path:
            (bid level, 'ge'|'le', kind, item, fill-at-level, fill-if-gapped(x))
        """
        out = []
        slip = self.slip
        for o in self._ord:
            P = o['price_open']
            if o['side'] == 'buy' and o['kind'] == 'limit':      # ask <= P
                out.append((P - sp, 'le', 'fill', o, P, lambda x, sp=sp: x + sp))
            elif o['side'] == 'buy' and o['kind'] == 'stop':     # ask >= P
                out.append((P - sp, 'ge', 'fill', o, P + slip,
                            lambda x, sp=sp: x + sp + slip))
            elif o['side'] == 'sell' and o['kind'] == 'limit':   # bid >= P
                out.append((P, 'ge', 'fill', o, P, lambda x: x))
            else:                                                 # sell stop: bid <= P
                out.append((P, 'le', 'fill', o, P - slip, lambda x: x - slip))
        for p in self._pos:
            if p['side'] == 'buy':
                if p['sl']:
                    out.append((p['sl'], 'le', 'sl', p, p['sl'] - slip, lambda x: x - slip))
                if p['tp']:
                    out.append((p['tp'], 'ge', 'tp', p, p['tp'], lambda x: x))
            else:
                if p['sl']:                                        # ask >= sl
                    out.append((p['sl'] - sp, 'ge', 'sl', p, p['sl'] + slip,
                                lambda x, sp=sp: x + sp + slip))
                if p['tp']:                                        # ask <= tp
                    out.append((p['tp'] - sp, 'le', 'tp', p, p['tp'],
                                lambda x, sp=sp: x + sp))
        return out

    def _fire(self, kind: str, item: dict, price: float) -> None:
        if kind == 'fill':
            if item in self._ord:
                self._ord.remove(item)
                self._open(item['side'], item['volume'], price, item['sl'], item['tp'],
                           item['comment'], item['ticket'])
        elif item in self._pos:
            self._close(item, price, REASON_SL if kind == 'sl' else REASON_TP)

    def run_minute(self, t0: int, o: float, h: float, l: float, c: float,
                   spread_pts: float) -> None:
        """Walk one M1 bar: expiries, then every fill and exit in path order."""
        sp = float(spread_pts or 0.0) * self.point
        for od in [x for x in self._ord if x['expiration_ms'] and x['expiration_ms'] <= t0]:
            self._ord.remove(od)
            if self.is_manual(od['comment']):
                self._emit('expire', f"pending {od['side']} {od['kind']} expired",
                           ticket=od['ticket'])

        # At the open: anything already beyond its level fills AT the open.
        for _ in range(8):
            hit = next(((k, it, gap) for (lvl, cond, k, it, _at, gap) in self._triggers(sp)
                        if (o <= lvl if cond == 'le' else o >= lvl)), None)
            if hit is None:
                break
            self._fire(hit[0], hit[1], hit[2](o))

        path = [o, l, h, c] if c >= o else [o, h, l, c]
        for a, b in zip(path, path[1:]):
            if a == b:
                continue
            up = b > a
            done = 0.0
            for _ in range(32):                     # bounded: each event removes an item
                best = None
                for (lvl, cond, k, it, at, _gap) in self._triggers(sp):
                    if up and cond == 'ge' and a < lvl <= b:
                        f = (lvl - a) / (b - a)
                    elif (not up) and cond == 'le' and b <= lvl < a:
                        f = (a - lvl) / (a - b)
                    else:
                        continue
                    # A position opened part-way along this leg only reacts to
                    # levels the path reaches AFTER its fill.
                    if f < done or (it.get('_born') is not None and f <= it['_born']):
                        continue
                    if best is None or f < best[0]:
                        best = (f, k, it, at)
                if best is None:
                    break
                done = best[0]
                self._fire(best[1], best[2], best[3])
                if best[1] == 'fill':
                    newest = self._pos[-1] if self._pos else None
                    if newest is not None:
                        newest['_born'] = done
            for p in self._pos:
                p.pop('_born', None)
        self.set_quote(c, spread_pts)


class SimFeed:
    """The executor's `feed`: quotes and specs from the simulation, bars from history."""

    def __init__(self, session):
        self.s = session

    def quote(self, symbol: str) -> dict:
        b = self.s.broker
        return {'bid': b.bid, 'ask': b.ask, 'point': b.point}

    def spec(self, symbol: str) -> dict:
        return dict(self.s.spec)

    def bars(self, symbol: str, tf: str, count: int = 300, live: bool = True):
        return self.s.bars_now(count)


__all__ = ['SimBroker', 'SimFeed']
