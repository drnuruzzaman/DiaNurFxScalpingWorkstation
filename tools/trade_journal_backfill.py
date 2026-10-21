#!/usr/bin/env python
"""
tools/trade_journal_backfill.py - trade journal lines for trades that closed before it existed.

Read-only for everything live. Two sources:

  live   Every CLOSED record in the signal store - the archived copy taken
         2026-09-27 (order_ledger/archive/signal_store_2026-09-27.json, before
         the store's 7-day pruning reached the first trades) merged with the
         current store - joined to the order ledger, written exactly as
         server/trade_journal.record() writes a live close. What those records
         never held is rebuilt and marked so:
           entry_context / exit_context   re-analysed from the disk bars on
               the same closed-bar rule live uses (a higher-timeframe bar only
               once it has closed), 'reconstructed': true
           exit_ms   the store's CLOSED time (when the executor saw it, a few
               seconds after the deal), 'exit_ms_source': 'detected'
         -> order_ledger/logs/trade_journal_backfill.jsonl  (rewritten each run)

  lab    A headless backtest-lab run in auto mode - the live executor, the
         minute-by-minute simulated broker, closed bars only - over one year,
         with today's live settings. Its journal is the backtest side of the
         journal, written the way the lab writes it.
         -> runs/research/journal/lab_<symbol>_<tf>_<year>.json

    python tools/trade_journal_backfill.py live
    python tools/trade_journal_backfill.py lab --year 2025 [--symbol XAUUSD.a --tf 5m]

The research replays (tools/exp_exits.py, playbook_quality) are deliberately
NOT a source: they check exits only from the bar after the fill and read the
higher timeframes with their forming bar - fine for comparing arms with each
other, not for measuring what a real trade went through.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ARCHIVE = ROOT / 'order_ledger' / 'archive' / 'signal_store_2026-09-27.json'
STORE = ROOT / 'order_ledger' / 'signal_store.json'
LEDGER = ROOT / 'order_ledger' / 'order_ledger.json'
LIVE_OUT = ROOT / 'order_ledger' / 'logs' / 'trade_journal_backfill.jsonl'
LAB_OUT = ROOT / 'runs' / 'research' / 'journal'
NY = ZoneInfo('America/New_York')
WINDOW = 600
# Months per lab session, to stay under its 60,000-bar cap (5m kept at halves, as first run).
CHUNK_MONTHS = {'1m': 1, '3m': 3, '5m': 6}


def broker_offset(utc_ms: int) -> int:
    """Broker time minus UTC at that moment: New York + 7 h (3 h in the NY summer)."""
    t = datetime.fromtimestamp(utc_ms / 1000, timezone.utc).astimezone(NY)
    return int((t.utcoffset() + timedelta(hours=7)).total_seconds() * 1000)


# --------------------------------------------------------------------------- #
# live                                                                        #
# --------------------------------------------------------------------------- #
class Reader:
    """Disk bars per (symbol, tf), analysed on the closed-bar rule live uses."""

    def __init__(self):
        from server.lab.data import spec as lab_spec
        self.spec = lab_spec
        self.cache: dict = {}

    def series(self, symbol, tf):
        from server.datafeed import load_disk
        key = (symbol, tf)
        if key not in self.cache:
            yrs = [2025, 2026]
            self.cache[key] = load_disk(symbol, tf, yrs)
        return self.cache[key]

    def context(self, symbol: str, tf: str, close_utc_ms: int) -> dict | None:
        """The market read at the close of the last bar closed by `close_utc_ms`."""
        from server.config import MTF_LADDER, TF_SECONDS
        from server.engine.analysis import analyse, quick_trend
        from server.trade_journal import market_context
        s = self.series(symbol, tf)
        if s.empty():
            return None
        step = TF_SECONDS[tf] * 1000
        close_disk = close_utc_ms + broker_offset(close_utc_ms)
        i = int(np.searchsorted(s.t + step, close_disk, 'right'))      # bars closed by then
        if i < 100:
            return None
        w = s.slice(max(0, i - WINDOW), i)
        mtf = {}
        for name in dict.fromkeys(x for x in MTF_LADDER.get(tf, []) if x != tf):
            h = self.series(symbol, name)
            if h.empty():
                continue
            j = int(np.searchsorted(h.t + TF_SECONDS[name] * 1000, close_disk, 'right'))
            hw = h.slice(max(0, j - 300), j)
            if len(hw) >= 60:
                mtf[name] = quick_trend(hw)
        snap = analyse(w, mtf, self.spec(symbol))
        ctx = market_context(snap)
        if ctx:
            from server.config import session_quality
            bar_utc = int(w.t[-1]) - broker_offset(close_utc_ms)            # back on UTC
            # analyse() reads the session off the bar's hour, and disk bars are
            # broker time - so the session is re-read on UTC, as live reads it
            ctx.update(reconstructed=True, bar_ms=bar_utc,
                       session=session_quality(datetime.fromtimestamp(
                           bar_utc / 1000, timezone.utc).hour)[0])
        return ctx


def live() -> int:
    from server.trade_journal import record
    from server.config import TF_SECONDS
    recs = {}
    for f in (ARCHIVE, STORE):
        if f.exists():
            recs.update(json.loads(f.read_text(encoding='utf-8')).get('records') or {})
    ledger = json.loads(LEDGER.read_text(encoding='utf-8')).get('orders') or {}
    closed = sorted((r for r in recs.values() if r.get('stage') == 'CLOSED'),
                    key=lambda r: r.get('fill_ms') or 0)
    rd = Reader()
    lines, t0 = [], time.time()
    for r in closed:
        tf_ms = TF_SECONDS.get(r['tf'], 300) * 1000
        exit_src = 'deal' if r.get('exit_ms') else 'detected'
        if not r.get('exit_ms'):
            r['exit_ms'] = next((h[0] for h in reversed(r.get('history') or [])
                                 if h[1] == 'CLOSED'), None)
        if not r.get('entry_context'):
            r['entry_context'] = rd.context(r['symbol'], r['tf'], int(r['final_bar_ms']) + tf_ms)
        exit_ctx = rd.context(r['symbol'], r['tf'], int(r['exit_ms'])) if r.get('exit_ms') else None
        line = record(r, ledger.get(r['id']), source='live')
        line.update(backfill=True, exit_ms_source=exit_src, exit_context=exit_ctx)
        lines.append(line)
    LIVE_OUT.parent.mkdir(parents=True, exist_ok=True)
    LIVE_OUT.write_text(''.join(json.dumps(x, default=str) + '\n' for x in lines),
                        encoding='utf-8')
    miss = sum(1 for x in lines if not x['regime_at_entry'])
    print(f'{len(lines)} closed live trades -> {LIVE_OUT.relative_to(ROOT)} '
          f'({miss} without disk data for the regime; {time.time() - t0:.0f}s)')
    return 0


# --------------------------------------------------------------------------- #
# lab                                                                         #
# --------------------------------------------------------------------------- #
def lab(year: int, symbol: str, tf: str) -> int:
    from tools.forecast_build import _below_normal
    _below_normal()
    from server.lab import settings as lab_settings
    from server.lab.session import ReplaySession
    end = min(datetime(year + 1, 1, 1), datetime.now() - timedelta(days=1))
    # A replay holds at most 60,000 bars (a year of 5m is ~71,000, of 1m
    # ~350,000): the year in parts of CHUNK_MONTHS, each its own session. A
    # trade open across a seam is cut by the earlier part.
    step = CHUNK_MONTHS.get(tf, 12)
    halves, m = [], 1
    while m <= 12:
        a0 = datetime(year, m, 1)
        a1 = datetime(year + (m + step > 12), (m + step - 1) % 12 + 1, 1)
        if a0 >= end:
            break
        halves.append((a0, min(a1, end)))
        m += step
    t0, ran, journal, eff = time.time(), 0, [], None
    for a0, a1 in halves:
        s = ReplaySession({'symbol': symbol, 'tf': tf, 'start': a0.strftime('%Y-%m-%dT00:00'),
                           'end': a1.strftime('%Y-%m-%dT00:00'), 'mode': 'auto',
                           'name': f'journal backfill {year}'})
        while True:
            n = s.advance(2000)
            ran += n
            del s.frames[:-50]              # the UI's per-bar frames: not needed headless
            print(f'  {year}: {ran} bars, {len(journal) + len(s.journal)} trades  '
                  f'{time.time() - t0:6.0f}s', flush=True)
            if n == 0:
                break
        journal += s.journal
        eff = s.eff
    LAB_OUT.mkdir(parents=True, exist_ok=True)
    out = LAB_OUT / f'lab_{symbol}_{tf}_{year}.json'
    doc = {'symbol': symbol, 'tf': tf, 'year': year, 'bars': ran,
           'settings': lab_settings.jsonable(eff),
           'made': datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'journal': journal}
    out.write_text(json.dumps(doc, default=str), encoding='utf-8')
    print(f'{year}: {len(journal)} trades -> {out.relative_to(ROOT)} ({time.time() - t0:.0f}s)')
    return 0


def window(start: str, end: str, symbol: str, tf: str, tag: str, ff: str | None,
           all_playbooks: bool = False, settings_file: str = None, fidelity: int = None,
           spread_floor: float = None, spec_file: str = None) -> int:
    """
    A headless lab run over [start, end) - live settings, auto mode, the lab's
    forecast filter `ff` (None = off), no news blackout - cut into sessions of
    CHUNK_MONTHS where the timeframe needs it. `all_playbooks` switches on the
    playbooks live has disabled (a lab override - live is untouched).

    `settings_file` freezes the settings: a JSON of every group (as
    tools/market_batch.py writes once per batch) passed as the full override,
    so an edit to configs/settings.json while a long batch runs cannot reach
    the later sessions. `spec_file` pins the contract facts the same way (the
    saved spec follows whichever account the bridge is attached to). `fidelity` and `spread_floor` go to the session as is
    (server/lab/session.py); None means the session's own default.

    Besides the trades, every signal's FIRST verdict is kept (its gates on the
    bar it was made - SignalStore.first_verdict), with whether it went on to an
    order and whether the forecast filter vetoed a send of it: the input to
    the gate distribution per playbook.
    -> runs/research/journal/win_<tag>_<tf>.json
    """
    from tools.forecast_build import _below_normal
    _below_normal()
    from server.forecast import store as fstore
    from server.lab import settings as lab_settings
    from server.lab.session import ReplaySession
    a0, a1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
    step = CHUNK_MONTHS.get(tf, 12)
    parts, cur = [], a0
    while cur < a1:
        m = cur.month - 1 + step
        nxt = min(datetime(cur.year + m // 12, m % 12 + 1, min(cur.day, 28)), a1)
        parts.append((cur, nxt))
        cur = nxt
    # The filter reads the forecast store; a timeframe without one is unfiltered.
    has_store = fstore.load(symbol, tf, allow_stale=True) is not None
    overrides = {}
    if settings_file:
        overrides = json.loads(Path(settings_file).read_text(encoding='utf-8'))['settings']
    if all_playbooks:
        overrides.setdefault('gates', {})['disabled_playbooks'] = []
    spec_ov = json.loads(Path(spec_file).read_text(encoding='utf-8'))['pinned'] if spec_file else {}
    t0, ran, journal, eff, vetoes, verdicts = time.time(), 0, [], None, 0, []
    for p0, p1 in parts:
        s = ReplaySession({'symbol': symbol, 'tf': tf, 'start': p0.strftime('%Y-%m-%dT%H:%M'),
                           'end': p1.strftime('%Y-%m-%dT%H:%M'), 'mode': 'auto',
                           'forecast_filter': ff, 'news_gate': False, 'overrides': overrides,
                           'fidelity': fidelity, 'spread_floor_points': spread_floor,
                           'spec_overrides': spec_ov,
                           'record': False, 'name': f'timeframe window {tag} {tf}'})
        while True:
            n = s.advance(2000)
            ran += n
            del s.frames[:-50]              # the UI's per-bar frames: not needed headless
            print(f'  {tf}: {ran} bars, {len(journal) + len(s.journal)} trades  '
                  f'{time.time() - t0:6.0f}s', flush=True)
            if n == 0:
                break
        journal += s.journal
        vetoes += sum(1 for e in s.events if 'forecast filter' in str(e.get('text', '')))
        filtered = {(e.get('data') or {}).get('id') for e in s.events
                    if e.get('kind') == 'decision' and 'forecast filter' in str(e.get('text', ''))}
        sent = {fid for fid, r in list(s.ledger.rows.items()) if r.get('state') != 'refused'}
        for v in s.verdicts:
            q = v.get('first_qual') or {}
            verdicts.append({'id': v['id'], 'playbook': v.get('playbook'), 'side': v.get('side'),
                             'status': q.get('status'), 'confidence': q.get('confidence'),
                             'gates': q.get('gates') or [], 'sent': v['id'] in sent,
                             'filtered': v['id'] in filtered})
        eff = s.eff
    LAB_OUT.mkdir(parents=True, exist_ok=True)
    out = LAB_OUT / f'win_{tag}_{tf}.json'
    out.write_text(json.dumps({
        'symbol': symbol, 'tf': tf, 'start': start, 'end': end, 'bars': ran,
        'forecast_filter': ff, 'filter_active': bool(ff and has_store),
        'filter_notes': vetoes, 'news_gate': False, 'all_playbooks': all_playbooks,
        'fidelity': s.cfg.get('fidelity'), 'spread_floor_points': s.spec.get('spread_floor_points'),
        'settings_file': settings_file, 'spec_file': spec_file,
        'verdicts': verdicts,
        'settings': lab_settings.jsonable(eff),
        # the contract facts the P&L was priced with (tick_value is in the account's currency)
        'spec': {k: s.spec.get(k) for k in ('tick_value', 'tick_size', 'contract_size',
                                            'commission_per_lot_side', 'source',
                                            'swap_mode', 'swap_long_points',
                                            'swap_short_points')},
        'swap_charged': s.broker.swap is not None,
        'made': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'journal': journal}, default=str), encoding='utf-8')
    print(f'{tf}: {len(journal)} trades -> {out.relative_to(ROOT)} ({time.time() - t0:.0f}s)')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('live')
    b = sub.add_parser('lab')
    b.add_argument('--year', type=int, required=True)
    b.add_argument('--symbol', default='XAUUSD.a')
    b.add_argument('--tf', default='5m')
    w = sub.add_parser('window')
    w.add_argument('--start', required=True, help='YYYY-MM-DD')
    w.add_argument('--end', required=True, help='YYYY-MM-DD')
    w.add_argument('--symbol', default='XAUUSD.a')
    w.add_argument('--tf', required=True)
    w.add_argument('--tag', required=True)
    w.add_argument('--filter', default=None, help='the lab forecast filter, e.g. no_transition')
    w.add_argument('--all-playbooks', action='store_true',
                   help='switch on the playbooks live has disabled (lab override only)')
    w.add_argument('--settings', default=None,
                   help='frozen settings JSON (tools/market_batch.py writes it)')
    w.add_argument('--fidelity', type=int, default=None, help='lab fidelity (default: current)')
    w.add_argument('--spec', default=None,
                   help='pinned contract facts JSON (tools/market_batch.py writes it)')
    w.add_argument('--spread-floor', type=float, default=None,
                   help='minimum spread in points (default: the symbol floor)')
    a = ap.parse_args()
    if a.cmd == 'window':
        return window(a.start, a.end, a.symbol, a.tf, a.tag, a.filter, a.all_playbooks,
                      a.settings, a.fidelity, a.spread_floor, a.spec)
    return live() if a.cmd == 'live' else lab(a.year, a.symbol, a.tf)


if __name__ == '__main__':
    sys.exit(main())
