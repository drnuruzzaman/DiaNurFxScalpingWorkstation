#!/usr/bin/env python
"""
tools/structure_labels.py - Step 0B: a trader's verdict on what the engine draws.

    python tools/structure_labels.py --sample      pick the moments (once - never re-run)
    python tools/structure_labels.py --prepare     the engine's objects at every moment
    python tools/structure_labels.py --score       precision / recall from the verdicts

The audit (tools/structure_audit.py) measures CONSISTENCY - does a line flicker,
does a trigger sit past its own invalidation. Only a trader can say whether
what is drawn is RIGHT. The backtest lab's "Label structure" view shows each
moment's chart up to that bar - nothing after it - with the engine's swings,
trendlines, channels and patterns, and records for each

    right    I would draw this
    close    I would draw it, with other anchors / points
    wrong    I would not draw this

plus anything the engine MISSED, drawn in by hand.

    precision  = right / judged            (and "right or close" / judged)
    recall     = right / (right + missed)

Moments: gold 5m and 15m, four random bars from each of the audit's 18 spans
(2018-2026, Feb + Aug), seeded, so 144 in all. The queue is ordered in four
rounds, each covering every span once on both timeframes - label any prefix
and the years stay balanced.

Files
    labels/structure/moments.json        the sample (tracked; written once)
    labels/structure/labels.jsonl        the verdicts (tracked; append-only,
                                         the newest record for a moment wins)
    runs/audit/labels/<engine>/          prepared snapshots, one per moment
                                         (rebuildable: --prepare)

When the engine changes (Steps 1-4), --prepare writes a new snapshot set under
the new engine hash, and --score --engine <hash> matches the old verdicts to
the new objects: same key = same verdict; a new object matching something the
trader drew in as MISSED counts as right; anything else is "unjudged" and the
view asks for it.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.structure_audit import SYMBOL, TFS, spans   # noqa: E402  the same fixed spans

LABEL_DIR = ROOT / 'labels' / 'structure'
MOMENTS = LABEL_DIR / 'moments.json'
LABELS = LABEL_DIR / 'labels.jsonl'
PREP_DIR = ROOT / 'runs' / 'audit' / 'labels'
SEED = 20261002
PER_SPAN = 4
WINDOW = 600
TL_ON_CHART, PT_FEATURED = 5, 2          # what ChartEngine draws / features
MATCH_BARS = 3                           # a drawn-in object matches within this many bars


# --------------------------------------------------------------------------- #
# keys - an object's identity, by bar TIMES so any engine version can match   #
# --------------------------------------------------------------------------- #
def key_swing(t, kind) -> str:
    return f"sw:{int(t)}:{'H' if kind in ('high', 'H') else 'L'}"


def key_line(kind, t1, t2) -> str:
    return f"tl:{kind[0]}:{int(t1)}:{int(t2)}"


def key_channel(kind, t1, slope_atr) -> str:
    return f"ch:{kind}:{int(t1)}:{slope_atr:+.3f}"


def key_pattern(kind, points) -> str:
    return f"pt:{kind}:" + ','.join(str(int(p['t'])) for p in points)


# --------------------------------------------------------------------------- #
# sampling                                                                    #
# --------------------------------------------------------------------------- #
def sample() -> dict:
    if MOMENTS.exists():
        raise SystemExit(f'{MOMENTS} exists - the sample is fixed; delete it by hand to redraw')
    from server.lab import data as lab_data
    from server.lab.session import parse_ms
    from server.config import TF_SECONDS
    per = {}                                  # (tf, span index) -> [moments]
    sp = spans()
    for tf in TFS:
        for si, (start, end) in enumerate(sp):
            s, i0 = lab_data.load(SYMBOL, tf, parse_ms(f'{start}T00:00'),
                                  parse_ms(f'{end}T00:00'), WINDOW)
            if i0 < WINDOW - 1 or len(s) - i0 < PER_SPAN:
                raise SystemExit(f'{tf} {start}: not enough bars on disk')
            rng = random.Random(f'{SEED}:{tf}:{start}')
            ks = sorted(rng.sample(range(i0, len(s)), PER_SPAN))
            rng.shuffle(ks)                   # round order inside the span
            per[(tf, si)] = [{'tf': tf, 't': int(s.t[k]), 'span': start,
                              'year': int(start[:4])} for k in ks]
    order = list(range(len(sp)))
    random.Random(f'{SEED}:order').shuffle(order)
    out = []
    for r in range(PER_SPAN):
        for si in order:
            for tf in TFS:
                m = dict(per[(tf, si)][r])
                m['id'] = f"{tf}-{m['t']}"
                m['round'] = r + 1
                out.append(m)
    doc = {'symbol': SYMBOL, 'seed': SEED, 'per_span': PER_SPAN, 'spans': sp,
           'created_utc': datetime.now(timezone.utc).isoformat(), 'moments': out,
           'note': 'Fixed before any verdict existed. Do not regenerate.'}
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    MOMENTS.write_text(json.dumps(doc, indent=1), encoding='utf-8')
    return doc


def moments() -> list:
    return json.loads(MOMENTS.read_text(encoding='utf-8'))['moments']


# --------------------------------------------------------------------------- #
# preparing - the engine at each moment, as the chart would show it           #
# --------------------------------------------------------------------------- #
def snapshot_at(m: dict) -> dict:
    """The 600 closed bars up to the moment's bar, and every object analyse() returns."""
    from server.engine.analysis import analyse
    from server.lab import data as lab_data
    from server.config import TF_SECONDS
    tf_ms = TF_SECONDS[m['tf']] * 1000
    s, j = lab_data.load(SYMBOL, m['tf'], m['t'], m['t'] + tf_ms, WINDOW)
    if int(s.t[j]) != m['t']:
        raise RuntimeError(f"{m['id']}: bar not found")
    w = s.slice(j + 1 - WINDOW, j + 1)
    snap = analyse(w, None, lab_data.spec(SYMBOL))
    if not snap.get('ok'):
        raise RuntimeError(f"{m['id']}: analyse failed: {snap.get('reason')}")
    a = float(snap['atr'])
    t = [int(x) for x in w.t]
    lines = sorted(snap.get('trendlines') or [], key=lambda x: -x['score'])
    on_chart = {id(x) for x in lines[:TL_ON_CHART]}
    tls = [{'key': key_line(x['kind'], x['t1'], x['t2']), 'kind': x['kind'],
            'x1': x['x1'], 'y1': x['y1'], 'x2': x['x2'], 'y2': x['y2'], 'slope': x['slope'],
            'touches': x['touches'], 'touch_x': x['touch_idx'], 'score': x['score'],
            'broken': x['broken'], 'on_chart': id(x) in on_chart}
           for x in lines]
    chs = [{'key': key_channel(x['kind'], x['t1'], x['slope'] / a), 'kind': x['kind'],
            'x1': x['upper']['x1'], 'slope': x['slope'],
            'upper': x['upper']['y1'], 'lower': x['lower']['y1'],
            'containment': x['containment'], 'score': x['score']}
           for x in snap.get('channels') or []]
    pats = snap.get('patterns') or []
    feat = {id(p) for p in sorted((p for p in pats if p['actionable']),
                                  key=lambda p: -p['relevance'])[:PT_FEATURED]}
    pts = [{'key': key_pattern(p['kind'], p['points']), 'kind': p['kind'], 'label': p['label'],
            'direction': p['direction'], 'status': p['status'], 'quality': p['quality'],
            'points': [{'x': t.index(int(q['t'])) if int(q['t']) in t else None,
                        't': int(q['t']), 'price': q['price'], 'role': q['role']}
                       for q in p['points']],
            'break_level': p['break_level'], 'target': p['target'],
            'invalidation': p['invalidation'], 'actionable': p['actionable'],
            'relevance': p['relevance'], 'featured': id(p) in feat}
           for p in pats]
    sws = [{'key': key_swing(x['t'], x['kind']), 'x': x['idx'], 'kind': x['kind'],
            'price': x['price'], 'label': x.get('label', ''), 'strength': x.get('strength', 0)}
           for x in snap.get('swings') or []]
    return {'id': m['id'], 'tf': m['tf'], 't': m['t'], 'symbol': SYMBOL, 'atr': a,
            'bars': {'t': t, 'o': [float(x) for x in w.o], 'h': [float(x) for x in w.h],
                     'l': [float(x) for x in w.l], 'c': [float(x) for x in w.c]},
            'swings': sws, 'trendlines': tls, 'channels': chs, 'patterns': pts}


def prepare() -> Path:
    from server.forecast import engine_hash, sync_engine_settings
    sync_engine_settings()
    eng = engine_hash()
    d = PREP_DIR / eng
    d.mkdir(parents=True, exist_ok=True)
    ms = moments()
    for i, m in enumerate(ms, 1):
        snap = snapshot_at(m)
        snap['engine'] = eng
        (d / f"{m['id']}.json").write_text(json.dumps(snap, separators=(',', ':')), encoding='utf-8')
        if i % 24 == 0 or i == len(ms):
            print(f'  prepared {i}/{len(ms)}', flush=True)
    (d / 'prepared.json').write_text(json.dumps({
        'engine': eng, 'n': len(ms), 'created_utc': datetime.now(timezone.utc).isoformat()}),
        encoding='utf-8')
    print(f'prepared {len(ms)} moments -> {d}')
    return d


# --------------------------------------------------------------------------- #
# verdicts                                                                    #
# --------------------------------------------------------------------------- #
def latest_labels() -> dict:
    """moment id -> its newest record."""
    out = {}
    try:
        with LABELS.open(encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    out[r['id']] = r
    except FileNotFoundError:
        pass
    return out


def _near_missed(obj_type: str, obj: dict, snap: dict, missed: list) -> bool:
    """Does an engine object match something the trader drew in as missed?"""
    bt = snap['bars']['t']

    def bar_of(ms):
        try:
            return bt.index(int(ms))
        except ValueError:
            return None

    for m in missed:
        if m.get('type') != obj_type:
            continue
        if obj_type == 'trendline':
            if m.get('kind') != obj['kind']:
                continue
            a, b = bar_of(m['p1']['t']), bar_of(m['p2']['t'])
            if a is None or b is None:
                continue
            if abs(a - obj['x1']) <= MATCH_BARS and abs(b - obj['x2']) <= MATCH_BARS:
                return True
        elif obj_type == 'pattern':
            if m.get('kind') != obj['kind'] or len(m.get('points') or []) != len(obj['points']):
                continue
            xs = [bar_of(q['t']) for q in m['points']]
            if all(x is not None and p['x'] is not None and abs(x - p['x']) <= MATCH_BARS
                   for x, p in zip(xs, obj['points'])):
                return True
        elif obj_type == 'swing':
            x = bar_of(m['t'])
            if x is not None and m.get('kind') == obj['kind'][0].upper() and abs(x - obj['x']) <= 1:
                return True
    return False


def score(engine: str = None) -> str:
    labels = latest_labels()
    if not labels:
        return 'no verdicts yet - label some moments in the lab (Backtest > Label structure)'
    eng = engine or Counter(r.get('engine') for r in labels.values()).most_common(1)[0][0]
    d = PREP_DIR / eng
    if not d.exists():
        raise SystemExit(f'no prepared set for engine {eng} (python tools/structure_labels.py --prepare)')
    acc = defaultdict(Counter)        # (tf, type) -> counts
    kinds = defaultdict(Counter)      # (tf, pattern kind) -> counts
    n_moments = Counter()
    for mid, r in labels.items():
        if r.get('skipped'):
            continue
        p = d / f'{mid}.json'
        if not p.exists():
            continue
        snap = json.loads(p.read_text(encoding='utf-8'))
        tf, verdicts, missed = snap['tf'], r.get('verdicts') or {}, r.get('missed') or []
        n_moments[tf] += 1
        groups = [('swing', snap['swings'], 'swings'), ('trendline', snap['trendlines'], 'trendlines'),
                  ('channel', snap['channels'], 'channels'), ('pattern', snap['patterns'], 'patterns')]
        in_view = set(r.get('swings_in_view') or [])
        for typ, objs, name in groups:
            for o in objs:
                if typ == 'swing' and o['key'] not in in_view:
                    continue
                v = verdicts.get(o['key'])
                if v is None and typ == 'swing' and o['key'] in in_view:
                    v = 'right'                   # unmarked swings in view count as right
                if v is None and typ in ('trendline', 'pattern', 'swing') and \
                        _near_missed(typ, o, snap, missed):
                    v = 'right'
                c = acc[(tf, name)]
                if v is None:
                    c['unjudged'] += 1
                    continue
                c[v] += 1
                if typ == 'pattern':
                    kinds[(tf, o['kind'])][v] += 1
                if typ == 'trendline' and o.get('on_chart'):
                    acc[(tf, 'trendlines on chart')][v] += 1
                if typ == 'pattern' and o.get('featured'):
                    acc[(tf, 'featured patterns')][v] += 1
            # missed objects that no engine object of this version matches
            matched_missed = 0
            for m in missed:
                if m.get('type') != typ:
                    continue
                if any(_near_missed(typ, o, snap, [m]) for o in objs):
                    matched_missed += 1
                    continue
                acc[(tf, name)]['missed'] += 1
    L = [f'STRUCTURE LABELS  engine {eng}   moments judged: ' +
         ', '.join(f'{tf} {n}' for tf, n in sorted(n_moments.items())), '',
         f"  {'':<26}{'judged':>8}{'right':>8}{'close':>8}{'wrong':>8}{'missed':>8}"
         f"{'precision':>11}{'p incl close':>14}{'recall':>8}{'unjudged':>10}"]
    for tf in sorted(n_moments):
        L.append(f'  {tf}')
        for name in ('swings', 'trendlines', 'trendlines on chart', 'channels', 'patterns',
                     'featured patterns'):
            c = acc[(tf, name)]
            j = c['right'] + c['close'] + c['wrong']
            prec = f"{c['right'] / j:.2f}" if j else '-'
            pinc = f"{(c['right'] + c['close']) / j:.2f}" if j else '-'
            rec = (f"{c['right'] / (c['right'] + c['missed']):.2f}"
                   if c['right'] + c['missed'] else '-')
            L.append(f"  {name:<26}{j:>8}{c['right']:>8}{c['close']:>8}{c['wrong']:>8}"
                     f"{c['missed']:>8}{prec:>11}{pinc:>14}{rec:>8}{c['unjudged']:>10}")
        L.append('')
    L.append('  patterns by kind (right / close / wrong)')
    for (tf, kind), c in sorted(kinds.items()):
        L.append(f"  {tf:<5}{kind:<26}{c['right']:>5}{c['close']:>6}{c['wrong']:>6}")
    return '\n'.join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--sample', action='store_true')
    ap.add_argument('--prepare', action='store_true')
    ap.add_argument('--score', action='store_true')
    ap.add_argument('--engine', default=None)
    a = ap.parse_args()
    if a.sample:
        doc = sample()
        print(f"sampled {len(doc['moments'])} moments -> {MOMENTS}")
    if a.prepare:
        prepare()
    if a.score:
        print(score(a.engine))
    if not (a.sample or a.prepare or a.score):
        ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
