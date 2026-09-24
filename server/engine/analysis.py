"""
server/engine/analysis.py - one call, one complete read of the market.

Everything the UI draws and everything the signal engine reasons over comes
from `analyse()`. Having a single entry point matters more than it looks:
during a backtest the identical function runs on an expanding window, so the
live chart and the backtest are provably looking at the same computation. If
they diverged, the backtest would be measuring a system that does not exist.

The returned snapshot is deliberately plain dicts and lists - JSON-ready, no
custom encoders - because it goes straight down a websocket.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np

from ..config import CONFIG, MTF_LADDER, TF_SECONDS, session_quality, sessions_at
from ..datafeed import Series
from . import events as ev
from . import levels as lv
from . import patterns as pat
from . import regime as rg
from . import structure as st
from . import trendlines as tl
from . import zones as zn
from .indicators import (atr, candle_stats, ema, last_valid, macd, rolling_percentile,
                         roc, rsi, volume_profile)


def _momentum_block(o, h, l, c, v, atr_arr) -> dict:
    """RSI / MACD / ROC plus a candle-pressure read, normalised to -100..100."""
    r = rsi(c, CONFIG.engine.rsi_period)
    line, sig, hist = macd(c)
    ro = roc(c, 10)
    cs = candle_stats(o, h, l, c)
    atr_now = last_valid(atr_arr, 1e-9) or 1e-9

    rsi_now = last_valid(r, 50.0)
    hist_now = last_valid(hist, 0.0)
    roc_now = last_valid(ro, 0.0)

    # Candle pressure: where the last N bars closed inside their own range.
    tail = min(10, c.size)
    close_pos = float(np.mean(cs['close_pos'][-tail:])) if tail else 0.5
    bull_share = float(np.mean(cs['bull'][-tail:])) if tail else 0.5

    # Volume lean: up-bar volume vs down-bar volume, a crude order-flow proxy.
    flow = 0.0
    if v is not None and v.size >= tail and v[-tail:].sum() > 0:
        up_v = float(v[-tail:][cs['bull'][-tail:]].sum())
        flow = (2.0 * up_v / float(v[-tail:].sum())) - 1.0

    scale = lambda x, span: int(max(-100, min(100, round(100.0 * x / span))))  # noqa: E731

    return {
        'rsi': round(rsi_now, 1),
        'rsi_score': scale(rsi_now - 50.0, 50.0),
        'macd_hist': round(hist_now, 4),
        'macd_score': scale(hist_now / atr_now, 0.6),
        'roc': round(roc_now, 3),
        'roc_score': scale(roc_now, 0.5),
        'candles_score': scale(close_pos - 0.5, 0.5),
        'flow_score': scale(flow, 1.0),
        'bull_share': round(bull_share, 2),
        'total': 0,          # filled below
        'divergence': _divergence(c, r),
    }


def _divergence(c: np.ndarray, r: np.ndarray, lookback: int = 60) -> dict:
    """
    Regular and hidden RSI divergence over the last two price extremes.

    Regular divergence = reversal warning (price makes a new extreme, momentum
    does not). Hidden divergence = continuation (momentum makes a new extreme,
    price does not). Both matter; conflating them is a common and expensive bug.
    """
    n = c.size
    if n < 25 or r.size != n:
        return {'kind': None}
    lb = min(lookback, n - 2)
    seg_c, seg_r = c[-lb:], r[-lb:]
    if np.isnan(seg_r).all():
        return {'kind': None}

    half = lb // 2
    a_hi = int(np.nanargmax(seg_c[:half])); b_hi = int(half + np.nanargmax(seg_c[half:]))
    a_lo = int(np.nanargmin(seg_c[:half])); b_lo = int(half + np.nanargmin(seg_c[half:]))

    def rv(i):
        return float(seg_r[i]) if not np.isnan(seg_r[i]) else 50.0

    # Segment indices are useless to a caller that wants to DRAW this - the
    # chart addresses bars in the full series. Convert once, here, rather than
    # making every consumer reconstruct the offset.
    base = n - lb

    def found(kind, bias, note, i, j):
        return {
            'kind': kind, 'bias': bias, 'note': note,
            # The two pivots the conclusion rests on: bar index, price and RSI
            # at each. Everything needed to draw the pair of lines that make
            # the divergence visible instead of merely asserted.
            'pivots': {
                'from': {'idx': base + i, 'price': float(seg_c[i]), 'rsi': rv(i)},
                'to': {'idx': base + j, 'price': float(seg_c[j]), 'rsi': rv(j)},
            },
            # Bearish setups are always read off the HIGHS, bullish off the
            # lows - regular and hidden alike. The chart needs this to know
            # which price extreme to anchor its line to.
            'side': 'high' if bias == 'bearish' else 'low',
        }

    # bearish regular: higher price high, lower RSI high
    if seg_c[b_hi] > seg_c[a_hi] and rv(b_hi) < rv(a_hi) - 2:
        return found('bearish_regular', 'bearish',
                     f'price made a higher high, RSI made a lower high '
                     f'({rv(a_hi):.0f} -> {rv(b_hi):.0f})', a_hi, b_hi)
    # bullish regular: lower price low, higher RSI low
    if seg_c[b_lo] < seg_c[a_lo] and rv(b_lo) > rv(a_lo) + 2:
        return found('bullish_regular', 'bullish',
                     f'price made a lower low, RSI made a higher low '
                     f'({rv(a_lo):.0f} -> {rv(b_lo):.0f})', a_lo, b_lo)
    # hidden bullish: higher price low, lower RSI low (continuation up)
    if seg_c[b_lo] > seg_c[a_lo] and rv(b_lo) < rv(a_lo) - 2:
        return found('bullish_hidden', 'bullish',
                     'higher low in price against a lower low in RSI - continuation',
                     a_lo, b_lo)
    # hidden bearish
    if seg_c[b_hi] < seg_c[a_hi] and rv(b_hi) > rv(a_hi) + 2:
        return found('bearish_hidden', 'bearish',
                     'lower high in price against a higher high in RSI - continuation',
                     a_hi, b_hi)
    return {'kind': None}


def _participation(v: np.ndarray, tail: int = 20) -> float:
    """Current activity as a percentile of its own recent history, 0..100."""
    if v is None or v.size < 30 or not v.any():
        return 50.0
    pct = rolling_percentile(v.astype(np.float64), min(300, v.size), tail=tail)
    return float(np.nanmean(pct[-tail:])) if not np.isnan(pct[-tail:]).all() else 50.0


def analyse(series: Series, mtf_reads: dict = None,
            spec: dict = None, quote: dict = None) -> dict:
    """
    The full snapshot for one symbol on one timeframe.

    `mtf_reads` is an optional {tf: trend_dict} from higher timeframes; when
    omitted the MTF block reports "not evaluated" rather than silently claiming
    alignment, because a false alignment claim is worse than none.
    """
    t0 = time.perf_counter()
    cfg = CONFIG.engine
    n = len(series)
    if n < 60:
        return {'ok': False, 'reason': f'need 60+ bars, have {n}',
                'symbol': series.symbol, 'tf': series.tf}

    o, h, l, c, v, t = series.o, series.h, series.l, series.c, series.v, series.t
    atr_arr = atr(h, l, c, cfg.atr_period)
    atr_now = last_valid(atr_arr, 0.0)
    if atr_now <= 0:
        return {'ok': False, 'reason': 'ATR unavailable', 'symbol': series.symbol,
                'tf': series.tf}

    ema_f = ema(c, cfg.ema_fast)
    ema_s = ema(c, cfg.ema_slow)
    price = float(c[-1])

    # --- structure --------------------------------------------------------- #
    swings = st.find_swings(h, l, c, t, cfg.swing_fractal_k, cfg.swing_atr_mult,
                            atr_arr, cfg.swing_max)
    breaks = st.structure_breaks(swings, h, l, c, t)
    trend = st.trend_state(swings, breaks, c, ema_f, ema_s)
    fib = st.leg_retracement(swings, price)
    # Retracement of the last impulse in each direction - what a pullback
    # entry actually reasons over. See structure.impulse_retracement.
    # min_span skips noise legs so the search finds a real impulse to measure.
    min_impulse = atr_now * 3.0
    pullback = {
        'up': st.impulse_retracement(swings, price, 'up', min_impulse),
        'down': st.impulse_retracement(swings, price, 'down', min_impulse),
    }

    eq_tol = atr_now * 0.30
    eq_highs = st.equal_levels(swings, eq_tol, 'high')
    eq_lows = st.equal_levels(swings, eq_tol, 'low')

    # --- levels ------------------------------------------------------------ #
    levels = lv.find_levels(h, l, c, t, swings, atr_arr, cfg.level_tolerance_atr,
                            cfg.level_min_touches, cfg.level_max, v)
    above, below = lv.nearest_levels(levels, price)
    sess_levels = lv.session_levels(t, h, l, c)
    pools = lv.liquidity_pools(swings, eq_highs, eq_lows, price, atr_now)

    # --- areas ------------------------------------------------------------- #
    # Bands rather than lines: imbalances price skipped, and the bases strong
    # moves left from. See zones.py for why each is size-tested against ATR.
    fvgs = zn.fair_value_gaps(o, h, l, c, t, atr_arr)
    sd_zones = zn.supply_demand(o, h, l, c, t, atr_arr)

    # --- geometry ---------------------------------------------------------- #
    # Geometry gets a STRUCTURAL window, not the whole buffer. The price arrays
    # stay full so touches and breaks are still tested against all history, and
    # so the bar indices on the returned lines still address the same series the
    # chart is drawing - only the ANCHOR pool is narrowed.
    geo_swings, geo_start = st.structural_pool(
        swings, c.size, cfg.tl_structural_swings,
        cfg.tl_lookback_min_bars, cfg.tl_lookback_max_bars)
    lines = tl.find_trendlines(h, l, c, t, geo_swings, atr_now, cfg.tl_min_touches,
                               cfg.tl_max_violation_atr, cfg.tl_max)
    channels = tl.find_channels(h, l, c, t, lines, atr_now,
                                cfg.channel_min_containment)
    reg_channel = tl.regression_channel(c, t, cfg.regime_lookback, atr_now)

    # --- patterns ---------------------------------------------------------- #
    found = pat.detect_patterns(swings, h, l, c, t, atr_now, cfg.pattern_min_quality)

    # --- events ------------------------------------------------------------ #
    sweeps = ev.detect_sweeps(h, l, c, o, t, swings, atr_now,
                              cfg.sweep_lookback, cfg.sweep_reclaim_bars)
    rejections = ev.detect_rejections(o, h, l, c, t, levels, atr_now,
                                      cfg.wick_rejection_ratio)
    breakouts = ev.detect_breaks(o, h, l, c, v, t, levels, atr_now,
                                 cfg.false_break_bars, cfg.retest_bars)
    all_events = sweeps + rejections + breakouts
    r_arr = rsi(c, cfg.rsi_period)
    reversal = ev.detect_reversal(all_events, breaks, c, t, atr_now, r_arr)

    # --- regime ------------------------------------------------------------ #
    regime = rg.classify(h, l, c, t, swings, trend, channels, atr_arr,
                         cfg.regime_lookback)

    # --- momentum ---------------------------------------------------------- #
    mom = _momentum_block(o, h, l, c, v, atr_arr)
    mom['total'] = int(round(np.mean([
        mom['rsi_score'], mom['macd_score'], mom['roc_score'],
        mom['candles_score'], mom['flow_score'],
    ])))

    # --- multi-timeframe --------------------------------------------------- #
    if mtf_reads:
        mtf = rg.mtf_alignment(mtf_reads, series.tf)
    else:
        mtf = {'score': 0, 'direction': None, 'verdict': 'not evaluated',
               'agreement': 0.0, 'rows': [], 'base_tf': series.tf}

    # --- session ----------------------------------------------------------- #
    bar_time = datetime.fromtimestamp(float(t[-1]) / 1000.0, tz=timezone.utc)
    primary, quality = session_quality(bar_time.hour)
    participation = _participation(v)

    gauge = rg.fear_greed(trend, regime, mom, regime['volatility'], participation)

    vp = volume_profile(h, l, c, v, bins=42)

    snapshot = {
        'ok': True,
        'symbol': series.symbol,
        'tf': series.tf,
        'bars': n,
        'source': series.source,
        'generated_ms': int(time.time() * 1000),
        'bar_time_ms': int(t[-1]),
        'price': round(price, 3),
        # The recorded spread for this bar, when the source carries one. Passing
        # a guessed spread into the cost gates made the same market look
        # tradeable in one year and not in another purely because of the
        # assumption, so the measured value is preferred wherever it exists.
        'spread_points': (round(float(series.spread[-1]), 1)
                          if series.spread is not None and series.spread.size else None),
        'atr': round(atr_now, 4),
        'atr_points': round(atr_now / (spec or {}).get('point', 0.01), 1),
        'ema_fast': round(last_valid(ema_f, price), 3),
        'ema_slow': round(last_valid(ema_s, price), 3),

        'trend': trend,
        'swings': [s.to_dict() for s in swings],
        'breaks': [b.to_dict() for b in breaks[-12:]],
        'fib': fib,
        'pullback': pullback,

        'levels': [x.to_dict() for x in levels],
        'nearest': {
            'above': above.to_dict() if above else None,
            'below': below.to_dict() if below else None,
        },
        'session_levels': sess_levels,
        'liquidity': pools,
        'fvg': fvgs,
        'zones': sd_zones,
        'equal_highs': eq_highs[:5],
        'equal_lows': eq_lows[:5],

        'trendlines': [x.to_dict() for x in lines],
        'channels': [x.to_dict() for x in channels],
        'regression_channel': reg_channel,

        'patterns': [p.to_dict() for p in found],
        'events': [e.to_dict() for e in sorted(all_events, key=lambda e: -e.idx)[:14]],
        'reversal': reversal,

        'regime': regime,
        'momentum': mom,
        'mtf': mtf,
        'volume_profile': {k: vp[k] for k in ('poc', 'vah', 'val')},

        'session': {
            'primary': primary,
            'quality': quality,
            'active': sessions_at(bar_time.hour),
            'utc_hour': bar_time.hour,
            'bar_time': bar_time.isoformat(),
        },
        'gauge': gauge,
        'compute_ms': round((time.perf_counter() - t0) * 1000, 1),
    }
    return snapshot


def quick_trend(series: Series) -> dict:
    """
    The cheap read used to build the MTF ladder.

    Running the full `analyse()` on four timeframes per tick was ~60 ms; this is
    ~4 ms and produces the only part the alignment calculation actually reads.
    """
    if len(series) < 60:
        return {}
    a = atr(series.h, series.l, series.c, 14)
    swings = st.find_swings(series.h, series.l, series.c, series.t, 2, 0.55, a, 24)
    breaks = st.structure_breaks(swings, series.h, series.l, series.c, series.t)
    return st.trend_state(swings, breaks, series.c,
                          ema(series.c, 21), ema(series.c, 55))


def build_mtf(feed, symbol: str, base_tf: str, live: bool = True,
              bars: int = 300) -> dict:
    """Trend reads for the ladder above `base_tf`."""
    out = {}
    for tf in MTF_LADDER.get(base_tf, [base_tf]):
        if tf in out:
            continue
        s = feed.bars(symbol, tf, bars, live=live)
        if len(s) >= 60:
            out[tf] = quick_trend(s)
    return out


__all__ = ['analyse', 'quick_trend', 'build_mtf']
