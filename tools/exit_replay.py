#!/usr/bin/env python
"""
tools/exit_replay.py - exit handling re-tested on the lab path. BACKTEST ONLY.

The trade journal (tools/trade_journal_report.py) found the entries go the
right way far more often than random (65% reach +0.5R against 49%), yet the
trades lose: 15% reach +0.5R and are then stopped at -1R before the +1R lock
arms. This asks whether managing the +0.5R..+1R zone differently keeps more
of that - measured on the backtest LAB's trades (closed bars, no
higher-timeframe leak, fills from the fill minute), not the research
simulator the earlier exit study (Phase 1) used.

    python tools/exit_replay.py            all years, all arms (parallel by year)
    python tools/exit_replay.py --check    only the check below, one year

METHOD
    Each lab trade (runs/research/journal/lab_XAUUSD.a_5m_<year>.json) is
    re-opened at its own fill price and time in the lab's own simulated
    broker (server/lab/broker.SimBroker - the same minute path, spread,
    slippage and gap rules the lab used) and walked minute by minute, with
    the stop managed after every minute the way the live executor's _trail()
    manages it: arm on the 5m bars since the fill (the forming bar included),
    lock, then trail behind the high / low of CLOSED 5m bars, never at or
    through the market, only ever tightened. Entries are the lab's, unchanged;
    only the exit differs between arms. Position caps and the daily-loss
    limit are not re-run, so an arm that looks better goes to a full lab run
    before anything else.

    CHECK first: arm X0 (today's live exit) must reproduce the lab's own R on
    the same trades. If it does not, nothing below is read.

ARMS, fixed 2026-09-27 before any result was read (R from the fill)
    X0  today: arm at TP1 (1R from the signal entry), lock +0.5R, trail 1 ATR, TP = TP2
    A1  X0, and at +0.5R first move the stop to breakeven (0R)
    A2  X0, and at +0.5R first move the stop to -0.5R (half the risk)
    A3  arm at +0.75R, lock +0.25R, trail 1 ATR, TP = TP2
    A4  arm at +0.5R, lock 0R (breakeven), trail 1 ATR, TP = TP2
    A5  X0, and close HALF at +0.5R (the stop moves for both halves)
    A6  all out at +0.75R, no trail
    A7  no management: stop and TP2 only (reference)
    A8  X0 with a 0.5 ATR trail

    Each arm is also run on RANDOM entries - one per trade: a random 5m bar of
    the same month, random side, market entry, the same stop in ATR, TP1 at 1R
    and TP2 at the trade's own R multiple. Costs make every exit lose on
    random entries; an arm that 'improves' random entries as much as the
    real ones is not using the entries' direction.

READING, fixed in advance
    Net R per trade (commission, spread, slippage). An arm is BETTER than X0
    only if the paired difference (same trades) has a 90% day-block interval
    above zero in 2018-2023 AND in 2024-2026. Eight arms are compared, so a
    single borderline pass is expected by chance. BETTER means: build it as a
    switch (off in live), confirm it with full lab runs, then the shadow log -
    never straight to live.
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = ROOT / 'runs' / 'research' / 'journal'
OUT = ROOT / 'runs' / 'research' / 'exit_replay'
SYMBOL, TF, TF_MS = 'XAUUSD.a', '5m', 300_000
YEARS = list(range(2018, 2027))
DEV, CONFIRM = range(2018, 2024), range(2024, 2027)
MAX_MIN = 20 * 1440                  # a trade still open after 20 days is closed at market
SLIP_POINTS = 3.0                    # the lab's default_slippage_points

X0 = {'arm': 'tp1', 'lock': 0.5, 'trail': 1.0, 'tp': 'tp2'}
ARMS = {
    'X0': X0,
    'A1': {**X0, 'early': [(0.5, 0.0)]},
    'A2': {**X0, 'early': [(0.5, -0.5)]},
    'A3': {'arm': 0.75, 'lock': 0.25, 'trail': 1.0, 'tp': 'tp2'},
    'A4': {'arm': 0.5, 'lock': 0.0, 'trail': 1.0, 'tp': 'tp2'},
    'A5': {**X0, 'partial': (0.5, 0.5)},
    'A6': {'arm': None, 'tp': 0.75},
    'A7': {'arm': None, 'tp': 'tp2'},
    'A8': {**X0, 'trail': 0.5},
}


class Data:
    def __init__(self, years):
        from server.datafeed import load_disk
        from server.engine.indicators import atr
        from server.lab.data import spec as lab_spec
        self.spec = lab_spec(SYMBOL)
        yrs = sorted(set([min(years) - 1] + list(years) + [max(years) + 1]))
        yrs = [y for y in yrs if 2017 <= y <= datetime.now(timezone.utc).year]
        self.m1 = load_disk(SYMBOL, '1m', yrs)
        self.s5 = load_disk(SYMBOL, TF, yrs)
        self.atr5 = atr(self.s5.h, self.s5.l, self.s5.c, 14)
        self.point = float(self.spec.get('point') or 0.01)
        self.tick = float(self.spec.get('tick_size') or self.point)
        self.digits = int(self.spec.get('digits') or 2)
        self.floor = max(int(self.spec.get('stops_level_points') or 0) * self.point, self.tick)
        sp = self.m1.spread
        self.sp = sp if sp is not None else np.full(len(self.m1), 20.0)


def simulate(d: Data, tr: dict, arm: dict) -> dict | None:
    """One trade, one exit arm, in the lab's broker. Prices are bid, as the lab's."""
    from server.lab.broker import SimBroker
    from server.signal_store import round_to_tick
    m1 = d.m1
    buy = tr['side'] == 'buy'
    sign = 1.0 if buy else -1.0
    fill, stop = float(tr['fill_price']), float(tr['stop'])
    risk = abs(fill - stop)
    if not risk > 0:
        return None
    fill_ms = int(tr['fill_ms'])
    # A market order fills before the minute at fill_ms is walked; a pending
    # one fills part-way through that minute, so its walk starts at the next.
    j0 = int(np.searchsorted(m1.t, fill_ms, 'left' if tr.get('kind') == 'market' else 'right'))
    if j0 >= len(m1):
        return None
    lvl = lambda r: fill + sign * r * risk                                   # noqa: E731
    tp_px = (float(tr['tp2']) if arm['tp'] == 'tp2' else lvl(arm['tp']) if arm['tp'] else 0.0)
    arm_px = (float(tr['tp1']) if arm.get('arm') == 'tp1'
              else lvl(arm['arm']) if arm.get('arm') is not None else None)
    atr = float(tr.get('atr') or 0.0)

    now = [fill_ms]
    b = SimBroker(SYMBOL, d.spec, 1e7, SLIP_POINTS, clock=lambda: now[0])
    b.set_quote(float(m1.o[j0]), float(d.sp[j0]))
    parts = [(1.0, tp_px)]
    if arm.get('partial'):
        at_r, frac = arm['partial']
        parts = [(frac, lvl(at_r)), (1.0 - frac, tp_px)]
    for lots, tpx in parts:
        b._open(tr['side'], lots, fill, stop, tpx, 'replay', b._next())

    # 5m bars since the fill's bar, built from minutes as the lab's bars_now does.
    fill_bar = (fill_ms // TF_MS) * TF_MS
    a0 = int(np.searchsorted(m1.t, fill_bar, 'left'))
    bars: list = []                     # [t_open, high, low]

    def add_minute(j):
        t = (int(m1.t[j]) // TF_MS) * TF_MS
        if bars and bars[-1][0] == t:
            bars[-1][1] = max(bars[-1][1], float(m1.h[j]))
            bars[-1][2] = min(bars[-1][2], float(m1.l[j]))
        else:
            bars.append([t, float(m1.h[j]), float(m1.l[j])])
    for j in range(a0, j0):
        add_minute(j)

    armed_t = None
    early = list(arm.get('early') or [])
    mfe = 0.0
    j = j0
    while b._pos and j < min(len(m1), j0 + MAX_MIN):
        mt = int(m1.t[j])
        now[0] = mt
        b.run_minute(mt, float(m1.o[j]), float(m1.h[j]), float(m1.l[j]), float(m1.c[j]),
                     float(d.sp[j]))
        add_minute(j)
        mfe = max(mfe, ((float(m1.h[j]) - fill) if buy else (fill - float(m1.l[j]))) / risk)
        j += 1
        if not b._pos:
            break
        now[0] = mt + 60_000
        cur = b.bid if buy else b.ask
        reached = lambda px: any((x[1] >= px) if buy else (x[2] <= px) for x in bars) or \
            ((cur >= px) if buy else (cur <= px))                            # noqa: E731
        target = None
        # stages before the arm: a fixed stop once a level has printed
        for trig, to in early:
            if reached(lvl(trig)):
                target = lvl(to) if target is None else (max(target, lvl(to)) if buy
                                                         else min(target, lvl(to)))
        if arm_px is not None:
            if armed_t is None:
                armed_t = next((x[0] for x in bars if ((x[1] >= arm_px) if buy
                                                        else (x[2] <= arm_px))), None)
                if armed_t is None and ((cur >= arm_px) if buy else (cur <= arm_px)):
                    armed_t = bars[-1][0]
            if armed_t is not None:
                t_lock = lvl(arm['lock'])
                closed = [x for x in bars if x[0] >= armed_t and x[0] + TF_MS <= now[0]]
                if closed and atr > 0 and arm.get('trail'):
                    ref = max(x[1] for x in closed) if buy else min(x[2] for x in closed)
                    cand = ref - sign * arm['trail'] * atr
                    t_lock = max(t_lock, cand) if buy else min(t_lock, cand)
                target = t_lock if target is None else (max(target, t_lock) if buy
                                                        else min(target, t_lock))
        if target is None:
            continue
        target = min(target, b.bid - d.floor) if buy else max(target, b.ask + d.floor)
        target = round_to_tick(target, d.tick, d.digits)
        for p in list(b._pos):
            cur_sl = float(p['sl'] or 0)
            if (target > cur_sl + d.tick / 2) if buy else (cur_sl == 0 or target < cur_sl - d.tick / 2):
                b.trade('/order/modify', ticket=p['ticket'], sl=target)

    # anything still open: closed at market, as a manual close would be
    for p in list(b._pos):
        b._close(p, b.bid if buy else b.ask, 0)
    exits = [x for x in b._deals if x['entry'] == 1]
    lots = sum(x['volume'] for x in exits)
    px_r = sum(x['volume'] * sign * (x['price'] - fill) for x in exits) / (risk * lots)
    comm = 2 * float(d.spec.get('commission_per_lot_side') or 0) * lots
    net_money = sum(x['profit'] for x in exits) - comm
    vpu = float(d.spec.get('tick_value') or 1.0) / d.tick
    reason = exits[-1]['reason']
    return {'r': px_r, 'net_r': net_money / (risk * vpu * lots),
            'outcome': 'tp' if reason == 5 else ('stop' if reason == 4 else 'open'),
            'exit_ms': int(exits[-1]['time_ms']), 'mfe': mfe}


def random_twin(d: Data, tr: dict, g) -> dict | None:
    """A random market entry matched to one trade (see RANDOM in the header)."""
    s = d.s5
    fm = datetime.fromtimestamp(int(tr['fill_ms']) / 1000, timezone.utc)
    m0 = int(datetime(fm.year, fm.month, 1, tzinfo=timezone.utc).timestamp() * 1000)
    m_end = int(datetime(fm.year + (fm.month == 12), fm.month % 12 + 1, 1,
                         tzinfo=timezone.utc).timestamp() * 1000)
    lo, hi = max(15, int(np.searchsorted(s.t, m0))), int(np.searchsorted(s.t, m_end)) - 1
    atr0, risk0 = float(tr.get('atr') or 0), abs(float(tr['fill_price']) - float(tr['stop']))
    if hi - lo < 10 or not atr0 > 0 or not risk0 > 0:
        return None
    k = int(g.integers(lo, hi))
    a = float(d.atr5[k - 1])
    if not a > 0:
        return None
    buy = bool(g.random() < 0.5)
    sign = 1.0 if buy else -1.0
    j = int(np.searchsorted(d.m1.t, int(s.t[k]), 'left'))
    if j >= len(d.m1):
        return None
    bid = float(d.m1.o[j])
    slip = SLIP_POINTS * d.point
    fill = bid + float(d.sp[j]) * d.point + slip if buy else bid - slip
    risk = risk0 / atr0 * a
    tp2_r = abs(float(tr['tp2']) - float(tr['fill_price'])) / risk0
    return {'side': 'buy' if buy else 'sell', 'kind': 'market', 'fill_ms': int(d.m1.t[j]),
            'fill_price': fill, 'stop': fill - sign * risk, 'tp1': fill + sign * risk,
            'tp2': fill + sign * tp2_r * risk, 'atr': a}


def load_trades(year: int) -> list:
    doc = json.loads((SRC / f'lab_{SYMBOL}_{TF}_{year}.json').read_text(encoding='utf-8'))
    return [x for x in doc['journal'] if x.get('fill_ms') and x.get('fill_price')
            and x.get('outcome') != 'manual']


def run_year(year: int) -> dict:
    from tools.forecast_build import _below_normal
    _below_normal()
    trades = load_trades(year)
    d = Data([year])
    g = np.random.default_rng(year)
    out, t0 = [], time.time()
    for n, tr in enumerate(trades):
        twin = random_twin(d, tr, g)
        row = {'id': tr['signal_id'], 'year': year, 'day': int(tr['fill_ms']) // 86_400_000,
               'playbook': tr['playbook'], 'lab_r': tr.get('r_multiple'),
               'lab_outcome': tr.get('outcome'), 'arms': {}, 'rnd': {}}
        for name, arm in ARMS.items():
            row['arms'][name] = simulate(d, tr, arm)
            if twin is not None:
                row['rnd'][name] = simulate(d, twin, arm)
        out.append(row)
        if n % 200 == 0:
            print(f'  {year}: {n}/{len(trades)}  {time.time() - t0:5.0f}s', flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f'replay_{year}.pkl').write_bytes(pickle.dumps(out))
    print(f'{year}: {len(out)} trades ({time.time() - t0:.0f}s)', flush=True)
    return {'year': year, 'n': len(out)}


# --------------------------------------------------------------------------- #
# report                                                                      #
# --------------------------------------------------------------------------- #
def paired_ci(rows: list, a: str, b: str, key='arms', reps=2000, seed=5) -> tuple:
    """Mean of (a - b) net R over the same trades, with a 90% day-block interval."""
    days: dict = {}
    for r in rows:
        x, y = r[key].get(a), r[key].get(b)
        if x and y:
            days.setdefault(r['day'], []).append(x['net_r'] - y['net_r'])
    if len(days) < 10:
        return float('nan'), float('nan'), float('nan'), 0
    grp = [np.array(v) for v in days.values()]
    s = np.array([v.sum() for v in grp])
    c = np.array([v.size for v in grp])
    g = np.random.default_rng(seed)
    pick = g.integers(0, len(grp), (reps, len(grp)))
    m = s[pick].sum(1) / c[pick].sum(1)
    return float(s.sum() / c.sum()), float(np.percentile(m, 5)), float(np.percentile(m, 95)), int(c.sum())


def mean(rows, arm, key='arms'):
    v = [r[key][arm]['net_r'] for r in rows if r[key].get(arm)]
    return float(np.mean(v)) if v else float('nan'), len(v)


def report(check_only: bool = False) -> int:
    rows = []
    for y in YEARS:
        f = OUT / f'replay_{y}.pkl'
        if f.exists():
            rows += pickle.loads(f.read_bytes())
    if not rows:
        raise SystemExit('no replay files - run without --report first')
    L = []
    w = L.append
    w(f'EXIT REPLAY on the lab path - {SYMBOL} {TF}, lab trades with live settings, '
      f'{len(rows)} trades   {datetime.now():%Y-%m-%d %H:%M}')
    w('Arms and reading fixed in advance: tools/exit_replay.py header. Net R includes '
      'commission, spread and slippage.')

    ok = [r for r in rows if r['arms'].get('X0') and r['lab_r'] is not None]
    diff = np.array([r['arms']['X0']['r'] - float(r['lab_r']) for r in ok])
    same_out = np.mean([(r['arms']['X0']['outcome'] == 'stop') ==
                        (r['lab_outcome'] in ('stop', 'trail')) for r in ok])
    close = float(np.mean(np.abs(diff) <= 0.05))
    w(f'\nCHECK - X0 against the lab on the same trades: {close * 100:.1f}% within 0.05R, '
      f'median |diff| {np.median(np.abs(diff)):.3f}R, mean diff {diff.mean():+.4f}R, '
      f'mean lab R {np.mean([float(r["lab_r"]) for r in ok]):+.4f} vs replay '
      f'{np.mean([r["arms"]["X0"]["r"] for r in ok]):+.4f}; stop-or-not agrees {same_out * 100:.1f}%')
    passed = close >= 0.9 and abs(diff.mean()) <= 0.01
    w(f'  -> {"REPRODUCES the lab - the arms below can be read" if passed else "DOES NOT reproduce the lab - do not read the arms"}')
    if check_only or not passed:
        txt = '\n'.join(L)
        print(txt)
        (OUT / 'report.txt').write_text(txt, encoding='utf-8')
        return 0

    dev = [r for r in rows if r['year'] in DEV]
    cf = [r for r in rows if r['year'] in CONFIRM]
    w(f"\n{'arm':<5}{'2018-23 R':>11}{'- X0':>8}{'90% interval':>20}{'2024-26 R':>12}{'- X0':>8}"
      f"{'90% interval':>20}   random: 2018-23 / 2024-26 - X0   verdict")
    for name in ARMS:
        cells = []
        verdict = []
        for part in (dev, cf):
            m, _ = mean(part, name)
            d, lo, hi, _ = paired_ci(part, name, 'X0')
            cells.append(f'{m:>+11.3f}{d:>+8.3f}   [{lo:+.3f}, {hi:+.3f}]')
            verdict.append(lo > 0)
        rnd = [paired_ci(part, name, 'X0', key='rnd')[0] for part in (dev, cf)]
        better = name != 'X0' and all(verdict)
        w(f'{name:<5}' + ' '.join(cells) + f'   {rnd[0]:+.3f} / {rnd[1]:+.3f}'
          + ('   BETTER' if better else ''))

    w('\nPer year, net R per trade:')
    w('      ' + ''.join(f'{n:>8}' for n in ARMS))
    for y in YEARS:
        part = [r for r in rows if r['year'] == y]
        w(f'{y}  ' + ''.join(f'{mean(part, n)[0]:>+8.3f}' for n in ARMS))
    w('\nPer playbook, 2018-26, net R per trade:')
    for pb in sorted({r['playbook'] for r in rows}):
        part = [r for r in rows if r['playbook'] == pb]
        w(f'{pb[:14]:<16}' + ''.join(f'{mean(part, n)[0]:>+8.3f}' for n in ARMS) + f'   n {len(part)}')
    w('\nRandom entries, 2018-26, net R per trade (every exit should lose here):')
    w('      ' + ''.join(f'{mean(rows, n, "rnd")[0]:>+8.3f}' for n in ARMS))
    txt = '\n'.join(L)
    (OUT / 'report.txt').write_text(txt, encoding='utf-8')
    print(txt)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--check', action='store_true', help='one year, then only the X0 check')
    ap.add_argument('--year', type=int, help='run one year only')
    ap.add_argument('--report', action='store_true', help='report from saved replays')
    a = ap.parse_args()
    if a.report:
        return report()
    if a.check or a.year:
        run_year(a.year or 2025)
        return report(check_only=a.check)
    with ProcessPoolExecutor(max_workers=4) as ex:
        list(ex.map(run_year, YEARS))
    return report()


if __name__ == '__main__':
    sys.exit(main())
