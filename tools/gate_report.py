#!/usr/bin/env python
"""
tools/gate_report.py - which gates stop the most LIVE signals.

    python tools/gate_report.py [--since 2026-09-27] [--symbol XAUUSD.a] [--tf 5m]

Reads order_ledger/logs/signal_verdicts.jsonl - each FINAL signal's first
verdict, written by the live server since 2026-09-27 (SignalStore
.on_first_verdict) - and the order ledger for which signals went on to an
order. The counting is server/lab/stats.gates(), the same the backtest lab's
Performance tab shows. Read-only; measurement, not trading advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import CONFIG                                  # noqa: E402
from server.lab.stats import gates                                # noqa: E402

LOG = ROOT / 'order_ledger' / 'logs' / 'signal_verdicts.jsonl'
LEDGER = ROOT / 'order_ledger' / 'order_ledger.json'
SETTINGS = ROOT / 'configs' / 'settings.json'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--since', help='YYYY-MM-DD (UTC), on the signal bar')
    ap.add_argument('--symbol')
    ap.add_argument('--tf')
    a = ap.parse_args()

    rows = []
    if LOG.exists():
        for line in LOG.read_text(encoding='utf-8').splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    if a.since:
        cut = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp() * 1000
        rows = [r for r in rows if int(r.get('final_bar_ms') or 0) >= cut]
    rows = [r for r in rows if (not a.symbol or r.get('symbol') == a.symbol)
            and (not a.tf or r.get('tf') == a.tf)]
    orders = (json.loads(LEDGER.read_text(encoding='utf-8')).get('orders') or {}) \
        if LEDGER.exists() else {}
    sent = {k for k, v in orders.items() if v.get('state') != 'refused'}
    floor = CONFIG.gates.min_confidence
    try:
        floor = int(json.loads(SETTINGS.read_text(encoding='utf-8'))['gates']['min_confidence'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    g = gates(rows, sent, floor)

    first = min((int(r['final_bar_ms']) for r in rows if r.get('final_bar_ms')), default=None)
    since = datetime.fromtimestamp(first / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M') \
        if first else '-'
    st = g['status']
    print(f"LIVE GATES - {g['n']} signals since {since} UTC"
          + (f" ({a.symbol or 'all symbols'} {a.tf or 'all timeframes'})") +
          f": qualified {st.get('qualified', 0)}, watch {st.get('watch', 0)}, "
          f"rejected {st.get('rejected', 0)} on their first verdict; {g['sent']} went on to an order")
    if not g['n']:
        print('No verdicts logged yet - the live server writes one per new FINAL signal.')
        return 0
    print(f"\n  {'gate':<11}{'blocked':>8}{'only':>6}{'sent':>6}{'warned':>8}{'-conf':>7}"
          f"{'decided':>9}   usually means")
    for name, x in g['gates'].items():
        pen = f"{x['pen_avg']:.0f}" if x['pen_avg'] is not None else '-'
        print(f"  {name:<11}{x['block']:>8}{x['sole']:>6}{x['sent_after_block']:>6}{x['warn']:>8}"
              f"{pen:>7}{x['decided']:>9}   {g['meaning'].get(name, '')}")
    print('\n  blocked = hard blocks; only = the sole block; sent = blocked at first, sent later; '
          'warned / -conf = warnings and their average confidence cost;\n  decided = watch '
          f'signals that penalty alone put under the {floor} floor.')
    print(f"\n  {'playbook':<20}{'n':>5}{'qual':>6}{'watch':>7}{'rej':>6}{'sent':>6}   blocks")
    for pb, x in sorted(g['playbooks'].items(), key=lambda kv: -kv[1]['n']):
        bl = ', '.join(f'{k} {v}' for k, v in sorted(x['blocks'].items(), key=lambda kv: -kv[1]))
        print(f"  {pb:<20}{x['n']:>5}{x.get('qualified', 0):>6}{x.get('watch', 0):>7}"
              f"{x.get('rejected', 0):>6}{x['sent']:>6}   {bl}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
