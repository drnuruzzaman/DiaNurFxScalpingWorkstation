#!/usr/bin/env python
"""
tools/structure_audit.py - Step 0A: how CORRECT are swings, trendlines and patterns today?

    python tools/structure_audit.py                     record gold 5m + 15m (about 20 min)
    python tools/structure_audit.py --report runs/audit/structure/<run>

Measurement only - the engine is imported, never changed. Each bar of each span
is analysed exactly as the backtest lab analyses it (ReplaySession._analyse:
600 closed bars, higher timeframes on their closed bars), and everything the
chart would show is recorded with real bar TIMES, so objects can be followed
from bar to bar:

    swings      time, high/low, price, HH/HL/LH/LL, when it became knowable
    trendlines  kind, both anchors, slope, touches, score, broken
    channels    kind, both rails, containment, score
    patterns    kind, direction, status, quality, points, break / target levels
    signals     what the playbooks detected on the bar (before the gates)

Every 5th bar two more windows are analysed on the same bars, for the parity
and window checks: 599 closed bars (what LIVE analyses - it fetches 600 and
drops the forming one) and 1500 (does the answer depend on where the window
starts?).

The spans are fixed here, before anything is measured: two 3-week spans a
year, 2018-2026, from the first Monday of February and of August.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'runs' / 'audit' / 'structure'
SYMBOL = 'XAUUSD.a'
TFS = ('5m', '15m')
YEARS = range(2018, 2027)
SPAN_DAYS = 21
EXTRA_EVERY = 5                 # 599- and 1500-bar windows on every 5th bar
LIVE_WINDOW, LONG_WINDOW = 599, 1500


def spans() -> list:
    """(start, end) ISO dates: two 3-week spans a year, fixed in advance."""
    out = []
    for y in YEARS:
        for m in (2, 8):
            d = date(y, m, 1)
            d += timedelta(days=(7 - d.weekday()) % 7)          # first Monday
            out.append((d.isoformat(), (d + timedelta(days=SPAN_DAYS)).isoformat()))
    return out


# --------------------------------------------------------------------------- #
# compact records - ABSOLUTE bar indices (into the span's series), so objects  #
# from different windows and different bars compare directly                 #
# --------------------------------------------------------------------------- #
def _ab(off, i):
    try:
        return int(i) + off
    except (TypeError, ValueError):
        return None


def rec_snapshot(snap: dict, off: int) -> dict:
    """`off` = absolute index of the window's first bar."""
    if not snap.get('ok'):
        return {'ok': False}
    sw = [[_ab(off, s['idx']), 'H' if s['kind'] == 'high' else 'L', round(s['price'], 3),
           s.get('label', ''), _ab(off, s.get('confirmed_at')), s.get('strength', 0.0),
           bool(s.get('swept'))]
          for s in snap.get('swings') or []]
    tl = [[x['kind'][0], _ab(off, x['x1']), round(x['y1'], 3), _ab(off, x['x2']),
           round(x['y2'], 3), x['slope'], x['touches'], x['score'], bool(x['broken']),
           [_ab(off, i) for i in x.get('touch_idx') or []]]
          for x in snap.get('trendlines') or []]
    ch = [[x['kind'], _ab(off, (x.get('upper') or {}).get('x1')),
           round((x.get('upper') or {}).get('y1', 0.0), 3),
           round((x.get('lower') or {}).get('y1', 0.0), 3), x.get('slope'),
           x.get('containment'), x.get('score'), x.get('width_atr')]
          for x in snap.get('channels') or []]
    pt = [[p['kind'], p['direction'], p['status'], p['quality'],
           [[q.get('t'), q.get('price'), q.get('role')] for q in p.get('points') or []],
           p.get('break_level'), p.get('target'), p.get('invalidation'),
           _ab(off, p.get('start_idx')), _ab(off, p.get('end_idx')),
           bool(p.get('actionable')), p.get('relevance'), p.get('distance_to_break_atr')]
          for p in snap.get('patterns') or []]
    return {'ok': True, 'sw': sw, 'tl': tl, 'ch': ch, 'pt': pt}


def rec_signals(sigs: list) -> list:
    return [[s.playbook, s.side, s.entry, s.stop, s.tp1, s.tp2, s.confidence, s.entry_type]
            for s in sigs]


# --------------------------------------------------------------------------- #
# recording                                                                   #
# --------------------------------------------------------------------------- #
def record_span(tf: str, start: str, end: str, out_dir: str) -> dict:
    from tools.forecast_build import _below_normal
    _below_normal()
    from server.forecast import sync_engine_settings
    sync_engine_settings()                         # analyse with the live settings
    from server.engine.analysis import analyse
    from server.engine.signals import generate
    from server.lab import session as S
    S.WARM = LONG_WINDOW + 100                     # history for the 1500-bar window too
    t0 = time.perf_counter()
    s = S.ReplaySession({'symbol': SYMBOL, 'tf': tf, 'start': f'{start}T00:00',
                         'end': f'{end}T00:00', 'record': False})
    path = Path(out_dir) / f'{tf}_{start}.jsonl.gz'
    part = path.with_name(path.name + '.part')     # renamed only once complete
    n = 0
    with gzip.open(part, 'wt', encoding='utf-8') as fh:
        for k in range(s.i0, s.last + 1):
            win = s.series.slice(k + 1 - S.WINDOW, k + 1)
            snap = s._analyse(k)
            row = {'k': k, 't': int(s.series.t[k]), 'atr': snap.get('atr'),
                   **rec_snapshot(snap, k + 1 - S.WINDOW)}
            if snap.get('ok'):
                row['sig'] = rec_signals(generate(snap, win))
            if (k - s.i0) % EXTRA_EVERY == 0:
                close = int(s.series.t[k]) + s.tf_ms
                w599 = s.series.slice(k + 1 - LIVE_WINDOW, k + 1)
                s599 = analyse(w599, s._mtf_at(close, k, memo=False), s.spec)
                row['live'] = rec_snapshot(s599, k + 1 - LIVE_WINDOW)
                if s599.get('ok'):
                    row['live']['sig'] = rec_signals(generate(s599, w599))
                wl = s.series.slice(k + 1 - LONG_WINDOW, k + 1)
                row['long'] = rec_snapshot(analyse(wl, None, s.spec), k + 1 - LONG_WINDOW)
            fh.write(json.dumps(row, separators=(',', ':')) + '\n')
            n += 1
    part.replace(path)
    return {'tf': tf, 'start': start, 'bars': n, 'seconds': round(time.perf_counter() - t0)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--tfs', default=','.join(TFS))
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--run-id', default=None)
    ap.add_argument('--report', default=None)
    ap.add_argument('--limit', type=int, default=None, help='first N spans only (smoke test)')
    a = ap.parse_args()
    if a.report:
        from tools.structure_audit_report import report
        txt = report(Path(a.report))
        (Path(a.report) / 'report.txt').write_text(txt, encoding='utf-8')
        print(txt)
        return 0
    from tools.forecast_build import _below_normal
    _below_normal()
    from server.forecast import engine_hash, sync_engine_settings
    sync_engine_settings()
    run_id = a.run_id or datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    d = OUT / run_id
    d.mkdir(parents=True, exist_ok=True)
    plan = [(tf, s, e) for tf in a.tfs.split(',') for s, e in spans()][:a.limit]
    todo = [p for p in plan if not (d / f'{p[0]}_{p[1]}.jsonl.gz').exists()]
    (d / 'manifest.json').write_text(json.dumps({
        'run_id': run_id, 'symbol': SYMBOL, 'tfs': a.tfs.split(','), 'spans': spans(),
        'engine': engine_hash(), 'window': 600, 'live_window': LIVE_WINDOW,
        'long_window': LONG_WINDOW, 'extra_every': EXTRA_EVERY, 'warm': LONG_WINDOW + 100,
        'created_utc': datetime.now(timezone.utc).isoformat()}, indent=1), encoding='utf-8')
    print(f'structure audit {run_id}: {len(todo)} spans on {a.workers} workers', flush=True)
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=max(1, a.workers)) as pool:
        futs = {pool.submit(record_span, tf, s, e, str(d)): (tf, s) for tf, s, e in todo}
        for f in as_completed(futs):
            tf, s = futs[f]
            try:
                r = f.result()
                print(f"  {tf} {s}: {r['bars']} bars, {r['seconds']}s "
                      f"(elapsed {time.perf_counter() - t0:.0f}s)", flush=True)
            except Exception as e:                                 # noqa: BLE001
                print(f'  [FAIL] {tf} {s}: {e!r}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
