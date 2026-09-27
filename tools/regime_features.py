#!/usr/bin/env python
"""
tools/regime_features.py - do richer regime features separate good trades from bad?

Read-only, descriptive: NO rule, filter or weight comes out of this file. It
answers one question per feature - do trades taken when the feature is high
do reliably better or worse than when it is low, in BOTH periods?

    python tools/regime_features.py

TRADES   the backtest lab's journal (tools/trade_journal_backfill.py lab):
         XAUUSD.a 5m, 2018-2026, the live executor and today's live settings,
         minute-by-minute broker, closed bars only. Net R = profit / money at
         risk (commission and spread included).

FEATURES, each on the closed 5m bars up to and including the bar that made
the signal (fixed 2026-09-27 before any result was read)
    vol_expansion   ATR14 now / ATR14 12 bars ago (> 1 = volatility expanding)
    vol_pct         ATR14's percentile in its own last 500 bars
    efficiency      |close - close 20 bars ago| / sum of |bar-to-bar moves| over
                    those 20 bars (Kaufman; 1 = a straight line), SIGNED + when the
                    20-bar move is in the trade's direction
    ma_spread       (EMA20 - EMA100) / ATR14, signed + when with the trade
    variance_ratio  var(4-bar returns) / (4 x var(1-bar returns)) over 200 bars
                    (< 1 mean-reverting, > 1 trending; the Hurst question, stably)
    volume_ratio    mean tick volume of the last 3 bars / median of the 50 before
    atr_cost        ATR14 / (spread + round-trip commission), both in price - how
                    many round trips one ATR is worth

READING, fixed in advance
    Quintile edges come from 2018-2023 only and are applied unchanged to
    2024-2026. A feature SEPARATES if the difference in mean net R between its
    best and its worst quintile has a 90% day-block interval that excludes zero
    in 2018-2023 AND in 2024-2026, with the same sign. Seven features are
    compared, so one passing by chance is plausible: passing is a reason to
    pre-register ONE threshold rule and test it through the lab, not a result.
    A feature whose scale drifts with the gold price (atr_cost: gold went from
    ~1,900 to ~4,500 while the spread in points did not) can put most of
    2024-2026 into one 2018-2023 quintile. Where more than 60% of the 2024-2026
    trades fall in one quintile, 2024-2026 is cut at ITS OWN quintile edges and
    the report says so (added 2026-09-27, before any real result was read).
    Also shown, as asked: the 2 x 2 of 'ATR in its top 10%' x 'expanding'.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = ROOT / 'runs' / 'research' / 'journal'
OUT = SRC / 'regime_features.txt'
SYMBOL, TF = 'XAUUSD.a', '5m'
DEV, CONFIRM = range(2018, 2024), range(2024, 2027)
FEATURES = ['vol_expansion', 'vol_pct', 'efficiency', 'ma_spread', 'variance_ratio',
            'volume_ratio', 'atr_cost']


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x)
    k = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, x.size):
        out[i] = out[i - 1] + k * (x[i] - out[i - 1])
    return out


def load_trades() -> list:
    rows = []
    for f in sorted(SRC.glob(f'lab_{SYMBOL}_{TF}_*.json')):
        doc = json.loads(f.read_text(encoding='utf-8'))
        rows += [x for x in doc.get('journal') or [] if x.get('outcome') != 'manual']
    return rows


def features(trades: list) -> list:
    from server.datafeed import load_disk
    from server.engine.indicators import atr
    from server.lab.data import spec as lab_spec
    spec = lab_spec(SYMBOL)
    s = load_disk(SYMBOL, TF, list(range(2017, datetime.now(timezone.utc).year + 1)))
    c, v = s.c.astype(float), (s.v.astype(float) if s.v is not None else None)
    a = atr(s.h, s.l, s.c, 14)
    e20, e100 = ema(c, 20), ema(c, 100)
    point = float(spec.get('point') or 0.01)
    per_price = float(spec.get('tick_value') or 1) / float(spec.get('tick_size') or point)
    comm_price = float(spec.get('commission_per_lot_side') or 0) * 2 / per_price
    r1 = np.diff(np.log(c), prepend=np.log(c[0]))
    out = []
    for x in trades:
        if not x.get('fill_ms') or not x.get('final_bar_ms'):
            continue                    # no fill time recorded (a handful of lab closes)
        i = int(np.searchsorted(s.t, int(x['final_bar_ms'])))
        if i >= len(s) or int(s.t[i]) != int(x['final_bar_ms']) or i < 520:
            continue
        side = 1.0 if x['side'] == 'buy' else -1.0
        move = c[i] - c[i - 20]
        path = np.abs(np.diff(c[i - 20:i + 1])).sum()
        rr = r1[i - 199:i + 1]
        r4 = np.log(c[i - 196:i + 1]) - np.log(c[i - 200:i - 3])
        sp = float(s.spread[i]) * point if s.spread is not None else 20 * point
        f = {
            'vol_expansion': a[i] / a[i - 12] if a[i - 12] > 0 else np.nan,
            'vol_pct': float((a[i - 499:i + 1] <= a[i]).mean() * 100),
            'efficiency': (np.sign(move) * side) * (abs(move) / path if path > 0 else 0.0),
            'ma_spread': side * (e20[i] - e100[i]) / a[i] if a[i] > 0 else np.nan,
            'variance_ratio': (r4.var() / (4 * rr.var())) if rr.var() > 0 else np.nan,
            'volume_ratio': (v[i - 2:i + 1].mean() / np.median(v[i - 52:i - 2])
                             if v is not None and np.median(v[i - 52:i - 2]) > 0 else np.nan),
            'atr_cost': a[i] / (sp + comm_price) if sp + comm_price > 0 else np.nan,
        }
        risk_money = abs(float(x['fill_price']) - float(x['stop'])) * per_price * float(x['lots'] or 0)
        if not risk_money > 0 or x.get('profit') is None:
            continue
        out.append({'year': datetime.fromtimestamp(int(x['fill_ms']) / 1000, timezone.utc).year,
                    'day': int(x['fill_ms']) // 86_400_000, 'playbook': x['playbook'],
                    'net_r': float(x['profit']) / risk_money, **f})
    return out


def diff_ci(a: list, b: list, reps: int = 2000, seed: int = 9) -> tuple:
    """90% day-block interval of mean(a) - mean(b)."""
    def groups(xs):
        d = {}
        for t in xs:
            d.setdefault(t['day'], []).append(t['net_r'])
        return [np.array(g) for g in d.values()]
    ga, gb = groups(a), groups(b)
    if len(ga) < 10 or len(gb) < 10:
        return float('nan'), float('nan')
    rng = np.random.default_rng(seed)
    sa, na = np.array([g.sum() for g in ga]), np.array([g.size for g in ga])
    sb, nb = np.array([g.sum() for g in gb]), np.array([g.size for g in gb])
    pa = rng.integers(0, len(ga), (reps, len(ga)))
    pb = rng.integers(0, len(gb), (reps, len(gb)))
    d = sa[pa].sum(1) / na[pa].sum(1) - sb[pb].sum(1) / nb[pb].sum(1)
    return float(np.percentile(d, 5)), float(np.percentile(d, 95))


def main() -> int:
    trades = load_trades()
    if not trades:
        raise SystemExit(f'no lab journal in {SRC} - run tools/trade_journal_backfill.py lab')
    rows = features(trades)
    dev = [t for t in rows if t['year'] in DEV]
    cf = [t for t in rows if t['year'] in CONFIRM]
    lines = []
    w = lines.append
    w(f'REGIME FEATURES vs net R - {SYMBOL} {TF} backtest lab, live settings, '
      f'{len(dev)} trades 2018-23 | {len(cf)} trades 2024-26   {datetime.now():%Y-%m-%d %H:%M}')
    w('Quintile edges from 2018-2023. SEPARATES = best-minus-worst quintile interval '
      'excludes 0 in both periods, same sign. Descriptive only.')
    w(f'Baseline mean net R: 2018-23 {np.mean([t["net_r"] for t in dev]):+.3f}  '
      f'2024-26 {np.mean([t["net_r"] for t in cf]):+.3f}')
    for f in FEATURES:
        d_ok = [t for t in dev if np.isfinite(t[f])]
        edges = np.nanpercentile([t[f] for t in d_ok], [20, 40, 60, 80])
        q = lambda t: int(np.searchsorted(edges, t[f], 'right'))       # noqa: E731
        w(f'\n{f}   quintile edges {", ".join(f"{e:.3g}" for e in edges)}')
        per = {}
        for name, part in (('2018-23', dev), ('2024-26', cf)):
            ok = [t for t in part if np.isfinite(t[f])]
            buckets = [[t for t in ok if q(t) == k] for k in range(5)]
            note = ''
            if name == '2024-26' and ok and max(len(b) for b in buckets) > 0.6 * len(ok):
                own = np.nanpercentile([t[f] for t in ok], [20, 40, 60, 80])
                buckets = [[t for t in ok if int(np.searchsorted(own, t[f], 'right')) == k]
                           for k in range(5)]
                note = f'   (own quintiles {", ".join(f"{e:.3g}" for e in own)}: the scale moved)'
            per[name] = buckets
            w(f'  {name}  ' + '  '.join(
                f'Q{k + 1} {len(b):>5} {np.mean([t["net_r"] for t in b]) if b else float("nan"):+.3f}'
                for k, b in enumerate(buckets)) + note)
        means = [np.mean([t['net_r'] for t in b]) if b else np.nan for b in per['2018-23']]
        hi, lo = int(np.nanargmax(means)), int(np.nanargmin(means))
        verdicts = []
        for name in ('2018-23', '2024-26'):
            b = per[name]
            m = np.mean([t['net_r'] for t in b[hi]]) - np.mean([t['net_r'] for t in b[lo]]) \
                if b[hi] and b[lo] else np.nan
            ci = diff_ci(b[hi], b[lo])
            verdicts.append((m, ci))
            w(f'  {name}  Q{hi + 1} - Q{lo + 1} (best - worst in 2018-23) {m:+.3f}  '
              f'90% [{ci[0]:+.3f}, {ci[1]:+.3f}]')
        sep = all(ci[0] > 0 for _, ci in verdicts)
        w(f'  -> {"SEPARATES" if sep else "does not separate"}')

    w('\nAsked for: ATR in its top 10% x volatility expanding (vol_expansion > 1)')
    for name, part in (('2018-23', dev), ('2024-26', cf)):
        cells = []
        for top in (False, True):
            for exp in (False, True):
                b = [t for t in part if (t['vol_pct'] >= 90) == top and (t['vol_expansion'] > 1) == exp]
                cells.append(f"{'top10' if top else 'rest '}/{'expanding ' if exp else 'contracting'} "
                             f"{len(b):>5} {np.mean([t['net_r'] for t in b]) if b else float('nan'):+.3f}")
        w(f'  {name}  ' + '   '.join(cells))

    txt = '\n'.join(lines)
    OUT.write_text(txt, encoding='utf-8')
    print(txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
