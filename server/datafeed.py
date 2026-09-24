"""
server/datafeed.py - every bar the system sees enters through here.

Two sources, one shape:

  * DISK   data/<symbol>/<tf>/<year>.csv.gz, the history you downloaded.
           Used for backtests, for indicator warm-up, and whenever MT5 is shut.
  * BRIDGE the read-only MT5 bridge on localhost, for live bars and quotes.

Both are normalised to a `Series`: parallel numpy arrays, never a DataFrame.
The analysis engine runs on every incoming bar during a backtest, and pandas
row access is roughly two orders of magnitude too slow for that; numpy slicing
of a preloaded array is effectively free.

A note on time. Disk timestamps are epoch SECONDS in broker server time as MT5
delivered them. Bridge timestamps are epoch MILLISECONDS already corrected to
UTC by the bridge. `Series.t` is always epoch MILLISECONDS, and `tz_offset_ms`
records what was subtracted so the UI can label the axis honestly.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import CONFIG, DATA_DIR, TF_SECONDS


# --------------------------------------------------------------------------- #
# the bar container                                                           #
# --------------------------------------------------------------------------- #
@dataclass
class Series:
    """OHLCV as parallel float arrays. `t` is epoch ms, ascending, deduplicated."""
    symbol: str
    tf: str
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    spread: np.ndarray = field(default=None)
    source: str = 'disk'
    tz_offset_ms: int = 0
    #: 'ok' | 'warming' (MT5 still downloading) | 'unknown_symbol' | 'unavailable'
    status: str = 'ok'

    def __len__(self) -> int:
        return int(self.t.size)

    def slice(self, start: int, stop: int = None) -> 'Series':
        """A window as a new Series. Views, not copies - this runs per bar."""
        stop = len(self) if stop is None else stop
        s = slice(max(0, start), stop)
        return Series(
            symbol=self.symbol, tf=self.tf,
            t=self.t[s], o=self.o[s], h=self.h[s],
            l=self.l[s], c=self.c[s], v=self.v[s],
            spread=None if self.spread is None else self.spread[s],
            source=self.source, tz_offset_ms=self.tz_offset_ms,
            status=self.status,
        )

    def tail(self, n: int) -> 'Series':
        return self.slice(max(0, len(self) - n))

    def last_price(self) -> float:
        return float(self.c[-1]) if len(self) else 0.0

    def to_payload(self, limit: int = None) -> list:
        """Compact list-of-lists for the wire: [t, o, h, l, c, v]."""
        s = self.tail(limit) if limit else self
        return [
            [int(s.t[i]), float(s.o[i]), float(s.h[i]),
             float(s.l[i]), float(s.c[i]), float(s.v[i])]
            for i in range(len(s))
        ]

    def empty(self) -> bool:
        return self.t.size == 0


def empty_series(symbol: str, tf: str) -> Series:
    z = np.zeros(0, dtype=np.float64)
    return Series(symbol=symbol, tf=tf, t=z.copy(), o=z.copy(), h=z.copy(),
                  l=z.copy(), c=z.copy(), v=z.copy())


# --------------------------------------------------------------------------- #
# disk history                                                                #
# --------------------------------------------------------------------------- #
_DISK_CACHE: dict = {}
_DISK_LOCK = threading.Lock()


def available_symbols() -> list:
    """
    Instruments with bars on disk.

    Directories starting with '_' or '.' are house-keeping, not instruments.
    Without this guard anything the app stores under data/ becomes a symbol:
    the news archive did exactly that and appeared in the watchlist.
    """
    if not DATA_DIR.exists():
        return []
    return sorted(p.name for p in DATA_DIR.iterdir()
                  if p.is_dir() and not p.name.startswith(('_', '.')))


def available_timeframes(symbol: str) -> list:
    base = DATA_DIR / symbol
    if not base.exists():
        return []
    found = [p.name for p in base.iterdir() if p.is_dir()]
    return [tf for tf in TF_SECONDS if tf in found]


def available_years(symbol: str, tf: str) -> list:
    base = DATA_DIR / symbol / tf
    if not base.exists():
        return []
    years = []
    for p in base.glob('*.csv.gz'):
        try:
            years.append(int(p.stem.replace('.csv', '')))
        except ValueError:
            continue
    return sorted(years)


def _read_year(path: Path) -> np.ndarray:
    """
    One year file -> an (N, 6) float array of [ts, o, h, l, c, v] plus spread.

    np.loadtxt and pandas.read_csv were both tried; a hand-rolled split is
    ~3x faster here because the files are small, uniform and already sorted,
    and it means one less import in the hot path of a multi-year backtest.
    """
    rows = []
    with gzip.open(path, 'rt') as fh:
        header = fh.readline()
        if not header.lower().startswith('ts'):
            fh.seek(0)
        for line in fh:
            parts = line.split(',')
            if len(parts) < 5:
                continue
            try:
                ts = float(parts[0])
                o = float(parts[1]); hi = float(parts[2])
                lo = float(parts[3]); cl = float(parts[4])
                tv = float(parts[5]) if len(parts) > 5 else 0.0
                rv = float(parts[6]) if len(parts) > 6 else 0.0
                sp = float(parts[7]) if len(parts) > 7 else 0.0
            except ValueError:
                continue
            rows.append((ts, o, hi, lo, cl, rv or tv, sp))
    if not rows:
        return np.zeros((0, 7), dtype=np.float64)
    return np.asarray(rows, dtype=np.float64)


def load_disk(symbol: str, tf: str, years: list = None,
              start_ms: int = None, end_ms: int = None) -> Series:
    """
    Bars from disk. Year files are cached in memory, so a backtest that sweeps a
    window repeatedly pays the gzip cost once.
    """
    want = years if years is not None else available_years(symbol, tf)
    if not want:
        return empty_series(symbol, tf)

    chunks = []
    for year in want:
        key = (symbol, tf, year)
        with _DISK_LOCK:
            arr = _DISK_CACHE.get(key)
        if arr is None:
            path = DATA_DIR / symbol / tf / f'{year}.csv.gz'
            if not path.exists():
                continue
            arr = _read_year(path)
            with _DISK_LOCK:
                _DISK_CACHE[key] = arr
        if arr.size:
            chunks.append(arr)

    if not chunks:
        return empty_series(symbol, tf)

    data = np.vstack(chunks)
    # Sort + dedupe: year boundaries occasionally repeat a bar, and a duplicate
    # timestamp turns every "bars since" calculation into quiet nonsense.
    order = np.argsort(data[:, 0], kind='stable')
    data = data[order]
    _, first = np.unique(data[:, 0], return_index=True)
    data = data[np.sort(first)]

    t_ms = data[:, 0] * 1000.0
    if start_ms is not None:
        data = data[t_ms >= start_ms]
        t_ms = data[:, 0] * 1000.0
    if end_ms is not None:
        data = data[t_ms <= end_ms]
        t_ms = data[:, 0] * 1000.0

    return Series(
        symbol=symbol, tf=tf,
        t=t_ms, o=data[:, 1], h=data[:, 2], l=data[:, 3],
        c=data[:, 4], v=data[:, 5], spread=data[:, 6],
        source='disk',
    )


def load_recent(symbol: str, tf: str, bars: int) -> Series:
    """
    The last N bars from disk, reading only as many year files as needed.

    Loading 28 years of M1 to look at the last 600 bars costs ~4 s and 2 GB;
    walking back year by year costs ~30 ms.
    """
    years = available_years(symbol, tf)
    if not years:
        return empty_series(symbol, tf)
    per_year_est = max(1, int(365 * 24 * 3600 / TF_SECONDS.get(tf, 60) * 0.72))
    need = max(1, int(np.ceil(bars / per_year_est)) + 1)
    picked = years[-need:]
    series = load_disk(symbol, tf, picked)
    return series.tail(bars)


def resample(series: Series, target_tf: str) -> Series:
    """
    Aggregate a fine series onto a coarser grid.

    Needed because the 1m file is the only one that reaches the present for some
    timeframes, and because backtests replay M1 while analysing M5/M15.
    """
    src = TF_SECONDS.get(series.tf)
    dst = TF_SECONDS.get(target_tf)
    if not src or not dst or dst <= src or series.empty():
        return series
    bucket = (series.t // (dst * 1000)).astype(np.int64)
    edges = np.flatnonzero(np.diff(bucket)) + 1
    starts = np.concatenate(([0], edges))
    stops = np.concatenate((edges, [len(series)]))

    # reduceat over the group starts, not a Python loop: aggregating a year of
    # M1 into M15 is 300k rows, and the loop version cost ~2 s per call, which a
    # backtest pays thousands of times.
    t = bucket[starts] * (dst * 1000.0)
    o = series.o[starts]
    h = np.maximum.reduceat(series.h, starts)
    lo = np.minimum.reduceat(series.l, starts)
    c = series.c[stops - 1]
    v = np.add.reduceat(series.v, starts)
    return Series(symbol=series.symbol, tf=target_tf, t=t, o=o, h=h, l=lo,
                  c=c, v=v, source=series.source + '/resampled',
                  tz_offset_ms=series.tz_offset_ms)


# --------------------------------------------------------------------------- #
# the MT5 bridge                                                              #
# --------------------------------------------------------------------------- #
class BridgeClient:
    """
    Thin HTTP client for bridge/mt5_bridge.py.

    Deliberately forgiving: the bridge is an optional component. Every call
    returns None rather than raising when the terminal is shut, because the
    workstation must stay fully usable on disk history alone.
    """

    def __init__(self, base_url: str = None, timeout: float = 4.0):
        self.base = (base_url or CONFIG.bridge_url).rstrip('/')
        self.timeout = timeout
        self._last_error = None
        self._last_ok_ms = 0

    def _get(self, path: str, **params):
        qs = '&'.join(f'{k}={urllib.parse.quote(str(v))}'
                      for k, v in params.items() if v is not None)
        url = f'{self.base}{path}' + (f'?{qs}' if qs else '')
        try:
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
            self._last_error = None
            self._last_ok_ms = int(time.time() * 1000)
            return payload
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            self._last_error = str(exc)
            return None

    # -- reads -------------------------------------------------------------- #
    def health(self):
        return self._get('/health')

    def account(self):
        return self._get('/account')

    # These two UNWRAP the bridge's envelope and hand back a bare list.
    # The bridge answers {"positions": [...]}, and every caller here treats
    # the result as a list - guarded with isinstance(x, list), which a dict
    # silently fails. That made the dock's POSITIONS tab permanently empty AND
    # pinned open_positions to 0 in the signal-gate context, so a gate meant
    # to cap concurrent exposure never saw a single open trade.
    def positions(self, strict: bool = False):
        """
        Open positions. With strict=True an unreachable bridge returns None
        instead of []: the executor must never read "the bridge did not
        answer" as "every position has closed".
        """
        payload = self._get('/positions')
        if payload is None or 'positions' not in payload:
            return None if strict else []
        return payload.get('positions') or []

    def orders(self, strict: bool = False):
        payload = self._get('/orders')
        if payload is None or 'orders' not in payload:
            return None if strict else []
        return payload.get('orders') or []

    def trade(self, path: str, **params):
        """
        Call an order endpoint and keep the bridge's answer even when it is a
        refusal. _get() swallows HTTP errors into None, which would turn "the
        demo guard refused this" into an indistinguishable silence.

        Returns (status_code, payload). status 0 means the bridge was not
        reached - or reached and the answer lost - so the caller must treat
        the outcome as UNKNOWN, not as failed.
        """
        qs = '&'.join(f'{k}={urllib.parse.quote(str(v))}'
                      for k, v in params.items() if v is not None)
        url = f'{self.base}{path}' + (f'?{qs}' if qs else '')
        try:
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode('utf-8'))
            except (ValueError, OSError):
                body = {'error': str(exc)}
            return exc.code, body
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            return 0, {'error': f'bridge unreachable: {exc}'}

    def deals(self, days: int = 7):
        return self._get('/deals', days=days)

    def spec(self, symbol: str):
        return self._get('/spec', symbol=symbol)

    def quotes(self, symbols):
        joined = ','.join(symbols) if not isinstance(symbols, str) else symbols
        return self._get('/quotes', symbols=joined)

    def calendar(self):
        return self._get('/calendar')

    def symbols(self):
        """Every instrument the terminal exposes, not just the ones on disk."""
        return self._get('/symbols')

    def bars(self, symbol: str, tf: str, count: int = 600) -> Series:
        payload = self._get('/bars', symbol=symbol, tf=tf, count=count)
        if not payload or not payload.get('bars'):
            out = empty_series(symbol, tf)
            # Carry WHY it is empty, so the UI can distinguish "MT5 is still
            # downloading this" from "no such instrument".
            out.status = (payload or {}).get('status') or 'unavailable'
            return out
        rows = payload['bars']
        n = len(rows)
        t = np.empty(n); o = np.empty(n); h = np.empty(n)
        lo = np.empty(n); c = np.empty(n); v = np.empty(n)
        for i, b in enumerate(rows):
            t[i] = b['t']; o[i] = b['o']; h[i] = b['h']
            lo[i] = b['l']; c[i] = b['c']; v[i] = b.get('v', 0) or 0
        return Series(symbol=payload.get('symbol', symbol), tf=tf, t=t, o=o,
                      h=h, l=lo, c=c, v=v, source='bridge',
                      status=payload.get('status') or 'ok')

    @property
    def online(self) -> bool:
        return self._last_error is None and self._last_ok_ms > 0

    @property
    def last_error(self):
        return self._last_error


BRIDGE = BridgeClient()


# --------------------------------------------------------------------------- #
# the unified feed                                                            #
# --------------------------------------------------------------------------- #
class Feed:
    """
    What the rest of the system asks for bars.

    Live mode prefers the bridge and falls back to disk, stitching the two so a
    chart never goes blank mid-session. Backtest mode reads disk only - the
    backtester must never be able to see a live bar by accident.
    """

    def __init__(self, bridge: BridgeClient = None):
        self.bridge = bridge or BRIDGE
        self._cache: dict = {}
        self._cache_at: dict = {}
        self._spec: dict = {}
        self._spec_at: dict = {}
        self._lock = threading.Lock()

    def bars(self, symbol: str, tf: str, count: int = 600,
             live: bool = True, max_age_s: float = 1.5) -> Series:
        key = (symbol, tf, count, live)
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            age = now - self._cache_at.get(key, 0)
        if cached is not None and age < max_age_s:
            return cached

        series = empty_series(symbol, tf)
        if live:
            series = self.bridge.bars(symbol, tf, count)
        live_status = series.status
        if series.empty():
            series = load_recent(symbol, tf, count)
            # The 1m file runs furthest into the present; if a coarse timeframe
            # stopped earlier, rebuild it rather than showing a stale chart.
            if series.empty() or _is_stale(series, tf):
                fine = load_recent(symbol, '1m', count * max(1, TF_SECONDS.get(tf, 60) // 60) + 500)
                if not fine.empty():
                    rebuilt = resample(fine, tf).tail(count)
                    if len(rebuilt) > len(series):
                        series = rebuilt
            if series.empty():
                # Nothing on disk either. The bridge's reason is the useful one.
                series.status = live_status

        with self._lock:
            self._cache[key] = series
            self._cache_at[key] = now
        return series

    def history(self, symbol: str, tf: str,
                start_ms: int = None, end_ms: int = None) -> Series:
        """Disk only. This is what the backtester uses."""
        years = None
        if start_ms or end_ms:
            import datetime as _dt
            lo_y = _dt.datetime.utcfromtimestamp((start_ms or 0) / 1000).year if start_ms else None
            hi_y = _dt.datetime.utcfromtimestamp((end_ms or 0) / 1000).year if end_ms else None
            allyears = available_years(symbol, tf)
            years = [y for y in allyears
                     if (lo_y is None or y >= lo_y) and (hi_y is None or y <= hi_y)]
        return load_disk(symbol, tf, years, start_ms, end_ms)

    def quote(self, symbol: str):
        q = self.bridge.quotes([symbol])
        # The bridge answers {"quotes": {symbol: {...}}}. Not unwrapping that
        # envelope made this return {symbol: {...}} - a dict with no bid or ask
        # - and the executor, finding no price, quietly sent nothing.
        if isinstance(q, dict) and isinstance(q.get('quotes'), dict):
            q = q['quotes']
        if q and symbol in q:
            return q[symbol]
        if q:
            return next(iter(q.values()))
        # No terminal: synthesise from the last disk close so sizing still works.
        s = self.bars(symbol, '1m', 2, live=False)
        if s.empty():
            return None
        px = float(s.c[-1])
        half = CONFIG.instrument.point * 12
        return {'symbol': symbol, 'bid': px - half, 'ask': px + half, 'last': px,
                'digits': CONFIG.instrument.digits, 'point': CONFIG.instrument.point,
                'time_ms': int(s.t[-1]), 'synthetic': True}

    # How long an instrument spec is trusted.
    #
    # Everything that matters here is a CONTRACT fact - digits, tick size and
    # value, lot step, the broker's minimum stop distance. Those do not change
    # while the market is open; a broker changing its contract size mid-session
    # is not a thing this cache needs to be fast about.
    #
    # It was being re-fetched from the bridge on every engine pass, which is
    # every socket frame, for every symbol on the board. Measured at 37-112ms a
    # call, that was the single largest recurring cost in the pass - larger
    # than the analysis it was feeding.
    #
    # The one field that does move is spread_points_now, and qualify.py reads
    # it only as a THIRD fallback, after the live quote and the bar's own
    # recorded spread. A minute-old value in that position is still better than
    # the "assume 20 points" default it would otherwise reach.
    SPEC_TTL_S = 60.0

    def spec(self, symbol: str) -> dict:
        now = time.time()
        with self._lock:
            hit = self._spec.get(symbol)
            if hit is not None and now - self._spec_at.get(symbol, 0) < self.SPEC_TTL_S:
                return hit
        out = self._spec_uncached(symbol)
        # Only a spec the BRIDGE answered is worth keeping. Caching the
        # defaults would pin a symbol to placeholder contract values for a
        # minute every time MetaTrader is slow to answer once.
        if out.get('source') == 'mt5':
            with self._lock:
                self._spec[symbol] = out
                self._spec_at[symbol] = now
        return out

    def _spec_uncached(self, symbol: str) -> dict:
        live = self.bridge.spec(symbol)
        base = CONFIG.instrument
        out = {
            'symbol': symbol, 'digits': base.digits, 'point': base.point,
            'tick_size': base.tick_size, 'tick_value': base.tick_value,
            'contract_size': base.contract_size, 'volume_min': base.volume_min,
            'volume_max': base.volume_max, 'volume_step': base.volume_step,
            'stops_level_points': base.stops_level_points,
            'commission_per_lot_side': base.commission_per_lot_side,
            'swap_free': base.swap_free, 'source': 'defaults',
        }
        if live and live.get('symbol'):
            for k in ('digits', 'point', 'tick_size', 'tick_value', 'contract_size',
                      'volume_min', 'volume_max', 'volume_step', 'stops_level_points'):
                if live.get(k) is not None:
                    out[k] = live[k]
            out['spread_points_now'] = live.get('spread_points_now')
            out['margin_per_lot'] = live.get('margin_per_lot')
            out['source'] = 'mt5'
        return out


def _is_stale(series: Series, tf: str, max_bars_behind: int = 400) -> bool:
    """Has this series stopped well short of now?"""
    if series.empty():
        return True
    step = TF_SECONDS.get(tf, 60) * 1000
    behind = (time.time() * 1000 - float(series.t[-1])) / step
    return behind > max_bars_behind


FEED = Feed()

__all__ = ['Series', 'Feed', 'FEED', 'BridgeClient', 'BRIDGE', 'load_disk',
           'load_recent', 'resample', 'empty_series', 'available_symbols',
           'available_timeframes', 'available_years']
