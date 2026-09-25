"""
server/lab/data.py - history for the lab, from disk and nowhere else.

The lab has no bridge. Everything it replays is data/<symbol>/<tf>/<year>.csv.gz,
the same files every backtest and study in this repo reads, so a lab session
and a research script looking at the same week see the same candles.
"""
from __future__ import annotations

import json
import math
import threading

import numpy as np

from ..config import TF_SECONDS
from ..datafeed import (DATA_DIR, available_symbols, available_timeframes, available_years,
                        load_disk)
from . import settings as lab_settings

_COVER: dict = {}
_LOCK = threading.Lock()


def symbols() -> list:
    """Instruments the lab can replay: they need 1m bars for the fill path."""
    return [s for s in available_symbols() if '1m' in available_timeframes(s)]


def coverage(symbol: str) -> dict:
    """{tf: {'first_ms', 'last_ms'}} per timeframe on disk. Cached per process."""
    with _LOCK:
        hit = _COVER.get(symbol)
    if hit is not None:
        return hit
    out = {}
    for tf in available_timeframes(symbol):
        years = available_years(symbol, tf)
        if not years:
            continue
        first = load_disk(symbol, tf, [years[0]])
        last = first if len(years) == 1 else load_disk(symbol, tf, [years[-1]])
        if len(first) and len(last):
            out[tf] = {'first_ms': int(first.t[0]), 'last_ms': int(last.t[-1])}
    with _LOCK:
        _COVER[symbol] = out
    return out


def spec(symbol: str) -> dict:
    """
    Contract facts for P&L: data/<symbol>/spec.json when the live app has
    saved one from MT5, otherwise the configured instrument defaults. Which
    one was used is reported, so a P&L on defaults is never mistaken for a
    broker-accurate one.
    """
    base = lab_settings._PRISTINE['instrument']
    out = {
        'symbol': symbol, 'digits': base.digits, 'point': base.point,
        'tick_size': base.tick_size, 'tick_value': base.tick_value,
        'contract_size': base.contract_size, 'volume_min': base.volume_min,
        'volume_max': base.volume_max, 'volume_step': base.volume_step,
        'stops_level_points': base.stops_level_points,
        'commission_per_lot_side': base.commission_per_lot_side,
        'swap_free': base.swap_free, 'source': 'defaults',
    }
    path = DATA_DIR / symbol / 'spec.json'
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        for k in ('digits', 'point', 'tick_size', 'tick_value', 'contract_size',
                  'volume_min', 'volume_max', 'volume_step', 'stops_level_points',
                  'commission_per_lot_side'):
            if saved.get(k) is not None:
                out[k] = saved[k]
        out['source'] = 'mt5 (saved ' + str(saved.get('saved_at', '')) + ')'
    except (OSError, ValueError):
        pass
    return out


def _years_for(tf: str, start_ms: int, end_ms: int, warm_bars: int) -> list:
    """Year files covering [start - warm_bars of this tf, end]."""
    import datetime as dt
    tf_s = TF_SECONDS.get(tf, 300)
    # Markets trade ~5 days in 7 and gold ~23h a day: pad the calendar span.
    warm_days = math.ceil(warm_bars * tf_s / 86400 * 1.6) + 4
    y0 = dt.datetime.fromtimestamp((start_ms / 1000) - warm_days * 86400, dt.timezone.utc).year
    y1 = dt.datetime.fromtimestamp(end_ms / 1000, dt.timezone.utc).year
    return list(range(y0, y1 + 1))


def load(symbol: str, tf: str, start_ms: int, end_ms: int, warm_bars: int):
    """
    Bars of `tf` from `warm_bars` before start_ms through end_ms, and the
    index of the first bar at or after start_ms.
    """
    years = [y for y in _years_for(tf, start_ms, end_ms, warm_bars)
             if y in set(available_years(symbol, tf))]
    s = load_disk(symbol, tf, years, None, end_ms)
    if not len(s):
        return s, 0
    i0 = int(np.searchsorted(s.t, start_ms, 'left'))
    lo = max(0, i0 - warm_bars)
    return s.slice(lo), i0 - lo


__all__ = ['symbols', 'coverage', 'spec', 'load']
