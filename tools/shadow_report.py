#!/usr/bin/env python
"""
tools/shadow_report.py - shadow live: did the trades regime_agree would have vetoed do worse?

    python tools/shadow_report.py

Joins the live shadow log (order_ledger/logs/shadow_forecast.jsonl, written by
server/forecast/shadow.py beside every 5m send) to the order ledger's closed
outcomes, and compares the trades the filter would have VETOED with the ones
it would have let PASS: count, win rate, mean R, total R, net.

The rule for reading it was fixed before any row existed:

    no evidence    until both groups have at least 30 closed trades
    worth a look   vetoed mean R below passed mean R, with the 90% day-block
                   interval of (vetoed - passed) entirely below zero
    anything else  the filter is not doing live what Phase D hoped

"Worth a look" means a filter test on the newer history and a decision by the
trader - never an automatic switch. Read-only. Measurements, not trading advice.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / 'order_ledger' / 'logs' / 'shadow_forecast.jsonl'
LEDGER = ROOT / 'order_ledger' / 'order_ledger.json'
MIN_N = 30


def _rows() -> list:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding='utf-8').splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _diff_ci(a: list, b: list, reps: int = 2000, seed: int = 5) -> tuple:
    """90% day-block interval for mean R(a) - mean R(b)."""
    def by_day(xs):
        d = {}
        for x in xs:
            d.setdefault(int(x['closed_ms']) // 86_400_000, []).append(float(x['r']))
        return [np.array(v) for v in d.values()]
    da, db = by_day(a), by_day(b)
    if len(da) < 5 or len(db) < 5:
        return float('nan'), float('nan')
    g = np.random.default_rng(seed)
    v = [np.concatenate([da[i] for i in g.integers(0, len(da), len(da))]).mean()
         - np.concatenate([db[i] for i in g.integers(0, len(db), len(db))]).mean()
         for _ in range(reps)]
    return float(np.quantile(v, 0.05)), float(np.quantile(v, 0.95))


def summary() -> dict:
    rows = _rows()
    ledger = (json.loads(LEDGER.read_text(encoding='utf-8')).get('orders') or {}) \
        if LEDGER.exists() else {}
    groups = {'veto': [], 'pass': []}
    counts = {'veto': 0, 'pass': 0, 'abstain': 0, 'error': 0}
    abstain = {}
    open_n = 0
    seen = set()
    for r in rows:
        if r.get('id') in seen:
            continue
        seen.add(r.get('id'))
        if r.get('error'):
            counts['error'] += 1
            continue
        v = r.get('verdict')
        counts[v] = counts.get(v, 0) + 1
        if v == 'abstain':
            abstain[r.get('why')] = abstain.get(r.get('why'), 0) + 1
            continue
        o = ledger.get(r.get('id')) or {}
        if o.get('state') != 'closed' or o.get('r') is None:
            open_n += 1
            continue
        groups[v].append({'r': float(o['r']), 'profit': float(o.get('profit') or 0),
                          'closed_ms': int(o.get('updated_ms') or r.get('at_ms') or 0)})
    out = {'rows': len(rows), 'counts': counts, 'abstain': abstain, 'not_closed': open_n,
           'first_ms': min((r['at_ms'] for r in rows if r.get('at_ms')), default=None)}
    for k, g in groups.items():
        rs = np.array([x['r'] for x in g])
        out[k] = {'n': len(g), 'win': float(np.mean(rs > 0)) if len(g) else None,
                  'mean_r': float(rs.mean()) if len(g) else None,
                  'sum_r': float(rs.sum()) if len(g) else 0.0,
                  'net': float(sum(x['profit'] for x in g))}
    lo, hi = _diff_ci(groups['veto'], groups['pass'])
    out['diff'] = {'mean': (out['veto']['mean_r'] - out['pass']['mean_r'])
                   if out['veto']['n'] and out['pass']['n'] else None, 'lo': lo, 'hi': hi}
    if min(out['veto']['n'], out['pass']['n']) < MIN_N:
        out['reading'] = (f"no evidence yet - {out['veto']['n']} vetoed and {out['pass']['n']} "
                          f"passed trades closed; the rule needs {MIN_N} in each")
    elif hi != hi:
        out['reading'] = 'no evidence yet - the closed trades span too few days for an interval'
    elif hi < 0:
        out['reading'] = ('worth a look - the trades it would have vetoed did worse, interval '
                          'clear of zero. Next: a filter test on the newer history, then your call.')
    else:
        out['reading'] = 'the filter is not doing live what Phase D hoped (interval includes zero or favours the vetoed)'
    return out


def report(s: dict) -> str:
    f = lambda x, d=3: '-' if x is None or x != x else f'{x:+.{d}f}'   # noqa: E731
    since = datetime.fromtimestamp(s['first_ms'] / 1000, timezone.utc).strftime('%Y-%m-%d') \
        if s.get('first_ms') else '-'
    lines = ['SHADOW LIVE - regime_agree beside every 5m send (log only, never used)',
             f"log since {since}: {s['rows']} rows - would veto {s['counts']['veto']}, "
             f"pass {s['counts']['pass']}, abstain {s['counts']['abstain']}, "
             f"errors {s['counts']['error']}; {s['not_closed']} not closed yet"]
    pct = lambda x: '-' if x is None else f'{100 * x:.0f}%'            # noqa: E731
    for k in ('veto', 'pass'):
        g = s[k]
        lines.append(f"  would {k:<5} {g['n']:>4} closed  win {pct(g['win'])}  "
                     f"mean {f(g['mean_r'])}R  total {g['sum_r']:+.1f}R  net {g['net']:+.2f}")
    d = s['diff']
    lines.append(f"  vetoed - passed: {f(d['mean'])}R per trade  90% [{f(d['lo'])}, {f(d['hi'])}]")
    for why, n in s['abstain'].items():
        lines.append(f'  abstained {n}x: {why}')
    lines.append('reading: ' + s['reading'])
    lines.append('Measurements on live history, not trading advice.')
    return '\n'.join(lines)


def main() -> int:
    print(report(summary()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
