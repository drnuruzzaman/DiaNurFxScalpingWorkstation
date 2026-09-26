"""
tools/mt5_download.py - top up data/<symbol>/<tf>/<year>.csv.gz from MetaTrader 5.

Loaded by bridge/mt5_bridge.py's /download/start (download_worker) INSIDE the
bridge process, on the bridge's own MT5 session, while it holds MT5_LOCK - so
live quotes and orders wait until this returns. It is therefore short, bounded
and read-only: copy_rates_range and symbol_info_tick only. Nothing here can
place, modify or close an order. The bridge reloads this module on every
download, so an edit here needs no bridge restart.

Incremental: for each symbol and timeframe it finds the newest bar on disk and
asks MT5 for everything from two days before it up to now, in bounded chunks.
Only CLOSED bars are written; a bar still forming is left for the next update.
A timeframe with nothing on disk gets `years` of history. Rows merge into the
broker-year files - a new row replaces an old one with the same timestamp -
then sort, then an atomic replace, so a crash never leaves a half-written file.

Format, as the rest of data/ is written:
    ts,open,high,low,close,tick_volume,real_volume,spread
ts is MT5 SERVER time in epoch seconds (the broker clock; server/forecast/
timebase.py converts it to UTC). Nothing is converted here.
"""
from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
HEADER = 'ts,open,high,low,close,tick_volume,real_volume,spread'
TF_SECONDS = {'1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600,
              '2h': 7200, '4h': 14400, '1d': 86400, '1w': 604800}
OVERLAP_S = 2 * 86400            # re-read the last two days: late corrections land
CHUNK_BARS = 50_000              # bars per copy_rates_range call, at most
FLUSH_ROWS = 250_000             # merge to disk once this many rows are held
DAY_S = 86400


def _tf_const(mt5, tf: str):
    return {'1m': mt5.TIMEFRAME_M1, '3m': mt5.TIMEFRAME_M3, '5m': mt5.TIMEFRAME_M5,
            '15m': mt5.TIMEFRAME_M15, '30m': mt5.TIMEFRAME_M30, '1h': mt5.TIMEFRAME_H1,
            '2h': mt5.TIMEFRAME_H2, '4h': mt5.TIMEFRAME_H4, '1d': mt5.TIMEFRAME_D1,
            '1w': mt5.TIMEFRAME_W1}[tf]


def _year_of(ts: int) -> int:
    return datetime.fromtimestamp(int(ts), timezone.utc).year      # broker clock as-is


def disk_tfs(symbol: str) -> list:
    base = DATA / symbol
    return [tf for tf in TF_SECONDS if (base / tf).is_dir()] if base.is_dir() else []


def disk_years(symbol: str, tf: str) -> list:
    base = DATA / symbol / tf
    out = []
    for p in base.glob('*.csv.gz') if base.is_dir() else []:
        try:
            out.append(int(p.name.split('.')[0]))
        except ValueError:
            continue
    return sorted(out)


def _read_rows(path: Path) -> dict:
    """ts -> the CSV line, for one year file ({} if it does not exist)."""
    if not path.exists():
        return {}
    rows = {}
    with gzip.open(path, 'rt', encoding='utf-8') as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line or (i == 0 and line.startswith('ts')):
                continue
            try:
                rows[int(line.split(',', 1)[0])] = line
            except ValueError:
                continue
    return rows


def last_ts(symbol: str, tf: str) -> int | None:
    """The newest bar on disk (server epoch seconds), or None."""
    for y in reversed(disk_years(symbol, tf)):
        rows = _read_rows(DATA / symbol / tf / f'{y}.csv.gz')
        if rows:
            return max(rows)
    return None


def _num(x: float, digits: int) -> str:
    return repr(round(float(x), digits))


def _line(r, digits: int) -> str:
    return ','.join((str(int(r['time'])), _num(r['open'], digits), _num(r['high'], digits),
                     _num(r['low'], digits), _num(r['close'], digits),
                     str(int(r['tick_volume'])), str(int(r['real_volume'])),
                     str(int(r['spread']))))


def _write_year(path: Path, rows: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8', newline='\n') as fh:
        fh.write(HEADER + '\n')
        for ts in sorted(rows):
            fh.write(rows[ts] + '\n')
    os.replace(tmp, path)                  # never a half-written year file


def merge(symbol: str, tf: str, lines: dict) -> int:
    """Merge {ts: csv line} into the year files. Returns how many were new or changed."""
    by_year: dict = {}
    for ts, line in lines.items():
        by_year.setdefault(_year_of(ts), {})[ts] = line
    changed = 0
    for y, new in by_year.items():
        path = DATA / symbol / tf / f'{y}.csv.gz'
        rows = _read_rows(path)
        n = sum(1 for ts, line in new.items() if rows.get(ts) != line)
        if not n:
            continue
        rows.update(new)
        _write_year(path, rows)
        changed += n
    return changed


# A symbol's last tick older than this means its market is shut (the weekend, a
# holiday, the daily break) rather than merely quiet.
IDLE_S = 15 * 60


def _server_now(tick_time: int) -> int:
    """
    "Now" on the broker's clock, for deciding which bars have closed.

    The last tick's time is that clock while the market trades. Once it shuts,
    no later tick ever arrives, so the bar holding the final tick - Friday's
    last bar on every timeframe - looked forever unfinished and was never
    written until the next week's download. When the tick is stale, the true
    broker time is used instead: the bridge that loads this module keeps the
    measured offset of its clock from UTC (STATE['time_offset_ms']).
    """
    import sys
    st = getattr(sys.modules.get('__main__'), 'STATE', None)
    off = st.get('time_offset_ms') if isinstance(st, dict) else None
    if off is None or not tick_time:
        return tick_time
    real = int(time.time() + int(off) / 1000)
    return real if real - tick_time > IDLE_S else tick_time


def fetch_bars(symbols, tfs, years, progress=None, cancelled=None) -> int:
    """Top up every (symbol, timeframe). Returns bars written (new or corrected)."""
    import MetaTrader5 as mt5                                   # the bridge's session

    progress = progress or (lambda **kw: None)
    cancelled = cancelled or (lambda: False)
    plan = [(s, tf) for s in symbols for tf in (tfs or disk_tfs(s))]
    written = 0
    for done, (sym, tf) in enumerate(plan):
        if cancelled():
            break
        progress(phase='bars', symbol=sym, tf=tf, item='reading disk', done=done, total=len(plan))
        mt5.symbol_select(sym, True)
        info = mt5.symbol_info(sym)
        digits = int(getattr(info, 'digits', 2) or 2)
        tick = mt5.symbol_info_tick(sym)
        now_srv = _server_now(int(getattr(tick, 'time', 0) or 0))
        step = TF_SECONDS[tf]
        last = last_ts(sym, tf)
        start = (last - OVERLAP_S) if last else int(time.time()) - int(years) * 365 * DAY_S
        end = int(time.time()) + DAY_S                      # a margin for the broker offset
        lines, n, newest = {}, 0, last
        t0, chunks = start, max(1, -(-(end - start) // (CHUNK_BARS * step)))
        k = 0
        while t0 < end and not cancelled():
            t1 = min(end, t0 + CHUNK_BARS * step)
            rows = mt5.copy_rates_range(sym, _tf_const(mt5, tf),
                                        datetime.fromtimestamp(t0, timezone.utc),
                                        datetime.fromtimestamp(t1, timezone.utc))
            for r in (rows if rows is not None else []):
                ts = int(r['time'])
                # A bar still forming is not history yet. Without a tick time
                # (terminal just started) keep all but the newest bar.
                if now_srv and ts + step > now_srv:
                    continue
                lines[ts] = _line(r, digits)
            t0, k = t1, k + 1
            # A first download of years of 1m is millions of rows: written as it
            # goes rather than held in the bridge's memory until the end.
            if len(lines) >= FLUSH_ROWS:
                newest = max(newest or 0, max(lines))
                n += merge(sym, tf, lines)
                lines = {}
            if chunks > 1:
                progress(phase='bars', symbol=sym, tf=tf, item=f'chunk {k} of {chunks}',
                         done=done + k / chunks, total=len(plan))
        if not now_srv and lines:
            lines.pop(max(lines))
        if lines:
            newest = max(newest or 0, max(lines))
            n += merge(sym, tf, lines)
        written += n
        progress(phase='bars', symbol=sym, tf=tf,
                 item=f'{n} new or corrected bars, newest '
                      f'{datetime.fromtimestamp(newest, timezone.utc):%Y-%m-%d %H:%M}'
                 if newest else 'nothing on MT5', done=done + 1, total=len(plan))
    return written


def fetch_ticks(symbols, days, progress=None, cancelled=None) -> int:
    """Ticks are not stored by this workstation; the lab fills on 1m bars."""
    if progress:
        progress(phase='ticks', symbol=','.join(symbols), tf='-', item='not supported - skipped',
                 done=0, total=0)
    return 0


def coverage(symbol: str) -> dict:
    """{tf: {'first_year', 'last_ts'}} straight from the files."""
    out = {}
    for tf in disk_tfs(symbol):
        ys = disk_years(symbol, tf)
        if ys:
            out[tf] = {'first_year': ys[0], 'last_ts': last_ts(symbol, tf)}
    return out


def write_manifest(_=None) -> None:
    """Record what is on disk in data/manifest.json, keeping every other key (the clock)."""
    path = DATA / 'manifest.json'
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        doc = {}
    doc['coverage'] = {s.name: coverage(s.name) for s in sorted(DATA.iterdir())
                       if s.is_dir() and not s.name.startswith('_')}
    doc['coverage_ms'] = int(time.time() * 1000)
    tmp = path.with_name('manifest.json.tmp')
    tmp.write_text(json.dumps(doc, indent=1), encoding='utf-8')
    os.replace(tmp, path)


__all__ = ['fetch_bars', 'fetch_ticks', 'write_manifest', 'coverage', 'last_ts', 'merge',
           'disk_tfs', 'disk_years']
