"""
server/config.py - one place for every knob, so nothing is hard-coded twice.

Values come from the environment (see .env.example) with sane defaults for a
Raw-Spread Islamic gold account. Nothing secret is stored here; credentials stay
with the bridge.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'data'
LOG_DIR = ROOT / 'logs'
RUN_DIR = ROOT / 'runs'
CONFIG_DIR = ROOT / 'config'

for _d in (LOG_DIR, RUN_DIR, CONFIG_DIR):
    _d.mkdir(exist_ok=True)


def _load_dotenv() -> None:
    """
    Read .env into the environment, without overriding what is already set.

    Kept to a dozen lines rather than pulling in python-dotenv: the only thing
    that lives in .env is credentials, and a credential loader is a bad place
    for a dependency nobody reads. A real shell export always wins.
    """
    path = ROOT / '.env'
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


def _env(key: str, default: str = '') -> str:
    return os.environ.get(key, default).strip()


# Timeframes, ordered coarse-last. Used everywhere a TF ladder is needed.
TIMEFRAMES = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '1d', '1w']
TF_SECONDS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1h': 3600, '2h': 7200, '4h': 14400, '1d': 86400, '1w': 604800,
}
# The ladder a scalping signal consults for higher-timeframe context.
MTF_LADDER = {
    '1m':  ['1m', '5m', '15m', '1h'],
    '3m':  ['3m', '15m', '30m', '1h'],
    '5m':  ['5m', '15m', '1h', '4h'],
    '15m': ['15m', '1h', '4h', '1d'],
    '30m': ['30m', '2h', '4h', '1d'],
    '1h':  ['1h', '4h', '1d', '1w'],
    '2h':  ['2h', '4h', '1d', '1w'],
    '4h':  ['4h', '1d', '1w', '1w'],
    '1d':  ['1d', '1w', '1w', '1w'],
    '1w':  ['1w', '1w', '1w', '1w'],
}


@dataclass
class InstrumentSpec:
    """
    Contract terms. Defaults are IC Markets Raw Spread XAUUSD; live values are
    pulled from the bridge's /spec when a terminal is attached and override
    these. They exist so backtests run without MT5 open.
    """
    symbol: str = 'XAUUSD.a'
    digits: int = 2
    point: float = 0.01
    tick_size: float = 0.01
    contract_size: float = 100.0          # 100 oz per lot
    tick_value: float = 1.0               # USD per 0.01 move, 1 lot
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    stops_level_points: int = 0
    # Raw Spread Islamic: commission is the cost, swap is zero by account type.
    commission_per_lot_side: float = 3.0  # USD, charged each side
    swap_free: bool = True
    default_slippage_points: float = 3.0  # points of adverse fill on market entry


@dataclass
class RiskSettings:
    account_currency: str = 'AUD'
    equity: float = 25_000.0
    risk_per_trade_pct: float = 0.5       # % of equity at risk per idea
    max_concurrent: int = 2
    max_daily_loss_pct: float = 2.0
    max_daily_trades: int = 12
    min_rr: float = 1.5                   # below this a signal cannot qualify
    atr_stop_mult: float = 1.5
    # A stop closer than this to entry sits INSIDE the instrument's normal
    # noise and gets hit by random fluctuation whether the idea was right or
    # wrong. Measured on 5m gold: the breakout-retest playbook was placing
    # stops at ~0.35 ATR and stopping out of 22 of 35 trades in a median of
    # two bars, with a median adverse excursion of -1.31R. Widening the stop
    # does not increase risk - position size is derived from it - it just stops
    # paying for noise.
    min_stop_atr: float = 1.0
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    breakeven_at_r: float = 1.0
    # The exit plan.
    #   'trail'    at TP1 the stop locks trail_lock_r beyond entry, then trails
    #              trail_atr x ATR on CLOSED bars. Whole position, no fixed
    #              target - TP2 becomes an objective, not an order.
    #   'partial'  the old plan: half off at TP1, stop to break-even, rest to TP2.
    # Measured 2026-09-21 with tools/exp_exits.py, spread charged, same entries:
    # 'trail' beat 'partial' in 2026 (+0.021R/trade, t=+2.2) and was level in
    # 2025 (t=+0.4) - never worse. All-out at TP1 was the WORST exit in both
    # years (t=-3.3, -2.6), because only ~36% of trades ever reach +1R.
    exit_mode: str = 'trail'
    # +0.5R, as decided 2026-09-21. Measured against the tested +0.3R on three
    # years: indistinguishable (paired -0.001R, +0.003R, -0.002R per trade).
    trail_lock_r: float = 0.5
    trail_atr: float = 1.0


@dataclass
class GateSettings:
    """Qualification gates. Every one of these can veto a detected opportunity."""
    max_spread_points: float = 45.0       # gold raw spread blows out on news

    # Volatility bounds are RELATIVE, never absolute points.
    #
    # Gold ran from 1945 (2023) to 4561 (2026) and its median M5 ATR went from
    # 102 points to 548. A fixed [40, 900] point band that rejected 1.6% of 2023
    # rejected 16% of 2026 - the gate was measuring the gold price, not the
    # market condition. Both bounds are now scale-free:
    #
    #   FLOOR    ATR must be worth several round trips, or the cost of trading
    #            eats the move. This is an economic statement, not a guess.
    #   CEILING  a percentile of the instrument's OWN recent ATR, so a news
    #            spike is caught in any year at any price.
    min_atr_cost_multiple: float = 4.0    # ATR >= 4x (spread + commission)
    max_atr_percentile: float = 97.0      # top 3% of its own range = news spike
    min_confidence: int = 55
    news_blackout_min: int = 15           # minutes either side of high impact
    # 24-hour operation. The four sessions between them cover every hour of
    # the trading week (sydney 21-06, tokyo 00-09, london 07-16, ny 12-21),
    # so with sydney included there is no hour without a live session.
    sessions_allowed: tuple = ('sydney', 'tokyo', 'london', 'newyork')
    # Whether a thin hour BLOCKS a signal or merely warns. Off: the session
    # read still scores the setup - sydney is thin and says so, and the
    # confidence penalty is unchanged - but it no longer prevents a trade.
    session_blocks: bool = False
    # Whether the open-position cap blocks a SIGNAL. Off: signals and alerts
    # keep coming however many positions are open; the cap is enforced at
    # order time instead, which is the only place it protects money.
    exposure_blocks: bool = False
    require_mtf_agreement: bool = True
    min_mtf_score: int = 25               # -100..100
    min_room_to_target_r: float = 1.0     # clear space before the first obstacle
    cooldown_bars: int = 5                # bars between signals on one playbook
    # Playbooks that generate nothing. Adopted 2026-09-21 after three years
    # of testing (tools/exp_entries.py, tools/exp_confirm.py), on a rule fixed
    # BEFORE the holdout was seen:
    #   - sweep_reversal   45% of trades; -81.6R in 2025, -63.9R in 2024
    #   - breakout_retest  negative in all three years
    #   - range_fade       negative in all three years (small sample)
    # Real backtest, all -> without these: 2026 PF 1.08 -> 1.24, 2025 PF
    # 0.96 -> 1.02; 2024 holdout (screen) -35.1% -> +3.2%. An improvement, not
    # a proven edge. Note the freed daily slots go mostly to pattern_break,
    # which then carries ~69% of trades.
    # Only applied when no explicit playbook list is passed, so an experiment
    # asking for a playbook by name still gets it.
    disabled_playbooks: tuple = ('sweep_reversal', 'breakout_retest', 'range_fade')


@dataclass
class EngineSettings:
    # Swing detection
    swing_fractal_k: int = 2              # bars either side of a pivot
    swing_atr_mult: float = 0.55          # a leg must clear this * ATR to count
    swing_max: int = 60                   # keep the most recent N swings
    # Levels
    level_tolerance_atr: float = 0.35     # cluster width
    level_min_touches: int = 2
    level_max: int = 14
    # Trendlines
    tl_min_touches: int = 3
    tl_max_violation_atr: float = 0.45
    tl_max: int = 8

    # Geometry lookback is STRUCTURAL, not a fixed bar count: anchor the window
    # on how many recent swings it holds, and clamp it in bars at both ends.
    # 300 bars of a dead Asian session contains two swings; 300 bars of an NFP
    # afternoon contains twenty. Counting bars makes the window mean something
    # different every session - counting swings keeps its content constant.
    tl_structural_swings: int = 14        # ~7 per side, enough for 3 touches
    tl_lookback_min_bars: int = 120       # never analyse less than this
    tl_lookback_max_bars: int = 300       # matches the default chart view, so
                                          # anchors stay on screen at 1:1 zoom
    # Channels
    channel_min_containment: float = 0.72
    # Patterns
    pattern_min_quality: int = 48
    # Events
    sweep_lookback: int = 60
    sweep_reclaim_bars: int = 4
    false_break_bars: int = 5
    retest_bars: int = 12
    wick_rejection_ratio: float = 0.55    # wick / total range
    # Regime
    regime_lookback: int = 120
    vol_percentile_lookback: int = 500
    # Indicators
    atr_period: int = 14
    rsi_period: int = 14
    adx_period: int = 14
    ema_fast: int = 21
    ema_slow: int = 55
    bars_for_analysis: int = 600          # window handed to the engine


@dataclass
class ExecutionSettings:
    """
    Automatic execution through the bridge. OFF by default, and even when on
    nothing reaches MT5 unless the bridge itself was started with
    --enable-trading - two independent switches, one per process.
    """
    # Send FINAL, qualified watchlist signals without a click. Toggled at
    # runtime through /api/execution/auto and remembered in run/execution.json.
    auto: bool = False
    # Entry policy: market when price is within this many ATR of the frozen
    # entry, otherwise a pending limit/stop AT the frozen entry.
    entry_tolerance_atr: float = 0.2
    # Lot size is always clamped into this range, whatever risk sizing says.
    min_lots: float = 0.01
    max_lots: float = 0.03
    # Timeframes auto mode may send from. The Place button ignores this - it
    # limits automation, not what you can do by hand.
    auto_timeframes: tuple = ('1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d')
    # One live order or position per symbol AND timeframe.
    one_per_symbol_tf: bool = True
    # How often the executor walks the lifecycle and manages trailing stops.
    every_s: float = 2.0
    # The take-profit sent WITH the order, so MT5 itself holds it. The trailing
    # stop runs in this server; if the server or the PC is off, a position
    # keeps only what MT5 holds. Choices:
    #   'none'  SL only - exactly the tested exit, but no profit taken while
    #           the system is down
    #   'tp1'   the signal's TP1 - all out at 1R. Measured WORST of every exit
    #           in 2025 and 2026 (only ~36% of trades reach +1R); the trailing
    #           stop never acts, because MT5 closes the trade at TP1 first
    #   'tp2'   the signal's TP2 - banks the objective even with the system
    #           off, but caps every winner at TP2
    #   'cap'   a far safety target, broker_tp_r x R from entry - rarely hit,
    #           so it barely changes the tested exit
    # The trailing stop still manages the stop under any of them.
    broker_tp: str = 'tp2'
    broker_tp_r: float = 3.0


@dataclass
class AppConfig:
    host: str = field(default_factory=lambda: _env('DIANUR_HOST', '127.0.0.1'))
    port: int = field(default_factory=lambda: int(_env('DIANUR_PORT', '8770')))
    bridge_url: str = field(
        default_factory=lambda: _env('DIANUR_BRIDGE', 'http://127.0.0.1:8765'))
    symbol: str = field(default_factory=lambda: _env('DIANUR_SYMBOL', 'XAUUSD.a'))
    anthropic_key: str = field(default_factory=lambda: _env('ANTHROPIC_API_KEY', ''))
    # Trading is OFF unless the bridge itself was started with --enable-trading.
    # This flag only controls whether the UI offers the button at all.
    allow_trading_ui: bool = field(
        default_factory=lambda: _env('DIANUR_TRADING_UI', '') in ('1', 'true', 'yes'))

    instrument: InstrumentSpec = field(default_factory=InstrumentSpec)
    risk: RiskSettings = field(default_factory=RiskSettings)
    gates: GateSettings = field(default_factory=GateSettings)
    engine: EngineSettings = field(default_factory=EngineSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)

    def to_dict(self) -> dict:
        return asdict(self)


CONFIG = AppConfig()

# --------------------------------------------------------------------------- #
# Sessions, in UTC. Gold's character changes hard across these, and a scalping
# signal that ignores them will happily fire into the Sydney dead zone.
# --------------------------------------------------------------------------- #
SESSIONS = {
    'sydney':  {'open': 21, 'close': 6,  'label': 'Sydney',   'liquidity': 'thin'},
    'tokyo':   {'open': 0,  'close': 9,  'label': 'Tokyo',    'liquidity': 'moderate'},
    'london':  {'open': 7,  'close': 16, 'label': 'London',   'liquidity': 'deep'},
    'newyork': {'open': 12, 'close': 21, 'label': 'New York', 'liquidity': 'deep'},
}
# The overlap is where gold actually moves.
OVERLAP_HOURS = (12, 16)


def sessions_at(utc_hour: int) -> list:
    live = []
    for name, s in SESSIONS.items():
        o, c = s['open'], s['close']
        inside = (o <= utc_hour < c) if o < c else (utc_hour >= o or utc_hour < c)
        if inside:
            live.append(name)
    return live


def session_quality(utc_hour: int):
    """(primary session, a 0..1 multiplier on how tradeable this hour is)."""
    live = sessions_at(utc_hour)
    if OVERLAP_HOURS[0] <= utc_hour < OVERLAP_HOURS[1]:
        return 'london/newyork overlap', 1.0
    if 'london' in live:
        return 'london', 0.9
    if 'newyork' in live:
        return 'newyork', 0.85
    if 'tokyo' in live:
        return 'tokyo', 0.55
    if 'sydney' in live:
        return 'sydney', 0.3
    return 'closed', 0.15
