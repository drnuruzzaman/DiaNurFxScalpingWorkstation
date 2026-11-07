#!/usr/bin/env python
"""
tools/live_diagnostic.py - why the live trades made what they made.

The first task the trading-robot brief asks for: a trade-level diagnostic that
separates STRATEGY failure from EXECUTION failure and poor trade MANAGEMENT,
before anything more is built on top. Read-only: it reads the order ledger, the
live trade journal and the bridge's daily logs, replays the same weeks in the
lab, and writes runs/research/live_diagnostic/report.txt.

For every closed live trade (order_ledger/logs/trade_journal*.jsonl):
  execution   the decision delay (bar close -> order sent), the fill delay
              (sent -> filled, from the ledger events), and for market orders
              the slippage: the fill against the price the bridge logged with
              the request (logs/bridge-<date>.log, SEND ... @price), in points
              and in R.
  strategy    the lab twin: the same signal (same symbol, timeframe,
              playbook, side and bar) in a fidelity-2 lab replay of the live
              weeks, with today's live settings and every playbook switched
              on - what the rules alone produced with modelled fills and the
              coded exit.
  management  live minus lab for the same signal, split into the entry (fill
              price) and everything after it (manual closes, the exit as it
              ran live - the lock was 0.75 live and a different value in some
              weeks - and any intervention).
Live R here is net of commission (r_multiple minus 2 x commission over the
money risked; the live journal predates r_net). Signals the lab does not
reproduce (a symbol with no history on disk, settings that differed at the
time, a different bar feed) are counted as unmatched, and the match rate is
reported as a parity check.

    python tools/live_diagnostic.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JOURNALS = (ROOT / 'order_ledger' / 'logs' / 'trade_journal_backfill.jsonl',
            ROOT / 'order_ledger' / 'logs' / 'trade_journal.jsonl')
LEDGER = ROOT / 'order_ledger' / 'order_ledger.json'
BRIDGE_LOGS = ROOT / 'logs'
OUT = ROOT / 'runs' / 'research' / 'live_diagnostic'
SETTINGS = ROOT / 'runs' / 'research' / 'batches' / 'gold-f2' / 'settings.json'
TF_MS = {'1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
         '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000}
SEND = re.compile(r'^(\S+)\s+order\s+SEND (\w+) (buy|sell) (\S+) ([\d.]+) lots @([\d.]+) .*'
                  r'signal=(\S+)')
RESULT = re.compile(r'^(\S+)\s+order\s+RESULT ok=(\w+) retcode=(\d+)(?: ticket=(\d+))?'
                    r'(?: price=([\d.]+))?')


def load_trades() -> list:
    out = {}
    for f in JOURNALS:
        if not f.exists():
            continue
        for line in f.read_text(encoding='utf-8').splitlines():
            if line.strip():
                j = json.loads(line)
                out[j['signal_id']] = j
    return list(out.values())


def bridge_orders() -> dict:
    """signal id -> the last accepted SEND: requested price, kind, times, fill price."""
    out = {}
    for f in sorted(BRIDGE_LOGS.glob('bridge-*.log')):
        pending = None
        for line in f.read_text(encoding='utf-8', errors='replace').splitlines():
            m = SEND.match(line)
            if m:
                pending = {'t_send': datetime.fromisoformat(m.group(1)).timestamp() * 1000,
                           'kind': m.group(2), 'requested': float(m.group(6)),
                           'signal': m.group(7)}
                continue
            m = RESULT.match(line)
            if m and pending:
                if m.group(2) == 'True':
                    pending.update(t_result=datetime.fromisoformat(m.group(1)).timestamp() * 1000,
                                   result_price=float(m.group(5)) if m.group(5) else None)
                    out[pending['signal']] = pending
                pending = None
    return out


def ledger_rows() -> dict:
    L = json.loads(LEDGER.read_text(encoding='utf-8'))
    rows = L if isinstance(L, list) else L.get('orders', L.get('rows', L))
    rows = list(rows.values()) if isinstance(rows, dict) else rows
    return {r['id']: r for r in rows}


def spec_for(symbol: str, price: float) -> dict:
    """
    The symbol's saved contract facts; for a symbol with none on disk, a USD
    account's for an FX pair quoted to 5 digits (or 3 for a price above 20) -
    never gold's defaults, which priced a GBPUSD trade's commission at 60R.
    """
    from server.lab import data as lab_data
    s = {}
    try:
        s = lab_data.spec(symbol)
    except Exception:                                          # noqa: BLE001
        pass
    if s.get('source', 'defaults') == 'defaults' and not symbol.startswith('XAU'):
        five = price < 20
        s = {'point': 1e-5 if five else 1e-3, 'tick_size': 1e-5 if five else 1e-3,
             'tick_value': 1.0 if five else 1e-3 * 1e5 / price,
             'commission_per_lot_side': 3.0, 'source': 'inferred (no spec on disk)'}
    return s


def net_r(r_price: float, risk_price: float, spec: dict) -> float:
    vpu = float(spec.get('tick_value') or 1.0) / float(spec.get('tick_size') or 0.01)
    comm = 2 * float(spec.get('commission_per_lot_side') or 3.0)
    return r_price - comm / (risk_price * vpu) if risk_price > 0 else r_price


def lab_twins(trades: list) -> dict:
    """{broker-clock signal id: lab journal line} from fidelity-2 replays of the live weeks."""
    from server.forecast.timebase import utc_to_broker
    from server.lab import data as lab_data
    from server.lab.session import ReplaySession
    settings = json.loads(SETTINGS.read_text(encoding='utf-8'))['settings']
    need = defaultdict(list)
    for t in trades:
        if t.get('fill_ms'):
            need[(t['symbol'], t['tf'])].append(t)
    out = {}
    have = set(lab_data.symbols())
    for (sym, tf), ts in sorted(need.items()):
        if sym not in have or tf not in TF_MS or tf == '1m':
            continue
        cover = lab_data.coverage(sym)
        a = int(utc_to_broker(min(int(t['final_bar_ms']) for t in ts))) - 2 * 86_400_000
        b = min(int(utc_to_broker(max(int(t.get('exit_ms') or t['fill_ms']) for t in ts)))
                + 2 * 86_400_000, int(cover['1m']['last_ms']))
        iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M')  # noqa: E731
        t0 = time.time()
        s = ReplaySession({'symbol': sym, 'tf': tf, 'start': iso(a), 'end': iso(b),
                           'mode': 'auto', 'record': False, 'overrides': settings,
                           'name': f'live diagnostic {sym} {tf}'})
        while s.advance(2000):
            del s.frames[:-50]
        for j in s.journal:
            out[j['signal_id']] = j
        for v in s.verdicts:                       # every FINAL signal, traded or not
            out.setdefault('__signals__', {})[v['id']] = (v.get('first_qual') or {}).get('status')
        print(f'  lab twin {sym} {tf}: {len(s.journal)} lab trades ({time.time() - t0:.0f}s)',
              flush=True)
    return out


def main() -> int:
    from server.forecast.timebase import utc_to_broker
    trades = load_trades()
    orders = bridge_orders()
    ledger = ledger_rows()
    print(f'{len(trades)} live trades, {len(orders)} accepted sends in the bridge logs', flush=True)
    cache = OUT / 'lab_twins_v2.json'
    sha = json.loads(SETTINGS.read_text(encoding='utf-8')).get('sha')
    hit = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
    if hit.get('settings_sha') == sha and hit.get('n_trades') == len(trades):
        twins = hit['twins']
    else:
        twins = lab_twins(trades)
        OUT.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({'settings_sha': sha, 'n_trades': len(trades),
                                     'twins': twins}, default=str), encoding='utf-8')
    # A looser key for the twin: the same bar and side, whichever playbook's
    # name the merged signal carries. With every playbook on, the lab merges
    # same-side hits under the strongest one, so the exact id differs from a
    # live run that had fewer playbooks switched on.
    lab_signals = twins.pop('__signals__', {})
    loose_sig = {}
    for sid, st in lab_signals.items():
        sym, tf, _pb, side, bar = sid.split(':')
        loose_sig.setdefault(f'{sym}:{tf}:{side}:{bar}', st)
    loose = {}
    for sid, j in twins.items():
        sym, tf, _pb, side, bar = sid.split(':')
        loose.setdefault(f'{sym}:{tf}:{side}:{bar}', j)
    from server.lab import data as lab_data
    data_to = {}
    for sym in {t['symbol'] for t in trades}:
        try:
            data_to[sym] = int(lab_data.coverage(sym)['1m']['last_ms'])
        except Exception:                                      # noqa: BLE001
            pass
    rows = []
    for t in trades:
        if not t.get('fill_ms') or t.get('stop') is None or t.get('fill_price') is None:
            continue
        spec = spec_for(t['symbol'], float(t['fill_price']))
        sign = 1.0 if t['side'] == 'buy' else -1.0
        point = float(spec.get('point') or 0.01)
        risk = abs(float(t['fill_price']) - float(t['stop']))
        live_r = net_r(float(t['r_multiple']), risk, spec)
        row = {'id': t['signal_id'], 'symbol': t['symbol'], 'tf': t['tf'],
               'playbook': t['playbook'], 'side': t['side'], 'kind': t.get('kind'),
               'outcome': t.get('outcome'), 'live_r': live_r, 'risk_pts': risk / point}
        led = ledger.get(t['signal_id']) or {}
        ev = {e[1]: e[0] for e in led.get('events') or []}
        close_ms = int(t['final_bar_ms']) + TF_MS.get(t['tf'], 0)
        if ev.get('sending'):
            row['decision_s'] = (ev['sending'] - close_ms) / 1000
        if ev.get('sending') and ev.get('filled') and t.get('kind') == 'market':
            row['fill_s'] = (ev['filled'] - ev['sending']) / 1000
        o = orders.get(t['signal_id'])
        if o and o['kind'] == 'market' and t.get('kind') == 'market':
            slip_pts = sign * (float(t['fill_price']) - o['requested']) / point
            row['slip_pts'] = slip_pts                     # + = paid more than asked
            row['slip_r'] = slip_pts * point / risk
        if t.get('entry_planned') is not None:
            row['vs_plan_r'] = sign * (float(t['fill_price']) - float(t['entry_planned'])) / risk
        bid = f"{t['symbol']}:{t['tf']}:{t['playbook']}:{t['side']}:" \
              f"{int(utc_to_broker(int(t['final_bar_ms'])))}"
        lkey = f"{t['symbol']}:{t['tf']}:{t['side']}:{int(utc_to_broker(int(t['final_bar_ms'])))}"
        row['in_data'] = (t['symbol'] in data_to and t['tf'] != '1m' and
                          int(utc_to_broker(int(t['final_bar_ms']))) + TF_MS.get(t['tf'], 0) * 3
                          < data_to[t['symbol']])
        row['lab_signal'] = ('exact' if bid in lab_signals else
                             'same bar and side' if lkey in loose_sig else None)
        lab = twins.get(bid)
        row['match'] = 'exact' if lab else None
        if lab is None:
            lab = loose.get(f"{t['symbol']}:{t['tf']}:{t['side']}:"
                            f"{int(utc_to_broker(int(t['final_bar_ms'])))}")
            row['match'] = 'same bar and side' if lab else None
        if lab and lab.get('r_net') is not None:
            lrisk = abs(float(lab['fill_price']) - float(lab['stop']))
            row['lab_r'] = float(lab['r_net'])
            row['lab_outcome'] = lab.get('outcome')
            # Entry: what the live fill gained (+) or lost against the lab's,
            # measured on the live trade's own risk.
            row['entry_gap_r'] = sign * (float(lab['fill_price']) - float(t['fill_price'])) / risk
            row['after_entry_gap_r'] = live_r - row['lab_r'] - row['entry_gap_r'] \
                if lrisk > 0 else None
        rows.append(row)

    OUT.mkdir(parents=True, exist_ok=True)
    lines = []
    w = lines.append
    m = lambda xs: float(np.mean(xs)) if len(xs) else float('nan')        # noqa: E731
    med = lambda xs: float(np.median(xs)) if len(xs) else float('nan')    # noqa: E731
    tot = [r['live_r'] for r in rows]
    w(f'LIVE TRADE DIAGNOSTIC - {len(rows)} closed live trades '
      f'({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)')
    w('Net R after commission. Strategy = the lab twin of the same signal; execution = fills '
      'and delays; management = what happened after the entry.')
    w('')
    w('1. RESULT')
    w(f'   all trades: mean {m(tot):+.3f} R, total {sum(tot):+.1f} R, win rate '
      f'{np.mean([x > 0 for x in tot]):.0%}')
    for key in ('symbol', 'tf', 'playbook', 'outcome'):
        g = defaultdict(list)
        for r in rows:
            g[r[key]].append(r['live_r'])
        w(f'   by {key}: ' + ', '.join(f'{k} {m(v):+.2f} (n={len(v)})'
                                      for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))))
    w('')
    w('2. EXECUTION')
    mk = [r for r in rows if 'slip_r' in r]
    w(f'   market orders with a logged request: {len(mk)}; slippage median '
      f'{med([r["slip_pts"] for r in mk]):+.1f} pts, mean {m([r["slip_pts"] for r in mk]):+.1f} pts, '
      f'worst {max([r["slip_pts"] for r in mk] or [float("nan")]):+.1f} pts; in R mean '
      f'{m([r["slip_r"] for r in mk]):+.3f} (+ = paid more than asked)')
    dl = [r['decision_s'] for r in rows if 'decision_s' in r]
    fl = [r['fill_s'] for r in rows if 'fill_s' in r]
    w(f'   decision delay (bar close -> order sent): median {med(dl):.0f}s, p90 '
      f'{(np.percentile(dl, 90) if dl else float("nan")):.0f}s (n={len(dl)})')
    w(f'   send -> fill, market orders: median {med(fl):.1f}s, p90 '
      f'{(np.percentile(fl, 90) if fl else float("nan")):.1f}s (n={len(fl)}); pending orders '
      f'wait for their price and are not counted')
    vp = [r['vs_plan_r'] for r in rows if 'vs_plan_r' in r]
    w(f'   fill vs the signal\'s planned entry: mean {m(vp):+.3f} R (+ = worse; includes '
      f'price moving during the decision delay, not only slippage)')
    w('')
    w('3. STRATEGY vs MANAGEMENT (trades the lab reproduced)')
    mt = [r for r in rows if 'lab_r' in r]
    eligible = [r for r in rows if r.get('in_data')]
    w(f'   history on disk ends: ' + ', '.join(
        f"{s} {datetime.fromtimestamp(ms / 1000, timezone.utc):%Y-%m-%d}" for s, ms in sorted(data_to.items()))
      + f' (broker clock) - {sum(1 for r in rows if not r.get("in_data"))} live trades fall after '
        f'it or on symbols with no history, and cannot have a lab twin until the data is updated')
    ex = sum(1 for r in mt if r.get('match') == 'exact')
    w(f'   matched {len(mt)} of {len(eligible)} trades on symbols and timeframes with lab history '
      f'({len(mt) / max(1, len(eligible)):.0%}; {ex} exact id, {len(mt) - ex} same bar and '
      f'side). Unmatched ones were not produced by today\'s settings on disk bars at that bar - '
      f'a parity gap in its own right (settings changed during the live weeks; live reads '
      f'bridge bars, the lab disk bars)')
    gen = [r for r in eligible if r.get('lab_signal')]
    w(f'   signal parity: the lab generated the same signal for {len(gen)} of {len(eligible)} '
      f'({len(gen) / max(1, len(eligible)):.0%}: '
      f'{sum(1 for r in gen if r["lab_signal"] == "exact")} exact, '
      f'{sum(1 for r in gen if r["lab_signal"] != "exact")} same bar and side); it traded '
      f'{len(mt)} of them. Signals it never generated point at engine, data or settings '
      f'differences; generated-but-untraded ones at caps, requalification or fills.')
    if mt:
        w(f'   same signals: live {m([r["live_r"] for r in mt]):+.3f} R | lab (rules + modelled '
          f'execution + coded exit) {m([r["lab_r"] for r in mt]):+.3f} R')
        w(f'   difference split: entry {m([r["entry_gap_r"] for r in mt]):+.3f} R, after entry '
          f'(management / exit) {m([r["after_entry_gap_r"] for r in mt if r.get("after_entry_gap_r") is not None]):+.3f} R')
        man = [r for r in mt if r['outcome'] == 'manual']
        if man:
            w(f'   manual closes ({len(man)}): live {m([r["live_r"] for r in man]):+.3f} R vs the '
              f'lab exit for the same signals {m([r["lab_r"] for r in man]):+.3f} R')
        nm = [r for r in mt if r['outcome'] != 'manual']
        w(f'   robot-managed ({len(nm)}): live {m([r["live_r"] for r in nm]):+.3f} R vs lab '
          f'{m([r["lab_r"] for r in nm]):+.3f} R')
    w('')
    w('4. WHERE THE RESULT CAME FROM')
    if mt:
        strat = m([r['lab_r'] for r in mt])
        entry = m([r['entry_gap_r'] for r in mt])
        after = m([r['after_entry_gap_r'] for r in mt if r.get('after_entry_gap_r') is not None])
        w(f'   strategy (lab twin): {strat:+.3f} R per trade')
        w(f'   execution (live entry vs lab entry): {entry:+.3f} R per trade')
        w(f'   management after entry: {after:+.3f} R per trade')
        w('   (live = strategy + execution + management, per matched trade)')
    w(f'   n is small ({len(rows)} trades, SD ~1.2 R): a difference under ~0.2 R is not '
      f'distinguishable from noise.')
    w('Measurements, not trading advice.')
    (OUT / 'report.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (OUT / 'trades.json').write_text(json.dumps(rows, indent=1, default=str), encoding='utf-8')
    print('\n'.join(lines))
    print(f'\n-> {(OUT / "report.txt").relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
