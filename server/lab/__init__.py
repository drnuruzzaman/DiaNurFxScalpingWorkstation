"""
server/lab - the backtest lab: replay history bar by bar, trade it with the
live system's own execution code, record everything, review it.

Separation from live trading is structural, not a convention:

  PROCESS   the lab is its own server (python -m server.lab, port 8771). It
            never imports server.main, so it has no live state, no signal
            store or order ledger of the live account, no Telegram, and no
            connection to the MT5 bridge. A replay's CPU never slows the
            live engine, and a live API restart never kills a replay.
  DATA      history comes from data/<symbol>/<tf>/<year>.csv.gz only. The
            lab's broker (broker.SimBroker) is a simulation; there is no code
            path in this package that can reach a real account.
  SETTINGS  each session carries its own settings - the live settings.json
            as a starting point plus the session's overrides - applied to
            THIS process's CONFIG only. configs/settings.json is never
            written.
  STORAGE   sessions live under runs/lab/<id>/, never in order_ledger/.

Fidelity: the replay drives the live Executor, SignalStore and OrderLedger
(server/executor.py and friends) against simulated time (server/clock.py)
and a broker that fills on the M1 path. So a backtest takes the same entry,
re-judges pending orders on the same closed bars, and trails the same stop
the live system would - it tests what trades, not an approximation of it.
"""
