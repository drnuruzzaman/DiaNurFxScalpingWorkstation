#!/usr/bin/env python
"""
tools/cell_table.py - every playbook x timeframe of a lab batch, after all costs.

Reads the window journals a batch wrote (runs/research/journal/win_<tag>-<year>_<tf>.json,
tools/market_batch.py) and tables net R per trade by cell: the development years,
the confirmation years, each year, buys against sells, with a day-block
bootstrap interval.

Net R is the journal's r_net (commission and swap in) where the line has it;
older lines only carry r_multiple, which is price R (spread and slippage in,
commission NOT), so commission is subtracted from it: 2 x commission per lot
side over the money risked. Which one was used is reported.

Tiers, fixed in advance (the same as the gold table in the permission-to-trade
proposal, section 5.3):
  1  positive in dev AND confirm, n >= 100, 90% interval above 0
  2  positive in both periods (smaller n, or an interval that reaches 0)
  3a positive in confirm only     3b positive in dev only
  4  negative in both
A scan of 49 cells finds t ~ 2.2-2.5 in the best one by chance alone; nothing
here is an edge until forward data confirms it.

    python tools/cell_table.py --tag eurusd --dev-to 2023
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JOURNAL = ROOT / 'runs' / 'research' / 'journal'
BATCHES = ROOT / 'runs' / 'research' / 'batches'
TFS = ('3m', '5m', '15m', '30m', '1h', '2h', '4h')
PLAYBOOKS = ('mtf_pullback', 'sweep_reversal', 'breakout_retest', 'false_break_fade',
             'pattern_break', 'range_fade', 'flag_continuation')


def net_r(line: dict, spec: dict) -> tuple:
    """(net R, source) for one journal line."""
    if line.get('r_net') is not None:
        return float(line['r_net']), 'r_net'
    risk = abs(float(line['fill_price']) - float(line['stop']))
    vpu = float(spec.get('tick_value') or 1.0) / float(spec.get('tick_size') or 0.01)
    comm = 2 * float(spec.get('commission_per_lot_side') or 0.0)
    lots = float(line.get('lots') or 0.0)
    if risk <= 0 or lots <= 0:
        return float(line['r_multiple']), 'r_multiple'
    return float(line['r_multiple']) - comm * lots / (risk * vpu * lots), 'r_multiple-comm'


def day_ci(rows: list, reps: int = 2000, seed: int = 7) -> tuple:
    """90% day-block bootstrap interval of the mean."""
    days = defaultdict(list)
    for d, r in rows:
        days[d].append(r)
    if len(days) < 10:
        return (float('nan'), float('nan'))
    blocks = list(days.values())
    rng = np.random.default_rng(seed)
    sums = np.array([sum(b) for b in blocks]); ns = np.array([len(b) for b in blocks])
    idx = rng.integers(0, len(blocks), (reps, len(blocks)))
    means = sums[idx].sum(1) / ns[idx].sum(1)
    return float(np.quantile(means, 0.05)), float(np.quantile(means, 0.95))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--tag', required=True)
    ap.add_argument('--dev-to', type=int, required=True, help='last development year')
    a = ap.parse_args()

    cells = defaultdict(list)                  # (pb, tf) -> [(year, side, day, r)]
    sources, files, meta = defaultdict(int), 0, {}
    for tf in TFS:
        for f in sorted(JOURNAL.glob(f'win_{a.tag}-*_{tf}.json')):
            d = json.loads(f.read_text(encoding='utf-8'))
            files += 1
            meta.setdefault('symbol', d.get('symbol'))
            meta.setdefault('fidelity', d.get('fidelity'))
            meta.setdefault('spread_floor_points', d.get('spread_floor_points'))
            meta.setdefault('swap_charged', d.get('swap_charged'))
            spec = d.get('spec') or {}
            if not spec.get('commission_per_lot_side'):
                # Journals written before the window run recorded its contract
                # facts: price commission with the symbol's spec on disk.
                from server.lab import data as lab_data
                spec = lab_data.spec(d.get('symbol'))
                meta['spec_from_disk'] = True
            for line in d['journal']:
                if line.get('playbook') == 'manual' or not line.get('fill_ms'):
                    continue
                r, src = net_r(line, spec)
                sources[src] += 1
                ms = int(line['fill_ms'])
                y = datetime.fromtimestamp(ms / 1000, timezone.utc).year
                cells[(line['playbook'], tf)].append((y, line['side'], ms // 86_400_000, r))
    if not files:
        print(f'no journals for tag {a.tag} in {JOURNAL}')
        return 1

    years = sorted({y for v in cells.values() for (y, _s, _d, _r) in v})
    out, lines = [], []
    w = lines.append
    w(f"CELL TABLE - {meta.get('symbol')}, batch '{a.tag}', {files} journal files, "
      f"fidelity {meta.get('fidelity')}, spread floor {meta.get('spread_floor_points')} pts, "
      f"swap {'charged' if meta.get('swap_charged') else 'not charged (swap-free account)'}")
    w(f"Net R per trade after commission{' and swap' if meta.get('swap_charged') else ''}; "
      f"dev = {years[0]}-{a.dev_to}, confirm = {a.dev_to + 1}-{years[-1]}. "
      f"Source: {dict(sources)}")
    w('')
    hdr = (f"{'playbook':<18}{'tf':<5}{'n':>6}{'dev':>8}{'conf':>8}{'all':>8}"
           f"{'90% all':>17}{'t':>6}{'yrs+':>7}{'buy':>8}{'sell':>8}  tier")
    w(hdr)
    for pb in PLAYBOOKS:
        for tf in TFS:
            rows = cells.get((pb, tf))
            if not rows:
                continue
            r_all = [r for (_y, _s, _d, r) in rows]
            dev = [r for (y, _s, _d, r) in rows if y <= a.dev_to]
            con = [r for (y, _s, _d, r) in rows if y > a.dev_to]
            buy = [r for (_y, s, _d, r) in rows if s == 'buy']
            sell = [r for (_y, s, _d, r) in rows if s == 'sell']
            by_year = {y: [r for (yy, _s, _d, r) in rows if yy == y] for y in years}
            m = lambda x: float(np.mean(x)) if x else float('nan')   # noqa: E731
            lo, hi = day_ci([(d, r) for (_y, _s, d, r) in rows])
            sd = float(np.std(r_all)) if len(r_all) > 1 else float('nan')
            t = m(r_all) / (sd / math.sqrt(len(r_all))) if len(r_all) > 1 and sd > 0 else float('nan')
            have = [y for y in years if len(by_year[y]) >= 5]
            pos = sum(1 for y in have if m(by_year[y]) > 0)
            md, mc = m(dev), m(con)
            if md > 0 and mc > 0:
                tier = '1' if len(r_all) >= 100 and lo > 0 else '2'
            elif mc > 0:
                tier = '3a'
            elif md > 0:
                tier = '3b'
            else:
                tier = '4'
            cell = {'playbook': pb, 'tf': tf, 'n': len(r_all), 'n_dev': len(dev), 'n_confirm': len(con),
                    'dev': md, 'confirm': mc, 'all': m(r_all), 'ci90': [lo, hi], 't': t,
                    'years_positive': pos, 'years_counted': len(have),
                    'by_year': {y: [m(by_year[y]), len(by_year[y])] for y in years},
                    'buy': m(buy), 'sell': m(sell), 'n_buy': len(buy), 'n_sell': len(sell),
                    'tier': tier}
            out.append(cell)
            w(f"{pb:<18}{tf:<5}{len(r_all):>6}{md:>+8.3f}{mc:>+8.3f}{m(r_all):>+8.3f}"
              f"  [{lo:+.3f},{hi:+.3f}]{t:>6.2f}{pos:>4}/{len(have):<2}{m(buy):>+8.3f}{m(sell):>+8.3f}  {tier}")
    w('')
    for tier, name in (('1', 'TIER 1 - positive in both periods, n >= 100, interval above 0'),
                       ('2', 'TIER 2 - positive in both periods'),
                       ('3a', 'TIER 3a - confirm only'), ('3b', 'TIER 3b - dev only'),
                       ('4', 'TIER 4 - negative in both')):
        names = [f"{c['playbook']} {c['tf']} (n={c['n']})" for c in out if c['tier'] == tier]
        w(f"{name}: {', '.join(names) if names else 'none'}")
    w('')
    w('By timeframe, all playbooks pooled (net R per trade, dev | confirm, n):')
    for tf in TFS:
        rows = [x for (pb, t2), v in cells.items() if t2 == tf for x in v]
        if not rows:
            continue
        dev = [r for (y, _s, _d, r) in rows if y <= a.dev_to]
        con = [r for (y, _s, _d, r) in rows if y > a.dev_to]
        w(f"  {tf:<4} {np.mean(dev) if dev else float('nan'):+.3f} | "
          f"{np.mean(con) if con else float('nan'):+.3f}   n={len(rows)}")
    w('')
    w('A scan of this many cells finds t ~ 2.2-2.5 in the best by chance alone. '
      'Measurements, not trading advice.')
    folder = BATCHES / a.tag
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'cells.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (folder / 'cells.json').write_text(json.dumps({'meta': meta, 'dev_to': a.dev_to,
                                                   'cells': out}, indent=1), encoding='utf-8')
    print('\n'.join(lines))
    print(f"\n-> {(folder / 'cells.txt').relative_to(ROOT)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
