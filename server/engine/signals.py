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

Eight playbooks, each mapped to the regimes where it has an edge:

    liquidity_sweep_reversal  range/transition  stops taken, no follow-through
    breakout_retest     trend/squeeze     the break held and came back
    false_break         any        a range edge / strong level broke on two
                                   closes and came back inside - fade it
    pattern_break       any        a classical pattern reaching its trigger
    range_edge_fade     any        fade the edge of the range price is in, by
                                   type: horizontal / ascending / descending /
                                   contracting / expanding
    trendline_bounce    trend      a rejection off the leg's trend line (2026-11-07)

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
from .precision import ra, rp, using
from .ranges import failed_break as rg_failed_break


PLAYBOOK_LABELS = {
    'liquidity_sweep_reversal': 'LIQUIDITY SWEEP REVERSAL',
    'breakout_retest': 'BREAKOUT RETEST',
    'false_break': 'FALSE BREAK',
    'pattern_break': 'PATTERN BREAK',
    'range_edge_fade': 'RANGE EDGE FADE',
    'trendline_bounce': 'TRENDLINE BOUNCE',
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


# The evidence line an order-block stop leaves (see _apply_stop_mode).
OB_STOP_TEXT = 'stop beyond the '


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
    # An order-block stop (risk.stop_mode) is already placed on structure;
    # resolve() re-finishing a merged signal must not widen it again.
    if any(e.get('text', '').startswith(OB_STOP_TEXT) for e in sig.evidence):
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
    sig.risk_points = ra(risk)
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
        setattr(sig, f, rp(getattr(sig, f)))       # max(3, digits): engine/precision.py
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
# playbook 2: liquidity sweep reversal                                        #
# --------------------------------------------------------------------------- #
# Rebuilt 2026-11-08 after two lab losses (gold 1h 13 Oct and 21 Oct, both
# -1R after reaching +0.7R): it bought 2.2 ATR above the swept level (late),
# into a bearish order block 0.6-0.7R away (TP1 sat INSIDE it), once with no
# structure confirmation and the higher timeframes at -91. Each is now a rule:
SW_FRESH_BARS = 3          # the sweep is at most this many bars old
SW_NEAR_ATR = 1.0          # entry within this of the swept level - no chasing
SW_HTF_BLOCK = 50          # higher timeframes this strongly against: no trade
SW_MIN_ROOM_R = 1.0        # the first opposing zone / level at least this far


def _first_obstacle(snap: dict, price: float, side: str) -> tuple:
    """The nearest thing in the way of the trade: an opposing order block, fair
    value gap or supply / demand zone (its near edge), or a strong level
    (score 55+). Returns (price, description) or (None, '')."""
    buy = side == 'buy'
    found = []
    for z in snap.get('order_blocks') or []:
        if z.get('side') == ('bearish' if buy else 'bullish'):
            found.append((z['low'] if buy else z['high'], f"{z['side']} order block {z['low']:.2f}-{z['high']:.2f}"))
    for z in snap.get('fvg') or []:
        if z.get('side') == ('bearish' if buy else 'bullish'):
            found.append((z['low'] if buy else z['high'], f"{z['side']} fair value gap {z['low']:.2f}-{z['high']:.2f}"))
    for z in snap.get('zones') or []:
        if z.get('side') == ('supply' if buy else 'demand'):
            found.append((z['low'] if buy else z['high'], f"{z['side']} zone {z['low']:.2f}-{z['high']:.2f}"))
    for lv in snap.get('levels') or []:
        if lv.get('score', 0) >= 55:
            found.append((lv['price'], f"{lv.get('kind', 'level')} {lv['price']:.2f} (score {lv['score']})"))
    ahead = [(p_, d) for p_, d in found if (p_ > price if buy else p_ < price)]
    if not ahead:
        return None, ''
    return min(ahead, key=lambda x: abs(x[0] - price))


def pb_liquidity_sweep_reversal(snap: dict, series) -> list:
    atr_v = snap['atr']
    price = snap['price']
    reversal = snap.get('reversal') or {}
    events = snap.get('events') or []

    sweep = next((e for e in events
                  if e['kind'] == 'sweep' and e['confirmed'] and e['bars_since'] <= SW_FRESH_BARS), None)
    if sweep is None:
        return []
    side = 'buy' if sweep['bias'] == 'bullish' else 'sell'

    # No chasing: still near the level that was swept.
    if abs(price - sweep['price']) > SW_NEAR_ATR * atr_v:
        return []

    # Confirmation: structure changed character, or a rejection wick, after the sweep.
    rej = next((e for e in events
                if e['kind'] == 'rejection' and e['bias'] == sweep['bias']
                and e['bars_since'] <= 4), None)
    if not reversal.get('has_choch') and rej is None:
        return []

    # Higher timeframes strongly the other way: the sweep is more likely a pause.
    mtf = snap.get('mtf') or {}
    score = int(mtf.get('score') or 0)
    if (score <= -SW_HTF_BLOCK and side == 'buy') or (score >= SW_HTF_BLOCK and side == 'sell'):
        return []

    ev_list = [
        _ev(f"confirmed sweep of {sweep['price']:.2f}, reclaimed and held",
            24, 'liquidity'),
        _ev(sweep['notes'][0], 8, 'liquidity'),
    ]
    against = []

    if reversal.get('has_choch'):
        ev_list.append(_ev('structure changed character after the sweep', 16, 'structure'))
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

    if mtf.get('direction') == ('up' if side == 'buy' else 'down'):
        ev_list.append(_ev(f"higher timeframes agree ({score:+d})", 12, 'mtf'))
    elif mtf.get('direction'):
        against.append(f"higher timeframes lean the other way ({score:+d})")

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
        playbook='liquidity_sweep_reversal', label='', side=side,
        symbol=snap['symbol'], tf=snap['tf'],
        entry=price, stop=stop, tp1=tp1, tp2=tp2, entry_type='market',
        confidence=_score(ev_list, 30), evidence=ev_list, against=against,
        atr_at_signal=atr_v,
        invalidation=f"a close beyond {extreme:.2f} means the sweep failed",
        expiry_bars=12, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    sig = _finish(sig)

    # Room: the first opposing zone / level must be at least SW_MIN_ROOM_R away,
    # measured with the FINAL stop (after the noise-floor widening). TP1 at 1R
    # inside a supply zone is the trade both losses were.
    ob_px, ob_txt = _first_obstacle(snap, price, side)
    risk = abs(sig.entry - sig.stop)
    if ob_px is not None and risk > 0:
        room = abs(ob_px - sig.entry) / risk
        if room < SW_MIN_ROOM_R:
            return []
        sig.evidence.append(_ev(f"first obstacle {ob_txt} is {room:.1f}R away", 6, 'structure'))
    return [sig]


# --------------------------------------------------------------------------- #
# playbook 3: breakout + retest                                               #
# --------------------------------------------------------------------------- #
# Fixed 2026-11-08 after two lab losses (gold 1h 9 Oct and 14 Oct, both -1R):
#   1. it entered BEFORE the break was confirmed (only -10 confidence) - a dip
#      0.04 ATR under a level in an uptrend was sold as a "breakdown". Now the
#      break must be the chart's two-close rule: two closes in a row more than
#      BR_BREAK_ATR x ATR beyond the level, and the retest after them.
#   2. the regime bonus ignored direction ("UPTREND - breaks tend to run" +12
#      on a SELL). Now a trend only scores when it points the break's way, and
#      a trend the other way is a penalty.
#   3. the break bar always scored +10, even at 0.6x the average range. Now it
#      is scored by its size, and a spike (> BR_SPIKE_ATR x ATR) counts against.
#   4. it could enter up to 5 bars after the retest, 1.4 ATR from the level
#      (chasing). Now only on the retest bar itself, within BR_NEAR_ATR of it.
BR_BREAK_ATR = 0.3
BR_NEAR_ATR = 0.5
BR_SPIKE_ATR = 3.0


def pb_breakout_retest(snap: dict, series) -> list:
    atr_v = snap['atr']
    price = snap['price']
    events = snap.get('events') or []
    n = len(series)
    c = np.asarray(series.c, dtype=float)
    h = np.asarray(series.h, dtype=float)
    l = np.asarray(series.l, dtype=float)

    # (4) the retest is THIS bar, and price is still at the level
    retest = next((e for e in events
                   if e['kind'] == 'retest' and e['confirmed'] and e['bars_since'] <= 0), None)
    if retest is None:
        return []
    brk = next((e for e in events
                if e['kind'] == 'breakout' and e['direction'] == retest['direction']
                and abs(e['price'] - retest['price']) < atr_v * 0.3), None)
    if brk is None:
        return []

    side = 'buy' if retest['bias'] == 'bullish' else 'sell'
    up = side == 'buy'
    level = retest['price']
    if abs(price - level) > BR_NEAR_ATR * atr_v:
        return []

    # (1) two closes beyond the level, starting at the break bar, before the retest
    bi = int(brk['idx'])
    if bi < 1 or bi + 1 >= n - 1:
        return []
    d = BR_BREAK_ATR * atr_v
    outside = (lambda k: c[k] > level + d) if up else (lambda k: c[k] < level - d)
    if not (outside(bi) and outside(bi + 1)):
        return []

    ev_list = [
        _ev(f"broke {level:.2f} on two closes and retested it", 24, 'structure'),
        _ev(f"retest held ({retest['strength']}/100)", 14, 'event'),
    ]
    against = []

    # (3) the break bar, by its size against the 20 bars before it
    rng = float(h[bi] - l[bi])
    avg = float(np.mean(h[max(0, bi - 20):bi] - l[max(0, bi - 20):bi])) if bi > 1 else rng
    ratio = rng / avg if avg > 0 else 1.0
    if rng > BR_SPIKE_ATR * atr_v:
        against.append(f"the break bar was a {rng / atr_v:.1f} ATR spike - spikes like this "
                       f"often whipsaw back")
        ev_list.append(_ev(f"break bar a {rng / atr_v:.1f} ATR spike", -12, 'event'))
    elif ratio >= 1.2:
        ev_list.append(_ev(f"strong break bar ({ratio:.1f}x average range)", 10, 'event'))
    elif ratio >= 0.8:
        ev_list.append(_ev(f"break bar {ratio:.1f}x average range", 4, 'event'))
    else:
        against.append(f"weak break bar ({ratio:.1f}x average range)")
        ev_list.append(_ev(f"weak break bar ({ratio:.1f}x average range)", -6, 'event'))

    # (2) the regime only helps when it points the break's way
    regime = snap.get('regime') or {}
    rdir = regime.get('direction')
    if regime.get('state') == 'trend':
        if rdir == ('up' if up else 'down'):
            ev_list.append(_ev(f"regime is {regime.get('label')} - with the break", 12, 'regime'))
        else:
            against.append(f"regime is {regime.get('label')} - the break is against it")
            ev_list.append(_ev(f"break against the {regime.get('label')} regime", -12, 'regime'))
    elif regime.get('state') == 'squeeze':
        ev_list.append(_ev(f"regime is {regime.get('label')} - breaks tend to run", 8, 'regime'))
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
# playbook 4: false break (2026-11-08; replaces false_break_fade)            #
# --------------------------------------------------------------------------- #
# The break of a RANGE EDGE or a STRONG LEVEL that failed, by the chart's own
# breakout rule (ranges.failed_break): two closes in a row more than 0.3 ATR
# beyond it, then a close back inside within 3 bars. The signal is the bar
# that closed back inside; the trade fades the failed side. false_break_fade
# took any engine 'false_break' event and targeted the regime's 120-bar
# high / low - this one only fades structure a trader would draw, and
# targets the other side of THAT structure.
FB_LEVEL_SCORE = 55          # a level this strong (0-100) counts as structure
FB_LEVEL_NEAR_ATR = 4.0      # ...when it is within this of price


def pb_false_break(snap: dict, series) -> list:
    atr_v = snap['atr']
    price = snap['price']
    t, h, l, c = series.t, series.h, series.l, series.c
    rs = snap.get('range_structure') or {}

    # Range edges first: they are what the chart draws and the cleanest trap.
    cands = []
    if rs.get('type') == 'horizontal':
        cands.append((rs['top'], f"range top {rs['top']:.2f}", rs['bot']))
        cands.append((rs['bot'], f"range bottom {rs['bot']:.2f}", rs['top']))
    for lv in sorted(snap.get('levels') or [], key=lambda x: -x.get('score', 0)):
        if lv.get('score', 0) < FB_LEVEL_SCORE:
            continue
        if abs(lv['price'] - price) > atr_v * FB_LEVEL_NEAR_ATR:
            continue
        cands.append((lv['price'], f"{lv.get('kind', 'level')} {lv['price']:.2f} "
                                   f"(score {lv['score']})", None))
        if len(cands) >= 8:
            break

    fb, name, far = None, '', None
    for level, nm, opp in cands:
        hit = rg_failed_break(t, h, l, c, level, within=1)
        if hit is not None:
            fb, name, far = hit, nm, opp
            break
    if fb is None:
        return []

    side = 'sell' if fb['side'] == 'above' else 'buy'
    ev_list = [
        _ev(f"false break of the {name}: {fb['outside_bars']} closes "
            f"{fb['side']} it, then back inside", 26, 'event'),
    ]
    against = []
    regime = snap.get('regime') or {}
    if regime.get('state') in ('range', 'transition'):
        ev_list.append(_ev(f"{regime.get('label')} regime - failed breaks are "
                           f"the dominant behaviour here", 12, 'regime'))
    elif regime.get('state') == 'trend' and regime.get('direction') == \
            ('up' if fb['side'] == 'above' else 'down'):
        against.append(f"{regime.get('label')} regime - the break was WITH the trend "
                       f"and may yet resolve that way")
        ev_list.append(_ev('fading a with-trend break', -10, 'regime'))
    mom = snap.get('momentum') or {}
    if (side == 'buy' and mom.get('total', 0) > 0) or (side == 'sell' and mom.get('total', 0) < 0):
        ev_list.append(_ev(f"momentum turned with the fade ({mom.get('total'):+d})", 8, 'momentum'))

    pad = atr_v * 0.3
    stop = fb['extreme'] - pad if side == 'buy' else fb['extreme'] + pad
    if (side == 'buy' and stop >= price) or (side == 'sell' and stop <= price):
        return []
    # Target: the other side of the range; for a level, the next strong level
    # the other way.
    structural = far
    if structural is None:
        beyond = [lv['price'] for lv in (snap.get('levels') or [])
                  if lv.get('score', 0) >= FB_LEVEL_SCORE and
                  (lv['price'] > price if side == 'buy' else lv['price'] < price)]
        if beyond:
            structural = min(beyond) if side == 'buy' else max(beyond)
    tp1, tp2 = _targets(price, stop, side, structural)

    sig = Signal(
        playbook='false_break', label='', side=side,
        symbol=snap['symbol'], tf=snap['tf'],
        entry=price, stop=stop, tp1=tp1, tp2=tp2, entry_type='market',
        confidence=_score(ev_list, 30), evidence=ev_list, against=against,
        atr_at_signal=atr_v,
        invalidation=f"a new close beyond {fb['extreme']:.2f} means the break was real",
        expiry_bars=10, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    return [_finish(sig)]


# --------------------------------------------------------------------------- #
# playbook 5: classical pattern reaching its trigger                          #
# --------------------------------------------------------------------------- #
CLASSIC_PATTERNS = ('head_shoulders', 'inverse_head_shoulders', 'double_top',
                    'double_bottom', 'triple_top', 'triple_bottom')


def _break_confirmed(p: dict, side: str, snap: dict, series) -> bool:
    """
    Research rule C2: the break has happened and has force behind it -
    momentum at least +15 with the break, and tick volume over the last 3 bars
    at least 1.2x the median of the 50 before them. A pending (forming)
    pattern has nothing to confirm yet, so it does not pass.
    """
    if p.get('status') != 'confirmed':
        return False
    tot = int((snap.get('momentum') or {}).get('total') or 0)
    if (tot if side == 'buy' else -tot) < 15:
        return False
    v = getattr(series, 'v', None)
    if v is None or len(v) < 53:
        return False
    base = float(np.median(v[-53:-3]))
    return base > 0 and float(np.mean(v[-3:])) >= 1.2 * base


# Tightened 2026-11-08 after a lab loss (gold 1h 24 Aug): a confirmed pattern
# stayed "actionable" for 30 bars, so a pullback 22 bars after the break was
# bought - after the pattern had already reached its target - with the stop at
# the head, 8.4 ATR away. Now:
PB_FRESH_BARS = 5        # a confirmed pattern is traded within this many bars of its break
PB_MAX_STOP_ATR = 4.0    # invalidation further than this from the entry: no trade


def _pattern_break_age(p: dict, c: np.ndarray, side: str):
    """Bars since the first close through the break level after the pattern
    completed, or None if none has closed through."""
    lvl = float(p['break_level'])
    end = int(p.get('end_idx') or 0)
    seg = c[end + 1:] if end + 1 < c.size else c[:0]
    hit = np.nonzero(seg > lvl if side == 'buy' else seg < lvl)[0]
    if not hit.size:
        return None
    return int(c.size - 1 - (end + 1 + int(hit[0])))


def pb_pattern_break(snap: dict, series) -> list:
    atr_v = snap['atr']
    price = snap['price']
    out = []
    q = CONFIG.quality
    # Research rule C3 (off in live): trend regime only.
    if q.pattern_trend_only and (snap.get('regime') or {}).get('state') != 'trend':
        return out

    for p in (snap.get('patterns') or [])[:4]:
        if p['direction'] == 'neutral':
            continue
        if not p.get('actionable'):
            continue
        dist = abs(p['break_level'] - price) / atr_v
        if dist > 1.6:
            continue                      # trigger too far to be a live plan

        side = 'buy' if p['direction'] == 'bullish' else 'sell'
        # Research rules C1, C2 (off in live).
        if q.pattern_classic_only and (p['kind'] not in CLASSIC_PATTERNS
                                       or int(p.get('quality') or 0) < 65):
            continue
        if q.pattern_need_confirm and not _break_confirmed(p, side, snap, series):
            continue
        c_arr = np.asarray(series.c, dtype=float)
        if p['status'] == 'confirmed':
            # Fresh: the break happened in the last PB_FRESH_BARS bars.
            age = _pattern_break_age(p, c_arr, side)
            if age is None or age > PB_FRESH_BARS:
                continue
            # Not spent: price has not already reached the pattern's target.
            end = int(p.get('end_idx') or 0)
            tgt = float(p.get('target') or 0)
            if tgt:
                after_h = np.asarray(series.h, dtype=float)[end:]
                after_l = np.asarray(series.l, dtype=float)[end:]
                if (side == 'buy' and after_h.size and after_h.max() >= tgt) or \
                        (side == 'sell' and after_l.size and after_l.min() <= tgt):
                    continue
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
        if abs(entry - stop) > PB_MAX_STOP_ATR * atr_v:
            continue                      # the pattern is too big to trade from here

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
# playbook 6: range edge fade (2026-11-08; replaces range_fade)               #
# --------------------------------------------------------------------------- #
# Fades an edge of the range price is trading in NOW (ranges.range_structure:
# the chart's flat box when there is one, else a fitted pair of lines), with a
# rule per range TYPE:
#
#   horizontal   either edge; target the opposite edge
#   ascending    buy the lower line only (selling the top of a rising range
#                is fading the move); target the upper line
#   descending   sell the upper line only; target the lower line
#   contracting  either edge while the range is still 2.5+ ATR wide; target
#                the middle - the far edge is closing in
#   expanding    either edge, but only after a SWEEP through the line that
#                closed back inside (the edges keep giving way); target the
#                middle
#
# Every type needs a rejection ON the signal bar: it reached the edge (within
# 0.2 ATR, or through it) and closed back away from it, in the fade's colour.
# range_fade fired off the regime's 120-bar high / low whenever the regime
# read RANGE; this one needs the range itself.
RANGE_EDGE_ZONE = 0.18       # within this fraction of the width from an edge
RANGE_MIN_W_ATR = 2.5        # narrower than this leaves no room for 1R
RANGE_TAG_ATR = 0.2          # the bar must reach the edge to within this
RANGE_STOP_ATR = 0.45        # stop beyond the edge / the bar's extreme


def pb_range_edge_fade(snap: dict, series) -> list:
    rs = snap.get('range_structure')
    if not rs:
        return []
    atr_v = snap['atr']
    price = snap['price']
    kind, top, bot, w = rs['type'], rs['top'], rs['bot'], rs['width']
    if w < atr_v * RANGE_MIN_W_ATR:
        return []
    pos = rs['position']
    if pos >= 1 - RANGE_EDGE_ZONE:
        side, edge, far = 'sell', top, bot
    elif pos <= RANGE_EDGE_ZONE:
        side, edge, far = 'buy', bot, top
    else:
        return []
    if kind == 'ascending' and side == 'sell':
        return []
    if kind == 'descending' and side == 'buy':
        return []

    o, h, l, c = (float(series.o[-1]), float(series.h[-1]),
                  float(series.l[-1]), float(series.c[-1]))
    if side == 'buy':
        tagged = l <= edge + RANGE_TAG_ATR * atr_v
        held = c > o and c > edge
        swept = l < edge
    else:
        tagged = h >= edge - RANGE_TAG_ATR * atr_v
        held = c < o and c < edge
        swept = h > edge
    if not (tagged and held):
        return []
    if kind == 'expanding' and not swept:
        return []

    names = {'horizontal': 'horizontal range', 'ascending': 'ascending range',
             'descending': 'descending range', 'contracting': 'contracting range',
             'expanding': 'expanding range'}
    where = 'top' if side == 'sell' else 'bottom'
    ev_list = [
        _ev(f"{names[kind]} {bot:.2f}-{top:.2f} ({rs['width_atr']:.1f} ATR"
            + (f", {rs['tier']}" if rs.get('tier') else '') + ")", 16, 'structure'),
        _ev(f"rejected the {where} edge {edge:.2f}"
            + (' after a sweep through it' if swept else ''), 16, 'event'),
    ]
    against = []
    if kind in ('ascending', 'descending'):
        ev_list.append(_ev(f"with the {kind} range's slope", 8, 'structure'))
    if kind == 'contracting':
        against.append('a contracting range resolves in a breakout - the fade has '
                       'less room each swing')
    if kind == 'expanding':
        against.append('expanding range - edges keep giving way; size accordingly')
    regime = snap.get('regime') or {}
    if regime.get('state') == 'range':
        ev_list.append(_ev(f"range regime ({regime.get('confidence')})", 8, 'regime'))
    mtf = snap.get('mtf') or {}
    if mtf.get('direction') and mtf.get('direction') != ('up' if side == 'buy' else 'down'):
        against.append(f"higher timeframes lean the other way ({mtf.get('score'):+d})")

    pad = atr_v * RANGE_STOP_ATR
    stop = min(l, edge) - pad if side == 'buy' else max(h, edge) + pad
    if (side == 'buy' and stop >= price) or (side == 'sell' and stop <= price):
        return []
    if kind in ('contracting', 'expanding'):
        structural = bot + w * 0.5
    else:
        # Just inside the far edge: the edge is where the opposite fade sits.
        structural = far - w * 0.1 if side == 'buy' else far + w * 0.1
    tp1, tp2 = _targets(price, stop, side, structural)

    sig = Signal(
        playbook='range_edge_fade', label='', side=side,
        symbol=snap['symbol'], tf=snap['tf'],
        entry=price, stop=stop, tp1=tp1, tp2=tp2, entry_type='market',
        confidence=_score(ev_list, 28), evidence=ev_list, against=against,
        atr_at_signal=atr_v,
        invalidation=f"two closes beyond {edge:.2f} break the range and void the fade",
        expiry_bars=10, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    return [_finish(sig)]


# --------------------------------------------------------------------------- #
# playbook 8: trendline bounce (2026-11-07 - added for the forward books and  #
# the lab; OFF in live settings until a backtest says it earns its place)     #
# --------------------------------------------------------------------------- #
TL_MIN_TOUCHES = 3          # a line price has respected at least this often
TL_TAG_ATR = 0.3            # the bar reached the line within this, or pierced it by no more
TL_STOP_ATR = 0.5           # stop beyond the line


def pb_trendline_bounce(snap: dict, series) -> list:
    """
    A rejection off the CURRENT leg's trend line, traded with the leg: in a
    rally, the bar tags the rising support line and closes back above it, up;
    in a decline, it tags the falling resistance and closes back below, down.
    The lines are engine/geometry.trendlines - the ones the chart draws.
    """
    lines = [x for x in ((snap.get('chart_trendlines') or {}).get('lines') or []) if x.get('live')]
    if not lines:
        return []
    atr_v = snap['atr']
    sup = next((x for x in lines if x['kind'] == 'support'), None)
    res = next((x for x in lines if x['kind'] == 'resistance'), None)
    trend_line = None
    if sup and sup['slope_bar'] > 0 and (res is None or res['slope_bar'] > 0):
        side, trend_line, other = 'buy', sup, res
    elif res and res['slope_bar'] < 0 and (sup is None or sup['slope_bar'] < 0):
        side, trend_line, other = 'sell', res, sup
    if trend_line is None or int(trend_line.get('touches') or 0) < TL_MIN_TOUCHES:
        return []

    line = float(trend_line['y2'])                # its value at the last bar
    o, h, l, c = (float(series.o[-1]), float(series.h[-1]),
                  float(series.l[-1]), float(series.c[-1]))
    if side == 'buy':
        tagged = abs(l - line) <= TL_TAG_ATR * atr_v
        held = c > line and c > o
    else:
        tagged = abs(h - line) <= TL_TAG_ATR * atr_v
        held = c < line and c < o
    if not (tagged and held):
        return []

    ev_list = [
        _ev(f"{'rising support' if side == 'buy' else 'falling resistance'} held "
            f"({trend_line['touches']} touches)", 22, 'structure'),
        _ev(f"bar closed back {'above' if side == 'buy' else 'below'} the line, "
            f"{'up' if side == 'buy' else 'down'}", 10, 'event'),
    ]
    against = []
    mtf = snap.get('mtf') or {}
    if mtf.get('direction') == ('up' if side == 'buy' else 'down'):
        ev_list.append(_ev(f"higher timeframes agree ({mtf.get('score'):+d})", 12, 'mtf'))
    elif mtf.get('direction'):
        against.append(f"higher timeframes lean the other way ({mtf.get('score'):+d})")
        ev_list.append(_ev('against the higher-timeframe lean', -10, 'mtf'))
    mom = snap.get('momentum') or {}
    if (side == 'buy' and mom.get('total', 0) > 10) or (side == 'sell' and mom.get('total', 0) < -10):
        ev_list.append(_ev(f"momentum agrees ({mom.get('total'):+d})", 6, 'momentum'))

    price = snap['price']
    stop = line - TL_STOP_ATR * atr_v if side == 'buy' else line + TL_STOP_ATR * atr_v
    if (side == 'buy' and stop >= price) or (side == 'sell' and stop <= price):
        return []
    structural = float(other['y2']) if other else None   # the leg's other line, now
    tp1, tp2 = _targets(price, stop, side, structural)

    sig = Signal(
        playbook='trendline_bounce', label='', side=side,
        symbol=snap['symbol'], tf=snap['tf'],
        entry=price, stop=stop, tp1=tp1, tp2=tp2, entry_type='market',
        confidence=_score(ev_list, 30), evidence=ev_list, against=against,
        atr_at_signal=atr_v,
        invalidation=f"a close {'below' if side == 'buy' else 'above'} {line:.2f} breaks the line",
        expiry_bars=12, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    return [_finish(sig)]


PLAYBOOKS = {
    'liquidity_sweep_reversal': pb_liquidity_sweep_reversal,
    'breakout_retest': pb_breakout_retest,
    'false_break': pb_false_break,
    'pattern_break': pb_pattern_break,
    'range_edge_fade': pb_range_edge_fade,
    'trendline_bounce': pb_trendline_bounce,
}


def playbook_on(name: str, tf: str) -> bool:
    """
    Settings -> Playbooks: is this playbook switched on for this timeframe?
    Off-list first, then the per-timeframe map (a playbook not in the map runs
    on every timeframe). The one rule _generate, the Telegram alerts and the
    send-time re-check all use (2026-11-12).
    """
    if name in set(CONFIG.gates.disabled_playbooks or ()):
        return False
    tfs = (CONFIG.gates.playbook_timeframes or {}).get(name)
    return tfs is None or tf in tfs


def generate(snap: dict, series, only: list = None,
             respect_regime: bool = True) -> list:
    """_generate() under the snapshot's price precision (engine/precision.py)."""
    with using((snap or {}).get('digits')):
        return _generate(snap, series, only, respect_regime)


def _generate(snap: dict, series, only: list = None,
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
    # The taxonomy playbooks carry their own context rules (trend, range,
    # structure) - the regime table does not gate them.
    allowed.update(MORE)
    # liquidity_sweep_reversal carries its own context rules since 2026-11-08
    # (fresh sweep, confirmation, HTF block, room to the first obstacle); the
    # range/transition-only gate removed the best of what those let through.
    allowed.add('liquidity_sweep_reversal')

    out = []
    # Settings -> Playbooks (the off-list and the per-timeframe map). Ignored
    # for an explicit request.
    tf = snap.get('tf')
    for name, fn in PLAYBOOKS.items():
        if only and name not in only:
            continue
        if not only and not playbook_on(name, tf):
            continue
        if respect_regime and name not in allowed:
            continue
        try:
            out.extend(fn(snap, series) or [])
        except Exception:                       # noqa: BLE001
            # One broken playbook must not silence the other six.
            continue

    out = _apply_stop_mode(out, snap)
    if CONFIG.quality.zone_confluence:
        for sig in out:
            _zone_confluence(sig, snap, series)
    return resolve(out)


# --------------------------------------------------------------------------- #
# order-block stops (2026-11-12) - risk.stop_mode = 'order_block'             #
# --------------------------------------------------------------------------- #
# The stop goes beyond the order block the trade leans on - the nearest live
# block on the trade's OWN timeframe, on the trade's side, behind the entry -
# instead of a fixed ATR distance. Higher-timeframe blocks are not used: on
# the lab trades they sat ~3 ATR away and lost money as stops. See
# RiskSettings.stop_mode in config.py for the measurement.
_TF_MIN = {'1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '2h': 120,
           '4h': 240, '1d': 1440, '1w': 10080}


def _order_block_stop(sig: Signal, snap: dict):
    r = CONFIG.risk
    atr_v = float(sig.atr_at_signal or snap.get('atr') or 0.0)
    if atr_v <= 0:
        return None, None
    buf = (r.ob_stop_buffer_low_atr if _TF_MIN.get(snap.get('tf'), 60) <= 30
           else r.ob_stop_buffer_high_atr) * atr_v
    best, best_z = None, None
    for z in snap.get('order_blocks') or []:
        lo, hi = float(z['low']), float(z['high'])
        if sig.side == 'buy' and z.get('side') == 'bullish' and lo < sig.entry:
            stop = lo - buf
        elif sig.side == 'sell' and z.get('side') == 'bearish' and hi > sig.entry:
            stop = hi + buf
        else:
            continue
        d = abs(sig.entry - stop) / atr_v
        if r.ob_stop_min_atr <= d <= r.ob_stop_max_atr and (
                best is None or d < abs(sig.entry - best) / atr_v):
            best, best_z = stop, z
    return best, best_z


def _apply_stop_mode(out: list, snap: dict) -> list:
    r = CONFIG.risk
    if r.stop_mode != 'order_block' or snap.get('tf') not in tuple(r.ob_stop_timeframes):
        return out
    kept = []
    for sig in out:
        stop, z = _order_block_stop(sig, snap)
        if stop is None:
            if r.ob_stop_fallback == 'skip':
                continue
            sig.evidence.append(_ev(
                f'no order block {r.ob_stop_min_atr:g}-{r.ob_stop_max_atr:g} ATR behind the '
                'entry - kept the playbook stop', 0, 'risk'))
            kept.append(sig)
            continue
        # Keep a structural TP2 across the move, as _widen_to_noise_floor does.
        old_risk = abs(sig.entry - sig.stop)
        t_sign = 1.0 if sig.side == 'buy' else -1.0
        fixed_tp2 = sig.entry + t_sign * old_risk * r.tp2_r
        structural = (None if abs(sig.tp2 - fixed_tp2) <= 1e-6 * max(1.0, abs(fixed_tp2))
                      else sig.tp2)
        sig.stop = stop
        sig.tp1, sig.tp2 = _targets(sig.entry, sig.stop, sig.side, structural)
        risk = abs(sig.entry - sig.stop)
        sig.risk_points = ra(risk)
        sig.rr1 = round(abs(sig.tp1 - sig.entry) / risk, 2) if risk > 1e-9 else 0.0
        sig.rr2 = round(abs(sig.tp2 - sig.entry) / risk, 2) if risk > 1e-9 else 0.0
        for f in ('stop', 'tp1', 'tp2'):
            setattr(sig, f, rp(getattr(sig, f)))
        sig.evidence = [e for e in sig.evidence if 'noise floor' not in e.get('text', '')]
        atr_v = float(sig.atr_at_signal or snap.get('atr') or 1.0)
        sig.evidence.append(_ev(
            f"{OB_STOP_TEXT}{z['side']} order block {z['low']:g}-{z['high']:g} "
            f"({risk / atr_v:.2f} ATR)", 0, 'risk'))
        kept.append(sig)
    return kept


# --------------------------------------------------------------------------- #
# zone confluence (2026-11-07) - order blocks, supply/demand, FVGs             #
# --------------------------------------------------------------------------- #
# Read on EVERY playbook's signal, after the playbook has scored it. FOR: price
# came back into a band on the trade's own side and the entry sits on it - a
# buy off a bullish order block, a demand zone or a bullish gap. AGAINST: an
# opposing band waits between the entry and TP1. A band counts while price has
# not closed through it (zones.py drops the dead ones); one used three times or
# more is known to everyone and is not counted for.
ZONE_FOR = {'ob': 8, 'zone': 6, 'fvg': 4}         # confidence, per kind
ZONE_AGAINST = {'ob': 8, 'zone': 6, 'fvg': 4}
ZONE_CAP = 12                                     # most a side's bands may add or take
ZONE_TAP_BARS = 3                                 # price touched the band this recently
ZONE_NAMES = {'ob': 'order block', 'zone': 'zone', 'fvg': 'fair value gap'}


def _zone_name(z: dict) -> str:
    side = {'demand': 'demand', 'supply': 'supply'}.get(z['side'], z['side'])
    return (f"{side} zone" if z['kind'] == 'zone' else f"{side} {ZONE_NAMES[z['kind']]}")         + f" {z['low']:g}-{z['high']:g}"


def _zone_confluence(sig: Signal, snap: dict, series) -> None:
    from .zones import BUY_SIDES, SELL_SIDES, all_zones
    bands = all_zones(snap)
    if not bands:
        return
    atr_v = float(snap.get('atr') or 0.0)
    buy = sig.side == 'buy'
    own, opp = (BUY_SIDES, SELL_SIDES) if buy else (SELL_SIDES, BUY_SIDES)
    k = min(ZONE_TAP_BARS, len(series.c))
    reach = float(min(series.l[-k:])) if buy else float(max(series.h[-k:]))
    pad = 0.1 * atr_v

    plus, minus, seen_for, seen_against = 0, 0, set(), set()
    for z in bands:
        lo, hi, kind = float(z['low']), float(z['high']), z['kind']
        if z['side'] in own and kind not in seen_for and int(z.get('touches') or 0) <= 2:
            # Tapped from the trade's side, entry not beyond its far edge.
            tapped = (lo - pad <= reach <= hi + pad) if buy else (lo - pad <= reach <= hi + pad)
            held = sig.entry >= lo if buy else sig.entry <= hi
            if tapped and held:
                seen_for.add(kind)
                w = min(ZONE_FOR[kind], ZONE_CAP - plus)
                if w > 0:
                    plus += w
                    sig.evidence.append(_ev(f'entry off a {_zone_name(z)}', w, 'zone'))
        elif z['side'] in opp and kind not in seen_against:
            near = lo if buy else hi
            ahead = (sig.entry < near < sig.tp1) if buy else (sig.tp1 < near < sig.entry)
            if ahead:
                seen_against.add(kind)
                w = min(ZONE_AGAINST[kind], ZONE_CAP - minus)
                if w > 0:
                    minus += w
                    sig.evidence.append(_ev(f'{_zone_name(z)} before TP1', -w, 'zone'))
                    sig.against.append(f'{_zone_name(z)} sits between entry and TP1')
    if plus or minus:
        # The playbook's score, back to its raw sum (_score's curve inverted),
        # moved by the zones and calibrated again.
        conf = sig.confidence
        raw = conf if conf <= 55 else 55 + (conf - 55) / 0.45
        sig.confidence = _score([], int(round(raw + plus - minus)))


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


# --------------------------------------------------------------------------- #
# the playbook taxonomy (2026-11-08) - server/engine/playbooks_more.py         #
# --------------------------------------------------------------------------- #
# Imported LAST: that module builds its signals with the helpers above.
from .playbooks_more import MORE, MORE_LABELS          # noqa: E402

PLAYBOOKS.update(MORE)
PLAYBOOK_LABELS.update(MORE_LABELS)
