#!/usr/bin/env python
"""
tools/forward_book.py - the forward (paper) record of every playbook x timeframe.

FORWARD RECORD ONLY. It reads data/ and writes runs/forward/book_<symbol>/. No
order is placed; nothing in the live API, the bridge or the live executor is
touched (the replay runs the lab's own copy of the executor against the
lab's simulated broker).

Why a replay and not a live paper engine
----------------------------------------
Licences in the permission-to-trade plan (docs/proposals/2026-10-13-...) are
earned on FORWARD data: bars that did not exist when the rules were fixed.
Every backtest year has been looked at already. The most faithful forward
record replays the SAME frozen lab - same engine, fills, costs, settings and
contract facts as the backtest batch it is compared with - on each bar that
arrives after the start. Because the engine reads closed bars only, a replay
on new bars gives exactly the trades it would have logged as they happened,
and forward and backtest numbers are comparable one to one. What it cannot
show is the live spread at the moment; that is the live shadow log's job.

Frozen at --init (manifest.json):
  - the start (UTC; the replay starts at the matching broker-clock minute);
  - the settings and pinned contract facts of a market_batch.py batch, copied,
    and its lab fidelity;
  - a fingerprint of every file the replay's behaviour depends on. A later run
    whose fingerprint differs says so loudly, and its forward numbers stop
    counting - the rules changed (re-init a new book to start a new record);
  - the batch's cell table (tools/cell_table.py) as the backtest reference.

Each run replays from the start to the last minute on disk in calendar-month
pieces; a month that was complete on disk is cached and not replayed again
(a trade open across a month seam is closed with the earlier piece, as in the
batches). New bars come from Settings > Market data.

How the record is read, fixed in advance (proposal section 6.3):
  - A cell is PILOT-ELIGIBLE (your click, minimum size) only with at least 40
    forward trades AND a forward mean net R above 0, and only if its backtest
    reference was tier 1 or 2. Everything else is recorded, not judged.
  - At 1h a cell sees ~35-40 trades a year; 40 forward trades is about a year.

    python tools/forward_book.py --init --symbol XAUUSD.a --batch gold-f2
    python tools/forward_book.py --symbol XAUUSD.a          replay new bars, write the report
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FORWARD = ROOT / 'runs' / 'forward'
BATCHES = ROOT / 'runs' / 'research' / 'batches'
TFS = ('3m', '5m', '15m', '30m', '1h', '2h', '4h')
MIN_TRADES = 40
# Everything the replay's behaviour depends on. A change to any of these voids
# the forward numbers from that run on (the report says so).
FINGERPRINTED = (
    'server/engine/analysis.py', 'server/engine/events.py', 'server/engine/indicators.py',
    'server/engine/legs.py', 'server/engine/levels.py', 'server/engine/patterns.py',
    'server/engine/precision.py', 'server/engine/qualify.py', 'server/engine/regime.py',
    'server/engine/signals.py', 'server/engine/structure.py', 'server/engine/trendlines.py',
    'server/engine/zones.py', 'server/engine/geometry.py', 'server/executor.py', 'server/signal_store.py',
    'server/order_ledger.py', 'server/daily.py', 'server/trade_journal.py',
    'server/lab/broker.py', 'server/lab/session.py', 'server/lab/data.py',
    'server/lab/settings.py', 'server/forecast/timebase.py',
)


def fingerprint() -> str:
    h = hashlib.sha256()
    for rel in FINGERPRINTED:
        p = ROOT / rel
        h.update(rel.encode())
        # Normalise line endings: a checkout's CRLF/LF choice is not a rule change.
        h.update(p.read_bytes().replace(b'\r\n', b'\n') if p.exists() else b'<missing>')
    return h.hexdigest()[:16]


def book_dir(symbol: str) -> Path:
    return FORWARD / f'book_{symbol}'


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M')


def _month_starts(a_ms: int, b_ms: int) -> list:
    """[(start_ms, end_ms)] calendar-month pieces covering [a, b) on the disk clock."""
    out, cur = [], a_ms
    while cur < b_ms:
        d = datetime.fromtimestamp(cur / 1000, timezone.utc)
        nxt = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=timezone.utc)
        end = min(int(nxt.timestamp() * 1000), b_ms)
        out.append((cur, end))
        cur = end
    return out


def init(symbol: str, batch: str, reinit: bool) -> int:
    from server.forecast.timebase import utc_to_broker
    from server.lab.session import LAB_FIDELITY
    folder = book_dir(symbol)
    man_path = folder / 'manifest.json'
    if man_path.exists() and not reinit:
        print(f'{man_path.relative_to(ROOT)} exists: this book is already running. A new '
              f'record needs --reinit (the old one is archived, not deleted).')
        return 1
    bdir = BATCHES / batch
    settings = json.loads((bdir / 'settings.json').read_text(encoding='utf-8'))
    spec = json.loads((bdir / 'spec.json').read_text(encoding='utf-8'))
    if settings.get('symbol') not in (None, symbol) or spec.get('symbol') not in (None, symbol):
        print(f'batch {batch} is for {settings.get("symbol")}, not {symbol}')
        return 1
    if man_path.exists():
        arch = folder.with_name(folder.name + '_archived_' + time.strftime('%Y%m%d-%H%M%S'))
        shutil.move(str(folder), str(arch))
        print(f'previous book archived to {arch.relative_to(ROOT)}')
    folder.mkdir(parents=True, exist_ok=True)
    now_utc = int(time.time() * 1000)
    start_disk = int(utc_to_broker(now_utc))
    start_disk -= start_disk % 60_000
    doc = {
        'symbol': symbol, 'batch': batch,
        'start_utc': datetime.fromtimestamp(now_utc / 1000, timezone.utc).isoformat(timespec='seconds'),
        'start_disk_ms': start_disk, 'start_disk': _iso(start_disk) + ' (broker clock)',
        'fidelity': settings.get('fidelity') or LAB_FIDELITY,
        'settings_sha': settings.get('sha'), 'settings': settings['settings'],
        'spec_pinned': spec.get('pinned') or {}, 'spec_doc': spec,
        'fingerprint': fingerprint(), 'fingerprinted': list(FINGERPRINTED),
        'reference': str((bdir / 'cells.json').relative_to(ROOT)),
        'rule': f'PILOT-eligible: reference tier 1 or 2, >= {MIN_TRADES} forward trades, '
                f'forward mean net R > 0 (proposal section 6.3)',
    }
    man_path.write_text(json.dumps(doc, indent=1), encoding='utf-8')
    print(f'{symbol}: forward book frozen at {doc["start_utc"]} '
          f'(disk clock {doc["start_disk"]}), fingerprint {doc["fingerprint"]}, '
          f'settings {doc["settings_sha"]}')
    return 0


def replay_piece(symbol: str, tf: str, a_ms: int, b_ms: int, man: dict) -> list:
    from server.lab.session import ReplaySession
    s = ReplaySession({'symbol': symbol, 'tf': tf, 'start': _iso(a_ms), 'end': _iso(b_ms),
                       'mode': 'auto', 'record': False, 'news_gate': False,
                       'overrides': man['settings'], 'fidelity': man['fidelity'],
                       'spec_overrides': man['spec_pinned'],
                       'name': f'forward book {symbol} {tf}'})
    while s.advance(2000):
        del s.frames[:-50]
    return s.journal


def day_ci(rows: list, reps: int = 2000) -> tuple:
    days: dict = {}
    for d, r in rows:
        days.setdefault(d, []).append(r)
    if len(days) < 10:
        return (float('nan'), float('nan'))
    blocks = list(days.values())
    sums = np.array([sum(b) for b in blocks]); ns = np.array([len(b) for b in blocks])
    idx = np.random.default_rng(11).integers(0, len(blocks), (reps, len(blocks)))
    m = sums[idx].sum(1) / ns[idx].sum(1)
    return float(np.quantile(m, 0.05)), float(np.quantile(m, 0.95))


def run(symbol: str) -> int:
    from tools.forecast_build import _below_normal
    _below_normal()
    from server.lab import data as lab_data
    folder = book_dir(symbol)
    man = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    fp = fingerprint()
    valid = fp == man['fingerprint']
    cover = lab_data.coverage(symbol)
    last = int(cover['1m']['last_ms']) + 60_000
    start = int(man['start_disk_ms'])
    cache = folder / 'cache'
    cache.mkdir(exist_ok=True)
    t0 = time.time()
    trades = []
    pieces = _month_starts(start, last) if last > start else []
    for tf in TFS:
        for a_ms, b_ms in pieces:
            key = cache / f"{tf}_{datetime.fromtimestamp(a_ms / 1000, timezone.utc):%Y-%m}.json"
            hit = None
            if key.exists():
                hit = json.loads(key.read_text(encoding='utf-8'))
                if hit.get('fingerprint') != fp or hit.get('end_ms') != b_ms:
                    hit = None
            if hit is None:
                try:
                    journal = replay_piece(symbol, tf, a_ms, b_ms, man)
                except ValueError as exc:            # e.g. too few bars yet for this piece
                    journal, err = [], str(exc)
                else:
                    err = None
                hit = {'tf': tf, 'start_ms': a_ms, 'end_ms': b_ms, 'fingerprint': fp,
                       'error': err, 'journal': journal}
                # Only a piece that ended at a month boundary is final.
                month_end = _month_starts(a_ms, a_ms + 32 * 86_400_000)[0][1]
                if b_ms == month_end and err is None:
                    key.write_text(json.dumps(hit, default=str), encoding='utf-8')
            for j in hit['journal']:
                if j.get('playbook') != 'manual' and j.get('r_net') is not None:
                    trades.append(dict(j, tf=tf))

    ref = {}
    ref_path = ROOT / man['reference']
    if ref_path.exists():
        for c in json.loads(ref_path.read_text(encoding='utf-8'))['cells']:
            ref[(c['playbook'], c['tf'])] = c
    from tools.cell_table import PLAYBOOKS
    lines, rows = [], []
    w = lines.append
    w(f"FORWARD BOOK - {symbol}   started {man['start_utc']}   data on disk to "
      f"{_iso(last - 60_000)} (broker clock)   ran {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    w(f"Frozen: batch {man['batch']} (settings {man['settings_sha']}), fidelity {man['fidelity']}, "
      f"spec pinned {man['spec_pinned']}. Paper only - no orders.")
    if not valid:
        w(f"!! THE CODE CHANGED SINCE THE START (fingerprint {fp} != {man['fingerprint']}). "
          f"Forward numbers from this run do NOT count; re-init to start a new record.")
    w(f"Rule fixed in advance: {man['rule']}.")
    w('')
    w(f"{'playbook':<18}{'tf':<5}{'fwd n':>6}{'fwd R':>8}{'90% interval':>19}"
      f"{'ref dev':>9}{'ref conf':>9}{'tier':>5}  status")
    for pb in PLAYBOOKS:
        for tf in TFS:
            rs = [(int(t['fill_ms']) // 86_400_000, float(t['r_net'])) for t in trades
                  if t['playbook'] == pb and t['tf'] == tf and t.get('fill_ms')]
            rc = ref.get((pb, tf), {})
            tier = rc.get('tier', '-')
            n = len(rs)
            mean = float(np.mean([r for _d, r in rs])) if rs else float('nan')
            lo, hi = day_ci(rs)
            if tier in ('1', '2') and n >= MIN_TRADES and mean > 0 and valid:
                status = 'PILOT-ELIGIBLE (needs your click)'
            elif tier in ('1', '2'):
                status = f'candidate: {max(0, MIN_TRADES - n)} more trades to read' \
                    if n < MIN_TRADES else 'candidate: forward mean <= 0'
            else:
                status = 'recorded'
            rows.append({'playbook': pb, 'tf': tf, 'n': n, 'mean': mean, 'ci90': [lo, hi],
                         'ref_dev': rc.get('dev'), 'ref_confirm': rc.get('confirm'),
                         'tier': tier, 'status': status})
            if n or tier in ('1', '2'):
                w(f"{pb:<18}{tf:<5}{n:>6}{mean:>+8.3f}  [{lo:+.3f},{hi:+.3f}]"
                  f"{(rc.get('dev') if rc else float('nan')):>+9.3f}"
                  f"{(rc.get('confirm') if rc else float('nan')):>+9.3f}{tier:>5}  {status}")
    tot = [float(t['r_net']) for t in trades]
    w('')
    w(f"All cells: {len(tot)} forward paper trades, mean net R "
      f"{(np.mean(tot) if tot else float('nan')):+.3f}. Cells with no trades and no candidate "
      f"tier are not listed. ({time.time() - t0:.0f}s)")
    if not ref:
        w(f"(backtest reference {man['reference']} not built yet: "
          f"python tools/cell_table.py --tag {man['batch']} --dev-to 2023)")
    w('Measurements, not trading advice.')
    (folder / 'report.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    with open(folder / 'trades.csv', 'w', newline='', encoding='utf-8') as fh:
        cols = ['tf', 'playbook', 'side', 'kind', 'fill_ms', 'exit_ms', 'fill_price', 'exit_price',
                'stop', 'outcome', 'r_multiple', 'r_net', 'profit']
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction='ignore')
        wr.writeheader()
        for t in sorted(trades, key=lambda x: int(x.get('fill_ms') or 0)):
            wr.writerow(t)
    with open(folder / 'runs.jsonl', 'a', encoding='utf-8') as fh:
        fh.write(json.dumps({'ran_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                             'data_to': _iso(last - 60_000), 'fingerprint': fp, 'valid': valid,
                             'trades': len(tot), 'cells': rows}, default=str) + '\n')
    print('\n'.join(lines))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--symbol', required=True)
    ap.add_argument('--init', action='store_true', help='freeze and start the book (once)')
    ap.add_argument('--reinit', action='store_true', help='archive the old book, start a new one')
    ap.add_argument('--batch', help='the market_batch.py tag whose settings and spec to freeze')
    a = ap.parse_args()
    if a.init or a.reinit:
        if not a.batch:
            ap.error('--init needs --batch')
        return init(a.symbol, a.batch, a.reinit)
    return run(a.symbol)


if __name__ == '__main__':
    sys.exit(main())
