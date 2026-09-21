"""
server/engine/backtest.py - event-driven replay of the live system.

The only honest way to backtest a discretionary-style engine is to run the
EXACT code the live path runs, on an expanding window, and never let it see a
bar that had not closed. That is what this does: `analyse()` -> `generate()` ->
`qualify_all()`, the same three calls the websocket loop makes.

Where backtests usually lie, and what is done about it here:

  LOOKAHEAD      the window handed to the engine ends at the bar being decided;
                 fills are resolved on the NEXT bar onward. Swing confirmation
                 lag is already modelled inside structure.py.
  FILL FANTASY   a stop and a target inside the same bar is ambiguous. This
                 resolves it PESSIMISTICALLY - the stop is assumed first - and
                 counts how often it happened so the number is visible rather
                 than hidden. Optionally it re-resolves such bars on M1 data,
                 which removes the ambiguity entirely.
  COST OMISSION  commission per side, the bar's own recorded spread, and
                 slippage are all charged. Islamic account: no swap.
  SURVIVORSHIP   every qualified signal is taken, in order, subject only to the
                 same exposure limits the live system enforces.

The output is a run: trades, an equity curve, and breakdowns by playbook,
session and hour, which the narrator's backtest mode quotes verbatim.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

import numpy as np

from ..config import CONFIG, TF_SECONDS, session_quality
from ..datafeed import Series, load_disk, resample
from .analysis import analyse, quick_trend
from .qualify import qualify_all
from .signals import generate


@dataclass
class Trade:
    signal_id: str
    playbook: str
    side: str
    entry_t: int
    entry_price: float
    exit_t: int = 0
    exit_price: float = 0.0
    stop: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    lots: float = 0.0
    outcome: str = 'open'       # tp2 | tp1 | stop | expired | eod
    r_multiple: float = 0.0
    pnl: float = 0.0
    costs: float = 0.0
    bars_held: int = 0
    mae_r: float = 0.0          # worst excursion against, in R
    mfe_r: float = 0.0          # best excursion for, in R
    confidence: int = 0
    session: str = ''
    hour: int = 0
    # The regime at entry. Recorded because "which market state was this taken
    # in" is the first question when a playbook underperforms, and it cannot be
    # reconstructed after the fact without re-running the whole analysis.
    regime: str = ''
    regime_confidence: int = 0
    ambiguous_bar: bool = False  # stop and target both inside one bar
    atr: float = 0.0             # ATR at signal time; the trail distance unit
    entry_reason: str = ''

    def to_dict(self) -> dict:
        return asdict(self)


def _value_per_price_unit(spec: dict, lots: float) -> float:
    tick_size = float(spec.get('tick_size') or 0.01)
    tick_value = float(spec.get('tick_value') or 1.0)
    return (1.0 / tick_size) * tick_value * lots


def _resolve_exit(series: Series, start_idx: int, trade: Trade, spec: dict,
                  expiry_bars: int, m1: Series = None) -> Trade:
    """
    Walk forward bar by bar until the trade closes.

    Partial at TP1 is modelled the way the signal actually specifies it: half
    off at TP1, stop to break-even, remainder to TP2. That changes the R
    distribution materially versus an all-or-nothing exit, and modelling it
    wrongly is the difference between a system that looks profitable and one
    that is.

    With CONFIG.risk.exit_mode == 'trail' the trailing plan is used instead.
    """
    if CONFIG.risk.exit_mode == 'trail':
        return _resolve_exit_trail(series, start_idx, trade, spec, expiry_bars, m1)
    n = len(series)
    long = trade.side == 'buy'
    risk = abs(trade.entry_price - trade.stop)
    if risk <= 0:
        trade.outcome = 'invalid'
        return trade

    stop = trade.stop
    took_tp1 = False
    realised_r = 0.0
    mae = mfe = 0.0
    point = float(spec.get('point') or 0.01)
    slip = float(CONFIG.instrument.default_slippage_points) * point

    for i in range(start_idx, min(n, start_idx + expiry_bars + 1)):
        hi, lo = float(series.h[i]), float(series.l[i])
        # excursions, in R
        if long:
            mfe = max(mfe, (hi - trade.entry_price) / risk)
            mae = min(mae, (lo - trade.entry_price) / risk)
            hit_stop = lo <= stop
            hit_tp1 = hi >= trade.tp1
            hit_tp2 = hi >= trade.tp2
        else:
            mfe = max(mfe, (trade.entry_price - lo) / risk)
            mae = min(mae, (trade.entry_price - hi) / risk)
            hit_stop = hi >= stop
            hit_tp1 = lo <= trade.tp1
            hit_tp2 = lo <= trade.tp2

        # --- ambiguity: both sides touched inside one bar ------------------ #
        if hit_stop and (hit_tp1 or hit_tp2):
            trade.ambiguous_bar = True
            resolved = None
            if m1 is not None:
                resolved = _resolve_on_m1(m1, series.t[i], TF_SECONDS.get(series.tf, 300),
                                          trade, stop, took_tp1, long)
            if resolved is not None:
                hit_stop, hit_tp1, hit_tp2 = resolved
            else:
                # No finer data: assume the stop went first. Pessimistic by
                # design - the alternative flatters every result.
                hit_tp1 = hit_tp2 = False

        if hit_stop:
            exit_px = stop - slip if long else stop + slip
            leg_r = ((exit_px - trade.entry_price) if long
                     else (trade.entry_price - exit_px)) / risk
            realised_r += leg_r * (0.5 if took_tp1 else 1.0)
            trade.exit_price = exit_px
            trade.exit_t = int(series.t[i])
            trade.outcome = 'tp1' if took_tp1 else 'stop'
            trade.bars_held = i - start_idx + 1
            break

        if hit_tp2:
            leg_r = abs(trade.tp2 - trade.entry_price) / risk
            realised_r += leg_r * (0.5 if took_tp1 else 1.0)
            trade.exit_price = trade.tp2
            trade.exit_t = int(series.t[i])
            trade.outcome = 'tp2'
            trade.bars_held = i - start_idx + 1
            break

        if hit_tp1 and not took_tp1:
            # Half off, stop to break-even - exactly what the plan says.
            realised_r += 0.5 * abs(trade.tp1 - trade.entry_price) / risk
            took_tp1 = True
            stop = trade.entry_price
    else:
        # Ran out of bars: close at the last available price.
        i = min(n - 1, start_idx + expiry_bars)
        if i >= start_idx:
            exit_px = float(series.c[i])
            leg_r = ((exit_px - trade.entry_price) if long
                     else (trade.entry_price - exit_px)) / risk
            realised_r += leg_r * (0.5 if took_tp1 else 1.0)
            trade.exit_price = exit_px
            trade.exit_t = int(series.t[i])
            trade.outcome = 'expired'
            trade.bars_held = i - start_idx + 1

    # --- money --------------------------------------------------------------- #
    vpu = _value_per_price_unit(spec, trade.lots)
    commission = float(spec.get('commission_per_lot_side')
                       or CONFIG.instrument.commission_per_lot_side) * 2.0 * trade.lots
    trade.costs = round(commission, 2)
    trade.r_multiple = round(realised_r, 3)
    trade.pnl = round(realised_r * risk * vpu - commission, 2)
    trade.mae_r = round(mae, 2)
    trade.mfe_r = round(mfe, 2)
    return trade


def _resolve_exit_trail(series: Series, start_idx: int, trade: Trade, spec: dict,
                        expiry_bars: int, m1: Series = None) -> Trade:
    """
    The trailing exit, whole position.

    Until TP1 prints, the original stop stands. The bar that prints TP1 moves
    the stop to entry + trail_lock_r x risk, and from that bar's close on the
    stop trails trail_atr x ATR behind the best price of COMPLETED bars. It is
    never set from a price the current bar has not printed, and it only ever
    moves in the trade's favour.

    The only ambiguous bar is one that touches both the original stop and TP1
    before the trail is armed; it is settled on M1, stop-first when M1 cannot
    say. Once armed there is no target, so there is nothing to be ambiguous
    about. Mirrors tools/exp_exits.py exit 'T4' exactly - verified trade by
    trade.
    """
    n = len(series)
    long = trade.side == 'buy'
    risk = abs(trade.entry_price - trade.stop)
    if risk <= 0:
        trade.outcome = 'invalid'
        return trade
    sign = 1.0 if long else -1.0
    point = float(spec.get('point') or 0.01)
    slip = float(CONFIG.instrument.default_slippage_points) * point
    lock = float(CONFIG.risk.trail_lock_r)
    k = float(CONFIG.risk.trail_atr)
    atr = float(trade.atr or 0.0)

    stop = trade.stop
    armed = False
    ref = None
    mae = mfe = 0.0
    realised_r = 0.0
    closed = False

    for i in range(start_idx, min(n, start_idx + expiry_bars + 1)):
        hi, lo = float(series.h[i]), float(series.l[i])
        mfe = max(mfe, ((hi - trade.entry_price) if long else (trade.entry_price - lo)) / risk)
        mae = min(mae, ((lo - trade.entry_price) if long else (trade.entry_price - hi)) / risk)
        hit_stop = lo <= stop if long else hi >= stop
        hit_tp1 = (not armed) and (hi >= trade.tp1 if long else lo <= trade.tp1)

        if hit_stop and hit_tp1:
            trade.ambiguous_bar = True
            first = None
            if m1 is not None and not m1.empty():
                t0 = int(series.t[i])
                a = int(np.searchsorted(m1.t, t0, 'left'))
                b = int(np.searchsorted(m1.t, t0 + TF_SECONDS.get(series.tf, 300) * 1000, 'left'))
                for j in range(a, b):
                    h, l = float(m1.h[j]), float(m1.l[j])
                    if (l <= stop) if long else (h >= stop):
                        first = 'stop'
                        break
                    if (h >= trade.tp1) if long else (l <= trade.tp1):
                        first = 'tp1'
                        break
            if first == 'tp1':
                hit_stop = False
            else:
                hit_tp1 = False

        if hit_stop:
            exit_px = stop - slip if long else stop + slip
            realised_r = sign * (exit_px - trade.entry_price) / risk
            trade.exit_price = exit_px
            trade.exit_t = int(series.t[i])
            trade.outcome = 'trail' if armed else 'stop'
            trade.bars_held = i - start_idx + 1
            closed = True
            break

        if hit_tp1:
            armed = True
            stop = trade.entry_price + sign * lock * risk
            ref = hi if long else lo

        # This bar has closed: it may move the stop for the NEXT bar.
        if armed and atr > 0:
            ref = max(ref, hi) if long else min(ref, lo)
            cand = ref - sign * k * atr
            stop = max(stop, cand) if long else min(stop, cand)

    if not closed:
        i = min(n - 1, start_idx + expiry_bars)
        if i >= start_idx:
            exit_px = float(series.c[i])
            realised_r = sign * (exit_px - trade.entry_price) / risk
            trade.exit_price = exit_px
            trade.exit_t = int(series.t[i])
            trade.outcome = 'expired'
            trade.bars_held = i - start_idx + 1

    vpu = _value_per_price_unit(spec, trade.lots)
    commission = float(spec.get('commission_per_lot_side')
                       or CONFIG.instrument.commission_per_lot_side) * 2.0 * trade.lots
    trade.costs = round(commission, 2)
    trade.r_multiple = round(realised_r, 3)
    trade.pnl = round(realised_r * risk * vpu - commission, 2)
    trade.mae_r = round(mae, 2)
    trade.mfe_r = round(mfe, 2)
    return trade


def _resolve_on_m1(m1: Series, bar_start_ms, tf_seconds: int, trade: Trade,
                   stop: float, took_tp1: bool, long: bool):
    """
    Zoom into M1 to settle which side of an ambiguous bar was reached first.

    Returns (hit_stop, hit_tp1, hit_tp2) or None when M1 does not cover it.
    """
    if m1 is None or m1.empty():
        return None
    end_ms = bar_start_ms + tf_seconds * 1000
    lo_i = int(np.searchsorted(m1.t, bar_start_ms, 'left'))
    hi_i = int(np.searchsorted(m1.t, end_ms, 'left'))
    if hi_i <= lo_i:
        return None
    for j in range(lo_i, hi_i):
        h, l = float(m1.h[j]), float(m1.l[j])
        if long:
            if l <= stop:
                return True, False, False
            if h >= trade.tp2:
                return False, False, True
            if h >= trade.tp1 and not took_tp1:
                return False, True, False
        else:
            if h >= stop:
                return True, False, False
            if l <= trade.tp2:
                return False, False, True
            if l <= trade.tp1 and not took_tp1:
                return False, True, False
    return None


def run(symbol: str, tf: str, start_ms: int = None, end_ms: int = None,
        equity: float = None, step: int = 1, window: int = 600,
        playbooks: list = None, respect_regime: bool = True,
        use_m1_resolution: bool = True, spec: dict = None,
        progress=None, max_bars: int = 0) -> dict:
    """
    Replay the system over a period.

    `step` evaluates every Nth bar; 1 is faithful, higher is a fast sweep.
    `use_m1_resolution` settles ambiguous bars against M1 data when available.
    """
    t0 = time.time()
    from ..datafeed import FEED

    spec = spec or FEED.spec(symbol)
    equity = float(equity or CONFIG.risk.equity)
    start_equity = equity

    series = load_disk(symbol, tf, None, start_ms, end_ms)
    if len(series) < window + 50:
        return {'ok': False, 'error': f'not enough bars: {len(series)}'}

    m1 = None
    if use_m1_resolution and tf != '1m':
        m1 = load_disk(symbol, '1m', None, start_ms, end_ms)
        if m1.empty():
            m1 = None

    # Higher-timeframe series, preloaded once and indexed by timestamp.
    from ..config import MTF_LADDER
    ladder = [x for x in MTF_LADDER.get(tf, [tf]) if x != tf]
    htf: dict = {}
    for name in dict.fromkeys(ladder):
        s = load_disk(symbol, name, None, start_ms, end_ms)
        if len(s) >= 60:
            htf[name] = s

    def mtf_at(ts: float) -> dict:
        out = {}
        for name, s in htf.items():
            i = int(np.searchsorted(s.t, ts, 'right'))
            w = s.slice(max(0, i - 300), i)
            if len(w) >= 60:
                out[name] = quick_trend(w)
        return out

    trades: list = []
    equity_curve: list = []
    open_trades: list = []
    daily_pnl = 0.0
    current_day = None
    trades_today = 0
    last_signal_bar: dict = {}
    detected = qualified_n = rejected_n = watch_n = 0

    total = len(series)
    stop_at = min(total, window + max_bars) if max_bars else total

    for i in range(window, stop_at, step):
        w = series.slice(i - window, i)
        bar_t = float(series.t[i - 1])

        day = int(bar_t // 86_400_000)
        if day != current_day:
            current_day, daily_pnl, trades_today = day, 0.0, 0

        # --- close any trades that resolved ------------------------------- #
        still_open = []
        for tr, entry_idx, expiry in open_trades:
            if tr.outcome == 'open':
                still_open.append((tr, entry_idx, expiry))
        open_trades = still_open

        snap = analyse(w, mtf_at(bar_t), spec)
        if not snap.get('ok'):
            continue

        ctx = {
            'equity': equity,
            'open_positions': len(open_trades),
            'trades_today': trades_today,
            'daily_pnl_pct': daily_pnl / max(equity, 1) * 100.0,
        }
        sigs = qualify_all(generate(snap, w, playbooks, respect_regime),
                           snap, spec, ctx)

        for sig in sigs:
            if sig.status == 'conflicted':
                continue
            detected += 1
            if sig.status == 'rejected':
                rejected_n += 1
                continue
            if sig.status == 'watch':
                watch_n += 1
                continue
            qualified_n += 1

            # cooldown: one idea per playbook per N bars
            key = (sig.playbook, sig.side)
            if i - last_signal_bar.get(key, -999) < CONFIG.gates.cooldown_bars:
                continue
            if len(open_trades) >= CONFIG.risk.max_concurrent:
                continue
            last_signal_bar[key] = i

            lots = float((sig.sizing or {}).get('lots') or 0.0)
            if lots <= 0:
                continue

            # FILL ON THE NEXT BAR. The decision was made on bar i-1's close;
            # the earliest it can be acted on is bar i's open.
            if i >= total:
                break
            point = float(spec.get('point') or 0.01)
            slip = float(CONFIG.instrument.default_slippage_points) * point
            fill = float(series.o[i]) + (slip if sig.side == 'buy' else -slip)

            if sig.entry_type == 'stop':
                # A pending order only fills if price actually reaches it.
                reached = (float(series.h[i]) >= sig.trigger if sig.side == 'buy'
                           else float(series.l[i]) <= sig.trigger)
                if not reached:
                    continue
                fill = sig.trigger + (slip if sig.side == 'buy' else -slip)

            hour = time.gmtime(bar_t / 1000).tm_hour
            primary, _ = session_quality(hour)
            tr = Trade(
                signal_id=sig.id, playbook=sig.playbook, side=sig.side,
                entry_t=int(series.t[i]), entry_price=round(fill, 3),
                stop=sig.stop, tp1=sig.tp1, tp2=sig.tp2, lots=lots,
                confidence=sig.confidence, session=primary, hour=hour,
                regime=(snap.get('regime') or {}).get('state', ''),
                regime_confidence=int((snap.get('regime') or {}).get('confidence', 0)),
                entry_reason=(sig.evidence[0]['text'] if sig.evidence else ''),
                atr=float(sig.atr_at_signal or snap.get('atr') or 0.0),
            )
            tr = _resolve_exit(series, i + 1, tr, spec, sig.expiry_bars, m1)
            if tr.outcome in ('invalid',):
                continue
            trades.append(tr)
            equity += tr.pnl
            daily_pnl += tr.pnl
            trades_today += 1
            equity_curve.append({'t': int(tr.exit_t or series.t[i]),
                                 'equity': round(equity, 2)})

        if progress and i % 500 == 0:
            progress(i - window, stop_at - window, len(trades))

    return _summarise(trades, equity_curve, start_equity, equity, symbol, tf,
                      series, time.time() - t0,
                      {'detected': detected, 'qualified': qualified_n,
                       'rejected': rejected_n, 'watch': watch_n},
                      respect_regime, step)


# --------------------------------------------------------------------------- #
# statistics                                                                  #
# --------------------------------------------------------------------------- #
def _stats_for(trades: list) -> dict:
    if not trades:
        return {'trades': 0, 'win_rate': 0.0, 'profit_factor': 0.0,
                'expectancy_r': 0.0, 'avg_win_r': 0.0, 'avg_loss_r': 0.0,
                'net_pnl': 0.0}
    rs = np.array([t.r_multiple for t in trades])
    pnl = np.array([t.pnl for t in trades])
    wins = rs[rs > 0]
    losses = rs[rs <= 0]
    gross_win = float(pnl[pnl > 0].sum())
    gross_loss = float(-pnl[pnl < 0].sum())
    # A bucket with no losing trades has an undefined profit factor, not an
    # infinite one. float('inf') is not JSON-encodable and took the whole
    # /backtest/result response down with a 500 the first time a playbook went
    # unbeaten over a small sample. None says "undefined" honestly and the UI
    # renders it as such.
    if gross_loss > 0:
        pf = round(gross_win / gross_loss, 2)
    elif gross_win > 0:
        pf = None
    else:
        pf = 0.0

    return {
        'trades': len(trades),
        'win_rate': round(float(len(wins) / len(rs) * 100), 1),
        'profit_factor': pf,
        'expectancy_r': round(float(rs.mean()), 3),
        'avg_win_r': round(float(wins.mean()), 2) if wins.size else 0.0,
        'avg_loss_r': round(float(losses.mean()), 2) if losses.size else 0.0,
        'net_pnl': round(float(pnl.sum()), 2),
        'best_r': round(float(rs.max()), 2),
        'worst_r': round(float(rs.min()), 2),
        'avg_bars': round(float(np.mean([t.bars_held for t in trades])), 1),
    }


def _summarise(trades, equity_curve, start_equity, end_equity, symbol, tf,
               series, elapsed, funnel, respect_regime, step) -> dict:
    summary = _stats_for(trades)

    # drawdown from the equity curve
    max_dd_pct = 0.0
    if equity_curve:
        eq = np.array([p['equity'] for p in equity_curve])
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / np.maximum(peak, 1e-9) * 100.0
        max_dd_pct = round(float(dd.max()), 2)
    summary['max_drawdown_pct'] = max_dd_pct
    summary['start_equity'] = round(start_equity, 2)
    summary['end_equity'] = round(end_equity, 2)
    summary['return_pct'] = round((end_equity / start_equity - 1) * 100, 2)

    def group(keyfn):
        buckets: dict = {}
        for t in trades:
            buckets.setdefault(keyfn(t), []).append(t)
        return {k: _stats_for(v) for k, v in sorted(buckets.items(), key=lambda kv: str(kv[0]))}

    ambiguous = sum(1 for t in trades if t.ambiguous_bar)

    return {
        'ok': True,
        'symbol': symbol, 'tf': tf,
        'from_t': int(series.t[0]), 'to_t': int(series.t[-1]),
        'bars': len(series),
        'elapsed_s': round(elapsed, 1),
        'step': step,
        'respect_regime': respect_regime,
        'summary': summary,
        'funnel': funnel,
        'by_playbook': group(lambda t: t.playbook),
        'by_regime': group(lambda t: t.regime or 'unknown'),
        # The cross-tab is what actually answers "where does this playbook
        # work" - a playbook can look flat overall while being strongly
        # positive in one regime and strongly negative in another.
        'by_playbook_regime': group(lambda t: f'{t.playbook}/{t.regime or "unknown"}'),
        'by_session': group(lambda t: t.session),
        'by_hour': group(lambda t: t.hour),
        'by_outcome': group(lambda t: t.outcome),
        'by_side': group(lambda t: t.side),
        'trades': [t.to_dict() for t in trades],
        'equity_curve': equity_curve,
        'integrity': {
            'ambiguous_bars': ambiguous,
            'ambiguous_pct': round(ambiguous / max(len(trades), 1) * 100, 1),
            'note': ('Bars where stop and target were both touched. Settled on '
                     'M1 where available, otherwise assumed stop-first.'),
        },
    }


__all__ = ['run', 'Trade']
