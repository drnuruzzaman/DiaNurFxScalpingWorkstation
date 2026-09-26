"""
server/forecast/shadow.py - shadow live: what a forecast filter WOULD have said, logged, never used.

Phase D left one marginal pass - regime_agree on 5m: better R/trade, total R
and net in 2024-2026, every interval including zero. The ladder's next rung is
SHADOW LIVE: beside every order the live executor sends on 5m, record whether
regime_agree would have vetoed it. Nothing else changes. The verdict never
reaches the executor, the gates, the sizing or the order; this module has no
way to - it only appends to a log. Months of that log, joined to the order
ledger (tools/shadow_report.py), say whether the trades it would have vetoed
really did worse live.

The regime forecast for a live bar, the same numbers the lab would show:

    regime now        the live engine's own closed-bar analysis - its regime
                      label and volatility state (the forecast's reads are
                      reads.regime_read, kept line for line with analyse())
    momentum          (close - close H bars back) / ATR on the closed bars
    table, baseline   the newest lift table and baseline day the offline build
                      holds (runs/forecast/cache/<symbol>/5m/forecast.npz) -
                      a bar after the build uses the build's last ones: stale
                      by the time since the build, never ahead of the bar. Each
                      log row says which month and day it used.

Every call is guarded: an error becomes a log row, never an exception in the
live process. The work runs on one daemon thread, off the executor's path.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import numpy as np

from . import CACHE_DIR, HORIZON
from . import cond_model as cm
from . import rules

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / 'order_ledger' / 'logs' / 'shadow_forecast.jsonl'
FILTER = 'regime_agree'
TFS = ('5m',)
STATES = ('UPTREND', 'DOWNTREND', 'RANGE', 'TRANSITION', 'SQUEEZE')
VOLS = ('very_low', 'low', 'normal', 'high', 'extreme')
DAY_MS = 86_400_000

_TABLES: dict = {}
_MOM: dict = {}                     # (symbol, tf) -> {'bar_time_ms', 'mom_h'}
_Q: queue.Queue = queue.Queue(maxsize=1000)
_WORKER = {'thread': None}
_LOCK = threading.Lock()


def _tables(symbol: str, tf: str) -> dict | None:
    """Only the regime arrays of the offline store - a few hundred KB, loaded once."""
    key = (symbol, tf)
    d = CACHE_DIR / symbol / tf
    npz, meta = d / 'forecast.npz', d / 'forecast.json'
    if not (npz.exists() and meta.exists()):
        return None
    hit = _TABLES.get(key)
    if hit is not None and hit['built_mtime'] == int(npz.stat().st_mtime):
        return hit                      # reloaded when the monthly update rebuilds the store
    with np.load(npz) as z:
        t = {k: z[k] for k in ('st_lift', 'st_neff', 'c_months', 'b_state', 'd0')}
    info = json.loads(meta.read_text(encoding='utf-8'))
    t['last_ms'] = int(info.get('last_ms') or 0)
    t['built_mtime'] = int(npz.stat().st_mtime)
    _TABLES[key] = t
    return t


def momentum(closes, atr: float, tf: str) -> float | None:
    """(close - close H bars back) / ATR, as features.assemble computes mom_h."""
    H = HORIZON[tf]
    c = np.asarray(closes, dtype=np.float64)
    if c.size <= H or not atr > 0:
        return None
    return float((c[-1] - c[-1 - H]) / atr)


def regime_forecast(snap: dict, mom_h: float | None, tf: str, symbol: str) -> dict | None:
    """The regime distribution at the horizon for a closed-bar snapshot - the store's lookup."""
    t = _tables(symbol, tf)
    if t is None or not snap or not snap.get('ok'):
        return None
    rg = snap.get('regime') or {}
    state = STATES.index(rg['label']) if rg.get('state') != 'unknown' and rg.get('label') in STATES \
        else -1
    vol = (rg.get('volatility') or {}).get('state')
    P = {'regime': cm._regime([state]), 'mom': cm._mom([np.nan if mom_h is None else mom_h]),
         'vol': cm._vol([VOLS.index(vol) if vol in VOLS else -1])}
    cell = int(cm.cells('state', P)[0])
    close_ms = int(snap.get('bar_time_ms') or 0) + int(tf_ms(tf))
    m = int(np.clip(np.searchsorted(t['c_months'], close_ms, 'right') - 1, 0,
                    t['c_months'].size - 1))
    d = int(np.clip((close_ms - int(t['d0'])) // DAY_MS, 0, t['b_state'].shape[0] - 1))
    base = t['b_state'][d].astype(np.float64)
    p = cm._apply(base, t['st_lift'][m, cell].astype(np.float64))
    return {'p': [round(float(x), 4) for x in p], 'base': [round(float(x), 4) for x in base],
            'cell': cell, 'why': cm.describe('state', cell), 'neff': float(t['st_neff'][m, cell]),
            'table_month_ms': int(t['c_months'][m]), 'baseline_day': d,
            'after_build': close_ms > t['last_ms']}


def tf_ms(tf: str) -> int:
    from ..config import TF_SECONDS
    return TF_SECONDS.get(tf, 300) * 1000


# --------------------------------------------------------------------------- #
# the live hooks - both return at once and never raise                        #
# --------------------------------------------------------------------------- #
def remember_closed(symbol: str, tf: str, closed, snap: dict) -> None:
    """At each bar close: momentum on the closed bars, for the send that may follow."""
    try:
        if tf not in TFS or not snap or not snap.get('ok'):
            return
        mom = momentum(closed.c[-(HORIZON[tf] + 1):], float(snap.get('atr') or 0), tf)
        with _LOCK:
            _MOM[(symbol, tf)] = {'bar_time_ms': int(snap.get('bar_time_ms') or 0), 'mom_h': mom}
    except Exception:                                          # noqa: BLE001
        pass


def note_send(rec: dict, snap: dict | None) -> None:
    """The executor sent an order: queue what regime_agree would have said about it."""
    try:
        if rec.get('tf') not in TFS:
            return
        with _LOCK:
            mom = dict(_MOM.get((rec.get('symbol'), rec.get('tf'))) or {})
        _Q.put_nowait({'rec': {k: rec.get(k) for k in ('id', 'symbol', 'tf', 'side', 'playbook',
                                                       'final_bar_ms')},
                       'snap': {k: (snap or {}).get(k) for k in ('ok', 'bar_time_ms', 'atr', 'regime')},
                       'mom': mom, 'at_ms': int(time.time() * 1000)})
        _ensure_worker()
    except Exception:                                          # noqa: BLE001
        pass


def _ensure_worker() -> None:
    t = _WORKER['thread']
    if t is None or not t.is_alive():
        t = threading.Thread(target=_work, name='shadow-forecast', daemon=True)
        _WORKER['thread'] = t
        t.start()


def _work() -> None:
    while True:
        job = _Q.get()
        try:
            row = evaluate(job)
        except Exception as exc:                               # noqa: BLE001
            row = {'at_ms': job.get('at_ms'), 'id': (job.get('rec') or {}).get('id'),
                   'error': f'{type(exc).__name__}: {exc}'}
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(row, default=str) + '\n')
        except OSError:
            pass


def evaluate(job: dict) -> dict:
    """One log row: the send, the forecast behind the verdict, the verdict."""
    rec, snap, mom = job['rec'], job['snap'], job.get('mom') or {}
    row = {'at_ms': job.get('at_ms'), 'id': rec.get('id'), 'symbol': rec.get('symbol'),
           'tf': rec.get('tf'), 'side': rec.get('side'), 'playbook': rec.get('playbook'),
           'final_bar_ms': rec.get('final_bar_ms'), 'filter': FILTER,
           'judged_bar_ms': snap.get('bar_time_ms')}
    if not snap.get('ok'):
        return dict(row, verdict='abstain', why='no closed-bar analysis at send time')
    if mom.get('bar_time_ms') != snap.get('bar_time_ms'):
        return dict(row, verdict='abstain', why='momentum was not taken on the judged bar')
    fc = regime_forecast(snap, mom.get('mom_h'), rec['tf'], rec['symbol'])
    if fc is None:
        return dict(row, verdict='abstain', why='no offline forecast store for this timeframe')
    why = rules.regime_agree(fc['p'], rec['side'])
    return dict(row, verdict='veto' if why else 'pass', why=why, mom_h=mom.get('mom_h'),
                regime_now=(snap.get('regime') or {}).get('label'), forecast=fc)


__all__ = ['LOG', 'FILTER', 'TFS', 'momentum', 'regime_forecast', 'remember_closed',
           'note_send', 'evaluate']
