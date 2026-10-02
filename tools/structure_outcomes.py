#!/usr/bin/env python
"""
tools/structure_outcomes.py - Step 0C: does what the engine draws predict anything?

    python tools/structure_outcomes.py runs/audit/structure/<run>

Reads a structure audit run (tools/structure_audit.py) and, for three things
the chart claims, compares what price did next with what it does from a
random bar facing the SAME barriers:

    TOUCH   price touches an unbroken trendline the chart draws (wick within
            0.35 ATR of it, close still on its side). Claim: it bounces.
            Win = 1 ATR away from the line before 1 ATR through it.
    BREAK   the engine flips a line to broken (two closes beyond it).
            Claim: price follows through. Win = 1 ATR further before 1 ATR back.
    TARGET  a pattern is confirmed (first seen confirmed, or forming ->
            confirmed). Claim: it reaches its target before its invalidation.

Everything is measured from the CLOSE of the bar the event became known on,
using only what the engine had shown by then. A bar that hits both barriers
counts as a loss. Unresolved after the horizon = left out (and counted).

Baselines:
    random  every bar of the same span, same direction, same barrier distances
            in ATR - what a coin-flip entry facing the same barriers gets
    walk    TARGET only: d_stop / (d_target + d_stop), the hit rate of a
            driftless random walk - asymmetric barriers are not an edge

Edge = event rate - random rate; the interval is +/- 1.96 binomial standard
errors of the event rate (events overlap in time, so read it as optimistic).
By year: the same, per year - an edge that changes sign between years is not
one. Measurements, not trading advice.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.structure_audit_report import _rows, TL_ON_CHART   # noqa: E402

HORIZON = 300            # bars to resolve
TOUCH_TOL, BREAK_VIOL = 0.35, 0.45
BAR_MS = {'5m': 300_000, '15m': 900_000}


def _series_ext(symbol: str, tf: str, start: str, end: str, warm: int):
    """The audit's series, plus enough bars after the span for every outcome."""
    from server.lab import data as lab_data
    from server.lab.session import parse_ms
    end_ms = parse_ms(f'{end}T00:00') + (HORIZON + 50) * BAR_MS[tf] * 2   # weekends
    s, _ = lab_data.load(symbol, tf, parse_ms(f'{start}T00:00'), end_ms, warm)
    return s


def first_passage(h, l, k: int, up: float, dn: float, horizon: int = HORIZON):
    """+1 = `up` reached first after bar k, -1 = `dn` first (or both in one bar), 0 = neither."""
    hs = h[k + 1:k + 1 + horizon]
    ls = l[k + 1:k + 1 + horizon]
    if not hs.size:
        return 0
    hu = np.flatnonzero(hs >= up)
    ld = np.flatnonzero(ls <= dn)
    iu = hu[0] if hu.size else None
    idn = ld[0] if ld.size else None
    if iu is None and idn is None:
        return 0
    if idn is None:
        return 1
    if iu is None or idn <= iu:
        return -1
    return 1


def _random_rate(h, l, c, atr_at, ks, d_win_atr, d_loss_atr, long: bool):
    """Mean win rate from the bars `ks`, same direction, barriers in ATR of each bar."""
    w = n = 0
    for j in ks:
        a = atr_at[j]
        if not np.isfinite(a) or a <= 0:
            continue
        if long:
            r = first_passage(h, l, j, c[j] + d_win_atr * a, c[j] - d_loss_atr * a)
        else:
            r = -first_passage(h, l, j, c[j] + d_loss_atr * a, c[j] - d_win_atr * a)
        if r:
            n += 1
            w += r > 0
    return w, n


def measure(rows: list, s, rng) -> dict:
    """Events of one span: {'touch'|'break'|'target': [ {win, base_w, base_n, walk}, ... ]}."""
    from server.engine.indicators import atr
    h, l, c, t = s.h, s.l, s.c, s.t
    for r in rows:
        if int(t[r['k']]) != r['t']:
            raise RuntimeError('series does not line up with the recording')
    atr_at = atr(h, l, c, 14).astype(float)
    for r in rows:
        if r.get('atr'):
            atr_at[r['k']] = r['atr']
    span_ks = np.array([r['k'] for r in rows if r.get('ok')])
    out = defaultdict(list)
    t_index = {int(x): i for i, x in enumerate(t)}

    # cache of the random baseline per (direction, win, loss) on this span
    base_cache = {}

    def base(long, dw, dl):
        key = (long, round(dw, 1), round(dl, 1))
        if key not in base_cache:
            ks = rng.choice(span_ks, size=min(400, span_ks.size), replace=False)
            base_cache[key] = _random_rate(h, l, c, atr_at, ks, key[1], key[2], long)
        return base_cache[key]

    prev_lines, prev_pats = None, None
    counted = set()                  # a pattern's confirmation counts once per span
    for r in rows:
        if not r.get('ok'):
            prev_lines = prev_pats = None
            continue
        k, a = r['k'], r['atr'] or 0.0
        if a <= 0:
            continue
        lines = {(x[0], x[1], x[3]): x for x in r['tl']}
        # ---- touches: a line the chart showed on the previous bar ---------- #
        if prev_lines is not None:
            shown = sorted(prev_lines.values(), key=lambda x: -x[7])[:TL_ON_CHART]
            for x in shown:
                if x[8]:
                    continue
                val = x[2] + x[5] * (k - x[1])
                if x[0] == 's':
                    touched = l[k] <= val + TOUCH_TOL * a and c[k] > val
                    long = True
                else:
                    touched = h[k] >= val - TOUCH_TOL * a and c[k] < val
                    long = False
                # a touch counts once - not again on the next bar of the same test
                if not touched or x[-1] == '_t':
                    continue
                if long:
                    res = first_passage(h, l, k, val + a, val - a)
                    dw, dl = (val + a - c[k]) / a, (c[k] - (val - a)) / a
                else:
                    res = -first_passage(h, l, k, val + a, val - a)
                    dw, dl = (c[k] - (val - a)) / a, (val + a - c[k]) / a
                if dw <= 0 or dl <= 0:
                    continue
                bw, bn = base(long, dw, dl)
                out['touch'].append({'res': res, 'bw': bw, 'bn': bn, 'kind': x[0]})
            # ---- breaks: the engine's own flag flipping ------------------- #
            for lid, x in lines.items():
                p = prev_lines.get(lid)
                if p is None or p[8] or not x[8]:
                    continue
                long = x[0] == 'r'                   # resistance broken -> up
                if long:
                    res = first_passage(h, l, k, c[k] + a, c[k] - a)
                else:
                    res = -first_passage(h, l, k, c[k] + a, c[k] - a)
                bw, bn = base(long, 1.0, 1.0)
                out['break'].append({'res': res, 'bw': bw, 'bn': bn, 'kind': x[0]})
        # mark lines touched on this bar so the next bar does not count them again
        if prev_lines is not None:
            for lid, x in lines.items():
                val = x[2] + x[5] * (k - x[1])
                near = (l[k] <= val + TOUCH_TOL * a) if x[0] == 's' else (h[k] >= val - TOUCH_TOL * a)
                if near:
                    lines[lid] = list(x) + ['_t']
        prev_lines = lines

        # ---- pattern confirmations --------------------------------------- #
        pats = {(p[0], tuple(q[0] for q in p[4])): p for p in r['pt']}
        for pid, p in pats.items():
            if p[2] != 'confirmed' or p[1] not in ('bullish', 'bearish'):
                continue
            was = prev_pats.get(pid) if prev_pats is not None else None
            if prev_pats is None or pid in counted or (was is not None and was[2] == 'confirmed'):
                continue                             # not new on this bar, or seen before
            counted.add(pid)
            tgt, inv = p[6], p[7]
            long = p[1] == 'bullish'
            if long:
                if not (inv < c[k] < tgt):
                    out['target_skipped'].append({'res': 0})
                    continue
                res = first_passage(h, l, k, tgt, inv)
                dw, dl = (tgt - c[k]) / a, (c[k] - inv) / a
            else:
                if not (tgt < c[k] < inv):
                    out['target_skipped'].append({'res': 0})
                    continue
                res = -first_passage(h, l, k, inv, tgt)
                dw, dl = (c[k] - tgt) / a, (inv - c[k]) / a
            bw, bn = base(long, dw, dl)
            out['target'].append({'res': res, 'bw': bw, 'bn': bn, 'kind': p[0],
                                  'walk': dl / (dw + dl), 'first_seen': was is None})
        prev_pats = pats
    return out


def _summ(ev: list) -> dict:
    res = [e for e in ev if e['res'] != 0]
    n = len(res)
    w = sum(1 for e in res if e['res'] > 0)
    bw = sum(e['bw'] for e in res)
    bn = sum(e['bn'] for e in res)
    rate = w / n if n else float('nan')
    brate = bw / bn if bn else float('nan')
    se = (rate * (1 - rate) / n) ** 0.5 if n else float('nan')
    walk = float(np.mean([e['walk'] for e in res])) if res and 'walk' in res[0] else None
    return {'n': n, 'unresolved': len(ev) - n, 'rate': rate, 'random': brate,
            'edge': rate - brate, 'ci': 1.96 * se, 'walk': walk}


def _fmt(s: dict) -> str:
    if not s['n']:
        return f"{'-':>8}"
    walk = f"{s['walk'] * 100:7.1f}%" if s['walk'] is not None else f"{'':>8}"
    return (f"{s['n']:>7,}{s['rate'] * 100:8.1f}%{s['random'] * 100:8.1f}%{walk}"
            f"{s['edge'] * 100:+9.1f} ± {s['ci'] * 100:4.1f}{s['unresolved']:>8}")


def report(run_dir: Path) -> str:
    man = json.loads((run_dir / 'manifest.json').read_text(encoding='utf-8'))
    rng = np.random.default_rng(20261002)
    tfs = [tf for tf in man['tfs'] if list(run_dir.glob(f'{tf}_*.jsonl.gz'))]
    by = defaultdict(list)            # (tf, event, year|'all', sub) -> events
    for tf in tfs:
        for start, end in man['spans']:
            p = run_dir / f'{tf}_{start}.jsonl.gz'
            if not p.exists():
                continue
            rows = _rows(p)
            s = _series_ext(man['symbol'], tf, start, end, man['warm'])
            ev = measure(rows, s, rng)
            y = start[:4]
            for name, xs in ev.items():
                by[(tf, name, 'all', 'all')].extend(xs)
                by[(tf, name, y, 'all')].extend(xs)
                for e in xs:
                    if 'kind' in e:
                        by[(tf, name, 'all', e['kind'])].append(e)
    hdr = (f"  {'':<50}{'events':>7}{'win':>9}{'random':>9}{'walk':>8}"
           f"{'edge (pts)':>16}{'unres.':>8}")
    L = [f"STRUCTURE OUTCOMES  {man['run_id']}   engine {man['engine']}   {man['symbol']}",
         f'win = reached its claim first, within {HORIZON} bars; random = a bar of the same span '
         'facing the same barriers; walk = a driftless random walk',
         'Measurements of the engine as it is. Not trading advice.', '']
    names = (('touch', 'TOUCH -> bounce 1 ATR before 1 ATR through'),
             ('break', 'BREAK -> 1 ATR more before 1 ATR back'),
             ('target', 'PATTERN CONFIRMED -> target before invalidation'))
    for tf in tfs:
        L += [f'{tf}', hdr]
        for name, title in names:
            L.append(f"  {title:<50}" + _fmt(_summ(by[(tf, name, 'all', 'all')])))
            subs = sorted({k[3] for k in by if k[0] == tf and k[1] == name and k[2] == 'all'} - {'all'})
            for sub in subs:
                lab = {'s': 'support', 'r': 'resistance'}.get(sub, sub)
                L.append(f"    {lab:<48}" + _fmt(_summ(by[(tf, name, 'all', sub)])))
        sk = len(by[(tf, 'target_skipped', 'all', 'all')])
        L.append(f'  (patterns confirmed with price already past the target or invalidation, '
                 f'left out: {sk:,})')
        L.append('')
        L.append(f'  {tf} by year - edge in points (event win rate minus random)')
        years = sorted({k[2] for k in by if k[0] == tf and k[2] != 'all'})
        L.append(f"  {'':<8}" + ''.join(f'{n_[0]:>16}' for n_ in names))
        for y in years:
            cells = []
            for name, _ in names:
                sm = _summ(by[(tf, name, y, 'all')])
                cells.append(f"{sm['edge'] * 100:+6.1f} (n {sm['n']:>4})" if sm['n'] else '-')
            L.append(f'  {y:<8}' + ''.join(f'{x:>16}' for x in cells))
        L.append('')
    return '\n'.join(L)


if __name__ == '__main__':
    d = Path(sys.argv[1])
    txt = report(d)
    (d / 'outcomes.txt').write_text(txt, encoding='utf-8')
    print(txt)
