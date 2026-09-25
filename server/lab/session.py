"""
server/lab/session.py - one replay: history, a simulated account, a recorder.

A session walks a symbol's history one CLOSED bar at a time, exactly as the
live engine meets it:

    for every minute inside the bar      the broker walks the M1 path (fills,
                                         stops, targets) and the executor
                                         ticks - trailing, pending checks
    at the bar's close                   the engine analyses the closed bars
                                         (higher timeframes on THEIR closed
                                         bars), FINAL signals are locked, the
                                         executor decides what to send

Nothing past the bar being closed is ever handed to the engine. The chart may
show the future ("reveal"); the engine never sees it.

Frontier and view
-----------------
The FRONTIER (`k`) is the last bar simulated. The VIEW (`v`) is the bar on
screen and can be anywhere from the first decision bar up to the frontier:
stepping back only moves the view, through bars already simulated, so the
account never un-happens. Stepping forward past the frontier simulates more
market. Manual trading happens at the frontier only.

Deterministic
-------------
Same data, same code, same settings, same manual actions -> same trades. A
session is saved as its configuration plus its manual actions (with the bar
each happened on), so reopening it re-runs it and checks the result matches
what was recorded - which doubles as a regression test of the engine.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

import numpy as np

from .. import clock
from ..daily import day_start_ms, daily_pnl_pct, judging_context
from ..config import CONFIG, MTF_LADDER, TF_SECONDS
from ..datafeed import Series
from ..engine.analysis import analyse, quick_trend
from ..engine.qualify import fixed_lots, qualify, qualify_all
from ..engine.signals import Signal, generate
from ..executor import Executor
from ..order_ledger import OrderLedger, tag_for
from ..signal_store import (CANCELLED, CLOSED, EXPIRED, FILLED, REVERSED, SENT,
                            SignalStore)
from . import data as lab_data
from . import settings as lab_settings
from . import stats as lab_stats
from .broker import REASON_SL, REASON_TP, SimBroker, SimFeed

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / 'runs' / 'lab'
WINDOW = 600            # bars the engine analyses, as live (engine.bars_for_analysis)
WARM = 720              # history loaded before the first decision bar
MAX_BARS = 60_000       # longer than this is a batch job, not a replay
SNAP_CACHE = 240        # analyses kept for instant back-stepping
ENGINE_REV = 'unknown'  # set by app.py from git at start-up

_SID = re.compile(r'^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$')


def valid_sid(sid: str) -> bool:
    return bool(_SID.match(str(sid or '')))


def new_id() -> str:
    return dt.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]


def parse_ms(v) -> int:
    """Epoch ms from ms, or an ISO date/time read as UTC."""
    if v is None or v == '':
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(' ', 'T')
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def _wall_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# the live store and ledger, in memory, reporting to the recorder             #
# --------------------------------------------------------------------------- #
class MemStore(SignalStore):
    """The live SignalStore unchanged, minus the disk, plus a journal hook."""

    def __init__(self, on_move, on_note):
        self._on_move, self._on_note = on_move, on_note
        super().__init__(ROOT / 'runs' / 'lab' / '.unused')

    def load(self) -> None:
        self.recs = {}

    def save(self) -> None:
        pass

    def _move(self, rec, stage, note, **fields):
        before = rec['stage']
        super()._move(rec, stage, note, **fields)
        if before != stage:
            self._on_move(rec, stage, note)

    def update(self, fid, **fields):
        before = (self.recs.get(fid) or {}).get('note')
        super().update(fid, **fields)
        note = fields.get('note')
        if note and note != before:
            self._on_note(fid, note)


class MemLedger(OrderLedger):
    """The live OrderLedger unchanged, minus the disk, plus a journal hook."""

    def __init__(self, on_mark):
        self._lock = threading.RLock()
        self.rows = {}
        self.path = None
        self._on_mark = on_mark

    def _save(self) -> None:
        pass

    def mark(self, fid, state, note='', **fields):
        super().mark(fid, state, note, **fields)
        self._on_mark(fid, state, note)


# --------------------------------------------------------------------------- #
# the session                                                                 #
# --------------------------------------------------------------------------- #
class ReplaySession:
    def __init__(self, cfg: dict, sid: str = None, created_ms: int = None):
        self.sid = sid or new_id()
        self.created_ms = created_ms or _wall_ms()
        self.lock = threading.RLock()
        self.cfg = self._normalise(cfg)
        self.dir = RUNS / self.sid
        self.commands: list = []
        self.pending: list = []          # manual actions waiting to be re-applied
        self.bar_notes: list = []
        self.snaps: list = []
        self.trade_tags: dict = {}
        self.saved: dict | None = None
        self.run_no = 0
        self._load_data()
        self.reset()

    # ------------------------------------------------------------- set-up
    def _normalise(self, cfg: dict) -> dict:
        symbol = str(cfg.get('symbol') or CONFIG.symbol)
        tf = str(cfg.get('tf') or '5m')
        if tf not in TF_SECONDS:
            raise ValueError(f'unknown timeframe {tf}')
        if symbol not in lab_data.symbols():
            raise ValueError(f'{symbol} has no 1m history on disk - the lab needs it to fill orders')
        cover = lab_data.coverage(symbol)
        if tf not in cover:
            raise ValueError(f'no {tf} history for {symbol} on disk')
        last = min(cover[tf]['last_ms'], cover.get('1m', cover[tf])['last_ms'])
        start = parse_ms(cfg.get('start')) or (last - 14 * 86_400_000)
        end = parse_ms(cfg.get('end')) or last
        end = min(end, last)
        if start >= end:
            raise ValueError('the start must be before the end of the data '
                             f'({dt.datetime.fromtimestamp(last / 1000, dt.timezone.utc):%Y-%m-%d %H:%M} UTC)')
        mode = cfg.get('mode') if cfg.get('mode') in ('auto', 'manual') else 'auto'
        name = str(cfg.get('name') or '').strip() or \
            f"{symbol} {tf} from {dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc):%d %b %Y %H:%M}"
        return {'symbol': symbol, 'tf': tf, 'start': int(start), 'end': int(end),
                'mode': mode, 'name': name[:80], 'record': bool(cfg.get('record', True)),
                'overrides': copy.deepcopy(cfg.get('overrides') or {}),
                'notes': str(cfg.get('notes') or ''), 'tags': list(cfg.get('tags') or [])}

    def _load_data(self) -> None:
        c = self.cfg
        sym, tf = c['symbol'], c['tf']
        self.tf_ms = TF_SECONDS[tf] * 1000
        self.spec = lab_data.spec(sym)
        self.series, self.i0 = lab_data.load(sym, tf, c['start'], c['end'], WARM)
        n = len(self.series)
        if n - self.i0 < 2:
            raise ValueError('no bars between that start and end')
        if self.i0 < WINDOW:
            raise ValueError(f'only {self.i0} bars of history before the start - the engine '
                             f'needs {WINDOW}. Pick a later start.')
        if n - self.i0 > MAX_BARS:
            raise ValueError(f'{n - self.i0} bars is more than a replay handles '
                             f'({MAX_BARS}). Shorten the range.')
        if tf == '1m':
            self.m1 = self.series
        else:
            self.m1, _ = lab_data.load(sym, '1m', int(self.series.t[self.i0 - 1]),
                                       c['end'] + self.tf_ms, 2)
        self.htf = {}
        for name in dict.fromkeys(MTF_LADDER.get(tf, [tf])):
            if name == tf:
                continue
            s, _ = lab_data.load(sym, name, c['start'], c['end'] + TF_SECONDS[name] * 1000, 320)
            if len(s) >= 60:
                self.htf[name] = (s, s.t + TF_SECONDS[name] * 1000)

    def reset(self) -> None:
        """A fresh run from the first bar, with this session's settings."""
        with self.lock:
            self.eff = lab_settings.effective(self.cfg['overrides'])
            self.activate()
            self.run_no += 1
            self.events: list = []
            self.trades: list = []
            self.frames: list = []
            self._snaps = OrderedDict()
            self._mtf = {}
            self.k = self.i0 - 1
            self.v = self.k
            self._bar_i = self.i0
            self.closed_snap = None
            self.cancel = False
            self.now_ms = int(self.series.t[self.k]) + self.tf_ms
            self.balance0 = float(self.eff['risk']['equity'])
            self.broker = SimBroker(self.cfg['symbol'], self.spec, self.balance0,
                                    self.eff['instrument'].get('default_slippage_points', 3.0),
                                    clock=lambda: self.now_ms)
            self.broker.on_close = self._on_close
            self.broker.on_event = self._on_broker_event
            self.broker.set_quote(float(self.series.c[self.k]), self._spread(self.k))
            self.store = MemStore(self._on_move, self._on_note)
            self.ledger = MemLedger(self._on_mark)
            self.feed = SimFeed(self)
            self.executor = Executor(
                self.store, self.ledger, self.broker, self.feed, CONFIG,
                requalify=self._requalify, auto=lambda: self.cfg['mode'] == 'auto',
                trading_enabled=lambda: True, on_sent=self._on_sent,
                tf_ms=lambda tf: self.tf_ms, log=self._log)
            self._day = None
            self._day_sends = 0
            self._day_start = self.balance0
            # Manual actions start over with the run. Re-applying recorded ones
            # is open()'s job: it queues them in `pending` before re-running.
            self.commands = []
            self._emit('session', f"run {self.run_no}: {self.cfg['symbol']} {self.cfg['tf']}, "
                                  f"{self.cfg['mode']} mode, balance {self.balance0:,.2f}")
            self._advance_one()
            self.v = self.k

    def activate(self) -> None:
        """This session's settings and clock onto this (lab) process."""
        lab_settings.activate(self.eff)
        # Auto mode trades the session's own timeframe, whatever the live
        # auto list says - the lab replays one timeframe at a time.
        CONFIG.execution.auto_timeframes = (self.cfg['tf'],)
        clock.set_source(lambda: self.now_ms)

    # --------------------------------------------------------------- hooks
    def _log(self, msg: str) -> None:
        self._emit('error', str(msg))

    def _emit(self, kind: str, text: str, **data) -> dict:
        e = {'seq': len(self.events), 't': int(self.now_ms), 'i': int(self._bar_i),
             'kind': kind, 'text': text}
        if data:
            e['data'] = data
        self.events.append(e)
        return e

    def _on_broker_event(self, kind, text, data) -> None:
        self._emit(kind, 'manual: ' + text, **(data or {}))

    def _on_move(self, rec, stage, note) -> None:
        sig = rec.get('signal') or {}
        label = f"{rec['side'].upper()} {rec.get('playbook', '')}"
        if stage == SENT:
            row = self.ledger.get(rec['id']) or {}
            kind = row.get('kind') or rec.get('kind')
            at = '' if kind == 'market' else f" at {sig.get('entry')}"
            self._emit('order', f"{label}: {kind} {row.get('lots', '')} lots{at}",
                       id=rec['id'], order_kind=kind)
        elif stage == FILLED:
            self._emit('fill', f"{label}: filled at {rec.get('fill_price')}", id=rec['id'])
        elif stage in (EXPIRED, CANCELLED, REVERSED):
            self._emit('cancel', f"{label}: {stage.lower()} - {note}", id=rec['id'],
                       stage=stage)
        elif stage == CLOSED:
            pass                                    # the trade record says it better

    def _on_note(self, fid, note) -> None:
        rec = self.store.recs.get(fid) or {}
        self._emit('decision', f"{rec.get('side', '').upper()} {rec.get('playbook', '')}: {note}",
                   id=fid)

    def _on_mark(self, fid, state, note) -> None:
        if state == 'filled' and str(note).startswith('stop ->'):
            rec = self.store.recs.get(fid) or {}
            self._emit('stop', f"{rec.get('side', '').upper()} {rec.get('playbook', '')}: "
                               f"trailed {note}", id=fid)

    def _on_sent(self, rec) -> None:
        self._day_sends += 1

    def _rec_for_comment(self, comment: str):
        for r in self.store.recs.values():
            if str(comment or '').startswith(tag_for(r['id'])):
                return r
        return None

    def _on_close(self, pos, deal) -> None:
        """Every closed position becomes a trade record - executor or manual."""
        rec = self._rec_for_comment(pos['comment'])
        sig = (rec or {}).get('signal') or {}
        buy = pos['side'] == 'buy'
        sign = 1.0 if buy else -1.0
        entry, exit_px = float(pos['price_open']), float(deal['price'])
        stop0 = float(sig.get('stop') or pos.get('initial_sl') or 0)
        risk = abs(entry - stop0) if stop0 else 0.0
        r = round(sign * (exit_px - entry) / risk, 3) if risk else 0.0
        armed = bool(((rec or {}).get('trail') or {}).get('armed'))
        reason = deal['reason']
        outcome = ('target' if reason == REASON_TP else
                   ('trail' if armed else 'stop') if reason == REASON_SL else 'manual')
        mfe = mae = 0.0
        if risk:
            a = int(np.searchsorted(self.m1.t, (int(pos['time_ms']) // 60_000) * 60_000, 'left'))
            b = int(np.searchsorted(self.m1.t, int(deal['time_ms']), 'right'))
            if b > a:
                hi, lo = float(self.m1.h[a:b].max()), float(self.m1.l[a:b].min())
                fav, adv = ((hi - entry), (entry - lo)) if buy else ((entry - lo), (hi - entry))
                mfe, mae = round(max(0.0, fav) / risk, 2), round(max(0.0, adv) / risk, 2)
        profit = round(float(deal['profit']) + float(deal['commission'])
                       + float(pos.get('commission') or 0), 2)
        tid = rec['id'] if rec else f"manual-{pos['ticket']}"
        entry_t, exit_t = int(pos['time_ms']), int(deal['time_ms'])
        t = {
            'id': tid, 'n': len(self.trades) + 1, 'manual': rec is None,
            'playbook': rec.get('playbook') if rec else 'manual', 'side': pos['side'],
            'lots': pos['volume'], 'entry_t': entry_t, 'exit_t': exit_t,
            'entry_i': int(np.searchsorted(self.series.t, entry_t, 'right') - 1),
            'exit_i': int(np.searchsorted(self.series.t, exit_t, 'right') - 1),
            'entry': entry, 'exit': exit_px, 'stop0': stop0, 'sl_final': pos.get('sl'),
            'tp1': sig.get('tp1'), 'tp2': sig.get('tp2'), 'tp': pos.get('tp'),
            'outcome': outcome, 'r': r, 'profit': profit,
            'bars': round((exit_t - entry_t) / self.tf_ms, 1), 'mfe_r': mfe, 'mae_r': mae,
            'confidence': sig.get('confidence'),
            'kind': (self.ledger.get(rec['id']) or {}).get('kind') if rec else 'market',
            'final_bar_ms': (rec or {}).get('final_bar_ms'),
        }
        self.trades.append(t)
        self._emit('exit', f"{pos['side'].upper()} {t['playbook']}: {outcome} at {exit_px} "
                           f"({r:+.2f}R, {profit:+.2f})", id=tid, r=r, profit=profit)

    # --------------------------------------------------------- the engine
    def _spread(self, i: int) -> float:
        sp = self.series.spread
        if sp is None or i >= len(sp):
            return 20.0
        return float(sp[i]) or 20.0

    def _mtf_at(self, close_ms: int, k: int, memo: bool = True) -> dict:
        """
        Trend reads for the ladder, each from bars CLOSED by `close_ms` - the
        base timeframe's own last 300 closed bars included, as live builds it.
        """
        out = {}
        base = self.series.slice(max(0, k + 1 - 300), k + 1)
        if len(base) >= 60:
            out[self.cfg['tf']] = quick_trend(base)
        for name, (s, closes) in self.htf.items():
            j = int(np.searchsorted(closes, close_ms, 'right'))
            hit = self._mtf.get(name) if memo else None
            if hit and hit[0] == j:
                val = hit[1]
            else:
                w = s.slice(max(0, j - 300), j)
                val = quick_trend(w) if len(w) >= 60 else None
                if memo:
                    self._mtf[name] = (j, val)
            if val:
                out[name] = val
        # Ladder order, as live: base first, then coarser.
        order = [x for x in MTF_LADDER.get(self.cfg['tf'], [self.cfg['tf']])]
        return {k2: out[k2] for k2 in dict.fromkeys(order) if k2 in out}

    def _analyse(self, k: int, memo: bool = True) -> dict:
        window = self.series.slice(k + 1 - WINDOW, k + 1)
        close = int(self.series.t[k]) + self.tf_ms
        snap = analyse(window, self._mtf_at(close, k, memo), self.spec)
        if snap.get('ok'):
            snap['leg_gate'] = snap.get('leg')      # closed bars: the gate's own read
        return snap

    def snapshot_at(self, v: int) -> dict:
        hit = self._snaps.get(v)
        if hit is not None:
            self._snaps.move_to_end(v)
            return hit
        snap = self._analyse(v, memo=False)
        self._keep_snap(v, snap)
        return snap

    def _keep_snap(self, v: int, snap: dict) -> None:
        self._snaps[v] = snap
        self._snaps.move_to_end(v)
        while len(self._snaps) > SNAP_CACHE:
            self._snaps.popitem(last=False)

    def context(self) -> dict:
        b = self.broker
        # The same daily figures as live (server/daily.py): sends this broker
        # day, and realised-today plus floating over the day's opening balance.
        pct = daily_pnl_pct({'balance': b.balance, 'profit': b.floating()},
                            b.balance - self._day_start)
        return {
            'equity': b.equity(),
            'open_positions': len(b._pos),
            'trades_today': self._day_sends,
            'daily_pnl_pct': pct if pct is not None else 0.0,
            'spread_points': self._spread(self.k),
            'risk_per_trade_pct': CONFIG.risk.risk_per_trade_pct,
            # No historical calendar on disk: the news gate is not simulated.
            'minutes_to_high_impact': None,
        }

    def _requalify(self, rec):
        snap = self.closed_snap
        if not snap or not snap.get('ok'):
            return None
        sig = Signal(**{k: v for k, v in rec['signal'].items() if k in Signal.__dataclass_fields__})
        out = qualify(sig, snap, self.spec, judging_context(self.context(), rec.get('stage')))
        if out is not None:
            out.judged_bar_ms = int(snap.get('bar_time_ms') or 0)
        return out

    def _day_check(self) -> None:
        # The broker's day, as live. History on disk is stamped in broker
        # server time already (datafeed.py), so offset 0 cuts at the broker's
        # midnight - the same moment live's clock-plus-offset does.
        d = day_start_ms(self.now_ms, 0)
        if d != self._day:
            self._day = d
            self._day_sends = 0
            self._day_start = self.broker.balance

    def bars_now(self, count: int = 300):
        """What the live feed would return now: closed bars plus the forming one."""
        k = self.k
        s = self.series.slice(max(0, k + 2 - count), k + 1)
        t_open = int(self.series.t[k + 1]) if k + 1 < len(self.series) else int(s.t[-1]) + self.tf_ms
        a = int(np.searchsorted(self.m1.t, t_open, 'left'))
        b = int(np.searchsorted(self.m1.t, self.now_ms, 'left'))
        if b > a:
            o, h = float(self.m1.o[a]), float(self.m1.h[a:b].max())
            lo, c = float(self.m1.l[a:b].min()), float(self.m1.c[b - 1])
        else:
            o = h = lo = c = float(self.broker.bid or s.c[-1])
        return Series(symbol=s.symbol, tf=s.tf, t=np.append(s.t, t_open),
                      o=np.append(s.o, o), h=np.append(s.h, h), l=np.append(s.l, lo),
                      c=np.append(s.c, c), v=np.append(s.v, 0.0), source='lab')

    def _advance_one(self) -> bool:
        """Simulate the next bar: its minutes, then its close."""
        k1 = self.k + 1
        if k1 >= len(self.series):
            return False
        self._bar_i = k1
        t_open = int(self.series.t[k1])
        t_close = t_open + self.tf_ms
        m1 = self.m1
        a = int(np.searchsorted(m1.t, t_open, 'left'))
        b = int(np.searchsorted(m1.t, t_close, 'left'))
        sp_series = m1.spread
        for j in range(a, b):
            mt = int(m1.t[j])
            self.now_ms = mt
            self._day_check()
            sp = float(sp_series[j]) if sp_series is not None else self._spread(k1)
            self.broker.run_minute(mt, float(m1.o[j]), float(m1.h[j]), float(m1.l[j]),
                                   float(m1.c[j]), sp or self._spread(k1))
            self.now_ms = mt + 60_000
            self.executor.step()

        # ---- the close
        self.now_ms = t_close
        self._day_check()
        self.k = k1
        snap = self._analyse(k1)
        created = []
        if snap.get('ok'):
            self.closed_snap = snap
            self._keep_snap(k1, snap)
            window = self.series.slice(k1 + 1 - WINDOW, k1 + 1)
            created = self.store.finalize(self.cfg['symbol'], self.cfg['tf'],
                                          generate(snap, window), self.spec, t_open, self.tf_ms)
        # The price when the close is acted on: the next minute's open.
        nxt_bid = float(m1.o[b]) if b < len(m1) and int(m1.t[b]) < t_close + self.tf_ms \
            else float(self.series.c[k1])
        self.broker.set_quote(nxt_bid, self._spread(k1))
        # The journal reads in cause-and-effect order: each new signal and its
        # verdict first, then whatever the executor did about it.
        if created:
            fresh = [x for x in self.store.view(self.cfg['symbol'], self.cfg['tf'])
                     if x.id in created]
            for s in qualify_all(fresh, snap, self.spec, self.context()):
                blocks = [g['detail'] for g in s.gates if g['verdict'] == 'BLOCK']
                self._emit('signal',
                           f"{s.side.upper()} {s.playbook} at {s.entry}: {s.status}"
                           + (f" - {blocks[0]}" if blocks else f" (confidence {s.confidence})"),
                           id=s.id, status=s.status, playbook=s.playbook, side=s.side,
                           confidence=s.confidence, blocks=blocks[:3])
        self.executor.step()
        while self.pending and int(self.pending[0].get('i', -1)) <= k1:
            cmd = self.pending.pop(0)
            if int(cmd.get('i', -1)) == k1:
                self._apply(cmd)
                self.commands.append(cmd)
        sigs = []
        if snap.get('ok'):
            sigs = qualify_all(self.store.view(self.cfg['symbol'], self.cfg['tf']),
                               snap, self.spec, self.context())
            self.store.note_qualification(sigs)
        self.frames.append(self._frame_state(k1, sigs))
        return True

    def _frame_state(self, k: int, sigs: list) -> dict:
        return {'i': k, 't': int(self.series.t[k]), 'now': int(self.now_ms),
                'positions': self.broker.positions(), 'orders': self.broker.orders(),
                'account': self.account(), 'signals': [s.to_dict() for s in sigs],
                'ev': len(self.events), 'tr': len(self.trades)}

    def account(self) -> dict:
        return {'balance': round(self.broker.balance, 2), 'equity': self.broker.equity(),
                'floating': self.broker.floating(), 'open': len(self.broker._pos),
                'pending': len(self.broker._ord), 'balance0': self.balance0}

    # ---------------------------------------------------------- navigation
    @property
    def last(self) -> int:
        return len(self.series) - 1

    def at_end(self) -> bool:
        return self.k >= self.last

    def advance(self, n: int) -> int:
        """Simulate up to n more bars at the frontier. Returns how many ran."""
        ran = 0
        with self.lock:
            self.activate()
            while ran < n and not self.cancel and self._advance_one():
                ran += 1
        return ran

    def step(self, n: int) -> bool:
        """Move the view n bars; past the frontier the market is simulated."""
        with self.lock:
            before = self.v
            target = max(self.i0, min(self.last, self.v + n))
            if target > self.k:
                self.advance(target - self.k)
            self.v = max(self.i0, min(target, self.k))
            return self.v != before

    def index_at(self, t_ms: int) -> int:
        i = int(np.searchsorted(self.series.t, int(t_ms), 'right')) - 1
        return max(self.i0, min(self.last, i))

    def set_view(self, i: int) -> None:
        with self.lock:
            self.v = max(self.i0, min(int(i), self.k))

    def next_event_bar(self, kinds: tuple) -> int | None:
        """The first bar after the view where one of `kinds` happened, if simulated."""
        for e in self.events:
            if e['i'] > self.v and e['kind'] in kinds:
                return e['i']
        return None

    # ------------------------------------------------------------- actions
    def command(self, cmd: dict) -> dict:
        """A manual action - at the frontier only, recorded for re-runs."""
        with self.lock:
            if self.v != self.k:
                return {'ok': False, 'error': 'You are looking at the past. Jump to the '
                                              'latest bar to trade.'}
            self.activate()
            cmd = dict(cmd, i=self.k, t=int(self.now_ms))
            res = self._apply(cmd)
            if res.get('ok'):
                self.commands.append(cmd)
                # The bar on screen now shows the account after the action.
                sigs = []
                if self.closed_snap and self.closed_snap.get('ok'):
                    sigs = qualify_all(self.store.view(self.cfg['symbol'], self.cfg['tf']),
                                       self.closed_snap, self.spec, self.context())
                self.frames[-1] = self._frame_state(self.k, sigs)
            return res

    def _apply(self, cmd: dict) -> dict:
        op = cmd.get('op')
        b = self.broker
        if op == 'order':
            side = cmd.get('side')
            if side not in ('buy', 'sell'):
                return {'ok': False, 'error': 'side must be buy or sell'}
            lots = float(cmd.get('lots') or fixed_lots(self.cfg['symbol']))
            code, body = b.trade('/order/send', symbol=self.cfg['symbol'], side=side, lots=lots,
                                 sl=float(cmd.get('sl') or 0), tp=float(cmd.get('tp') or 0),
                                 kind='market', price=0.0, expiration_ms=0,
                                 comment=f"LAB MANUAL {len(self.commands) + 1}", confirm=1)
            if body.get('ok'):
                self._emit('manual', f"manual {side.upper()} {lots} at {body.get('price')} "
                                     f"(SL {cmd.get('sl') or '-'}, TP {cmd.get('tp') or '-'})",
                           ticket=body.get('ticket'))
            return {'ok': bool(body.get('ok')), 'error': body.get('error'), 'ticket': body.get('ticket')}
        if op == 'close':
            code, body = b.trade('/position/close', ticket=cmd.get('ticket'))
            return {'ok': bool(body.get('ok')), 'error': body.get('error')}
        if op == 'cancel':
            code, body = b.trade('/order/cancel', ticket=cmd.get('ticket'), confirm=1)
            return {'ok': bool(body.get('ok')), 'error': body.get('error')}
        if op == 'modify':
            code, body = b.trade('/order/modify', ticket=cmd.get('ticket'),
                                 sl=float(cmd.get('sl') or 0), tp=cmd.get('tp'), confirm=1)
            return {'ok': bool(body.get('ok')), 'error': body.get('error')}
        if op == 'take':
            msg = self.executor.send_now(str(cmd.get('id')))
            ok = (self.store.get(str(cmd.get('id'))) or {}).get('stage') in (SENT, FILLED)
            self._emit('manual', f"take signal: {msg}", id=cmd.get('id'))
            return {'ok': ok, 'error': None if ok else msg}
        return {'ok': False, 'error': f'unknown action {op}'}

    def note(self, text: str) -> dict:
        with self.lock:
            n = {'i': self.v, 't': int(self.series.t[self.v]), 'text': str(text)[:500],
                 'at': _wall_ms()}
            self.bar_notes.append(n)
            return n

    def tag_trade(self, tid: str, tag: str = None, note: str = None) -> dict:
        with self.lock:
            cur = self.trade_tags.get(tid, {})
            if tag is not None:
                cur['tag'] = str(tag)[:40]
            if note is not None:
                cur['note'] = str(note)[:500]
            self.trade_tags[tid] = cur
            return cur

    def configure(self, patch: dict) -> None:
        """New settings / mode / name: a new run from the first bar."""
        with self.lock:
            cfg = dict(self.cfg)
            for k in ('mode', 'name', 'overrides', 'notes', 'tags', 'record'):
                if k in patch:
                    cfg[k] = patch[k]
            new = self._normalise(dict(cfg, start=self.cfg['start'], end=self.cfg['end']))
            self.cfg = new
            self.pending = []
            self.reset()

    # -------------------------------------------------------------- output
    def stats(self) -> dict:
        return lab_stats.compute(self.trades, self.balance0)

    def view_payload(self) -> dict:
        with self.lock:
            v = self.v
            fr = self.frames[v - self.i0]
            snap = self.snapshot_at(v)
            return {
                'type': 'frame',
                'cursor': {'v': v, 't': fr['t'], 'k': self.k, 'first': self.i0,
                           'last': self.last, 'frontier_t': int(self.series.t[self.k]),
                           'now': fr['now'], 'at_end': self.at_end()},
                'snapshot': snap, 'signals': fr['signals'], 'positions': fr['positions'],
                'orders': fr['orders'], 'account': fr['account'],
                'ev': fr['ev'], 'tr': fr['tr'],
            }

    def meta(self) -> dict:
        s = self.stats()
        return {
            'id': self.sid, 'name': self.cfg['name'], 'symbol': self.cfg['symbol'],
            'tf': self.cfg['tf'], 'start': self.cfg['start'], 'end': self.cfg['end'],
            'mode': self.cfg['mode'], 'record': self.cfg['record'],
            'created_ms': self.created_ms, 'updated_ms': _wall_ms(),
            'frontier_t': int(self.series.t[self.k]), 'bars_done': self.k - self.i0 + 1,
            'bars_total': self.last - self.i0 + 1, 'engine_rev': ENGINE_REV,
            'spec_source': self.spec.get('source'), 'balance0': self.balance0,
            'summary': lab_stats.summary(s), 'changed': lab_settings.changed_from_live(self.eff),
            'notes': self.cfg['notes'], 'tags': self.cfg['tags'], 'run_no': self.run_no,
            'digits': int(self.spec.get('digits') or 2),
        }

    def session_payload(self) -> dict:
        with self.lock:
            return {
                'type': 'session', 'meta': self.meta(), 'cfg': self.cfg,
                'settings': lab_settings.jsonable(self.eff),
                'bars': self.series.to_payload(), 'first': self.i0,
                'spec': {k: self.spec.get(k) for k in ('digits', 'point', 'tick_size',
                                                       'tick_value', 'contract_size', 'source')},
                'events': self.events, 'trades': self.trades, 'stats': self.stats(),
                'notes': self.bar_notes, 'snaps': self.snaps, 'tags': self.trade_tags,
                'saved': self.saved,
            }

    # ------------------------------------------------------------ storage
    def save(self) -> None:
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            doc = {'meta': self.meta(), 'cfg': self.cfg, 'commands': self.commands,
                   'bar_notes': self.bar_notes, 'snapshots': self.snaps,
                   'trade_tags': self.trade_tags, 'created_ms': self.created_ms,
                   'settings': lab_settings.jsonable(self.eff)}
            _write_json(self.dir / 'session.json', doc)
            _write_json(self.dir / 'trades.json', self.trades)
            _write_json(self.dir / 'events.json', self.events)

    def save_snapshot(self, png: bytes, note: str = '') -> dict:
        with self.lock:
            (self.dir / 'snapshots').mkdir(parents=True, exist_ok=True)
            n = len(self.snaps) + 1
            t = int(self.series.t[self.v])
            name = f"snap-{n:03d}-{dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc):%Y%m%d-%H%M}.png"
            (self.dir / 'snapshots' / name).write_bytes(png)
            meta = {'file': name, 'i': self.v, 't': t, 'note': str(note)[:300], 'at': _wall_ms()}
            self.snaps.append(meta)
            return meta

    @classmethod
    def open(cls, sid: str) -> 'ReplaySession':
        if not valid_sid(sid):
            raise ValueError('bad session id')
        base = RUNS / sid
        doc = json.loads((base / 'session.json').read_text(encoding='utf-8'))
        s = cls(doc['cfg'], sid=sid, created_ms=doc.get('created_ms'))
        s.bar_notes = doc.get('bar_notes') or []
        s.snaps = doc.get('snapshots') or []
        s.trade_tags = doc.get('trade_tags') or {}
        try:
            trades = json.loads((base / 'trades.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            trades = []
        meta = doc.get('meta') or {}
        s.saved = {'frontier_t': meta.get('frontier_t'), 'trades': trades,
                   'summary': meta.get('summary'), 'engine_rev': meta.get('engine_rev'),
                   'commands': doc.get('commands') or []}
        # The first bar ran in the constructor with no actions queued; queue
        # the recorded ones for the re-run to the saved frontier.
        s.pending = [c for c in (doc.get('commands') or []) if int(c.get('i', -1)) > s.k]
        for c in (doc.get('commands') or []):
            if int(c.get('i', -1)) == s.k:
                s._apply(c)
                s.commands.append(c)
        return s

    def resume_target(self) -> int:
        ft = (self.saved or {}).get('frontier_t')
        return self.index_at(ft) if ft else self.k

    def compare_saved(self) -> dict:
        saved = (self.saved or {}).get('trades') or []
        a = [(t.get('id'), round(float(t.get('r') or 0), 3)) for t in saved]
        b = [(t.get('id'), round(float(t.get('r') or 0), 3)) for t in self.trades]
        return {'same': a == b, 'saved_n': len(a), 'now_n': len(b),
                'saved_net': round(sum(float(t.get('profit') or 0) for t in saved), 2),
                'now_net': round(sum(float(t.get('profit') or 0) for t in self.trades), 2),
                'engine_then': (self.saved or {}).get('engine_rev'), 'engine_now': ENGINE_REV}


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, default=_default, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def _default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, tuple):
        return list(o)
    return str(o)


def list_sessions() -> list:
    out = []
    if not RUNS.exists():
        return out
    for p in RUNS.iterdir():
        if not (p.is_dir() and valid_sid(p.name)):
            continue
        try:
            doc = json.loads((p / 'session.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        m = doc.get('meta') or {}
        m['snapshots'] = len(doc.get('snapshots') or [])
        out.append(m)
    return sorted(out, key=lambda m: -(m.get('updated_ms') or 0))


def delete_session(sid: str) -> bool:
    if not valid_sid(sid):
        return False
    path = RUNS / sid
    if path.exists() and path.parent == RUNS:
        shutil.rmtree(path)
        return True
    return False


__all__ = ['ReplaySession', 'list_sessions', 'delete_session', 'valid_sid', 'parse_ms']
