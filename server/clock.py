"""
server/clock.py - the one clock the execution layer reads.

The executor, the signal store and the order ledger all ask "what time is it"
to expire orders, stamp their history and pace their retries. Live, that is
the wall clock and nothing here changes it.

The backtest lab (server/lab) runs those SAME three modules against a
simulated broker, so a replay trades exactly the way the live system does -
the same entry tolerance, the same pending-order re-judging, the same
trailing stop. That only works if they read the simulation's time rather
than today's, so the lab points this clock at its own. It runs in its own
process (server/lab/app.py), so doing that can never move the live clock.
"""
from __future__ import annotations

import time

_source = None


def now_ms() -> int:
    """Epoch milliseconds - simulated in the lab, the wall clock everywhere else."""
    return int(_source()) if _source is not None else int(time.time() * 1000)


def set_source(fn) -> None:
    """Replace the clock with `fn() -> epoch ms`; None restores the wall clock."""
    global _source
    _source = fn


__all__ = ['now_ms', 'set_source']
