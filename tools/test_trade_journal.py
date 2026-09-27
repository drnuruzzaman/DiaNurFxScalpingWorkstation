#!/usr/bin/env python
"""
tools/test_trade_journal.py - every closed trade gets one true journal line.

    python tools/test_trade_journal.py

1. A backtest-lab replay in auto mode (the live executor, closed bars): one
   journal line per closed trade, with the trade's own id, R and outcome, the
   regime of the bar that made the signal, the confidence it was sent on, the
   evidence, and the deal's exit time.
2. server/trade_journal: record() from a bare store record, market_context()
   of nothing, and append() to a path that cannot be written - none raises.
3. tools/trade_journal_report.measure(): the excursions of a trade whose path
   is known.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

PASSED, FAILED = [], []


def check(name, ok, detail=''):
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def lab_run() -> None:
    from server.lab.session import ReplaySession
    s = ReplaySession({'symbol': 'XAUUSD.a', 'tf': '5m', 'start': '2026-07-06T00:00',
                       'end': '2026-07-09T00:00', 'mode': 'auto', 'name': 'test_trade_journal'})
    s.advance(900)
    tr = {t['id']: t for t in s.trades if not t['manual']}
    j = {x['signal_id']: x for x in s.journal}
    check('one journal line per closed trade', set(tr) == set(j) and len(j) > 0,
          f'{len(tr)} trades, {len(j)} lines')
    same = all(abs(tr[k]['r'] - j[k]['r_multiple']) < 1e-9 and tr[k]['outcome'] == j[k]['outcome']
               for k in tr if k in j)
    check('each line carries its trade\'s R and outcome', same)
    check('regime of the signal bar on every line', all(x['regime_at_entry'] for x in j.values()))
    check('confidence it was sent on on every line',
          all(x['confidence_at_send'] is not None for x in j.values()))
    check('evidence and the exit deal\'s time on every line',
          all(x['evidence_top3'] and x['exit_ms'] and x['fill_ms'] for x in j.values()))
    check('entry context is the bar that made the signal',
          all(x['entry_context']['bar_ms'] == x['final_bar_ms'] for x in j.values()))


def guards() -> None:
    from server import trade_journal as tj
    line = tj.record({'id': 'x', 'signal': {'evidence': 'junk'}}, None, source='live')
    check('record() of a bare store record', line['signal_id'] == 'x' and line['clock'] == 'utc')
    check('market_context() of nothing is None', tj.market_context(None) is None)
    bad = Path(tempfile.mkdtemp()) / 'is_a_dir'
    bad.mkdir()
    try:
        tj.append(bad, {'a': 1})
        check('append() to an unwritable path does not raise', True)
    except Exception as exc:                                   # noqa: BLE001
        check('append() to an unwritable path does not raise', False, repr(exc))


def excursions() -> None:
    import trade_journal_report as T

    class M1:
        t = np.arange(10, dtype=np.int64) * 60_000 + 600_000
        o = np.full(10, 100.0)
        h = np.array([100.5, 101.0, 100.2, 100.1, 100.0, 99.8, 100.0, 100.0, 102.1, 100.0])
        l = np.array([99.9, 99.8, 99.5, 99.0, 98.9, 99.0, 100.0, 100.0, 100.0, 100.0])

        def __len__(self):
            return self.t.size

    class P:
        def one(self, symbol):
            return M1()

    x = {'symbol': 'X', 'tf': '1m', 'side': 'buy', 'fill_price': 100.0, 'stop': 99.0,
         'fill_ms': 600_000, 'exit_ms': 600_000 + 4 * 60_000, 'outcome': 'stop', 'atr': 1.0,
         'clock': 'broker'}
    m = T.measure(x, P())
    check('MFE and MAE on a known path', abs(m['mfe'] - 1.0) < 1e-9 and abs(m['mae'] - 1.1) < 1e-9,
          f"mfe {m['mfe']:.2f} mae {m['mae']:.2f}")
    check('right then stopped, then TP1 after the stop',
          m['right_stopped'] and m['after_tp1'] and not m['never'])


def main() -> int:
    print('trade journal')
    guards()
    excursions()
    lab_run()
    print(f'\n  {len(PASSED)} passed, {len(FAILED)} failed')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
