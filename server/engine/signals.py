"""
server/engine/signals.py - opportunity detection: the playbooks.

This is stage ONE of two. A playbook's job is to notice that a recognised
situation exists and to express it as a fully-formed order - side, entry, stop,
two targets - with the evidence that produced it. It does NOT decide whether to
trade; qualify.py does that, and keeping the two apart is deliberate:

  * a detector that also judges tends to quietly lower its own bar
  * the UI can show "found but rejected, here is why", which is where the
    learning is
  * the backtester can measure detection quality and filter quality separately

Seven playbooks, each mapped to the regimes where it has an edge:

    mtf_pullback        trend      buy the dip with the higher timeframe
    sweep_reversal      range/transition  stops taken, no follow-through
    breakout_retest     trend/squeeze     the break held and came back
    false_break_fade    range/transition  the break failed, fade it
    pattern_break       any        a classical pattern reaching its trigger
    range_fade          range      sell the top, buy the bottom of a box
    flag_continuation   trend      the pause inside a move

EVERY stop is placed beyond a STRUCTURAL price - a swing, a level, a sweep
extreme - and then padded by an ATR fraction. Stops at round R multiples with
no structure behind them get hit by design; the market does not know or care
where your 1R is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

import numpy as np

from ..config import CONFIG


PLAYBOOK_LABELS = {
    'mtf_pullback': 'MTF PULLBACK',
    'sweep_reversal': 'SWEEP REVERSAL',
    'breakout_retest': 'BREAKOUT RETEST',
    'false_break_fade': 'FALSE BREAK FADE',
    'pattern_break': 'PATTERN BREAK',
    'range_fade': 'RANGE FADE',
    'flag_continuation': 'FLAG CONTINUATION',
}


@dataclass
class Signal:
    playbook: str
    label: str
    side: str                  # 'buy' | 'sell'
    symbol: str
    tf: str

    entry: float
    stop: float
    tp1: float
    tp2: float
    entry_type: str = 'market'      # market | limit | stop
    trigger: float = 0.0            # the price that arms a pending entry

    confidence: int = 0             # 0..100, detection confidence only
    rr1: float = 0.0
    rr2: float = 0.0
    risk_points: float = 0.0
    atr_at_signal: float = 0.0

    # How the trade is managed once filled - set from CONFIG.risk so every
    # consumer (narrator, Telegram, snapshot, an EA) describes the same plan.
    exit_plan: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)   # [{text, weight, kind}]
    against: list = field(default_factory=list)    # counter-evidence, honest
    invalidation: str = ''
    expiry_bars: int = 20
    created_ms: int = 0
    bar_time_ms: int = 0
    bar_idx: int = 0
    id: str = ''

    # filled by qualify.py
    qualified: bool = False
    status: str = 'detected'        # detected | qualified | watch | rejected
    # Lifecycle, set by the live signal store: FORMING, FINAL, SENT, FILLED,
    # CLOSED, EXPIRED, CANCELLED, REVERSED. Empty in backtests.
    stage: str = ''
    gates: list = field(default_factory=list)
    sizing: dict = field(default_factory=dict)
    reason: str = ''

    def to_dict(self) -> dict:
        return asdict(self)


def _mk_id(playbook: str, side: str, bar_ms: int) -> str:
    return f'{playbook}:{side}:{int(bar_ms)}'


def _widen_to_noise_floor(sig: Signal) -> Signal:
    """
    Push the stop out to at least `min_stop_atr` from entry.

    Structural stops are correct in principle and sometimes far too tight in
    practice: when price has already moved back toward a level, "just beyond
    the level" can be a fraction of an ATR away. That stop is not protecting
    the idea, it is sampling noise.

    Widening costs nothing in risk terms because size_position() derives lots
    from the stop distance - a wider stop simply means fewer lots for the same
    cash at risk. Targets are re-derived from the new distance so R stays
    meaningful.
    """
    atr_v = float(sig.atr_at_signal or 0.0)
    if atr_v <= 0:
        return sig
    floor = CONFIG.risk.min_stop_atr * atr_v
    risk = abs(sig.entry - sig.stop)
    if risk >= floor:
        return sig
    # Keep a STRUCTURAL TP2 across the widening.
    #
    # Targets are re-derived because R changed, but the old call passed no
    # structural objective - so every widened signal fell back to a flat 2R
    # TP2 and the prior swing it was aiming at was silently discarded. A TP2
    # that is not the fixed multiple of the old risk WAS structural; it is
    # offered back to _targets, which re-checks it against the new, wider R
    # (1.2R-6R) exactly as it would for any other signal.
    t_sign = 1.0 if sig.side == 'buy' else -1.0
    fixed_tp2 = sig.entry + t_sign * risk * CONFIG.risk.tp2_r
    structural = (None if abs(sig.tp2 - fixed_tp2) <= 1e-6 * max(1.0, abs(fixed_tp2))
                  else sig.tp2)
    sign = -1.0 if sig.side == 'buy' else 1.0
    sig.stop = sig.entry + sign * floor
    sig.tp1, sig.tp2 = _targets(sig.entry, sig.stop, sig.side, structural)
    sig.evidence.append(
        _ev(f'stop widened to the {CONFIG.risk.min_stop_atr:.1f} ATR noise floor '
            f'({risk / atr_v:.2f} ATR was inside normal fluctuation)',
            -4, 'risk'))
    return sig


def _finish(sig: Signal) -> Signal:
    """Compute derived risk numbers and a stable id."""
    sig = _widen_to_noise_floor(sig)
    risk = abs(sig.entry - sig.stop)
    sig.risk_points = round(risk, 4)
    sig.rr1 = round(abs(sig.tp1 - sig.entry) / risk, 2) if risk > 1e-9 else 0.0
    sig.rr2 = round(abs(sig.tp2 - sig.entry) / risk, 2) if risk > 1e-9 else 0.0
    sig.created_ms = int(time.time() * 1000)
    sig.id = _mk_id(sig.playbook, sig.side, sig.bar_time_ms)
    sig.label = PLAYBOOK_LABELS.get(sig.playbook, sig.playbook.upper())
    r = CONFIG.risk
    sig.exit_plan = ({'mode': 'trail', 'arm_r': r.tp1_r, 'lock_r': r.trail_lock_r,
                      'trail_atr': r.trail_atr}
                     if r.exit_mode == 'trail' else
                     {'mode': 'partial', 'partial_at': 'tp1', 'fraction': 0.5,
                      'then': 'breakeven', 'final': 'tp2'})
    for f in ('entry', 'stop', 'tp1', 'tp2', 'trigger'):
        setattr(sig, f, round(float(getattr(sig, f)), 3))
    return sig


def _ev(text: str, weight: int, kind: str = 'structure') -> dict:
    """One piece of evidence. `weight` is the confidence contribution."""
    return {'text': text, 'weight': int(weight), 'kind': kind}


def _score(evidence: list, base: int = 30) -> int:
    """
    Evidence weights -> a calibrated 0..100 confidence.

    The raw sum is not a confidence. Four or five agreeing pieces of evidence
    push it past 100, which clipped a year of scanning to a median of 88 and a
    p75 of 100 - at which point the number ranks nothing and means nothing.

    Above 55 the curve compresses, so stacking more confirmations keeps raising
    the score but with sharply diminishing returns. That is the honest shape:
    the sixth agreeing indicator on a gold M5 chart is not independent evidence,
    it is the same move measured again.
    """
    raw = base + sum(e['weight'] for e in evidence)
    if raw <= 55:
        calibrated = raw
    else:
        calibrated = 55 + (raw - 55) * 0.45
    return int(max(0, min(100, round(calibrated))))


def _targets(entry: float, stop: float, side: str,
             structural: float = None) -> tuple:
    """
    TP1 at a fixed R, TP2 at the structural objective when there is one.

    Using structure for TP2 rather than a second fixed multiple is what makes
    the difference between "2R because I said so" and "2.4R because that is
    where the prior swing high sits".
    """
    risk = abs(entry - stop)
    r = CONFIG.risk
    sign = 1.0 if side == 'buy' else -1.0
    tp1 = entry + sign * risk * r.tp1_r
    tp2 = entry + sign * risk * r.tp2_r
    if structural is not None:
        # Only accept the structural target if it is further than TP1 and not
        # absurdly far - a 9R "target" is a fantasy, not a plan.
        reach = (structural - entry) * sign
        if risk * 1.2 <= reach <= risk * 6.0:
            tp2 = structural
    return float(tp1), float(tp2)


# --------------------------------------------------------------------------- #
# playbook 1: multi-timeframe pullback                                        #
# --------------------------------------------------------------------------- #
def pb_mtf_pullback(snap: dict, series) -> list:
    mtf = snap.get('mtf') or {}
    trend = snap.get('trend') or {}
    regime = snap.get('regime') or {}
    atr_v = snap['atr']
    price = snap['price']

    direction = mtf.get('direction')
    if not direction or abs(mtf.get('score', 0)) < 30:
        return []
    if regime.get('state') not in ('trend', 'transition'):
        return []

    # Measure against the last IMPULSE in the higher-timeframe direction, not
    # the last leg - in an uptrend the last leg IS the pullback, so reading it
    # directly asks the wrong question and the playbook never fires.
    fib = (snap.get('pullback') or {}).get(direction) or {}
    if not fib.get('valid'):
        return []

    side = 'buy' if direction == 'up' else 'sell'
    if fib.get('zone') not in ('normal', 'pocket', 'deep'):
        return []

    ev_list = [
        _ev(f"higher timeframes {mtf.get('verdict')} ({mtf.get('score'):+d})", 22, 'mtf'),
        _ev(f"pullback into the {fib.get('zone')} "
            f"({fib.get('retracement', 0) * 100:.0f}% of the last leg)",
            18 if fib.get('in_pocket') else 10, 'fib'),
    ]
    against = []

    levels = snap.get('levels') or []
    pocket_lo = fib['levels'].get('0.618')
    pocket_hi = fib['levels'].get('0.786')
    zone_lo, zone_hi = sorted([pocket_lo, pocket_hi])
    confluent = [lv for lv in levels
                 if zone_lo - atr_v * 0.4 <= lv['price'] <= zone_hi + atr_v * 0.4
                 and lv['score'] >= 55]
    if confluent:
        best = max(confluent, key=lambda x: x['score'])
        ev_list.append(_ev(f"level confluence at {best['price']:.2f} "
                           f"({best['touches']} touches, score {best['score']})",
                           14, 'level'))

    # A reclaim signal on the entry timeframe is what turns a falling knife into
    # a pullback: rejection wick, or a CHoCH back in the trend direction.
    recent = [e for e in (snap.get('events') or []) if e['bars_since'] <= 5]
    want_bias = 'bullish' if side == 'buy' else 'bearish'
    trigger_ev = next((e for e in recent
                       if e['bias'] == want_bias and e['kind'] in ('rejection', 'sweep')), None)
    if trigger_ev:
        ev_list.append(_ev(f"{trigger_ev['kind']} at {trigger_ev['price']:.2f} "
                           f"({trigger_ev['strength']}/100)", 14, 'event'))
    else:
        against.append('no reclaim confirmation on this timeframe yet')

    mom = snap.get('momentum') or {}
    if (side == 'buy' and mom.get('total', 0) > 10) or \
       (side == 'sell' and mom.get('total', 0) < -10):
        ev_list.append(_ev(f"momentum agrees ({mom.get('total'):+d})", 8, 'momentum'))
    elif (side == 'buy' and mom.get('total', 0) < -35) or \
         (side == 'sell' and mom.get('total', 0) > 35):
        against.append(f"momentum still against the entry ({mom.get('total'):+d})")

    div = (mom.get('divergence') or {}).get('kind')
    if div and div.endswith('hidden') and (mom['divergence']['bias'] ==
                                           ('bullish' if side == 'buy' else 'bearish')):
        ev_list.append(_ev(f'hidden divergence supports continuation', 10, 'momentum'))

    # --- order ------------------------------------------------------------- #
    swing_ref = trend.get('swing_low') if side == 'buy' else trend.get('swing_high')
    leg_ref = fib.get('leg_low') if side == 'buy' else fib.get('leg_high')
    anchor = swing_ref if swing_ref is not None else leg_ref
    if anchor is None:
        return []
    pad = atr_v * 0.35
    stop = anchor - pad if side == 'buy' else anchor + pad
    if (side == 'buy' and stop >= price) or (side == 'sell' and stop <= price):
        return []

    structural = fib.get('leg_high') if side == 'buy' else fib.get('leg_low')
    tp1, tp2 = _targets(price, stop, side, structural)

    sig = Signal(
        playbook='mtf_pullback', label='', side=side,
        symbol=snap['symbol'], tf=snap['tf'],
        entry=price, stop=stop, tp1=tp1, tp2=tp2, entry_type='market',
        confidence=_score(ev_list, 34), evidence=ev_list, against=against,
        atr_at_signal=atr_v,
        invalidation=f"close beyond {anchor:.2f} ends the pullback read",
        expiry_bars=18, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    return [_finish(sig)]


# --------------------------------------------------------------------------- #
# playbook 2: liquidity sweep reversal                                        #
# --------------------------------------------------------------------------- #
def pb_sweep_reversal(snap: dict, series) -> list:
    atr_v = snap['atr']
    price = snap['price']
    reversal = snap.get('reversal') or {}
    events = snap.get('events') or []

    sweep = next((e for e in events
                  if e['kind'] == 'sweep' and e['confirmed'] and e['bars_since'] <= 6), None)
    if sweep is None:
        return []
    side = 'buy' if sweep['bias'] == 'bullish' else 'sell'

    ev_list = [
        _ev(f"confirmed sweep of {sweep['price']:.2f}, reclaimed and held",
            24, 'liquidity'),
        _ev(sweep['notes'][0], 8, 'liquidity'),
    ]
    against = []

    if reversal.get('has_choch'):
        ev_list.append(_ev('structure changed character after the sweep', 16, 'structure'))
    else:
        against.append('no CHoCH yet - the sweep has not been confirmed by structure')

    rej = next((e for e in events
                if e['kind'] == 'rejection' and e['bias'] == sweep['bias']
                and e['bars_since'] <= 4), None)
    if rej:
        ev_list.append(_ev(f"rejection wick at {rej['price']:.2f} "
                           f"({rej['strength']}/100)", 12, 'event'))

    # A sweep into a real liquidity pool is worth more than one into thin air.
    pools = snap.get('liquidity') or []
    want = 'sellside' if side == 'buy' else 'buyside'
    hit = next((p for p in pools
                if p['side'] == want and abs(p['price'] - sweep['price']) <= atr_v * 0.6), None)
    if hit:
        ev_list.append(_ev(f"took {hit['label']} - a genuine stop pocket", 12, 'liquidity'))

    regime = snap.get('regime') or {}
    if regime.get('state') == 'trend':
        trend_dir = regime.get('direction')
        if (side == 'buy' and trend_dir == 'down') or (side == 'sell' and trend_dir == 'up'):
            against.append(f"counter-trend: the {snap['tf']} regime is a "
                           f"{regime.get('label')}")
            ev_list.append(_ev('counter-trend reversal attempt', -14, 'regime'))

    mtf = snap.get('mtf') or {}
    if mtf.get('direction') == ('up' if side == 'buy' else 'down'):
        ev_list.append(_ev(f"higher timeframes agree ({mtf.get('score'):+d})", 12, 'mtf'))
    elif mtf.get('direction'):
        against.append(f"higher timeframes lean the other way ({mtf.get('score'):+d})")

    # --- order ------------------------------------------------------------- #
    extreme = sweep['extreme']
    pad = atr_v * 0.30
    stop = extreme - pad if side == 'buy' else extreme + pad
    if (side == 'buy' and stop >= price) or (side == 'sell' and stop <= price):
        return []

    # Target the opposite liquidity pool - that is where the move is headed.
    opp = 'buyside' if side == 'buy' else 'sellside'
    candidates = [p for p in pools if p['side'] == opp and
                  ((p['price'] > price) if side == 'buy' else (p['price'] < price))]
    structural = max(candidates, key=lambda p: p['pull'])['price'] if candidates else None
    tp1, tp2 = _targets(price, stop, side, structural)

    sig = Signal(
        playbook='sweep_reversal', label='', side=side,
        symbol=snap['symbol'], tf=snap['tf'],
        entry=price, stop=stop, tp1=tp1, tp2=tp2, entry_type='market',
        confidence=_score(ev_list, 30), evidence=ev_list, against=against,
        atr_at_signal=atr_v,
        invalidation=f"a close beyond {extreme:.2f} means the sweep failed",
        expiry_bars=12, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    return [_finish(sig)]


# --------------------------------------------------------------------------- #
# playbook 3: breakout + retest                                               #
# --------------------------------------------------------------------------- #
def pb_breakout_retest(snap: dict, series) -> list:
    atr_v = snap['atr']
    price = snap['price']
    events = snap.get('events') or []

    retest = next((e for e in events
                   if e['kind'] == 'retest' and e['confirmed'] and e['bars_since'] <= 5), None)
    if retest is None:
        return []
    brk = next((e for e in events
                if e['kind'] == 'breakout' and e['direction'] == retest['direction']
                and abs(e['price'] - retest['price']) < atr_v * 0.3), None)
    if brk is None:
        return []

    side = 'buy' if retest['bias'] == 'bullish' else 'sell'
    level = retest['price']

    ev_list = [
        _ev(f"broke {level:.2f} and retested it successfully", 24, 'structure'),
        _ev(brk['notes'][0], 10, 'event'),
        _ev(f"retest held ({retest['strength']}/100)", 14, 'event'),
    ]
    against = []
    if not brk['confirmed']:
        against.append('the break is younger than the false-break window')
        ev_list.append(_ev('break not yet past the false-break window', -10, 'event'))

    regime = snap.get('regime') or {}
    if regime.get('state') in ('trend', 'squeeze'):
        ev_list.append(_ev(f"regime is {regime.get('label')} - breaks tend to run",
                           12, 'regime'))
    elif regime.get('state') == 'range':
        against.append('range regime - breakouts fail more often than they run')
        ev_list.append(_ev('range regime penalises breakout continuation', -12, 'regime'))

    mtf = snap.get('mtf') or {}
    if mtf.get('direction') == ('up' if side == 'buy' else 'down'):
        ev_list.append(_ev(f"higher timeframes agree ({mtf.get('score'):+d})", 12, 'mtf'))

    vol = (regime.get('volatility') or {})
    if vol.get('expanding'):
        ev_list.append(_ev('volatility expanding into the break', 8, 'volatility'))

    # --- order ------------------------------------------------------------- #
    pad = atr_v * 0.35
    stop = level - pad if side == 'buy' else level + pad
    if (side == 'buy' and stop >= price) or (side == 'sell' and stop <= price):
        return []

    # Measured move: the range that preceded the break, projected from it.
    levels = snap.get('levels') or []
    beyond = [lv['price'] for lv in levels
              if (lv['price'] > price if side == 'buy' else lv['price'] < price)
              and lv['score'] >= 55]
    structural = (min(beyond) if side == 'buy' else max(beyond)) if beyond else None
    tp1, tp2 = _targets(price, stop, side, structural)

    sig = Signal(
        playbook='breakout_retest', label='', side=side,
        symbol=snap['symbol'], tf=snap['tf'],
        entry=price, stop=stop, tp1=tp1, tp2=tp2, entry_type='market',
        confidence=_score(ev_list, 32), evidence=ev_list, against=against,
        atr_at_signal=atr_v,
        invalidation=f"back inside {level:.2f} turns this into a false break",
        expiry_bars=15, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    return [_finish(sig)]


# --------------------------------------------------------------------------- #
# playbook 4: false break fade                                                #
# --------------------------------------------------------------------------- #
def pb_false_break_fade(snap: dict, series) -> list:
    atr_v = snap['atr']
    price = snap['price']
    events = snap.get('events') or []

    fb = next((e for e in events
               if e['kind'] == 'false_break' and e['bars_since'] <= 5), None)
    if fb is None:
        return []
    side = 'buy' if fb['bias'] == 'bullish' else 'sell'
    level = fb['price']

    ev_list = [
        _ev(f"false break {fb['direction']} through {level:.2f} - "
            f"price closed back inside", 24, 'event'),
        _ev(fb['notes'][1], 8, 'event'),
    ]
    against = []

    regime = snap.get('regime') or {}
    if regime.get('state') in ('range', 'transition'):
        ev_list.append(_ev(f"{regime.get('label')} regime - failed breaks are "
                           f"the dominant behaviour here", 14, 'regime'))
    else:
        against.append(f"{regime.get('label')} regime - a failed break can still "
                       f"resolve in the original direction")

    mom = snap.get('momentum') or {}
    if (side == 'buy' and mom.get('total', 0) > 0) or \
       (side == 'sell' and mom.get('total', 0) < 0):
        ev_list.append(_ev(f"momentum turned with the fade ({mom.get('total'):+d})",
                           10, 'momentum'))

    # --- order ------------------------------------------------------------- #
    pad = atr_v * 0.3
    stop = fb['extreme'] - pad if side == 'buy' else fb['extreme'] + pad
    if (side == 'buy' and stop >= price) or (side == 'sell' and stop <= price):
        return []

    # The natural target is the far side of the structure that was faked out of.
    regime_hi, regime_lo = regime.get('range_high'), regime.get('range_low')
    structural = regime_hi if side == 'buy' else regime_lo
    tp1, tp2 = _targets(price, stop, side, structural)

    sig = Signal(
        playbook='false_break_fade', label='', side=side,
        symbol=snap['symbol'], tf=snap['tf'],
        entry=price, stop=stop, tp1=tp1, tp2=tp2, entry_type='market',
        confidence=_score(ev_list, 30), evidence=ev_list, against=against,
        atr_at_signal=atr_v,
        invalidation=f"a second push through {fb['extreme']:.2f} means it was real",
        expiry_bars=12, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    return [_finish(sig)]


# --------------------------------------------------------------------------- #
# playbook 5: classical pattern reaching its trigger                          #
# --------------------------------------------------------------------------- #
def pb_pattern_break(snap: dict, series) -> list:
    atr_v = snap['atr']
    price = snap['price']
    out = []

    for p in (snap.get('patterns') or [])[:4]:
        if p['direction'] == 'neutral':
            continue
        if not p.get('actionable'):
            continue
        dist = abs(p['break_level'] - price) / atr_v
        if dist > 1.6:
            continue                      # trigger too far to be a live plan

        side = 'buy' if p['direction'] == 'bullish' else 'sell'
        # For a forming pattern this is a STOP order at the break level; for a
        # freshly confirmed one price is already through, so enter at market.
        if p['status'] == 'forming':
            entry, entry_type, trigger = p['break_level'], 'stop', p['break_level']
        else:
            entry, entry_type, trigger = price, 'market', 0.0

        stop = p['invalidation']
        pad = atr_v * 0.2
        stop = stop - pad if side == 'buy' else stop + pad
        if (side == 'buy' and stop >= entry) or (side == 'sell' and stop <= entry):
            continue

        ev_list = [
            _ev(f"{p['label']} ({p['status']}), quality {p['quality']}/100",
                20 if p['quality'] >= 65 else 12, 'pattern'),
        ]
        for note in p.get('notes', [])[:2]:
            ev_list.append(_ev(note, 5, 'pattern'))
        against = []

        mtf = snap.get('mtf') or {}
        if mtf.get('direction') == ('up' if side == 'buy' else 'down'):
            ev_list.append(_ev(f"higher timeframes agree ({mtf.get('score'):+d})",
                               12, 'mtf'))
        elif mtf.get('direction'):
            against.append(f"pattern points against the higher-timeframe lean "
                           f"({mtf.get('score'):+d})")
            ev_list.append(_ev('against higher-timeframe direction', -10, 'mtf'))

        if p['status'] == 'forming':
            against.append('the break level has not traded yet - this is a pending order')

        tp1, tp2 = _targets(entry, stop, side, p.get('target'))
        sig = Signal(
            playbook='pattern_break', label='', side=side,
            symbol=snap['symbol'], tf=snap['tf'],
            entry=entry, stop=stop, tp1=tp1, tp2=tp2,
            entry_type=entry_type, trigger=trigger,
            confidence=_score(ev_list, 30), evidence=ev_list, against=against,
            atr_at_signal=atr_v,
            invalidation=f"{p['label']} fails on a close beyond {p['invalidation']:.2f}",
            expiry_bars=20, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
        )
        out.append(_finish(sig))
    return out


# --------------------------------------------------------------------------- #
# playbook 6: range fade                                                      #
# --------------------------------------------------------------------------- #
def pb_range_fade(snap: dict, series) -> list:
    regime = snap.get('regime') or {}
    if regime.get('state') != 'range':
        return []
    atr_v = snap['atr']
    price = snap['price']
    hi, lo = regime.get('range_high'), regime.get('range_low')
    if hi is None or lo is None or hi - lo < atr_v * 2.5:
        return []

    span = hi - lo
    pos = (price - lo) / span
    if pos >= 0.82:
        side, edge = 'sell', hi
    elif pos <= 0.18:
        side, edge = 'buy', lo
    else:
        return []

    ev_list = [
        _ev(f"range regime, confidence {regime.get('confidence')}", 16, 'regime'),
        _ev(f"price at the {'top' if side == 'sell' else 'bottom'} of the "
            f"{lo:.2f}-{hi:.2f} box ({pos * 100:.0f}% of range)", 16, 'structure'),
    ]
    against = []

    events = snap.get('events') or []
    want = 'bearish' if side == 'sell' else 'bullish'
    rej = next((e for e in events if e['bias'] == want and
                e['kind'] in ('rejection', 'sweep') and e['bars_since'] <= 4), None)
    if rej:
        ev_list.append(_ev(f"{rej['kind']} at the edge ({rej['strength']}/100)",
                           14, 'event'))
    else:
        against.append('no rejection at the edge yet - price may simply break through')
        ev_list.append(_ev('no edge rejection confirmation', -8, 'event'))

    ch = (snap.get('channels') or [None])[0]
    if ch and ch['kind'] == 'horizontal':
        ev_list.append(_ev(f"horizontal channel, {ch['containment'] * 100:.0f}% "
                           f"containment", 10, 'structure'))

    pad = atr_v * 0.45
    stop = edge + pad if side == 'sell' else edge - pad
    structural = lo + span * 0.5 if side == 'sell' else hi - span * 0.5
    tp1, tp2 = _targets(price, stop, side, structural)

    sig = Signal(
        playbook='range_fade', label='', side=side,
        symbol=snap['symbol'], tf=snap['tf'],
        entry=price, stop=stop, tp1=tp1, tp2=tp2, entry_type='market',
        confidence=_score(ev_list, 28), evidence=ev_list, against=against,
        atr_at_signal=atr_v,
        invalidation=f"a close beyond {edge:.2f} breaks the range and voids the fade",
        expiry_bars=14, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    return [_finish(sig)]


# --------------------------------------------------------------------------- #
# playbook 7: flag continuation                                               #
# --------------------------------------------------------------------------- #
def pb_flag_continuation(snap: dict, series) -> list:
    atr_v = snap['atr']
    price = snap['price']
    regime = snap.get('regime') or {}
    flags = [p for p in (snap.get('patterns') or [])
             if p['kind'].endswith('flag') or p['kind'].endswith('pennant')]
    if not flags:
        return []
    p = flags[0]
    if not p.get('actionable'):
        return []
    side = 'buy' if p['direction'] == 'bullish' else 'sell'
    if abs(p['break_level'] - price) / atr_v > 1.5:
        return []

    ev_list = [
        _ev(f"{p['label']} with a {p['notes'][0]}", 20, 'pattern'),
        _ev(p['notes'][1], 10, 'pattern'),
    ]
    against = []
    if regime.get('state') == 'trend' and regime.get('direction') == ('up' if side == 'buy' else 'down'):
        ev_list.append(_ev(f"{regime.get('label')} - continuation is the base case",
                           14, 'regime'))
    else:
        against.append('no confirmed trend behind the flag')

    entry = p['break_level'] if p['status'] == 'forming' else price
    entry_type = 'stop' if p['status'] == 'forming' else 'market'
    stop = p['invalidation'] + (-atr_v * 0.2 if side == 'buy' else atr_v * 0.2)
    if (side == 'buy' and stop >= entry) or (side == 'sell' and stop <= entry):
        return []
    tp1, tp2 = _targets(entry, stop, side, p.get('target'))

    sig = Signal(
        playbook='flag_continuation', label='', side=side,
        symbol=snap['symbol'], tf=snap['tf'],
        entry=entry, stop=stop, tp1=tp1, tp2=tp2,
        entry_type=entry_type, trigger=entry if entry_type == 'stop' else 0.0,
        confidence=_score(ev_list, 30), evidence=ev_list, against=against,
        atr_at_signal=atr_v,
        invalidation=f"losing {p['invalidation']:.2f} breaks the flag",
        expiry_bars=16, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    return [_finish(sig)]


PLAYBOOKS = {
    'mtf_pullback': pb_mtf_pullback,
    'sweep_reversal': pb_sweep_reversal,
    'breakout_retest': pb_breakout_retest,
    'false_break_fade': pb_false_break_fade,
    'pattern_break': pb_pattern_break,
    'range_fade': pb_range_fade,
    'flag_continuation': pb_flag_continuation,
}


def generate(snap: dict, series, only: list = None,
             respect_regime: bool = True) -> list:
    """
    Run every playbook the current regime permits and return detected signals.

    `respect_regime` exists so the backtester can measure what the regime filter
    is actually worth by running with it off.
    """
    if not snap.get('ok'):
        return []
    allowed = set((snap.get('regime') or {}).get('allowed_playbooks') or PLAYBOOKS)
    # pattern_break is always allowed: a completing head and shoulders is
    # information in any regime.
    allowed.add('pattern_break')

    out = []
    disabled = set() if only else set(CONFIG.gates.disabled_playbooks)
    for name, fn in PLAYBOOKS.items():
        if only and name not in only:
            continue
        if name in disabled:
            continue
        if respect_regime and name not in allowed:
            continue
        try:
            out.extend(fn(snap, series) or [])
        except Exception:                       # noqa: BLE001
            # One broken playbook must not silence the other six.
            continue

    return resolve(out)


def resolve(out: list) -> list:
    """
    Turn raw playbook hits into the final signal list.

    Extracted from generate() so that experiments which vary the gating rule
    (see tools/exp_pattern_break.py) reuse the real resolution logic instead of
    a copy that can drift away from it.
    """
    # Two playbooks firing the same side is confluence, not duplication - merge
    # them so the UI shows one idea with the combined case.
    merged: dict = {}
    for sig in out:
        key = sig.side
        if key not in merged:
            merged[key] = sig
            continue
        other = merged[key]
        if sig.confidence > other.confidence:
            sig.evidence = sig.evidence + [
                _ev(f"{other.label} agrees on the same side", 8, 'confluence')]
            sig.confidence = min(100, sig.confidence + 8)
            merged[key] = _finish(sig)
        else:
            other.evidence.append(
                _ev(f"{sig.label} agrees on the same side", 8, 'confluence'))
            other.confidence = min(100, other.confidence + 8)

    result = sorted(merged.values(), key=lambda s: -s.confidence)

    # A buy AND a sell on the same bar is not two opportunities, it is one
    # unresolved market. Emitting both looked "thorough" and was actually
    # incoherent - whichever way price went, the system could claim it called
    # it. The stronger side survives as the idea; the weaker becomes explicit
    # counter-evidence against it and is returned flagged, never as a tradeable
    # signal.
    if len(result) == 2:
        primary, opposing = result[0], result[1]
        gap = primary.confidence - opposing.confidence
        primary.against.append(
            f'an opposing {opposing.label} {opposing.side} was also detected '
            f'(confidence {opposing.confidence} vs {primary.confidence})')
        # A near-tie means the market genuinely has not decided. Say so, and
        # cut the confidence rather than pretending the edge is clean.
        if gap <= 12:
            primary.confidence = max(0, primary.confidence - (14 - gap))
            primary.evidence.append(
                _ev('two-sided setup - conviction reduced', -(14 - gap), 'conflict'))
        opposing.status = 'conflicted'
        opposing.qualified = False
        result = [primary, opposing]

    return result


__all__ = ['Signal', 'generate', 'resolve', 'PLAYBOOKS', 'PLAYBOOK_LABELS']
