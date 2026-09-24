#!/usr/bin/env python
"""
tools/exp_be_reversal.py - move the stop to break-even on an opposite signal?

Question: between the fill and TP1 the live exit does nothing but hold the
original stop. If the engine then finalizes a signal on the OPPOSITE side of
the same symbol and timeframe, should an open position's stop go to
break-even?

Nothing live is touched. This replays the captures tools/exp_exits.py already
made (2026 in-sample, 2025 out-of-sample, 2024 hold-out) through the exact
same entry logic, and changes only the exit.

Baseline is TODAY'S LIVE EXIT, not the one exp_exits.py called "today" when it
was written:
    whole position, at TP1 the stop locks +0.5R, then trails 1.0 ATR on
    closed bars, and MT5 holds TP2 as the take-profit.
and today's live entries: the disabled playbooks are removed, and the reward
gate reads the live min_rr.

Two definitions of "opposite signal", because they are different rules:

  ANY   any opposite detection finalized on a closed bar. This is exactly the
        event that already REVERSES pending orders in signal_store.finalize -
        the live system's own meaning of "opposite FINAL".
  QUAL  only an opposite signal that would itself have QUALIFIED (gates and
        confidence). Stricter: a watch-grade detection moves nothing.

The rule, applied literally:
  - only BEFORE TP1 (after TP1 the stop is already past break-even)
  - only when the position is IN PROFIT at the moment the signal is known.
    A long that is underwater cannot have a stop placed at its entry - that
    price is above the market and the broker would refuse it. "Move the stop
    to break-even" has no meaning there, so nothing happens.
  - timing: a signal detected on bar k-1's close is known at bar k's open.
    The stop is moved at that open, before bar k trades. No look-ahead.

Faithfulness check: with no opposite signals, simulate_be() must reproduce
exp_exits.simulate() trade for trade. It is asserted on every run.

    python tools/exp_be_reversal.py
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import exp_exits as X                                              # noqa: E402
from server.config import CONFIG, TF_SECONDS                       # noqa: E402

def apply_live_settings() -> list:
    """
    Load configs/settings.json onto CONFIG, the way the server does at start.

    A standalone script only sees the code defaults, and the live settings
    differ where it matters - the first run of this experiment measured a
    1.5 reward floor, a 5-bar cooldown and three playbooks switched off, none
    of which is how the account actually trades. Returns what was changed.
    """
    import json
    path = ROOT / 'configs' / 'settings.json'
    if not path.exists():
        return []
    saved = json.loads(path.read_text(encoding='utf-8'))
    changed = []
    for group in ('risk', 'gates'):
        target = getattr(CONFIG, group)
        for k, v in (saved.get(group) or {}).items():
            if not hasattr(target, k):
                continue
            if isinstance(getattr(target, k), tuple):
                v = tuple(v)
            if getattr(target, k) != v:
                changed.append(f'{group}.{k}: {getattr(target, k)!r} -> {v!r}')
                setattr(target, k, v)
    return changed


LABELS = [('is2026', '2026 in-sample'), ('oos2025', '2025 out-of-sample'),
          ('hold2024', '2024 hold-out')]

# Today's live exit, expressed in the harness's own terms.
LIVE = X.Exit('live: +0.5R lock, trail 1 ATR, TP2 at broker',
              partial=0.0, first_r=1.0, final='tp2',
              be_lock_r=float(CONFIG.risk.trail_lock_r),
              trail_atr=float(CONFIG.risk.trail_atr))


# =========================================================================== #
# the exit, with the one added rule                                           #
# =========================================================================== #
def simulate_be(c, ex, series, m1, spec, fill, start_idx, opp_bars):
    """
    exp_exits.simulate(), line for line, plus the break-even rule.

    `opp_bars` is the set of bar indices at whose OPEN an opposite signal
    becomes known. Everything outside the marked block is unchanged, and
    main() asserts that an empty set reproduces the original exactly.
    """
    n = len(series)
    long = c['side'] == 'buy'
    risk = abs(fill - c['stop'])
    if risk <= 0:
        return None
    sign = 1.0 if long else -1.0
    point = float(spec.get('point') or 0.01)
    slip = float(CONFIG.instrument.default_slippage_points) * point

    first_px = X._tp_at(c, ex.first_r) if ex.first_r != 1.0 else c['tp1']
    if ex.final == 'tp2':
        final_px = c['tp2']
    elif ex.final is None:
        final_px = None
    else:
        final_px = X._tp_at(c, float(ex.final))
    if ex.partial >= 1.0:
        final_px, first_px = first_px, None

    stop = c['stop']
    took = False
    open_frac = 1.0
    r = 0.0
    trail_ref = None
    mfe = 0.0
    outcome = 'expired'
    bars = 0
    last = min(n, start_idx + c['expiry'] + 1)
    be_moved = False          # added

    def hits(h, l, stop_now):
        if long:
            return (l <= stop_now,
                    first_px is not None and not took and h >= first_px,
                    final_px is not None and h >= final_px)
        return (h >= stop_now,
                first_px is not None and not took and l <= first_px,
                final_px is not None and l <= final_px)

    for i in range(start_idx, last):
        # ---- added: break-even on an opposite signal ------------------------
        if i in opp_bars and not took and not be_moved:
            o = float(series.o[i])
            in_profit = (o > fill) if long else (o < fill)
            if in_profit:
                stop = fill
                be_moved = True
        # ---------------------------------------------------------------------
        h, l = float(series.h[i]), float(series.l[i])
        mfe = max(mfe, ((h - fill) if long else (fill - l)) / risk)
        hs, h1, h2 = hits(h, l, stop)

        if hs and (h1 or h2):
            res = None
            if m1 is not None:
                t0 = int(series.t[i])
                lo_i = int(np.searchsorted(m1.t, t0, 'left'))
                hi_i = int(np.searchsorted(m1.t, t0 + TF_SECONDS[X.TF] * 1000, 'left'))
                for j in range(lo_i, hi_i):
                    a, b, cc = hits(float(m1.h[j]), float(m1.l[j]), stop)
                    if a:
                        res = (True, False, False)
                        break
                    if cc:
                        res = (False, False, True)
                        break
                    if b:
                        res = (False, True, False)
                        break
            hs, h1, h2 = res if res is not None else (True, False, False)

        if hs:
            px = stop - slip if long else stop + slip
            r += open_frac * sign * (px - fill) / risk
            outcome = ('trail' if trail_ref is not None and stop != c['stop']
                       and ex.trail_atr else ('first' if took else 'stop'))
            if be_moved and not took:
                outcome = 'breakeven'                  # added: label only
            bars = i - start_idx + 1
            open_frac = 0.0
            break
        if h2:
            r += open_frac * sign * (final_px - fill) / risk
            outcome = 'target'
            bars = i - start_idx + 1
            open_frac = 0.0
            break
        if h1:
            r += ex.partial * sign * (first_px - fill) / risk
            open_frac -= ex.partial
            took = True
            stop = fill + sign * ex.be_lock_r * risk
            if ex.trail_atr:
                trail_ref = h if long else l

        if trail_ref is not None and ex.trail_atr:
            trail_ref = max(trail_ref, h) if long else min(trail_ref, l)
            cand = trail_ref - sign * ex.trail_atr * c['atr']
            stop = max(stop, cand) if long else min(stop, cand)

    if open_frac > 0:
        i = min(n - 1, start_idx + c['expiry'])
        if i >= start_idx:
            px = float(series.c[i])
            r += open_frac * sign * (px - fill) / risk
            bars = i - start_idx + 1

    vpu = (1.0 / float(spec.get('tick_size') or 0.01)) * float(spec.get('tick_value') or 1.0)
    commission = float(spec.get('commission_per_lot_side')
                       or CONFIG.instrument.commission_per_lot_side) * 2.0
    comm_r = commission / (risk * vpu)
    spread_r = c['spread_price'] / risk
    return {'gross_r': r, 'bt_r': r - comm_r, 'net_r': r - comm_r - spread_r,
            'outcome': outcome, 'bars': bars, 'mfe': mfe, 'be_moved': be_moved}


# =========================================================================== #
# which bars carry an opposite signal                                         #
# =========================================================================== #
def qualifies(c, spec) -> bool:
    """The same test evaluate() applies before taking a trade."""
    skip = {'reward', 'daily', 'exposure'}
    if any(v == 'BLOCK' for (nm, v, _) in c['gates'] if nm not in skip):
        return False
    pen = sum(p for (nm, v, p) in c['gates'] if v == 'WARN' and nm not in skip)
    ok, rpen = X.gate_current(c, spec)
    if not ok:
        return False
    return max(0, min(100, c['raw_conf'] - pen - rpen)) >= CONFIG.gates.min_confidence


def signal_bars(cands, spec, qualified_only: bool) -> dict:
    """{side: set of bar indices at whose open a signal on that side is known}."""
    out = {'buy': set(), 'sell': set()}
    for c in cands:
        if qualified_only and not qualifies(c, spec):
            continue
        out[c['side']].add(int(c['i']))
    return out


# =========================================================================== #
# running it                                                                  #
# =========================================================================== #
def run_arm(cap, series, m1, opp):
    """evaluate() with simulate swapped for simulate_be and the given triggers."""
    original = X.simulate

    def patched(c, ex, series_, m1_, spec_, fill, start_idx):
        other = 'sell' if c['side'] == 'buy' else 'buy'
        bars = opp[other] if opp else set()
        return simulate_be(c, ex, series_, m1_, spec_, fill, start_idx, bars)

    X.simulate = patched
    try:
        return X.evaluate(cap, series, m1, X.gate_current, LIVE)
    finally:
        X.simulate = original


def paired(base, arm):
    """Per-trade difference on the trades both runs took."""
    key = lambda t: (t['entry_t'], t['side'], t['playbook'])
    b = {key(t): t['net_r'] for t in base}
    d = np.array([t['net_r'] - b[key(t)] for t in arm if key(t) in b])
    if len(d) < 2 or d.std(ddof=1) == 0:
        return len(d), 0.0, 0.0
    return len(d), d.mean(), d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))


def main() -> int:
    for line in apply_live_settings():
        print(f'live setting  : {line}')
    # The exit object was built at import, before the live values were loaded.
    LIVE.be_lock_r = float(CONFIG.risk.trail_lock_r)
    LIVE.trail_atr = float(CONFIG.risk.trail_atr)
    disabled = set(CONFIG.gates.disabled_playbooks)
    print(f'baseline exit : {LIVE.name}')
    print(f'playbooks off : {", ".join(sorted(disabled)) or "none"}')
    print(f'reward floor  : net TP2 >= {CONFIG.risk.min_rr}   min confidence {CONFIG.gates.min_confidence}')

    for label, title in LABELS:
        path = X.OUT / f'exp_exits_{label}.pkl'
        if not path.exists():
            print(f'\n{label}: no capture at {path}, skipped')
            continue
        cap = pickle.loads(path.read_bytes())
        cap = dict(cap, candidates=[c for c in cap['candidates']
                                    if c['playbook'] not in disabled])
        series, m1 = X.load_period(cap)
        spec = cap['spec']

        base = run_arm(cap, series, m1, None)
        # Faithfulness: the patched path with no triggers must equal the
        # untouched harness, trade for trade.
        ref = X.evaluate(cap, series, m1, X.gate_current, LIVE)
        assert len(ref) == len(base) and all(
            abs(a['net_r'] - b['net_r']) < 1e-12 for a, b in zip(ref, base)), \
            'simulate_be() does not reproduce exp_exits.simulate()'

        any_opp = signal_bars(cap['candidates'], spec, qualified_only=False)
        qual_opp = signal_bars(cap['candidates'], spec, qualified_only=True)
        arms = [
            ('BASE  live exit, no change', base),
            ('ANY   BE on any opposite FINAL', run_arm(cap, series, m1, any_opp)),
            ('QUAL  BE on a qualified opposite', run_arm(cap, series, m1, qual_opp)),
        ]

        print(f'\n{"=" * 112}\n  {title}   ({label})   '
              f'costs: commission + spread + slippage   risk 0.5%/trade\n{"=" * 112}')
        print(X.HEAD + f' {"moved":>6} {"dE[R]":>7} {"t":>6}')
        for name, trades in arms:
            s = X.stats(trades)
            moved = sum(1 for t in trades if t.get('be_moved'))
            n, dmean, tval = paired(base, trades)
            tail = '' if trades is base else f' {moved:>6} {dmean:>+7.3f} {tval:>+6.1f}'
            print(X._row(name, s) + tail)

        # What the moved trades actually did, against what they did untouched.
        for name, trades in arms[1:]:
            key = lambda t: (t['entry_t'], t['side'], t['playbook'])
            b = {key(t): t for t in base}
            moved = [t for t in trades if t.get('be_moved') and key(t) in b]
            if not moved:
                continue
            saved = sum(1 for t in moved if b[key(t)]['outcome'] == 'stop'
                        and t['outcome'] == 'breakeven')
            cut = sum(1 for t in moved if b[key(t)]['net_r'] > t['net_r'] + 1e-9)
            print(f'  {name[:5]} moved {len(moved)} trades: {saved} that would have been '
                  f'stopped out for a loss exited at break-even instead; '
                  f'{cut} that would have done better left alone')
    return 0


if __name__ == '__main__':
    sys.exit(main())
