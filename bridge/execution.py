"""
bridge/execution.py - the order_send path, and every guard around it.

Kept in its own module on purpose. mt5_bridge.py's contract is "this process
cannot move money", and that claim is worth more if the code that CAN is a
separate file you have to deliberately enable, import and arm.

FIVE independent conditions must all hold before a single order reaches MT5:

  1. the bridge was started with --enable-trading            (process flag)
  2. ARMED is True, set only by that flag                    (module state)
  3. the request carries confirm=1                           (per order)
  4. the account passes the configured guard                 (demo / allowlist)
  5. the order passes sanity checks: known symbol, sane lot size, a stop that
     exists and sits on the correct side of entry

Any one missing and nothing is sent. There is no "force" parameter and no way
to disable the checks over HTTP - lifting a guard means editing this file.

Every attempt, accepted or refused, is written to the audit log with the reason.
A rejected order you cannot explain later is how people lose trust in a system
they should be able to audit.
"""

from __future__ import annotations

import threading
import time

# Set once, at startup, from the --enable-trading flag. Nothing over HTTP can
# change it.
ARMED = False
DEMO_ONLY = True          # refuse any account whose trade_mode is not DEMO
MAGIC = 778899            # stamps every order this system places
MAX_LOTS = 5.0            # absolute ceiling regardless of what was requested
ALLOWED_LOGINS: set = set()   # empty = any account that passes the demo guard

_SEND_LOCK = threading.Lock()
_LAST_SEND = {'ms': 0, 'signal_id': None}
MIN_GAP_MS = 2000         # throttle: no two orders inside two seconds


class Refused(Exception):
    """Raised with a human-readable reason. Never leaks MT5 internals."""


def arm(enabled: bool, demo_only: bool = True, allowed_logins=None,
        magic: int = MAGIC, max_lots: float = MAX_LOTS) -> None:
    global ARMED, DEMO_ONLY, MAGIC, MAX_LOTS, ALLOWED_LOGINS
    ARMED = bool(enabled)
    DEMO_ONLY = bool(demo_only)
    MAGIC = int(magic)
    MAX_LOTS = float(max_lots)
    ALLOWED_LOGINS = set(int(x) for x in (allowed_logins or []))


def status() -> dict:
    return {
        'trading_enabled': ARMED,
        'demo_only': DEMO_ONLY,
        'magic': MAGIC,
        'max_lots': MAX_LOTS,
        'allowed_logins': sorted(ALLOWED_LOGINS) or 'any (subject to demo guard)',
    }


def _check_account(mt5) -> dict:
    info = mt5.account_info()
    if info is None:
        raise Refused('no account is logged in')
    login = int(info.login)
    # MT5: ACCOUNT_TRADE_MODE_DEMO = 0, CONTEST = 1, REAL = 2
    is_demo = int(getattr(info, 'trade_mode', 2)) == 0
    if DEMO_ONLY and not is_demo:
        raise Refused(
            f'account {login} is a LIVE account and the demo guard is on. '
            f'Nothing was sent. Disable DEMO_ONLY in bridge/execution.py only '
            f'when you intend to trade real money.')
    if ALLOWED_LOGINS and login not in ALLOWED_LOGINS:
        raise Refused(f'account {login} is not in the allowlist')
    if not bool(getattr(info, 'trade_allowed', False)):
        raise Refused('the terminal has trading disabled for this account')
    return {'login': login, 'demo': is_demo, 'currency': info.currency,
            'balance': info.balance, 'equity': info.equity}


def _check_terminal(mt5) -> None:
    term = mt5.terminal_info()
    if term is None:
        raise Refused('terminal is unreachable')
    if not bool(getattr(term, 'trade_allowed', False)):
        raise Refused(
            'Algo Trading is switched OFF in the MetaTrader terminal. Turn it '
            'on there first - this is the terminal\'s own safety switch and '
            'the bridge will not override it.')


def _check_order(mt5, symbol: str, side: str, lots: float,
                 sl: float, tp: float, kind: str = 'market',
                 entry: float = 0.0) -> dict:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise Refused(f'unknown symbol {symbol!r}')
    if not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
    if int(getattr(info, 'trade_mode', 0)) == 0:
        raise Refused(f'{symbol} is disabled for trading by the broker')

    if side not in ('buy', 'sell'):
        raise Refused(f'side must be buy or sell, got {side!r}')

    lots = float(lots)
    if lots <= 0:
        raise Refused('lot size must be positive')
    if lots > MAX_LOTS:
        raise Refused(f'{lots} lots exceeds the {MAX_LOTS} lot ceiling')
    if lots < info.volume_min or lots > info.volume_max:
        raise Refused(f'{lots} lots is outside the broker range '
                      f'{info.volume_min}-{info.volume_max}')
    step = info.volume_step or 0.01
    if abs(round(lots / step) * step - lots) > 1e-9:
        raise Refused(f'{lots} lots is not a multiple of the {step} lot step')

    tick = mt5.symbol_info_tick(symbol)
    if tick is None or not (tick.bid and tick.ask):
        raise Refused(f'no live quote for {symbol}')
    market = tick.ask if side == 'buy' else tick.bid

    if kind not in ('market', 'limit', 'stop'):
        raise Refused(f'order kind must be market, limit or stop, got {kind!r}')
    if kind == 'market':
        price = market
    else:
        # A pending order's stop and target are judged against ITS price, and
        # the price must sit on the side of the market its type implies -
        # otherwise MT5 fills a "limit" instantly at a worse price, or rejects.
        price = float(entry or 0)
        if price <= 0:
            raise Refused('a pending order needs a price')
        ok_side = {('buy', 'limit'): price < market, ('buy', 'stop'): price > market,
                   ('sell', 'limit'): price > market, ('sell', 'stop'): price < market}
        if not ok_side[(side, kind)]:
            raise Refused(f'{side} {kind} at {price} is on the wrong side of the '
                          f'market ({market})')

    # A stop is mandatory. An unprotected position is not something this system
    # will open on your behalf, ever.
    if not sl:
        raise Refused('a stop loss is required')
    if side == 'buy' and sl >= price:
        raise Refused(f'stop {sl} is not below the buy price {price}')
    if side == 'sell' and sl <= price:
        raise Refused(f'stop {sl} is not above the sell price {price}')
    if tp:
        if side == 'buy' and tp <= price:
            raise Refused(f'target {tp} is not above the buy price {price}')
        if side == 'sell' and tp >= price:
            raise Refused(f'target {tp} is not below the sell price {price}')

    # Broker minimum stop distance.
    stops_level = int(getattr(info, 'trade_stops_level', 0) or 0)
    if stops_level:
        point = info.point or 0.01
        if abs(price - sl) / point < stops_level:
            raise Refused(f'stop is {abs(price - sl) / point:.0f} points away but '
                          f'the broker requires at least {stops_level}')
        if kind != 'market' and abs(price - market) / point < stops_level:
            raise Refused(f'pending price is {abs(price - market) / point:.0f} points '
                          f'from the market but the broker requires {stops_level}')
    return {'price': price, 'digits': info.digits, 'point': info.point}


def send_order(mt5, mt5_lock, symbol: str, side: str, lots: float,
               sl: float, tp: float = 0.0, comment: str = '',
               signal_id: str = '', deviation: int = 20,
               log=lambda kind, msg: None, kind: str = 'market',
               price: float = 0.0, expiration_broker_s: int = 0) -> dict:
    """
    Place an order with a stop attached. Returns the broker's answer.

    `kind` is market, limit or stop. A pending order takes `price` and, when
    given, an expiry in BROKER seconds; a broker that refuses specified
    expiries gets a GTC order instead, and the server cancels it at expiry.

    Raises Refused with an explanation for anything that does not pass. The
    caller turns that into an HTTP error; the trader sees the reason.
    """
    if not ARMED:
        raise Refused('execution is disabled. Start the bridge with '
                      '--enable-trading to arm it. Nothing was sent.')

    with _SEND_LOCK:
        now = int(time.time() * 1000)
        if now - _LAST_SEND['ms'] < MIN_GAP_MS:
            raise Refused('an order was sent moments ago - throttled. '
                          'Nothing was sent.')
        # Same signal twice is almost always a double-click or a retry.
        if signal_id and signal_id == _LAST_SEND['signal_id'] and \
                now - _LAST_SEND['ms'] < 60_000:
            raise Refused(f'signal {signal_id} was already sent in the last '
                          f'minute. Nothing was sent.')

        with mt5_lock:
            acct = _check_account(mt5)
            _check_terminal(mt5)
            checked = _check_order(mt5, symbol, side, lots, sl, tp, kind, price)

            if kind == 'market':
                otype = mt5.ORDER_TYPE_BUY if side == 'buy' else mt5.ORDER_TYPE_SELL
            else:
                otype = {('buy', 'limit'): mt5.ORDER_TYPE_BUY_LIMIT,
                         ('buy', 'stop'): mt5.ORDER_TYPE_BUY_STOP,
                         ('sell', 'limit'): mt5.ORDER_TYPE_SELL_LIMIT,
                         ('sell', 'stop'): mt5.ORDER_TYPE_SELL_STOP}[(side, kind)]
            request = {
                'action': (mt5.TRADE_ACTION_DEAL if kind == 'market'
                           else mt5.TRADE_ACTION_PENDING),
                'symbol': symbol,
                'volume': float(lots),
                'type': otype,
                'price': checked['price'],
                'sl': float(sl),
                'tp': float(tp) if tp else 0.0,
                'deviation': int(deviation),
                'magic': MAGIC,
                'comment': (comment or 'DiaNurFx')[:31],
                'type_time': mt5.ORDER_TIME_GTC,
                'type_filling': (mt5.ORDER_FILLING_IOC if kind == 'market'
                                 else mt5.ORDER_FILLING_RETURN),
            }
            if kind != 'market' and expiration_broker_s:
                request['type_time'] = mt5.ORDER_TIME_SPECIFIED
                request['expiration'] = int(expiration_broker_s)

            log('order', 'SEND %s %s %s %.2f lots @%s sl=%s tp=%s signal=%s account=%s'
                % (kind, side, symbol, lots, checked['price'], sl, tp, signal_id,
                   acct['login']))
            result = mt5.order_send(request)
            # Some brokers refuse a specified expiry. GTC is safe here because
            # the server cancels the order itself when the signal expires.
            if (result is not None and kind != 'market'
                    and request['type_time'] == mt5.ORDER_TIME_SPECIFIED
                    and int(result.retcode) == getattr(mt5, 'TRADE_RETCODE_INVALID_EXPIRATION', 10022)):
                request['type_time'] = mt5.ORDER_TIME_GTC
                request.pop('expiration', None)
                log('order', 'RETRY as GTC: broker refused the expiry')
                result = mt5.order_send(request)

            if result is None:
                err = mt5.last_error()
                log('order', 'FAILED send returned None: %s' % (err,))
                raise Refused(f'MT5 rejected the request: {err}')

            retcode = int(result.retcode)
            # PLACED is what a pending order returns; DONE is a filled deal.
            ok = retcode in (mt5.TRADE_RETCODE_DONE,
                             getattr(mt5, 'TRADE_RETCODE_PLACED', 10008))
            payload = {
                'ok': ok,
                'retcode': retcode,
                'comment': getattr(result, 'comment', ''),
                'ticket': int(getattr(result, 'order', 0) or 0),
                'deal': int(getattr(result, 'deal', 0) or 0),
                'volume': float(getattr(result, 'volume', 0) or 0),
                'price': float(getattr(result, 'price', 0) or 0),
                'account': acct['login'],
                'demo': acct['demo'],
                'magic': MAGIC,
                'signal_id': signal_id,
                'kind': kind,
            }
            log('order', 'RESULT ok=%s retcode=%s ticket=%s price=%s comment=%r'
                % (ok, retcode, payload['ticket'], payload['price'],
                   payload['comment']))
            if ok:
                _LAST_SEND['ms'] = now
                _LAST_SEND['signal_id'] = signal_id
            else:
                payload['error'] = (f'broker returned {retcode}: '
                                    f'{payload["comment"] or "no reason given"}')
            return payload


def modify_sl(mt5, mt5_lock, ticket: int, sl: float,
              log=lambda kind, msg: None) -> dict:
    """
    Move the stop of one of OUR positions. Tighten only.

    The trailing exit only ever moves a stop in the trade's favour, so this
    refuses anything else outright: a bug upstream that computed a looser stop
    must fail here rather than quietly widen the risk on a live position.
    """
    if not ARMED:
        raise Refused('execution is disabled. Nothing was changed.')
    with mt5_lock:
        _check_account(mt5)
        _check_terminal(mt5)
        rows = mt5.positions_get(ticket=int(ticket)) or []
        if not rows:
            raise Refused(f'no open position {ticket}')
        p = rows[0]
        if int(p.magic) != MAGIC:
            raise Refused(f'position {ticket} was not opened by this system '
                          f'(magic {p.magic}). Nothing was changed.')
        long = int(p.type) == 0
        sl = float(sl)
        if p.sl and ((long and sl <= p.sl) or (not long and sl >= p.sl)):
            raise Refused(f'new stop {sl} does not tighten the current {p.sl}. '
                          f'Stops only move in the trade\'s favour.')
        tick = mt5.symbol_info_tick(p.symbol)
        info = mt5.symbol_info(p.symbol)
        if tick is None or info is None:
            raise Refused(f'no quote for {p.symbol}')
        if (long and sl >= tick.bid) or (not long and sl <= tick.ask):
            raise Refused(f'stop {sl} is on the wrong side of the market')
        stops_level = int(getattr(info, 'trade_stops_level', 0) or 0)
        ref = tick.bid if long else tick.ask
        if stops_level and abs(ref - sl) / (info.point or 0.01) < stops_level:
            raise Refused(f'stop {sl} is inside the broker minimum distance')
        request = {'action': mt5.TRADE_ACTION_SLTP, 'position': int(ticket),
                   'symbol': p.symbol, 'sl': sl, 'tp': float(p.tp or 0.0),
                   'magic': MAGIC}
        log('order', 'MODIFY position=%s sl %s -> %s' % (ticket, p.sl, sl))
        result = mt5.order_send(request)
        if result is None:
            raise Refused(f'MT5 rejected the change: {mt5.last_error()}')
        ok = int(result.retcode) == mt5.TRADE_RETCODE_DONE
        log('order', 'MODIFY RESULT ok=%s retcode=%s' % (ok, result.retcode))
        out = {'ok': ok, 'retcode': int(result.retcode), 'ticket': int(ticket), 'sl': sl}
        if not ok:
            out['error'] = f'broker returned {result.retcode}: {getattr(result, "comment", "")}'
        return out


def cancel_order(mt5, mt5_lock, ticket: int, log=lambda kind, msg: None) -> dict:
    """Delete one of OUR pending orders. Anything else is refused."""
    if not ARMED:
        raise Refused('execution is disabled. Nothing was changed.')
    with mt5_lock:
        _check_account(mt5)
        rows = mt5.orders_get(ticket=int(ticket)) or []
        if not rows:
            raise Refused(f'no pending order {ticket}')
        if int(rows[0].magic) != MAGIC:
            raise Refused(f'order {ticket} was not placed by this system. '
                          f'Nothing was changed.')
        log('order', 'CANCEL order=%s' % ticket)
        result = mt5.order_send({'action': mt5.TRADE_ACTION_REMOVE, 'order': int(ticket)})
        if result is None:
            raise Refused(f'MT5 rejected the cancel: {mt5.last_error()}')
        ok = int(result.retcode) == mt5.TRADE_RETCODE_DONE
        log('order', 'CANCEL RESULT ok=%s retcode=%s' % (ok, result.retcode))
        out = {'ok': ok, 'retcode': int(result.retcode), 'ticket': int(ticket)}
        if not ok:
            out['error'] = f'broker returned {result.retcode}: {getattr(result, "comment", "")}'
        return out


__all__ = ['arm', 'status', 'send_order', 'modify_sl', 'cancel_order', 'Refused', 'ARMED']
