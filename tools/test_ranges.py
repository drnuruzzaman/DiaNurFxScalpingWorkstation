#!/usr/bin/env python
"""
tools/test_ranges.py - the engine's range finder matches the chart's.

    python tools/test_ranges.py            # check
    python tools/test_ranges.py --regen    # rebuild the golden file from the TS

server/engine/ranges.py is a port of web/src/chart/ranges.ts. The golden file
holds fixed bar sets and the boxes the TS returns for them; the Python must
return the same boxes, field for field (prices to 1e-9).

--regen bundles ranges.ts with web/node_modules/.bin/esbuild and runs it with
node, on the bar sets listed in SETS (cut from data/<symbol>/<tf>/<year>.csv.gz
by end time, so the file does not drift as new data arrives). Run it after a
deliberate change to ranges.ts, then port the change and rerun the check.

Also checked: a range found on a prefix of the bars never uses a bar after the
prefix (point in time).
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.engine.ranges import find_ranges  # noqa: E402

GOLDEN = ROOT / 'tools' / 'fixtures' / 'ranges_golden.json'
# (symbol, tf, year, last bar strictly before, bars)
SETS = [
    ('XAUUSD.a', '1h', 2026, '2026-11-07', 900),
    ('XAUUSD.a', '4h', 2026, '2026-11-07', 900),
    ('XAUUSD.a', '15m', 2026, '2026-11-07', 900),
    ('XAUUSD.a', '1h', 2026, '2026-06-01', 900),
    ('USDJPY.a', '1h', 2026, '2026-11-07', 900),
]
FIELDS = ('i0', 'i1', 't0', 't1', 'top', 'bot', 'tier', 'active', 'breakout',
          'strong', 'touchesTop', 'touchesBot')

PASSED, FAILED = [], []


def check(name, ok, detail=''):
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def load_bars(sym, tf, year, before, count):
    from datetime import datetime, timezone
    cut = int(datetime.fromisoformat(before).replace(tzinfo=timezone.utc).timestamp())
    rows = []
    with gzip.open(ROOT / 'data' / sym / tf / f'{year}.csv.gz', 'rt') as f:
        next(f)
        for line in f:
            p = line.split(',')
            if int(p[0]) >= cut:
                break
            rows.append({'t': int(p[0]) * 1000, 'o': float(p[1]), 'h': float(p[2]),
                         'l': float(p[3]), 'c': float(p[4]), 'v': 0})
    return rows[-count:]


def regen():
    sets = []
    for sym, tf, year, before, count in SETS:
        sets.append({'name': f'{sym} {tf} <{before}', 'bars': load_bars(sym, tf, year, before, count)})
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        esb = ROOT / 'web' / 'node_modules' / '.bin' / ('esbuild.cmd' if sys.platform == 'win32' else 'esbuild')
        subprocess.run([str(esb), str(ROOT / 'web' / 'src' / 'chart' / 'ranges.ts'), '--bundle',
                        '--format=esm', '--platform=node', f'--outfile={td / "ranges.mjs"}',
                        '--log-level=warning'], check=True)
        (td / 'in.json').write_text(json.dumps(sets))
        (td / 'run.mjs').write_text(
            "import { findRanges } from './ranges.mjs'\n"
            "import fs from 'fs'\n"
            "const sets = JSON.parse(fs.readFileSync(new URL('./in.json', import.meta.url)))\n"
            "for (const s of sets) s.expect = findRanges(s.bars)\n"
            "fs.writeFileSync(new URL('./out.json', import.meta.url), JSON.stringify(sets))\n")
        subprocess.run(['node', str(td / 'run.mjs')], check=True)
        out = json.loads((td / 'out.json').read_text())
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(out, separators=(',', ':')))
    for s in out:
        print(f"  {s['name']}: {len(s['bars'])} bars, {len(s['expect'])} ranges")
    print(f'wrote {GOLDEN.relative_to(ROOT)}')


def same(a: dict, b: dict) -> bool:
    for f in FIELDS:
        x, y = a.get(f), b.get(f)
        if isinstance(x, float) or isinstance(y, float):
            if x is None or y is None or abs(x - y) > 1e-9:
                return False
        elif x != y:
            return False
    return True


def run():
    sets = json.loads(GOLDEN.read_text())
    for s in sets:
        b = s['bars']
        got = [r.to_dict() for r in find_ranges([x['t'] for x in b], [x['h'] for x in b],
                                                [x['l'] for x in b], [x['c'] for x in b])]
        exp = s['expect']
        ok = len(got) == len(exp) and all(same(g, e) for g, e in zip(got, exp))
        detail = f'{len(got)} ranges'
        if not ok:
            for k, (g, e) in enumerate(zip(got, exp)):
                if not same(g, e):
                    detail += f'; first diff at #{k}: py {g} ts {e}'
                    break
            else:
                detail += f' (ts {len(exp)})'
        check(f"parity: {s['name']}", ok, detail)
        tiers = {t: sum(1 for r in got if r['tier'] == t) for t in ('major', 'minor', 'developing')}
        check(f"non-trivial: {s['name']}", len(got) > 0, str(tiers))

    # Point in time: on a prefix, no box reaches past the prefix.
    b = sets[0]['bars']
    for cut in (300, 450, 600, 750):
        p = b[:cut]
        rs = find_ranges([x['t'] for x in p], [x['h'] for x in p], [x['l'] for x in p], [x['c'] for x in p])
        check(f'point in time: prefix {cut}', all(r.i1 <= cut - 1 for r in rs),
              f'{len(rs)} ranges, {sum(r.active for r in rs)} active')


if __name__ == '__main__':
    if '--regen' in sys.argv:
        regen()
    else:
        run()
        print(f'\n{len(PASSED)} passed, {len(FAILED)} failed')
        sys.exit(1 if FAILED else 0)
