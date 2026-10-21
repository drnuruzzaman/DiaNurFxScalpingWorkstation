#!/usr/bin/env python
"""
tools/market_batch.py - every playbook on every timeframe of one market, in the lab.

The research base for a market the robot does not trade yet: a headless lab
replay (auto mode, the live executor against the minute-path broker, closed
bars only) of ALL seven playbooks on each timeframe, one job per timeframe and
year, several at a time at below-normal priority. Each job is
tools/trade_journal_backfill.py `window`, so the output is the same
runs/research/journal/win_<tag>-<year>_<tf>.json the gold tables read.

Frozen once, recorded with the run (runs/research/batches/<tag>/manifest.json):
  - the settings: today's live settings with every playbook switched on,
    written to settings.json and passed to every job as the FULL override, so
    an edit to configs/settings.json while the batch runs cannot reach it;
  - the lab fidelity (server/lab/session.py LAB_FIDELITY) and the spread floor;
  - the contract facts (spec.json): priced as a USD account - tick value from
    tick size x contract size for a USD-quoted symbol, commission per lot side
    as configured - because data/<symbol>/spec.json follows whichever account
    the bridge is attached to (2026-10-20 it switched from the USD demo to an
    AUD live account mid-batch). The account's swap terms are recorded beside
    it for a swap stress run; whether swap is charged is the settings' call
    (instrument.swap_free).
Jobs whose output exists are skipped, so a stopped batch resumes where it was.

    python tools/market_batch.py --symbol EURUSD.a --tag eurusd --start 2020-06-01
    python tools/market_batch.py --tag eurusd --status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JOURNAL = ROOT / 'runs' / 'research' / 'journal'
BATCHES = ROOT / 'runs' / 'research' / 'batches'
TFS = ('4h', '2h', '1h', '30m', '15m', '5m', '3m')      # cheapest first: results arrive early


def freeze(tag: str, symbol: str, fidelity: int, spread_floor) -> Path:
    from server.lab import settings as lab_settings
    from server.lab.session import LAB_FIDELITY
    folder = BATCHES / tag
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'settings.json'
    if path.exists():
        return path                                     # a resumed batch keeps its settings
    eff = lab_settings.jsonable(lab_settings.effective({'gates': {'disabled_playbooks': []}}))
    body = json.dumps(eff, sort_keys=True)
    doc = {'symbol': symbol, 'made': datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'sha': hashlib.sha256(body.encode()).hexdigest()[:12],
           'fidelity': fidelity or LAB_FIDELITY, 'spread_floor_points': spread_floor,
           'note': 'live settings at freeze time, every playbook enabled (lab override)',
           'settings': eff}
    path.write_text(json.dumps(doc, indent=1), encoding='utf-8')
    return path


def freeze_spec(tag: str, symbol: str) -> Path:
    from server.lab import data as lab_data
    path = BATCHES / tag / 'spec.json'
    if path.exists():
        return path
    disk = lab_data.spec(symbol)
    pinned = {'commission_per_lot_side': float(disk.get('commission_per_lot_side') or 0.0)}
    if disk.get('currency_profit') in (None, 'USD'):
        pinned['tick_value'] = float(disk['tick_size']) * float(disk['contract_size'])
    doc = {'symbol': symbol, 'account_currency': 'USD' if 'tick_value' in pinned else 'as on disk',
           'pinned': pinned,
           'swap_terms_seen': {k: disk.get(k) for k in ('swap_mode', 'swap_long_points',
                                                        'swap_short_points',
                                                        'swap_rollover_3days_weekday')},
           'disk_spec_source': disk.get('source'),
           'made': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    path.write_text(json.dumps(doc, indent=1), encoding='utf-8')
    return path


def jobs(tag: str, symbol: str, start: str, end: str | None) -> list:
    a = datetime.fromisoformat(start)
    b = datetime.fromisoformat(end) if end else datetime.now() - timedelta(days=1)
    out = []
    for tf in TFS:
        y = a.year
        while datetime(y, 1, 1) < b:
            p0 = max(a, datetime(y, 1, 1))
            p1 = min(b, datetime(y + 1, 1, 1))
            if p0 < p1:
                out.append({'tf': tf, 'year': y, 'start': p0.strftime('%Y-%m-%d'),
                            'end': p1.strftime('%Y-%m-%d'), 'tag': f'{tag}-{y}'})
            y += 1
    return out


def out_path(job: dict) -> Path:
    return JOURNAL / f"win_{job['tag']}_{job['tf']}.json"


def run(job: dict, symbol: str, settings: Path, spec: Path, fidelity, spread_floor) -> tuple:
    cmd = [sys.executable, '-u', str(ROOT / 'tools' / 'trade_journal_backfill.py'), 'window',
           '--start', job['start'], '--end', job['end'], '--symbol', symbol,
           '--tf', job['tf'], '--tag', job['tag'], '--all-playbooks',
           '--settings', str(settings), '--spec', str(spec)]
    if fidelity:
        cmd += ['--fidelity', str(fidelity)]
    if spread_floor is not None:
        cmd += ['--spread-floor', str(spread_floor)]
    log = JOURNAL / f"win_{job['tag']}_{job['tf']}.log"
    t0 = time.time()
    with open(log, 'w', encoding='utf-8') as fh:
        code = subprocess.call(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)
    return job, code, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--symbol', default=None)
    ap.add_argument('--tag', required=True, help='names the outputs: win_<tag>-<year>_<tf>.json')
    ap.add_argument('--start', default=None, help='first decision day, YYYY-MM-DD')
    ap.add_argument('--end', default=None, help='last day (default: yesterday)')
    ap.add_argument('--workers', type=int, default=5)
    ap.add_argument('--fidelity', type=int, default=None)
    ap.add_argument('--spread-floor', type=float, default=None)
    ap.add_argument('--status', action='store_true', help='report progress and exit')
    a = ap.parse_args()

    man_path = BATCHES / a.tag / 'manifest.json'
    if a.status or not a.symbol:
        man = json.loads(man_path.read_text(encoding='utf-8'))
        todo = [j for j in man['jobs'] if not out_path(j).exists()]
        print(f"{a.tag}: {len(man['jobs']) - len(todo)} of {len(man['jobs'])} jobs done")
        for j in todo:
            log = JOURNAL / f"win_{j['tag']}_{j['tf']}.log"
            lines = log.read_text(encoding='utf-8', errors='replace').strip().splitlines() \
                if log.exists() else []
            state = lines[-1].strip() if lines else ('starting' if log.exists() else 'queued')
            print(f"  {j['tf']:>4} {j['year']}: {state[:100]}")
        return 0

    settings = freeze(a.tag, a.symbol, a.fidelity, a.spread_floor)
    spec = freeze_spec(a.tag, a.symbol)
    work = jobs(a.tag, a.symbol, a.start, a.end)
    man = {'tag': a.tag, 'symbol': a.symbol, 'start': a.start, 'end': a.end,
           'settings': str(settings.relative_to(ROOT)),
           'spec': str(spec.relative_to(ROOT)),
           'pinned_spec': json.loads(spec.read_text(encoding='utf-8'))['pinned'],
           'settings_sha': json.loads(settings.read_text(encoding='utf-8'))['sha'],
           'jobs': work}
    man_path.write_text(json.dumps(man, indent=1), encoding='utf-8')
    todo = [j for j in work if not out_path(j).exists()]
    print(f'{a.tag}: {len(todo)} of {len(work)} jobs to run, {a.workers} at a time; '
          f'settings {man["settings_sha"]}', flush=True)
    failed = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        for job, code, secs in pool.map(
                lambda j: run(j, a.symbol, settings, spec, a.fidelity, a.spread_floor), todo):
            ok = code == 0 and out_path(job).exists()
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {job['tf']:>4} {job['year']}  {secs:6.0f}s",
                  flush=True)
    print(f'done: {len(todo) - failed} ok, {failed} failed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
