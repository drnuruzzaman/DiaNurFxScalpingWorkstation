"""
server/engine/playbooks_more.py - the playbook taxonomy (2026-11-08).

Thirty playbooks in four families, each its own setup with its own entry, stop
and target, added to signals.PLAYBOOKS. They follow the same contract as the
original eight: detect a situation, express it as a fully-formed order with
the evidence behind it, and leave the trade / no-trade decision to qualify.py.

ALL ARE OFF BY DEFAULT (configs/settings.json gates.disabled_playbooks): the
owner switches each on from Settings -> Playbooks after testing it in the lab.
None of them has been backtested yet - the rules below are the starting
definitions, written to be readable, not tuned.

    RANGE      range_breakout, range_retest, range_sweep, mid_range_rotation,
               range_compression_breakout, range_expansion, range_failure
    BREAKOUT   hz_resistance_breakout, hz_support_breakdown, trendline_breakout,
               squeeze_breakout, news_breakout, displacement_breakout,
               liquidity_sweep_breakout, bos_breakout, choch_breakout,
               fvg_ob_breakout
    REVERSAL   choch_reversal, ob_reversal, fvg_reversal, engulfing_reversal,
               rejection_wick_reversal, three_candle_reversal,
               double_top_bottom, divergence_reversal
    PULLBACK   trend_structure_pullback, bos_retest_pullback,
               internal_structure_pullback, fvg_pullback, ob_pullback,
               fibonacci_pullback, ma_pullback, volatility_pullback

Conventions, the same in every rule:
  * a setup is judged on CLOSED bars; "the last bar" is the bar that just
    closed, and the market entry is at its close
  * a breakout is the chart's own rule: two closes in a row more than
    BREAK_ATR x ATR beyond the level, the second being the last bar
  * every stop sits beyond a structural price plus an ATR pad; qualify.py /
    _finish then widen it to the noise floor if it is too tight
  * TP1 is 1R; TP2 is the structural target when it is 1.2-6R away, else the
    fixed tp2_r (signals._targets)
  * indices in the snapshot (swings, breaks, events, ranges, zones) are
    indices into the same window the playbook receives

docs/playbooks.html explains every rule with a diagram.
"""
from __future__ import annotations

import numpy as np

from ..config import CONFIG


# signals.py imports this module at its end, so its helpers are reached at
# CALL time - importing them here would make the two modules a cycle.
def _ev(*a, **k):
    from .signals import _ev as f
    return f(*a, **k)


def _score(*a, **k):
    from .signals import _score as f
    return f(*a, **k)


def _targets(*a, **k):
    from .signals import _targets as f
    return f(*a, **k)


def _finish(*a, **k):
    from .signals import _finish as f
    return f(*a, **k)


def Signal(*a, **k):                                   # noqa: N802
    from .signals import Signal as f
    return f(*a, **k)

BREAK_ATR = 0.3          # a close this far beyond a level is outside it
STOP_PAD = 0.3           # ATR beyond the structural stop price
LEVEL_SCORE = 55         # a level this strong counts as structure


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #
def _bars(series):
    return (np.asarray(series.o, float), np.asarray(series.h, float),
            np.asarray(series.l, float), np.asarray(series.c, float))


def _make(snap, series, playbook, side, stop, structural, ev_list, against,
          invalidation, expiry=12, base=30, entry=None, entry_type='market',
          trigger=0.0):
    """One signal, or [] when the stop is on the wrong side of the entry."""
    price = float(snap['price']) if entry is None else float(entry)
    if stop is None or not np.isfinite(stop):
        return []
    if (side == 'buy' and stop >= price) or (side == 'sell' and stop <= price):
        return []
    tp1, tp2 = _targets(price, stop, side, structural)
    sig = Signal(
        playbook=playbook, label='', side=side, symbol=snap['symbol'], tf=snap['tf'],
        entry=price, stop=float(stop), tp1=tp1, tp2=tp2, entry_type=entry_type,
        trigger=float(trigger), confidence=_score(ev_list, base), evidence=ev_list,
        against=against, atr_at_signal=snap['atr'], invalidation=invalidation,
        expiry_bars=expiry, bar_time_ms=snap['bar_time_ms'], bar_idx=len(series) - 1,
    )
    return [_finish(sig)]


def _broke(c, level, atr_v, up: bool) -> bool:
    """The last two closes are beyond `level` by BREAK_ATR x ATR, and the close
    before them was not - a breakout that completes on the last bar."""
    if c.size < 3:
        return False
    d = BREAK_ATR * atr_v
    if up:
        return c[-1] > level + d and c[-2] > level + d and not c[-3] > level + d
    return c[-1] < level - d and c[-2] < level - d and not c[-3] < level - d


def _bull(o, c, i=-1):
    return c[i] > o[i]


def _bear(o, c, i=-1):
    return c[i] < o[i]


def _trend_dir(snap):
    """'up' / 'down' / None from the swing structure (trend.state)."""
    st = str((snap.get('trend') or {}).get('state') or '')
    return 'up' if 'up' in st else 'down' if 'down' in st else None


def _with_htf(snap, side, ev_list, against, w=10):
    mtf = snap.get('mtf') or {}
    d = mtf.get('direction')
    if not d:
        return
    if d == ('up' if side == 'buy' else 'down'):
        ev_list.append(_ev(f"higher timeframes agree ({mtf.get('score'):+d})", w, 'mtf'))
    else:
        against.append(f"higher timeframes lean the other way ({mtf.get('score'):+d})")


def _next_level(snap, price, side, min_score=45):
    """The nearest level beyond price in the trade's direction, or None."""
    lv = [x['price'] for x in (snap.get('levels') or [])
          if x.get('score', 0) >= min_score and
          (x['price'] > price if side == 'buy' else x['price'] < price)]
    if not lv:
        return None
    return min(lv) if side == 'buy' else max(lv)


def _pool(snap, price, side):
    """The strongest unswept liquidity pool ahead of the trade, or None."""
    want = 'buyside' if side == 'buy' else 'sellside'
    ps = [p for p in (snap.get('liquidity') or []) if p.get('side') == want and not p.get('swept')
          and (p['price'] > price if side == 'buy' else p['price'] < price)]
    return max(ps, key=lambda p: p.get('pull', 0))['price'] if ps else None


def _at_support(snap, price_lo, price_hi, side, atr_v, near=0.5):
    """A reason this bar is at structure: a strong level, a zone on the
    trade's side, or a liquidity pool, within `near` ATR. Returns text or ''."""
    probe = price_lo if side == 'buy' else price_hi
    for x in snap.get('levels') or []:
        if x.get('score', 0) >= LEVEL_SCORE and abs(x['price'] - probe) <= near * atr_v:
            return f"{x.get('kind', 'level')} {x['price']:.2f} (score {x['score']})"
    want = 'demand' if side == 'buy' else 'supply'
    for z in snap.get('zones') or []:
        if z.get('side') == want and z['low'] - near * atr_v <= probe <= z['high'] + near * atr_v:
            return f"{want} zone {z['low']:.2f}-{z['high']:.2f}"
    for p in snap.get('liquidity') or []:
        if abs(p['price'] - probe) <= near * atr_v and p.get('swept'):
            return f"swept {p.get('label', 'liquidity')} {p['price']:.2f}"
    return ''


def _ema(x, n):
    a = 2.0 / (n + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, x.size):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _last_breakout_range(snap, n, max_age, min_age=0):
    """The most recent chart range that broke out between min_age and max_age
    bars ago (age of the confirming close), or None."""
    best = None
    for r in snap.get('ranges') or []:
        if not r.get('breakout'):
            continue
        k = int(r['i1']) + 2                       # the second close outside
        age = n - 1 - k
        if min_age <= age <= max_age and (best is None or k > int(best['i1']) + 2):
            best = r
    return best


# =========================================================================== #
# RANGE                                                                       #
# =========================================================================== #
def pb_range_breakout(snap, series):
    """A chart range breaks out on two closes - trade the break."""
    o, h, l, c = _bars(series)
    n = c.size
    r = _last_breakout_range(snap, n, max_age=0)
    if r is None:
        return []
    atr_v = snap['atr']
    up = r['breakout'] == 'up'
    side = 'buy' if up else 'sell'
    top, bot = r['top'], r['bot']
    w = top - bot
    edge = top if up else bot
    mid = (top + bot) / 2
    ev_list = [_ev(f"{r['tier']} range {bot:.2f}-{top:.2f} broke {'up' if up else 'down'} "
                   f"on two closes ({r['i1'] - r['i0'] + 1} bars, "
                   f"{r['touchesTop']}+{r['touchesBot']} edge tests)", 24, 'structure')]
    against = []
    if r['tier'] == 'major':
        ev_list.append(_ev('a major range - more stops behind the edge', 8, 'structure'))
    _with_htf(snap, side, ev_list, against)
    stop = mid
    return _make(snap, series, 'range_breakout', side, stop,
                 edge + w if up else edge - w, ev_list, against,
                 f"a close back inside {edge:.2f} within 3 bars makes it a false break")


def pb_range_retest(snap, series):
    """A range broke out 3-20 bars ago; price came back to the broken edge and
    held - join the breakout on the retest."""
    o, h, l, c = _bars(series)
    n = c.size
    r = _last_breakout_range(snap, n, max_age=20, min_age=3)
    if r is None:
        return []
    atr_v = snap['atr']
    up = r['breakout'] == 'up'
    side = 'buy' if up else 'sell'
    top, bot = r['top'], r['bot']
    edge = top if up else bot
    tag = 0.3 * atr_v
    if up:
        ok = l[-1] <= edge + tag and c[-1] > edge and _bull(o, c)
    else:
        ok = h[-1] >= edge - tag and c[-1] < edge and _bear(o, c)
    if not ok:
        return []
    w = top - bot
    ev_list = [_ev(f"range {bot:.2f}-{top:.2f} broke {r['breakout']}, price came back to "
                   f"{edge:.2f} and held", 26, 'structure')]
    against = []
    _with_htf(snap, side, ev_list, against)
    stop = (min(l[-1], edge) - 0.45 * atr_v) if up else (max(h[-1], edge) + 0.45 * atr_v)
    return _make(snap, series, 'range_retest', side, stop,
                 edge + w if up else edge - w, ev_list, against,
                 f"a close back inside the range below {edge:.2f}" if up else
                 f"a close back inside the range above {edge:.2f}")


def pb_range_sweep(snap, series):
    """A wick through a range edge that closed back inside on the same bar -
    liquidity taken, no acceptance. Fade it toward the far edge."""
    rs = snap.get('range_structure') or {}
    if rs.get('type') != 'horizontal':
        return []
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    top, bot = rs['top'], rs['bot']
    w = top - bot
    if w < 2.0 * atr_v:
        return []
    if h[-1] > top + 0.1 * atr_v and c[-1] < top and _bear(o, c):
        side, ext, far, edge = 'sell', h[-1], bot, top
    elif l[-1] < bot - 0.1 * atr_v and c[-1] > bot and _bull(o, c):
        side, ext, far, edge = 'buy', l[-1], top, bot
    else:
        return []
    ev_list = [_ev(f"swept the range {'top' if side == 'sell' else 'bottom'} {edge:.2f} "
                   f"to {ext:.2f} and closed back inside", 24, 'event')]
    against = []
    _with_htf(snap, side, ev_list, against)
    stop = ext - STOP_PAD * atr_v if side == 'buy' else ext + STOP_PAD * atr_v
    return _make(snap, series, 'range_sweep', side, stop,
                 far + 0.1 * w if side == 'sell' else far - 0.1 * w, ev_list, against,
                 f"a close beyond the sweep {ext:.2f} turns it into a breakout")


def pb_mid_range_rotation(snap, series):
    """Inside a wide range: after a test of one edge, price closes through the
    midline - rotation to the far edge is under way."""
    rs = snap.get('range_structure') or {}
    if rs.get('type') != 'horizontal':
        return []
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    top, bot = rs['top'], rs['bot']
    w = top - bot
    if w < 4.0 * atr_v:
        return []
    mid = (top + bot) / 2
    look = slice(-9, -1)
    if c[-1] > mid and c[-2] <= mid and l[look].min() <= bot + 0.2 * w and _bull(o, c):
        side, far, q = 'buy', top, bot + 0.25 * w
    elif c[-1] < mid and c[-2] >= mid and h[look].max() >= top - 0.2 * w and _bear(o, c):
        side, far, q = 'sell', bot, top - 0.25 * w
    else:
        return []
    ev_list = [_ev(f"range {bot:.2f}-{top:.2f}: tested the "
                   f"{'bottom' if side == 'buy' else 'top'} and closed through the "
                   f"midline {mid:.2f}", 22, 'structure')]
    against = []
    _with_htf(snap, side, ev_list, against)
    stop = q - 0.2 * atr_v if side == 'buy' else q + 0.2 * atr_v
    return _make(snap, series, 'mid_range_rotation', side, stop,
                 far - 0.1 * w if side == 'buy' else far + 0.1 * w, ev_list, against,
                 f"back through the quarter line {q:.2f}", expiry=10)


def pb_range_compression_breakout(snap, series):
    """A range whose bars have been shrinking (or a contracting range) breaks
    on a strong close - the coil releasing."""
    rs = snap.get('range_structure') or {}
    if rs.get('type') not in ('horizontal', 'contracting'):
        return []
    o, h, l, c = _bars(series)
    n = c.size
    atr_v = snap['atr']
    i0 = int(rs.get('i0') or 0)
    if n - 1 - i0 < 20:
        return []
    rng = h - l
    early = rng[i0:i0 + max(5, (n - 1 - i0) // 2)].mean()
    late = rng[-11:-1].mean()
    squeezed = rs['type'] == 'contracting' or late <= 0.65 * early
    if not squeezed:
        return []
    box_hi, box_lo = h[-11:-1].max(), l[-11:-1].min()
    body = abs(c[-1] - o[-1])
    strong = rng[-1] > 0 and body >= 0.6 * rng[-1]
    if c[-1] > box_hi + BREAK_ATR * atr_v and strong:
        side, stop = 'buy', box_lo - 0.2 * atr_v
    elif c[-1] < box_lo - BREAK_ATR * atr_v and strong:
        side, stop = 'sell', box_hi + 0.2 * atr_v
    else:
        return []
    w = rs['top'] - rs['bot']
    ev_list = [_ev(f"{rs['type']} range, bars shrank to {late / max(early, 1e-9) * 100:.0f}% "
                   f"of its early size", 18, 'volatility'),
               _ev(f"strong close out of the {box_lo:.2f}-{box_hi:.2f} coil", 18, 'event')]
    against = []
    _with_htf(snap, side, ev_list, against)
    return _make(snap, series, 'range_compression_breakout', side, stop,
                 c[-1] + w if side == 'buy' else c[-1] - w, ev_list, against,
                 'back inside the coil', expiry=8)


def pb_range_expansion(snap, series):
    """An expanding range: the edges keep giving way. When a line breaks on two
    closes, follow the expansion."""
    rs = snap.get('range_structure') or {}
    if rs.get('type') != 'expanding':
        return []
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    top, bot = rs['top'], rs['bot']
    if _broke(c, top, atr_v, True):
        side, edge = 'buy', top
    elif _broke(c, bot, atr_v, False):
        side, edge = 'sell', bot
    else:
        return []
    w = top - bot
    ev_list = [_ev(f"expanding range (width +{rs['width_change'] * 100:.0f}%), "
                   f"two closes {'above' if side == 'buy' else 'below'} {edge:.2f}", 22, 'structure')]
    against = ['expanding ranges whipsaw - this one may swing back again']
    _with_htf(snap, side, ev_list, against)
    stop = edge - 0.6 * atr_v if side == 'buy' else edge + 0.6 * atr_v
    return _make(snap, series, 'range_expansion', side, stop,
                 edge + 0.5 * w if side == 'buy' else edge - 0.5 * w, ev_list, against,
                 f"back inside {edge:.2f}", expiry=8)


def pb_range_failure(snap, series):
    """A range broke out (confirmed), but 4-15 bars later price is back inside
    and through the midline - the breakout failed and a new structure is
    forming. Trade toward the opposite edge."""
    o, h, l, c = _bars(series)
    n = c.size
    r = _last_breakout_range(snap, n, max_age=15, min_age=4)
    if r is None:
        return []
    atr_v = snap['atr']
    top, bot = r['top'], r['bot']
    mid = (top + bot) / 2
    k = int(r['i1']) + 2
    if r['breakout'] == 'up':
        if not (c[-1] < mid and c[-2] >= mid):
            return []
        side, ext, far = 'sell', h[k:].max(), bot
    else:
        if not (c[-1] > mid and c[-2] <= mid):
            return []
        side, ext, far = 'buy', l[k:].min(), top
    ev_list = [_ev(f"range {bot:.2f}-{top:.2f} broke {r['breakout']} but price is back "
                   f"through its midline {mid:.2f}", 24, 'structure')]
    against = []
    _with_htf(snap, side, ev_list, against)
    stop = mid + 0.5 * (top - mid) + 0.3 * atr_v if side == 'sell' else mid - 0.5 * (mid - bot) - 0.3 * atr_v
    return _make(snap, series, 'range_failure', side, stop, far, ev_list, against,
                 f"failed-breakout extreme {ext:.2f}", expiry=12)


# =========================================================================== #
# BREAKOUT                                                                    #
# =========================================================================== #
def _hz_break(snap, series, up: bool, name: str):
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    kinds = ('resistance', 'flip') if up else ('support', 'flip')
    for x in sorted(snap.get('levels') or [], key=lambda v: -v.get('score', 0)):
        if x.get('score', 0) < LEVEL_SCORE or x.get('kind') not in kinds:
            continue
        lvl = x['price']
        if not _broke(c, lvl, atr_v, up):
            continue
        side = 'buy' if up else 'sell'
        ev_list = [_ev(f"two closes {'above' if up else 'below'} {x['kind']} {lvl:.2f} "
                       f"(score {x['score']}, {x.get('touches', 0)} touches)", 24, 'structure')]
        against = []
        _with_htf(snap, side, ev_list, against)
        if (snap.get('regime') or {}).get('state') == 'range':
            against.append('range regime - level breaks fail more often here')
            ev_list.append(_ev('range regime', -8, 'regime'))
        stop = lvl - 0.5 * atr_v if up else lvl + 0.5 * atr_v
        return _make(snap, series, name, side, stop, _next_level(snap, c[-1], side),
                     ev_list, against, f"a close back {'below' if up else 'above'} {lvl:.2f}")
    return []


def pb_hz_resistance_breakout(snap, series):
    """Bullish: a strong horizontal resistance broken on two closes."""
    return _hz_break(snap, series, True, 'hz_resistance_breakout')


def pb_hz_support_breakdown(snap, series):
    """Bearish: a strong horizontal support broken on two closes."""
    return _hz_break(snap, series, False, 'hz_support_breakdown')


def pb_trendline_breakout(snap, series):
    """A 3+ touch trendline broken AGAINST its slope on two closes: a falling
    resistance broken up, or a rising support broken down."""
    o, h, l, c = _bars(series)
    n = c.size
    atr_v = snap['atr']
    for tl in snap.get('trendlines') or []:
        if int(tl.get('touches') or 0) < 3:
            continue
        slope = float(tl['slope'])
        at = lambda i: tl['y1'] + slope * (i - tl['x1'])           # noqa: E731
        d = BREAK_ATR * atr_v
        if tl['kind'] == 'resistance' and slope < 0:
            if c[-1] > at(n - 1) + d and c[-2] > at(n - 2) + d and not c[-3] > at(n - 3) + d:
                side, stop, tgt = 'buy', at(n - 1) - 0.5 * atr_v, max(tl['y1'], tl['y2'])
            else:
                continue
        elif tl['kind'] == 'support' and slope > 0:
            if c[-1] < at(n - 1) - d and c[-2] < at(n - 2) - d and not c[-3] < at(n - 3) - d:
                side, stop, tgt = 'sell', at(n - 1) + 0.5 * atr_v, min(tl['y1'], tl['y2'])
            else:
                continue
        else:
            continue
        ev_list = [_ev(f"{tl['touches']}-touch {'falling resistance' if side == 'buy' else 'rising support'} "
                       f"broken on two closes", 24, 'structure')]
        against = []
        _with_htf(snap, side, ev_list, against)
        return _make(snap, series, 'trendline_breakout', side, stop, tgt, ev_list, against,
                     'a close back across the line')
    return []


def pb_squeeze_breakout(snap, series):
    """Bollinger width in its lowest 20% of the last 120 bars on the bar before
    (a squeeze), then a strong close beyond the 20-bar high / low."""
    o, h, l, c = _bars(series)
    n = c.size
    if n < 160:
        return []
    atr_v = snap['atr']
    k = 20
    sd = np.array([c[i - k + 1:i + 1].std() for i in range(n - 121, n)])
    ma = np.array([c[i - k + 1:i + 1].mean() for i in range(n - 121, n)])
    bbw = 4 * sd / np.maximum(ma, 1e-9)
    prev = bbw[-2]
    pct = (bbw[:-1] < prev).mean() * 100
    if pct > 20:
        return []
    hi, lo = h[-k - 1:-1].max(), l[-k - 1:-1].min()
    body = abs(c[-1] - o[-1])
    strong = (h[-1] - l[-1]) > 0 and body >= 0.55 * (h[-1] - l[-1])
    if c[-1] > hi and strong:
        side, stop = 'buy', (hi + lo) / 2
    elif c[-1] < lo and strong:
        side, stop = 'sell', (hi + lo) / 2
    else:
        return []
    ev_list = [_ev(f"Bollinger squeeze (width in the lowest {pct:.0f}% of 120 bars)", 18, 'volatility'),
               _ev(f"strong close out of the {lo:.2f}-{hi:.2f} squeeze box", 20, 'event')]
    against = []
    _with_htf(snap, side, ev_list, against)
    return _make(snap, series, 'squeeze_breakout', side, stop,
                 c[-1] + (hi - lo) if side == 'buy' else c[-1] - (hi - lo), ev_list, against,
                 'back inside the squeeze box', expiry=8)


NEWS_WINDOW_MIN = 90          # the breakout must come within this of the release
NEWS_PRE_BARS = 6             # the pre-release range: this many bars before it


def pb_news_breakout(snap, series):
    """Within 90 minutes after a MAJOR US release (NFP, CPI, FOMC, PPI, GDP,
    PCE, retail sales): the first close beyond the pre-release range."""
    nw = snap.get('news') or {}
    if not nw or nw.get('bar_idx') is None or not (0 <= nw['minutes_since'] <= NEWS_WINDOW_MIN):
        return []
    o, h, l, c = _bars(series)
    n = c.size
    atr_v = snap['atr']
    j = int(nw['bar_idx'])
    if j - NEWS_PRE_BARS < 0 or j > n - 1:
        return []
    hi, lo = h[j - NEWS_PRE_BARS:j].max(), l[j - NEWS_PRE_BARS:j].min()
    d = BREAK_ATR * atr_v
    if c[-1] > hi + d and not (c[j:n - 1] > hi + d).any():
        side, stop = 'buy', min(l[j:].min(), (hi + lo) / 2)
    elif c[-1] < lo - d and not (c[j:n - 1] < lo - d).any():
        side, stop = 'sell', max(h[j:].max(), (hi + lo) / 2)
    else:
        return []
    ev_list = [_ev(f"{nw['kind']} released {nw['minutes_since']:.0f} min ago", 16, 'news'),
               _ev(f"first close {'above' if side == 'buy' else 'below'} the pre-release "
                   f"range {lo:.2f}-{hi:.2f}", 20, 'event')]
    against = ['news spikes can reverse hard - spreads widen around releases']
    return _make(snap, series, 'news_breakout', side, stop, None, ev_list, against,
                 'back inside the pre-release range', expiry=6)


def pb_displacement_breakout(snap, series):
    """A displacement candle: body >= 1.5 ATR and >= 70% of its range, closing
    beyond the last 10 bars' high / low. Momentum has arrived."""
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    body = abs(c[-1] - o[-1])
    rng = h[-1] - l[-1]
    if rng <= 0 or body < 1.5 * atr_v or body < 0.7 * rng:
        return []
    if _bull(o, c) and c[-1] > h[-11:-1].max():
        side, stop = 'buy', (o[-1] + c[-1]) / 2 - 0.2 * atr_v
    elif _bear(o, c) and c[-1] < l[-11:-1].min():
        side, stop = 'sell', (o[-1] + c[-1]) / 2 + 0.2 * atr_v
    else:
        return []
    ev_list = [_ev(f"displacement candle: body {body / atr_v:.1f} ATR, "
                   f"{body / rng * 100:.0f}% of its range", 22, 'momentum'),
               _ev('closed beyond the last 10 bars', 10, 'structure')]
    against = []
    _with_htf(snap, side, ev_list, against)
    return _make(snap, series, 'displacement_breakout', side, stop,
                 _next_level(snap, c[-1], side), ev_list, against,
                 'back below the candle midpoint' if side == 'buy' else 'back above the candle midpoint',
                 expiry=6)


def _broke_before(x, c, i, n, up):
    """Did any bar between the sweep (i) and the last bar already close beyond
    the extreme of the bars before it? Then the last bar is not the FIRST break."""
    for j in range(i + 1, n - 1):
        ref = x[i:j].max() if up else x[i:j].min()
        if (c[j] > ref) if up else (c[j] < ref):
            return True
    return False


def pb_liquidity_sweep_breakout(snap, series):
    """Liquidity on one side is swept (within 10 bars), then price breaks the
    OTHER side of that move with a strong close: the sweep fuelled a move the
    other way."""
    o, h, l, c = _bars(series)
    n = c.size
    atr_v = snap['atr']
    sweeps = [e for e in snap.get('events') or []
              if e.get('kind') == 'sweep' and 1 <= e.get('bars_since', 99) <= 10]
    rng = h[-1] - l[-1]
    if not sweeps or rng <= 0 or abs(c[-1] - o[-1]) < 0.6 * rng:
        return []                                    # needs a strong breakout candle
    # Only the MOST RECENT sweep: several sweeps inside one swing would each
    # claim the same breakout.
    for e in [min(sweeps, key=lambda x: x['bars_since'])]:
        i = int(e['idx'])
        if e['bias'] == 'bullish':                   # swept lows -> break the highs
            hi = h[i:n - 1].max()
            if not (c[-1] > hi and _bull(o, c)) or _broke_before(h, c, i, n, True):
                continue
            side, stop = 'buy', e['extreme'] - STOP_PAD * atr_v
        else:
            lo = l[i:n - 1].min()
            if not (c[-1] < lo and _bear(o, c)) or _broke_before(l, c, i, n, False):
                continue
            side, stop = 'sell', e['extreme'] + STOP_PAD * atr_v
        ev_list = [_ev(f"{e.get('label', 'sweep')} {e['bars_since']} bars ago", 16, 'event'),
                   _ev(f"then a close through the other side of the move", 18, 'structure')]
        against = []
        _with_htf(snap, side, ev_list, against)
        return _make(snap, series, 'liquidity_sweep_breakout', side, stop,
                     _pool(snap, c[-1], side), ev_list, against,
                     f"a new extreme beyond the sweep {e['extreme']:.2f}")
    return []


def _last_break(snap, n, kind):
    b = [x for x in snap.get('breaks') or [] if x.get('kind') == kind]
    return b[-1] if b and int(b[-1]['idx']) == n - 1 else None


# Tightened 2026-11-08 after two lab losses (gold 1h): 25 Aug, the 4th BOS of a
# rally that had run 3.5 ATR; 30 Oct, a BOS at the bottom of a 2.4 ATR drop with
# the stop at the last swing high 6.6 ATR away. A BOS late in a move is where
# the move runs out, and a far protected swing makes the loss huge. Now:
BOS_MAX_COUNT = 2        # only the 1st or 2nd BOS since the last CHoCH (early trend)
BOS_MAX_LEG_ATR = 2.5    # the current leg has run no more than this
BOS_MAX_STOP_ATR = 3.0   # the protected swing no further than this from the entry


def _bos_count(snap, up: bool) -> int:
    """Consecutive BOS in this direction since the last CHoCH (or the last break
    the other way), counting the newest."""
    k = 0
    for b in reversed(snap.get('breaks') or []):
        if b.get('kind') == 'BOS' and b.get('direction') == ('up' if up else 'down'):
            k += 1
            continue
        break
    return k


def pb_bos_breakout(snap, series):
    """A Break of Structure on the last bar, EARLY in the move: continuation in
    its direction."""
    o, h, l, c = _bars(series)
    n = c.size
    b = _last_break(snap, n, 'BOS')
    if b is None:
        return []
    atr_v = snap['atr']
    up = b['direction'] == 'up'
    side = 'buy' if up else 'sell'
    count = _bos_count(snap, up)
    if count > BOS_MAX_COUNT:
        return []                                  # late in the move
    leg = snap.get('leg') or {}
    if int(leg.get('dir') or 0) == (1 if up else -1) and float(leg.get('ext_atr') or 0) > BOS_MAX_LEG_ATR:
        return []                                  # the leg is already stretched
    sw = [s for s in snap.get('swings') or [] if s['idx'] < n - 1 and s['kind'] == ('low' if up else 'high')]
    if not sw:
        return []
    prot = sw[-1]['price']
    stop = prot - STOP_PAD * atr_v if up else prot + STOP_PAD * atr_v
    if abs(c[-1] - stop) > BOS_MAX_STOP_ATR * atr_v:
        return []                                  # protected swing too far for this R
    ev_list = [_ev(f"BOS {b['direction']} through {b['price']:.2f} "
                   f"({'first' if count == 1 else 'second'} since the last CHoCH)", 22, 'structure')]
    against = []
    if _trend_dir(snap) == ('up' if up else 'down'):
        ev_list.append(_ev('with the swing trend', 10, 'structure'))
    _with_htf(snap, side, ev_list, against)
    return _make(snap, series, 'bos_breakout', side, stop, _pool(snap, c[-1], side),
                 ev_list, against, f"the protected swing {prot:.2f}")


def pb_choch_breakout(snap, series):
    """A Change of Character on the last bar: the trend may be turning - trade
    the new direction from the break."""
    o, h, l, c = _bars(series)
    n = c.size
    b = _last_break(snap, n, 'CHoCH')
    if b is None:
        return []
    atr_v = snap['atr']
    up = b['direction'] == 'up'
    side = 'buy' if up else 'sell'
    i0 = int(b.get('from_swing') or max(0, n - 30))
    ext = l[i0:].min() if up else h[i0:].max()
    stop = ext - STOP_PAD * atr_v if up else ext + STOP_PAD * atr_v
    ev_list = [_ev(f"CHoCH {b['direction']} through the protected level {b['price']:.2f}", 22, 'structure')]
    against = ['a first CHoCH is often retested - the retest (choch_reversal) is the safer entry']
    _with_htf(snap, side, ev_list, against)
    return _make(snap, series, 'choch_breakout', side, stop, _pool(snap, c[-1], side),
                 ev_list, against, f"the leg extreme {ext:.2f}")


def pb_fvg_ob_breakout(snap, series):
    """Price closes clean through an OPPOSING fair value gap or order block - the
    supply / demand there failed."""
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    n = c.size
    # The snapshot only holds zones still ALIVE - one closed through is gone
    # from it. So the zones are rebuilt as they stood one bar ago (every bar
    # but the last), and the last close is tested against those.
    from . import zones as zn
    from .indicators import atr as atr_fn
    t = np.asarray(series.t)
    po, ph, pl, pc, pt = o[:-1], h[:-1], l[:-1], c[:-1], t[:-1]
    pa = atr_fn(ph, pl, pc, 14)
    from types import SimpleNamespace
    prev_breaks = [SimpleNamespace(**b) for b in snap.get('breaks') or [] if int(b['idx']) < n - 1]
    zones = [(z, 'fair value gap') for z in zn.fair_value_gaps(po, ph, pl, pc, pt, pa)] + \
            [(z, 'order block') for z in zn.order_blocks(po, ph, pl, pc, pt, pa, prev_breaks)]
    for z, name in zones:
        if z.get('side') == 'bearish' and c[-1] > z['high'] + 0.2 * atr_v and c[-2] <= z['high']:
            side, stop = 'buy', z['low'] - STOP_PAD * atr_v
        elif z.get('side') == 'bullish' and c[-1] < z['low'] - 0.2 * atr_v and c[-2] >= z['low']:
            side, stop = 'sell', z['high'] + STOP_PAD * atr_v
        else:
            continue
        ev_list = [_ev(f"closed through the {z['side']} {name} {z['low']:.2f}-{z['high']:.2f}",
                       22, 'structure')]
        against = []
        _with_htf(snap, side, ev_list, against)
        return _make(snap, series, 'fvg_ob_breakout', side, stop,
                     _next_level(snap, c[-1], side), ev_list, against,
                     f"back inside the {name}")
    return []


# =========================================================================== #
# REVERSAL                                                                    #
# =========================================================================== #
def pb_choch_reversal(snap, series):
    """A CHoCH that followed a liquidity sweep (1-12 bars ago); price pulls back
    to the broken level and rejects it - enter the new direction on the retest."""
    o, h, l, c = _bars(series)
    n = c.size
    atr_v = snap['atr']
    br = [b for b in snap.get('breaks') or [] if b.get('kind') == 'CHoCH' and 1 <= n - 1 - int(b['idx']) <= 12]
    if not br:
        return []
    b = br[-1]
    up = b['direction'] == 'up'
    swept = any(e.get('kind') == 'sweep' and e['bias'] == ('bullish' if up else 'bearish')
                and int(e['idx']) <= int(b['idx']) and int(b['idx']) - int(e['idx']) <= 20
                for e in snap.get('events') or [])
    if not swept:
        return []
    lvl = b['price']
    if up:
        ok = l[-1] <= lvl + 0.3 * atr_v and c[-1] > lvl and _bull(o, c)
    else:
        ok = h[-1] >= lvl - 0.3 * atr_v and c[-1] < lvl and _bear(o, c)
    if not ok:
        return []
    side = 'buy' if up else 'sell'
    i0 = int(b.get('from_swing') or max(0, n - 30))
    ext = l[i0:].min() if up else h[i0:].max()
    ev_list = [_ev('liquidity sweep, then a CHoCH', 22, 'structure'),
               _ev(f"retest of the CHoCH level {lvl:.2f} held", 16, 'event')]
    against = []
    _with_htf(snap, side, ev_list, against)
    stop = ext - STOP_PAD * atr_v if up else ext + STOP_PAD * atr_v
    return _make(snap, series, 'choch_reversal', side, stop, _pool(snap, c[-1], side),
                 ev_list, against, f"the reversal extreme {ext:.2f}")


def _zone_tap(snap, series, zones, side_word, name, playbook, need_kind):
    """Price dips into a zone of the trade's side and closes back out of it with
    the trade's colour. `need_kind` picks which structure break made the zone
    (CHoCH = a reversal origin, BOS = a with-trend base) - for FVGs it is read
    off the last structure break instead."""
    o, h, l, c = _bars(series)
    n = c.size
    atr_v = snap['atr']
    last = (snap.get('trend') or {}).get('last_break') or {}
    for z in zones:
        if z.get('side') != side_word[0] and z.get('side') != side_word[1]:
            continue
        bull = z['side'] in ('bullish', 'demand')
        kind = z.get('break_kind') or (last.get('kind') if last.get('direction') == ('up' if bull else 'down') else None)
        if kind != need_kind:
            continue
        if int(z.get('idx', 0)) >= n - 2 or float(z.get('filled') or 0) >= 1.0:
            continue
        if bull:
            ok = l[-1] <= z['high'] and l[-1] >= z['low'] - 0.3 * atr_v and c[-1] > z['high'] and _bull(o, c)
            side, stop = 'buy', min(l[-1], z['low']) - STOP_PAD * atr_v
        else:
            ok = h[-1] >= z['low'] and h[-1] <= z['high'] + 0.3 * atr_v and c[-1] < z['low'] and _bear(o, c)
            side, stop = 'sell', max(h[-1], z['high']) + STOP_PAD * atr_v
        if not ok:
            continue
        ev_list = [_ev(f"tapped the {z['side']} {name} {z['low']:.2f}-{z['high']:.2f} "
                       f"(made by a {need_kind}) and closed back out", 24, 'structure')]
        against = []
        _with_htf(snap, side, ev_list, against)
        sw = [s for s in snap.get('swings') or [] if s['kind'] == ('high' if bull else 'low')]
        tgt = sw[-1]['price'] if sw else None
        return _make(snap, series, playbook, side, stop, tgt, ev_list, against,
                     f"a close through the {name}")
    return []


def pb_ob_reversal(snap, series):
    """Price returns to the order block that a CHoCH came from and rejects it."""
    return _zone_tap(snap, series, snap.get('order_blocks') or [], ('bullish', 'bearish'),
                     'order block', 'ob_reversal', 'CHoCH')


def pb_fvg_reversal(snap, series):
    """After a CHoCH, price fills back into the fair value gap it left and rejects."""
    return _zone_tap(snap, series, snap.get('fvg') or [], ('bullish', 'bearish'),
                     'fair value gap', 'fvg_reversal', 'CHoCH')


def _move_into(c, side, atr_v, bars=5, need=1.0):
    """Price fell (for a buy) / rose (for a sell) at least `need` ATR into the
    signal over the last `bars` bars - there is something to reverse."""
    d = c[-1 - bars] - c[-2]
    return d >= need * atr_v if side == 'buy' else -d >= need * atr_v


def pb_engulfing_reversal(snap, series):
    """A body that engulfs the previous opposite body, after a move into
    structure (a strong level, a zone or a swept pool)."""
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    if _bull(o, c) and _bear(o, c, -2) and c[-1] > o[-2] and o[-1] <= c[-2]:
        side = 'buy'
    elif _bear(o, c) and _bull(o, c, -2) and c[-1] < o[-2] and o[-1] >= c[-2]:
        side = 'sell'
    else:
        return []
    if not _move_into(c, side, atr_v):
        return []
    lo, hi = min(l[-1], l[-2]), max(h[-1], h[-2])
    where = _at_support(snap, lo, hi, side, atr_v)
    if not where:
        return []
    ev_list = [_ev(f"{'bullish' if side == 'buy' else 'bearish'} engulfing", 18, 'event'),
               _ev(f"at {where}", 14, 'structure')]
    against = []
    _with_htf(snap, side, ev_list, against)
    stop = lo - 0.2 * atr_v if side == 'buy' else hi + 0.2 * atr_v
    return _make(snap, series, 'engulfing_reversal', side, stop,
                 _next_level(snap, c[-1], side), ev_list, against,
                 'a close beyond the engulfing extreme', expiry=8)


def pb_rejection_wick_reversal(snap, series):
    """A pin bar: the wick toward the move is >= 2x the body and >= 60% of the
    range, the close in the far third - at structure, after a move into it."""
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    rng = h[-1] - l[-1]
    if rng < 0.6 * atr_v:
        return []
    body = abs(c[-1] - o[-1])
    lower = min(o[-1], c[-1]) - l[-1]
    upper = h[-1] - max(o[-1], c[-1])
    if lower >= 2 * body and lower >= 0.6 * rng and c[-1] >= l[-1] + 0.66 * rng:
        side = 'buy'
    elif upper >= 2 * body and upper >= 0.6 * rng and c[-1] <= h[-1] - 0.66 * rng:
        side = 'sell'
    else:
        return []
    if not _move_into(c, side, atr_v, need=0.8):
        return []
    where = _at_support(snap, l[-1], h[-1], side, atr_v)
    if not where:
        return []
    ev_list = [_ev(f"rejection wick {(lower if side == 'buy' else upper) / atr_v:.1f} ATR long", 18, 'event'),
               _ev(f"at {where}", 14, 'structure')]
    against = []
    _with_htf(snap, side, ev_list, against)
    stop = l[-1] - 0.2 * atr_v if side == 'buy' else h[-1] + 0.2 * atr_v
    return _make(snap, series, 'rejection_wick_reversal', side, stop,
                 _next_level(snap, c[-1], side), ev_list, against,
                 'a close beyond the wick', expiry=8)


def pb_three_candle_reversal(snap, series):
    """Morning / evening star: a strong candle, a small indecision candle, then
    a strong candle the other way closing past the first one's midpoint."""
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    b1 = c[-3] - o[-3]
    b2 = abs(c[-2] - o[-2])
    b3 = c[-1] - o[-1]
    m1 = (o[-3] + c[-3]) / 2
    if b1 <= -0.8 * atr_v and b2 <= 0.35 * abs(b1) and b3 > 0 and c[-1] > m1:
        side = 'buy'
    elif b1 >= 0.8 * atr_v and b2 <= 0.35 * abs(b1) and b3 < 0 and c[-1] < m1:
        side = 'sell'
    else:
        return []
    lo, hi = l[-3:].min(), h[-3:].max()
    ev_list = [_ev(f"{'morning' if side == 'buy' else 'evening'} star", 20, 'event')]
    against = []
    where = _at_support(snap, lo, hi, side, atr_v)
    if where:
        ev_list.append(_ev(f"at {where}", 12, 'structure'))
    else:
        against.append('not at a known level or zone')
    _with_htf(snap, side, ev_list, against)
    stop = lo - 0.2 * atr_v if side == 'buy' else hi + 0.2 * atr_v
    return _make(snap, series, 'three_candle_reversal', side, stop,
                 _next_level(snap, c[-1], side), ev_list, against,
                 'a close beyond the pattern extreme', expiry=8)


def pb_double_top_bottom(snap, series):
    """A double top / bottom whose neckline was just closed through."""
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    for p in snap.get('patterns') or []:
        if p.get('kind') not in ('double_top', 'double_bottom'):
            continue
        lvl = float(p.get('break_level') or 0)
        if not lvl:
            continue
        top = p['kind'] == 'double_top'
        end = int(p.get('end_idx') or 0)
        n = c.size
        if not (0 < end < n - 1) or n - 1 - end > 30:
            continue
        past = c[end:n - 1]
        if (past < lvl).any() if top else (past > lvl).any():
            continue                                # not the FIRST close through
        if top and c[-1] < lvl and c[-2] >= lvl:
            side, stop = 'sell', float(p['invalidation']) + 0.2 * atr_v
        elif not top and c[-1] > lvl and c[-2] <= lvl:
            side, stop = 'buy', float(p['invalidation']) - 0.2 * atr_v
        else:
            continue
        ev_list = [_ev(f"{p.get('label', p['kind'])} - neckline {lvl:.2f} closed through "
                       f"(quality {p.get('quality')})", 24, 'pattern')]
        against = []
        _with_htf(snap, side, ev_list, against)
        return _make(snap, series, 'double_top_bottom', side, stop, p.get('target'),
                     ev_list, against, f"back through the neckline {lvl:.2f}", expiry=15)
    return []


def pb_divergence_reversal(snap, series):
    """Regular RSI divergence at the latest price extreme (within 5 bars), then
    a confirmation close beyond the previous bar's high / low."""
    o, h, l, c = _bars(series)
    n = c.size
    atr_v = snap['atr']
    dv = (snap.get('momentum') or {}).get('divergence') or {}
    if dv.get('kind') not in ('bullish_regular', 'bearish_regular'):
        return []
    j = int(((dv.get('pivots') or {}).get('to') or {}).get('idx', -99))
    if not (1 <= n - 1 - j <= 5):
        return []
    if dv['kind'] == 'bullish_regular' and c[-1] > h[-2]:
        side, ext = 'buy', l[j:].min()
    elif dv['kind'] == 'bearish_regular' and c[-1] < l[-2]:
        side, ext = 'sell', h[j:].max()
    else:
        return []
    ev_list = [_ev(f"{dv['kind'].replace('_', ' ')} RSI divergence", 20, 'momentum'),
               _ev("confirmation close past the previous bar", 12, 'event')]
    against = []
    _with_htf(snap, side, ev_list, against)
    stop = ext - STOP_PAD * atr_v if side == 'buy' else ext + STOP_PAD * atr_v
    return _make(snap, series, 'divergence_reversal', side, stop,
                 _next_level(snap, c[-1], side), ev_list, against,
                 f"a new extreme beyond {ext:.2f}")


# =========================================================================== #
# PULLBACK                                                                    #
# =========================================================================== #
def pb_trend_structure_pullback(snap, series):
    """Swing trend up (HH + HL): price pulls back to the last higher low and
    rejects it (mirror for a down trend)."""
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    d = _trend_dir(snap)
    if d is None:
        return []
    up = d == 'up'
    want = 'HL' if up else 'LH'
    sw = [s for s in snap.get('swings') or [] if s.get('label') == want]
    if not sw:
        return []
    s = sw[-1]
    lvl = s['price']
    if up:
        ok = l[-1] <= lvl + 0.5 * atr_v and l[-1] >= lvl - 0.3 * atr_v and c[-1] > lvl and _bull(o, c)
    else:
        ok = h[-1] >= lvl - 0.5 * atr_v and h[-1] <= lvl + 0.3 * atr_v and c[-1] < lvl and _bear(o, c)
    if not ok:
        return []
    side = 'buy' if up else 'sell'
    ext = [x for x in snap.get('swings') or [] if x.get('label') == ('HH' if up else 'LL')]
    ev_list = [_ev(f"trend {d}: pullback to the last {want} {lvl:.2f} held", 24, 'structure')]
    against = []
    _with_htf(snap, side, ev_list, against)
    stop = min(l[-1], lvl) - STOP_PAD * atr_v if up else max(h[-1], lvl) + STOP_PAD * atr_v
    return _make(snap, series, 'trend_structure_pullback', side, stop,
                 ext[-1]['price'] if ext else None, ev_list, against,
                 f"a close through the {want} {lvl:.2f} ends the trend")


def pb_bos_retest_pullback(snap, series):
    """A BOS 3-20 bars ago; price comes back to the broken level and holds."""
    o, h, l, c = _bars(series)
    n = c.size
    atr_v = snap['atr']
    br = [b for b in snap.get('breaks') or [] if b.get('kind') == 'BOS' and 3 <= n - 1 - int(b['idx']) <= 20]
    if not br:
        return []
    b = br[-1]
    up = b['direction'] == 'up'
    lvl = b['price']
    if up:
        ok = l[-1] <= lvl + 0.3 * atr_v and c[-1] > lvl and _bull(o, c)
    else:
        ok = h[-1] >= lvl - 0.3 * atr_v and c[-1] < lvl and _bear(o, c)
    if not ok:
        return []
    side = 'buy' if up else 'sell'
    i = int(b['idx'])
    ext = h[i:].max() if up else l[i:].min()
    ev_list = [_ev(f"BOS {b['direction']} {n - 1 - i} bars ago, retest of {lvl:.2f} held", 24, 'structure')]
    against = []
    _with_htf(snap, side, ev_list, against)
    stop = lvl - 0.5 * atr_v if up else lvl + 0.5 * atr_v
    return _make(snap, series, 'bos_retest_pullback', side, stop, ext, ev_list, against,
                 f"a close back through {lvl:.2f}")


def pb_internal_structure_pullback(snap, series):
    """Inside a swing trend, the pullback makes its own small (3-bar fractal)
    swings; a close through the pullback's last internal swing re-joins the trend."""
    o, h, l, c = _bars(series)
    n = c.size
    atr_v = snap['atr']
    d = _trend_dir(snap)
    if d is None:
        return []
    up = d == 'up'
    seg = slice(n - 15, n - 1)
    hh, ll = h[seg], l[seg]
    piv = None
    for i in range(len(hh) - 2, 0, -1):            # last internal swing against the trend
        if up and hh[i] > hh[i - 1] and hh[i] >= hh[i + 1]:
            piv = (n - 15 + i, hh[i])
            break
        if not up and ll[i] < ll[i - 1] and ll[i] <= ll[i + 1]:
            piv = (n - 15 + i, ll[i])
            break
    if piv is None:
        return []
    i, p = piv
    depth = (h[n - 15:n - 1].max() - l[i:n - 1].min()) if up else (h[i:n - 1].max() - l[n - 15:n - 1].min())
    if depth < 1.0 * atr_v:
        return []
    if up and c[-1] > p and c[-2] <= p:
        side, stop = 'buy', l[i:].min() - STOP_PAD * atr_v
    elif not up and c[-1] < p and c[-2] >= p:
        side, stop = 'sell', h[i:].max() + STOP_PAD * atr_v
    else:
        return []
    ev_list = [_ev(f"trend {d}; the pullback's internal swing {p:.2f} broken", 22, 'structure')]
    against = []
    _with_htf(snap, side, ev_list, against)
    tgt = h[n - 15:n - 1].max() if up else l[n - 15:n - 1].min()
    return _make(snap, series, 'internal_structure_pullback', side, stop, tgt, ev_list, against,
                 'a new pullback extreme', expiry=10)


def pb_fvg_pullback(snap, series):
    """After a BOS, price pulls back into the fair value gap the impulse left and
    rejects it."""
    return _zone_tap(snap, series, snap.get('fvg') or [], ('bullish', 'bearish'),
                     'fair value gap', 'fvg_pullback', 'BOS')


def pb_ob_pullback(snap, series):
    """After a BOS, price pulls back to the order block the BOS came from and rejects it."""
    return _zone_tap(snap, series, snap.get('order_blocks') or [], ('bullish', 'bearish'),
                     'order block', 'ob_pullback', 'BOS')


def pb_fibonacci_pullback(snap, series):
    """Swing trend; the pullback reaches the 61.8-78.6% pocket of the last leg and
    the bar rejects it. No higher-timeframe requirement."""
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    d = _trend_dir(snap)
    if d is None:
        return []
    fib = (snap.get('pullback') or {}).get(d) or {}
    if not fib.get('valid') or not fib.get('levels'):
        return []
    lv = fib['levels']
    a, b = lv.get('0.618'), lv.get('0.786')
    if a is None or b is None:
        return []
    lo_p, hi_p = min(a, b), max(a, b)
    up = d == 'up'
    if up:
        ok = l[-1] <= hi_p and l[-1] >= lo_p - 0.3 * atr_v and c[-1] > l[-1] + 0.5 * (h[-1] - l[-1]) and _bull(o, c)
        side, stop, tgt = 'buy', fib['leg_low'] - STOP_PAD * atr_v, fib['leg_high']
    else:
        ok = h[-1] >= lo_p and h[-1] <= hi_p + 0.3 * atr_v and c[-1] < h[-1] - 0.5 * (h[-1] - l[-1]) and _bear(o, c)
        side, stop, tgt = 'sell', fib['leg_high'] + STOP_PAD * atr_v, fib['leg_low']
    if not ok:
        return []
    ev_list = [_ev(f"pullback into the 61.8-78.6% pocket {lo_p:.2f}-{hi_p:.2f}", 22, 'fib'),
               _ev('rejection close', 10, 'event')]
    against = []
    _with_htf(snap, side, ev_list, against)
    return _make(snap, series, 'fibonacci_pullback', side, stop, tgt, ev_list, against,
                 'a close beyond the leg start')


def pb_ma_pullback(snap, series):
    """EMAs stacked and sloping with the trend; price dips to the fast EMA (not
    closing through the slow one) and closes back with the trend."""
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    if c.size < 120:
        return []
    ef = _ema(c, CONFIG.engine.ema_fast)
    es = _ema(c, CONFIG.engine.ema_slow)
    up = ef[-1] > es[-1] and ef[-1] > ef[-6] and es[-1] > es[-6]
    dn = ef[-1] < es[-1] and ef[-1] < ef[-6] and es[-1] < es[-6]
    if up and l[-1] <= ef[-1] and c[-1] > ef[-1] and c[-1] > es[-1] and _bull(o, c):
        side, stop = 'buy', min(l[-1], es[-1]) - 0.3 * atr_v
    elif dn and h[-1] >= ef[-1] and c[-1] < ef[-1] and c[-1] < es[-1] and _bear(o, c):
        side, stop = 'sell', max(h[-1], es[-1]) + 0.3 * atr_v
    else:
        return []
    ev_list = [_ev(f"EMAs stacked {'up' if side == 'buy' else 'down'}, dip to the fast EMA "
                   f"{ef[-1]:.2f} rejected", 20, 'trend')]
    against = []
    _with_htf(snap, side, ev_list, against)
    tgt = h[-20:].max() if side == 'buy' else l[-20:].min()
    return _make(snap, series, 'ma_pullback', side, stop, tgt, ev_list, against,
                 'a close through the slow EMA', expiry=10)


def pb_volatility_pullback(snap, series):
    """A quiet pullback inside a trend: the last 5 bars' range is under 70% of
    the 20 before it and the last bar is the narrowest of 4 (NR4); a close past
    that bar's high (low) re-joins the trend."""
    o, h, l, c = _bars(series)
    atr_v = snap['atr']
    d = _trend_dir(snap)
    if d is None or c.size < 30:
        return []
    rng = h - l
    if rng[-6:-1].mean() > 0.7 * rng[-26:-6].mean():
        return []
    nr = rng[-2]
    if nr > rng[-5:-2].min():
        return []
    if d == 'up' and c[-1] > h[-2] and _bull(o, c):
        side, stop = 'buy', l[-6:].min() - STOP_PAD * atr_v
    elif d == 'down' and c[-1] < l[-2] and _bear(o, c):
        side, stop = 'sell', h[-6:].max() + STOP_PAD * atr_v
    else:
        return []
    ev_list = [_ev(f"trend {d}; quiet pullback (bars at {rng[-6:-1].mean() / rng[-26:-6].mean() * 100:.0f}% "
                   f"of their earlier size), NR4 bar broken", 22, 'volatility')]
    against = []
    _with_htf(snap, side, ev_list, against)
    tgt = h[-26:].max() if side == 'buy' else l[-26:].min()
    return _make(snap, series, 'volatility_pullback', side, stop, tgt, ev_list, against,
                 'a new pullback extreme', expiry=8)


# --------------------------------------------------------------------------- #
# registry                                                                    #
# --------------------------------------------------------------------------- #
MORE = {
    'range_breakout': pb_range_breakout,
    'range_retest': pb_range_retest,
    'range_sweep': pb_range_sweep,
    'mid_range_rotation': pb_mid_range_rotation,
    'range_compression_breakout': pb_range_compression_breakout,
    'range_expansion': pb_range_expansion,
    'range_failure': pb_range_failure,
    'hz_resistance_breakout': pb_hz_resistance_breakout,
    'hz_support_breakdown': pb_hz_support_breakdown,
    'trendline_breakout': pb_trendline_breakout,
    'squeeze_breakout': pb_squeeze_breakout,
    'news_breakout': pb_news_breakout,
    'displacement_breakout': pb_displacement_breakout,
    'liquidity_sweep_breakout': pb_liquidity_sweep_breakout,
    'bos_breakout': pb_bos_breakout,
    'choch_breakout': pb_choch_breakout,
    'fvg_ob_breakout': pb_fvg_ob_breakout,
    'choch_reversal': pb_choch_reversal,
    'ob_reversal': pb_ob_reversal,
    'fvg_reversal': pb_fvg_reversal,
    'engulfing_reversal': pb_engulfing_reversal,
    'rejection_wick_reversal': pb_rejection_wick_reversal,
    'three_candle_reversal': pb_three_candle_reversal,
    'double_top_bottom': pb_double_top_bottom,
    'divergence_reversal': pb_divergence_reversal,
    'trend_structure_pullback': pb_trend_structure_pullback,
    'bos_retest_pullback': pb_bos_retest_pullback,
    'internal_structure_pullback': pb_internal_structure_pullback,
    'fvg_pullback': pb_fvg_pullback,
    'ob_pullback': pb_ob_pullback,
    'fibonacci_pullback': pb_fibonacci_pullback,
    'ma_pullback': pb_ma_pullback,
    'volatility_pullback': pb_volatility_pullback,
}

# Each family's HOME regimes (2026-11-12) - where qualify.py's regime gate
# passes it; anywhere else it warns (-12 confidence), as it does for the
# original playbooks through regime.py's table. Before this the taxonomy
# playbooks were in no regime's list, so they warned in EVERY regime.
GROUP_OF = {
    **{k: 'range' for k in ('range_breakout', 'range_retest', 'range_sweep', 'mid_range_rotation',
                            'range_compression_breakout', 'range_expansion', 'range_failure')},
    **{k: 'breakout' for k in ('hz_resistance_breakout', 'hz_support_breakdown', 'trendline_breakout',
                               'squeeze_breakout', 'news_breakout', 'displacement_breakout',
                               'liquidity_sweep_breakout', 'bos_breakout', 'choch_breakout',
                               'fvg_ob_breakout')},
    **{k: 'reversal' for k in ('choch_reversal', 'ob_reversal', 'fvg_reversal', 'engulfing_reversal',
                               'rejection_wick_reversal', 'three_candle_reversal',
                               'double_top_bottom', 'divergence_reversal')},
    **{k: 'pullback' for k in ('trend_structure_pullback', 'bos_retest_pullback',
                               'internal_structure_pullback', 'fvg_pullback', 'ob_pullback',
                               'fibonacci_pullback', 'ma_pullback', 'volatility_pullback')},
}
GROUP_HOMES = {
    'range': ('range', 'squeeze'),        # inside a range, or the coil before it breaks
    'breakout': ('trend', 'squeeze'),     # with a trend, or out of a squeeze
    'reversal': ('range', 'transition'),  # where a move is losing control
    'pullback': ('trend',),               # the trend is intact
}

MORE_LABELS = {k: k.replace('_', ' ').upper()
               .replace('HZ ', 'HORIZONTAL ').replace('OB ', 'ORDER BLOCK ')
               .replace('FVG OB', 'FVG / OB').replace('BOS', 'BOS').replace('CHOCH', 'CHoCH')
               for k in MORE}
