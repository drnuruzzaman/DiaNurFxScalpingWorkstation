"""
server/engine/precision.py - how many decimals the engine keeps for a price.

The engine was written for gold (2 digits) and rounded every price it derived
- the snapshot price, levels, pattern break/target/invalidation, legs, signal
entry/stop/targets - to 3 decimals, and ATR to 4. On a 5-digit pair that is a
10-pip grid: EURUSD's 5m ATR (~0.0004) came out 15-30% wrong and every stop
was snapped to a multiple of 0.001. Found 2026-10-20 when EURUSD went into
the lab.

The fix keeps max(3, the instrument's digits) decimals - so gold and USDJPY
round exactly as before, byte for byte - and ATR two more than that (still 4
for gold). The precision is a context variable, set for the duration of one
analyse() / generate() / qualify() call: the live board analyses several
symbols on parallel threads, so a module-wide value would race. Code that sets
nothing gets the old 3 / 4.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager

LEGACY_DP = 3
_DP = contextvars.ContextVar('engine_price_dp', default=LEGACY_DP)


def dp_for(digits) -> int:
    """Decimals to keep for an instrument quoted to `digits` (None: the legacy 3)."""
    try:
        return max(LEGACY_DP, int(digits))
    except (TypeError, ValueError):
        return LEGACY_DP


def price_dp() -> int:
    return _DP.get()


def atr_dp() -> int:
    dp = _DP.get()
    return 4 if dp <= LEGACY_DP else dp + 2


def rp(x) -> float:
    """A price (or a price distance), to the current precision."""
    return round(float(x), _DP.get())


def ra(x) -> float:
    """An ATR-sized quantity, to the current precision."""
    return round(float(x), atr_dp())


@contextmanager
def using(digits):
    tok = _DP.set(dp_for(digits))
    try:
        yield
    finally:
        _DP.reset(tok)


# Round numbers that attract resting orders, by how prices are quoted. Gold
# (2 digits): whole dollars to x00. Kept for 3-digit symbols too (USDJPY), as
# they always were. 4-5 digit FX: the big figure (0.0100), the half (0.0050),
# 10 pips and 5 pips, and the single pip.
_ROUND_STEPS = {
    'legacy': ((100.0, 1.0), (50.0, 0.85), (10.0, 0.6), (5.0, 0.4), (1.0, 0.2)),
    'fx': ((0.01, 1.0), (0.005, 0.85), (0.001, 0.6), (0.0005, 0.4), (0.0001, 0.2)),
}


def round_steps() -> tuple:
    return _ROUND_STEPS['fx' if _DP.get() >= 4 else 'legacy']


__all__ = ['dp_for', 'price_dp', 'atr_dp', 'rp', 'ra', 'using', 'round_steps', 'LEGACY_DP']
