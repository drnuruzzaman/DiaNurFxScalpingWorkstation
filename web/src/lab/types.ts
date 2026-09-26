/**
 * The backtest lab's wire types - what server/lab/app.py sends.
 *
 * Kept apart from the live types on purpose: a lab frame is a SIMULATED
 * account at a historical bar, and nothing in the live UI should be able to
 * mistake one for the other.
 */
import type { Signal, Snapshot } from '../chart/types'

export type LabCursor = {
  v: number            // bar on screen (series index)
  t: number            // its open time
  k: number            // frontier: last bar simulated
  first: number        // first decision bar
  last: number         // last bar of the session
  frontier_t: number
  now: number          // simulated clock at that bar's close
  at_end: boolean
}

export type LabAccount = {
  balance: number
  equity: number
  floating: number
  open: number
  pending: number
  balance0: number
}

export type LabEvent = {
  seq: number
  t: number
  i: number
  kind: string
  text: string
  data?: Record<string, any>
}

export type LabTrade = {
  id: string
  n: number
  manual: boolean
  playbook: string
  side: 'buy' | 'sell'
  lots: number
  entry_t: number
  exit_t: number
  entry_i: number
  exit_i: number
  entry: number
  exit: number
  stop0: number
  sl_final: number
  tp1?: number | null
  tp2?: number | null
  tp?: number | null
  outcome: string
  r: number
  profit: number
  bars: number
  mfe_r: number
  mae_r: number
  confidence?: number | null
  kind?: string | null
}

export type LabGroup = { n: number; win?: number; sum_r?: number; exp_r?: number; net?: number; pf?: number | null }

export type LabStats = {
  n: number
  balance0: number
  net: number
  sum_r: number
  exp_r?: number
  win?: number
  pf?: number | null
  avg_win_r?: number
  avg_loss_r?: number
  best?: number
  worst?: number
  max_dd?: number
  max_dd_pct?: number
  max_losing_streak?: number
  avg_bars?: number
  end_balance?: number
  equity: [number, number][]
  by: Record<string, Record<string, LabGroup>>
  hist: [number, number][]
}

export type LabMeta = {
  id: string
  name: string
  symbol: string
  tf: string
  start: number
  end: number
  mode: 'auto' | 'manual'
  record: boolean
  created_ms: number
  updated_ms: number
  frontier_t: number
  bars_done: number
  bars_total: number
  engine_rev: string
  spec_source: string
  balance0: number
  summary: Partial<LabStats>
  changed: Record<string, Record<string, { live: any; session: any }>>
  notes: string
  tags: string[]
  run_no: number
  digits: number
  snapshots?: number
  active?: boolean
}

export type LabNote = { i: number; t: number; text: string; at: number }
export type LabSnap = { file: string; i: number; t: number; note: string; at: number }

export type LabSession = {
  meta: LabMeta
  cfg: {
    symbol: string; tf: string; start: number; end: number; mode: 'auto' | 'manual'
    name: string; record: boolean; overrides: Record<string, Record<string, any>>
    notes: string; tags: string[]
  }
  settings: Record<string, Record<string, any>>
  bars: number[][]
  first: number
  spec: { digits: number; point: number; tick_size: number; tick_value: number;
          contract_size: number; source: string }
  events: LabEvent[]
  trades: LabTrade[]
  stats: LabStats
  notes: LabNote[]
  snaps: LabSnap[]
  tags: Record<string, { tag?: string; note?: string }>
  saved: null | { frontier_t: number; trades: LabTrade[]; summary: any; engine_rev: string }
}

export type LabFrame = {
  cursor: LabCursor
  snapshot: Snapshot
  signals: Signal[]
  positions: any[]
  orders: any[]
  account: LabAccount
  ev: number
  tr: number
  forecast?: LabForecast
}

/** [P20, P50, P80] in ATR of the forecast bar. */
export type Q3 = [number, number, number]

export type LabGateYear = {
  up?: { skill: number | null; lo: number | null; hi: number | null; cov: number | null; cov_base?: number | null }
  dn?: { skill: number | null; lo: number | null; hi: number | null; cov: number | null; cov_base?: number | null }
}

export type LabGate = {
  promoted: boolean
  years: Record<string, LabGateYear>
  k_prior?: number
  half_life_months?: number | null
}

export type LabBarrierOdds = {
  target: number | null; stop: number | null; neither: number | null
  inside: boolean; target_atr: number
  /** the conditions' lift for the nearest template, on top of the baseline */
  model?: { target: number; stop: number; neither: number; template: string; why: string[]
            neff: number; promoted: boolean }
}

export type LabConfidence = 'high' | 'moderate' | 'baseline'

export type LabCondGate = {
  promoted: boolean
  years: Record<string, { skill: number | null; lo: number | null; hi: number | null
                          ece?: number | null; ece_base?: number | null }>
}

export type LabDirection = {
  p: number[]; base: number[]; lo: number; hi: number; neff: number; w: number; why: string[]
  promoted: boolean; gate: LabCondGate | null; confidence: LabConfidence
}

export type LabRegime = {
  p: number[]; base: number[]; top: number; lo: number; hi: number; neff: number; w: number
  why: string[]; promoted: boolean; gate: LabCondGate | null; confidence: LabConfidence
}

export type LabAnalogs = {
  n: number; n_eff: number; p_up?: number; up_med?: number; dn_med?: number; first_ms?: number
  why?: string[]; tp_buy?: number | null; tp_sell?: number | null
  agreement?: 'agree' | 'disagree' | 'neutral' | 'too few'
  rows: { t: number; close_ms: number; up: number; dn: number; ret_up: number; state_h: number
          in_session?: boolean }[]
}

export type LabForecastTrade = {
  kind: 'signal' | 'position'
  id: string
  side: 'buy' | 'sell'
  label?: string
  stop_atr: number
  tp1?: LabBarrierOdds
  tp2?: LabBarrierOdds
  tp?: LabBarrierOdds
}

export type LabForecastMtf = {
  tf: string; horizon: number; t: number; close: number; atr: number
  up: Q3; dn: Q3; base_up: Q3; base_dn: Q3; ratio: number; p_up: number; promoted: boolean
  p_up_model?: number | null
}

export type LabForecastSession = {
  n: number; cov_up?: number; cov_dn?: number; b_cov_up?: number; b_cov_dn?: number
  skill?: number | null
}

/** The forecast engine's read at the bar on screen (server/lab/forecast.py). */
export type LabForecast = {
  symbol?: string
  tf: string
  available: boolean
  reason?: string
  stale?: boolean
  horizon?: number
  row?: number
  t?: number
  close_ms?: number
  close?: number
  atr?: number
  /** per step k = 1..H */
  up?: Q3[]
  dn?: Q3[]
  base_up?: Q3[]
  base_dn?: Q3[]
  ratio?: number
  cell?: {
    hour: number; news: number; vol: number; vol_name: string; neff: number; w: number
    news_next_min: number; news_next: string | null; rr_w: number | null; session?: string
  }
  p_up?: number[]
  p_state?: number[]
  state_now?: number
  gate: LabGate | null
  promoted: boolean
  run_id?: string | null
  direction?: LabDirection | null
  regime?: LabRegime | null
  analogs?: LabAnalogs | null
  agreement?: { label: string; spread: number; rows: [string, number][]; promoted: boolean }
  /** Phase E: the next release's measured reaction, from releases resolved before this bar */
  news?: LabNewsReaction | null
  mtf: LabForecastMtf[]
  trades: LabForecastTrade[]
  session: LabForecastSession
}

export type LabNewsReaction = {
  kind: string; n: number; x: number; x_p25: number; x_p75: number; p_up: number; first_ms: number
}

export type LabForecastSettled = {
  i: number; t: number; up: number; dn: number
  q_up: Q3; q_dn: Q3; b_up: Q3; b_dn: Q3
  in_up: boolean; in_dn: boolean; b_in_up: boolean; b_in_dn: boolean
  loss_m: number; loss_b: number
}

export type LabForecastSummary = {
  run_id?: string
  created_utc?: string
  eval_years?: number[]
  versions?: Record<string, string>
  range?: Record<string, LabGate & { decay: { up: (number | null)[]; dn: (number | null)[] } }>
  range_conditions?: Record<string, { side: string; cond: string; group: string; skill: number;
    lo: number; hi: number; per_year: Record<string, number>; n_eff: number }[]>
  reference?: Record<string, Record<string, [number | null, number | null, number | null]>>
  trade?: Record<string, Record<string, Record<string, [number | null, number | null, number | null]>>>
  cond?: Record<string, { direction?: LabCondGate & { decay?: (number | null)[] }; state?: LabCondGate
                          trade?: LabCondGate & { templates?: Record<string, LabCondGate> } }>
}

export type LabJob = { label: string; done: number; total: number } | null

export type LabCompare = {
  same: boolean; saved_n: number; now_n: number; saved_net: number; now_net: number
  engine_then: string; engine_now: string
}

export type LabDataInfo = {
  symbols: { symbol: string; coverage: Record<string, { first_ms: number; last_ms: number }>;
             spec_source: string }[]
  timeframes: string[]
  playbooks: string[]
  speeds: number[]
  max_bars: number
}

export type SchemaRow = {
  group: string; key: string; label: string
  type: 'float' | 'int' | 'bool' | 'enum' | 'playbooks'
  lo?: number; hi?: number; options?: string[]; hint?: string
}

/** Phase E: the supervisor's challenge (server/lab/challenge.py). */
export interface LabChallenge {
  text: string
  source: 'llm' | 'engine'
  model?: string
  note?: string
  facts: string[]
  checks: string[]
  subject: { id: string; side: string; label: string } | null
  withheld?: boolean
  untraced?: string[]
  traced?: boolean
  cached?: boolean
  v?: number
  t?: number
}
