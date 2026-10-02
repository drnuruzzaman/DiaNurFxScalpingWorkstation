#!/usr/bin/env python
"""
tools/structure_audit_report.py - Step 0A metrics over a tools/structure_audit.py run.

    python tools/structure_audit.py --report runs/audit/structure/<run>

Writes report.txt (for reading) and metrics.json (for comparing a later engine
version against this baseline) into the run folder.

Every check is a statement the engine makes about itself, tested against the
bars, using two yardsticks: the engine's own rule exactly as it ran, and the
same rule with the ATR of the moment the object formed. The engine measures
everything with TODAY'S ATR, so a line or a pattern can appear or disappear
only because volatility changed - which a trader looking at the chart would
never do.

What "removed", "flicker" and the disappearance causes mean is spelled out next
to each number. Causes are tested in the order listed, and the first one that
explains a disappearance takes it.
"""
from __future__ import annotations

import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RECENT = 300            # "recent" = the newest 300 bars, the half of the window a trader reads
PAT_MAX_AGE = 150       # detect_patterns drops patterns that ended longer ago than this
PAT_CAP, TL_CAP = 8, 8  # most patterns / trendlines the engine returns
TL_ON_CHART = 5         # ChartEngine draws the top 5 lines by score
PT_FEATURED = 2         # ... and features the top 2 actionable patterns (FEATURED_PATTERNS)


def _rows(path: Path) -> list:
    with gzip.open(path, 'rt', encoding='utf-8') as fh:
        return [json.loads(line) for line in fh]


def _series(symbol: str, tf: str, start: str, end: str, warm: int):
    from server.lab import data as lab_data
    from server.lab.session import parse_ms
    s, _ = lab_data.load(symbol, tf, parse_ms(f'{start}T00:00'), parse_ms(f'{end}T00:00'), warm)
    return s


class Acc:
    """Additive counters and value lists, so spans pool into tf and year totals."""

    def __init__(self):
        self.n = Counter()
        self.v = defaultdict(list)

    def add(self, other: 'Acc') -> 'Acc':
        self.n.update(other.n)
        for k, xs in other.v.items():
            self.v[k].extend(xs)
        return self


# --------------------------------------------------------------------------- #
# per-span measurement                                                        #
# --------------------------------------------------------------------------- #
def measure(rows: list, s, cfg) -> Acc:
    from server.engine.indicators import atr
    A = Acc()
    h, l, c, t = s.h, s.l, s.c, s.t
    for r in rows:
        if int(t[r['k']]) != r['t']:
            raise RuntimeError(f"bar {r['k']}: series time {int(t[r['k']])} != recorded {r['t']}")
    # ATR of the moment: the window's own ATR at that bar where the span has
    # it, the full-series ATR (same period, converged) for the warm-up bars.
    atr_at = atr(h, l, c, cfg.atr_period).astype(float)
    for r in rows:
        if r.get('atr'):
            atr_at[r['k']] = r['atr']
    t_index = {int(x): i for i, x in enumerate(t)}
    tol_t, viol_t = 0.35, cfg.tl_max_violation_atr
    first_k = rows[0]['k']
    A.n['bars'] += len(rows)

    # ---------------- swings ---------------------------------------------- #
    seen, removed, relabel, back = {}, {}, set(), set()
    prev = None
    for r in rows:
        if not r.get('ok'):
            prev = None
            continue
        k = r['k']
        cur = {(x[0], x[1]): x for x in r['sw']}
        oldest = min((x[0] for x in r['sw']), default=k)
        for key, x in cur.items():
            if key not in seen:
                seen[key] = {'first': k, 'label': x[3], 'conf': x[4], 'pre': k == first_k}
            else:
                lab0 = seen[key]['label']
                if (lab0 and x[3] and x[3] != lab0 and key[0] >= k - RECENT):
                    relabel.add(key)
                if key in removed:
                    back.add(key)
        if prev is not None:
            for key in prev.keys() - cur.keys():
                if key[0] > oldest and key not in removed:
                    removed[key] = (k, key[0] >= k - RECENT)
        prev = cur
    new = [k_ for k_, m in seen.items() if not m['pre']]
    A.n['sw_new'] += len(new)
    for key in new:
        m = seen[key]
        A.v['sw_lag'].append(m['first'] - key[0])
        if m['conf'] is not None and m['first'] > m['conf']:
            A.n['sw_late'] += 1
        if key in removed and removed[key][1]:
            A.n['sw_removed'] += 1
            d = removed[key][0] - m['first']
            A.v['sw_removed_after'].append(d)
        if key in relabel:
            A.n['sw_relabel'] += 1
        if key in back:
            A.n['sw_back'] += 1

    # ---------------- trendlines ------------------------------------------ #
    def tl_id(x):
        return (x[0], x[1], x[3])

    def line_vals(x, xs):
        return x[2] + x[5] * (xs - x[1])

    def pool_start(r):
        sw = r['sw']
        k = r['k']
        if not sw:
            return k + 1 - cfg.tl_lookback_max_bars
        recent = sw[-cfg.tl_structural_swings:]
        st = recent[0][0]
        return min(max(st, k + 1 - cfg.tl_lookback_max_bars), k + 1 - cfg.tl_lookback_min_bars)

    life, vlife, prev = {}, {}, None
    for r in rows:
        if not r.get('ok'):
            prev = None
            continue
        k, a_now = r['k'], r['atr'] or 0.0
        cur = {tl_id(x): x for x in r['tl']}
        A.n['tl_bars'] += len(cur)
        for x in sorted(r['tl'], key=lambda x: -x[7])[:TL_ON_CHART]:
            m = vlife.get(tl_id(x))
            if m is None:
                vlife[tl_id(x)] = {'first': k, 'last': k, 'gaps': 0, 'pre': k == first_k}
            else:
                if m['last'] != k - 1:
                    m['gaps'] += 1
                m['last'] = k
        for lid, x in cur.items():
            m = life.get(lid)
            if m is None:
                life[lid] = {'first': k, 'last': k, 'gaps': 0, 'pre': k == first_k}
            else:
                if m['last'] != k - 1:
                    m['gaps'] += 1
                m['last'] = k
            kind, x1, x2 = x[0], x[1], x[3]
            if x[8]:
                A.n['tl_bars_broken'] += 1
                continue
            # anchor span, wicks, ATR of each bar
            xs = np.arange(x1, x2 + 1)
            vals = line_vals(x, xs)
            exc = (vals - l[xs]) if kind == 's' else (h[xs] - vals)
            if exc.size and (exc > viol_t * atr_at[xs]).any():
                A.n['tl_viol_anchor_atrtime'] += 1
            # after the second anchor, closes, ATR of each bar
            ys = np.arange(x2, k + 1)
            v2 = line_vals(x, ys)
            thr = viol_t * atr_at[ys]
            through = (c[ys] < v2 - thr) if kind == 's' else (c[ys] > v2 + thr)
            if int(through.sum()) >= 2:
                A.n['tl_broken_atrtime'] += 1
            # price beyond an "unbroken" line right now, by more than 1 ATR
            here = line_vals(x, np.array([k]))[0]
            if (kind == 's' and c[k] < here - a_now) or (kind == 'r' and c[k] > here + a_now):
                A.n['tl_wrong_side'] += 1
            # touches, ATR of each touch bar
            ti = np.array(x[9], dtype=int)
            if ti.size:
                px = l[ti] if kind == 's' else h[ti]
                good = np.abs(px - line_vals(x, ti)) <= tol_t * atr_at[ti]
                A.n['tl_touch_total'] += int(ti.size)
                A.n['tl_touch_bad'] += int((~good).sum())
                if int(good.sum()) < cfg.tl_min_touches:
                    A.n['tl_under_min_atrtime'] += 1
            A.n['tl_bars_unbroken'] += 1
        if prev is not None:
            sw_keys = {(q[0], q[1]) for q in r['sw']}
            p0 = pool_start(r)
            for lid in prev.keys() - cur.keys():
                x = prev[lid]
                kind, x1, x2 = x[0], x[1], x[3]
                want = 'L' if kind == 's' else 'H'
                cause = None
                if x[8]:
                    cause = 'was already broken (score -25), lost the ranking'
                elif (x1, want) not in sw_keys or (x2, want) not in sw_keys:
                    cause = 'an anchor swing was redrawn'
                elif x1 < p0:
                    cause = 'first anchor left the anchor window'
                else:
                    xs = np.arange(x1, x2 + 1)
                    vals = line_vals(x, xs)
                    exc = (vals - l[xs]) if kind == 's' else (h[xs] - vals)
                    pool = [q for q in r['sw'] if q[1] == want and q[0] >= x1]
                    tch = sum(1 for q in pool if abs(q[2] - line_vals(x, np.array([q[0]]))[0])
                              <= tol_t * a_now)
                    ys = np.arange(x2, k + 1)
                    v2 = line_vals(x, ys)
                    thr = viol_t * a_now
                    thru = (c[ys] < v2 - thr) if kind == 's' else (c[ys] > v2 + thr)
                    if exc.size and float(exc.max()) > viol_t * a_now:
                        cause = "anchor span now 'violated' at today's ATR"
                    elif tch < cfg.tl_min_touches:
                        cause = "lost a touch at today's ATR"
                    elif int(thru.sum()) >= 2:
                        cause = 'broke this bar, lost the ranking'
                    elif len(cur) >= TL_CAP:
                        cause = 'crowded out (8-line cap)'
                    else:
                        cause = 'merged into a near-duplicate / outranked'
                A.n['tl_gone:' + cause] += 1
                A.n['tl_gone'] += 1
        prev = cur
    for tag, lives in (('tl', life), ('tlv', vlife)):
        for lid, m in lives.items():
            if m['pre']:
                continue
            A.n[f'{tag}_new'] += 1
            A.v[f'{tag}_life'].append(m['last'] - m['first'] + 1)
            if m['gaps']:
                A.n[f'{tag}_flicker'] += 1

    # ---------------- channels -------------------------------------------- #
    clife, prev = {}, None
    for r in rows:
        if not r.get('ok'):
            continue
        k = r['k']
        for x in r['ch']:
            cid = (x[1], round(x[4] or 0.0, 8))
            m = clife.get(cid)
            if m is None:
                clife[cid] = {'first': k, 'last': k, 'gaps': 0, 'pre': k == first_k}
            else:
                if m['last'] != k - 1:
                    m['gaps'] += 1
                m['last'] = k
        A.n['ch_bars_any'] += bool(r['ch'])
    for m in clife.values():
        if m['pre']:
            continue
        A.n['ch_new'] += 1
        A.v['ch_life'].append(m['last'] - m['first'] + 1)
        if m['gaps']:
            A.n['ch_flicker'] += 1

    # ---------------- patterns -------------------------------------------- #
    def pt_id(p):
        return (p[0], tuple(q[0] for q in p[4]))

    plife, prev, feat_prev = {}, None, None
    for r in rows:
        if not r.get('ok'):
            prev = feat_prev = None
            continue
        k = r['k']
        cur = {pt_id(p): p for p in r['pt']}
        A.n['pt_bars'] += len(cur)
        A.n['pt_rows_any'] += bool(cur)
        A.v['pt_per_bar'].append(len(cur))
        act = {p[1] for p in r['pt'] if p[10]}
        if {'bullish', 'bearish'} <= act:
            A.n['pt_rows_both_dirs'] += 1
        feat = {pt_id(p) for p in sorted((p for p in r['pt'] if p[10]),
                                         key=lambda p: -p[11])[:PT_FEATURED]}
        if feat_prev is not None:
            A.n['pt_feat_rows'] += 1
            A.n['pt_feat_changed'] += feat != feat_prev
        feat_prev = feat
        for pid, p in cur.items():
            m = plife.get(pid)
            if m is None:
                plife[pid] = m = {'first': k, 'last': k, 'gaps': 0, 'pre': k == first_k,
                                  'status0': p[2], 'end': p[9], 'kind': p[0], 'p': p}
            else:
                if m['last'] != k - 1:
                    m['gaps'] += 1
                m['last'] = k
                if m['p'][2] != p[2]:
                    A.n[f'pt_status:{m["p"][2]}->{p[2]}'] += 1
                m['p'] = p
            if p[1] in ('bullish', 'bearish'):          # neutral shapes have no fixed side
                brk, tgt, inv = p[5], p[6], p[7]
                ok = (tgt < brk < inv) if p[1] == 'bearish' else (tgt > brk > inv)
                grp = 'hs' if 'head_shoulders' in p[0] else 'other'
                A.n[f'pt_levels_checked:{grp}'] += 1
                if not ok:
                    A.n[f'pt_levels_bad:{grp}'] += 1
        if prev is not None:
            sw_idx = {q[0] for q in r['sw']}
            for pid in prev.keys() - cur.keys():
                p = prev[pid]
                end, inv = p[9], p[7]
                seg = slice(end, k + 1)
                if p[1] == 'bearish':
                    failed = c[seg].size and (c[seg] > inv).any() or (
                        p[0] == 'head_shoulders' and h[seg].max() > inv)
                else:
                    failed = c[seg].size and (c[seg] < inv).any() or (
                        p[0] == 'inverse_head_shoulders' and l[seg].min() < inv)
                idxs = [t_index.get(int(q[0])) for q in p[4]]
                if failed:
                    cause = 'price beyond its invalidation (failed)'
                elif k - end > PAT_MAX_AGE:
                    cause = 'aged out (150 bars)'
                elif any(i is None or i not in sw_idx for i in idxs):
                    cause = 'a point stopped being a swing (redrawn)'
                elif len(cur) >= PAT_CAP:
                    cause = 'crowded out (8-pattern cap)'
                else:
                    cause = "quality / risk / RR filter at today's ATR, or merged"
                A.n['pt_gone:' + cause] += 1
                A.n['pt_gone'] += 1
        prev = cur
    for pid, m in plife.items():
        if m['pre']:
            continue
        kind = m['kind']
        A.n['pt_new'] += 1
        A.n[f'pt_new:{kind}'] += 1
        A.v['pt_life'].append(m['last'] - m['first'] + 1)
        A.v['pt_first_lag'].append(m['first'] - m['end'])
        if m['gaps']:
            A.n['pt_flicker'] += 1
        if m['status0'] == 'confirmed':
            A.n['pt_first_confirmed'] += 1
            A.n['pt_first_confirmed_late' if m['first'] - m['end'] > 3 else
                'pt_first_confirmed_soon'] += 1
        # the detector's own thresholds, with the ATR of the pattern's last point
        p, a_end = m['p'], atr_at[m['end']]
        pr = [q[1] for q in p[4]]
        if kind in ('double_top', 'double_bottom', 'triple_top', 'triple_bottom') and len(pr) == 3:
            A.n['pt_dt_checked'] += 1
            gap = abs(pr[0] - pr[2])
            depth = abs(pr[1] - (pr[0] + pr[2]) / 2)
            if gap > 0.9 * a_end or depth < 1.3 * a_end:
                A.n['pt_dt_fail_atrtime'] += 1
        elif kind in ('head_shoulders', 'inverse_head_shoulders') and len(pr) == 5:
            A.n['pt_hs_checked'] += 1
            if abs(pr[2] - (pr[1] + pr[3]) / 2) < 1.2 * a_end:
                A.n['pt_hs_fail_atrtime'] += 1

    # ---------------- signals (raw, before gates) ------------------------- #
    prev = set()
    for r in rows:
        cur = {(x[0], x[1]) for x in r.get('sig') or []}
        for pb, side in cur:
            A.n[f'sig:{pb}'] += 1
            if (pb, side) in prev:
                A.n[f'sig_repeat:{pb}'] += 1
        prev = cur

    # ---------------- lab 600 vs live 599, 600 vs 1500 -------------------- #
    def sets(snap, k):
        sw = {(x[0], x[1]) for x in snap['sw'] if x[0] >= k - RECENT}
        lab = {(x[0], x[1]): x[3] for x in snap['sw'] if x[0] >= k - RECENT}
        return {'swings': sw, 'trendlines': {tl_id(x) for x in snap['tl']},
                'channels': {(x[1], round(x[4] or 0.0, 8)) for x in snap['ch']},
                'patterns': {pt_id(p) for p in snap['pt']},
                'pattern status': {(pt_id(p), p[2]) for p in snap['pt']},
                'signals': {(x[0], x[1]) for x in snap.get('sig') or []}}, lab

    for r in rows:
        if not r.get('ok'):
            continue
        k = r['k']
        a, alab = sets(r, k)
        for name in ('live', 'long'):
            o = r.get(name)
            if not o or not o.get('ok'):
                continue
            b, blab = sets(o, k)
            A.n[f'cmp:{name}:rows'] += 1
            for obj in a:
                if name == 'long' and obj == 'signals':
                    continue
                x, y = a[obj], b[obj]
                if x == y:
                    A.n[f'cmp:{name}:{obj}:same'] += 1
                if x or y:
                    A.n[f'cmp:{name}:{obj}:nonempty'] += 1
                    A.v[f'cmp:{name}:{obj}:jac'].append(len(x & y) / len(x | y))
            common = alab.keys() & blab.keys()
            A.n[f'cmp:{name}:label_common'] += len(common)
            A.n[f'cmp:{name}:label_diff'] += sum(1 for q in common if alab[q] != blab[q])
    return A


# --------------------------------------------------------------------------- #
# formatting                                                                  #
# --------------------------------------------------------------------------- #
def _pct(a, b):
    return f'{100.0 * a / b:5.1f}%' if b else '   - '


def _med(xs, q=50):
    return f'{float(np.percentile(xs, q)):6.0f}' if xs else '     -'


def _table(title: str, lines: list, tfs: list) -> list:
    out = [title, '  ' + ' ' * 58 + ''.join(f'{tf:>10}' for tf in tfs)]
    for label, vals in lines:
        out.append(f'  {label:<58}' + ''.join(f'{v:>10}' for v in vals))
    return out + ['']


def report(run_dir: Path) -> str:
    from server.config import CONFIG
    from server.forecast import sync_engine_settings
    sync_engine_settings()
    cfg = CONFIG.engine
    man = json.loads((run_dir / 'manifest.json').read_text(encoding='utf-8'))
    tfs = [tf for tf in man['tfs'] if list(run_dir.glob(f'{tf}_*.jsonl.gz'))]
    by_tf, by_year = {}, defaultdict(dict)
    for tf in tfs:
        tot = Acc()
        for start, end in man['spans']:
            p = run_dir / f'{tf}_{start}.jsonl.gz'
            if not p.exists():
                continue
            rows = _rows(p)
            if not rows:
                continue
            s = _series(man['symbol'], tf, start, end, man['warm'])
            a = measure(rows, s, cfg)
            tot.add(a)
            y = start[:4]
            by_year[y].setdefault(tf, Acc()).add(a)
        by_tf[tf] = tot

    def col(fn):
        return [fn(by_tf[tf]) for tf in tfs]

    L = [f"STRUCTURE AUDIT  {man['run_id']}   engine {man['engine']}   {man['symbol']}",
         f"spans: two 3-week spans a year 2018-2026 (Feb + Aug, fixed in advance)   "
         f"window {man['window']} closed bars (lab), {man['live_window']} (live), "
         f"{man['long_window']} (long), compared every {man['extra_every']}th bar",
         'Measurements of the engine as it is. Not trading advice.', '']
    L += _table('0. DATA', [
        ('bars analysed', col(lambda a: f"{a.n['bars']:,}")),
    ], tfs)

    # swings
    L += _table('1. SWINGS   (each swing counted once, from the bar it first appeared)', [
        ('new swings shown', col(lambda a: f"{a.n['sw_new']:,}")),
        ('later REMOVED while still in the newest 300 bars', col(lambda a: _pct(a.n['sw_removed'], a.n['sw_new']))),
        ('   removed within 2 bars of appearing', col(lambda a: _pct(sum(1 for d in a.v['sw_removed_after'] if d <= 2), a.n['sw_new']))),
        ('   removed 3-10 bars after', col(lambda a: _pct(sum(1 for d in a.v['sw_removed_after'] if 3 <= d <= 10), a.n['sw_new']))),
        ('   removed 11+ bars after', col(lambda a: _pct(sum(1 for d in a.v['sw_removed_after'] if d > 10), a.n['sw_new']))),
        ('came back after being removed', col(lambda a: _pct(a.n['sw_back'], a.n['sw_new']))),
        ('HH/HL/LH/LL label changed after it was shown', col(lambda a: _pct(a.n['sw_relabel'], a.n['sw_new']))),
        ('bars from the extreme to first appearing: median', col(lambda a: _med(a.v['sw_lag']).strip())),
        ('   90th percentile', col(lambda a: _med(a.v['sw_lag'], 90).strip())),
        ("appeared LATER than its own confirmed_at says", col(lambda a: _pct(a.n['sw_late'], a.n['sw_new']))),
    ], tfs)

    tl_causes = sorted({k.split(':', 1)[1] for a in by_tf.values() for k in a.n if k.startswith('tl_gone:')})
    L += _table('2. TRENDLINES   (a line = its two anchor bars; "line-bars" = one line on one bar)', [
        ('distinct new lines per 1000 bars', col(lambda a: f"{1000 * a.n['tl_new'] / max(a.n['bars'], 1):.0f}")),
        ('life on the chart, bars: median', col(lambda a: _med(a.v['tl_life']).strip())),
        ('   lines shown for 5 bars or fewer', col(lambda a: _pct(sum(1 for d in a.v['tl_life'] if d <= 5), a.n['tl_new']))),
        ('FLICKER: vanished, then came back', col(lambda a: _pct(a.n['tl_flicker'], a.n['tl_new']))),
        ('ON THE CHART (top 5 by score): new lines per 1000 bars', col(lambda a: f"{1000 * a.n['tlv_new'] / max(a.n['bars'], 1):.0f}")),
        ('   life on the chart, bars: median', col(lambda a: _med(a.v['tlv_life']).strip())),
        ('   FLICKER: vanished, then came back', col(lambda a: _pct(a.n['tlv_flicker'], a.n['tlv_new']))),
        ('line-bars drawn although flagged broken', col(lambda a: _pct(a.n['tl_bars_broken'], a.n['tl_bars']))),
        ('unbroken line-bars that fail, with ATR of the time:', col(lambda a: '')),
        ('   anchor span sliced by a wick > 0.45 ATR', col(lambda a: _pct(a.n['tl_viol_anchor_atrtime'], a.n['tl_bars_unbroken']))),
        ('   2+ closes beyond it since the 2nd anchor (= broken)', col(lambda a: _pct(a.n['tl_broken_atrtime'], a.n['tl_bars_unbroken']))),
        ('   under 3 real touches', col(lambda a: _pct(a.n['tl_under_min_atrtime'], a.n['tl_bars_unbroken']))),
        ('   touches outside 0.35 ATR (share of touches)', col(lambda a: _pct(a.n['tl_touch_bad'], a.n['tl_touch_total']))),
        ('unbroken line-bars with price > 1 ATR through it now', col(lambda a: _pct(a.n['tl_wrong_side'], a.n['tl_bars_unbroken']))),
        ('disappearances', col(lambda a: f"{a.n['tl_gone']:,}")),
    ] + [(f'   {c_}', col(lambda a, c_=c_: _pct(a.n['tl_gone:' + c_], a.n['tl_gone']))) for c_ in tl_causes], tfs)

    L += _table('3. CHANNELS', [
        ('bars with a channel', col(lambda a: _pct(a.n['ch_bars_any'], a.n['bars']))),
        ('distinct new channels per 1000 bars', col(lambda a: f"{1000 * a.n['ch_new'] / max(a.n['bars'], 1):.0f}")),
        ('life, bars: median', col(lambda a: _med(a.v['ch_life']).strip())),
        ('FLICKER: vanished, then came back', col(lambda a: _pct(a.n['ch_flicker'], a.n['ch_new']))),
    ], tfs)

    kinds = sorted({k.split(':', 1)[1] for a in by_tf.values() for k in a.n if k.startswith('pt_new:')})
    trans = sorted({k.split(':', 1)[1] for a in by_tf.values() for k in a.n if k.startswith('pt_status:')})
    pt_causes = sorted({k.split(':', 1)[1] for a in by_tf.values() for k in a.n if k.startswith('pt_gone:')})
    L += _table('4. PATTERNS   (a pattern = its kind + the bars of its points)', [
        ('bars showing at least one pattern', col(lambda a: _pct(a.n['pt_rows_any'], a.n['bars']))),
        ('patterns per bar: median', col(lambda a: _med(a.v['pt_per_bar']).strip())),
        ('bars with BOTH a bullish and a bearish actionable one', col(lambda a: _pct(a.n['pt_rows_both_dirs'], a.n['bars']))),
        ('distinct new patterns per 1000 bars', col(lambda a: f"{1000 * a.n['pt_new'] / max(a.n['bars'], 1):.0f}")),
    ] + [(f'   {k_}', col(lambda a, k_=k_: f"{1000 * a.n['pt_new:' + k_] / max(a.n['bars'], 1):.1f}")) for k_ in kinds] + [
        ('FIRST SHOWN ALREADY CONFIRMED (never seen forming)', col(lambda a: _pct(a.n['pt_first_confirmed'], a.n['pt_new']))),
        ('   ... within 3 bars of its last point (swing still confirming)', col(lambda a: _pct(a.n['pt_first_confirmed_soon'], a.n['pt_new']))),
        ('   ... 4+ bars after (it appeared in hindsight)', col(lambda a: _pct(a.n['pt_first_confirmed_late'], a.n['pt_new']))),
        ('first shown N bars after its last point: median', col(lambda a: _med(a.v['pt_first_lag']).strip())),
        ('life on the chart, bars: median', col(lambda a: _med(a.v['pt_life']).strip())),
        ('FLICKER: vanished, then came back', col(lambda a: _pct(a.n['pt_flicker'], a.n['pt_new']))),
        ('status changes (count)', col(lambda a: '')),
    ] + [(f'   {tr}', col(lambda a, tr=tr: f"{a.n['pt_status:' + tr]:,}")) for tr in trans] + [
        ('FEATURED pair (top 2 actionable) changed from the bar before', col(lambda a: _pct(a.n['pt_feat_changed'], a.n['pt_feat_rows']))),
        ('target/break/invalidation out of order (pattern-bars):', col(lambda a: '')),
        ('   head & shoulders - the break is past the head', col(lambda a: _pct(a.n['pt_levels_bad:hs'], a.n['pt_levels_checked:hs']))),
        ('   other bullish/bearish patterns', col(lambda a: _pct(a.n['pt_levels_bad:other'], a.n['pt_levels_checked:other']))),
        ('fail own thresholds at ATR of their last point:', col(lambda a: '')),
        ('   double/triple tops+bottoms (peaks 0.9, depth 1.3 ATR)', col(lambda a: _pct(a.n['pt_dt_fail_atrtime'], a.n['pt_dt_checked']))),
        ('   head & shoulders (head depth 1.2 ATR)', col(lambda a: _pct(a.n['pt_hs_fail_atrtime'], a.n['pt_hs_checked']))),
        ('disappearances', col(lambda a: f"{a.n['pt_gone']:,}")),
    ] + [(f'   {c_}', col(lambda a, c_=c_: _pct(a.n['pt_gone:' + c_], a.n['pt_gone']))) for c_ in pt_causes], tfs)

    pbs = sorted({k.split(':', 1)[1] for a in by_tf.values() for k in a.n if k.startswith('sig:')})
    L += _table('5. RAW SIGNALS   (what the playbooks detect, before any gate)', [
        (f'{pb}: per 1000 bars / also fired the bar before',
         col(lambda a, pb=pb: f"{1000 * a.n['sig:' + pb] / max(a.n['bars'], 1):.0f} / "
                              f"{100 * a.n['sig_repeat:' + pb] / max(a.n['sig:' + pb], 1):.0f}%"))
        for pb in pbs], tfs)

    objs = ('swings', 'trendlines', 'channels', 'patterns', 'pattern status', 'signals')
    for name, title in (('live', '6. LAB (600 bars) vs LIVE (599 bars), same bar'),
                        ('long', '7. WINDOW DEPENDENCE: 600 vs 1500 bars, same bar')):
        rowsL = [('bars compared', col(lambda a: f"{a.n[f'cmp:{name}:rows']:,}"))]
        for o in objs:
            if name == 'long' and o == 'signals':
                continue
            rowsL.append((f'identical {o}' + (' (newest 300 bars)' if o == 'swings' else ''),
                          col(lambda a, o=o: _pct(a.n[f'cmp:{name}:{o}:same'], a.n[f'cmp:{name}:rows']))))
            rowsL.append((f'   overlap when either has one (mean Jaccard)',
                          col(lambda a, o=o: f"{np.mean(a.v[f'cmp:{name}:{o}:jac']):.2f}"
                              if a.v[f'cmp:{name}:{o}:jac'] else '-')))
        rowsL.append(('swing labels that differ (shared swings)',
                      col(lambda a: _pct(a.n[f'cmp:{name}:label_diff'], a.n[f'cmp:{name}:label_common']))))
        L += _table(title, rowsL, tfs)

    # by year - the headline numbers
    years = sorted(by_year)
    head = [('swings removed', lambda a: _pct(a.n['sw_removed'], a.n['sw_new'])),
            ('chart lines flicker', lambda a: _pct(a.n['tlv_flicker'], a.n['tlv_new'])),
            ('patterns 1st seen confirmed', lambda a: _pct(a.n['pt_first_confirmed'], a.n['pt_new'])),
            ('patterns flicker', lambda a: _pct(a.n['pt_flicker'], a.n['pt_new'])),
            ('lab=live signals', lambda a: _pct(a.n['cmp:live:signals:same'], a.n['cmp:live:rows'])),
            ('lab=live patterns', lambda a: _pct(a.n['cmp:live:patterns:same'], a.n['cmp:live:rows']))]
    L.append('8. BY YEAR')
    hdr = '  ' + f"{'':<6}" + ''.join(f'{h_[0][:16]:>18}' for h_ in head)
    for tf in tfs:
        L.append(f'  {tf}')
        L.append(hdr)
        for y in years:
            a = by_year[y].get(tf)
            if a:
                L.append(f'  {y:<6}' + ''.join(f'{fn(a):>18}' for _, fn in head))
        L.append('')

    metrics = {tf: {'n': dict(a.n), 'med': {k: float(np.median(v)) for k, v in a.v.items() if v}}
               for tf, a in by_tf.items()}
    (run_dir / 'metrics.json').write_text(json.dumps(metrics, indent=1), encoding='utf-8')
    return '\n'.join(L)


if __name__ == '__main__':
    d = Path(sys.argv[1])
    txt = report(d)
    (d / 'report.txt').write_text(txt, encoding='utf-8')
    print(txt)
