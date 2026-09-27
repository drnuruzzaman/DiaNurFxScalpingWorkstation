"""
server/lab/strategies.py - research strategies the lab can replay instead of the engine's playbooks.

LAB ONLY: the live API never imports this module, and nothing here can reach
a real account - orders go to the session's SimBroker like everything else.

  mtf_ls_v4   tools/research_mtf_liquidity.py's variant v4 on the ladder whose
              entry timeframe is the session's (1m, 3m, 5m, 15m, 30m, 1h): a
              sweep of liquidity against the higher-timeframe bias, a change of
              character with a displacement that left a fair value gap, a limit
              order at the 50% retracement valid 12 bars, stop beyond the
              sweep, 2R target, a 96-bar time stop.

The rules are READ from the research tool, never copied or edited here: that
file is frozen for the forward test (tools/forward_mtf_ls.py), and a second
copy of them would drift. Signals are computed once per session from the
history on disk - each one fires at the close of the bar it was knowable at,
from bars closed by then, exactly as the research tool finds them.
"""
from __future__ import annotations

from datetime import datetime, timezone

STRATEGIES = {
    'mtf_ls_v4': 'MTF-LS v4 - liquidity sweep, change of character with displacement, '
                 'limit at the 50% retracement (research)',
}


def ladder_tfs() -> tuple:
    import tools.research_mtf_liquidity as R
    return tuple(R.LADDERS)


class MtfLsV4:
    """Every v4 signal between start_ms and end_ms on one symbol and entry timeframe."""

    name = 'mtf_ls_v4'
    tag = 'MTFLS'                       # order comments: 'MTFLS <n>'

    def __init__(self, symbol: str, tf: str, start_ms: int, end_ms: int):
        import tools.research_mtf_liquidity as R
        from ..datafeed import load_disk
        from ..forecast.timebase import broker_to_utc
        if tf not in R.LADDERS:
            raise ValueError(f"MTF-LS v4 runs on {', '.join(R.LADDERS)} - not {tf}")
        R.RULES.clear()
        R.RULES.update(R.RULES_DEFAULT)
        R.RULES.update(R.VARIANTS['v4'])
        R.use_ladder(tf)
        self.rules = dict(R.RULES)
        y0 = datetime.fromtimestamp(start_ms / 1000, timezone.utc).year - 1
        y1 = datetime.fromtimestamp(end_ms / 1000, timezone.utc).year
        years = list(range(y0, y1 + 1))
        bias, pool, entry = (load_disk(symbol, t, years) for t in
                             (self.rules['bias_tf'], self.rules['pool_tf'], tf))
        fr = R.Frame(entry, bias, pool)
        self.signals = {}                                       # entry bar close ms -> signal
        for ev in R.sweeps(pool, broker_to_utc(pool.t)):
            if not start_ms <= ev[1] < end_ms:
                continue
            sig = fr.signal(ev)
            if not (sig and fr.passes(sig)):
                continue
            i = sig['i']
            close = float(entry.c[i])
            sig.update(close=close, atr=float(fr.atr[i]), band=float(fr.band[i]),
                       limit=close + self.rules['limit_frac'] * (sig['anchor'] - close))
            self.signals.setdefault(sig['close_ms'], sig)       # the first per close, as ordered
        self.timeframes = (tf, self.rules['pool_tf'], self.rules['bias_tf'])

    def at(self, close_ms: int):
        return self.signals.get(int(close_ms))


def build(name: str, symbol: str, tf: str, start_ms: int, end_ms: int):
    if name == 'mtf_ls_v4':
        return MtfLsV4(symbol, tf, start_ms, end_ms)
    raise ValueError(f'unknown strategy {name}')


__all__ = ['STRATEGIES', 'ladder_tfs', 'MtfLsV4', 'build']
