"""
server/trade_journal.py - one record per closed trade, live and in the lab. Measurement only.

When a position closes, the facts about it are written once, as one JSON line:
what the signal said (playbook, evidence, the case against, confidence when it
was made and when it was sent), the market read on the bar that made it
(regime, higher timeframes, the leg), the market read when it closed, and how
it went (fill, exit, outcome, R, profit, times).

Only FACTS are recorded here - the things that are gone once the signal store
forgets a finished signal (after 7 days). What the facts MEAN - the adverse
and favourable excursion, "right idea but stopped", "never went the right
way", "the stop was hit and price went on to the target" - is worked out
later from the 1m price history by tools/trade_journal_report.py, so a
definition can be refined without losing a single trade.

    live   order_ledger/logs/trade_journal.jsonl
    lab    runs/lab/<session>/journal.json

Nothing here feeds back into a decision: the journal is written after the
trade is over, and every call is guarded - an error loses a journal line,
never an order.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_LOG = ROOT / 'order_ledger' / 'logs' / 'trade_journal.jsonl'
VERSION = 1
_LOCK = threading.Lock()


def market_context(snap: dict | None) -> dict | None:
    """The small part of a closed-bar analysis worth keeping with a trade."""
    try:
        if not snap or not snap.get('ok'):
            return None
        rg = snap.get('regime') or {}
        mtf = snap.get('mtf') or {}
        leg = snap.get('leg') or {}
        return {
            'bar_ms': int(snap.get('bar_time_ms') or 0),
            'regime': rg.get('state'), 'regime_label': rg.get('label'),
            'regime_confidence': rg.get('confidence'),
            'volatility': (rg.get('volatility') or {}).get('state'),
            'mtf_score': mtf.get('score'), 'mtf_direction': mtf.get('direction'),
            'atr': snap.get('atr'),
            'session': (snap.get('session') or {}).get('primary'),
            'leg': ({'dir': leg.get('dir'), 'ext_atr': round(float(leg.get('ext_atr') or 0), 2),
                     'pull_atr': round(float(leg.get('pull_atr') or 0), 2)} if leg else None),
        }
    except Exception:                                          # noqa: BLE001
        return None


def record(rec: dict, row: dict | None, *, source: str, session: str = None,
           exit_snap: dict = None) -> dict:
    """
    The journal line for a CLOSED signal-store record `rec` and its order
    ledger row `row` (live: the order ledger; lab: its in-memory copy).
    """
    row = row or {}
    sig = rec.get('signal') or {}
    ev = [e for e in (sig.get('evidence') or []) if isinstance(e, dict)]
    top = sorted(ev, key=lambda e: -float(e.get('weight') or 0))[:3]
    sent = rec.get('send_verdict') or {}
    ctx = rec.get('entry_context') or {}
    return {
        'v': VERSION, 'source': source, 'session': session,
        # live times are UTC (the bridge's clock); the lab runs on the disk
        # bars' broker time (New York + 7 h)
        'clock': 'utc' if source == 'live' else 'broker',
        'signal_id': rec.get('id'), 'symbol': rec.get('symbol'), 'tf': rec.get('tf'),
        'side': rec.get('side'), 'playbook': rec.get('playbook'),
        'kind': rec.get('kind') or row.get('kind'),
        'final_bar_ms': rec.get('final_bar_ms'),
        'fill_ms': rec.get('fill_ms'), 'exit_ms': rec.get('exit_ms'),
        'entry_planned': sig.get('entry'), 'fill_price': rec.get('fill_price'),
        'exit_price': rec.get('close_price'),
        'stop': sig.get('stop'), 'tp1': sig.get('tp1'), 'tp2': sig.get('tp2'),
        'atr': sig.get('atr_at_signal'), 'lots': rec.get('lots'),
        'outcome': rec.get('outcome'), 'r_multiple': rec.get('r'),
        'profit': row.get('profit'),
        'trail_armed': bool((rec.get('trail') or {}).get('armed')),
        'confidence': sig.get('confidence'),
        'confidence_at_send': sent.get('confidence'),
        'send_warnings': sent.get('warnings'),
        'regime_at_entry': ctx.get('regime'),
        'entry_context': ctx or None,
        'exit_context': market_context(exit_snap),
        'evidence_top3': [{'text': e.get('text'), 'weight': e.get('weight'),
                           'kind': e.get('kind')} for e in top],
        'evidence': [{'text': e.get('text'), 'weight': e.get('weight'),
                      'kind': e.get('kind')} for e in ev],
        'against': list(sig.get('against') or []),
        'invalidation': sig.get('invalidation'),
    }


def append(path: Path, line: dict) -> None:
    """One JSON line; never raises."""
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(line, default=str) + '\n')
    except Exception:                                          # noqa: BLE001
        pass


def send_verdict(q) -> dict:
    """What the executor knew when it sent: the fresh verdict's confidence and warnings."""
    try:
        return {'confidence': int(q.confidence),
                'judged_bar_ms': int(getattr(q, 'judged_bar_ms', 0) or 0),
                'warnings': [[g['name'], g.get('penalty', 0), g['detail']]
                             for g in (q.gates or []) if g.get('verdict') == 'WARN']}
    except Exception:                                          # noqa: BLE001
        return {}


__all__ = ['LIVE_LOG', 'VERSION', 'market_context', 'record', 'append', 'send_verdict']
