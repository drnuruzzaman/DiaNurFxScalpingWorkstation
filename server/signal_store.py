"""
The signal store: every issued signal, its frozen levels, and where it is in
its life. Saved to disk, so a restart neither forgets a signal nor re-issues it.

Lifecycle

    FORMING   detected on the bar that is still open. Shown, never traded -
              an open bar can repaint and the setup can vanish at the close.
    FINAL     the bar closed and the setup was still there. Entry, stop and
              targets are locked HERE, rounded to the broker's tick, and never
              recomputed.
    SENT      the order reached MT5 (market or pending).
    FILLED    a position exists.
    CLOSED    the position is gone - by its stop, its trailed stop, or by hand.

    Early ends:
    EXPIRED   its expiry_bars ran out before it filled.
    CANCELLED it could not or should not trade (one-per-symbol-and-timeframe,
              price already through the stop, the order vanished at the broker).
    REVERSED  the opposite side went FINAL on the same symbol and timeframe
              before this one filled. A FILLED position is never reversed - it
              is left to its own stop (decision D).

FINAL is decided on CLOSED bars only: when a new bar appears, the engine is
re-run on the series without the forming bar, and whatever it detects there is
what gets finalised. That is the same information a backtest acts on, which is
the point - the live system trades what was tested, not a preview of it.

The id of a FINAL signal is symbol:tf:playbook:side:closed_bar_ms. It is stable
across passes and restarts, and it is the key the order ledger uses.
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

from . import clock

FORMING, FINAL, SENT, FILLED, CLOSED = 'FORMING', 'FINAL', 'SENT', 'FILLED', 'CLOSED'
EXPIRED, CANCELLED, REVERSED = 'EXPIRED', 'CANCELLED', 'REVERSED'
ACTIVE = (FINAL, SENT, FILLED)
TERMINAL = (CLOSED, EXPIRED, CANCELLED, REVERSED)

# Finished signals are kept this long for the board and the audit trail; the
# order ledger keeps its own copy for good.
KEEP_FINISHED_MS = 7 * 24 * 3600 * 1000


def _now() -> int:
    # Wall clock live; the backtest lab points this at simulated time.
    return clock.now_ms()


def round_to_tick(x: float, tick: float, digits: int) -> float:
    return round(round(float(x) / tick) * tick, digits)


def broker_round(sig: dict, spec: dict) -> dict:
    """
    Round every level to the symbol's tick and make the stop legal.

    Done once, at FINAL, so the levels on the chart, in Telegram and in the
    MT5 order are the same numbers - not three roundings of one idea.
    """
    tick = float(spec.get('tick_size') or spec.get('point') or 0.01)
    digits = int(spec.get('digits') or 2)
    point = float(spec.get('point') or tick)
    for f in ('entry', 'stop', 'tp1', 'tp2', 'trigger'):
        if sig.get(f):
            sig[f] = round_to_tick(sig[f], tick, digits)
    # The broker's minimum stop distance, plus a tick so rounding cannot land
    # exactly on the limit.
    floor = max(int(spec.get('stops_level_points') or 0) * point, tick)
    sign = 1.0 if sig['side'] == 'buy' else -1.0
    if abs(sig['entry'] - sig['stop']) < floor:
        sig['stop'] = round_to_tick(sig['entry'] - sign * floor, tick, digits)
    risk = abs(sig['entry'] - sig['stop'])
    sig['risk_points'] = round(risk, digits)
    if risk > 0:
        sig['rr1'] = round(abs(sig['tp1'] - sig['entry']) / risk, 2)
        sig['rr2'] = round(abs(sig['tp2'] - sig['entry']) / risk, 2)
    return sig


class SignalStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.recs: dict = {}          # id -> record
        self._last_bar: dict = {}     # (symbol, tf) -> forming bar time last seen
        self._forming: dict = {}      # (symbol, tf) -> [Signal], display only
        self.load()

    # ------------------------------------------------------------ persistence
    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.recs = data.get('records') or {}
        except (OSError, ValueError):
            self.recs = {}

    def save(self) -> None:
        with self._lock:
            now = _now()
            for k in [k for k, r in self.recs.items()
                      if r['stage'] in TERMINAL and now - r['updated_ms'] > KEEP_FINISHED_MS]:
                del self.recs[k]
            blob = json.dumps({'records': self.recs, 'saved_ms': now}, indent=1, default=str)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.json.tmp')
            tmp.write_text(blob, encoding='utf-8')
            tmp.replace(self.path)          # never a half-written store

    # ------------------------------------------------------------- lifecycle
    def bar_closed(self, symbol: str, tf: str, forming_bar_ms: int) -> bool:
        """True the first time a new forming bar is seen - i.e. one just closed."""
        key = (symbol, tf)
        with self._lock:
            if self._last_bar.get(key) == forming_bar_ms:
                return False
            self._last_bar[key] = forming_bar_ms
            return True

    def active_for(self, symbol: str, tf: str, side: str = None) -> list:
        with self._lock:
            return [r for r in self.recs.values()
                    if r['symbol'] == symbol and r['tf'] == tf
                    and r['stage'] in ACTIVE and (side is None or r['side'] == side)]

    def finalize(self, symbol: str, tf: str, detections: list, spec: dict,
                 closed_bar_ms: int, tf_ms: int) -> list:
        """
        Lock what the engine found on the bar that just closed.

        Returns the ids created. A side that already has a live signal on this
        symbol and timeframe is not re-issued - the live one carries the idea.
        """
        created = []
        with self._lock:
            for s in detections:
                if getattr(s, 'status', '') == 'conflicted':
                    continue
                fid = f'{symbol}:{tf}:{s.playbook}:{s.side}:{int(closed_bar_ms)}'
                if fid in self.recs or self.active_for(symbol, tf, s.side):
                    continue
                opposite = 'sell' if s.side == 'buy' else 'buy'
                for r in self.active_for(symbol, tf, opposite):
                    if r['stage'] == FINAL:
                        self._move(r, REVERSED, f'opposite FINAL signal {fid}')
                    elif r['stage'] == SENT:
                        # The executor cancels the pending order, then marks it.
                        r['reverse'] = fid
                    # FILLED: left to its own stop, by decision.
                d = broker_round(copy.deepcopy(s.to_dict()), spec)
                d['id'] = fid
                now = _now()
                self.recs[fid] = {
                    'id': fid, 'symbol': symbol, 'tf': tf, 'side': s.side,
                    'playbook': s.playbook, 'stage': FINAL,
                    'signal': d,
                    'final_bar_ms': int(closed_bar_ms),
                    # expiry_bars counted from the close of the FINAL bar
                    'expires_ms': int(closed_bar_ms) + tf_ms * (1 + int(s.expiry_bars or 20)),
                    'created_ms': now, 'updated_ms': now,
                    'history': [[now, FINAL, 'bar closed with the setup intact']],
                    'qual': None, 'trail': {}, 'reverse': None,
                }
                created.append(fid)
            if created:
                self.save()
        return created

    def set_forming(self, symbol: str, tf: str, detections: list) -> None:
        with self._lock:
            self._forming[(symbol, tf)] = [s for s in detections
                                           if getattr(s, 'status', '') != 'conflicted']

    def view(self, symbol: str, tf: str) -> list:
        """
        Signals to qualify and show for this chart: the live FINAL/SENT/FILLED
        ones with their frozen levels, then FORMING ones for sides that have
        nothing live. Always copies - qualification writes into them.
        """
        from .engine.signals import Signal
        out = []
        with self._lock:
            live_sides = set()
            for r in self.active_for(symbol, tf):
                sig = Signal(**{k: v for k, v in copy.deepcopy(r['signal']).items()
                                if k in Signal.__dataclass_fields__})
                sig.id = r['id']
                sig.stage = r['stage']
                out.append(sig)
                live_sides.add(r['side'])
            for s in self._forming.get((symbol, tf), []):
                if s.side in live_sides:
                    continue
                f = copy.deepcopy(s)
                f.stage = FORMING
                f.id = f'{symbol}:{tf}:forming:{s.side}'
                out.append(f)
        return out

    def note_qualification(self, sigs: list) -> None:
        """Remember each live signal's latest verdict, for the board and the ledger."""
        with self._lock:
            for s in sigs:
                r = self.recs.get(s.id)
                if r is None or r['stage'] not in ACTIVE:
                    continue
                r['qual'] = {'status': s.status, 'reason': s.reason,
                             'confidence': s.confidence,
                             'net_rr2': (s.sizing or {}).get('net_rr2'),
                             'at_ms': _now()}

    def move(self, fid: str, stage: str, note: str = '', **fields) -> None:
        with self._lock:
            r = self.recs.get(fid)
            if r is None:
                return
            self._move(r, stage, note, **fields)
            self.save()

    def update(self, fid: str, **fields) -> None:
        with self._lock:
            r = self.recs.get(fid)
            if r is None:
                return
            r.update(fields)
            r['updated_ms'] = _now()
            self.save()

    def _move(self, rec: dict, stage: str, note: str, **fields) -> None:
        # `rec`, not `r`: callers pass the trade's R multiple as a field named
        # r=..., which collided with this parameter and crashed every close.
        rec.update(fields)
        if rec['stage'] != stage:
            rec['history'].append([_now(), stage, note])
        rec['stage'] = stage
        rec['updated_ms'] = _now()

    def get(self, fid: str):
        with self._lock:
            return copy.deepcopy(self.recs.get(fid))

    def active(self) -> list:
        with self._lock:
            return [copy.deepcopy(r) for r in self.recs.values() if r['stage'] in ACTIVE]

    def listing(self, limit: int = 200) -> list:
        with self._lock:
            rows = sorted(self.recs.values(), key=lambda r: -r['updated_ms'])[:limit]
            return copy.deepcopy(rows)


__all__ = ['SignalStore', 'broker_round', 'round_to_tick', 'FORMING', 'FINAL', 'SENT',
           'FILLED', 'CLOSED', 'EXPIRED', 'CANCELLED', 'REVERSED', 'ACTIVE', 'TERMINAL']
