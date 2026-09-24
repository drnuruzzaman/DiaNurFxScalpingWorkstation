"""
server/engine/qualify.py - stage two: should this detected setup be traded?

Detection asks "is this a recognised situation". Qualification asks the
separate, harder question: given the cost of trading it, the state of the book,
the clock, and the money already at risk, is taking it the right decision.

Every gate returns a verdict of its own so the UI can show the full ledger.
A gate is one of:

    PASS    no objection
    WARN    tradeable but worse - contributes a confidence penalty
    BLOCK   hard veto, the signal cannot be qualified

The output status is one of:

    qualified   every gate passed, size calculated, ready to place
    watch       no blocks, but warnings or confidence below the floor
    rejected    at least one hard block

"watch" is the important middle state. A binary take/skip throws away the
setups that are one confirmation short, which on a scalping desk is most of
them. Watch entries stay on the board and get re-evaluated each bar.

COSTS ARE REAL. This is a Raw Spread Islamic account: no swap, but commission
per side and a live spread that widens exactly when signals fire. A 1R target
that does not clear the round trip is not a 1R target, and `min_rr` is measured
AFTER costs, not before.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import CONFIG, session_quality


def _gate(name: str, verdict: str, detail: str, penalty: int = 0) -> dict:
    return {'name': name, 'verdict': verdict, 'detail': detail,
            'penalty': int(penalty)}


# --------------------------------------------------------------------------- #
# position sizing                                                             #
# --------------------------------------------------------------------------- #
def size_position(entry: float, stop: float, spec: dict, risk: dict) -> dict:
    """
    Lots such that a stop-out costs exactly the configured risk budget.

    Gold is 100 oz per lot, so a $1 move is $100 per lot. Deriving the value
    from the contract spec rather than hard-coding it means the same function is
    correct if the symbol ever changes.
    """
    equity = float(risk.get('equity') or CONFIG.risk.equity)
    risk_pct = float(risk.get('risk_per_trade_pct') or CONFIG.risk.risk_per_trade_pct)
    budget = equity * risk_pct / 100.0

    distance = abs(entry - stop)
    if distance <= 0:
        return {'lots': 0.0, 'risk_cash': 0.0, 'error': 'zero stop distance'}

    point = float(spec.get('point') or 0.01)
    tick_size = float(spec.get('tick_size') or point)
    tick_value = float(spec.get('tick_value') or 1.0)

    # Value of the stop distance for one lot.
    ticks = distance / tick_size
    value_per_lot = ticks * tick_value
    if value_per_lot <= 0:
        return {'lots': 0.0, 'risk_cash': 0.0, 'error': 'cannot value the stop'}

    commission = float(spec.get('commission_per_lot_side')
                       or CONFIG.instrument.commission_per_lot_side)
    # Round trip: entry + exit.
    cost_per_lot = commission * 2.0

    raw_lots = budget / (value_per_lot + cost_per_lot)

    step = float(spec.get('volume_step') or 0.01)
    vmin = float(spec.get('volume_min') or 0.01)
    vmax = float(spec.get('volume_max') or 100.0)
    lots = max(vmin, min(vmax, int(raw_lots / step) * step))
    lots = round(lots, 2)

    actual_risk = lots * (value_per_lot + cost_per_lot)
    return {
        'lots': lots,
        'risk_cash': round(actual_risk, 2),
        'risk_pct': round(actual_risk / equity * 100.0, 3),
        'budget': round(budget, 2),
        'value_per_lot': round(value_per_lot, 2),
        'commission_round_trip': round(cost_per_lot * lots, 2),
        'stop_distance': round(distance, 3),
        'stop_points': round(distance / point, 1),
        'equity': equity,
        'oversized': bool(raw_lots < vmin),   # budget too small for min lot
    }


def _net_rr(entry: float, stop: float, target: float, spec: dict,
            lots: float, spread_price: float) -> float:
    """
    R:R after commission, spread and slippage.

    Gross RR flatters every scalp. On gold at 0.5R-1R targets the round trip can
    eat a fifth of the reward, which is the difference between an edge and a
    treadmill.
    """
    risk = abs(entry - stop)
    if risk <= 0 or lots <= 0:
        return 0.0
    tick_size = float(spec.get('tick_size') or 0.01)
    tick_value = float(spec.get('tick_value') or 1.0)
    per_price_unit = (1.0 / tick_size) * tick_value * lots

    commission = float(spec.get('commission_per_lot_side')
                       or CONFIG.instrument.commission_per_lot_side) * 2.0 * lots
    slip = float(CONFIG.instrument.default_slippage_points) * \
        float(spec.get('point') or 0.01)

    gross_reward = abs(target - entry) * per_price_unit
    gross_risk = risk * per_price_unit
    costs = commission + (spread_price + slip) * per_price_unit

    net_reward = gross_reward - costs
    net_risk = gross_risk + costs
    return round(net_reward / net_risk, 2) if net_risk > 0 else 0.0


# --------------------------------------------------------------------------- #
# the gates                                                                   #
# --------------------------------------------------------------------------- #
def qualify(sig, snap: dict, spec: dict, context: dict = None):
    """
    Run every gate against a detected signal and stamp the verdict onto it.

    `context` carries live state the snapshot cannot know: current spread, open
    positions, trades already taken today, upcoming news.
    """
    ctx = context or {}
    gates: list = []
    g = CONFIG.gates
    r = CONFIG.risk

    atr_v = float(sig.atr_at_signal or snap.get('atr') or 0.0)
    point = float(spec.get('point') or 0.01)

    # --- 1. spread ---------------------------------------------------------- #
    # Preference order: live context (a real quote) > the bar's own recorded
    # spread > the broker spec > a conservative default.
    spread_points = ctx.get('spread_points')
    if spread_points is None:
        spread_points = snap.get('spread_points')
    if spread_points is None:
        spread_points = spec.get('spread_points_now')
    spread_price = (float(spread_points) * point) if spread_points is not None else point * 20
    if spread_points is None:
        gates.append(_gate('spread', 'WARN',
                           'no live spread available - assuming 20 points', 4))
    elif float(spread_points) > g.max_spread_points:
        gates.append(_gate('spread', 'BLOCK',
                           f'spread {float(spread_points):.0f} pts exceeds the '
                           f'{g.max_spread_points:.0f} pt ceiling'))
    elif float(spread_points) > g.max_spread_points * 0.6:
        gates.append(_gate('spread', 'WARN',
                           f'spread {float(spread_points):.0f} pts is elevated', 6))
    else:
        gates.append(_gate('spread', 'PASS',
                           f'spread {float(spread_points):.0f} pts'))

    # --- 2. volatility band, measured RELATIVE not absolute ------------------ #
    # See GateSettings: a points-denominated band tracks the gold price rather
    # than the market condition, so the floor is economic and the ceiling is a
    # percentile of the instrument's own recent ATR.
    atr_points = atr_v / point if point else 0.0
    vol = (snap.get('regime') or {}).get('volatility') or {}
    atr_pct = float(vol.get('atr_percentile') or 50.0)

    commission = float(spec.get('commission_per_lot_side')
                       or CONFIG.instrument.commission_per_lot_side)
    # Commission expressed in price points: $/lot / (value of one point per lot)
    tick_size = float(spec.get('tick_size') or point)
    tick_value = float(spec.get('tick_value') or 1.0)
    point_value = (point / tick_size) * tick_value if tick_size else 1.0
    commission_points = (commission * 2.0 / point_value) if point_value > 0 else 0.0
    spread_pts = float(spread_points) if spread_points is not None else 20.0
    round_trip_points = spread_pts + commission_points
    floor_points = round_trip_points * g.min_atr_cost_multiple

    if atr_points < floor_points:
        gates.append(_gate('volatility', 'BLOCK',
                           f'ATR {atr_points:.0f} pts is under {floor_points:.0f} '
                           f'- less than {g.min_atr_cost_multiple:.0f}x the '
                           f'{round_trip_points:.0f} pt round trip'))
    elif atr_pct >= g.max_atr_percentile:
        gates.append(_gate('volatility', 'BLOCK',
                           f'ATR is in the {atr_pct:.0f}th percentile of its own '
                           f'range - news-spike conditions'))
    elif atr_pct >= 90:
        gates.append(_gate('volatility', 'WARN',
                           f'ATR in the {atr_pct:.0f}th percentile - expect '
                           f'wider excursions than the stop assumes', 8))
    else:
        gates.append(_gate('volatility', 'PASS',
                           f'ATR {atr_points:.0f} pts ({atr_pct:.0f}th pct), '
                           f'{atr_points / max(round_trip_points, 1):.1f}x the round trip'))

    # --- 3. session ---------------------------------------------------------- #
    sess = snap.get('session') or {}
    primary = sess.get('primary', 'unknown')
    quality = float(sess.get('quality') or 0.0)
    active = set(sess.get('active') or [])
    if not active & set(g.sessions_allowed):
        if g.session_blocks:
            gates.append(_gate('session', 'BLOCK',
                               f'{primary} - outside the allowed sessions '
                               f'{", ".join(g.sessions_allowed)}'))
        else:
            gates.append(_gate('session', 'WARN',
                               f'{primary} - outside the preferred sessions, '
                               f'thinner book than the stop assumes', 12))
    elif quality < 0.5:
        gates.append(_gate('session', 'WARN',
                           f'{primary} - thin liquidity (quality {quality:.2f})', 10))
    else:
        gates.append(_gate('session', 'PASS',
                           f'{primary} (quality {quality:.2f})'))

    # --- 4. multi-timeframe agreement ---------------------------------------- #
    mtf = snap.get('mtf') or {}
    mtf_score = int(mtf.get('score') or 0)
    want_up = sig.side == 'buy'
    aligned = (mtf_score >= g.min_mtf_score) if want_up else (mtf_score <= -g.min_mtf_score)
    opposed = (mtf_score <= -g.min_mtf_score) if want_up else (mtf_score >= g.min_mtf_score)
    if mtf.get('verdict') == 'not evaluated':
        gates.append(_gate('mtf', 'WARN', 'higher timeframes not evaluated', 8))
    elif aligned:
        gates.append(_gate('mtf', 'PASS',
                           f"{mtf.get('verdict')} ({mtf_score:+d})"))
    elif opposed:
        if g.require_mtf_agreement and sig.playbook in (
                'mtf_pullback', 'breakout_retest', 'flag_continuation'):
            # Continuation playbooks have no reason to exist against the HTF.
            gates.append(_gate('mtf', 'BLOCK',
                               f'continuation setup against higher timeframes '
                               f'({mtf_score:+d})'))
        else:
            gates.append(_gate('mtf', 'WARN',
                               f'counter-trend to higher timeframes '
                               f'({mtf_score:+d})', 14))
    else:
        gates.append(_gate('mtf', 'WARN',
                           f'higher timeframes conflicted ({mtf_score:+d})', 6))

    # --- 5. sizing ----------------------------------------------------------- #
    risk_cfg = {'equity': ctx.get('equity', r.equity),
                'risk_per_trade_pct': ctx.get('risk_per_trade_pct', r.risk_per_trade_pct)}
    sizing = size_position(sig.entry, sig.stop, spec, risk_cfg)
    if sizing.get('error'):
        gates.append(_gate('sizing', 'BLOCK', sizing['error']))
    elif sizing.get('oversized'):
        gates.append(_gate('sizing', 'BLOCK',
                           f"stop is too wide for the risk budget - minimum lot "
                           f"would risk {sizing['risk_pct']:.2f}% against a "
                           f"{risk_cfg['risk_per_trade_pct']:.2f}% limit"))
    else:
        gates.append(_gate('sizing', 'PASS',
                           f"{sizing['lots']} lots risks "
                           f"{sizing['risk_cash']:.2f} ({sizing['risk_pct']:.2f}%) "
                           f"over {sizing['stop_points']:.0f} pts"))

    # --- 6. net reward after costs ------------------------------------------- #
    lots = sizing.get('lots') or 0.0
    net_rr1 = _net_rr(sig.entry, sig.stop, sig.tp1, spec, lots, spread_price)
    net_rr2 = _net_rr(sig.entry, sig.stop, sig.tp2, spec, lots, spread_price)
    if net_rr2 < r.min_rr:
        gates.append(_gate('reward', 'BLOCK',
                           f'net R:R to TP2 is {net_rr2:.2f} after costs, below '
                           f'the {r.min_rr:.2f} floor'))
    elif net_rr1 < 0.6:
        gates.append(_gate('reward', 'WARN',
                           f'TP1 is only {net_rr1:.2f}R net - costs eat most of '
                           f'the first target', 8))
    else:
        gates.append(_gate('reward', 'PASS',
                           f'net {net_rr1:.2f}R to TP1, {net_rr2:.2f}R to TP2 '
                           f'after commission and spread'))

    # --- 7. is the path to target actually clear? ---------------------------- #
    from .levels import Level, room_to_target
    levels = [Level(**{k: v for k, v in lv.items() if k in Level.__annotations__})
              for lv in (snap.get('levels') or [])]
    room = room_to_target(levels, sig.entry, sig.tp1, sig.side)
    if not room['clear'] and room.get('obstacle'):
        obs = room['obstacle']
        frac = room['fraction']
        if frac < 0.55:
            gates.append(_gate('room', 'BLOCK',
                               f"{obs['kind']} at {obs['price']:.2f} (score "
                               f"{obs['score']}) sits only {frac * 100:.0f}% of "
                               f"the way to TP1"))
        else:
            gates.append(_gate('room', 'WARN',
                               f"{obs['kind']} at {obs['price']:.2f} sits at "
                               f"{frac * 100:.0f}% of the way to TP1", 8))
    else:
        gates.append(_gate('room', 'PASS', 'path to TP1 is clear of scored levels'))

    # --- 8. regime suitability ----------------------------------------------- #
    regime = snap.get('regime') or {}
    allowed = set(regime.get('allowed_playbooks') or [])
    if sig.playbook == 'pattern_break':
        gates.append(_gate('regime', 'PASS',
                           f"{regime.get('label')} - pattern breaks are read in "
                           f"any regime"))
    elif sig.playbook in allowed:
        gates.append(_gate('regime', 'PASS',
                           f"{regime.get('label')} (confidence "
                           f"{regime.get('confidence')}) suits this playbook"))
    else:
        gates.append(_gate('regime', 'WARN',
                           f"{regime.get('label')} is not this playbook's "
                           f"home regime", 12))

    # --- 9. exposure and daily limits ---------------------------------------- #
    open_positions = int(ctx.get('open_positions') or 0)
    if open_positions >= r.max_concurrent and g.exposure_blocks:
        gates.append(_gate('exposure', 'BLOCK',
                           f'{open_positions} positions already open, limit is '
                           f'{r.max_concurrent}'))
    elif open_positions >= r.max_concurrent:
        # Informational only, and no confidence penalty: a penalty could still
        # push the signal under min_confidence and stop the alert, which is
        # the thing this setting exists to prevent. The cap is enforced where
        # it belongs, at /api/order/send.
        gates.append(_gate('exposure', 'WARN',
                           f'{open_positions} positions open (limit '
                           f'{r.max_concurrent} applies to orders, not signals)', 0))
    else:
        gates.append(_gate('exposure', 'PASS',
                           f'{open_positions}/{r.max_concurrent} positions open'))

    trades_today = int(ctx.get('trades_today') or 0)
    daily_pnl_pct = float(ctx.get('daily_pnl_pct') or 0.0)
    if trades_today >= r.max_daily_trades:
        gates.append(_gate('daily', 'BLOCK',
                           f'{trades_today} trades today, limit is '
                           f'{r.max_daily_trades}'))
    elif daily_pnl_pct <= -r.max_daily_loss_pct:
        gates.append(_gate('daily', 'BLOCK',
                           f'down {abs(daily_pnl_pct):.2f}% today - daily loss '
                           f'limit reached'))
    else:
        gates.append(_gate('daily', 'PASS',
                           f'{trades_today} trades, {daily_pnl_pct:+.2f}% today'))

    # --- 10. news blackout ---------------------------------------------------- #
    minutes_to_news = ctx.get('minutes_to_high_impact')
    if minutes_to_news is not None and abs(float(minutes_to_news)) <= g.news_blackout_min:
        if g.news_blocks:
            gates.append(_gate('news', 'BLOCK',
                               f'high-impact release in {float(minutes_to_news):.0f} '
                               f'minutes - inside the {g.news_blackout_min} min blackout'))
        else:
            # Same fact, no veto - and a heavy penalty, because the risk being
            # described is a stop that gets jumped rather than filled.
            gates.append(_gate('news', 'WARN',
                               f'high-impact release in {float(minutes_to_news):.0f} '
                               f'minutes - inside the {g.news_blackout_min} min '
                               f'blackout, which is not set to block', 20))
    elif minutes_to_news is not None and abs(float(minutes_to_news)) <= g.news_blackout_min * 3:
        gates.append(_gate('news', 'WARN',
                           f'high-impact release in {float(minutes_to_news):.0f} '
                           f'minutes', 6))
    else:
        gates.append(_gate('news', 'PASS', 'no high-impact release nearby'))

    # --- 11. stop distance vs broker minimum ---------------------------------- #
    stops_level = float(spec.get('stops_level_points') or 0)
    stop_points = abs(sig.entry - sig.stop) / point
    if stops_level and stop_points < stops_level:
        gates.append(_gate('broker', 'BLOCK',
                           f'stop is {stop_points:.0f} pts but the broker '
                           f'requires at least {stops_level:.0f}'))
    else:
        gates.append(_gate('broker', 'PASS',
                           f'stop {stop_points:.0f} pts clears the broker minimum'))

    # --- verdict -------------------------------------------------------------- #
    blocks = [x for x in gates if x['verdict'] == 'BLOCK']
    warns = [x for x in gates if x['verdict'] == 'WARN']
    penalty = sum(x['penalty'] for x in warns)

    final_conf = max(0, min(100, sig.confidence - penalty))

    if blocks:
        status, qualified = 'rejected', False
    elif final_conf < g.min_confidence:
        status, qualified = 'watch', False
    elif warns:
        status, qualified = 'qualified', True
    else:
        status, qualified = 'qualified', True

    sig.gates = gates
    sig.status = status
    sig.qualified = qualified
    sig.confidence = final_conf
    sig.sizing = {
        **sizing,
        'net_rr1': net_rr1,
        'net_rr2': net_rr2,
        'spread_points': float(spread_points) if spread_points is not None else None,
    }
    sig.sizing['summary'] = (
        f"{sizing.get('lots', 0)} lots | risk "
        f"{sizing.get('risk_cash', 0):.2f} {CONFIG.risk.account_currency} | "
        f"net {net_rr2:.2f}R to TP2")

    # A one-line reason, because the panel has one line.
    if blocks:
        sig.reason = blocks[0]['detail']
    elif status == 'watch':
        sig.reason = (f'confidence {final_conf} below the {g.min_confidence} '
                      f'floor after {len(warns)} warning(s)')
    else:
        sig.reason = f'{len(warns)} warning(s), cleared' if warns else 'all gates clear'
    return sig


def qualify_all(signals: list, snap: dict, spec: dict, context: dict = None) -> list:
    out = []
    for sig in signals:
        if sig.status == 'conflicted':
            out.append(sig)          # already resolved; do not re-judge
            continue
        try:
            out.append(qualify(sig, snap, spec, context))
        except Exception as exc:                  # noqa: BLE001
            sig.status = 'rejected'
            sig.qualified = False
            sig.gates = [_gate('engine', 'BLOCK', f'qualification error: {exc}')]
            sig.reason = 'qualification error'
            out.append(sig)
    return out


__all__ = ['qualify', 'qualify_all', 'size_position']
