#!/usr/bin/env python
"""
tools/forecast_build.py - build the forecast engine's caches (Phase A).

    python tools/forecast_build.py                 reads + labels, 2018 onwards
    python tools/forecast_build.py --only reads
    python tools/forecast_build.py --only labels
    python tools/forecast_build.py --only store    the range model + what the lab reads
    python tools/forecast_build.py --workers 4

Reads are the live engine's regime / leg / MTF-trend reads for every bar,
computed on the same windows the chart uses (server/forecast/reads.py). They
are the slow part - about 13 ms a bar - so they run in worker processes, one
timeframe-year per task, at BELOW-NORMAL priority so the live API and the MT5
bridge on this machine keep first call on the CPU. Fresh years are skipped:
an engine edit or a new download rebuilds only what it touches.

Read-only with respect to everything live: it reads data/ and writes only
under runs/forecast/.
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _below_normal() -> None:
    """Drop this process to below-normal priority (Windows); best effort elsewhere."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32                                   # type: ignore[attr-defined]
        # Typed explicitly: the process pseudo-handle is a 64-bit -1, and ctypes'
        # default int conversion truncates it, so the call silently does nothing.
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass.restype = ctypes.c_int
        if not k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000):  # BELOW_NORMAL
            raise OSError('SetPriorityClass failed')
    except Exception:                                                  # noqa: BLE001
        try:
            import os
            os.nice(10)
        except Exception:                                              # noqa: BLE001
            pass


def _task(symbol: str, tf: str, year: int) -> dict:
    from server.forecast import reads
    return reads.build_year(symbol, tf, year)


def build_reads(symbol: str, workers: int, last_year: int) -> int:
    from server.config import TF_SECONDS
    from server.datafeed import available_years
    from server.forecast import QT_TFS, TFS, TRAIN_START, reads

    first = int(TRAIN_START[:4])
    tasks = []
    for tf in QT_TFS:
        have = set(available_years(symbol, tf))
        # The base timeframes start at the training start; every timeframe that
        # serves as a higher rung also needs the year before, so the first bars
        # of the window find a closed higher-timeframe bar to read.
        y0 = first if tf == '5m' else first - 1
        for y in range(y0, last_year + 1):
            if y in have and not reads.is_fresh(symbol, tf, y):
                cost = (13.0 if tf in TFS else 3.0) / TF_SECONDS[tf]
                tasks.append((cost, tf, y))
    if not tasks:
        print('reads: every timeframe-year is fresh')
        return 0
    tasks.sort(reverse=True)                       # longest first: better packing
    print(f'reads: {len(tasks)} timeframe-years to build on {workers} workers '
          f'(below-normal priority)', flush=True)
    t0 = time.perf_counter()
    done = 0
    with ProcessPoolExecutor(max_workers=workers, initializer=_below_normal) as pool:
        futs = {pool.submit(_task, symbol, tf, y): (tf, y) for _, tf, y in tasks}
        for f in as_completed(futs):
            tf, y = futs[f]
            try:
                r = f.result()
                done += 1
                print(f'  [{done:>2}/{len(tasks)}] {tf:>3} {y}  {r["bars"]:>6} bars  '
                      f'{r["seconds"]:>6.0f}s   elapsed {time.perf_counter() - t0:>6.0f}s',
                      flush=True)
            except Exception as e:                                     # noqa: BLE001
                print(f'  [FAIL] {tf} {y}: {e!r}', flush=True)
                return 1
    return 0


def build_labels(symbol: str) -> int:
    from server.forecast import TFS, TRADE_TFS, labels
    for tf in TFS:
        t0 = time.perf_counter()
        _, info = labels.market(symbol, tf, rebuild=False, info=True)
        print(f'labels: market {tf:>3}  {info["rows"]:>7} rows  '
              f'{"cached" if info["cached"] else f"{time.perf_counter() - t0:.0f}s"}', flush=True)
    for tf in TRADE_TFS:
        t0 = time.perf_counter()
        _, info = labels.trade(symbol, tf, rebuild=False, info=True)
        print(f'labels: trade  {tf:>3}  {info["rows"]:>7} rows  '
              f'{"cached" if info["cached"] else f"{time.perf_counter() - t0:.0f}s"}', flush=True)
    return 0


def build_store(symbol: str) -> int:
    """The range model and baselines per forecast timeframe - what the lab reads."""
    from server.forecast import TFS, store
    for tf in TFS:
        if store.is_fresh(symbol, tf):
            print(f'store:  {tf:>3}  cached', flush=True)
            continue
        t0 = time.perf_counter()
        info = store.build(symbol, tf, log=lambda m: print(m, flush=True))
        print(f'store:  {tf:>3}  {info["rows"]:>7} rows  k {info["k_prior"]:g}  half-life '
              f'{info["half_life_months"] or "none"}  {time.perf_counter() - t0:.0f}s', flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--symbol', default=None)
    ap.add_argument('--only', choices=('reads', 'labels', 'store'), default=None)
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--last-year', type=int, default=None)
    a = ap.parse_args()

    _below_normal()
    from datetime import datetime, timezone

    from server.forecast import SYMBOL
    symbol = a.symbol or SYMBOL
    last_year = a.last_year or datetime.now(timezone.utc).year
    rc = 0
    if a.only in (None, 'reads'):
        rc = build_reads(symbol, max(1, a.workers), last_year)
    if rc == 0 and a.only in (None, 'labels'):
        rc = build_labels(symbol)
    if rc == 0 and a.only in (None, 'store'):
        rc = build_store(symbol)
    return rc


if __name__ == '__main__':
    sys.exit(main())
