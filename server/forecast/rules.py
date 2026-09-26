"""
server/forecast/rules.py - forecast filter rules, defined once.

The lab's filter test (server/lab/filters.py) vetoes sends with these; the
live shadow log (server/forecast/shadow.py) records what they WOULD say.
One definition, so the shadow log measures exactly the rule Phase D tested.
Pure functions of a regime distribution - no I/O, safe in the live process.
"""
from __future__ import annotations

UP, DOWN, TRANSITION = 0, 1, 3


def regime_agree(p, side: str) -> str | None:
    """Why regime_agree blocks this side, or None: never trade into the trend expected against you."""
    if side == 'buy' and p[DOWN] > p[UP]:
        return f'regime forecast favours a downtrend ({p[DOWN]:.0%} vs {p[UP]:.0%} up)'
    if side == 'sell' and p[UP] > p[DOWN]:
        return f'regime forecast favours an uptrend ({p[UP]:.0%} vs {p[DOWN]:.0%} down)'
    return None


__all__ = ['UP', 'DOWN', 'TRANSITION', 'regime_agree']
