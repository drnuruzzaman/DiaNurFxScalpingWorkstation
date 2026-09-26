"""
server/forecast/reads.py - the live engine's reads, for every historical bar.

What the chart shows at a bar - the regime, the leg in progress, each MTF
rung's trend - comes from analyse() on the last WINDOW bars and quick_trend()
on the last QT_WINDOW bars. The forecast conditions on exactly those reads,
so they are computed here the same way: the SAME engine functions on the SAME
windows, one bar at a time. No vectorised re-implementation that could drift
from what the chart says; the price is compute time, paid once and cached.

    regime_read(h, l, c, t)   the analyse() sub-pipeline that decides the
                              regime and the leg - checked against analyse()
                              itself in tools/test_forecast.py
    build_year(...)           every bar of one timeframe-year -> arrays
    load(symbol, tf)          all cached years, concatenated

The cache key holds the engine source hash and settings (engine_hash()), the
feature version and the size/mtime of every year file read, so an engine edit
or a fresh download rebuilds only what it touches.
"""
from __future__ import annotations

import json
import time

import numpy as np

from ..config import CONFIG, DATA_DIR, TF_SECONDS
from ..datafeed import available_years, load_disk
from ..engine import legs as lg
from ..engine import regime as rg
from ..engine import structure as st
from ..engine import trendlines as tl
from ..engine.analysis import quick_trend
from ..engine.indicators import atr, ema, last_valid
from . import (CACHE_DIR, FEATURE_VERSION, QT_WINDOW, TFS, WINDOW, engine_hash, ms_of,
               sync_engine_settings)

# Encodings. The forecast's state space is the chart's regime LABEL.
STATES = ('UPTREND', 'DOWNTREND', 'RANGE', 'TRANSITION', 'SQUEEZE')
_STATE_CODE = {s: i for i, s in enumerate(STATES)}          # UNKNOWN -> -1
VOLS = ('very_low', 'low', 'normal', 'high', 'extreme')
_VOL_CODE = {s: i for i, s in enumerate(VOLS)}
QT_MISSING = -9
_QT_STRENGTH = {'HIGH': 3, 'NORMAL': 2, 'WEAK': 1}

# Per-bar float and int fields a read produces (NaN / -1 when unavailable).
FLOATS = ('atr', 'conf', 's_trend', 's_range', 's_transition', 's_squeeze', 'adx', 'er',
          'slope_atr', 'dir_share', 'atr_pct', 'bbw_pct', 'leg_ext', 'leg_pull',
          'leg_depth', 'leg_age', 'ext_age')
INTS = ('state', 'vol', 'squeeze', 'expanding', 'contracting', 'leg_dir', 'qt')


def qt_code(read: dict | None) -> int:
    """quick_trend() / trend_state() -> signed strength: +3 strong up .. -3, 0 ranging."""
    if not read:
        return QT_MISSING
    s = str(read.get('state') or '')
    m = _QT_STRENGTH.get(read.get('strength'), 2)
    return m if 'up' in s else -m if 'down' in s else 0


def regime_read(h, l, c, t) -> tuple:
    """
    (regime, leg, atr) on one window, computed exactly as analyse() computes
    them. Kept line for line with server/engine/analysis.py; a parity test
    runs analyse() on sampled windows and requires identical output.
    """
    cfg = CONFIG.engine
    a = atr(h, l, c, cfg.atr_period)
    atr_now = last_valid(a, 0.0)
    if c.size < 60 or atr_now <= 0:
        return None, None, 0.0
    ema_f = ema(c, cfg.ema_fast)
    ema_s = ema(c, cfg.ema_slow)
    swings = st.find_swings(h, l, c, t, cfg.swing_fractal_k, cfg.swing_atr_mult, a,
                            cfg.swing_max)
    breaks = st.structure_breaks(swings, h, l, c, t)
    trend = st.trend_state(swings, breaks, c, ema_f, ema_s)
    geo, _ = st.structural_pool(swings, c.size, cfg.tl_structural_swings,
                                cfg.tl_lookback_min_bars, cfg.tl_lookback_max_bars)
    lines = tl.find_trendlines(h, l, c, t, geo, atr_now, cfg.tl_min_touches,
                               cfg.tl_max_violation_atr, cfg.tl_max)
    channels = tl.find_channels(h, l, c, t, lines, atr_now, cfg.channel_min_containment)
    regime = rg.classify(h, l, c, t, swings, trend, channels, a, cfg.regime_lookback)
    atr_leg = a if cfg.atr_period == lg.LEG_ATR_PERIOD else atr(h, l, c, lg.LEG_ATR_PERIOD)
    leg = lg.leg_state(h, l, c, atr_leg, t)
    if leg is not None:
        # Bars since the leg's starting pivot and since its extreme.
        leg['age'] = c.size - 1 - int(np.searchsorted(t, leg['start_ms']))
        leg['ext_age'] = c.size - 1 - int(np.searchsorted(t, leg['extreme_ms']))
    return regime, leg, atr_now


def _empty(n: int) -> dict:
    out = {k: np.full(n, np.nan, dtype=np.float32) for k in FLOATS}
    out.update({k: np.full(n, -1, dtype=np.int8) for k in INTS})
    out['qt'][:] = QT_MISSING
    out['leg_dir'][:] = 0
    return out


def _fill(out: dict, j: int, regime: dict | None, leg: dict | None, atr_now: float) -> None:
    if atr_now > 0:
        out['atr'][j] = atr_now
    if regime and regime.get('state') != 'unknown':
        out['state'][j] = _STATE_CODE.get(regime['label'], -1)
        out['conf'][j] = regime['confidence']
        sc = regime['scores']
        out['s_trend'][j], out['s_range'][j] = sc['trend'], sc['range']
        out['s_transition'][j], out['s_squeeze'][j] = sc['transition'], sc['squeeze']
        out['adx'][j] = regime['adx']
        out['er'][j] = regime['efficiency_ratio']
        out['slope_atr'][j] = regime['slope_atr_per_bar']
        out['dir_share'][j] = regime['directional_share']
        v = regime['volatility']
        out['vol'][j] = _VOL_CODE.get(v['state'], -1)
        out['atr_pct'][j] = v['atr_percentile']
        out['bbw_pct'][j] = v['bb_width_percentile']
        out['squeeze'][j] = int(bool(v['squeeze']))
        out['expanding'][j] = int(bool(v['expanding']))
        out['contracting'][j] = int(bool(v['contracting']))
    if leg:
        out['leg_dir'][j] = int(leg['dir'])
        out['leg_ext'][j] = leg['ext_atr']
        out['leg_pull'][j] = leg['pull_atr']
        out['leg_depth'][j] = leg['depth'] if leg.get('depth') is not None else np.nan
        out['leg_age'][j] = leg.get('age', np.nan)
        out['ext_age'][j] = leg.get('ext_age', np.nan)


# --------------------------------------------------------------------------- #
# the cache                                                                   #
# --------------------------------------------------------------------------- #
def _warm_years(tf: str) -> int:
    """Year files needed before a year to fill a WINDOW-bar window on its first bar."""
    bars_per_year = 365 * 86400 / TF_SECONDS[tf] * 5 / 7 * 0.95
    return int(np.ceil(WINDOW * 1.2 / bars_per_year)) + 1


def _files_key(symbol: str, tf: str, years: list) -> list:
    out = []
    for y in years:
        p = DATA_DIR / symbol / tf / f'{y}.csv.gz'
        if p.exists():
            s = p.stat()
            out.append([y, s.st_size, int(s.st_mtime)])
    return out


def _key(symbol: str, tf: str, year: int) -> dict:
    years = [y for y in range(year - _warm_years(tf), year + 1)
             if y in set(available_years(symbol, tf))]
    return {'v': FEATURE_VERSION, 'engine': engine_hash(), 'symbol': symbol, 'tf': tf,
            'year': year, 'files': _files_key(symbol, tf, years),
            'window': WINDOW, 'qt_window': QT_WINDOW, 'regime': tf in TFS}


def _paths(symbol: str, tf: str, year: int):
    d = CACHE_DIR / symbol / tf
    return d / f'reads_{year}.npz', d / f'reads_{year}.json'


def is_fresh(symbol: str, tf: str, year: int) -> bool:
    npz, meta = _paths(symbol, tf, year)
    if not (npz.exists() and meta.exists()):
        return False
    try:
        return json.loads(meta.read_text(encoding='utf-8')) == _key(symbol, tf, year)
    except (OSError, ValueError):
        return False


def build_year(symbol: str, tf: str, year: int, progress=None) -> dict:
    """
    Every bar of `tf` whose (broker-time) open falls in `year`: the regime and
    leg read on the WINDOW bars ending at it (forecast timeframes only) and
    the quick_trend() read on the QT_WINDOW bars ending at it. Writes the
    cache and returns a small summary. Runs in a worker process. `progress`,
    if given, is told the share done (0..1) about every 2% of the bars.
    """
    t0 = time.perf_counter()
    sync_engine_settings()                 # compute with the settings live runs with
    key = _key(symbol, tf, year)
    years = [f[0] for f in key['files']]
    s = load_disk(symbol, tf, years)
    first = int(np.searchsorted(s.t, ms_of(f'{year}-01-01'), 'left'))
    last = int(np.searchsorted(s.t, ms_of(f'{year + 1}-01-01'), 'left'))
    n = last - first
    out = _empty(n)
    out['t'] = s.t[first:last].astype(np.int64)
    h, l, c, t = s.h, s.l, s.c, s.t
    every = max(1, n // 50)
    for j, k in enumerate(range(first, last)):
        if progress is not None and j % every == 0:
            progress(j / max(1, n))
        lo = k + 1 - WINDOW
        if key['regime'] and lo >= 0:
            regime, leg, atr_now = regime_read(h[lo:k + 1], l[lo:k + 1], c[lo:k + 1],
                                               t[lo:k + 1])
            _fill(out, j, regime, leg, atr_now)
        qlo = max(0, k + 1 - QT_WINDOW)
        if k + 1 - qlo >= 60:
            out['qt'][j] = qt_code(quick_trend(s.slice(qlo, k + 1)))
    npz, meta = _paths(symbol, tf, year)
    npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(npz, **out)
    meta.write_text(json.dumps(key), encoding='utf-8')
    return {'symbol': symbol, 'tf': tf, 'year': year, 'bars': n,
            'seconds': round(time.perf_counter() - t0, 1)}


def years_for(symbol: str, tf: str, last_year: int = None) -> list:
    """
    The years the cache covers for `tf`: from TRAIN_START for the 5m base, and
    from the year before it for every timeframe that also serves as a higher
    MTF rung (so the first training bars find a closed rung to read).
    """
    from datetime import datetime, timezone

    from . import TRAIN_START
    last = last_year or datetime.now(timezone.utc).year
    y0 = int(TRAIN_START[:4]) - (0 if tf == '5m' else 1)
    have = set(available_years(symbol, tf))
    return [y for y in range(y0, last + 1) if y in have]


def aligned(t, rd: dict, field: str, fill) -> np.ndarray:
    """rd[field] laid onto the bar timestamps `t` (bars without a read get `fill`)."""
    t = np.asarray(t, dtype=np.int64)
    src = rd[field]
    out = np.full(t.size, fill, dtype=src.dtype)
    idx = np.searchsorted(t, rd['t'])
    ok = (idx < t.size) & (t[np.minimum(idx, t.size - 1)] == rd['t'])
    if not ok.all():
        raise ValueError(f'{int((~ok).sum())} reads have no matching bar')
    out[idx] = src
    return out


def load(symbol: str, tf: str, years: list) -> dict:
    """Cached reads for `years`, concatenated in time order. Missing -> KeyError."""
    parts = []
    for y in years:
        npz, _ = _paths(symbol, tf, y)
        if not is_fresh(symbol, tf, y):
            raise KeyError(f'reads cache for {symbol} {tf} {y} is missing or stale - '
                           f'run tools/forecast_build.py')
        with np.load(npz) as z:
            parts.append({k: z[k] for k in z.files})
    if not parts:
        return {}
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}


__all__ = ['STATES', 'VOLS', 'QT_MISSING', 'FLOATS', 'INTS', 'qt_code', 'regime_read',
           'build_year', 'is_fresh', 'years_for', 'aligned', 'load']
