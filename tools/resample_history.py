#!/usr/bin/env python
"""
tools/resample_history.py - build missing higher-timeframe year files from 1m.

Why this exists
---------------
tools/mt5_download.py fixes a timeframe's history depth at its FIRST download
and only tops up forward afterwards. EURUSD.a was first downloaded (2026-10-20)
with 1m back to 2020 - all the broker serves - but every other timeframe only
from 2025, so the lab could replay EURUSD 5m with no 15m/1h/4h context before
2025. The 1m history is the broker's own, so its aggregates ARE those bars.

What it does
------------
For each timeframe, every year that has 1m bars but no file of its own gets one
built from the 1m bars, on the broker clock the files already use: intraday
bars on epoch multiples of their length (which is also the broker midnight),
daily bars at broker midnight, weekly bars from Sunday 00:00 (MT5's week).

It never changes a bar the broker served - native bars always win. A year with
no file gets one; the FIRST native year, when the broker's file starts part-way
through it (the download asks for a span back from the download date: EURUSD's
higher timeframes start 2025-10-20), gets the earlier part of the year
prepended, with the original kept beside it as <year>.csv.gz.native. Nothing
is written until the same aggregation reproduces a year the broker DID serve
(--check-year, default the first native year) to within --tolerance of
matching bars. Spread is aggregated the way that check shows the native files
do it.

    python tools/resample_history.py --symbol EURUSD.a            # dry run
    python tools/resample_history.py --symbol EURUSD.a --write
"""
from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import TF_SECONDS  # noqa: E402

DATA = ROOT / 'data'
TFS = ('3m', '5m', '15m', '30m', '1h', '2h', '4h', '1d', '1w')
COLS = 'ts,open,high,low,close,tick_volume,real_volume,spread'
DAY = 86_400


def read_year(path: Path) -> np.ndarray:
    """(N, 8) float array in file column order."""
    with gzip.open(path, 'rt') as fh:
        lines = fh.read().splitlines()
    if lines and lines[0].lower().startswith('ts'):
        lines = lines[1:]
    if not lines:
        return np.zeros((0, 8))
    return np.array([[float(x) for x in ln.split(',')[:8]] for ln in lines])


def bucket(ts: np.ndarray, tf: str) -> np.ndarray:
    ts = ts.astype(np.int64)
    if tf == '1w':
        days = ts // DAY
        # 1970-01-01 was a Thursday; MT5 weeks start on Sunday (day 3).
        return ((days - 3) // 7 * 7 + 3) * DAY
    sec = TF_SECONDS[tf]
    return ts // sec * sec


SPREAD_AGG = {
    'min': lambda x: np.min(x),
    'first': lambda x: x[0],
    'last': lambda x: x[-1],
    'max': lambda x: np.max(x),
}


def aggregate(m1: np.ndarray, tf: str, spread_how: str) -> np.ndarray:
    if not len(m1):
        return np.zeros((0, 8))
    b = bucket(m1[:, 0], tf)
    starts = np.flatnonzero(np.r_[True, b[1:] != b[:-1]])
    ends = np.r_[starts[1:], len(b)]
    out = np.empty((len(starts), 8))
    agg = SPREAD_AGG[spread_how]
    for i, (a, z) in enumerate(zip(starts, ends)):
        seg = m1[a:z]
        out[i] = (b[a], seg[0, 1], seg[:, 2].max(), seg[:, 3].min(), seg[-1, 4],
                  seg[:, 5].sum(), seg[:, 6].sum(), agg(seg[:, 7]))
    return out


def compare(built: np.ndarray, native: np.ndarray, digits: int) -> dict:
    """Share of native bars reproduced exactly (time + OHLC to the digit)."""
    if not len(native):
        return {'n_native': 0, 'n_built': len(built), 'match': 0.0, 'spread': {}}
    nb = {int(r[0]): r for r in built}
    hit = 0
    sp = {}
    for r in native:
        x = nb.get(int(r[0]))
        if x is None:
            continue
        if all(round(x[j], digits) == round(r[j], digits) for j in (1, 2, 3, 4)):
            hit += 1
    return {'n_native': len(native), 'n_built': len(built), 'match': hit / len(native),
            'spread': sp}


def write_year(path: Path, rows: np.ndarray, digits: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with gzip.open(tmp, 'wt') as fh:
        fh.write(COLS + '\n')
        for r in rows:
            fh.write(f'{int(r[0])},{r[1]:.{digits}f},{r[2]:.{digits}f},{r[3]:.{digits}f},'
                     f'{r[4]:.{digits}f},{int(r[5])},{int(r[6])},{int(r[7])}\n')
    tmp.replace(path)


def years_of(folder: Path) -> list:
    out = []
    for p in folder.glob('*.csv.gz'):
        try:
            out.append(int(p.name.split('.')[0]))
        except ValueError:
            pass
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--symbol', required=True)
    ap.add_argument('--check-year', type=int, default=None,
                    help='a year the broker served for every timeframe (default: first native)')
    ap.add_argument('--tolerance', type=float, default=0.995,
                    help='share of native bars that must be reproduced exactly')
    ap.add_argument('--write', action='store_true', help='write the missing years')
    a = ap.parse_args()

    base = DATA / a.symbol
    import json
    spec = json.loads((base / 'spec.json').read_text(encoding='utf-8'))
    digits = int(spec.get('digits') or 5)
    m1_years = years_of(base / '1m')
    if not m1_years:
        print(f'no 1m history for {a.symbol}')
        return 1
    m1_cache: dict = {}

    def m1(year: int) -> np.ndarray:
        if year not in m1_cache:
            m1_cache[year] = read_year(base / '1m' / f'{year}.csv.gz')
        return m1_cache[year]

    plan, failed = [], False
    for tf in TFS:
        native = years_of(base / tf)
        check = a.check_year or (native[0] if native else None)
        if check is None or check not in m1_years:
            print(f'{tf:>4}: no native year with 1m bars to check against - skipped')
            failed = True
            continue
        nat = read_year(base / tf / f'{check}.csv.gz')
        # MT5 rolls a weekly bar across the new year; drop the edges from the check.
        best = None
        for how in SPREAD_AGG:
            built = aggregate(m1(check), tf, how)
            cmp = compare(built, nat, digits)
            sp_hit = 0
            nb = {int(r[0]): r for r in built}
            for r in nat:
                x = nb.get(int(r[0]))
                sp_hit += x is not None and int(x[7]) == int(r[7])
            score = (cmp['match'], sp_hit / max(1, len(nat)))
            if best is None or score > best[0]:
                best = (score, how, cmp)
        (ohlc, sp), how, cmp = best
        edge_ok = tf == '1w' and ohlc >= 0.9      # first/last week straddle the year files
        ok = ohlc >= a.tolerance or edge_ok
        missing = [y for y in m1_years if y not in native and y < (native[0] if native else 9999)]
        head = None
        if native and native[0] in m1_years:
            first_nat = int(read_year(base / tf / f'{native[0]}.csv.gz')[0, 0])
            first_m1 = int(m1(native[0])[0, 0])
            if first_nat - first_m1 > 7 * DAY:
                head = (native[0], first_nat)
        print(f'{tf:>4}: check {check}: {cmp["n_native"]} native bars, OHLC reproduced '
              f'{ohlc:.2%}, spread as "{how}" {sp:.2%} -> {"OK" if ok else "FAIL"}; '
              f'missing years {missing or "none"}'
              + (f'; {head[0]} starts {_day(head[1])} (head to prepend)' if head else ''))
        if not ok:
            failed = True
            continue
        plan.extend((tf, y, how, None) for y in missing)
        if head:
            plan.append((tf, head[0], how, head[1]))

    if failed:
        print('\nnot every timeframe reproduced its native year - nothing written for those')
    if not a.write:
        print(f'\ndry run: {len(plan)} year files would be written (add --write)')
        return 0
    for tf, y, how, first_nat in plan:
        path = base / tf / f'{y}.csv.gz'
        rows = aggregate(m1(y), tf, how)
        if first_nat is None:
            if path.exists():
                continue                                # never overwrite a broker file
            write_year(path, rows, digits)
            print(f'wrote {path.relative_to(ROOT)}: {len(rows)} bars')
            continue
        native = read_year(path)
        if int(native[0, 0]) != first_nat:
            continue                                    # already prepended
        keep = path.with_name(path.name + '.native')
        if not keep.exists():
            keep.write_bytes(path.read_bytes())
        head_rows = rows[rows[:, 0] < first_nat]
        write_year(path, np.vstack([head_rows, native]), digits)
        print(f'prepended {len(head_rows)} bars to {path.relative_to(ROOT)} '
              f'(native {len(native)} kept, original in {keep.name})')
    return 0


def _day(ts: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime('%Y-%m-%d')


if __name__ == '__main__':
    sys.exit(main())
