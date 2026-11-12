#!/usr/bin/env python
"""
tools/range_shadow.py - step 2 of the range port: would the new ranges have
changed anything? Read-only; no engine behaviour changes.

docs/proposals/2026-11-08-range-engine-port.md. The engine's idea of a range
is the regime's range_high / range_low (highest high / lowest low over the
last regime_lookback bars). The chart's is server/engine/ranges.py (swing
clusters, two-close breakouts). This replays history and measures the gap
where it matters: false_break_fade, whose TP2 is the far side of the regime
box, and range_fade, whose edges ARE the regime box.

    capture   Replay analyse() -> generate() -> qualify_all() bar by bar,
              exactly as backtest.run() does (closed higher-timeframe bars
              only), and record every non-conflicted false_break_fade /
              range_fade candidate with the regime box AND the active range
              from ranges.py on the same 600-bar window. One job per year,
              run in parallel.
    report    Agreement between the two boxes, then the false_break_fade
              trades under four arms, through tools/exp_exits.evaluate()
              (backtest.run()'s entries, cooldown, gates, daily limits and
              exits, M1-resolved; costs = commission + spread + slippage):
                A   today: TP2 = far side of the regime box
                B1  TP2 = far edge of the active range; regime box when none
                B2  TP2 = far edge of the active range; no trade when none
                C   today's TP2, but only when an active range exists
              B1 vs A is the target change; C vs A is the filter alone;
              B2 vs C is the target change among the filtered trades.
              The reward gate (net R:R to TP2 >= min_rr) is re-applied per
              arm, so a new TP2 can let a signal in or keep it out.

    python tools/range_shadow.py capture --tf 30m --years 2018-2026 --workers 5
    python tools/range_shadow.py report  --tf 30m --years 2018-2026

Captures: runs/research/range_shadow/cap_<symbol>_<tf>_<year>.pkl (skipped
when present, so a stopped run resumes).
"""
from __future__ import annotations

import argparse
import copy
import pickle
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

OUT = ROOT / 'runs' / 'research' / 'range_shadow'
WINDOW = 600
PLAYBOOKS = ('false_break_fade', 'range_fade')


def _ms(y: int, m: int = 1, d: int = 1) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def cap_path(symbol, tf, year) -> Path:
    return OUT / f'cap_{symbol}_{tf}_{year}.pkl'


# =========================================================================== #
# capture                                                                     #
# =========================================================================== #
def capture(symbol: str, tf: str, year: int) -> dict:
    from server.config import MTF_LADDER, TF_SECONDS
    from server.datafeed import FEED, load_disk
    from server.engine.analysis import analyse, quick_trend
    from server.engine.levels import Level
    from server.engine.qualify import qualify_all
    from server.engine.ranges import active_range
    from server.engine.signals import _targets, generate

    spec = FEED.spec(symbol)
    start, end = _ms(year), _ms(year + 1)
    base_ms = TF_SECONDS[tf] * 1000
    # Warm-up: the first decision of the year needs WINDOW bars before it.
    series = load_disk(symbol, tf, None, start - base_ms * WINDOW * 3, end)
    first = int(np.searchsorted(series.t, start, 'left'))
    first = max(first, WINDOW)

    ladder = [x for x in MTF_LADDER.get(tf, [tf]) if x != tf]
    htf, htf_close = {}, {}
    for name in dict.fromkeys(ladder):
        s = load_disk(symbol, name, None, start - 3 * 365 * 86_400_000, end)
        if len(s) >= 60:
            htf[name] = s
            htf_close[name] = s.t + TF_SECONDS.get(name, 300) * 1000

    def mtf_at(ts: float) -> dict:
        # Closed higher-timeframe bars only, as backtest.run() since 2026-11-06.
        out = {}
        for name, s in htf.items():
            i = int(np.searchsorted(htf_close[name], ts + base_ms, 'right'))
            w = s.slice(max(0, i - 300), i)
            if len(w) >= 60:
                out[name] = quick_trend(w)
        return out

    point = float(spec.get('point') or 0.01)
    cands = []
    n_bars = 0
    t0 = time.time()
    for i in range(first, len(series)):
        w = series.slice(i - WINDOW, i)
        bar_t = float(series.t[i - 1])
        n_bars += 1
        snap = analyse(w, mtf_at(bar_t), spec)
        if not snap.get('ok'):
            continue
        ctx = {'equity': 25000.0, 'open_positions': 0, 'trades_today': 0, 'daily_pnl_pct': 0.0}
        raw = generate(snap, w, None, True)
        if not any(s.playbook in PLAYBOOKS for s in raw):
            continue
        pre = copy.deepcopy(raw)
        post = qualify_all(raw, snap, spec, ctx)
        regime = snap.get('regime') or {}
        rng = active_range(w.t, w.h, w.l, w.c, lookback=0)
        sp = snap.get('spread_points')
        spread_price = float(sp) * point if sp is not None else point * 20
        levels = [{k: v for k, v in lv.items() if k in Level.__annotations__}
                  for lv in (snap.get('levels') or [])]
        for p, q in zip(pre, post):
            if q.status == 'conflicted' or q.playbook not in PLAYBOOKS:
                continue
            c = {
                # Entry bar by TIME: the report replays a series that starts on
                # 1 January, without this capture's warm-up bars.
                'i': i, 'entry_t': int(series.t[i]) if i < len(series) else None,
                'bar_t': bar_t, 'playbook': q.playbook, 'side': q.side,
                'entry_type': q.entry_type, 'trigger': q.trigger,
                'entry': q.entry, 'stop': q.stop, 'tp1': q.tp1, 'tp2': q.tp2,
                'atr': float(q.atr_at_signal or snap.get('atr') or 0.0),
                'expiry': int(q.expiry_bars), 'raw_conf': int(p.confidence),
                'status': q.status,
                'gates': [(g['name'], g['verdict'], float(g.get('penalty') or 0)) for g in q.gates],
                'lots': float((q.sizing or {}).get('lots') or 0.0),
                'spread_price': spread_price, 'levels': levels,
                'regime': regime.get('state', ''),
                'regime_hi': regime.get('range_high'), 'regime_lo': regime.get('range_low'),
                'price': float(snap.get('price') or q.entry),
                'range': None, 'tp2_range': None,
            }
            if rng is not None:
                c['range'] = {'top': rng.top, 'bot': rng.bot, 'tier': rng.tier,
                              'bars': rng.i1 - rng.i0 + 1,
                              'touches': rng.touchesTop + rng.touchesBot}
                far = rng.top if q.side == 'buy' else rng.bot
                # The same rule the playbook applies to its own structural
                # target: accepted only between 1.2R and 6R away.
                c['tp2_range'] = _targets(q.entry, q.stop, q.side, far)[1]
            cands.append(c)
    return {'symbol': symbol, 'tf': tf, 'year': year, 'start_ms': start, 'end_ms': end,
            'spec': spec, 'bars': n_bars, 'secs': time.time() - t0, 'candidates': cands}


def run_capture(symbol, tf, years, workers):
    OUT.mkdir(parents=True, exist_ok=True)
    todo = [y for y in years if not cap_path(symbol, tf, y).exists()]
    print(f'{len(years) - len(todo)} captured already; {len(todo)} to do: {todo}', flush=True)

    def job(y):
        t0 = time.time()
        r = subprocess.run([sys.executable, __file__, '_one', '--symbol', symbol, '--tf', tf,
                            '--year', str(y)], capture_output=True, text=True)
        tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or ['']
        print(f'  {y}: exit {r.returncode} in {time.time() - t0:.0f}s  {tail[0]}', flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(job, todo))


# =========================================================================== #
# report                                                                      #
# =========================================================================== #
def report(symbol, tf, years):
    import exp_exits as xx
    xx.SYMBOL, xx.TF = symbol, tf

    caps = []
    for y in years:
        p = cap_path(symbol, tf, y)
        if p.exists():
            caps.append(pickle.loads(p.read_bytes()))
        else:
            print(f'  (no capture for {y})')
    if not caps:
        return

    print(f'\n{"=" * 100}\n  range shadow  {symbol} {tf}  {caps[0]["year"]}-{caps[-1]["year"]}'
          f'   costs: commission + spread + slippage   risk 0.5%/trade\n{"=" * 100}')

    # --- 1. agreement ------------------------------------------------------ #
    for pb in PLAYBOOKS:
        cs = [c for cap in caps for c in cap['candidates'] if c['playbook'] == pb]
        if not cs:
            print(f'\n  {pb}: no candidates')
            continue
        with_r = [c for c in cs if c['range']]
        print(f'\n  1. AGREEMENT  {pb}: {len(cs)} candidates, '
              f'{len(with_r)} ({len(with_r) / len(cs) * 100:.0f}%) inside an active range')
        if with_r:
            tiers = {t: sum(1 for c in with_r if c['range']['tier'] == t)
                     for t in ('major', 'minor', 'developing')}
            print(f'     tiers: {tiers}')
        both = [c for c in with_r if c['regime_hi'] is not None]
        if both:
            d_top = np.array([(c['regime_hi'] - c['range']['top']) / c['atr'] for c in both if c['atr']])
            d_bot = np.array([(c['range']['bot'] - c['regime_lo']) / c['atr'] for c in both if c['atr']])
            h_old = np.array([(c['regime_hi'] - c['regime_lo']) / c['atr'] for c in both if c['atr']])
            h_new = np.array([(c['range']['top'] - c['range']['bot']) / c['atr'] for c in both if c['atr']])
            print(f'     box height in ATR   regime median {np.median(h_old):.1f}   '
                  f'range median {np.median(h_new):.1f}')
            print(f'     regime top above range top: median {np.median(d_top):+.1f} ATR;  '
                  f'regime bottom below range bottom: median {np.median(d_bot):+.1f} ATR')
        if pb == 'false_break_fade':
            # Is today's TP2 the regime edge at all, or the fixed tp2_r
            # fallback (the edge was outside 1.2R-6R)?
            def is_edge(c, px):
                return px is not None and abs(c['tp2'] - px) < 1e-6
            on_edge = sum(1 for c in cs if is_edge(c, c['regime_hi'] if c['side'] == 'buy' else c['regime_lo']))
            on_rng = sum(1 for c in with_r if c['tp2_range'] is not None and
                         is_edge({'tp2': c['tp2_range']}, c['range']['top'] if c['side'] == 'buy' else c['range']['bot']))
            print(f'     today\'s TP2 is the regime edge on {on_edge} of {len(cs)} '
                  f'({on_edge / len(cs) * 100:.0f}%); the rest use the fixed TP2 multiple')
            print(f'     the range edge would be accepted as TP2 on {on_rng} of {len(with_r)} in-range candidates')
            moved =[c for c in with_r if c['tp2_range'] is not None and abs(c['tp2_range'] - c['tp2']) > 1e-9]
            print(f'     TP2 would change on {len(moved)} of {len(with_r)} in-range candidates')
        if pb == 'range_fade':
            agree = 0
            for c in with_r:
                lo, hi = c['range']['bot'], c['range']['top']
                pos = (c['price'] - lo) / (hi - lo) if hi > lo else 0.5
                at_edge = pos >= 0.82 if c['side'] == 'sell' else pos <= 0.18
                agree += at_edge
            print(f'     at the same edge of the new range: {agree} of {len(with_r)}; '
                  f'{len(cs) - len(with_r)} fired with NO active range under the new rules')

    # --- 2. false_break_fade arms ------------------------------------------ #
    def arm(cap, series, kind):
        cs = []
        for c in cap['candidates']:
            if c['playbook'] != 'false_break_fade' or c['entry_t'] is None:
                continue
            c = dict(c)
            j = int(np.searchsorted(series.t, c['entry_t'], 'left'))
            if j >= len(series) or int(series.t[j]) != c['entry_t']:
                continue
            c['i'] = j
            if kind == 'B1' and c['tp2_range'] is not None:
                c['tp2'] = c['tp2_range']
            elif kind == 'B2':
                if c['range'] is None:
                    continue
                if c['tp2_range'] is not None:
                    c['tp2'] = c['tp2_range']
            elif kind == 'C' and c['range'] is None:
                continue
            cs.append(c)
        return {**cap, 'candidates': cs}

    arms = {'A': 'today: TP2 = regime box', 'B1': 'TP2 = range edge, else regime box',
            'B2': 'TP2 = range edge, range required', 'C': 'today\'s TP2, range required'}
    per_year = {k: [] for k in arms}
    all_trades = {k: [] for k in arms}
    for cap in caps:
        series, m1 = xx.load_period(cap)
        for k in arms:
            tr = xx.evaluate(arm(cap, series, k), series, m1, xx.gate_current, xx.EXITS['X0'])
            per_year[k].append((cap['year'], tr))
            all_trades[k].extend(tr)

    print(f'\n  2. FALSE_BREAK_FADE  exit X0 (50% at 1R, BE, rest to TP2), today\'s reward gate')
    print(xx.HEAD)
    for k, name in arms.items():
        print(xx._row(f'{k:<3} {name}', xx.stats(all_trades[k])))

    print('\n  net R by year')
    print('  ' + 'year'.ljust(6) + ''.join(f'{k:>16}' for k in arms))
    for idx, cap in enumerate(caps):
        cells = []
        for k in arms:
            tr = per_year[k][idx][1]
            cells.append(f'{sum(t["net_r"] for t in tr):+8.1f} ({len(tr):>3})')
        print(f'  {cap["year"]:<6}' + ''.join(f'{c:>16}' for c in cells))
    losing = {k: sum(1 for _, tr in per_year[k] if tr and sum(t['net_r'] for t in tr) < 0)
              for k in arms}
    print('  losing years: ' + ', '.join(f'{k} {v}' for k, v in losing.items()))


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('capture', 'report', '_one'):
        p = sub.add_parser(name)
        p.add_argument('--symbol', default='XAUUSD.a')
        p.add_argument('--tf', default='30m')
        if name == '_one':
            p.add_argument('--year', type=int, required=True)
        else:
            p.add_argument('--years', default='2018-2026')
        if name == 'capture':
            p.add_argument('--workers', type=int, default=5)
    a = ap.parse_args()
    if a.cmd == '_one':
        cap = capture(a.symbol, a.tf, a.year)
        OUT.mkdir(parents=True, exist_ok=True)
        cap_path(a.symbol, a.tf, a.year).write_bytes(pickle.dumps(cap))
        print(f'{len(cap["candidates"])} candidates over {cap["bars"]} bars in {cap["secs"]:.0f}s')
        return 0
    y0, _, y1 = a.years.partition('-')
    years = list(range(int(y0), int(y1 or y0) + 1))
    if a.cmd == 'capture':
        run_capture(a.symbol, a.tf, years, a.workers)
    else:
        report(a.symbol, a.tf, years)
    return 0


if __name__ == '__main__':
    sys.exit(main())
