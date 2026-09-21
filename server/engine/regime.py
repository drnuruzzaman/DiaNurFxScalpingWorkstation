"""
server/engine/regime.py - what KIND of market is this, right now.

A scalping system that uses one playbook in all conditions gives back in chop
what it makes in trend. Regime is therefore not decoration; it decides which
playbooks are even allowed to fire.

Four states, and the one everybody omits is the important one:

    TREND       directional, worth holding for a measured move
    RANGE       bounded, fade the edges, targets are the opposite edge
    TRANSITION  the range is breaking or the trend is dying - the most
                dangerous state, and the one where most signals should be
                suppressed rather than taken
    SQUEEZE     volatility compressed, no direction yet, a break is coming

The classifier is a weighted vote across measures that fail in different ways:
ADX (lags), efficiency ratio (noisy but honest), structure agreement (leads),
channel slope, and range containment. No single one is trusted.
"""

from __future__ import annotations

import numpy as np

from .indicators import (adx, atr, bb_width, efficiency_ratio, last_valid,
                         linreg_slope, rolling_percentile)


def volatility_regime(h, l, c, atr_arr, lookback: int = 500) -> dict:
    """
    ATR in percentile terms plus the Bollinger squeeze read.

    Absolute ATR is meaningless across years - gold at 1200 and gold at 4600
    have very different point ranges. Percentile makes 2019 and 2026 comparable.
    """
    pct_arr = rolling_percentile(atr_arr, lookback, tail=3)
    pct = last_valid(pct_arr, 50.0)
    atr_now = last_valid(atr_arr, 0.0)
    bbw = bb_width(c, 20, 2.0)
    bbw_now = last_valid(bbw, 0.0)
    bbw_pct = last_valid(rolling_percentile(bbw, min(lookback, 200), tail=3), 50.0)

    if pct >= 85:
        state, label = 'extreme', 'EXTREME VOLATILITY'
    elif pct >= 65:
        state, label = 'high', 'HIGH VOLATILITY'
    elif pct >= 35:
        state, label = 'normal', 'NORMAL VOLATILITY'
    elif pct >= 15:
        state, label = 'low', 'LOW VOLATILITY'
    else:
        state, label = 'very_low', 'VERY LOW VOLATILITY'

    squeeze = bool(bbw_pct <= 20 and pct <= 35)
    expanding = False
    finite = atr_arr[~np.isnan(atr_arr)]
    if finite.size >= 10:
        expanding = bool(finite[-1] > finite[-6] * 1.15)

    return {
        'state': state,
        'label': label,
        'atr': round(float(atr_now), 4),
        'atr_points': round(float(atr_now) / 0.01, 1),
        'atr_percentile': round(float(pct), 1),
        'bb_width': round(float(bbw_now), 3),
        'bb_width_percentile': round(float(bbw_pct), 1),
        'squeeze': squeeze,
        'expanding': expanding,
        'contracting': bool(finite.size >= 10 and finite[-1] < finite[-6] * 0.85),
    }


def classify(h, l, c, t, swings, trend, channels, atr_arr,
             lookback: int = 120) -> dict:
    """
    The regime read. Returns state, confidence, and the evidence behind it,
    so the UI can show WHY rather than just a label.
    """
    n = c.size
    atr_now = last_valid(atr_arr, 0.0)
    if n < 30 or atr_now <= 0:
        return {'state': 'unknown', 'label': 'UNKNOWN', 'confidence': 0,
                'evidence': [], 'trend_score': 0, 'range_score': 0}

    lb = int(min(lookback, n))
    adx_arr, pdi, mdi = adx(h, l, c, 14)
    adx_now = last_valid(adx_arr, 0.0)
    er = last_valid(efficiency_ratio(c, 20), 0.0)
    slope = last_valid(linreg_slope(c, min(40, lb), tail=3), 0.0)
    slope_atr = slope / atr_now

    window = c[-lb:]
    hi, lo = float(h[-lb:].max()), float(l[-lb:].min())
    span = hi - lo
    net = abs(float(window[-1] - window[0]))
    directional_share = net / span if span > 0 else 0.0

    # How much of the recent action sat inside the middle of the range?
    mid_band = (lo + span * 0.25, lo + span * 0.75)
    inside_mid = float(((window >= mid_band[0]) & (window <= mid_band[1])).mean())

    # Structure agreement: the swing sequence is the leading vote.
    seq = trend.get('sequence') or []
    trending_labels = sum(1 for s in seq if s in ('HH', 'HL')) - \
        sum(1 for s in seq if s in ('LH', 'LL'))
    structure_agrees = abs(trending_labels) >= 3

    ch = channels[0] if channels else None
    ch_slope_atr = (ch.slope / atr_now) if ch else 0.0

    # --- weighted votes ---------------------------------------------------- #
    trend_score = (
        min(adx_now / 40.0, 1.0) * 26 +
        min(er / 0.5, 1.0) * 22 +
        min(abs(slope_atr) / 0.12, 1.0) * 20 +
        directional_share * 18 +
        (14 if structure_agrees else 0)
    )
    range_score = (
        max(0.0, 1.0 - adx_now / 28.0) * 24 +
        max(0.0, 1.0 - er / 0.38) * 22 +
        inside_mid * 24 +
        (1.0 - directional_share) * 16 +
        (14 if (ch and ch.kind == 'horizontal' and ch.containment >= 0.85) else 0)
    )

    # --- transition: the tell is disagreement ------------------------------ #
    # A market in transition looks trending on one measure and ranging on
    # another. High ADX with collapsing efficiency is a dying trend; a tight
    # range with a fresh CHoCH is a range about to break.
    conflict = 1.0 - abs(trend_score - range_score) / max(trend_score + range_score, 1e-9)
    last_break = trend.get('last_break') or {}
    fresh_choch = bool((trend.get('last_choch') or {}) and
                       (n - 1 - (trend.get('last_choch') or {}).get('idx', 0)) <= 15)
    adx_falling = False
    finite_adx = adx_arr[~np.isnan(adx_arr)]
    if finite_adx.size >= 8:
        adx_falling = bool(finite_adx[-1] < finite_adx[-6] * 0.88)

    transition_score = (
        conflict * 42 +
        (20 if fresh_choch else 0) +
        (18 if (adx_now > 22 and adx_falling) else 0) +
        (12 if (er < 0.28 and abs(slope_atr) > 0.06) else 0)
    )

    vol = volatility_regime(h, l, c, atr_arr)
    squeeze_score = 62 if vol['squeeze'] else 0

    scores = {
        'trend': trend_score,
        'range': range_score,
        'transition': transition_score,
        'squeeze': squeeze_score,
    }
    state = max(scores, key=scores.get)
    top = scores[state]
    runner = sorted(scores.values())[-2]
    confidence = int(max(0, min(100, round(
        top * 0.7 + (top - runner) * 0.6))))

    direction = 'up' if slope_atr > 0 else 'down'
    if state == 'trend':
        label = f'UPTREND' if slope_atr > 0 else 'DOWNTREND'
    elif state == 'range':
        label = 'RANGE'
    elif state == 'squeeze':
        label = 'SQUEEZE'
    else:
        label = 'TRANSITION'

    evidence = [
        f'ADX {adx_now:.0f} ({"+DI" if pdi[-1] > mdi[-1] else "-DI"} leads)',
        f'efficiency ratio {er:.2f}',
        f'slope {slope_atr:+.3f} ATR/bar',
        f'{directional_share * 100:.0f}% of the {lb}-bar range was directional',
        f'{inside_mid * 100:.0f}% of closes sat mid-range',
    ]
    if structure_agrees:
        evidence.append(f"structure agrees: {' '.join(seq[-4:])}")
    if fresh_choch:
        evidence.append('character change within the last 15 bars')
    if adx_now > 22 and adx_falling:
        evidence.append('ADX rolling over - trend losing force')
    if vol['squeeze']:
        evidence.append('Bollinger squeeze - expansion pending')

    # What the regime IMPLIES for playbook selection.
    if state == 'trend':
        playbooks = ['mtf_pullback', 'breakout_retest', 'flag_continuation']
        guidance = f'Trade with the {direction}trend. Fade nothing.'
    elif state == 'range':
        playbooks = ['range_fade', 'sweep_reversal', 'false_break_fade']
        guidance = 'Fade the edges. Targets are the opposite boundary, not a measured move.'
    elif state == 'squeeze':
        playbooks = ['breakout_retest']
        guidance = 'Wait for the expansion. Do not pre-position inside the coil.'
    else:
        playbooks = ['sweep_reversal', 'false_break_fade']
        guidance = 'Transition - size down or stand aside. This is where fixed-R scalps bleed.'

    return {
        'state': state,
        'label': label,
        'direction': direction,
        'confidence': confidence,
        'scores': {k: int(round(v)) for k, v in scores.items()},
        'evidence': evidence,
        'guidance': guidance,
        'allowed_playbooks': playbooks,
        'adx': round(float(adx_now), 1),
        'efficiency_ratio': round(float(er), 3),
        'slope_atr_per_bar': round(float(slope_atr), 4),
        'range_high': hi, 'range_low': lo,
        'directional_share': round(float(directional_share), 3),
        'volatility': vol,
    }


def mtf_alignment(reads: dict, base_tf: str) -> dict:
    """
    Do the timeframes agree?

    `reads` maps tf -> the trend dict from structure.trend_state. Higher
    timeframes carry more weight: a 1-minute uptrend inside a 1-hour downtrend
    is a pullback to sell, not a trend to buy, and the weighting is what encodes
    that.
    """
    if not reads:
        return {'score': 0, 'direction': None, 'agreement': 0.0, 'rows': []}

    weights = {'1m': 1.0, '3m': 1.2, '5m': 1.5, '15m': 2.0, '30m': 2.3,
               '1h': 2.8, '2h': 3.0, '4h': 3.4, '1d': 4.0, '1w': 4.5}
    total_w = 0.0
    signed = 0.0
    rows = []
    for tf, read in reads.items():
        if not read:
            continue
        w = weights.get(tf, 1.0)
        state = read.get('state', 'ranging')
        strength = read.get('strength', 'NORMAL')
        mult = {'HIGH': 1.0, 'NORMAL': 0.7, 'WEAK': 0.4}.get(strength, 0.7)
        vote = 0.0
        if 'up' in state:
            vote = mult
        elif 'down' in state:
            vote = -mult
        signed += vote * w
        total_w += w
        rows.append({
            'tf': tf, 'state': state, 'strength': strength,
            'vote': round(vote, 2), 'weight': w,
            'structure': read.get('structure', '-'),
            'invalidation': read.get('invalidation'),
        })

    if total_w <= 0:
        return {'score': 0, 'direction': None, 'agreement': 0.0, 'rows': rows}

    score = int(round(100 * signed / total_w))
    votes = [r['vote'] for r in rows if r['vote'] != 0]
    if votes:
        pos = sum(1 for v in votes if v > 0)
        agreement = max(pos, len(votes) - pos) / len(votes)
    else:
        agreement = 0.0

    direction = 'up' if score >= 20 else 'down' if score <= -20 else None
    if score >= 60:
        verdict = 'strongly aligned up'
    elif score >= 20:
        verdict = 'aligned up'
    elif score <= -60:
        verdict = 'strongly aligned down'
    elif score <= -20:
        verdict = 'aligned down'
    else:
        verdict = 'conflicted'

    return {
        'score': score,
        'direction': direction,
        'verdict': verdict,
        'agreement': round(agreement, 2),
        'rows': rows,
        'base_tf': base_tf,
    }


def fear_greed(trend, regime, momentum: dict, vol: dict,
               participation: float) -> dict:
    """
    The gauge on the left rail: a single 0-100 read of crowd positioning.

    Not a proprietary oracle - an honest composite of direction, volatility and
    participation, with the components exposed so it can be argued with.
    """
    direction = 0.0
    state = trend.get('state', 'ranging')
    strength_mult = {'HIGH': 1.0, 'NORMAL': 0.65, 'WEAK': 0.35}.get(
        trend.get('strength', 'NORMAL'), 0.65)
    if 'up' in state:
        direction = strength_mult
    elif 'down' in state:
        direction = -strength_mult

    rsi_now = momentum.get('rsi', 50.0)
    rsi_push = (rsi_now - 50.0) / 50.0            # -1..1

    # Extreme volatility reads as FEAR regardless of direction - that is how a
    # gold selloff actually feels and how the index should behave.
    vol_pct = vol.get('atr_percentile', 50.0)
    fear_from_vol = max(0.0, (vol_pct - 70.0) / 30.0)

    raw = 50.0 + direction * 28.0 + rsi_push * 18.0 - fear_from_vol * 22.0
    value = int(max(0, min(100, round(raw))))

    if value <= 20:
        label = 'EXTREME FEAR'
    elif value <= 40:
        label = 'FEAR'
    elif value < 60:
        label = 'NEUTRAL'
    elif value < 80:
        label = 'GREED'
    else:
        label = 'EXTREME GREED'

    return {
        'value': value,
        'label': label,
        'direction': int(round(direction * 100)),
        'volatility': int(round(vol_pct)),
        'participation': int(round(max(0.0, min(100.0, participation)))),
        'components': {
            'trend': round(direction, 2),
            'rsi_push': round(rsi_push, 2),
            'vol_fear': round(fear_from_vol, 2),
        },
    }


__all__ = ['classify', 'volatility_regime', 'mtf_alignment', 'fear_greed']
