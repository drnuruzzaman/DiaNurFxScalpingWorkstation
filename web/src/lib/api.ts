/**
 * api.ts - the single place the UI talks to the backend.
 *
 * Everything is same-origin in dev (Vite proxies /api and /ws to the Python
 * service), so there is no base URL to configure and no CORS to arrange.
 *
 * The live channel is a websocket that pushes a whole snapshot per tick. It
 * reconnects with backoff, because a trading terminal that silently stops
 * updating is worse than one that visibly disconnects.
 */

import type { Bar, MtfTrendline, Signal, Snapshot } from '../chart/types'

export type LivePayload = {
  type: 'snapshot'
  symbol: string
  tf: string
  bars: number[][]          // [t,o,h,l,c,v]
  snapshot: Snapshot
  signals: Signal[]
  quote: any
  account: any
  positions: any[]
  orders: any[]
  bridge: any
  mt5: Mt5State
  pnl: Pnl | null
  mtf_trendlines: MtfTrendline[]
  board: Board
  narrative: any
  server_ms: number
}

export type Mt5State = {
  connected: boolean
  label: string            // 'CONNECTED' | 'MT5 OFFLINE'
  account_type: 'DEMO' | 'LIVE' | 'CONTEST' | 'UNKNOWN'
  login: number | null
  server: string | null
  terminal: string | null
  symbols: number | null
  recovering: boolean
  detail: string | null
}

/** Realised P&L, bucketed on the BROKER's day and month, not the browser's. */
export type BrokerSymbol = {
  name: string
  /** The broker's own folder, e.g. "Retail\\Forex\\Majors". */
  group: string
  digits: number | null
  visible: boolean
  /** Has bar history on disk - required for a BACKTEST, not for a live chart. */
  on_disk: boolean
}

export type Quote = {
  bid: number
  ask: number
  digits?: number
  point?: number
  /**
   * Contract facts, for turning a price distance into money.
   *
   * `tick_value` is what one tick is worth on ONE lot, already in the account
   * currency - so no cross rate has to be worked out in the browser, and a
   * yen pair on an AUD account comes out right without a special case.
   */
  tick_size?: number
  tick_value?: number
  contract_size?: number
}

export type Pnl = {
  today: number
  month: number
  today_trades: number
  month_trades: number
  day_start_ms: number
  month_start_ms: number
  offset_ms: number
}

/** One closed round trip - deals already paired by position id server-side. */
export type ClosedTrade = {
  position_id: number
  symbol: string
  side: 'buy' | 'sell' | null
  volume: number
  entry: number | null
  exit: number | null
  open_ms: number | null
  close_ms: number | null
  profit: number
  commission: number
  swap: number
  net: number
  reason: number | null
  sl: number
  deals: number
  /** The MT5 order comment, e.g. "DNX 3f2a1b9c04 PULLBACK". */
  comment?: string
}

export type DealsPayload = {
  days: number
  trades: ClosedTrade[]
  pnl: Pnl
  summary: {
    count: number; wins: number; losses: number
    win_rate: number | null; net: number
    gross_win: number; gross_loss: number
    profit_factor: number | null
    best: number; worst: number
  }
}

export type CalEvent = {
  ts: number
  currency: string
  title: string
  impact: string
  forecast: string
  previous: string
  minutes: number
  /** False for FRED backfill: the DATE is known, the time of day is not. */
  time_known?: boolean
  source?: string
}

export type BoardRow = {
  symbol: string
  tf: string
  ok: boolean
  error?: string
  scanned_ms: number
  bar_time_ms?: number
  price?: number
  atr_points?: number
  regime?: string
  regime_state?: string
  regime_confidence?: number
  trend?: string
  trend_strength?: string
  mtf_score?: number
  momentum?: number
  signals: Signal[]
}

export type BoardSignal = Signal & {
  tf_seconds: number
  scanned_ms: number
  regime?: string
  regime_state?: string
}

export type Board = {
  /** The symbols the signal list covers. */
  watchlist?: string[]
  symbol: string
  timeframes: string[]
  rows: BoardRow[]
  signals: BoardSignal[]
  qualified: number
  watch: number
  scanned_ms: number
  complete: boolean
}

export type Health = {
  ok: boolean
  bridge: any
  mt5: Mt5State
  pnl: Pnl | null
  symbols: string[]
  timeframes: string[]
  trading_enabled: boolean
  trading_ui: boolean
  llm_available: boolean
  version: string
}

async function jget<T>(url: string): Promise<T> {
  const r = await fetch(url, { headers: { Accept: 'application/json' } })
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} on ${url}`)
  return (await r.json()) as T
}

async function jpost<T>(url: string, body: any): Promise<T> {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`
    try { detail = (await r.json()).detail ?? detail } catch { /* keep status */ }
    throw new Error(detail)
  }
  return (await r.json()) as T
}

export const api = {
  health: () => jget<Health>('/api/health'),

  deals: (days = 7) => jget<DealsPayload>(`/api/deals?days=${days}`),

  symbols: (q = '') =>
    jget<{ symbols: BrokerSymbol[]; total: number; on_disk: string[] }>(
      `/api/symbols?q=${encodeURIComponent(q)}`),

  quotes: (symbols: string[]) =>
    jget<{ quotes: Record<string, Quote> }>(
      `/api/quotes?symbols=${encodeURIComponent(symbols.join(','))}`),

  /**
   * `timeKnownOnly` drops date-only rows (FRED backfill has no time of day).
   * Anything that positions an event ON the time axis must pass true, or a
   * release lands at midnight and points at the wrong candle.
   */
  calendar: (backHours = 6, aheadHours = 48, timeKnownOnly = false) =>
    jget<{ events: CalEvent[]; server_ms: number; sources: Record<string, boolean> }>(
      `/api/calendar?back_hours=${backHours}&ahead_hours=${aheadHours}`
      + `&time_known_only=${timeKnownOnly ? 'true' : 'false'}`),

  bars: (symbol: string, tf: string, count = 600, live = true) =>
    jget<{ bars: number[][]; digits: number; source: string }>(
      `/api/bars?symbol=${encodeURIComponent(symbol)}&tf=${tf}&count=${count}&live=${live ? 1 : 0}`),

  analyse: (symbol: string, tf: string, live = true) =>
    jget<{ snapshot: Snapshot; signals: Signal[] }>(
      `/api/analyse?symbol=${encodeURIComponent(symbol)}&tf=${tf}&live=${live ? 1 : 0}`),

  narrative: (symbol: string, tf: string) =>
    jget<any>(`/api/narrative?symbol=${encodeURIComponent(symbol)}&tf=${tf}`),

  brief: (signalId: string, symbol: string, tf: string) =>
    jget<any>(`/api/brief?signal_id=${encodeURIComponent(signalId)}&symbol=${encodeURIComponent(symbol)}&tf=${tf}`),

  ask: (question: string, symbol: string, tf: string, signalId?: string) =>
    jpost<{ answer: string; source: string; facts?: string[] }>('/api/ask', {
      question, symbol, tf, signal_id: signalId ?? null,
    }),

  backtestStart: (cfg: Record<string, any>) =>
    jpost<{ run_id: string }>('/api/backtest/start', cfg),

  backtestStatus: (runId: string) =>
    jget<any>(`/api/backtest/status?run_id=${encodeURIComponent(runId)}`),

  backtestResult: (runId: string) =>
    jget<any>(`/api/backtest/result?run_id=${encodeURIComponent(runId)}`),

  backtestList: () => jget<{ runs: any[] }>('/api/backtest/list'),

  signalBoard: (symbol: string, refresh = false) =>
    jget<Board>(`/api/signals/board?symbol=${encodeURIComponent(symbol)}${refresh ? '&refresh=1' : ''}`),

  history: (symbol: string, tf: string, fromMs: number, toMs: number) =>
    jget<{ bars: number[][] }>(
      `/api/history?symbol=${encodeURIComponent(symbol)}&tf=${tf}&from_ms=${fromMs}&to_ms=${toMs}`),

  replayAnalyse: (symbol: string, tf: string, atMs: number) =>
    jget<{ snapshot: Snapshot; signals: Signal[]; bars: number[][] }>(
      `/api/replay?symbol=${encodeURIComponent(symbol)}&tf=${tf}&at_ms=${atMs}`),

  settings: () => jget<any>('/api/settings'),
  saveSettings: (patch: any) => jpost<any>('/api/settings', patch),

  placeOrder: (payload: any) => jpost<any>('/api/order/send', payload),
  execution: () => jget<any>('/api/execution'),
  setAuto: (enabled: boolean) =>
    jpost<{ auto: boolean; trading_enabled: boolean }>('/api/execution/auto', { enabled }),

  /** Signals are generated for these symbols only. */
  setWatchlist: (symbols: string[]) =>
    jpost<{ symbols: string[]; removed: string[] }>('/api/watchlist', { symbols }),

  alertsConfig: () => jget<any>('/api/alerts/config'),
  alertsSave: (patch: any) => jpost<any>('/api/alerts/config', patch),
  alertsWatch: (symbol: string, tf: string, enabled: boolean) =>
    jpost<any>('/api/alerts/watch', { symbol, tf, enabled }),
  alertsTest: (target?: string, bot?: string) =>
    jpost<any>('/api/alerts/test', { target: target ?? null, bot: bot ?? null }),
  news: (impact = 'high') => jget<any>(`/api/news?impact=${impact}`),
  alertsResolve: (target: string, bot?: string) =>
    jpost<any>('/api/alerts/resolve', { target, bot: bot ?? null }),
  alertsLog: (limit = 60) => jget<{ entries: any[] }>(`/api/alerts/log?limit=${limit}`),
  /** History on disk, the running update, the monthly routine (server/marketdata.py). */
  dataStatus: () => jget<any>('/api/data/status'),
  /** The running (or last) update alone - light enough for the footer to poll. */
  dataJob: () => jget<any>('/api/data/job'),
  dataUpdate: (body: { rebuild: boolean; rescore: boolean; force: boolean
    symbols?: string[]; tfs?: string[]; years?: number; download?: boolean
    enable_forecast?: boolean | string[] }) =>
    jpost<any>('/api/data/update', body),
  /** Switch the forecast engine on or off for one symbol: the status, or {ok: false, error}. */
  dataForecast: (symbol: string, on: boolean) => jpost<any>('/api/data/forecast', { symbol, on }),
  dataCancel: () => jpost<any>('/api/data/cancel', {}),
  dataRoutine: (auto: boolean) => jpost<any>('/api/data/routine', { auto }),
}

/**
 * Union two bar arrays by timestamp, newest data winning on a collision.
 *
 * Needed because the live socket pushes a fixed 600-bar tail every second: if
 * that simply replaced state, any history the user dragged into view would be
 * thrown away a second later and the chart would snap back.
 */
export function mergeBars(older: Bar[], newer: Bar[]): Bar[] {
  if (!older.length) return newer
  if (!newer.length) return older
  const byT = new Map<number, Bar>()
  for (const b of older) byT.set(b.t, b)
  for (const b of newer) byT.set(b.t, b)
  return [...byT.values()].sort((a, b) => a.t - b.t)
}

export function toBars(rows: number[][]): Bar[] {
  const out: Bar[] = new Array(rows.length)
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i]
    out[i] = { t: r[0], o: r[1], h: r[2], l: r[3], c: r[4], v: r[5] }
  }
  return out
}

/**
 * Where the live websocket connects.
 *
 * In dev it goes STRAIGHT to the Python service rather than through Vite. A
 * `ws: true` proxy entry makes Vite register its own upgrade handler, which
 * swallowed the HMR socket as well as this one - every websocket to the dev
 * server failed, including Vite's own. Bypassing the proxy removes the whole
 * class of problem; the service already allows the dev origin, and websockets
 * are not subject to CORS preflight.
 *
 * In production the UI is served BY that same Python process, so same-origin
 * is both correct and what we want.
 */
export function wsBase(): string {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  if (import.meta.env.DEV) {
    const host = import.meta.env.VITE_API_HOST ?? '127.0.0.1:8770'
    return `${proto}://${host}`
  }
  return `${proto}://${location.host}`
}

/** Live websocket with exponential backoff and a visible state. */
export class LiveFeed {
  private ws: WebSocket | null = null
  private retry = 0
  private timer: any = null
  private closed = false
  private symbol: string
  private tf: string

  onMessage: (p: LivePayload) => void = () => {}
  onState: (s: 'connecting' | 'live' | 'down') => void = () => {}

  constructor(symbol: string, tf: string) {
    this.symbol = symbol
    this.tf = tf
  }

  setStream(symbol: string, tf: string) {
    this.symbol = symbol
    this.tf = tf
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ action: 'subscribe', symbol, tf }))
    }
  }

  connect() {
    this.closed = false
    this.onState('connecting')
    const url = `${wsBase()}/ws/live?symbol=${encodeURIComponent(this.symbol)}&tf=${this.tf}`
    try {
      this.ws = new WebSocket(url)
    } catch {
      this.scheduleRetry()
      return
    }
    this.ws.onopen = () => { this.retry = 0; this.onState('live') }
    this.ws.onmessage = (ev) => {
      try { this.onMessage(JSON.parse(ev.data)) } catch { /* ignore bad frame */ }
    }
    this.ws.onerror = () => { /* onclose follows */ }
    this.ws.onclose = () => {
      this.onState('down')
      if (!this.closed) this.scheduleRetry()
    }
  }

  private scheduleRetry() {
    clearTimeout(this.timer)
    const wait = Math.min(15000, 600 * Math.pow(1.7, this.retry++))
    this.timer = setTimeout(() => this.connect(), wait)
  }

  close() {
    this.closed = true
    clearTimeout(this.timer)
    this.ws?.close()
    this.ws = null
  }
}
