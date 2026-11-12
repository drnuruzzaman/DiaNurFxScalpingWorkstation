#!/usr/bin/env python
"""
tools/test_structure_port.py - the chart's swing / BOS / CHoCH port matches the engine.

    py -3.12 tools/test_structure_port.py

web/src/chart/structure.ts is a port of server/engine/structure.py
find_swings() + structure_breaks(), used by the chart for the history left of
the engine's analysis window. This runs both on the bar sets in
tools/fixtures/ranges_golden.json (bundling the TS with esbuild, running it
with node) and compares every swing and break. ATR's first value is a numpy
mean in Python and a plain sum in TS, so prices and strengths are compared to
1e-6, not bit for bit.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.engine import structure as st          # noqa: E402
from server.engine.indicators import atr           # noqa: E402

GOLDEN = ROOT / 'tools' / 'fixtures' / 'ranges_golden.json'
PASSED, FAILED = [], []


def check(name, ok, detail=''):
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def ts_side(sets):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        esb = ROOT / 'web' / 'node_modules' / '.bin' / ('esbuild.cmd' if sys.platform == 'win32' else 'esbuild')
        subprocess.run([str(esb), str(ROOT / 'web' / 'src' / 'chart' / 'structure.ts'), '--bundle',
                        '--format=esm', '--platform=node', f'--outfile={td / "s.mjs"}',
                        '--log-level=warning'], check=True)
        (td / 'in.json').write_text(json.dumps([s['bars'] for s in sets]))
        (td / 'run.mjs').write_text(
            "import { findSwings, structureBreaks } from './s.mjs'\n"
            "import fs from 'fs'\n"
            "const sets = JSON.parse(fs.readFileSync(new URL('./in.json', import.meta.url)))\n"
            "const out = sets.map((b) => { const sw = findSwings(b); return { sw, br: structureBreaks(sw, b) } })\n"
            "fs.writeFileSync(new URL('./out.json', import.meta.url), JSON.stringify(out))\n")
        subprocess.run(['node', str(td / 'run.mjs')], check=True)
        return json.loads((td / 'out.json').read_text())


def same(a: dict, b: dict, fields) -> bool:
    for f in fields:
        x, y = a[f], b[f]
        if isinstance(x, float) or isinstance(y, float):
            if abs(float(x) - float(y)) > 1e-6:
                return False
        elif x != y:
            return False
    return True


def main() -> int:
    sets = json.loads(GOLDEN.read_text())
    ts = ts_side(sets)
    sw_f = ('idx', 't', 'price', 'kind', 'confirmed_at', 'strength', 'label', 'swept')
    br_f = ('idx', 't', 'price', 'kind', 'direction', 'from_swing')
    for s, got in zip(sets, ts):
        b = s['bars']
        h = np.array([x['h'] for x in b]); l = np.array([x['l'] for x in b])
        c = np.array([x['c'] for x in b]); t = np.array([x['t'] for x in b])
        sw = st.find_swings(h, l, c, t, 2, 0.55, atr(h, l, c, 14), 10 ** 9)
        br = st.structure_breaks(sw, h, l, c, t)
        py_sw = [x.to_dict() for x in sw]
        py_br = [x.to_dict() for x in br]
        ok_sw = len(py_sw) == len(got['sw']) and all(same(p, q, sw_f) for p, q in zip(py_sw, got['sw']))
        ok_br = len(py_br) == len(got['br']) and all(same(p, q, br_f) for p, q in zip(py_br, got['br']))
        d1 = f"{len(py_sw)} swings (ts {len(got['sw'])})"
        if not ok_sw:
            k = next((i for i, (p, q) in enumerate(zip(py_sw, got['sw'])) if not same(p, q, sw_f)), None)
            if k is not None:
                d1 += f'; first diff #{k}: py {py_sw[k]} ts {got["sw"][k]}'
        check(f"swings: {s['name']}", ok_sw, d1)
        kinds = {k: sum(1 for x in py_br if x['kind'] == k) for k in ('BOS', 'CHoCH')}
        check(f"breaks: {s['name']}", ok_br, f"{len(py_br)} breaks {kinds} (ts {len(got['br'])})")
    print(f'\n{len(PASSED)} passed, {len(FAILED)} failed')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
