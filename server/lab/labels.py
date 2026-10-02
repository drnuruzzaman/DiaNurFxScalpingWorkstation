"""
server/lab/labels.py - Step 0B: serve label moments, store a trader's verdicts.

The moments and their engine snapshots are prepared OFFLINE by
tools/structure_labels.py, under the live settings. Not here: this process
puts each backtest session's settings onto its CONFIG, so analysing in it
would label whatever the open session overrides rather than the engine that
trades. Here we only read the prepared files and append verdicts.

    labels/structure/moments.json        the fixed sample
    labels/structure/labels.jsonl        verdicts, append-only, newest wins
    runs/audit/labels/<engine>/<id>.json what the engine drew at each moment
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL_DIR = ROOT / 'labels' / 'structure'
MOMENTS = LABEL_DIR / 'moments.json'
LABELS = LABEL_DIR / 'labels.jsonl'
PREP_DIR = ROOT / 'runs' / 'audit' / 'labels'
VERDICTS = ('right', 'close', 'wrong')
MISSED_TYPES = ('swing', 'trendline', 'pattern')
_LOCK = threading.Lock()


def prepared_dir() -> Path | None:
    """The newest prepared snapshot set."""
    best, when = None, ''
    try:
        for d in PREP_DIR.iterdir():
            p = d / 'prepared.json'
            if p.exists():
                c = json.loads(p.read_text(encoding='utf-8')).get('created_utc', '')
                if c > when:
                    best, when = d, c
    except OSError:
        return None
    return best


def _moments() -> list:
    try:
        return json.loads(MOMENTS.read_text(encoding='utf-8'))['moments']
    except (OSError, ValueError, KeyError):
        return []


def _latest() -> dict:
    out = {}
    try:
        with LABELS.open(encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                out[r.get('id')] = r
    except FileNotFoundError:
        pass
    return out


def queue() -> dict:
    d = prepared_dir()
    lab = _latest()
    rows = []
    for m in _moments():
        r = lab.get(m['id'])
        rows.append({**m, 'done': bool(r) and not r.get('skipped'),
                     'skipped': bool(r and r.get('skipped')),
                     'saved_utc': r.get('saved_utc') if r else None})
    return {'engine': d.name if d else None, 'prepared': d is not None, 'moments': rows,
            'done': sum(1 for x in rows if x['done']),
            'skipped': sum(1 for x in rows if x['skipped']),
            'hint': None if rows and d else
            'python tools/structure_labels.py --sample --prepare'}


def moment(mid: str) -> dict:
    d = prepared_dir()
    if d is None:
        raise FileNotFoundError('no prepared moments - python tools/structure_labels.py --prepare')
    if not any(m['id'] == mid for m in _moments()):
        raise KeyError(mid)
    snap = json.loads((d / f'{mid}.json').read_text(encoding='utf-8'))
    snap['label'] = _latest().get(mid)
    return snap


def _clean_point(p) -> dict:
    return {'t': int(p['t']), 'price': float(p['price'])}


def save(mid: str, body: dict) -> dict:
    d = prepared_dir()
    if d is None or not any(m['id'] == mid for m in _moments()):
        raise KeyError(mid)
    verdicts = {str(k): v for k, v in (body.get('verdicts') or {}).items() if v in VERDICTS}
    missed = []
    for m in body.get('missed') or []:
        typ = m.get('type')
        if typ not in MISSED_TYPES:
            continue
        if typ == 'swing':
            missed.append({'type': typ, 'kind': 'H' if m.get('kind') == 'H' else 'L',
                           't': int(m['t']), 'price': float(m['price'])})
        elif typ == 'trendline':
            missed.append({'type': typ,
                           'kind': 'support' if m.get('kind') == 'support' else 'resistance',
                           'p1': _clean_point(m['p1']), 'p2': _clean_point(m['p2'])})
        else:
            pts = [_clean_point(p) for p in m.get('points') or []]
            if len(pts) >= 2:
                missed.append({'type': typ, 'kind': str(m.get('kind') or '')[:40],
                               'points': pts})
    rec = {'id': mid, 'engine': d.name, 'saved_utc': datetime.now(timezone.utc).isoformat(),
           'skipped': bool(body.get('skipped')), 'verdicts': verdicts, 'missed': missed,
           'swings_in_view': [str(k) for k in body.get('swings_in_view') or []],
           'view_bars': int(body.get('view_bars') or 0),
           'note': str(body.get('note') or '')[:2000],
           'seconds': round(float(body.get('seconds') or 0), 1)}
    with _LOCK:
        LABEL_DIR.mkdir(parents=True, exist_ok=True)
        with LABELS.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, separators=(',', ':')) + '\n')
    return {'ok': True, 'saved_utc': rec['saved_utc']}


__all__ = ['queue', 'moment', 'save', 'prepared_dir']
