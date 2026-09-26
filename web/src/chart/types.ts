/**
 * Shared chart types. These mirror the JSON the analysis API emits, so a
 * snapshot can be handed to the renderer with no transformation step.
 */

export type Bar = { t: number; o: number; h: number; l: number; c: number; v: number }

export type Swing = {
  idx: number
  t: number
  price: number
  kind: 'high' | 'low'
  label: string
  strength: number
  swept: boolean
  confirmed_at: number
}

export type Level = {
  price: number
  kind: 'support' | 'resistance' | 'flip'
  touches: number
  score: number
  width: number
  first_t: number
  last_t: number
  broken: boolean
  is_round: boolean
  distance_atr: number
  sources: string[]
}

export type Trendline = {
  kind: 'support' | 'resistance'
  x1: number; y1: number; x2: number; y2: number
  t1: number; t2: number
  slope: number
  touches: number
  score: number
  broken: boolean
  price_now: number
  angle: number
  touch_idx: number[]
  age_bars: number
  distance_atr: number
}

export type ChannelLine = {
  x1: number; y1: number; x2: number; y2: number
  t1: number; t2: number; slope: number
}

export type Channel = {
  kind: 'ascending' | 'descending' | 'horizontal'
  upper: ChannelLine
  lower: ChannelLine
  slope: number
  containment: number
  touches_upper: number
  touches_lower: number
  width: number
  width_atr: number
  score: number
  position: number
  t1: number
  t2: number
}

export type PatternPoint = { t: number; price: number; role: string }

export type Pattern = {
  kind: string
  label: string
  direction: 'bullish' | 'bearish' | 'neutral'
  status: 'forming' | 'confirmed' | 'failed'
  quality: number
  relevance: number
  actionable: boolean
  points: PatternPoint[]
  break_level: number
  target: number
  invalidation: number
  rr: number
  start_t: number
  end_t: number
  notes: string[]
  zone: Record<string, number>
  distance_to_break_atr: number
  age_bars: number
}

export type MarketEvent = {
  kind: 'sweep' | 'rejection' | 'breakout' | 'retest' | 'false_break' | 'reversal'
  direction: 'up' | 'down'
  bias: 'bullish' | 'bearish'
  idx: number
  t: number
  price: number
  extreme: number
  strength: number
  confirmed: boolean
  bars_since: number
  label: string
  notes: string[]
}

export type StructureBreak = {
  idx: number; t: number; price: number
  kind: 'BOS' | 'CHoCH'
  direction: 'up' | 'down'
  from_swing: number
}

export type LiquidityPool = {
  price: number
  side: 'buyside' | 'sellside'
  count: number
  swept: boolean
  label: string
  distance_atr: number
  pull: number
}

export type Evidence = { text: string; weight: number; kind: string }
export type Gate = { name: string; verdict: 'PASS' | 'WARN' | 'BLOCK'; detail: string; penalty: number }

export type Signal = {
  id: string
  playbook: string
  label: string
  side: 'buy' | 'sell'
  symbol: string
  tf: string
  entry: number
  stop: number
  tp1: number
  tp2: number
  entry_type: 'market' | 'limit' | 'stop'
  trigger: number
  confidence: number
  rr1: number
  rr2: number
  risk_points: number
  atr_at_signal: number
  evidence: Evidence[]
  against: string[]
  invalidation: string
  expiry_bars: number
  created_ms: number
  bar_time_ms: number
  status: 'detected' | 'qualified' | 'watch' | 'rejected' | 'conflicted'
  /** Lifecycle: FORMING FINAL SENT FILLED CLOSED EXPIRED CANCELLED REVERSED. */
  stage?: string
  exit_plan?: { mode?: string; lock_r?: number; trail_atr?: number }
  qualified: boolean
  gates: Gate[]
  sizing: Record<string, any>
  reason: string
}

/**
 * The leg in progress, as the avoid-fade gate reads it (server/engine/legs.py).
 * Distances are in ATR of the bar the read was taken on.
 */
export type LegRead = {
  dir: 1 | -1
  ext_atr: number
  pull_atr: number
  depth: number | null
  start: number
  extreme: number
  close?: number
  atr?: number
  k_atr: number
  start_ms?: number
  extreme_ms?: number
  bar_ms?: number
  htf_dir?: number
  htf_tf?: string | null
  fade_side?: 'buy' | 'sell'
  /** Why a fade is blocked right now; null when it is not. */
  fade_block?: string | null
  /** Whether the gate is switched on (Settings -> Risk & Gates). */
  rules_on?: boolean
}

export type Snapshot = {
  ok: boolean
  reason?: string
  symbol: string
  tf: string
  bars: number
  source: string
  generated_ms: number
  bar_time_ms: number
  price: number
  spread_points: number | null
  atr: number
  atr_points: number
  ema_fast: number
  ema_slow: number
  trend: any
  swings: Swing[]
  breaks: StructureBreak[]
  fib: any
  pullback: Record<string, any>
  /** Leg read on this snapshot's bars (a live one includes the forming bar). */
  leg?: LegRead | null
  /** The CLOSED-bar leg read the gate actually judges on. Prefer this. */
  leg_gate?: LegRead | null
  levels: Level[]
  nearest: { above: Level | null; below: Level | null }
  session_levels: Record<string, number>
  liquidity: LiquidityPool[]
  fvg: PriceArea[]
  zones: PriceArea[]
  equal_highs: any[]
  equal_lows: any[]
  trendlines: Trendline[]
  channels: Channel[]
  regression_channel: any
  patterns: Pattern[]
  events: MarketEvent[]
  reversal: any
  regime: any
  momentum: any
  mtf: any
  volume_profile: any
  session: any
  gauge: any
  compute_ms: number
}

/** What the chart is allowed to draw. Toggled from the INDICATORS menu. */
/**
 * A trendline found on a HIGHER timeframe, expressed in time so this chart can
 * draw it.
 *
 * Bar indices are deliberately absent: bar 480 of the 4h series and bar 480 of
 * the 5m series are weeks apart, so an index is meaningless across frames.
 * `slope_ms` is price per millisecond, fixed by the line's origin and its
 * value at the source timeframe's latest bar.
 */
export type MtfTrendline = {
  tf: string
  tf_seconds: number
  kind: 'support' | 'resistance'
  t1: number
  y1: number
  t2: number
  y2: number
  slope_ms: number
  last_ms: number | null
  score: number
  touches: number
  broken: boolean
  price_now: number
}

/**
 * Chart furniture, as opposed to analysis.
 *
 * Kept apart from Overlays deliberately: those answer "what does the engine
 * think", these answer "what do I want to see on the canvas". Mixing them put
 * gridlines in the same list as BOS/CHoCH, which are not the same kind of
 * decision.
 */
export type LayoutOpts = {
  grid: boolean
  newsMarks: boolean
  positions: boolean
  /** The leg in progress and the avoid-fade rules, as the gate sees them. */
  legRead: boolean
}

export const DEFAULT_LAYOUT: LayoutOpts = {
  grid: true,
  newsMarks: true,
  positions: true,
  legRead: true,
}

/**
 * A trade drawn on the chart: closed (entry -> exit, coloured by result) or
 * open (entry rail with its live stop and target). Used by the backtest lab;
 * nothing about it is lab-specific.
 */
export type TradeMark = {
  id: string
  side: 'buy' | 'sell'
  entry_t: number
  entry: number
  exit_t?: number | null
  exit?: number | null
  sl?: number | null
  tp?: number | null
  r?: number | null
  profit?: number | null
  outcome?: string
  open?: boolean
  selected?: boolean
}

/**
 * The range cone (forecast engine, backtest lab): how far price is likely to
 * travel up and down over the next k bars, drawn to the right of the bar it
 * was forecast at. Distances are excursions from that bar's close, in its ATR.
 */
export type ForecastCone = {
  /** open time of the bar the cone starts from; its close is the anchor */
  anchorT: number
  close: number
  atr: number
  /** per step k = 1..H: [P20, P50, P80] */
  up: [number, number, number][]
  dn: [number, number, number][]
  /** the baseline's P50 per step, drawn thin to compare against (optional) */
  baseUp?: number[]
  baseDn?: number[]
  /** short tag drawn at the cone's end, e.g. 'MODEL 15m' */
  label?: string
  /** pinned: kept at its bar while the replay moves on */
  pinned?: boolean
}

/** A macro release, for the vertical marks. */
export type NewsMark = {
  ts: number
  currency: string
  title: string
  impact: string
  minutes: number
  forecast?: string
  previous?: string
  actual?: string
  source?: string
  time_known?: boolean
  time_inferred?: boolean
}

/** A news label under the cursor, with where to put the card. */
export type NewsHover = {
  event: NewsMark
  x: number
  y: number
} | null

/**
 * A band of price rather than a line: a fair value gap, or a supply/demand
 * base. Both come from server/engine/zones.py and are drawn the same way.
 */
export type PriceArea = {
  kind: 'fvg' | 'zone'
  /** fvg: bullish | bearish.  zone: demand | supply. */
  side: string
  low: number
  high: number
  mid: number
  /** Bar index in the SNAPSHOT's series, not the chart's - see snapX(). */
  idx: number
  to_idx?: number
  t: number
  size_atr?: number
  impulse_atr?: number
  /** Times price has come back to test the band since it formed. */
  touches?: number
  /** 0 untouched, 1 fully traded through. */
  filled: number
}

/**
 * What a price distance is worth, for the money figure on the signal rails.
 *
 * `tickValue` is the broker's own trade_tick_value: one tick on ONE lot, in
 * the account currency. Money is then (distance / tickSize) * tickValue *
 * lots, which needs no contract table and no cross rate.
 */
export type MoneyModel = {
  tickSize: number
  tickValue: number
  /** The size the chart prices its labels at - see LOTS_FOR in App. */
  lots: number
  /** Currency prefix, e.g. "$", "A$", "€". */
  symbol: string
}

export type Overlays = {
  mtfTrendlines: boolean
  zigzag: boolean
  macd: boolean
  levels: boolean
  trendlines: boolean
  channels: boolean
  patterns: boolean
  swings: boolean
  structure: boolean
  events: boolean
  liquidity: boolean
  /** Three-bar imbalances price has not yet filled. */
  fvg: boolean
  /** Supply and demand bases - where strong moves departed from. */
  zones: boolean
  /** Retracement levels of the current impulse leg. */
  fib: boolean
  signal: boolean
  volumeProfile: boolean
  regimeBands: boolean
  rsi: boolean
  volume: boolean
}

export const DEFAULT_OVERLAYS: Overlays = {
  mtfTrendlines: true,
  zigzag: false,
  macd: false,
  levels: true,
  trendlines: true,
  channels: true,
  patterns: true,
  // Off by default, all three. Each draws a BAND rather than a line, and
  // three sets of bands on top of the levels and channels already there is
  // an unreadable chart. They are switched on to answer a question.
  fvg: false,
  zones: false,
  fib: false,
  swings: true,
  structure: true,
  events: true,
  liquidity: true,
  signal: true,
  volumeProfile: false,
  regimeBands: true,
  rsi: true,
  volume: true,
}

export type Viewport = {
  /** index of the first visible bar (may be fractional while animating) */
  start: number
  /** number of bars visible */
  span: number
}
