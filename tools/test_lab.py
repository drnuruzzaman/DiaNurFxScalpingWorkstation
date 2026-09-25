#!/usr/bin/env python
"""
tools/test_lab.py - the backtest lab's guarantees, checked.

    1. the simulated broker fills on the minute path correctly
    2. NO LOOK-AHEAD: bars after a moment cannot change what was decided at it
    3. a saved session re-runs to the same trades
    4. the lab never loads the live API (separate process, separate state)

Run:  python tools/test_lab.py      (about a minute: it replays real history)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = '') -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'  [ok]   {name}' + (f' - {detail}' if detail else ''))
    else:
        FAIL += 1
        print(f'  [FAIL] {name}' + (f' - {detail}' if detail else ''))


def section(title: str) -> None:
    print(f'\n{title}\n' + '-' * len(title))


SPEC = {'point': 0.01, 'tick_size': 0.01, 'tick_value': 1.0, 'digits': 2,
        'commission_per_lot_side': 3.0}


def broker(**kw):
    from server.lab.broker import SimBroker
    b = SimBroker('XAUUSD.a', SPEC, 10_000.0, slippage_points=0.0, clock=lambda: 0)
    b.set_quote(kw.get('bid', 100.0), kw.get('spread', 0.0))
    return b


def main() -> int:
    from server.lab.broker import REASON_SL, REASON_TP

    # ------------------------------------------------------------------ 1 --- #
    section('1. the simulated broker')
    b = broker()
    b.trade('/order/send', side='buy', lots=1.0, kind='stop', price=101.0, sl=99.0, tp=0,
            comment='t', confirm=1)
    closed = []
    b.on_close = lambda p, d: closed.append(d)
    # Up minute: open 100 -> low 99.5 -> high 101.5 -> close 100.2. The stop
    # fills at 101 on the way up; its 99 stop is NOT touched afterwards (the
    # low came before the fill).
    b.run_minute(0, 100.0, 101.5, 99.5, 100.2, 0.0)
    pos = b.positions()
    check('a buy stop fills where the path crosses it',
          len(pos) == 1 and pos[0]['price_open'] == 101.0, str(pos[:1]))
    check('...and a low printed BEFORE the fill does not stop it out', not closed)

    b = broker()
    b.trade('/order/send', side='buy', lots=1.0, kind='stop', price=101.0, sl=100.5, tp=0,
            comment='t', confirm=1)
    closed = []
    b.on_close = lambda p, d: closed.append(d)
    # Down minute: open 100 -> high 101.5 -> low 99.5: filled at 101 on the
    # way up, then stopped at 100.5 on the way down, inside one minute.
    b.run_minute(0, 100.0, 101.5, 99.5, 99.8, 0.0)
    check('an entry and its stop in the same minute, in path order',
          len(closed) == 1 and closed[0]['reason'] == REASON_SL and closed[0]['price'] == 100.5,
          str(closed))

    b = broker()
    b.trade('/order/send', side='buy', lots=1.0, kind='market', sl=95.0, tp=104.0,
            comment='t', confirm=1)
    closed = []
    b.on_close = lambda p, d: closed.append(d)
    b.run_minute(0, 100.0, 104.5, 94.0, 101.0, 0.0)     # up minute: low (stop) first
    check('stop and target in one minute: the path decides (low first on an up bar)',
          len(closed) == 1 and closed[0]['reason'] == REASON_SL, str(closed))

    b = broker()
    b.trade('/order/send', side='sell', lots=1.0, kind='market', sl=103.0, tp=97.0,
            comment='t', confirm=1)
    closed = []
    b.on_close = lambda p, d: closed.append(d)
    b.run_minute(0, 96.0, 96.5, 95.5, 96.2, 0.0)          # gaps through the target
    check('a gap through the target fills at the open (better)',
          len(closed) == 1 and closed[0]['reason'] == REASON_TP and closed[0]['price'] == 96.0,
          str(closed))

    b = broker(spread=20.0)                                # 0.20 spread
    b.trade('/order/send', side='buy', lots=1.0, kind='limit', price=99.9, sl=98.0, tp=0,
            comment='t', confirm=1)
    b.run_minute(0, 100.0, 100.1, 99.75, 100.0, 20.0)     # bid low 99.75 -> ask 99.95
    check('a buy limit needs the ASK at its price, not the bid', not b.positions())
    b.run_minute(60_000, 100.0, 100.0, 99.6, 99.8, 20.0)  # ask 99.8 <= 99.9
    pos = b.positions()
    check('...and fills at the limit once the ask gets there',
          len(pos) == 1 and pos[0]['price_open'] == 99.9, str(pos[:1]))

    b = broker()
    b.trade('/order/send', side='sell', lots=1.0, kind='limit', price=105.0, sl=107.0, tp=0,
            comment='t', confirm=1, expiration_ms=60_000)
    b.run_minute(60_000, 100.0, 100.5, 99.5, 100.0, 0.0)
    check('a pending order expires at its expiration time', not b.orders())

    b = broker()
    bal0 = b.balance
    b.trade('/order/send', side='buy', lots=1.0, kind='market', sl=99.0, tp=101.0,
            comment='t', confirm=1)
    b.run_minute(0, 100.0, 101.2, 99.9, 101.1, 0.0)
    check('P&L and commission reach the balance',
          abs(b.balance - (bal0 + 100.0 - 6.0)) < 1e-6, f'{b.balance - bal0:+.2f}')

    # ------------------------------------------------------------------ 2 --- #
    section('2. no look-ahead (the property a replay rests on)')
    from server.lab.session import ReplaySession, delete_session
    short = ReplaySession({'symbol': 'XAUUSD.a', 'tf': '15m', 'start': '2026-06-01T00:00',
                           'end': '2026-06-04T00:00', 'record': False})
    long_ = ReplaySession({'symbol': 'XAUUSD.a', 'tf': '15m', 'start': '2026-06-01T00:00',
                           'end': '2026-06-30T00:00', 'record': False})
    n = short.last - short.i0
    short.advance(n)
    long_.advance(n)
    same_bars = short.series.t[short.k] == long_.series.t[long_.k]
    check('both sessions reached the same bar', bool(same_bars),
          f'{int(short.series.t[short.k])} vs {int(long_.series.t[long_.k])}')
    ks, kl = short.k, long_.k
    sa, sb = short.snapshot_at(ks), long_.snapshot_at(kl)
    keys = ('price', 'atr', 'bar_time_ms')
    check('the analysis at a bar ignores every bar after it',
          all(sa.get(x) == sb.get(x) for x in keys)
          and sa['trend']['state'] == sb['trend']['state']
          and len(sa['levels']) == len(sb['levels'])
          and [p['kind'] for p in sa['patterns']] == [p['kind'] for p in sb['patterns']],
          f"{sa['trend']['state']} / {len(sa['levels'])} levels")
    ta = [(t['id'], t['r']) for t in short.trades]
    tb = [(t['id'], t['r']) for t in long_.trades if t['exit_i'] <= kl]
    check('trades closed by that bar are identical', ta == tb, f'{len(ta)} trades')

    # ------------------------------------------------------------------ 3 --- #
    section('3. a saved session re-runs to the same result')
    s = ReplaySession({'symbol': 'XAUUSD.a', 'tf': '5m', 'start': '2026-07-06T00:00',
                       'end': '2026-07-10T00:00', 'mode': 'auto', 'name': 'test_lab'})
    s.step(120)                      # step moves the view with the frontier
    res = s.command({'op': 'order', 'side': 'buy', 'sl': round(s.broker.bid - 8, 2),
                     'tp': round(s.broker.bid + 12, 2)})
    check('a manual order at the latest bar is accepted', bool(res.get('ok')), str(res))
    s.set_view(s.i0)
    check('...and refused while looking at the past',
          not s.command({'op': 'order', 'side': 'sell'}).get('ok'))
    s.step(s.k - s.v + 80)
    s.save()
    r = ReplaySession.open(s.sid)
    target = r.resume_target()
    while r.k < target:
        r.advance(min(100, target - r.k))
    cmp = r.compare_saved()
    check('reopened, re-run to the saved bar: same trades',
          cmp['same'] and cmp['now_n'] == cmp['saved_n'],
          f"{cmp['now_n']} trades, {cmp['now_net']:+.2f}")
    check('...including the manual one', any(t['manual'] for t in r.trades)
          or any(p['comment'].startswith('LAB') for p in r.broker.positions()))
    delete_session(s.sid)

    # ------------------------------------------------------------------ 4 --- #
    section('4. separation from the live system')
    check('the lab never imported the live API module', 'server.main' not in sys.modules)
    import server.lab.app  # noqa: F401
    check('...not even its server', 'server.main' not in sys.modules)
    from server import clock
    check('the execution clock is the replay\'s while a session runs',
          clock.now_ms() == r.now_ms, f'{clock.now_ms()} == {r.now_ms}')

    print(f'\n{"=" * 52}\n  {PASS} passed, {FAIL} failed\n{"=" * 52}')
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
