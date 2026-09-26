"""
server/forecast/labels.py - what actually happened next.

Two label sets, both indexed by the CLOSED bar a forecast is made at, and
both stamped with the moment they became known (`resolve_ms`), which is what
keeps every baseline and table honest: nothing may learn from an outcome
before its resolve time.

MARKET labels, per forecast timeframe, horizon HORIZON[tf] of its own bars,
in ATR of the forecast bar (the reads' window ATR, as the chart shows it):
    up[k], dn[k]   highest high / lowest low over the next k bars, from the close
    ret[k]         close k bars later minus the close (its sign is direction)
    state_h        the chart's regime label k=H bars later (reads.STATES code)

TRADE labels, per decision timeframe (TRADE_TFS), walked on the M1 path for
TRADE_HORIZON one-minute bars, exactly as the lab broker fills them:
    - the close is acted on at the NEXT minute's open (bid) - or at the bar's
      own close when that minute is more than a bar away (daily break,
      weekend) - and entries pay the decision bar's spread and the lab's
      market slippage
    - inside a minute price walks open -> low -> high -> close for an up (or
      flat) minute and open -> high -> low -> close for a down one; a level
      the open is already beyond fills at the open
    - buy exits trigger on the bid, sell exits on the ask (bid + that
      minute's spread)
  Stored:
    tpl[t, i]      outcome of TEMPLATES[i]: +1 target first, -1 stop first,
                   0 neither inside the horizon - spread and path rule exact
    up_tau/dn_tau  first-touch time of each LEVELS grid level on the bid path
                   from the entry bid (no costs), for MFE/MAE and any
                   SL/TP pair; minute*4 + phase (0 at the open, 1 first
                   extreme, 2 second), NEVER when not reached
    mfe/mae        buy and sell, over the whole horizon, costs included, ATR
    raw_up/raw_dn  highest high / lowest low over the horizon from the entry
                   bid, no costs, ATR - MFE/MAE at ANY cost follow exactly
    tpl_cf[t,i,k]  TEMPLATES[i] replayed with a constant spread of
                   COST_LEVELS[k] ATR (entry and exits) - the baseline's
                   "what did this path do at today's cost"

Every array is cached under runs/forecast/cache/, keyed by the data files,
the reads it depends on, and LABEL_VERSION.
"""
from __future__ import annotations

import json

import numpy as np

from ..config import DATA_DIR, TF_SECONDS
from ..datafeed import available_years, load_disk
from . import (CACHE_DIR, COST_LEVELS, HORIZON, LABEL_VERSION, LEVELS, TEMPLATES,
               TRADE_HORIZON, TRAIN_START, engine_hash, ms_of, reads)

NEVER = np.uint16(0xFFFF)
SLIP_POINTS = 3.0          # lab default: instrument.default_slippage_points
DEFAULT_SPREAD = 20.0      # the lab's fallback when a bar records no spread (points)
CHUNK = 4000


# --------------------------------------------------------------------------- #
# cache plumbing                                                              #
# --------------------------------------------------------------------------- #
def _files(symbol: str, tf: str) -> list:
    out = []
    for y in available_years(symbol, tf):
        if y < int(TRAIN_START[:4]) - 1:
            continue
        p = DATA_DIR / symbol / tf / f'{y}.csv.gz'
        s = p.stat()
        out.append([y, s.st_size, int(s.st_mtime)])
    return out


def _years(symbol: str, tf: str) -> list:
    return [f[0] for f in _files(symbol, tf)]


def _cached(name: str, symbol: str, tf: str, key: dict, rebuild: bool):
    d = CACHE_DIR / symbol / tf
    npz, meta = d / f'{name}.npz', d / f'{name}.json'
    if not rebuild and npz.exists() and meta.exists():
        try:
            if json.loads(meta.read_text(encoding='utf-8')) == key:
                with np.load(npz) as z:
                    return {k: z[k] for k in z.files}, npz, meta
        except (OSError, ValueError):
            pass
    return None, npz, meta


def _save(npz, meta, key: dict, out: dict) -> None:
    npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(npz, **out)
    meta.write_text(json.dumps(key), encoding='utf-8')


# --------------------------------------------------------------------------- #
# market labels                                                               #
# --------------------------------------------------------------------------- #
def market(symbol: str, tf: str, rebuild: bool = False, info: bool = False):
    """Market labels for every bar of `tf` from TRAIN_START (see module doc)."""
    years = _years(symbol, tf)
    key = {'v': LABEL_VERSION, 'kind': 'market', 'symbol': symbol, 'tf': tf,
           'h': HORIZON[tf], 'files': _files(symbol, tf), 'engine': engine_hash()}
    hit, npz, meta = _cached('labels_market', symbol, tf, key, rebuild)
    if hit is not None:
        return (hit, {'rows': int(hit['t'].size), 'cached': True}) if info else hit
    bars = load_disk(symbol, tf, years)
    rd = reads.load(symbol, tf, reads.years_for(symbol, tf))
    out = market_arrays(bars, reads.aligned(bars.t, rd, 'atr', np.nan),
                        reads.aligned(bars.t, rd, 'state', -1), tf, ms_of(TRAIN_START))
    _save(npz, meta, key, out)
    return (out, {'rows': int(out['t'].size), 'cached': False}) if info else out


def market_arrays(s, atr_arr, state_arr, tf: str, start_ms: int) -> dict:
    """
    The market labels from bars `s` and, aligned to them, the ATR and regime
    state the reads give each bar. Pure: tested directly on synthetic bars.
    """
    H = HORIZON[tf]
    tf_ms = TF_SECONDS[tf] * 1000
    n = len(s)
    rows = np.nonzero(s.t >= start_ms)[0]
    a = np.asarray(atr_arr, dtype=np.float64)
    c0 = s.c[rows]
    atr0 = a[rows]
    up = np.full((rows.size, H), np.nan, dtype=np.float32)
    dn = np.full((rows.size, H), np.nan, dtype=np.float32)
    ret = np.full((rows.size, H), np.nan, dtype=np.float32)
    run_hi = np.full(rows.size, -np.inf)
    run_lo = np.full(rows.size, np.inf)
    with np.errstate(invalid='ignore', divide='ignore'):
        for k in range(1, H + 1):
            j = rows + k
            ok = j < n
            jj = np.minimum(j, n - 1)
            run_hi = np.where(ok, np.maximum(run_hi, s.h[jj]), np.nan)
            run_lo = np.where(ok, np.minimum(run_lo, s.l[jj]), np.nan)
            up[:, k - 1] = (run_hi - c0) / atr0
            dn[:, k - 1] = (c0 - run_lo) / atr0
            ret[:, k - 1] = np.where(ok, (s.c[jj] - c0) / atr0, np.nan)
    jH = rows + H
    complete = (jH < n) & (atr0 > 0)
    jHc = np.minimum(jH, n - 1)
    resolve = np.where(complete, s.t[jHc].astype(np.int64) + tf_ms, np.iinfo(np.int64).max)
    state_h = np.where(complete, np.asarray(state_arr)[jHc], -1).astype(np.int8)
    close_ms = s.t[rows].astype(np.int64) + tf_ms
    return {
        't': s.t[rows].astype(np.int64),
        'close_ms': close_ms,
        'resolve_ms': resolve.astype(np.int64),
        'span_min': np.where(complete, (resolve - close_ms) // 60000, -1).astype(np.int32),
        'complete': complete,
        'up': up, 'dn': dn, 'ret': ret,
        'state_h': state_h,
    }


# --------------------------------------------------------------------------- #
# trade labels                                                                #
# --------------------------------------------------------------------------- #
def first_touch(O, H, L, C, level, up: bool) -> np.ndarray:
    """
    First-touch time of `level` on each row's minute path: minute*4 + phase,
    NEVER when not reached. `level` is (D, 1) or (D, W) - per-minute levels
    are how sell exits ride the ask. Phase follows the lab broker exactly:
    0 = the open is already beyond it; an up (or flat) minute walks
    open -> low -> high -> close, so its low is phase 1 and its high phase 2;
    a down minute walks open -> high -> low -> close, the reverse.
    """
    hit = (H >= level) if up else (L <= level)
    any_ = hit.any(axis=1)
    j = hit.argmax(axis=1)
    r = np.arange(O.shape[0])
    lv = level[r, j] if level.shape[1] > 1 else level[:, 0]
    gap = (O[r, j] >= lv) if up else (O[r, j] <= lv)
    upmin = C[r, j] >= O[r, j]
    phase = np.where(gap, 0, np.where(upmin, 2, 1) if up else np.where(upmin, 1, 2))
    return np.where(any_, j * 4 + phase, NEVER).astype(np.uint16)


def trade(symbol: str, tf: str, rebuild: bool = False, info: bool = False):
    """Trade labels for every `tf` decision bar from TRAIN_START (see module doc)."""
    years = _years(symbol, tf)
    key = {'v': LABEL_VERSION, 'kind': 'trade', 'symbol': symbol, 'tf': tf,
           'horizon': TRADE_HORIZON, 'levels': list(LEVELS),
           'templates': [list(x) for x in TEMPLATES], 'slip': SLIP_POINTS,
           'cost_levels': list(COST_LEVELS),
           'files': _files(symbol, tf), 'm1': _files(symbol, '1m'), 'engine': engine_hash()}
    hit, npz, meta = _cached('labels_trade', symbol, tf, key, rebuild)
    if hit is not None:
        return (hit, {'rows': int(hit['t'].size), 'cached': True}) if info else hit
    from ..lab.data import spec as lab_spec
    sp = lab_spec(symbol)
    point = float(sp.get('point') or 0.01)
    digits = int(sp.get('digits') or 2)
    s = load_disk(symbol, tf, years)
    rd = reads.load(symbol, tf, reads.years_for(symbol, tf))
    m1 = load_disk(symbol, '1m', _years(symbol, '1m'))
    out = trade_arrays(s, reads.aligned(s.t, rd, 'atr', np.nan), m1, tf,
                       ms_of(TRAIN_START), point, digits=digits)
    _save(npz, meta, key, out)
    return (out, {'rows': int(out['t'].size), 'cached': False}) if info else out


def trade_arrays(s, atr_arr, m1, tf: str, start_ms: int, point: float,
                 horizon: int = TRADE_HORIZON, slip_points: float = SLIP_POINTS,
                 digits: int = 2) -> dict:
    """
    The trade labels from decision bars `s`, their ATR, and M1 bars. Pure.
    Template stops and targets are rounded to the symbol's digits, as the
    executor sends them and the lab broker stores them.
    """
    tf_ms = TF_SECONDS[tf] * 1000
    t1 = m1.t.astype(np.int64)
    n1 = t1.size
    rows = np.nonzero((s.t >= start_ms) & (atr_arr > 0))[0]
    close_ms = s.t[rows].astype(np.int64) + tf_ms
    idx0 = np.searchsorted(t1, close_ms, 'left')
    # A decision closed before the first minute on disk has no path to walk -
    # searchsorted would hand it that first minute, years later for a symbol
    # whose 1m history starts after its other timeframes'.
    first_m1 = int(t1[0]) if n1 else np.iinfo(np.int64).max
    complete = (idx0 + horizon <= n1) & (close_ms >= first_m1)
    rows, close_ms, idx0, complete = rows[complete], close_ms[complete], idx0[complete], \
        complete[complete]
    D = rows.size
    atr0 = atr_arr[rows].astype(np.float64)
    # The price the close is acted on, as the lab sets it: the next minute's
    # open - unless that minute is more than a bar away (the daily break, the
    # weekend), when it is the decision bar's own close.
    near = t1[np.minimum(idx0, n1 - 1)] < close_ms + tf_ms
    entry_bid = np.where(near, m1.o[np.minimum(idx0, n1 - 1)], s.c[rows]).astype(np.float64)
    sp_dec = s.spread[rows].astype(np.float64) if s.spread is not None else np.zeros(D)
    sp_dec = np.where(sp_dec > 0, sp_dec, DEFAULT_SPREAD)
    slip = slip_points * point
    L_n, T_n = len(LEVELS), len(TEMPLATES)
    out = {
        't': s.t[rows].astype(np.int64),
        'close_ms': close_ms,
        'resolve_ms': t1[idx0 + horizon - 1] + 60_000,
        'entry_bid': entry_bid,
        'spread_atr': (sp_dec * point / atr0).astype(np.float32),
        'tpl': np.zeros((D, T_n), dtype=np.int8),
        'up_tau': np.full((D, L_n), NEVER, dtype=np.uint16),
        'dn_tau': np.full((D, L_n), NEVER, dtype=np.uint16),
        'buy_mfe': np.zeros(D, dtype=np.float32), 'buy_mae': np.zeros(D, dtype=np.float32),
        'sell_mfe': np.zeros(D, dtype=np.float32), 'sell_mae': np.zeros(D, dtype=np.float32),
        'raw_up': np.zeros(D, dtype=np.float32), 'raw_dn': np.zeros(D, dtype=np.float32),
        'slip_atr': (slip / atr0).astype(np.float32),
        'tpl_cf': np.zeros((D, T_n, len(COST_LEVELS)), dtype=np.int8),
    }
    msp = m1.spread.astype(np.float64) if m1.spread is not None else np.zeros(n1)
    W = np.arange(horizon)
    for a in range(0, D, CHUNK):
        b = min(D, a + CHUNK)
        ix = idx0[a:b, None] + W[None, :]
        O, Hh, Ll, C = m1.o[ix], m1.h[ix], m1.l[ix], m1.c[ix]
        sp_min = msp[ix]
        sp_min = np.where(sp_min > 0, sp_min, sp_dec[a:b, None]) * point   # per-minute, price
        ref = entry_bid[a:b, None]                      # the bid the close is acted on
        at = atr0[a:b, None]
        # ---- the grid: bid path from the entry bid, no costs
        for li, g in enumerate(LEVELS):
            out['up_tau'][a:b, li] = first_touch(O, Hh, Ll, C, ref + g * at, True)
            out['dn_tau'][a:b, li] = first_touch(O, Hh, Ll, C, ref - g * at, False)
        # ---- exact templates
        e_buy = ref + sp_dec[a:b, None] * point + slip       # buy fills on the ask
        e_sell = ref - slip                                  # sell fills on the bid
        for ti, (side, sl_atr, tp_r) in enumerate(TEMPLATES):
            risk = sl_atr * at
            if side == 'buy':
                tp_px = np.round(e_buy + tp_r * risk, digits)
                sl_px = np.round(e_buy - risk, digits)
                tp = first_touch(O, Hh, Ll, C, tp_px, True)                     # bid >= tp
                sl = first_touch(O, Hh, Ll, C, sl_px, False)                    # bid <= sl
            else:
                tp_px = np.round(e_sell - tp_r * risk, digits)
                sl_px = np.round(e_sell + risk, digits)
                # Ask = bid + spread: ask <= tp  <=>  bid <= tp - spread (per minute)
                tp = first_touch(O, Hh, Ll, C, tp_px - sp_min, False)
                sl = first_touch(O, Hh, Ll, C, sl_px - sp_min, True)
            out['tpl'][a:b, ti] = np.where(tp < sl, 1, np.where(sl < tp, -1, 0))
            # The same trade replayed at each cost level: a constant spread of
            # c ATR on the entry and, for sells, on the exits (the ask path).
            for ci, c in enumerate(COST_LEVELS):
                spread = c * at
                if side == 'buy':
                    e = ref + spread + slip
                    tp = first_touch(O, Hh, Ll, C, np.round(e + tp_r * risk, digits), True)
                    sl = first_touch(O, Hh, Ll, C, np.round(e - risk, digits), False)
                else:
                    tp = first_touch(O, Hh, Ll, C,
                                     np.round(e_sell - tp_r * risk, digits) - spread, False)
                    sl = first_touch(O, Hh, Ll, C, np.round(e_sell + risk, digits) - spread, True)
                out['tpl_cf'][a:b, ti, ci] = np.where(tp < sl, 1, np.where(sl < tp, -1, 0))
        # ---- MFE / MAE over the whole horizon, costs included
        hi_bid, lo_bid = Hh.max(axis=1), Ll.min(axis=1)
        hi_ask, lo_ask = (Hh + sp_min).max(axis=1), (Ll + sp_min).min(axis=1)
        a0 = atr0[a:b]
        out['raw_up'][a:b] = (hi_bid - ref[:, 0]) / a0
        out['raw_dn'][a:b] = (ref[:, 0] - lo_bid) / a0
        out['buy_mfe'][a:b] = (hi_bid - e_buy[:, 0]) / a0
        out['buy_mae'][a:b] = (e_buy[:, 0] - lo_bid) / a0
        out['sell_mfe'][a:b] = (e_sell[:, 0] - lo_ask) / a0
        out['sell_mae'][a:b] = (hi_ask - e_sell[:, 0]) / a0
    return out


__all__ = ['NEVER', 'SLIP_POINTS', 'market', 'market_arrays', 'first_touch', 'trade',
           'trade_arrays']
