/**
 * labApi.ts - the backtest lab's line to its own server (server/lab, :8771).
 *
 * The lab is a separate process from the live API, so it has its own base
 * URL. REST goes to it directly (it allows this origin); the transport is a
 * websocket, connected directly as the live feed is.
 */
import type { LabChallenge, LabDataInfo, LabForecastSession, LabForecastSettled, LabForecastSummary, LabMeta, SchemaRow } from './types'

export const LAB_HTTP: string = (import.meta.env.VITE_LAB_URL as string | undefined)
  ?? 'http://127.0.0.1:8771'

export const labWsUrl = (): string => LAB_HTTP.replace(/^http/, 'ws') + '/lab/ws'

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(LAB_HTTP + path, init)
  if (!r.ok) {
    let msg = `HTTP ${r.status}`
    try { msg = (await r.json()).detail ?? msg } catch { /* not json */ }
    throw new Error(msg)
  }
  return r.json() as Promise<T>
}

/** The forecast endpoints answer for a symbol - the open session's when none is named. */
const sym = (symbol?: string) => (symbol ? `?symbol=${encodeURIComponent(symbol)}` : '')

export const lab = {
  health: () => j<{ ok: boolean; pid: number; engine_rev: string; session: string | null }>('/lab/health'),
  data: () => j<LabDataInfo>('/lab/data'),
  schema: () => j<{ schema: SchemaRow[]; live: Record<string, Record<string, any>> }>('/lab/schema'),
  sessions: () => j<{ sessions: LabMeta[] }>('/lab/sessions'),
  remove: (sid: string) => j<{ ok: boolean }>(`/lab/sessions/${sid}`, { method: 'DELETE' }),
  snapshot: (sid: string, png: string, note: string) => j<any>(`/lab/sessions/${sid}/snapshot`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ png, note }),
  }),
  snapshotUrl: (sid: string, file: string) => `${LAB_HTTP}/lab/sessions/${sid}/snapshots/${file}`,
  /** The same PNG sent as an attachment (a cross-port <a download> is ignored). */
  snapshotDownloadUrl: (sid: string, file: string) =>
    `${LAB_HTTP}/lab/sessions/${sid}/snapshots/${file}?download=1`,
  removeSnapshot: (sid: string, file: string) =>
    j<{ ok: boolean }>(`/lab/sessions/${sid}/snapshots/${file}`, { method: 'DELETE' }),
  /** A symbol's latest batch run's scorecard summary (gates, decay, calibration). */
  forecastSummary: (symbol?: string) => j<LabForecastSummary>(`/lab/forecast/summary${sym(symbol)}`),
  /** The newest Phase D filter test (tools/forecast_filtertest.py) - gold's; {} for others. */
  forecastFilterTest: (symbol?: string) => j<any>(`/lab/forecast/filtertest${sym(symbol)}`),
  /** Phase E: a symbol's news layer - its gate per timeframe and release reaction tables. */
  forecastNews: (symbol?: string) => j<any>(`/lab/forecast/news${sym(symbol)}`),
  /** Phase E: the supervisor's challenge of the forecast at the bar on screen. */
  forecastChallenge: (signalId?: string, fresh = false) => j<LabChallenge>('/lab/forecast/challenge', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ signal_id: signalId ?? null, fresh }),
  }),
  /** This session's settled forecasts and how they scored. */
  forecastLog: (limit = 300) => j<{ rows: LabForecastSettled[]; stats: LabForecastSession; tf?: string }>(
    `/lab/forecast/log?limit=${limit}`),
  exportUrl: (sid: string) => `${LAB_HTTP}/lab/sessions/${sid}/export`,
  /** Ask the LIVE API to launch the lab process if it is not running. */
  start: async (): Promise<{ ok: boolean; error?: string }> => {
    const r = await fetch('/api/lab/start', { method: 'POST' })
    return r.json()
  },
}

export type LabConn = 'offline' | 'starting' | 'connecting' | 'up' | 'down'

/**
 * The transport socket. Reconnects with backoff; every message is JSON.
 * On (re)connect it says hello, and the server answers with the open session.
 */
export class LabSocket {
  private ws: WebSocket | null = null
  private closed = false
  private retry = 0
  private timer: number | null = null

  constructor(private onMsg: (m: any) => void, private onConn: (c: LabConn) => void) {}

  connect(): void {
    this.closed = false
    this.onConn('connecting')
    const ws = new WebSocket(labWsUrl())
    this.ws = ws
    ws.onopen = () => {
      this.retry = 0
      this.onConn('up')
      ws.send(JSON.stringify({ op: 'hello' }))
    }
    ws.onmessage = (e) => {
      try { this.onMsg(JSON.parse(e.data)) } catch { /* ignore a bad frame */ }
    }
    ws.onclose = () => {
      this.ws = null
      if (this.closed) return
      this.onConn('down')
      const wait = Math.min(8000, 500 * 2 ** this.retry++)
      this.timer = window.setTimeout(() => this.connect(), wait)
    }
    ws.onerror = () => { /* onclose follows */ }
  }

  send(op: string, payload: Record<string, any> = {}): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false
    this.ws.send(JSON.stringify({ op, ...payload }))
    return true
  }

  close(): void {
    this.closed = true
    if (this.timer) window.clearTimeout(this.timer)
    this.ws?.close()
    this.ws = null
  }
}
