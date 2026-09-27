#!/usr/bin/env python
"""
tools/trade_journal_report.py - how the trades went wrong, by playbook, regime and period.

Read-only. Pure measurement: nothing here feeds an entry, a gate or an exit.

    python tools/trade_journal_report.py                   live + backtest, by month
    python tools/trade_journal_report.py --by week --source live --since 2026-09-01

SOURCES (server/trade_journal.py writes the lines, tools/trade_journal_backfill.py
the ones from before it existed)
    live       order_ledger/logs/trade_journal.jsonl, then trade_journal_backfill.jsonl
               for trades the live journal does not have
    backtest   runs/lab/<session>/journal.json (your lab sessions) and
               runs/research/journal/lab_*.json (headless lab runs, live settings)

DEFINITIONS, fixed 2026-09-27 before any result was read
    R            |fill - initial stop|
    path         the 1m bars from the fill's minute to the exit's minute (disk
                 data; live times moved onto the disk clock, New York + 7 h)
    MFE / MAE    best / worst price on that path against the fill, in R
                 (MAE shown negative)
    thesis right       MFE >= 0.5R before the exit
    right, stopped     outcome 'stop' and MFE >= 0.5R first ("right idea, stopped out")
    never right        MFE < 0.25R ("never went the right way")
    stopped, then TP1  outcome 'stop', and within 24 bars of the trade's timeframe
                       after the exit price reached the trade's TP1 (fill + 1R)
    stopped early      outcome 'stop' within 3 bars of the fill
    regime changed     the regime at the exit bar differs from the one at entry
    manual             closed by hand - counted, left out of every rate

    RANDOM  every rate is shown beside the same rate for random entries: for each
    trade, 5 entries at random bar opens of the same symbol, timeframe and month,
    random side, the same stop in ATR, held for the trade's own number of bars
    (or until that stop). Gold's 5m legs are close to a random walk (Phase 0), so
    a rate means something only where it differs from random.

    Cells with fewer than 30 trades are marked '(few)'.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIVE = [ROOT / 'order_ledger' / 'logs' / 'trade_journal.jsonl',
        ROOT / 'order_ledger' / 'logs' / 'trade_journal_backfill.jsonl']
LAB_SESSIONS = ROOT / 'runs' / 'lab'
LAB_BACKFILL = ROOT / 'runs' / 'research' / 'journal'
OUT = ROOT / 'runs' / 'research' / 'journal' / 'report.txt'
NY = ZoneInfo('America/New_York')
RIGHT, NEVER, AFTER_BARS, EARLY_BARS, RANDOM_K, FEW = 0.5, 0.25, 24, 3, 5, 30
TF_MIN = {'1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '2h': 120,
          '4h': 240, '1d': 1440}


def broker_offset(utc_ms: int) -> int:
    t = datetime.fromtimestamp(utc_ms / 1000, timezone.utc).astimezone(NY)
    return int((t.utcoffset() + timedelta(hours=7)).total_seconds() * 1000)


# --------------------------------------------------------------------------- #
# loading                                                                     #
# --------------------------------------------------------------------------- #
def load(source: str) -> list:
    out, seen = [], set()
    if source in ('live', 'all'):
        for f in LIVE:
            if f.exists():
                for line in f.read_text(encoding='utf-8').splitlines():
                    try:
                        x = json.loads(line)
                    except ValueError:
                        continue
                    if x.get('signal_id') not in seen:
                        seen.add(x.get('signal_id'))
                        out.append(dict(x, src='live'))
    if source in ('backtest', 'all'):
        files = sorted(LAB_SESSIONS.glob('*/journal.json')) + sorted(LAB_BACKFILL.glob('lab_*.json'))
        for f in files:
            try:
                doc = json.loads(f.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            rows = doc.get('journal') if isinstance(doc, dict) else doc
            tag = f.parent.name if f.name == 'journal.json' else f.stem
            for x in rows or []:
                key = (tag, x.get('signal_id'))
                if key not in seen:
                    seen.add(key)
                    out.append(dict(x, src='backtest', run=tag))
    return out


class Paths:
    """1m and timeframe bars per symbol, loaded once."""

    def __init__(self):
        self.m1, self.tf, self.atr = {}, {}, {}

    def one(self, symbol):
        from server.datafeed import load_disk
        if symbol not in self.m1:
            yrs = list(range(2017, datetime.now(timezone.utc).year + 1))
            s = load_disk(symbol, '1m', yrs)
            self.m1[symbol] = None if s.empty() else s
        return self.m1[symbol]

    def bars(self, symbol, tf):
        from server.datafeed import load_disk
        from server.engine.indicators import atr
        key = (symbol, tf)
        if key not in self.tf:
            yrs = list(range(2017, datetime.now(timezone.utc).year + 1))
            s = load_disk(symbol, tf, yrs)
            self.tf[key] = None if s.empty() else s
            self.atr[key] = None if s.empty() else atr(s.h, s.l, s.c, 14)
        return self.tf[key], self.atr[key]


def walk(m1, a: int, b: int, entry: float, risk: float, buy: bool):
    """(mfe, mae, stop_hit_at) in R over m1[a:b], stopping at the first -1R."""
    h, l = m1.h[a:b], m1.l[a:b]
    if h.size == 0:
        return None
    fav = np.maximum.accumulate((h - entry) if buy else (entry - l)) / risk
    adv = np.maximum.accumulate((entry - l) if buy else (h - entry)) / risk
    hit = np.flatnonzero(adv >= 1.0)
    k = int(hit[0]) if hit.size else h.size - 1
    return float(max(0.0, fav[k])), float(max(0.0, adv[k])), (a + k if hit.size else None)


def reached(m1, a: int, n: int, level: float, buy: bool) -> bool:
    h, l = m1.h[a:a + n], m1.l[a:a + n]
    return bool(h.size and ((h.max() >= level) if buy else (l.min() <= level)))


def measure(x: dict, P: Paths) -> dict | None:
    """The path facts of one journal line, or None without price data."""
    try:
        fill, stop = float(x['fill_price']), float(x['stop'])
        f_ms, e_ms = int(x['fill_ms']), int(x['exit_ms'])
    except (KeyError, TypeError, ValueError):
        return None
    risk = abs(fill - stop)
    m1 = P.one(x['symbol'])
    tfm = TF_MIN.get(x.get('tf'), 5)
    if not risk > 0 or m1 is None:
        return None
    if x.get('clock') == 'utc':
        f_ms, e_ms = f_ms + broker_offset(f_ms), e_ms + broker_offset(e_ms)
    a = int(np.searchsorted(m1.t, (f_ms // 60_000) * 60_000, 'left'))
    b = int(np.searchsorted(m1.t, e_ms, 'right'))
    if b <= a or b > len(m1) or int(m1.t[a]) - f_ms > 3_600_000:
        return None
    buy = x['side'] == 'buy'
    h, l = m1.h[a:b], m1.l[a:b]
    mfe = float(max(0.0, ((h.max() - fill) if buy else (fill - l.min())) / risk))
    mae = float(max(0.0, ((fill - l.min()) if buy else (h.max() - fill)) / risk))
    bars = (e_ms - f_ms) / 60_000 / tfm
    stop_out = x.get('outcome') == 'stop'
    after = stop_out and reached(m1, b, AFTER_BARS * tfm, fill + (risk if buy else -risk), buy)
    rg0 = x.get('regime_at_entry')
    rg1 = (x.get('exit_context') or {}).get('regime')
    return {'mfe': mfe, 'mae': mae, 'bars': bars,
            'right': mfe >= RIGHT, 'never': mfe < NEVER,
            'right_stopped': stop_out and mfe >= RIGHT, 'after_tp1': after,
            'early': stop_out and bars <= EARLY_BARS,
            'regime_changed': (rg0 != rg1) if (rg0 and rg1) else None,
            'stop_atr': risk / float(x.get('atr') or 0) if x.get('atr') else None,
            'm1_a': a, 'disk_ms': f_ms}


def random_twins(x: dict, m: dict, P: Paths, g) -> list:
    """RANDOM_K random entries matched to one trade (see RANDOM above)."""
    s, a14 = P.bars(x['symbol'], x.get('tf') or '5m')
    m1 = P.one(x['symbol'])
    if s is None or m1 is None or not m.get('stop_atr'):
        return []
    tfm = TF_MIN.get(x.get('tf'), 5)
    d = datetime.fromtimestamp(m['disk_ms'] / 1000, timezone.utc)
    m0 = int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp() * 1000)
    m_end = int((datetime(d.year + (d.month == 12), d.month % 12 + 1, 1,
                          tzinfo=timezone.utc)).timestamp() * 1000)
    lo, hi = int(np.searchsorted(s.t, m0)), int(np.searchsorted(s.t, m_end))
    lo = max(lo, 15)
    if hi - lo < 10:
        return []
    hold = max(1, int(round(m['bars']))) * tfm
    out = []
    for j in g.integers(lo, hi, RANDOM_K):
        atr_j = float(a14[j - 1])
        if not atr_j > 0:
            continue
        buy = bool(g.random() < 0.5)
        entry, risk = float(s.o[j]), m['stop_atr'] * atr_j
        a = int(np.searchsorted(m1.t, int(s.t[j]), 'left'))
        w = walk(m1, a, min(len(m1), a + hold), entry, risk, buy)
        if w is None:
            continue
        mfe, _, hit = w
        after = hit is not None and reached(m1, hit + 1, AFTER_BARS * tfm,
                                            entry + (risk if buy else -risk), buy)
        out.append({'right': mfe >= RIGHT, 'never': mfe < NEVER,
                    'right_stopped': hit is not None and mfe >= RIGHT, 'after_tp1': after})
    return out


# --------------------------------------------------------------------------- #
# the report                                                                  #
# --------------------------------------------------------------------------- #
def pct(v) -> str:
    return '   -' if v is None or v != v else f'{100 * v:4.0f}'


def rate(rows, key):
    v = [r[key] for r in rows if r.get(key) is not None]
    return float(np.mean(v)) if v else None


def block(rows: list, label: str, w, header: bool = True) -> None:
    if header:
        w(f"  {'':<30}{'n':>6}{'man':>5}{'meanR':>8}{'right%':>8}{'rnd':>5}{'r+stop%':>9}{'rnd':>5}"
          f"{'never%':>8}{'rnd':>5}{'stop>TP1%':>10}{'rnd':>5}{'early%':>8}{'MFE':>7}{'MAE':>7}"
          f"{'rg chg%':>8}")
    ok = [r for r in rows if r['m'] is not None and r['x'].get('outcome') != 'manual']
    man = sum(1 for r in rows if r['x'].get('outcome') == 'manual')
    if not ok:
        w(f'  {label:<30}{0:>6}{man:>5}')
        return
    M = [r['m'] for r in ok]
    rnd = [t for r in ok for t in r['rnd']]
    R = [float(r['x'].get('r_multiple') or 0) for r in ok]
    few = ' (few)' if len(ok) < FEW else ''
    w(f'  {label:<30}{len(ok):>6}{man:>5}{np.mean(R):>+8.3f}'
      f'{pct(rate(M, "right")):>8}{pct(rate(rnd, "right")):>5}'
      f'{pct(rate(M, "right_stopped")):>9}{pct(rate(rnd, "right_stopped")):>5}'
      f'{pct(rate(M, "never")):>8}{pct(rate(rnd, "never")):>5}'
      f'{pct(rate(M, "after_tp1")):>10}{pct(rate(rnd, "after_tp1")):>5}'
      f'{pct(rate(M, "early")):>8}{np.mean([m["mfe"] for m in M]):>+7.2f}'
      f'{-np.mean([m["mae"] for m in M]):>+7.2f}{pct(rate(M, "regime_changed")):>8}{few}')


def period_key(ms: int, by: str) -> str:
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    if by == 'week':
        y, wk, _ = d.isocalendar()
        return f'{y}-W{wk:02d}'
    return d.strftime('%Y-%m') if by == 'month' else str(d.year)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--source', choices=('live', 'backtest', 'all'), default='all')
    ap.add_argument('--by', choices=('week', 'month', 'year'), default='month')
    ap.add_argument('--since', help='YYYY-MM-DD, on the exit date')
    ap.add_argument('--symbol', help='only this symbol')
    a = ap.parse_args()

    rows = load(a.source)
    if a.symbol:
        rows = [x for x in rows if x.get('symbol') == a.symbol]
    if a.since:
        cut = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp() * 1000
        rows = [x for x in rows if int(x.get('exit_ms') or 0) >= cut]
    P, g = Paths(), np.random.default_rng(3)
    data = []
    for x in rows:
        m = measure(x, P)
        data.append({'x': x, 'm': m, 'rnd': random_twins(x, m, P, g) if m else []})

    lines = []
    w = lines.append
    w(f'TRADE JOURNAL - {a.source}, {len(data)} closed trades '
      f'({sum(1 for d in data if d["m"] is None)} without 1m price data), by {a.by}'
      + (f', since {a.since}' if a.since else '') + f'   {datetime.now():%Y-%m-%d %H:%M}')
    w('Definitions and the random comparison: tools/trade_journal_report.py header. '
      'Measurement, not trading advice.')
    w("Columns: right = MFE >= 0.5R; r+stop = right then stopped; never = MFE < 0.25R; "
      "stop>TP1 = stopped, then TP1 within 24 bars; rnd = random entries' rate.")

    for src in ('live', 'backtest'):
        part = [d for d in data if d['x']['src'] == src]
        if not part:
            continue
        w(f'\n=== {src.upper()} ===')
        w('\n1. By playbook')
        pbs = sorted({d['x'].get('playbook') for d in part}, key=str)
        for i, pb in enumerate(pbs):
            block([d for d in part if d['x'].get('playbook') == pb], str(pb), w, header=i == 0)
        block(part, 'ALL', w, header=False)

        w('\n2. Playbook x regime at entry')
        first = True
        for pb in pbs:
            for rg in sorted({d['x'].get('regime_at_entry') for d in part
                              if d['x'].get('playbook') == pb}, key=str):
                block([d for d in part if d['x'].get('playbook') == pb
                       and d['x'].get('regime_at_entry') == rg], f'{pb} / {rg}', w, header=first)
                first = False

        w(f'\n3. By {a.by} (exit date)')
        keys = sorted({period_key(int(d['x'].get('exit_ms') or 0), a.by) for d in part})
        first = True
        for k in keys:
            block([d for d in part if period_key(int(d['x'].get('exit_ms') or 0), a.by) == k],
                  k, w, header=first)
            first = False

        w('\n4. Most "right idea, stopped out" / most "never went the right way" (n >= 30)')
        cells = []
        for pb in pbs:
            ok = [d['m'] for d in part if d['x'].get('playbook') == pb and d['m']
                  and d['x'].get('outcome') != 'manual']
            rnd = [t for d in part if d['x'].get('playbook') == pb for t in d['rnd']]
            if len(ok) >= FEW:
                cells.append((pb, len(ok), rate(ok, 'right_stopped'), rate(rnd, 'right_stopped'),
                              rate(ok, 'never'), rate(rnd, 'never')))
        for title, i in (('right, stopped', 2), ('never right', 4)):
            order = sorted(cells, key=lambda c: -(c[i] or 0))
            w(f'  {title}: ' + ';  '.join(f'{c[0]} {pct(c[i]).strip()}% (random {pct(c[i + 1]).strip()}%, n {c[1]})'
                                          for c in order) if order else f'  {title}: no playbook has {FEW} trades yet')

    txt = '\n'.join(lines)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(txt, encoding='utf-8')
    print(txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
