"""
server/daily.py - the trading DAY, and what the daily limits measure.

One definition, used by the live risk gates (server/main.py), the footer's
realised P&L (main.realised_pnl) and the backtest lab (server/lab/session.py):

  THE DAY     midnight to midnight on the BROKER's clock - the day MT5 rolls
              swap on and totals its own history by, and the "today" the
              footer already shows. Live, the broker clock is UTC plus the
              offset the bridge measured. History on disk is stamped in broker
              server time already (see datafeed.py), so the lab uses offset 0
              and cuts its days at the same midnight.

  TRADES      orders SENT since the day began - placed at the broker, market
              or pending. Live, they are counted from the order ledger, which
              is on disk: an API restart mid-day used to reset an in-memory
              counter and hand out a fresh day's allowance.

  DAILY P&L   realised today plus floating now, as a % of the balance the day
              started with. That starting balance is the balance now minus
              what was realised today, so it survives a restart too; the whole
              thing is (equity - start-of-day balance) / start-of-day balance.

A deposit or withdrawal during the day moves the balance without being a
trade, and is not in realised P&L, so it shifts the derived starting balance
by that amount. Rare on this account, and noted rather than modelled.
"""
from __future__ import annotations

import threading
import time

DAY_MS = 86_400_000


def day_start_ms(now_ms: int, offset_ms: int = 0) -> int:
    """UTC epoch ms at which the broker day containing `now_ms` began."""
    off = int(offset_ms or 0)
    return ((int(now_ms) + off) // DAY_MS) * DAY_MS - off


def sends_since(rows, since_ms: int) -> int:
    """
    Orders placed at the broker at or after `since_ms`, from order-ledger rows.

    A row is one signal's order; its 'placed' event is the moment the broker
    accepted it. A refused attempt has no 'placed' event and does not count,
    exactly as the executor never reported it as sent.
    """
    n = 0
    for r in rows:
        for ev in r.get('events') or []:
            if len(ev) >= 2 and ev[1] == 'placed' and int(ev[0]) >= since_ms:
                n += 1
                break
    return n


def daily_pnl_pct(account: dict, realised_today) -> float | None:
    """
    Today's P&L - realised plus floating - as a % of the start-of-day balance.
    None when the account is unknown (the bridge did not answer).
    """
    if not isinstance(account, dict):
        return None
    try:
        balance = float(account['balance'])
    except (KeyError, TypeError, ValueError):
        return None
    floating = account.get('profit')
    if floating is None:
        try:
            floating = float(account['equity']) - balance
        except (KeyError, TypeError, ValueError):
            floating = 0.0
    realised = float(realised_today or 0.0)
    start = balance - realised
    if start <= 0:
        return None
    return (realised + float(floating)) / start * 100.0


def judging_context(ctx: dict, stage: str) -> dict:
    """
    The context an ALREADY-SENT order is re-judged against.

    The day's order cap is about SENDING, and this order was sent - it is in
    the count. Judged against a total that includes itself, the day's last
    order would be cancelled at its first closed bar for breaking a cap it was
    placed under. So a sent order is re-judged with the cap out of the way;
    the daily LOSS limit still applies to it and still pulls it.
    """
    if stage == 'SENT':
        return dict(ctx, trades_today=0)
    return ctx


class DailyLimits:
    """
    The live daily limits' inputs, kept current for the trading context.

    Everything it talks to is injected - the bridge's /deals and /health, the
    function that turns deals into realised P&L, the ledger's send count, the
    clock - so the tests drive it with fakes and no test can touch MT5.

    Realised P&L is ONE cache for the process: the footer (every websocket)
    and the daily-loss gate both read it. The deals behind it are a heavy
    read - a month of history plus the orders of every position in it - so it
    refreshes at most every REALISED_EVERY_S, and on its own thread: whoever
    asked (the executor re-judging an order, a board sweep) gets the last
    figure at once instead of waiting on the history.
    """

    REALISED_EVERY_S = 20.0     # realised P&L only moves when a trade closes
    OFFSET_EVERY_S = 600.0      # the broker's offset moves twice a year

    def __init__(self, *, deals, health, summarise, sends, now=time.time,
                 background: bool = True):
        self._deals = deals            # () -> bridge /deals payload, or None
        self._health = health          # () -> bridge /health payload, or None
        self._summarise = summarise    # (deals, offset_ms) -> {'today', 'day_start_ms', ...}
        self._sends = sends            # (since_ms) -> orders placed since
        self._now = now
        self._background = background
        self._lock = threading.Lock()
        self._realised: dict | None = None
        self._realised_at = 0.0
        self._busy = False
        self.offset_ms = 0             # broker clock minus UTC
        self._offset_try_at = 0.0      # when an offset was last asked for or seen
        self.figures: dict = {}        # the last refresh, for /api

    def note_health(self, health) -> None:
        """Keep the broker offset fresh from a /health answer someone already has."""
        if isinstance(health, dict) and health.get('time_offset_ms') is not None:
            try:
                off = int(health.get('time_offset_ms') or 0)
            except (TypeError, ValueError):
                return
            with self._lock:
                self.offset_ms = off
                self._offset_try_at = self._now()

    def day_start(self) -> int:
        return day_start_ms(self._now() * 1000, self.offset_ms)

    def realised(self, refresh: bool = True) -> dict | None:
        """
        The cached realised P&L - refreshed when older than REALISED_EVERY_S
        or taken before the broker day rolled. Never waits for the read.
        """
        now = self._now()
        with self._lock:
            cur = self._realised
            fresh = (cur is not None and now - self._realised_at < self.REALISED_EVERY_S
                     and cur.get('day_start_ms') == day_start_ms(now * 1000, self.offset_ms))
            if fresh or not refresh or self._busy:
                return cur
            self._busy = True
            off = self.offset_ms
        if self._background:
            threading.Thread(target=self._read, args=(off,), daemon=True,
                             name='realised-pnl').start()
            return cur
        self._read(off)
        return self._realised

    def _read(self, off: int) -> None:
        value = None
        try:
            raw = self._deals()
            if isinstance(raw, dict):
                value = self._summarise(raw.get('deals') or [], off)
        except Exception:                                 # noqa: BLE001
            value = None
        finally:
            with self._lock:
                if value is not None:
                    self._realised = value
                # Stamped on failure too: a bridge slow to answer must not be
                # asked again on every rebuild of the context.
                self._realised_at = self._now()
                self._busy = False

    def refresh(self, acct: dict) -> tuple:
        """
        (trades_today, daily_pnl_pct) for the context being built.

        At most ONE bridge call per refresh, on top of whatever the caller
        already made: /health when the broker offset has not been seen for a
        while (normally it arrives free with every websocket tick), otherwise
        /deals when realised P&L is due - never both.
        """
        now = self._now()
        if now - self._offset_try_at > self.OFFSET_EVERY_S:
            with self._lock:
                self._offset_try_at = now
            self.note_health(self._health())
            realised = self.realised(refresh=False)
        else:
            realised = self.realised()
        day0 = day_start_ms(now * 1000, self.offset_ms)
        # A figure from before the broker's midnight is yesterday's: nothing is
        # realised in the new day until the next read says otherwise.
        today = float(realised['today']) if realised and realised.get('day_start_ms') == day0 else 0.0
        trades = int(self._sends(day0))
        pct = daily_pnl_pct(acct, today) if acct else None
        self.figures = {
            'day_start_ms': day0, 'offset_ms': self.offset_ms, 'trades_today': trades,
            'realised_today': round(today, 2),
            'floating': (acct or {}).get('profit'),
            'pnl_pct': round(pct, 3) if pct is not None else None,
        }
        return trades, (round(pct, 3) if pct is not None else 0.0)


__all__ = ['DAY_MS', 'day_start_ms', 'sends_since', 'daily_pnl_pct', 'judging_context',
           'DailyLimits']
