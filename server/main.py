"""
server/main.py - the DiaNurFx analysis and signal service.

Binds 127.0.0.1 only. It reads from the MT5 bridge and from disk, runs the
engine, and serves the workstation. It holds no credentials: anything touching
the terminal goes through bridge/mt5_bridge.py.

Route map:
    GET  /api/health              service + bridge state
    GET  /api/bars                raw OHLCV
    GET  /api/analyse             snapshot + qualified signals
    GET  /api/narrative           the agent's standing market read
    GET  /api/brief               all four agent modes for one signal
    POST /api/ask                 free-text question (deterministic, LLM optional)
    GET  /api/history             a date range, for the backtest chart
    GET  /api/replay              analyse AS OF a past timestamp
    POST /api/backtest/start      queue a run
    GET  /api/backtest/status     progress
    GET  /api/backtest/result     the finished run
    GET  /api/backtest/list       saved runs
    GET/POST /api/settings        risk and gate configuration
    POST /api/order/send          EA execution - OFF unless explicitly enabled
    WS   /ws/live                 snapshot stream
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import CONFIG, MTF_LADDER, RUN_DIR, TF_SECONDS, TIMEFRAMES
from . import news as news_mod
from .datafeed import (BRIDGE, FEED, available_symbols, available_timeframes,
                       load_disk)
from .engine import backtest as bt
from .engine import narrator
from .engine.analysis import analyse, build_mtf
from .engine.qualify import qualify_all
from .engine.signals import generate
from . import alerts as alerts_mod
from .executor import Executor
from .order_ledger import OrderLedger
from .signal_store import SignalStore, FINAL
from .telegram_commands import TelegramCommands, private_destinations

VERSION = '1.0.0'

app = FastAPI(title='DiaNurFx', version=VERSION, docs_url='/api/docs')

# Same-origin in production; these exist for the Vite dev server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://127.0.0.1:5180', 'http://localhost:5180'],
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)


# --------------------------------------------------------------------------- #
# shared state                                                                #
# --------------------------------------------------------------------------- #
WATCHLIST_FILE = Path(__file__).resolve().parent.parent / 'configs' / 'watchlist.json'


def load_watchlist() -> list:
    """The saved watchlist, or just the configured symbol before one exists."""
    try:
        data = json.loads(WATCHLIST_FILE.read_text(encoding='utf-8'))
        names = [str(x) for x in (data.get('symbols') or []) if str(x).strip()]
        if names:
            return names
    except (OSError, ValueError, AttributeError):
        pass
    return [CONFIG.symbol]


def save_watchlist(names: list) -> None:
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_FILE.write_text(
        json.dumps({'symbols': names, 'updated_ms': int(time.time() * 1000)},
                   indent=2), encoding='utf-8')


def watched(symbol: str) -> bool:
    return symbol in STATE.watchlist


class State:
    """Process-wide caches and the live trading context."""

    def __init__(self):
        self.snapshots: dict = {}          # (symbol, tf) -> snapshot
        # The same analysis run WITHOUT the forming bar, kept separately.
        #
        # Detection is already closed-bar only (see signal_store.py), but
        # every later judgement - re-qualifying before sending, and now
        # re-qualifying a pending order - read `snapshots`, which includes
        # the bar currently being built. So a signal found on closed data
        # could be sent or cancelled on a half-formed candle whose high, low
        # and close all still move. These two dicts keep the distinction the
        # rest of the system already relies on: `snapshots` is what the chart
        # shows, `closed` is what decisions are made on.
        #
        # Refreshed once per bar, which is not a cache tradeoff but the
        # definition: closed-bar facts do not change between closes.
        self.closed: dict = {}             # (symbol, tf) -> snapshot, no forming bar
        self.signals: dict = {}            # (symbol, tf) -> [Signal]
        self.signal_index: dict = {}       # id -> Signal
        self.backtests: dict = {}          # run_id -> {status, progress, result}
        self.last_stats: dict = {}         # symbol -> latest backtest summary
        self.trades_today = 0
        self.daily_pnl_pct = 0.0
        self.lock = asyncio.Lock()
        # The multi-timeframe signal board: (symbol, tf) -> a compact row.
        # Refreshed by board_worker() on a per-timeframe cadence, never inside
        # the websocket tick - a full sweep takes over a second and would stall
        # the live chart if it ran there.
        self.board: dict = {}
        self.board_workers: dict = {}    # symbol -> asyncio.Task
        # When a chart last asked for each symbol's board. A worker for a
        # symbol that is neither watched nor on screen is stopped after a
        # while instead of scanning forever.
        self.board_seen: dict = {}       # symbol -> epoch seconds
        # The instruments signals are generated for. Pushed by the UI, kept on
        # disk so alerts keep flowing when no browser is open.
        self.watchlist: list = load_watchlist()

    def context(self) -> dict:
        """What qualify.py needs that the chart cannot know."""
        acct = BRIDGE.account() or {}
        positions = BRIDGE.positions() or []
        equity = float(acct.get('equity') or CONFIG.risk.equity)
        quote = None
        try:
            q = BRIDGE.quotes([CONFIG.symbol]) or {}
            q = q.get('quotes', q) if isinstance(q, dict) else {}
            # Same envelope bug as FEED.quote: the spread gate was reading a
            # dict with no bid/ask and silently falling back to the bar's
            # recorded spread instead of the live one.
            quote = q.get(CONFIG.symbol) or next(iter(q.values()), None)
        except Exception:                                 # noqa: BLE001
            quote = None
        spread_points = None
        if quote and quote.get('ask') and quote.get('bid'):
            point = float(quote.get('point') or 0.01)
            spread_points = round((quote['ask'] - quote['bid']) / point, 1)
        return {
            'equity': equity,
            'open_positions': len(positions) if isinstance(positions, list) else 0,
            'trades_today': self.trades_today,
            'daily_pnl_pct': self.daily_pnl_pct,
            'spread_points': spread_points,
            'risk_per_trade_pct': CONFIG.risk.risk_per_trade_pct,
            # None unless macro alerts are switched on; qualify.py treats None
            # as "no calendar", not as "all clear".
            'minutes_to_high_impact': minutes_to_high_impact(),
        }


STATE = State()

# Named for what it holds rather than when it is written. `RUN` sat one letter
# from `RUN_DIR` (runs/, the backtest outputs) and the two were easy to swap by
# eye - one is the live audit trail, the other is disposable analysis.
LEDGER_DIR = Path(__file__).resolve().parent.parent / 'order_ledger'
STORE = SignalStore(LEDGER_DIR / 'signal_store.json')
LEDGER = OrderLedger(LEDGER_DIR / 'order_ledger.json')


def _run_engine(symbol: str, tf: str, live: bool = True, bars: int = None,
                context: dict = None):
    """
    analyse -> generate -> qualify. The one path everything shares.

    `context` can be supplied by a caller that is running the engine across
    several timeframes. Building it costs three bridge round-trips (account,
    positions, quotes), and those answers are identical for every timeframe in
    the same sweep - recomputing them nine times was most of the cost of a full
    board scan.
    """
    count = bars or CONFIG.engine.bars_for_analysis
    series = FEED.bars(symbol, tf, count, live=live)
    if len(series) < 60:
        # Distinguish a symbol MT5 is still fetching from one that does not
        # exist. Both used to read as "no data", which is only true of one.
        if series.status == 'unknown_symbol':
            reason = f'{symbol} is not a symbol this broker offers'
        elif len(series) == 0:
            reason = (f'MetaTrader is downloading {symbol} {tf} history - '
                      f'it only fetches what it has been asked for')
        else:
            reason = (f'MetaTrader has {len(series)} of the {count} bars needed '
                      f'for {symbol} {tf}, still downloading')
        return series, {'ok': False, 'reason': reason, 'status': series.status,
                        'warming': series.status in ('warming', 'unavailable'),
                        'have': len(series), 'need': 60,
                        'symbol': symbol, 'tf': tf}, []
    spec = FEED.spec(symbol)
    mtf = build_mtf(FEED, symbol, tf, live=live)
    snap = analyse(series, mtf, spec)
    if not snap.get('ok'):
        return series, snap, []
    # Signals exist only for watchlist symbols. Anything else still gets its
    # full analysis - levels, patterns, structure on the chart - but no ideas
    # are generated, so nothing off the watchlist reaches the board or Telegram.
    if live and not watched(symbol):
        detected = []
    elif live:
        # FORMING: what the open bar shows right now. Display only.
        STORE.set_forming(symbol, tf, generate(snap, series))
        # FINAL: decided on CLOSED bars, once per bar - re-run the engine
        # without the forming bar and lock whatever it finds. That is exactly
        # the information a backtest acts on. See signal_store.py.
        if STORE.bar_closed(symbol, tf, int(series.t[-1])) and len(series) > 61:
            closed = series.slice(0, len(series) - 1)
            snap_c = analyse(closed, mtf, spec)
            if snap_c.get('ok'):
                STATE.closed[(symbol, tf)] = snap_c
                STORE.finalize(symbol, tf, generate(snap_c, closed), spec,
                               int(closed.t[-1]), int(TF_SECONDS.get(tf, 300) * 1000))
        detected = STORE.view(symbol, tf)
    else:
        detected = generate(snap, series)
    sigs = qualify_all(detected, snap, spec,
                       context if context is not None else STATE.context())
    STATE.snapshots[(symbol, tf)] = snap
    STATE.signals[(symbol, tf)] = sigs
    if live:
        STORE.note_qualification(sigs)
    for s in sigs:
        STATE.signal_index[s.id] = (s, snap)
    return series, snap, sigs


# --------------------------------------------------------------------------- #
# the multi-timeframe signal board                                            #
# --------------------------------------------------------------------------- #
# Which timeframes the board watches. 2h is omitted deliberately: it carries
# almost no information 1h and 4h do not already give, and every extra
# timeframe is a full engine pass.
BOARD_TFS = ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d']


def account_currency() -> str:
    acct = BRIDGE.account() or {}
    return str(acct.get('currency') or '').strip()


# The /profit command lives in server/telegram_commands.py.
#
# It used to live here TOO - an inline asyncio worker with its own copy of the
# long poll, the authorisation check and the reply text - and both were
# started. Telegram allows exactly ONE getUpdates consumer per bot, so the two
# of them fought: one won, the other logged a 409 every sixty seconds forever
# and /profit ran on whichever happened to win the race at startup. The
# dedicated module is the one kept - it persists its update offset across
# restarts and refuses to answer a command older than two minutes, neither of
# which the inline copy did.


async def news_alert_worker() -> None:
    """
    Fire a Telegram alert as each release enters its lead window.

    Runs on its own slow clock rather than off the signal stream: a release is
    scheduled, so there is nothing to react to and no reason to check it at the
    cadence of a price tick. Every gate lives in alerts.should_alert_news, so
    this loop only has to keep offering candidates.
    """
    while True:
        try:
            cfg = alerts_mod.load()
            news_cfg = cfg.get('news') or {}
            if news_cfg.get('enabled'):
                news_mod.refresh(BRIDGE.calendar())
                lead_ms = float(news_cfg.get('lead_minutes', 10)) * 60_000
                now_ms = int(time.time() * 1000)
                # Look a little either side of the window: the loop ticks every
                # 30s, so an event can cross the boundary between two passes.
                rows = news_mod.window(now_ms - lead_ms, now_ms + lead_ms,
                                       news_cfg.get('impact', 'high'),
                                       time_known_only=True)
                for ev in rows:
                    verdict = alerts_mod.should_alert_news(cfg, ev, now_ms)
                    if verdict.send:
                        await asyncio.to_thread(alerts_mod.dispatch_news, cfg, ev)
        except Exception as exc:                              # noqa: BLE001
            print(f'! news alert worker: {type(exc).__name__}: {exc}')
        await asyncio.sleep(30)


def board_ttl(tf: str) -> float:
    """
    How long a timeframe's board row stays fresh.

    Scaled to the bar, because a 4h setup does not change every second and
    re-running it as if it might is pure waste. A fifth of the bar length keeps
    a row at most ~20% of a bar stale, clamped so 1m does not thrash and 1d
    does not go stale for hours.
    """
    return max(6.0, min(150.0, TF_SECONDS.get(tf, 300) / 5.0))


def scan_tf(symbol: str, tf: str, context: dict) -> dict:
    """One board row: the engine's verdict for this symbol on this timeframe."""
    try:
        series, snap, sigs = _run_engine(symbol, tf, live=True, context=context)
    except Exception as exc:                              # noqa: BLE001
        return {'symbol': symbol, 'tf': tf, 'ok': False,
                'error': f'{type(exc).__name__}: {exc}',
                'scanned_ms': int(time.time() * 1000), 'signals': []}

    if not snap.get('ok'):
        return {'symbol': symbol, 'tf': tf, 'ok': False,
                'error': snap.get('reason'),
                'scanned_ms': int(time.time() * 1000), 'signals': []}

    regime = snap.get('regime') or {}
    trend = snap.get('trend') or {}
    return {
        'symbol': symbol, 'tf': tf, 'ok': True,
        'scanned_ms': int(time.time() * 1000),
        'bar_time_ms': snap.get('bar_time_ms'),
        'price': snap.get('price'),
        'atr_points': snap.get('atr_points'),
        'regime': regime.get('label'),
        'regime_state': regime.get('state'),
        'regime_confidence': regime.get('confidence'),
        'trend': trend.get('state'),
        'trend_strength': trend.get('strength'),
        'mtf_score': (snap.get('mtf') or {}).get('score'),
        'momentum': (snap.get('momentum') or {}).get('total'),
        # Conflicted entries are the losing half of a two-sided bar. They are
        # not tradeable and would double every row, so the board omits them.
        'signals': [s.to_dict() for s in sigs if s.status != 'conflicted'],
    }


# --------------------------------------------------------------------------- #
# higher-timeframe trendlines, projected onto a lower timeframe               #
# --------------------------------------------------------------------------- #
# A trendline is stored in BAR INDEX space, which only means something on the
# timeframe that produced it: bar 480 of the 4h series and bar 480 of the 5m
# series are weeks apart. Time is the only coordinate the two share, so a line
# crossing timeframes has to be re-expressed as a slope per MILLISECOND before
# a lower timeframe can draw it.
#
# Nothing here runs the engine. The board worker has already analysed every
# timeframe in BOARD_TFS and cached the snapshot, so projection is a read.

MTF_MAX_PER_TF = 4          # per source timeframe, best first
MTF_MIN_SCORE = 35          # below this a line is noise not worth the clutter


def _tf_rank(tf: str) -> int:
    return TF_SECONDS.get(tf, 0)


def project_trendline(tl: dict, tf: str, last_ms: int) -> dict | None:
    """
    One trendline, re-expressed in time.

    The slope is fixed by the line's FIRST ANCHOR and its value at the source
    timeframe's latest bar, rather than by converting price-per-bar through a
    nominal bar duration.

    Bar spacing is not uniform in real time - weekends and session breaks
    collapse to nothing in bar space - so any nominal conversion drifts further
    off with every gap it crosses. Measured against the source's own
    `price_now`, that error reached $4.41 on a 30m line, about 10 points of
    gold, which is enough to matter if a stop is being placed off it.
    Anchoring on (origin, now) instead makes the projection exact at both ends
    by construction and leaves any residual error in the middle, where nobody
    is reading a price off it.
    """
    t1, t2 = tl.get('t1'), tl.get('t2')
    y1, y2 = tl.get('y1'), tl.get('y2')
    now_price = tl.get('price_now')
    if t1 is None or t2 is None or y1 is None or y2 is None:
        return None
    if int(t2) - int(t1) <= 0:
        return None                      # degenerate: both anchors on one bar

    # Prefer the (origin -> now) span. Fall back to the anchor pair when the
    # snapshot is missing price_now or its last bar is not after the anchor.
    span_ms = int(last_ms) - int(t1) if last_ms else 0
    if now_price is not None and span_ms > 0:
        slope_ms = (float(now_price) - float(y1)) / span_ms
    else:
        slope_ms = (float(y2) - float(y1)) / (int(t2) - int(t1))

    return {
        'tf': tf,
        'tf_seconds': _tf_rank(tf),
        'kind': tl.get('kind'),
        't1': int(t1), 'y1': float(y1),
        't2': int(t2), 'y2': float(y2),
        # price per millisecond; the chart multiplies this by elapsed time
        'slope_ms': slope_ms,
        # The source's own latest bar and the line's value there. The chart
        # draws from (t1, y1) through this point, so a projected line sits
        # exactly where the higher timeframe says it sits right now.
        'last_ms': int(last_ms) if last_ms else None,
        'score': int(tl.get('score') or 0),
        'touches': int(tl.get('touches') or 0),
        'broken': bool(tl.get('broken')),
        'price_now': tl.get('price_now'),
    }


def mtf_trendlines(symbol: str, chart_tf: str) -> list:
    """
    Every cached trendline from a timeframe ABOVE `chart_tf`, in time space.

    Sources are not filtered to a preset ladder here. The client owns that
    choice and can change it without a round trip, and sending the full set
    costs a few kB of an already-cached read.
    """
    floor = _tf_rank(chart_tf)
    out = []
    for tf in BOARD_TFS:
        if _tf_rank(tf) <= floor:
            continue                     # same frame or finer: not a projection
        snap = STATE.snapshots.get((symbol, tf))
        if not snap or not snap.get('ok'):
            continue
        lines = sorted(snap.get('trendlines') or [],
                       key=lambda x: -(x.get('score') or 0))
        kept = 0
        for tl in lines:
            if (tl.get('score') or 0) < MTF_MIN_SCORE:
                continue
            row = project_trendline(tl, tf, snap.get('bar_time_ms'))
            if row is None:
                continue
            out.append(row)
            kept += 1
            if kept >= MTF_MAX_PER_TF:
                break
    # Coarsest first, so the chart draws the big structure underneath.
    out.sort(key=lambda r: (-r['tf_seconds'], -r['score']))
    return out


@app.get('/api/mtf/trendlines')
def mtf_trendlines_route(symbol: str = None, tf: str = '5m'):
    """The projection a chart on `tf` would draw, plus which sources exist."""
    sym = symbol or CONFIG.symbol
    rows = mtf_trendlines(sym, tf)
    return {
        'symbol': sym,
        'tf': tf,
        'trendlines': rows,
        # Everything above this chart's frame that the board actually holds -
        # what the source picker should offer rather than a hardcoded list.
        'sources': [x for x in BOARD_TFS if _tf_rank(x) > _tf_rank(tf)],
        'ladder': [x for x in MTF_LADDER.get(tf, []) if _tf_rank(x) > _tf_rank(tf)],
        'server_ms': int(time.time() * 1000),
    }


def board_rows(symbol: str) -> list:
    """Every cached row for this symbol, ordered fine timeframe first."""
    rows = []
    for tf in BOARD_TFS:
        row = STATE.board.get((symbol, tf))
        if row:
            rows.append(row)
    rows.sort(key=lambda r: TF_SECONDS.get(r['tf'], 0))
    return rows


def board_payload(symbol: str) -> dict:
    """
    The board: signals for EVERY watchlist symbol, plus this symbol's rows.

    The rows (one per timeframe, used by the TF matrix) stay per-symbol - they
    describe the chart you are looking at. The signal list is the whole
    watchlist, because the board is where you find out that something is
    happening on an instrument you are NOT looking at.
    """
    rows = board_rows(symbol)
    flat = []
    for sym in STATE.watchlist:
        for row in board_rows(sym):
            for sig in row['signals']:
                flat.append({**sig, 'tf_seconds': TF_SECONDS.get(row['tf'], 0),
                             'scanned_ms': row['scanned_ms'],
                             'regime': row.get('regime'),
                             'regime_state': row.get('regime_state')})
    # Sorted by timeframe, then instrument, then how actionable it is.
    status_rank = {'qualified': 0, 'watch': 1, 'rejected': 2}
    flat.sort(key=lambda s: (s['tf_seconds'], s.get('symbol', ''),
                             status_rank.get(s.get('status'), 3),
                             -s.get('confidence', 0)))
    return {
        'symbol': symbol,
        'watchlist': list(STATE.watchlist),
        'timeframes': BOARD_TFS,
        'rows': rows,
        'signals': flat,
        'qualified': sum(1 for s in flat if s.get('status') == 'qualified'),
        'watch': sum(1 for s in flat if s.get('status') == 'watch'),
        'scanned_ms': max((r['scanned_ms'] for r in rows), default=0),
        'complete': len(rows) == len(BOARD_TFS),
    }


async def board_worker(symbol: str):
    """
    Keep the board fresh, one timeframe at a time.

    Refreshing a single timeframe per pass rather than sweeping all eight keeps
    each await short, so the websocket tick never waits behind a full scan.
    """
    while True:
        try:
            now = time.time()
            stale = [tf for tf in BOARD_TFS
                     if now - (STATE.board.get((symbol, tf), {}).get('scanned_ms', 0) / 1000.0)
                     >= board_ttl(tf)]
            if stale:
                # Oldest first, so nothing starves while 1m refreshes constantly.
                stale.sort(key=lambda tf: STATE.board.get((symbol, tf), {}).get('scanned_ms', 0))
                tf = stale[0]
                context = await asyncio.to_thread(STATE.context)
                row = await asyncio.to_thread(scan_tf, symbol, tf, context)
                STATE.board[(symbol, tf)] = row
                await asyncio.to_thread(fire_alerts, row)
            await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            raise
        except Exception:                                 # noqa: BLE001
            # The board is a convenience. It must never take the service down.
            await asyncio.sleep(2.0)


def ensure_board(symbol: str):
    """
    Start the board worker for this symbol, once.

    Keyed per symbol rather than by a single flag: with one global boolean, a
    switch to a second instrument left the worker scanning the first one and
    the new symbol's board silently never populated.
    """
    STATE.board_seen[symbol] = time.time()
    task = STATE.board_workers.get(symbol)
    if task is not None and not task.done():
        return
    STATE.board_workers[symbol] = asyncio.create_task(board_worker(symbol))


# A worker for a symbol nobody watches or views is stopped after this long.
BOARD_IDLE_S = 600


async def board_supervisor() -> None:
    """
    Keep a board worker running for every watchlist symbol, and only those.

    Watchlist symbols are scanned whether or not a browser is open - that is
    what lets a signal on an instrument you are not looking at still reach
    Telegram. A symbol that is on screen but not watched keeps a worker while
    it is being viewed (the chart's higher-timeframe lines read from it) and
    loses it once nobody has asked for ten minutes.
    """
    while True:
        try:
            now = time.time()
            for sym in list(STATE.watchlist):
                task = STATE.board_workers.get(sym)
                if task is None or task.done():
                    STATE.board_workers[sym] = asyncio.create_task(board_worker(sym))
            for sym, task in list(STATE.board_workers.items()):
                if watched(sym):
                    continue
                if now - STATE.board_seen.get(sym, 0) > BOARD_IDLE_S:
                    task.cancel()
                    STATE.board_workers.pop(sym, None)
        except Exception as exc:                          # noqa: BLE001
            print(f'! board supervisor: {type(exc).__name__}: {exc}')
        await asyncio.sleep(15)


@app.get('/api/signals/board')
async def signals_board(symbol: str = Query(None), refresh: int = 0):
    """
    Signals across every watched timeframe, ordered by timeframe.

    `refresh=1` forces a synchronous full sweep instead of returning the cache -
    useful on first load, and it is the only path that blocks.
    """
    symbol = symbol or CONFIG.symbol
    ensure_board(symbol)
    if refresh or not STATE.board:
        context = await asyncio.to_thread(STATE.context)
        for tf in BOARD_TFS:
            STATE.board[(symbol, tf)] = await asyncio.to_thread(
                scan_tf, symbol, tf, context)
    return board_payload(symbol)


def fire_alerts(row: dict) -> None:
    """
    Alert on anything in this board row that qualifies.

    Driven off the board rather than the chart because the board is the only
    place that sees every watched timeframe - alerting from the live socket
    would only ever fire for the timeframe you happen to be looking at.
    """
    if not row.get('ok'):
        return
    try:
        cfg = alerts_mod.load()
        if not cfg.get('enabled'):
            return
        for sig in row.get('signals', []):
            # FINAL only: a FORMING signal can vanish when its bar closes, and
            # an alert for something that never existed is worse than none.
            if sig.get('stage') != FINAL:
                continue
            # Hand every signal to dispatch() and let IT gate and log.
            #
            # This used to pre-check with should_alert() and `continue` on a
            # false verdict, which meant dispatch - the only thing that writes
            # the log - was never reached for a suppressed signal. Every
            # "why didn't I get an alert" answer was silently discarded,
            # including the operational ones like a missing token.
            verdict = alerts_mod.should_alert(cfg, sig)
            snap = challenge = None
            if verdict.send:
                snap = STATE.snapshots.get((sig['symbol'], sig['tf']))
                if cfg.get('include_challenge') and snap:
                    entry = STATE.signal_index.get(sig['id'])
                    if entry:
                        challenge = narrator.challenge_signal(
                            entry[0], snap, STATE.last_stats.get(sig['symbol']))
            alerts_mod.dispatch(cfg, sig, snap, challenge)
    except Exception as exc:                              # noqa: BLE001
        print(f'! alert dispatch failed: {type(exc).__name__}: {exc}')


# --------------------------------------------------------------------------- #
# alerts                                                                      #
# --------------------------------------------------------------------------- #
@app.get('/api/alerts/config')
def alerts_config():
    cfg = alerts_mod.load()
    symbols = sorted(set(available_symbols()) | set(_bridge_symbols()) |
                     {c['symbol'] for c in cfg.get('watch', [])})
    return {
        'config': cfg,
        'status': alerts_mod.status(cfg),
        'symbols': symbols,
        'timeframes': BOARD_TFS,
        'matrix': alerts_mod.matrix(cfg, symbols, BOARD_TFS),
        'has_data': {s: bool(available_timeframes(s)) for s in symbols},
        'kinds': alerts_mod.KINDS,
        'bots': alerts_mod.available_bots(),
        'bot_info': {b: alerts_mod.describe_bot(None if b == 'default' else b)
                     for b in alerts_mod.available_bots()},
        # Instruments already on the watch list lead, so the cards the user
        # actually configured are not buried under sixteen they never chose.
        'watched_symbols': sorted({c['symbol'] for c in cfg.get('watch', [])}),
    }


def _bridge_symbols() -> list:
    """Instruments the terminal can quote, so the matrix is not limited to disk."""
    try:
        payload = BRIDGE._get('/symbols')
    except Exception:                                     # noqa: BLE001
        return []
    if not payload:
        return []
    rows = payload if isinstance(payload, list) else payload.get('symbols', [])
    out = []
    for r in rows:
        name = r if isinstance(r, str) else (r or {}).get('name')
        if name:
            out.append(name)
    return out


@app.post('/api/alerts/config')
def alerts_save(body: dict):
    cfg = alerts_mod.load()
    for key in ('enabled', 'min_confidence', 'statuses', 'cooldown_minutes',
                'quiet_hours_utc', 'include_challenge', 'destinations',
                'watch', 'news'):
        if key in body:
            cfg[key] = body[key]
    cfg = alerts_mod.save(cfg)
    return {'ok': True, 'config': cfg, 'status': alerts_mod.status(cfg)}


class WatchBody(BaseModel):
    symbol: str
    tf: str
    enabled: bool


@app.post('/api/alerts/watch')
def alerts_watch(body: WatchBody):
    """Toggle one cell of the instrument x timeframe grid."""
    cfg = alerts_mod.load()
    alerts_mod.set_watch(cfg, body.symbol, body.tf, body.enabled)
    cfg = alerts_mod.save(cfg)
    return {'ok': True, 'config': cfg, 'status': alerts_mod.status(cfg)}


class TestBody(BaseModel):
    target: str | None = None
    bot: str | None = None


@app.post('/api/alerts/test')
def alerts_test(body: TestBody):
    """
    Send a sample alert so a destination can be verified before it matters.

    Uses a clearly-labelled fake signal: a test that sends a real one would
    teach readers to act on a message that was never meant to be traded.
    """
    cfg = alerts_mod.load()
    if not alerts_mod.telegram_ready():
        raise HTTPException(400, 'TELEGRAM_BOT_TOKEN is not set in the '
                                 'environment. Nothing was sent.')
    sample = {
        'id': f'test:{int(time.time())}', 'symbol': CONFIG.symbol, 'tf': '5m',
        'side': 'buy', 'status': 'qualified', 'confidence': 72,
        'label': 'TEST MESSAGE — NOT A SIGNAL',
        'entry': 0.0, 'stop': 0.0, 'tp1': 0.0, 'tp2': 0.0, 'rr1': 0.0, 'rr2': 0.0,
        'evidence': [{'text': 'this is a configuration test from DiaNurFx',
                      'weight': 1, 'kind': 'test'}],
        'against': ['do not trade this - the prices above are zeros'],
        'invalidation': 'n/a, this is a test', 'sizing': {},
    }
    if body.target:
        text = alerts_mod.format_signal(sample)
        res = alerts_mod.send_telegram(body.target, text, bot=body.bot)
        return {'ok': res.get('ok'), 'results': [res]}
    return alerts_mod.dispatch(cfg, sample, force=True)


IMPACT_RANK = {'holiday': 0, 'low': 1, 'medium': 2, 'high': 3}


@app.get('/api/news')
def news(impact: str = 'high', within_hours: int = 24):
    """
    Upcoming macro releases from the bridge's calendar, filtered by impact.

    Also reports minutes_to_next, which is what the news gate in qualify.py
    consumes - so the blackout the UI shows and the blackout that actually
    vetoes a signal are the same number.
    """
    now_ms = int(time.time() * 1000)
    news_mod.refresh(BRIDGE.calendar())
    horizon = now_ms + within_hours * 3_600_000
    # time_known_only: the gate counts MINUTES to a release, and a date-only
    # FRED row has no minute. Letting one through would blackout trading from
    # midnight on a day something is merely scheduled.
    rows = news_mod.window(now_ms - 3_600_000, horizon, impact,
                           time_known_only=True)
    upcoming = [{**e, 'minutes_away': round((e['ts'] - now_ms) / 60_000, 1)}
                for e in rows]

    ahead = [e for e in upcoming if e['minutes_away'] >= 0]
    return {
        'source': 'merged: ' + ', '.join(
            k for k, on in news_mod.sources_status().items() if on),
        'sources': news_mod.sources_status(),
        'impact_floor': impact,
        'total_events': len(upcoming),
        'events': upcoming[:40],
        'next': ahead[0] if ahead else None,
        'minutes_to_next': ahead[0]['minutes_away'] if ahead else None,
        'fetched_ms': now_ms,
    }


# The news gate's input, cached hard.
#
# This used to call news() -> BRIDGE.calendar() directly. STATE.context() runs
# on every engine pass AND every board scan, so that was an external HTTP fetch
# several times a second: it wedged the bridge completely (ConnectionAborted,
# process alive and answering nothing) and stalled the board worker so no alert
# could fire. A macro calendar changes on the order of hours; fetching it more
# than once every few minutes buys nothing and cost everything.
_NEWS_CACHE = {'at': 0.0, 'minutes': None, 'refreshing': False}
_NEWS_TTL = 300.0          # 5 minutes
_NEWS_LOCK = threading.Lock()


def _refresh_news_cache() -> None:
    """Fetch in the background. Never called from a request path."""
    try:
        cfg = alerts_mod.load().get('news') or {}
        minutes = None
        # Either consumer is reason enough to fetch.
        #
        # This used to fetch only when NEWS ALERTS were enabled, which quietly
        # made the trading gate depend on a setting about Telegram messages:
        # turn alerts off and minutes_to_high_impact() returned None forever,
        # so the news gate passed every signal and nothing said why. The gate
        # now keeps its own reason to look.
        if cfg.get('enabled') or CONFIG.gates.news_blocks:
            data = news(impact=cfg.get('impact', 'high'), within_hours=6)
            minutes = data.get('minutes_to_next')
        with _NEWS_LOCK:
            _NEWS_CACHE.update(at=time.time(), minutes=minutes)
    except Exception:                                     # noqa: BLE001
        with _NEWS_LOCK:
            # Back off on failure too, or a dead calendar becomes a retry storm.
            _NEWS_CACHE.update(at=time.time(), minutes=None)
    finally:
        with _NEWS_LOCK:
            _NEWS_CACHE['refreshing'] = False


def minutes_to_high_impact() -> float | None:
    """
    Minutes to the next qualifying release, from cache.

    Returns immediately, always. A stale answer is fine here - the gate only
    cares about a 15-minute blackout - and blocking is not.
    """
    with _NEWS_LOCK:
        fresh = (time.time() - _NEWS_CACHE['at']) < _NEWS_TTL
        if fresh or _NEWS_CACHE['refreshing']:
            return _NEWS_CACHE['minutes']
        _NEWS_CACHE['refreshing'] = True
    threading.Thread(target=_refresh_news_cache, daemon=True).start()
    return _NEWS_CACHE['minutes']


class ResolveBody(BaseModel):
    target: str
    bot: str | None = None


@app.post('/api/alerts/resolve')
def alerts_resolve(body: ResolveBody):
    """
    What kind of chat is this, and can the chosen bot actually post to it?

    Read-only: getChat plus getChatMember, nothing is sent. Membership is the
    part that matters - a public channel is READABLE by any bot, so getChat
    succeeding says nothing about whether a message will arrive.
    """
    info = alerts_mod.resolve_chat(body.target, bot=body.bot)
    if info.get('ok'):
        info['can_post'] = alerts_mod.can_post(body.target, bot=body.bot)
    info['bot'] = alerts_mod.describe_bot(body.bot)
    return info


@app.get('/api/alerts/log')
def alerts_log(limit: int = 60):
    return {'entries': alerts_mod.recent_log(limit)}


# --------------------------------------------------------------------------- #
# health and metadata                                                         #
# --------------------------------------------------------------------------- #
@app.on_event('startup')
async def _start_workers() -> None:
    """
    Launch the news watcher with the app.

    The board workers start lazily off the first request for a symbol, which
    is right for them - nobody needs a board nobody is looking at. News alerts
    are the opposite: their whole job is to reach you when you are NOT sitting
    in front of the terminal, so they cannot wait for a page load.
    """
    asyncio.create_task(news_alert_worker())
    asyncio.create_task(board_supervisor())
    asyncio.create_task(executor_loop())
    TELEGRAM_COMMANDS.start()


WORKSPACE_FILE = Path(__file__).resolve().parent.parent / 'configs' / 'workspace.json'
# A workspace is a handful of small JSON values. Anything much bigger is not a
# workspace, and refusing it keeps one bad client from filling the disk.
WORKSPACE_MAX_BYTES = 256 * 1024


class WorkspaceBody(BaseModel):
    values: dict


@app.get('/api/workspace')
def get_workspace():
    """The saved workspace: open charts, indicators, layout, watchlist, theme."""
    try:
        data = json.loads(WORKSPACE_FILE.read_text(encoding='utf-8'))
        return {'values': data.get('values') or {}, 'saved_ms': data.get('saved_ms')}
    except (OSError, ValueError):
        return {'values': {}, 'saved_ms': None}


@app.post('/api/workspace')
def set_workspace(body: WorkspaceBody):
    """
    Replace the saved workspace. Keys must be the UI's own ('dianur.*').

    Written to a temp file and renamed into place, so a crash mid-write leaves
    the previous workspace intact instead of a half-written file that would
    load as nothing.
    """
    values = {k: v for k, v in body.values.items()
              if isinstance(k, str) and k.startswith('dianur.')}
    blob = json.dumps({'values': values, 'saved_ms': int(time.time() * 1000)}, indent=1)
    if len(blob) > WORKSPACE_MAX_BYTES:
        raise HTTPException(413, 'workspace too large')
    WORKSPACE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WORKSPACE_FILE.with_suffix('.json.tmp')
    tmp.write_text(blob, encoding='utf-8')
    tmp.replace(WORKSPACE_FILE)
    return {'ok': True, 'keys': sorted(values)}


class WatchlistBody(BaseModel):
    symbols: list[str]


@app.get('/api/watchlist')
def get_watchlist():
    return {'symbols': STATE.watchlist}


@app.post('/api/watchlist')
async def set_watchlist(body: WatchlistBody):
    """
    Replace the watchlist. Signals are generated for these symbols only.

    Order is kept, duplicates dropped. An empty list is refused rather than
    obeyed: it would silently switch signal generation off everywhere, which
    is never what clearing a list in the UI is meant to do.
    """
    names = list(dict.fromkeys(s.strip() for s in body.symbols if s and s.strip()))
    if not names:
        raise HTTPException(400, 'the watchlist cannot be empty')
    removed = [s for s in STATE.watchlist if s not in names]
    STATE.watchlist = names
    save_watchlist(names)
    # Drop signals already on the board for symbols no longer watched, so the
    # board does not keep showing ideas the system has stopped generating.
    for (sym, tf) in list(STATE.board.keys()):
        if sym in removed:
            row = STATE.board[(sym, tf)]
            STATE.board[(sym, tf)] = {**row, 'signals': []}
    return {'symbols': names, 'removed': removed}


@app.get('/api/symbols')
def symbols(q: str = '', limit: int = Query(300, ge=1, le=3000)):
    """
    Every instrument the broker exposes, for the symbol picker.

    `on_disk` marks the ones with bar history stored locally. Those are the
    only symbols a BACKTEST can run on; a live chart works for any of them,
    because the engine pulls bars from the bridge rather than from disk.
    """
    payload = BRIDGE.symbols() if hasattr(BRIDGE, 'symbols') else None
    rows = (payload or {}).get('symbols') or []
    disk = set(available_symbols())
    needle = (q or '').strip().lower()

    out = []
    for r in rows:
        name = r.get('name') or ''
        if needle and needle not in name.lower() and needle not in (r.get('path') or '').lower():
            continue
        out.append({
            'name': name,
            # The broker's own folder, e.g. "Retail\Forex\Majors" - the
            # cheapest way to group a few thousand instruments sensibly.
            'group': (r.get('path') or '').rsplit('\\', 1)[0],
            'digits': r.get('digits'),
            'visible': bool(r.get('visible')),
            'on_disk': name in disk,
        })
    out.sort(key=lambda x: (not x['on_disk'], not x['visible'], x['name']))
    return {'symbols': out[:limit], 'total': len(out), 'on_disk': sorted(disk)}


@app.get('/api/quotes')
def quotes(symbols: str = ''):
    """
    Live bid/ask for several instruments at once.

    The watchlist needs a price per row, and one request per row would be a
    bridge round trip per symbol per poll. The bridge takes a comma list.
    """
    names = [x.strip() for x in (symbols or '').split(',') if x.strip()]
    if not names:
        return {'quotes': {}}
    payload = BRIDGE.quotes(names) or {}
    rows = payload.get('quotes', payload)
    # Enrich with the contract facts, so a caller can turn a price DISTANCE
    # into money without a second round trip per symbol. tick_value is
    # already in the account currency, which is what makes this work across
    # instruments: no cross rate has to be guessed at this end.
    for name, row in (rows or {}).items():
        if not isinstance(row, dict):
            continue
        spec = FEED.spec(name) or {}
        for k in ('tick_size', 'tick_value', 'contract_size'):
            if spec.get(k) is not None:
                row.setdefault(k, spec[k])
    return {'quotes': rows}


@app.get('/api/news/headlines')
def news_headlines(limit: int = Query(30, ge=1, le=100)):
    """Finnhub general market news. Headlines, not a calendar."""
    return {'headlines': news_mod.headlines(limit)}


@app.get('/api/news/sources')
def news_sources():
    """Which providers are configured, and how big the archive is."""
    st = news_mod.refresh(BRIDGE.calendar())
    return {'sources': news_mod.sources_status(), **st}


@app.post('/api/news/test')
def news_test():
    """Send the next qualifying release to Telegram now, gates skipped."""
    cfg = alerts_mod.load()
    news_mod.refresh(BRIDGE.calendar())
    soon = news_mod.upcoming(14 * 86_400_000,
                             (cfg.get('news') or {}).get('impact', 'high'))
    if not soon:
        raise HTTPException(404, 'no upcoming release at this impact level')
    return alerts_mod.dispatch_news(cfg, soon[0], force=True)


@app.get('/api/health')
def health():
    bridge = BRIDGE.health()
    symbols = available_symbols() or [CONFIG.symbol]
    tfs = available_timeframes(symbols[0]) if symbols else TIMEFRAMES
    from .llm import llm_available
    acct = BRIDGE.account() if (bridge or {}).get('connected') else None
    return {
        'ok': True,
        'version': VERSION,
        'bridge': bridge or {'connected': False, 'error': BRIDGE.last_error},
        'mt5': mt5_state(bridge, acct),
        'pnl': realised_pnl(((BRIDGE.deals(35) or {}).get('deals') or []) if acct else [], bridge),
        'symbols': symbols,
        'timeframes': tfs or TIMEFRAMES,
        'trading_enabled': _trading_enabled(),
        'trading_ui': CONFIG.allow_trading_ui,
        'llm_available': llm_available(),
        'server_ms': int(time.time() * 1000),
    }


# --------------------------------------------------------------------------- #
# realised P&L and closed-trade history                                       #
# --------------------------------------------------------------------------- #
# Deals arrive from the bridge already normalised to UTC epoch ms. Traders do
# not think in UTC, though: a "day" ends at the BROKER's midnight, which is
# what rolls the swap and what the terminal's own history totals use. So every
# bucket below is cut on server time, derived from the offset the bridge
# measured at connect.

# Only real trade deals count. A deposit, a credit or a balance correction is
# also a "deal" in MT5 and would otherwise land in PROFIT TODAY as if it were
# something the strategy earned. Those carry a deal type outside {buy, sell},
# which the bridge leaves as a bare numeric string.
_TRADE_SIDES = ('buy', 'sell')


def _server_offset_ms(bridge: dict = None) -> int:
    """Broker clock minus UTC, in ms. Zero when the bridge has not said."""
    try:
        return int((bridge or {}).get('time_offset_ms') or 0)
    except (TypeError, ValueError):
        return 0


def _deal_net(d: dict) -> float:
    """Profit net of costs - the figure that actually moved the balance."""
    return (float(d.get('profit') or 0.0)
            + float(d.get('commission') or 0.0)
            + float(d.get('swap') or 0.0))


def realised_pnl(deals, bridge: dict = None) -> dict:
    """
    Realised P&L for the broker's current day and current month.

    Floating P&L is deliberately NOT included: it already has its own field on
    the ribbon, and folding an open position's swing into "profit today" makes
    a number that moves while nothing has actually been banked.
    """
    off = _server_offset_ms(bridge)
    now = datetime.fromtimestamp((time.time() * 1000 + off) / 1000.0, tz=timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    # The trading week, not the calendar one: Monday 00:00 on the BROKER's
    # clock. The session actually opens late Sunday UTC, which on a UTC+3
    # server is already Monday, so a Monday cut takes the whole week without
    # slicing the Sunday-evening open off the front of it.
    week_start = day_start - timedelta(days=day_start.weekday())
    day_ms = day_start.timestamp() * 1000 - off
    week_ms = week_start.timestamp() * 1000 - off
    month_ms = month_start.timestamp() * 1000 - off

    today = week = month = 0.0
    day_n = week_n = month_n = 0
    for d in deals or []:
        if d.get('side') not in _TRADE_SIDES:
            continue
        t = float(d.get('time_ms') or 0)
        if t < month_ms:
            continue
        net = _deal_net(d)
        # A CLOSING deal is one trade. An opening deal still contributes its
        # commission to the money, but it is not a result in its own right.
        closed = int(d.get('entry') or 0) != 0
        month += net
        month_n += closed
        if t >= week_ms:
            week += net
            week_n += closed
        if t >= day_ms:
            today += net
            day_n += closed

    return {
        'today': round(today, 2),
        'week': round(week, 2),
        'month': round(month, 2),
        'today_trades': day_n,
        'week_trades': week_n,
        'month_trades': month_n,
        'day_start_ms': int(day_ms),
        'week_start_ms': int(week_ms),
        'month_start_ms': int(month_ms),
        'offset_ms': off,
    }


def closed_trades(deals) -> list:
    """
    Pair raw deals into round trips, keyed by position_id.

    MT5 hands back one row per DEAL, so a single trade appears twice and a
    partial close three times or more. A history tab listing deals therefore
    reads as double the trades you took, half of them showing 0.00 profit.
    """
    by_pos: dict = {}
    for d in deals or []:
        if d.get('side') not in _TRADE_SIDES:
            continue
        pid = d.get('position_id') or d.get('ticket')
        t = by_pos.setdefault(pid, {
            'position_id': pid, 'symbol': d.get('symbol'), 'side': None,
            'volume': 0.0, 'entry': None, 'exit': None,
            'open_ms': None, 'close_ms': None, 'profit': 0.0,
            'commission': 0.0, 'swap': 0.0, 'net': 0.0,
            'reason': None, 'sl': d.get('position_sl') or 0.0, 'deals': 0,
            'comment': '',
        })
        t['deals'] += 1
        t['profit'] += float(d.get('profit') or 0.0)
        t['commission'] += float(d.get('commission') or 0.0)
        t['swap'] += float(d.get('swap') or 0.0)
        t['net'] = round(t['profit'] + t['commission'] + t['swap'], 2)
        ms = float(d.get('time_ms') or 0)
        # Take the comment from whichever leg has one. The OPENING deal
        # carries what we sent; a close driven by SL/TP gets the broker's own
        # text instead, which would otherwise overwrite ours.
        if not t['comment'] and d.get('comment'):
            t['comment'] = str(d['comment'])
        if int(d.get('entry') or 0) == 0:
            t['comment'] = str(d.get('comment') or t['comment'])
            # Opening leg. Its side is the side of the TRADE; the closing deal
            # carries the opposite one and would label every long a short.
            t['side'] = d.get('side')
            t['entry'] = d.get('price')
            t['volume'] += float(d.get('volume') or 0.0)
            if t['open_ms'] is None or ms < t['open_ms']:
                t['open_ms'] = ms
        else:
            t['exit'] = d.get('price')
            t['reason'] = d.get('reason')
            if t['close_ms'] is None or ms > t['close_ms']:
                t['close_ms'] = ms
        if not t['sl']:
            t['sl'] = d.get('position_sl') or 0.0

    rows = list(by_pos.values())
    for r in rows:
        r['profit'] = round(r['profit'], 2)
        r['commission'] = round(r['commission'], 2)
        r['swap'] = round(r['swap'], 2)
        r['volume'] = round(r['volume'], 2)
        # A position with no closing deal is still open; the positions tab owns
        # it, so it has no business in a history of what is finished.
        r['open'] = r['close_ms'] is None
    rows = [r for r in rows if not r['open']]
    rows.sort(key=lambda r: r['close_ms'] or 0, reverse=True)
    return rows


@app.get('/api/deals')
def deals(days: int = Query(7, ge=1, le=365)):
    """Closed round trips over the window, newest first, plus their totals."""
    payload = BRIDGE.deals(days) or {}
    raw = payload.get('deals') or []
    trades = closed_trades(raw)
    wins = [t for t in trades if t['net'] > 0]
    losses = [t for t in trades if t['net'] < 0]
    gross_win = sum(t['net'] for t in wins)
    gross_loss = -sum(t['net'] for t in losses)
    return {
        'days': days,
        'trades': trades,
        'pnl': realised_pnl(raw, BRIDGE.health()),
        'summary': {
            'count': len(trades),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': round(100.0 * len(wins) / len(trades), 1) if trades else None,
            'net': round(sum(t['net'] for t in trades), 2),
            'gross_win': round(gross_win, 2),
            'gross_loss': round(gross_loss, 2),
            # Guarded rather than inf: a window with no losing trade is a small
            # sample, not an infinitely good system.
            'profit_factor': round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            'best': round(max((t['net'] for t in trades), default=0.0), 2),
            'worst': round(min((t['net'] for t in trades), default=0.0), 2),
        },
    }


@app.get('/api/calendar')
def calendar(back_hours: int = Query(6, ge=0, le=720),
             ahead_hours: int = Query(48, ge=1, le=336),
             impact: str = 'all',
             time_known_only: bool = False):
    """
    The macro calendar around now - everything by default, rather than the
    high-impact subset /api/news returns for the signal gate.
    """
    now_ms = int(time.time() * 1000)
    # Refresh is throttled internally, so this is a dictionary lookup on all
    # but one call in fifteen minutes.
    news_mod.refresh(BRIDGE.calendar())
    frm = now_ms - back_hours * 3_600_000
    to = now_ms + ahead_hours * 3_600_000
    rows = news_mod.window(frm, to, impact, time_known_only=time_known_only)
    out = [{**e, 'minutes': round((e['ts'] - now_ms) / 60000.0)} for e in rows]
    return {'events': out, 'server_ms': now_ms,
            'sources': news_mod.sources_status()}


def mt5_state(bridge: dict = None, account: dict = None) -> dict:
    """
    The single source of truth for the connection badge.

    Computed server-side rather than in the browser so the REST health check
    and the websocket stream can never disagree about whether MT5 is up.
    """
    b = bridge or {}
    connected = bool(b.get('connected')) and not b.get('error')
    probe = b.get('session_probe') or {}
    kind = account_type(account, b)
    recovering = bool(not connected and probe.get('alive'))
    return {
        'connected': connected,
        'label': 'CONNECTED' if connected else 'MT5 OFFLINE',
        'account_type': kind,
        'login': (account or {}).get('login') or b.get('login'),
        'server': (account or {}).get('server') or b.get('server'),
        'terminal': b.get('terminal'),
        'symbols': probe.get('symbols'),
        'recovering': recovering,
        'detail': (
            b.get('error')
            or ('terminal reachable, session recovering' if recovering else None)
            or ('live account data' if connected else 'no MT5 terminal reachable')
        ),
    }


def account_type(account: dict = None, bridge: dict = None) -> str:
    """
    DEMO / LIVE / CONTEST, or UNKNOWN.

    Prefers MT5's own `trade_mode`, which is authoritative. Falls back to the
    server name because an older bridge build does not report trade_mode, and
    "unknown" on the badge that tells you whether this is real money is worse
    than a well-founded inference from "ICMarketsAU-Demo".
    """
    for src in (account or {}, bridge or {}):
        explicit = src.get('account_type')
        if explicit:
            return str(explicit)
        mode = src.get('trade_mode')
        if mode is not None:
            return {0: 'DEMO', 1: 'CONTEST', 2: 'LIVE'}.get(int(mode), 'UNKNOWN')
    name = str((account or {}).get('server') or (bridge or {}).get('server') or '')
    low = name.lower()
    if 'demo' in low:
        return 'DEMO'
    if 'contest' in low:
        return 'CONTEST'
    if 'live' in low or 'real' in low:
        return 'LIVE'
    return 'UNKNOWN'


def _trading_enabled() -> bool:
    """
    Trading is enabled only when the BRIDGE says so.

    The UI flag controls whether a button is drawn. Whether an order can
    actually be sent is decided by the bridge process, which must have been
    started with --enable-trading. Two independent switches, deliberately:
    a stray browser setting must never be able to arm execution.
    """
    h = BRIDGE.health() or {}
    return bool(h.get('trading_enabled'))


@app.get('/api/bars')
def bars(symbol: str = Query(None), tf: str = '5m', count: int = 600,
         live: int = 1):
    symbol = symbol or CONFIG.symbol
    series = FEED.bars(symbol, tf, count, live=bool(live))
    spec = FEED.spec(symbol)
    return {'symbol': symbol, 'tf': tf, 'bars': series.to_payload(),
            'digits': spec.get('digits', 2), 'source': series.source,
            'status': series.status, 'count': len(series)}


@app.get('/api/history')
def history(symbol: str = Query(None), tf: str = '5m',
            from_ms: int = 0, to_ms: int = 0):
    symbol = symbol or CONFIG.symbol
    series = load_disk(symbol, tf, None, from_ms or None, to_ms or None)
    # A year of M1 is 350k bars; the chart cannot use them and the browser
    # should not be asked to hold them.
    if len(series) > 20000:
        series = series.tail(20000)
    return {'symbol': symbol, 'tf': tf, 'bars': series.to_payload(),
            'count': len(series)}


# --------------------------------------------------------------------------- #
# analysis                                                                    #
# --------------------------------------------------------------------------- #
@app.get('/api/analyse')
def do_analyse(symbol: str = Query(None), tf: str = '5m', live: int = 1):
    symbol = symbol or CONFIG.symbol
    series, snap, sigs = _run_engine(symbol, tf, bool(live))
    return {
        'snapshot': snap,
        'signals': [s.to_dict() for s in sigs],
        'bars': series.to_payload(),
    }


@app.get('/api/narrative')
def do_narrative(symbol: str = Query(None), tf: str = '5m'):
    symbol = symbol or CONFIG.symbol
    snap = STATE.snapshots.get((symbol, tf))
    if snap is None:
        _, snap, _ = _run_engine(symbol, tf)
    return narrator.describe_market(snap)


@app.get('/api/brief')
def do_brief(signal_id: str, symbol: str = Query(None), tf: str = '5m'):
    entry = STATE.signal_index.get(signal_id)
    if entry is None:
        symbol = symbol or CONFIG.symbol
        _run_engine(symbol, tf)
        entry = STATE.signal_index.get(signal_id)
    if entry is None:
        raise HTTPException(404, f'signal {signal_id} not found or expired')
    sig, snap = entry
    stats = STATE.last_stats.get(snap['symbol'])
    return narrator.brief(sig, snap, STATE.context(), stats)


@app.get('/api/replay')
def do_replay(symbol: str = Query(None), tf: str = '5m', at_ms: int = 0):
    """
    Analyse the market AS IT LOOKED at `at_ms`.

    This is what makes the BACKTEST tab honest: scrubbing to a past bar runs the
    engine on data ending at that bar, so the annotations shown are the ones the
    system would actually have drawn, not ones built with hindsight.
    """
    symbol = symbol or CONFIG.symbol
    if not at_ms:
        raise HTTPException(400, 'at_ms is required')
    window = CONFIG.engine.bars_for_analysis
    full = load_disk(symbol, tf, None, None, at_ms)
    if len(full) < 60:
        raise HTTPException(400, f'only {len(full)} bars before that time')
    series = full.tail(window)
    spec = FEED.spec(symbol)

    import numpy as np
    from .engine.analysis import quick_trend
    from .config import MTF_LADDER
    mtf = {}
    for name in dict.fromkeys(MTF_LADDER.get(tf, [tf])):
        if name == tf:
            continue
        s = load_disk(symbol, name, None, None, at_ms)
        if len(s) >= 60:
            mtf[name] = quick_trend(s.tail(300))

    snap = analyse(series, mtf, spec)
    sigs = []
    if snap.get('ok'):
        sigs = qualify_all(generate(snap, series), snap, spec,
                           {'equity': CONFIG.risk.equity, 'open_positions': 0,
                            'trades_today': 0, 'daily_pnl_pct': 0.0})
        for s in sigs:
            STATE.signal_index[s.id] = (s, snap)
    return {'snapshot': snap, 'signals': [s.to_dict() for s in sigs],
            'bars': series.to_payload()}


# --------------------------------------------------------------------------- #
# ask the analyst                                                             #
# --------------------------------------------------------------------------- #
class AskBody(BaseModel):
    question: str
    symbol: str | None = None
    tf: str = '5m'
    signal_id: str | None = None


@app.post('/api/ask')
async def do_ask(body: AskBody):
    from .llm import answer_question
    symbol = body.symbol or CONFIG.symbol
    snap = STATE.snapshots.get((symbol, body.tf))
    if snap is None:
        _, snap, _ = _run_engine(symbol, body.tf)
    sig = None
    if body.signal_id:
        entry = STATE.signal_index.get(body.signal_id)
        sig = entry[0] if entry else None
    stats = STATE.last_stats.get(symbol)
    return await answer_question(body.question, snap, sig, STATE.context(), stats)


# --------------------------------------------------------------------------- #
# backtest                                                                    #
# --------------------------------------------------------------------------- #
class BacktestBody(BaseModel):
    symbol: str | None = None
    tf: str = '5m'
    start: str | None = None          # ISO date
    end: str | None = None
    equity: float = 25000.0
    step: int = 1
    playbooks: list[str] | None = None
    respect_regime: bool = True
    use_m1_resolution: bool = True


def _iso_ms(s: str | None):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


@app.post('/api/backtest/start')
async def backtest_start(body: BacktestBody):
    run_id = f'bt_{uuid.uuid4().hex[:10]}'
    symbol = body.symbol or CONFIG.symbol
    STATE.backtests[run_id] = {
        'status': 'queued', 'progress': 0.0, 'trades': 0,
        'started_ms': int(time.time() * 1000), 'config': body.model_dump(),
        'result': None, 'error': None,
    }

    def progress(done, total, trades):
        rec = STATE.backtests.get(run_id)
        if rec:
            rec['progress'] = round(done / max(total, 1) * 100, 1)
            rec['trades'] = trades
            rec['status'] = 'running'

    async def worker():
        rec = STATE.backtests[run_id]
        rec['status'] = 'running'
        try:
            # to_thread keeps the event loop free; a multi-month replay is
            # CPU-bound for minutes and would otherwise freeze the websocket.
            result = await asyncio.to_thread(
                bt.run, symbol, body.tf, _iso_ms(body.start), _iso_ms(body.end),
                body.equity, max(1, body.step), CONFIG.engine.bars_for_analysis,
                body.playbooks, body.respect_regime, body.use_m1_resolution,
                None, progress, 0,
            )
            rec['result'] = result
            rec['status'] = 'done' if result.get('ok') else 'error'
            rec['error'] = result.get('error')
            rec['progress'] = 100.0
            if result.get('ok'):
                STATE.last_stats[symbol] = result
                _save_run(run_id, result)
        except Exception as exc:                          # noqa: BLE001
            rec['status'] = 'error'
            rec['error'] = f'{type(exc).__name__}: {exc}'
            rec['traceback'] = traceback.format_exc()

    asyncio.create_task(worker())
    return {'run_id': run_id}


def _save_run(run_id: str, result: dict):
    try:
        path = RUN_DIR / f'{run_id}.json'
        slim = {k: v for k, v in result.items() if k != 'trades'}
        slim['trades'] = result.get('trades', [])[:4000]
        path.write_text(json.dumps(slim, allow_nan=False), encoding='utf-8')
    except (OSError, ValueError, TypeError) as exc:
        # Persisting a run is a convenience. It must never be able to fail the
        # run itself, which is what happened when a non-encodable float made
        # json.dumps raise inside the worker's success path.
        print(f'! could not save run {run_id}: {exc}')


@app.get('/api/backtest/status')
def backtest_status(run_id: str):
    rec = STATE.backtests.get(run_id)
    if rec is None:
        raise HTTPException(404, 'unknown run')
    return {k: v for k, v in rec.items() if k != 'result'}


@app.get('/api/backtest/result')
def backtest_result(run_id: str):
    rec = STATE.backtests.get(run_id)
    if rec is None:
        path = RUN_DIR / f'{run_id}.json'
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
        raise HTTPException(404, 'unknown run')
    if rec['status'] != 'done':
        raise HTTPException(409, f"run is {rec['status']}")
    return rec['result']


@app.get('/api/backtest/list')
def backtest_list():
    runs = []
    for rid, rec in STATE.backtests.items():
        runs.append({'run_id': rid, 'status': rec['status'],
                     'progress': rec['progress'], 'trades': rec['trades'],
                     'started_ms': rec['started_ms'], 'config': rec['config']})
    for p in sorted(RUN_DIR.glob('bt_*.json'), reverse=True)[:30]:
        rid = p.stem
        if any(r['run_id'] == rid for r in runs):
            continue
        try:
            doc = json.loads(p.read_text(encoding='utf-8'))
            runs.append({'run_id': rid, 'status': 'done', 'progress': 100.0,
                         'trades': doc.get('summary', {}).get('trades', 0),
                         'started_ms': 0, 'config': {'symbol': doc.get('symbol'),
                                                     'tf': doc.get('tf')},
                         'summary': doc.get('summary')})
        except (OSError, ValueError):
            continue
    runs.sort(key=lambda r: -r.get('started_ms', 0))
    return {'runs': runs}


# --------------------------------------------------------------------------- #
# settings                                                                    #
# --------------------------------------------------------------------------- #
SETTINGS_FILE = Path(__file__).resolve().parent.parent / 'configs' / 'settings.json'
SETTING_GROUPS = ('risk', 'gates', 'engine', 'instrument', 'execution')


def _playbook_names() -> list:
    from .engine.signals import PLAYBOOKS
    return list(PLAYBOOKS)


def _check_setting(group: str, key: str, v):
    """
    The range each setting may take, or None if it is fine. Anything with
    money attached gets a hard range: a slipped decimal in a lot size or a
    risk percentage is the kind of mistake that is cheap to type and
    expensive to trade.
    """
    rules = {
        ('execution', 'min_lots'): (0.01, 5.0),
        ('execution', 'max_lots'): (0.01, 5.0),
        ('execution', 'entry_tolerance_atr'): (0.0, 2.0),
        # 0 disables the cap; above 20 per slot is not a setting, it is a typo.
        ('execution', 'max_per_symbol_tf'): (0, 20),
        ('execution', 'every_s'): (1.0, 60.0),
        ('risk', 'risk_per_trade_pct'): (0.01, 5.0),
        ('risk', 'max_concurrent'): (1, 50),
        ('risk', 'max_daily_loss_pct'): (0.1, 20.0),
        ('risk', 'max_daily_trades'): (1, 500),
        ('risk', 'trail_lock_r'): (0.0, 0.95),
        ('risk', 'trail_atr'): (0.2, 5.0),
        ('risk', 'min_stop_atr'): (0.1, 5.0),
        ('gates', 'min_confidence'): (0, 100),
        # 0 means "never blackout"; beyond two hours it is not a news window,
        # it is switching the system off for the session.
        ('gates', 'news_blackout_min'): (0, 120),
    }
    lo_hi = rules.get((group, key))
    if lo_hi and not (lo_hi[0] <= v <= lo_hi[1]):
        return f'{group}.{key} must be between {lo_hi[0]} and {lo_hi[1]}'
    if (group, key) == ('risk', 'exit_mode') and v not in ('trail', 'partial'):
        return 'risk.exit_mode must be trail or partial'
    if (group, key) == ('execution', 'broker_tp') and v not in ('none', 'tp1', 'tp2', 'cap'):
        return 'execution.broker_tp must be none, tp1, tp2 or cap'
    if (group, key) == ('execution', 'broker_tp_r') and not (1.5 <= v <= 10.0):
        return 'execution.broker_tp_r must be between 1.5 and 10'
    if (group, key) == ('execution', 'auto_timeframes'):
        unknown = [x for x in v if x not in BOARD_TFS]
        if unknown:
            return f'unknown timeframe(s): {", ".join(unknown)}'
    if (group, key) == ('gates', 'disabled_playbooks'):
        unknown = [x for x in v if x not in _playbook_names()]
        if unknown:
            return f'unknown playbook(s): {", ".join(unknown)}'
        if len(set(v)) >= len(_playbook_names()):
            return 'at least one playbook must stay enabled'
    return None


def apply_settings(patch: dict, persist: bool = True) -> tuple:
    """
    Apply a {group: {key: value}} patch. Returns (applied, errors).

    Each value is cast to the setting's own type and range-checked; a bad value
    is reported and skipped, never half-applied. With persist=True the change
    is merged into configs/settings.json so it survives a restart.
    """
    applied, errors = {}, []
    before = {}
    for group in SETTING_GROUPS:
        target = getattr(CONFIG, group, None)
        if target is None or group not in (patch or {}):
            continue
        for key, value in (patch[group] or {}).items():
            if not hasattr(target, key):
                errors.append(f'unknown setting {group}.{key}')
                continue
            current = getattr(target, key)
            try:
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
                elif isinstance(current, tuple):
                    value = tuple(value)
                elif isinstance(current, str):
                    value = str(value)
            except (TypeError, ValueError):
                errors.append(f'{group}.{key}: not a valid value')
                continue
            err = _check_setting(group, key, value)
            if err:
                errors.append(err)
                continue
            before[(group, key)] = current
            setattr(target, key, value)
            applied[f'{group}.{key}'] = list(value) if isinstance(value, tuple) else value

    # Cross-field rules, checked after the whole patch: undo the offending
    # change rather than leave an impossible pair in place.
    e = CONFIG.execution
    if e.min_lots > e.max_lots:
        for k in ('min_lots', 'max_lots'):
            if ('execution', k) in before:
                setattr(e, k, before[('execution', k)])
                applied.pop(f'execution.{k}', None)
        errors.append('min lots cannot be above max lots')

    if persist and applied:
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        for dotted, value in applied.items():
            group, key = dotted.split('.', 1)
            saved.setdefault(group, {})[key] = value
        # Strip superseded keys on the way out, or a renamed setting keeps its
        # dead twin in the file forever - and a later edit that removed the new
        # key would let the old one spring back to life on the next load.
        saved = _migrate_settings(saved)
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(saved, indent=1), encoding='utf-8')
        tmp.replace(SETTINGS_FILE)
    return applied, errors


def _migrate_settings(saved: dict) -> dict:
    """
    Carry old keys onto their replacements.

    one_per_symbol_tf (bool) -> max_per_symbol_tf (int). Dropping the old key
    silently would have been the dangerous option: a saved `false` means "no
    cap", and losing it would fall back to the default of 1 and quietly
    TIGHTEN a live execution gate the owner had deliberately opened.
    """
    ex = (saved or {}).get('execution')
    if isinstance(ex, dict) and 'one_per_symbol_tf' in ex:
        legacy = ex.pop('one_per_symbol_tf')
        ex.setdefault('max_per_symbol_tf', 1 if legacy else 0)
    return saved


def _load_saved_settings() -> None:
    """Re-apply what was saved from the UI. Startup, before anything reads CONFIG."""
    try:
        saved = _migrate_settings(json.loads(SETTINGS_FILE.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        saved = {}
    # Carry over the auto switch from its old home, once.
    legacy = LEDGER_DIR / 'execution.json'
    if legacy.exists() and 'auto' not in (saved.get('execution') or {}):
        try:
            saved.setdefault('execution', {})['auto'] = bool(
                json.loads(legacy.read_text(encoding='utf-8')).get('auto'))
        except (OSError, ValueError):
            pass
    _, errors = apply_settings(saved, persist=False)
    for err in errors:
        print(f'! saved setting ignored: {err}')


_load_saved_settings()


@app.get('/api/settings')
def get_settings():
    return {
        'risk': CONFIG.risk.__dict__,
        'gates': {k: (list(v) if isinstance(v, tuple) else v)
                  for k, v in CONFIG.gates.__dict__.items()},
        'engine': CONFIG.engine.__dict__,
        'instrument': CONFIG.instrument.__dict__,
        'execution': {k: (list(v) if isinstance(v, tuple) else v)
                      for k, v in CONFIG.execution.__dict__.items()},
        'playbooks': _playbook_names(),
        'timeframes': list(BOARD_TFS),
        'trading_enabled': _trading_enabled(),
        'symbol': CONFIG.symbol,
        'saved_to': str(SETTINGS_FILE),
        # The BRIDGE's own hard ceiling, so Settings can warn before a size is
        # saved that the bridge will refuse at send time. None when the bridge
        # is unreachable - absence is not the same as "no limit".
        'bridge_max_lots': ((BRIDGE.health() or {}).get('execution') or {}).get('max_lots'),
    }


@app.post('/api/settings')
async def set_settings(patch: dict):
    applied, errors = apply_settings(patch)
    # Settings change what qualifies, so drop cached verdicts rather than
    # showing stale ones against new rules.
    if applied:
        STATE.snapshots.clear()
        STATE.closed.clear()
        STATE.signals.clear()
    return {'ok': not errors, 'applied': applied, 'errors': errors,
            'settings': get_settings()}


# --------------------------------------------------------------------------- #
# order execution - hard disabled by default                                  #
# --------------------------------------------------------------------------- #
class OrderBody(BaseModel):
    signal_id: str
    confirm: bool = False


def _requalify(rec: dict):
    """
    Judge a FINAL signal against the market NOW (decision 6).

    Uses the latest cached snapshot for its symbol and timeframe - refreshed by
    the board worker - and a fresh trading context. A snapshot too old to
    trust returns None, and nothing is sent on it.
    """
    from .engine.qualify import qualify
    from .engine.signals import Signal
    # The CLOSED-bar snapshot, not the live one.
    #
    # A signal is detected on closed bars; judging it again on a bar still
    # being built means the verdict can flip with a wick that is not there a
    # second later. Falling back to the live snapshot would quietly reinstate
    # exactly that, so there is no fallback: no closed analysis, no decision.
    snap = STATE.closed.get((rec['symbol'], rec['tf']))
    if not snap or not snap.get('ok'):
        return None
    # Staleness is measured in BARS here. The closed snapshot is rebuilt once
    # per close, so on 1h it is legitimately an hour old and the board's
    # seconds-based TTL would reject every one of them.
    bar_ms = TF_SECONDS.get(rec['tf'], 300) * 1000
    if time.time() * 1000 - float(snap.get('generated_ms') or 0) > 2.5 * bar_ms:
        return None
    sig = Signal(**{k: v for k, v in rec['signal'].items() if k in Signal.__dataclass_fields__})
    out = qualify(sig, snap, FEED.spec(rec['symbol']), STATE.context())
    # Which closed bar this verdict belongs to. The executor re-judges a
    # pending order once per bar, and needs to tell a new verdict from the
    # same one handed back two seconds later.
    if out is not None:
        out.judged_bar_ms = int(snap.get('bar_time_ms') or 0)
    return out


def _on_sent(rec: dict) -> None:
    STATE.trades_today += 1


EXECUTOR = Executor(
    STORE, LEDGER, BRIDGE, FEED, CONFIG,
    requalify=_requalify,
    auto=lambda: bool(CONFIG.execution.auto),
    trading_enabled=lambda: _trading_enabled(),
    on_sent=_on_sent,
    tf_ms=lambda tf: int(TF_SECONDS.get(tf, 300) * 1000),
)


def _profit_now():
    """(today, month, currency) - the footer's realised figures, or None offline."""
    raw = BRIDGE.deals(35)
    acct = BRIDGE.account()
    if not isinstance(raw, dict) or not isinstance(acct, dict):
        return None
    p = realised_pnl(raw.get('deals') or [], BRIDGE.health())
    return (float(p['today']), float(p['month']), str(acct.get('currency') or ''))


TELEGRAM_COMMANDS = TelegramCommands(
    token_fn=lambda: alerts_mod.bot_token(),
    allowed_fn=lambda: private_destinations(alerts_mod.load()),
    profit_fn=_profit_now,
    state_path=LEDGER_DIR / 'telegram_offset.json',
)


async def executor_loop() -> None:
    """Walk the lifecycle every few seconds. Never takes the service down."""
    while True:
        try:
            await asyncio.to_thread(EXECUTOR.step)
        except Exception as exc:                              # noqa: BLE001
            print(f'! executor: {type(exc).__name__}: {exc}')
        await asyncio.sleep(CONFIG.execution.every_s)


@app.post('/api/order/send')
def order_send(body: OrderBody):
    """
    The Place button: send one FINAL signal now, through the same path auto
    mode uses. The bridge must be armed, the signal must be FINAL and still
    qualified against the current market, its symbol+timeframe slot free, and
    the ledger must have no order for it. The bridge adds its own guards.
    """
    if not body.confirm:
        raise HTTPException(400, 'confirm=true is required. Nothing was sent.')
    if not _trading_enabled():
        raise HTTPException(403, 'execution is disabled: start the bridge with '
                                 '--enable-trading to arm it. Nothing was sent.')
    rec = STORE.get(body.signal_id)
    if rec is None:
        raise HTTPException(404, 'unknown signal - only a FINAL signal can be sent')
    outcome = EXECUTOR.send_now(body.signal_id)
    after = STORE.get(body.signal_id) or {}
    return {'ok': after.get('stage') not in (FINAL, None), 'stage': after.get('stage'),
            'message': outcome, 'ledger': LEDGER.get(body.signal_id)}


class AutoBody(BaseModel):
    enabled: bool


@app.get('/api/execution')
def execution_state(limit: int = Query(100, ge=1, le=1000)):
    """Auto mode, arming, every live signal's stage, and the order ledger."""
    return {
        'auto': bool(CONFIG.execution.auto),
        'trading_enabled': _trading_enabled(),
        'settings': {
            'entry_tolerance_atr': CONFIG.execution.entry_tolerance_atr,
            'lots': [CONFIG.execution.min_lots, CONFIG.execution.max_lots],
            'max_per_symbol_tf': CONFIG.execution.max_per_symbol_tf,
            'max_concurrent': CONFIG.risk.max_concurrent,
            'exit': {'mode': CONFIG.risk.exit_mode, 'lock_r': CONFIG.risk.trail_lock_r,
                     'trail_atr': CONFIG.risk.trail_atr},
        },
        'signals': STORE.listing(limit),
        'orders': LEDGER.listing(limit),
    }


@app.post('/api/execution/auto')
def execution_auto(body: AutoBody):
    """
    Switch automatic sending on or off - the same saved setting as the
    Execution section of Risk & Gates. It only ever sends through a bridge
    started with --enable-trading.
    """
    apply_settings({'execution': {'auto': bool(body.enabled)}})
    return {'auto': CONFIG.execution.auto, 'trading_enabled': _trading_enabled()}


# --------------------------------------------------------------------------- #
# live websocket                                                              #
# --------------------------------------------------------------------------- #
@app.websocket('/ws/live')
async def ws_live(ws: WebSocket, symbol: str = None, tf: str = '5m'):
    await ws.accept()
    symbol = symbol or CONFIG.symbol
    current = {'symbol': symbol, 'tf': tf}
    last_sent_bar = None
    # Realised P&L is refreshed on its own slow clock, not once per frame.
    # read_deals() walks a month of history AND asks MT5 for the orders behind
    # every position in it, which is far too heavy for a 1s loop - doing it
    # inline stalled the whole stream while it held MT5_LOCK.
    pnl_cache = {'value': None, 'at': 0.0}
    PNL_EVERY_S = 20.0

    async def reader():
        """Handle subscribe messages without blocking the push loop."""
        try:
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                if msg.get('action') == 'subscribe':
                    current['symbol'] = msg.get('symbol') or current['symbol']
                    current['tf'] = msg.get('tf') or current['tf']
        except (WebSocketDisconnect, RuntimeError, ValueError):
            return

    task = asyncio.create_task(reader())
    ensure_board(symbol)
    try:
        while True:
            sym, timeframe = current['symbol'], current['tf']
            # Every tick, not once at connect: a subscribe can move this socket
            # to another symbol, and the viewed symbol must stay marked as seen
            # or its board worker is reaped while you are looking at it.
            ensure_board(sym)
            try:
                series, snap, sigs = await asyncio.to_thread(
                    _run_engine, sym, timeframe, True)
            except Exception as exc:                      # noqa: BLE001
                await ws.send_text(json.dumps(
                    {'type': 'error', 'error': f'{type(exc).__name__}: {exc}'}))
                await asyncio.sleep(3)
                continue

            acct = await asyncio.to_thread(BRIDGE.account)
            positions = await asyncio.to_thread(BRIDGE.positions)
            orders = await asyncio.to_thread(BRIDGE.orders)
            quote = await asyncio.to_thread(FEED.quote, sym)
            bridge_health = await asyncio.to_thread(BRIDGE.health)

            if (isinstance(acct, dict)
                    and time.time() - pnl_cache['at'] > PNL_EVERY_S):
                try:
                    raw = await asyncio.to_thread(BRIDGE.deals, 35)
                    pnl_cache['value'] = realised_pnl(
                        (raw or {}).get('deals') or [], bridge_health)
                except Exception:                              # noqa: BLE001
                    # Keep the last good figure rather than blanking the
                    # ribbon over one slow history read.
                    pass
                pnl_cache['at'] = time.time()

            narrative = narrator.describe_market(snap) if snap.get('ok') else None

            payload = {
                'type': 'snapshot',
                'symbol': sym,
                'tf': timeframe,
                'bars': series.to_payload(),
                'data_status': series.status,
                'snapshot': snap,
                'signals': [s.to_dict() for s in sigs],
                'quote': quote,
                'account': acct,
                'positions': positions if isinstance(positions, list) else [],
                'orders': orders if isinstance(orders, list) else [],
                'bridge': bridge_health or {'connected': False,
                                            'error': BRIDGE.last_error},
                'mt5': mt5_state(bridge_health, acct if isinstance(acct, dict) else None),
                'pnl': pnl_cache['value'],
                # Cached read, not an engine pass - see mtf_trendlines().
                'mtf_trendlines': mtf_trendlines(sym, timeframe),
                'narrative': narrative,
                # Cached, never computed here: a full sweep is ~1s and this
                # loop runs every second.
                'board': board_payload(sym),
                'trading_enabled': _trading_enabled(),
                'auto_execute': bool(CONFIG.execution.auto),
                'server_ms': int(time.time() * 1000),
            }
            await ws.send_text(json.dumps(payload, default=str))
            last_sent_bar = snap.get('bar_time_ms')
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        task.cancel()


# --------------------------------------------------------------------------- #
# static frontend (after `npm run build`)                                     #
# --------------------------------------------------------------------------- #
_DIST = Path(__file__).resolve().parent.parent / 'web' / 'dist'
if _DIST.exists():
    app.mount('/assets', StaticFiles(directory=_DIST / 'assets'), name='assets')

    @app.get('/')
    def index():
        return FileResponse(_DIST / 'index.html')

    @app.get('/{path:path}')
    def spa(path: str):
        candidate = _DIST / path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / 'index.html')
else:
    @app.get('/')
    def dev_index():
        return JSONResponse({
            'service': 'DiaNurFx',
            'version': VERSION,
            'note': 'UI not built. Run `npm run dev` in web/ for development, '
                    'or `npm run build` to serve it from here.',
            'api': '/api/docs',
        })


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description='DiaNurFx analysis service')
    parser.add_argument('--host', default=CONFIG.host)
    parser.add_argument('--port', type=int, default=CONFIG.port)
    parser.add_argument('--reload', action='store_true')
    args = parser.parse_args()

    if args.host not in ('127.0.0.1', 'localhost'):
        print('! refusing to bind a non-loopback interface. This service reads '
              'your live account state and must not be reachable from the '
              'network.')
        raise SystemExit(2)

    print(f'DiaNurFx {VERSION}  ->  http://{args.host}:{args.port}')
    print(f'  bridge  : {CONFIG.bridge_url}')
    print(f'  symbol  : {CONFIG.symbol}')
    uvicorn.run('server.main:app' if args.reload else app,
                host=args.host, port=args.port, reload=args.reload,
                log_level='warning')


if __name__ == '__main__':
    main()
