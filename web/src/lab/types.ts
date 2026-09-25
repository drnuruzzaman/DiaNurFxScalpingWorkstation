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
