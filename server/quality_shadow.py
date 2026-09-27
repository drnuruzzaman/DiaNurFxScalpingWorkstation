"""
server/quality_shadow.py - shadow live for playbook rule C1: logged, never used.

tools/playbook_quality.py (2026-09-27) found one quality rule that held up in
both periods on XAUUSD 5m: C1, pattern_break only on the classic patterns -
head & shoulders (and inverse), double and triple tops and bottoms - with
quality >= 65. It was the user's call to put it on the shadow log, not live.

So, beside every pattern_break order the live executor sends on 5m, this
records whether C1 would have let that order through:

    pass      one of the patterns behind the signal is classic, quality >= 65
    veto      none is - under C1 this order would not have been sent
    abstain   the verdict for the signal's bar was not kept (restart between
              the bar close and the send, or an error)

The verdict is taken on the SAME closed-bar analysis the signal was made from
(remember_closed, at each bar close), and looked up when the order is sent
(note_send) by the signal's bar. It never reaches the executor, the gates, the
sizing or the order: this module only appends to a log. Months of that log,
joined to the order ledger (tools/shadow_report.py), say whether the orders C1
would have vetoed really did worse live.

Both hooks return at once and never raise.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / 'order_ledger' / 'logs' / 'shadow_quality.jsonl'
RULE = 'C1'
TFS = ('5m',)
PLAYBOOK = 'pattern_break'
MIN_QUALITY = 65
KEEP = 400                          # bar verdicts held in memory, newest kept

_VERDICTS: dict = {}                # (symbol, tf, bar_ms) -> {side: verdict}
_LOCK = threading.Lock()


def c1_sides(snap: dict) -> dict:
    """
    For each side pb_pattern_break would trade on this snapshot, the patterns
    behind it and whether C1 admits it (any of them classic, quality >= 65).

    The pattern filters are pb_pattern_break's own, line for line: not
    neutral, actionable, trigger within 1.6 ATR, a stop on the right side of
    the entry. tools/test_quality_shadow.py holds the two together.
    """
    from .engine.signals import CLASSIC_PATTERNS
    out: dict = {}
    atr_v = float(snap.get('atr') or 0.0)
    price = float(snap.get('price') or 0.0)
    if not atr_v > 0:
        return out
    for p in (snap.get('patterns') or [])[:4]:
        if p['direction'] == 'neutral' or not p.get('actionable'):
            continue
        if abs(p['break_level'] - price) / atr_v > 1.6:
            continue
        side = 'buy' if p['direction'] == 'bullish' else 'sell'
        entry = p['break_level'] if p['status'] == 'forming' else price
        pad = atr_v * 0.2
        stop = p['invalidation'] - pad if side == 'buy' else p['invalidation'] + pad
        if (side == 'buy' and stop >= entry) or (side == 'sell' and stop <= entry):
            continue
        classic = p['kind'] in CLASSIC_PATTERNS and int(p.get('quality') or 0) >= MIN_QUALITY
        s = out.setdefault(side, {'pass': False, 'patterns': []})
        s['patterns'].append({'kind': p['kind'], 'quality': int(p.get('quality') or 0),
                              'status': p['status'], 'classic': classic})
        s['pass'] = s['pass'] or classic
    return out


def remember_closed(symbol: str, tf: str, snap: dict) -> None:
    """At each bar close: C1's verdict per side, for a send that may follow."""
    try:
        if tf not in TFS or not snap or not snap.get('ok'):
            return
        sides = c1_sides(snap)
        with _LOCK:
            _VERDICTS[(symbol, tf, int(snap.get('bar_time_ms') or 0))] = sides
            while len(_VERDICTS) > KEEP:
                _VERDICTS.pop(next(iter(_VERDICTS)))
    except Exception:                                          # noqa: BLE001
        pass


def note_send(rec: dict) -> None:
    """The executor sent an order: log what C1 would have said about it."""
    try:
        if rec.get('playbook') != PLAYBOOK or rec.get('tf') not in TFS:
            return
        key = (rec.get('symbol'), rec.get('tf'), int(rec.get('final_bar_ms') or 0))
        with _LOCK:
            sides = _VERDICTS.get(key)
        row = {'at_ms': int(time.time() * 1000), 'id': rec.get('id'),
               'symbol': rec.get('symbol'), 'tf': rec.get('tf'), 'side': rec.get('side'),
               'playbook': PLAYBOOK, 'final_bar_ms': key[2], 'filter': RULE}
        if sides is None:
            row.update(verdict='abstain', why='no verdict kept for the signal bar')
        elif rec.get('side') not in sides:
            row.update(verdict='abstain', why='no pattern on this side at the signal bar')
        else:
            s = sides[rec['side']]
            row.update(verdict='pass' if s['pass'] else 'veto', patterns=s['patterns'],
                       why=None if s['pass'] else
                       f'no classic pattern with quality >= {MIN_QUALITY}')
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, default=str) + '\n')
    except Exception:                                          # noqa: BLE001
        pass


__all__ = ['LOG', 'RULE', 'c1_sides', 'remember_closed', 'note_send']
