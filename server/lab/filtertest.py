"""
server/lab/filtertest.py - Phase D: forecast filters through the real executor.

For one timeframe-year:

  1. BASELINE  one full replay through the live engine, the live executor and
               the simulated broker, exactly as the lab runs - no filter. On
               the way it caches, per bar, the slice of the analysis that
               qualification reads and the engine's raw signals.
  2. CHECK     the same year replayed from that cache, no filter. It must
               reproduce the baseline trade for trade, or the run stops: the
               cache is only trusted once it has proved it changes nothing.
  3. FILTERS   the year replayed from the cache once per filter. The market
               and the signals cannot depend on a filter, so they are reused;
               what the executor is allowed to SEND changes, and everything
               downstream of that - positions, daily caps, trailing stops,
               fills - is simulated for real.

Besides the forecast filters there is one more arm, 'news_gate': the live
news blackout (qualify.py gate 10), simulated from the release history. It
is not a forecast - it measures what a live gate the lab could not simulate
before Phase E does to the same year.

Run in its own process (tools/forecast_filtertest.py): it patches the session
module's analysis and signal generation for the replays, which must never
happen inside the lab server.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import time
import zlib
from datetime import datetime, timezone

from . import session as S
from . import settings as lab_settings
from . import stats as lab_stats
from .filters import FILTERS

NEWS_GATE = 'news_gate'
ARMS = FILTERS + (NEWS_GATE,)

# The heavy parts of a snapshot that qualification does not read. The CHECK
# replay proves the rest is enough: any key qualification needed and lost
# would change a trade, and the run would stop.
HEAVY = ('swings', 'breaks', 'trendlines', 'channels', 'patterns', 'events', 'fvg', 'zones',
         'liquidity', 'equal_highs', 'equal_lows', 'regression_channel', 'volume_profile')


def _pack(obj) -> bytes:
    """Cached per bar as compressed pickles: small, and every replay gets a fresh copy."""
    return zlib.compress(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL), 1)


def _unpack(b: bytes):
    return pickle.loads(zlib.decompress(b))


def _slim(snap: dict) -> bytes:
    return _pack({k: v for k, v in snap.items() if k not in HEAVY})


def _lean_frames() -> None:
    """Batch sessions keep no per-bar UI frames - nobody reviews them, and a 5m year of
    them is hundreds of MB."""
    S.ReplaySession._frame_state = lambda self, k, sigs: {'i': k}


def _cfg(symbol: str, tf: str, year: int, end_ms: int = None, ff: str = None,
         settings: dict = None) -> dict:
    start = f'{year}-01-01T00:00'
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M') \
        if end_ms else f'{year + 1}-01-01T00:00'
    return {'symbol': symbol, 'tf': tf, 'start': start, 'end': end, 'mode': 'auto',
            'record': False, 'name': f'filtertest {tf} {year} {ff or "baseline"}',
            'forecast_filter': None if ff == NEWS_GATE else ff, 'news_gate': ff == NEWS_GATE,
            'overrides': settings or {}}


def frozen_settings() -> dict:
    """Every live setting, read once. A session re-reads configs/settings.json when it
    starts, and a timeframe-year's replays start hours apart: without this, a save in the
    live app between them puts the baseline and the filters on different settings."""
    return lab_settings.jsonable(lab_settings.effective({}))


def settings_hash(settings: dict) -> str:
    return hashlib.sha1(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _run(cfg: dict, log=None) -> dict:
    s = S.ReplaySession(cfg)
    t0 = time.perf_counter()
    total = s.last - s.k
    done = 0
    while not s.at_end():
        n = s.advance(2000)
        done += n
        if log and n:
            log(f'      {cfg["name"]}: {done:,}/{total:,} bars ({time.perf_counter() - t0:.0f}s)')
        if n == 0:
            break
    # A forecast filter names itself in every veto. The news gate cannot be counted from
    # the journal: it names only the first gate that blocked, and the blackout is gate 10.
    blocked = None if cfg.get('news_gate') else \
        sum(1 for e in s.events if 'forecast filter' in str(e.get('text', '')))
    trades = [dict(t) for t in s.trades]
    return {'cfg': cfg, 'trades': trades, 'stats': lab_stats.compute(s.trades, s.balance0),
            'balance0': s.balance0, 'bars': done, 'blocked_events': blocked,
            'seconds': round(time.perf_counter() - t0, 1)}


def _fingerprint(trades: list) -> list:
    return [(t['entry_t'], t['exit_t'], t['side'], round(float(t['entry']), 2),
             round(float(t['exit']), 2), round(float(t['r']), 4)) for t in trades]


def _first_difference(a: list, b: list) -> str:
    fa, fb = _fingerprint(a), _fingerprint(b)
    for i, (x, y) in enumerate(zip(fa, fb)):
        if x != y:
            return f'trade {i}: baseline {x} vs cached {y}'
    i = min(len(fa), len(fb))
    return f'trade {i}: ' + (f'baseline {fa[i]} not in the cached run' if len(fa) > i
                             else f'cached {fb[i]} not in the baseline')


def run_year(symbol: str, tf: str, year: int, filters=FILTERS, log=None,
             settings: dict = None) -> dict:
    """Baseline, cache check and every filter for one timeframe-year, all on one frozen
    copy of the live settings."""
    S.MAX_BARS = 250_000                    # a whole year is a batch job, not a replay
    _lean_frames()
    settings = settings or frozen_settings()
    cache: dict = {}
    real_generate = S.generate
    real_analyse = S.ReplaySession._analyse

    # ---- 1. baseline, capturing
    def gen_capture(snap, window):
        out = real_generate(snap, window)
        cache.setdefault(int(snap.get('bar_time_ms') or 0), {})['gen'] = _pack(out)
        return out

    def analyse_capture(self, k, memo=True):
        snap = real_analyse(self, k, memo)
        cache.setdefault(int(self.series.t[k]), {})['snap'] = _slim(snap)
        return snap

    S.generate, S.ReplaySession._analyse = gen_capture, analyse_capture
    try:
        base = _run(_cfg(symbol, tf, year, settings=settings), log)
    finally:
        S.generate, S.ReplaySession._analyse = real_generate, real_analyse

    # ---- 2 + 3. replays from the cache
    def gen_replay(snap, window):
        hit = cache.get(int(snap.get('bar_time_ms') or 0), {})
        return _unpack(hit['gen']) if 'gen' in hit else []

    def analyse_replay(self, k, memo=True):
        hit = cache.get(int(self.series.t[k]))
        if hit is None or 'snap' not in hit:
            return real_analyse(self, k, memo)
        return _unpack(hit['snap'])

    out = {'symbol': symbol, 'tf': tf, 'year': year, 'settings': settings,
           'settings_hash': settings_hash(settings), 'baseline': base, 'filters': {}}
    S.generate, S.ReplaySession._analyse = gen_replay, analyse_replay
    try:
        check = _run(_cfg(symbol, tf, year, settings=settings), log)
        same = _fingerprint(check['trades']) == _fingerprint(base['trades'])
        out['check'] = {'same': same, 'trades': len(check['trades']), 'seconds': check['seconds']}
        if not same:
            raise RuntimeError(f'{tf} {year}: the cached replay did not reproduce the baseline '
                               f'({len(check["trades"])} vs {len(base["trades"])} trades; '
                               f'{_first_difference(base["trades"], check["trades"])})')
        for ff in filters:
            out['filters'][ff] = _run(_cfg(symbol, tf, year, ff=ff, settings=settings), log)
    finally:
        S.generate, S.ReplaySession._analyse = real_generate, real_analyse
    return out


__all__ = ['ARMS', 'HEAVY', 'NEWS_GATE', 'frozen_settings', 'run_year', 'settings_hash']
