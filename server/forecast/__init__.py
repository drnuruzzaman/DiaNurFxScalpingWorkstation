"""
server/forecast - the forecast measurement engine (Phase A: foundations).

Not a prediction engine first. Everything here exists to MEASURE whether a
forecast is worth anything, against a baseline that never sees the forecast:

    market state (closed bars) --+--> baseline  (empirical, resolved-only)
                                 +--> forecast  (Phase B onwards)
    outcome labels --> scorecard --> skill vs baseline, per year, per condition

Phase A builds the parts every later phase stands on:

    timebase.py   broker server time <-> UTC (the disk is broker time)
    reads.py      the live engine's own reads - regime, leg, MTF trend - for
                  every historical bar, on the same windows the chart uses
    news.py       scheduled US releases as causal features
    features.py   one feature row per closed bar, assembled from the above
    labels.py     what happened next: excursions, direction, state, and the
                  trade outcome on the lab broker's exact fill path
    baseline.py   empirical baselines, from outcomes RESOLVED before now
    score.py      Brier, log loss, calibration, pinball, coverage, skill
    batch.py      the whole-period run that turns forecasts into evidence

Nothing in this package can place an order, and none of it imports the live
API (server.main) - it reads disk history and writes under runs/forecast/.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import ROOT

SYMBOL = 'XAUUSD.a'

# The forecast timeframes and each one's horizon, in its own bars.
TFS = ('5m', '15m', '1h', '4h')
HORIZON = {'5m': 12, '15m': 8, '1h': 6, '4h': 3}

# Timeframes whose MTF trend read (quick_trend) is needed: every ladder rung
# above a forecast timeframe, as server.config.MTF_LADDER defines it.
QT_TFS = ('5m', '15m', '1h', '4h', '1d', '1w')

# Trade forecasts are decided on these timeframes and walked on the M1 path
# for at most TRADE_HORIZON one-minute bars (72 x 5m, as Phase 0b measured).
TRADE_TFS = ('5m', '15m')
TRADE_HORIZON = 360

# First-touch grid, in ATR of the decision timeframe, on the BID path from the
# decision bar's close. General-purpose: MFE/MAE thresholds and any SL/TP pair.
LEVELS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0)

# Standard trade templates resolved EXACTLY (spread and the lab broker's path
# rule included): side, stop in ATR, target in R. The live risk settings are
# a 1.0 ATR stop floor, a 1.5 ATR stop multiple, TP1 = 1R and TP2 = 2R.
TEMPLATES = tuple((side, sl, tp) for side in ('buy', 'sell')
                  for sl in (1.0, 1.5) for tp in (1.0, 1.5, 2.0))

# MFE / MAE thresholds reported as exceedance probabilities, in ATR.
MFE_AT = (0.5, 1.0, 1.5, 2.0)
MAE_AT = (0.5, 1.0, 1.5)

# Spread levels, in ATR, at which every historical trade path is REPLAYED for
# the baseline. The spread is known when a trade is decided and it moves the
# odds (gold's spread fell from ~0.10 to ~0.02 ATR between 2018 and 2026), so
# the baseline asks "what did history do AT TODAY'S COST" - matching on cost
# instead would also match on the volatility spikes that made cost cheap.
COST_LEVELS = (0.0, 0.01, 0.02, 0.03, 0.045, 0.065, 0.09, 0.13, 0.2)

# The engine's windows, exactly as live and the lab use them.
WINDOW = 600                # bars handed to analyse() (engine.bars_for_analysis)
QT_WINDOW = 300             # bars handed to quick_trend() for each MTF rung

# Expanding training window start, in broker time (decision 1).
TRAIN_START = '2018-01-01'

FEATURE_VERSION = 'f1'
LABEL_VERSION = 'l2'
BASELINE_VERSION = 'b2'

OUT_DIR = ROOT / 'runs' / 'forecast'
CACHE_DIR = OUT_DIR / 'cache'           # gitignored (cache/)
BATCH_DIR = OUT_DIR / 'batch'


def ms_of(day: str) -> int:
    """'YYYY-MM-DD' as epoch ms, read as broker time like every disk stamp."""
    return int(datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() * 1000)


def year_of(ms: float) -> int:
    return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).year


# Engine source files whose code decides a read. Any edit to them changes the
# hash, and the reads cache rebuilds instead of serving a stale computation.
_ENGINE_FILES = ('indicators.py', 'structure.py', 'trendlines.py', 'regime.py',
                 'legs.py', 'analysis.py')


def live_engine_settings() -> dict:
    """
    The engine settings the live strategy runs with: code defaults, then
    whatever the app saved in configs/settings.json - the same layering the
    lab uses for a session without overrides. The live API applies the saved
    file at start; a plain process would not, so it is read here.
    """
    from ..lab import settings as lab_settings
    return lab_settings.effective(None)['engine']


def sync_engine_settings() -> None:
    """Put the live engine settings onto this process's CONFIG (build processes only)."""
    from ..lab import settings as lab_settings
    lab_settings.activate({'engine': live_engine_settings()})


def engine_hash() -> str:
    """Hash of the engine code and the live engine settings a cached read depends on."""
    h = hashlib.sha256()
    base = Path(__file__).resolve().parent.parent / 'engine'
    for name in _ENGINE_FILES:
        h.update((base / name).read_bytes())
    h.update(json.dumps(live_engine_settings(), sort_keys=True).encode())
    return h.hexdigest()[:16]


def latest_run(symbol: str = SYMBOL, need: str = 'ui_summary.json') -> Path | None:
    """
    The newest batch run of one symbol that wrote `need`, or None. Every run
    records its symbol in manifest.json; a run without one predates other
    symbols and is gold's. One symbol's gates never stand in for another's.
    """
    try:
        runs = sorted((p for p in BATCH_DIR.iterdir() if (p / need).exists()),
                      key=lambda p: p.name, reverse=True)
    except OSError:
        return None
    for p in runs:
        try:
            sym = json.loads((p / 'manifest.json').read_text(encoding='utf-8')).get('symbol')
        except (OSError, ValueError):
            sym = None
        if (sym or SYMBOL) == symbol:
            return p
    return None


__all__ = ['SYMBOL', 'TFS', 'HORIZON', 'QT_TFS', 'TRADE_TFS', 'TRADE_HORIZON', 'LEVELS',
           'COST_LEVELS', 'TEMPLATES', 'MFE_AT', 'MAE_AT', 'WINDOW', 'QT_WINDOW', 'TRAIN_START',
           'FEATURE_VERSION', 'LABEL_VERSION', 'BASELINE_VERSION', 'OUT_DIR', 'CACHE_DIR',
           'BATCH_DIR', 'ms_of', 'year_of', 'live_engine_settings', 'sync_engine_settings',
           'engine_hash', 'latest_run']
