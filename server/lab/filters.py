"""
server/lab/filters.py - forecast filters for the Phase D filter test.

Candidate filters built ONLY from forecasts that passed their promotion gate
(Phase B range on 5m/15m, Phase C regime on every timeframe), with natural
thresholds fixed before any test was run - nothing here was tuned on the
years it is judged on:

    regime_agree   block a buy when the regime forecast says a DOWNtrend is
                   likelier than an uptrend at the horizon (and the reverse
                   for a sell): do not trade into the trend the forecast
                   expects against you
    range_active   block when the range forecast expects a quieter than usual
                   horizon (ratio < 1): a scalp needs the market to move
    both           regime_agree and range_active together
    no_transition  block when TRANSITION is the most likely regime at the
                   horizon - regime.py's own guidance says fixed-R scalps
                   bleed there

A filter only VETOES what the live gates already qualified; it never adds a
trade. It runs inside the lab's re-qualification at send time, exactly where
a gate would, so the executor, broker, sizing and exits are the live ones.
Lab-only: the live API never loads this module.
"""
from __future__ import annotations

from ..forecast import rules
from ..forecast import store as fstore

UP, DOWN, TRANSITION = rules.UP, rules.DOWN, rules.TRANSITION
FILTERS = ('regime_agree', 'range_active', 'both', 'no_transition')


def verdict(name: str, symbol: str, tf: str, bar_t: int, side: str) -> str | None:
    """Why this send is blocked by filter `name`, or None to let it through."""
    if not name:
        return None
    st = fstore.load(symbol, tf, allow_stale=True)
    if st is None:
        return None                       # no forecast here: the filter abstains
    i = st.row_at(int(bar_t))
    if i is None:
        return None
    reasons = []
    if name in ('regime_agree', 'both'):
        r = st.regime(i)
        why = rules.regime_agree(r['p'], side) if r is not None else None
        if why:
            reasons.append(why)
    if name in ('range_active', 'both'):
        c = st.cone(i)
        if c is not None and c['ratio'] < 1.0:
            reasons.append(f'range forecast below usual (x{c["ratio"]:.2f})')
    if name == 'no_transition':
        r = st.regime(i)
        if r is not None and r['top'] == TRANSITION:
            reasons.append(f"transition is the likeliest regime ahead ({r['p'][TRANSITION]:.0%})")
    return ('forecast filter: ' + '; '.join(reasons)) if reasons else None


__all__ = ['FILTERS', 'verdict']
