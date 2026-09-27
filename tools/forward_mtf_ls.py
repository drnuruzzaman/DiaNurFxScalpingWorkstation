#!/usr/bin/env python
"""
tools/forward_mtf_ls.py - forward test of MTF-LS v4 on gold, every timeframe ladder.

FORWARD TEST ONLY. Read-only: it reads data/ and writes runs/forward/mtf_ls_v4/.
No order is placed, nothing in the live API, the bridge or the executor is
touched or imported.

The rules are tools/research_mtf_liquidity.py's variant v4 (sweep of liquidity,
5m-style change of character, a displacement that left a fair value gap, a
limit order at the 50% retracement, stop beyond the sweep, 2R, a time stop),
on each ladder of LADDERS: 1m, 3m, 5m, 15m, 30m and 1h entries. They were
FROZEN when this test began (--init): the manifest records the start time, a
fingerprint of the code the rules live in, and each ladder's 2018-to-start
backtest as its reference. A later run whose code fingerprint differs says so
and its forward numbers stop counting.

Each run replays the frozen rules on every bar that arrived AFTER the start -
bars that did not exist when the rules were fixed - with the same fills and
costs as the backtest, and reports the forward trades against the reference.
The rules are causal, so a replay on new bars gives the trades the rules would
have logged as they happened. New bars come from Settings > Market data (the
update, or the monthly routine); this reads whatever is on disk.

Fixed in advance, how the forward record is read:
  - The 5m ladder is the candidate (its backtest: +0.14 R per trade). It HOLDS
    if, after at least 40 forward trades, its mean net R is above zero. The
    backtest is CONSISTENT with it if the backtest mean lies inside the
    forward mean's 90% interval.
  - The other ladders are recorded, not judged: their backtests did not
    support them (1m, 3m and 30m lost; 15m and 1h had too few trades).
  - At about 24 trades a year on the 5m ladder, 40 trades is well over a year.

    python tools/forward_mtf_ls.py --init        freeze the rules and start (once)
    python tools/forward_mtf_ls.py               replay the new bars, write the report
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.research_mtf_liquidity as R                                # noqa: E402
from server.datafeed import load_disk                                   # noqa: E402

SYMBOL = 'XAUUSD.a'
VARIANT = 'v4'
CANDIDATE = '5m'
MIN_TRADES = 40
OUT = ROOT / 'runs' / 'forward' / 'mtf_ls_v4'
MANIFEST = OUT / 'manifest.json'
# The code a trade's rules and fills live in: any edit invalidates the forward record.
FINGERPRINTED = ('tools/research_mtf_liquidity.py', 'server/forecast/labels.py',
                 'server/engine/indicators.py', 'server/forecast/timebase.py')


def fingerprint() -> str:
    h = hashlib.sha256()
    for rel in FINGERPRINTED:
        h.update(rel.encode())
        h.update((ROOT / rel).read_bytes())
    return h.hexdigest()[:16]


def configure(ladder: str) -> None:
    """v4's rules on one ladder - exactly as the research tool sets them."""
    R.RULES.clear()
    R.RULES.update(R.RULES_DEFAULT)
    R.RULES.update(R.VARIANTS[VARIANT])
    R.use_ladder(ladder)


def replay(ladder: str, start_ms: int, first_year: int) -> tuple:
    """(complete trades, still-open trades, last 1m bar on disk) from start_ms on one ladder."""
    configure(ladder)
    from server.lab.data import spec as lab_spec
    now_y = datetime.now(timezone.utc).year
    ctx = list(range(first_year, now_y + 1))
    s_bias, s_pool, s_entry = (load_disk(SYMBOL, tf, ctx) for tf in
                               (R.RULES['bias_tf'], R.RULES['pool_tf'], R.RULES['entry_tf']))
    m1 = load_disk(SYMBOL, '1m', list(range(first_year + 1, now_y + 1)))
    fr = R.Frame(s_entry, s_bias, s_pool)
    book = R.Book(SYMBOL, m1, lab_spec(SYMBOL))
    trades = R.framework(fr, s_pool, book, start_ms)
    last = int(m1.t[-1]) if m1.t.size else 0
    # A time exit on the last minute on disk is a trade still running, not a result.
    done = [t for t in trades if not (t['how'] == 'time' and t['exit_ms'] > last)]
    still = [t for t in trades if t not in done]
    return done, still, last


def stats(rs: list) -> dict:
    if not rs:
        return {'n': 0}
    r = np.array(rs, dtype=float)
    out = {'n': int(r.size), 'mean': float(r.mean()), 'total': float(r.sum()),
           'win': float(np.mean(r > 0))}
    if r.size >= 10:
        g = np.random.default_rng(5)
        means = r[g.integers(0, r.size, (4000, r.size))].mean(1)
        out['lo'], out['hi'] = float(np.percentile(means, 5)), float(np.percentile(means, 95))
    return out


def init() -> int:
    if MANIFEST.exists():
        print(f'already started: {MANIFEST.relative_to(ROOT)} - a forward test is frozen once')
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_ms = int(start.timestamp() * 1000)
    refs = {}
    for ladder in R.LADDERS:
        t0 = time.perf_counter()
        configure(ladder)
        # the backtest reference, 2018 to the start - the same code the replay runs
        s_bias, s_pool, s_entry = (R.load(SYMBOL, tf) for tf in
                                   (R.RULES['bias_tf'], R.RULES['pool_tf'], R.RULES['entry_tf']))
        m1 = R.load(SYMBOL, '1m')
        from server.lab.data import spec as lab_spec
        fr = R.Frame(s_entry, s_bias, s_pool)
        trades = R.framework(fr, s_pool, R.Book(SYMBOL, m1, lab_spec(SYMBOL)),
                             int(datetime(R.FIRST_YEAR, 1, 1, tzinfo=timezone.utc).timestamp() * 1000))
        span_years = (start_ms - datetime(R.FIRST_YEAR, 1, 1, tzinfo=timezone.utc).timestamp() * 1000) \
            / (365.25 * 86_400_000)
        refs[ladder] = dict(stats([t['r_net'] for t in trades]),
                            per_year=len(trades) / span_years,
                            timeframes=[R.RULES['entry_tf'], R.RULES['pool_tf'], R.RULES['bias_tf']])
        print(f"{ladder}: reference {refs[ladder]['n']} trades, {refs[ladder]['mean']:+.3f} R "
              f'({time.perf_counter() - t0:.0f}s)', flush=True)
    doc = {'test': 'MTF-LS v4 forward test', 'symbol': SYMBOL, 'variant': VARIANT,
           'start_utc': start.isoformat(), 'start_ms': start_ms, 'fingerprint': fingerprint(),
           'fingerprinted': list(FINGERPRINTED), 'rules_default': R.RULES_DEFAULT,
           'variant_rules': R.VARIANTS[VARIANT], 'ladders': R.LADDERS, 'reference': refs,
           'candidate': CANDIDATE, 'min_trades': MIN_TRADES,
           'reading': 'the candidate HOLDS if, after >= min_trades forward trades, its mean net R '
                      'is above zero; the backtest is CONSISTENT with it if the backtest mean '
                      'lies inside the forward 90% interval. Other ladders are recorded only.'}
    MANIFEST.write_text(json.dumps(doc, indent=1), encoding='utf-8')
    print(f'\nfrozen at {start.isoformat()} - fingerprint {doc["fingerprint"]}')
    print(f'-> {MANIFEST.relative_to(ROOT)}')
    return 0


def run() -> int:
    if not MANIFEST.exists():
        print('not started - run with --init first (once)')
        return 1
    man = json.loads(MANIFEST.read_text(encoding='utf-8'))
    start_ms = int(man['start_ms'])
    fp = fingerprint()
    valid = fp == man['fingerprint']
    first_year = datetime.fromtimestamp(start_ms / 1000, timezone.utc).year - 1
    rows, lines, all_trades = {}, [], []
    last_bar = 0
    for ladder in man['ladders']:
        done, still, last = replay(ladder, start_ms, first_year)
        last_bar = max(last_bar, last)
        rows[ladder] = (stats([t['r_net'] for t in done]), len(still))
        for t in done + still:
            all_trades.append(dict(t, ladder=ladder, status='closed' if t in done else 'open'))
    f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M')
    w = lines.append
    w(f"MTF-LS v4 FORWARD TEST - {man['symbol']}   started {f(start_ms)} UTC   "
      f"data on disk to {f(last_bar)} (broker)")
    w('Forward test only - no orders. Frozen rules replayed on bars that arrived after the start;')
    w('same fills and costs as the backtest. New bars come from Settings > Market data.')
    if not valid:
        w('')
        w(f"!! THE RULE CODE CHANGED SINCE THE START (fingerprint {fp} != {man['fingerprint']}).")
        w('!! These are no longer the frozen rules - the forward numbers below do not count.')
    w('')
    w(f"{'ladder':<16}{'forward':>8}{'open':>6}{'net R/tr':>10}{'total R':>9}{'win%':>7}"
      f"{'90% interval':>18}{'backtest R/tr':>15}{'bt trades/yr':>14}")
    for ladder, (s, n_open) in rows.items():
        ref = man['reference'][ladder]
        tfs = '/'.join(ref['timeframes'])
        iv = f"[{s['lo']:+.2f}, {s['hi']:+.2f}]" if 'lo' in s else '-'
        w(f"{tfs:<16}{s['n']:>8}{n_open:>6}"
          + (f"{s['mean']:>+10.3f}{s['total']:>+9.1f}{100 * s['win']:>6.0f}%" if s['n'] else
             f"{'-':>10}{'-':>9}{'-':>7}")
          + f"{iv:>18}{ref['mean']:>+15.3f}{ref['per_year']:>14.0f}")
    c = rows[man['candidate']][0]
    ref = man['reference'][man['candidate']]
    w('')
    if c['n'] < man['min_trades']:
        need = man['min_trades'] - c['n']
        w(f"CANDIDATE ({man['candidate']} ladder): {c['n']} of {man['min_trades']} forward trades - "
          f"too early to read (about {need / max(ref['per_year'], 1e-9):.1f} more years at its "
          'backtest rate).')
    else:
        holds = c['mean'] > 0
        consistent = c.get('lo', -9) <= ref['mean'] <= c.get('hi', 9)
        w(f"CANDIDATE ({man['candidate']} ladder): {'HOLDS' if holds else 'DOES NOT HOLD'} - "
          f"forward {c['mean']:+.3f} R over {c['n']} trades; the backtest's {ref['mean']:+.3f} is "
          f"{'inside' if consistent else 'outside'} the forward 90% interval.")
    w('Other ladders are recorded, not judged. Measurements, not trading advice.')
    txt = '\n'.join(lines)
    (OUT / 'report.txt').write_text(txt, encoding='utf-8')
    cols = ['ladder', 'status', 'side', 'entry_ms', 'exit_ms', 'fill', 'stop', 'target', 'exit',
            'how', 'risk', 'r_gross', 'r_net', 'sweep_ms']
    with open(OUT / 'trades.csv', 'w', newline='', encoding='utf-8') as fh:
        wr = csv.DictWriter(fh, fieldnames=cols)
        wr.writeheader()
        for t in sorted(all_trades, key=lambda x: x['entry_ms']):
            wr.writerow({k: t.get(k) for k in cols})
    with open(OUT / 'runs.jsonl', 'a', encoding='utf-8') as fh:
        fh.write(json.dumps({'at_utc': datetime.now(timezone.utc).isoformat(), 'data_to_ms': last_bar,
                             'valid': valid, 'fingerprint': fp,
                             'ladders': {k: v[0] | {'open': v[1]} for k, v in rows.items()}}) + '\n')
    print(txt)
    print(f'\n-> {OUT.relative_to(ROOT)}')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--init', action='store_true', help='freeze the rules and start the test (once)')
    a = ap.parse_args()
    return init() if a.init else run()


if __name__ == '__main__':
    sys.exit(main())
