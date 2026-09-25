"""
The order ledger: which signals became orders, and what happened to them.

Keyed by the FINAL signal id. The one rule it exists to enforce is ONE ORDER
PER SIGNAL, EVER - across retries, restarts and crashes.

The dangerous moment is between asking MT5 for an order and learning the
answer. If the process dies there, or the reply is lost, the order may or may
not exist. So the ledger writes an INTENT to disk before anything is sent, and
a signal with an unresolved intent is never sent again until the broker has
been searched for it:

    sending   intent recorded, request going out
    placed    MT5 accepted it (ticket known)
    unknown   no answer - the order MAY exist. Resolved by searching the
              broker's orders, positions and deals for this signal's tag.
    refused   MT5 or the bridge said no; nothing exists. May be retried.
    filled / closed / cancelled / expired   the order's afterlife

Every order carries a tag in its MT5 comment (DNX + 10 hex of the signal id),
which is how an order found at the broker is tied back to its signal even if
the ledger never learned its ticket.

Nothing is ever deleted from the ledger. It is the audit trail.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from . import clock

# States in which the signal must NOT be sent again.
BLOCKING = ('sending', 'placed', 'unknown', 'filled', 'closed', 'cancelled', 'expired')


# MT5 truncates an order comment at 31 characters. "DNX " plus 10 hex of the
# signal id is 14, which leaves 17 - one separator and 16 for a playbook name.
COMMENT_MAX = 31

# Short forms for the names that do not fit. Only the long ones need an entry;
# anything else is used as-is and truncated if it somehow overruns.
PLAYBOOK_SHORT = {
    'trend_continuation': 'TRENDCONT',
    'flag_continuation': 'FLAGCONT',
    'breakout_retest': 'BRKRETEST',
    'sweep_reversal': 'SWEEPREV',
    'false_break_fade': 'FBFADE',
    'pattern_break': 'PATBREAK',
    'mtf_pullback': 'MTFPULL',
    'false_break': 'FBREAK',
    'range_fade': 'RANGEFADE',
    'last_break': 'LASTBREAK',
}


def tag_for(signal_id: str) -> str:
    """
    The prefix that ties an order back to its signal.

    Reconciliation matches on this with startswith(), so it must stay at the
    FRONT of the comment - anything added for a human goes after it.
    """
    return 'DNX ' + hashlib.sha1(signal_id.encode('utf-8')).hexdigest()[:10]


def comment_for(signal_id: str, playbook: str = '', tf: str = '') -> str:
    """
    The full MT5 comment: the machine tag, then the trade's story for a human.

    Reading a broker statement or the terminal's own history, "DNX 3f2a..."
    says which system placed the order but nothing about why. The timeframe
    and the playbook are the why, and they are free to carry - the tag only
    used 14 of the 31 characters MT5 allows.

    Order is deliberate: tag, then timeframe, then playbook.

      THE TAG stays at the front because reconciliation matches on it with
      startswith(). Nothing may be inserted before it.
      THE TIMEFRAME comes next because it is two or three characters and must
      survive whole - "15m" truncated to "15" is a different claim. Putting
      it after the playbook would make it the first thing MT5 cut off.
      THE PLAYBOOK takes whatever is left and is truncated if it overruns,
      which is the right thing to lose: "BRKRETES" still reads.
    """
    tag = tag_for(signal_id)
    parts = [tag]
    room = COMMENT_MAX - len(tag)

    slot = (tf or '').strip().lower()
    if slot and room >= len(slot) + 1:
        parts.append(slot)
        room -= len(slot) + 1

    name = (playbook or '').strip().lower()
    if name and room > 1:
        short = PLAYBOOK_SHORT.get(name, name.replace('_', '').upper())
        parts.append(short[:room - 1])
    return ' '.join(parts)


def _now() -> int:
    # Wall clock live; the backtest lab points this at simulated time.
    return clock.now_ms()


class OrderLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.rows: dict = {}
        try:
            self.rows = json.loads(self.path.read_text(encoding='utf-8')).get('orders') or {}
        except (OSError, ValueError):
            self.rows = {}

    def _save(self) -> None:
        blob = json.dumps({'orders': self.rows, 'saved_ms': _now()}, indent=1, default=str)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(blob, encoding='utf-8')
        tmp.replace(self.path)

    def get(self, fid: str):
        with self._lock:
            r = self.rows.get(fid)
            return dict(r) if r else None

    def may_send(self, fid: str) -> bool:
        with self._lock:
            r = self.rows.get(fid)
            return r is None or r['state'] not in BLOCKING

    def intent(self, fid: str, request: dict) -> dict:
        """Record the order BEFORE it is sent. Raises if this signal already has one."""
        with self._lock:
            if not self.may_send(fid):
                raise RuntimeError(f'signal {fid} already has an order '
                                   f'({self.rows[fid]["state"]})')
            prev = self.rows.get(fid)
            row = {
                'id': fid, 'tag': tag_for(fid), 'state': 'sending',
                'attempts': (prev or {}).get('attempts', 0) + 1,
                'request': request, 'ticket': None, 'position': None,
                'fill_price': None, 'close_price': None, 'outcome': None,
                'r': None, 'profit': None,
                'events': (prev or {}).get('events', []) + [[_now(), 'sending', '']],
                'created_ms': (prev or {}).get('created_ms', _now()), 'updated_ms': _now(),
                **{k: request.get(k) for k in ('symbol', 'tf', 'side', 'kind', 'lots')},
            }
            self.rows[fid] = row
            self._save()        # on disk BEFORE the request leaves this process
            return dict(row)

    def mark(self, fid: str, state: str, note: str = '', **fields) -> None:
        with self._lock:
            r = self.rows.get(fid)
            if r is None:
                return
            r.update(fields)
            if r['state'] != state or note:
                r['events'].append([_now(), state, note])
            r['state'] = state
            r['updated_ms'] = _now()
            self._save()

    def live_for(self, symbol: str, tf: str) -> list:
        """Orders that still occupy this symbol+timeframe slot (decision C)."""
        with self._lock:
            return [dict(r) for r in self.rows.values()
                    if r.get('symbol') == symbol and r.get('tf') == tf
                    and r['state'] in ('sending', 'placed', 'unknown', 'filled')]

    def live_count(self) -> int:
        with self._lock:
            return sum(1 for r in self.rows.values()
                       if r['state'] in ('sending', 'placed', 'unknown', 'filled'))

    def placed_since(self, since_ms: int) -> int:
        """Orders the broker accepted at or after `since_ms` - the day's sends."""
        from .daily import sends_since
        with self._lock:
            return sends_since(self.rows.values(), since_ms)

    def listing(self, limit: int = 200) -> list:
        with self._lock:
            return [dict(r) for r in sorted(self.rows.values(),
                                            key=lambda r: -r['updated_ms'])[:limit]]


__all__ = ['OrderLedger', 'tag_for', 'BLOCKING']
