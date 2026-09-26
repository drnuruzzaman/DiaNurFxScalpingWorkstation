import React, { useEffect, useRef, useState } from 'react'
import { ChartEngine, type HoverInfo } from './ChartEngine'
import type { Bar, ForecastCone, LayoutOpts, MoneyModel, MtfTrendline, NewsHover, NewsMark, Overlays, Signal, Snapshot, TradeMark } from './types'
import { fmt } from '../lib/format'

/**
 * React wrapper around ChartEngine.
 *
 * The engine owns the canvases and all drawing; React only feeds it data and
 * renders the HTML legend on top. Keeping React out of the raster loop is the
 * whole point - re-rendering a component tree on every mouse move to move a
 * crosshair is how canvas charts end up janky.
 */
/** Parse a calendar figure like "-0.6M", "3.88%", "1.2K" into a number. */
function figure(v: string | undefined): number | null {
  if (!v) return null
  const m = String(v).replace(/,/g, '').match(/-?\d+(\.\d+)?/)
  if (!m) return null
  let n = parseFloat(m[0])
  if (/k/i.test(v)) n *= 1e3
  if (/m/i.test(v)) n *= 1e6
  if (/b/i.test(v)) n *= 1e9
  return Number.isFinite(n) ? n : null
}

/**
 * The hover card for a news mark.
 *
 * Sentiment is computed here rather than taken from a provider, because none
 * of them agree on what it means. It says only what the numbers say: actual
 * against previous. That is a statement about the PRINT, not about gold - a
 * hot CPI is "positive vs previous" and bearish for metal, so the card is
 * careful to label what it is comparing rather than implying a direction.
 */
function NewsCard({ hit, width }: { hit: NewsHover; width: number }) {
  if (!hit) return null
  const e = hit.event
  const act = figure(e.actual)
  const prev = figure(e.previous)
  const cmp = act != null && prev != null ? act - prev : null

  const when = e.minutes == null ? ''
    : e.minutes >= 1440 ? `in ${Math.round(e.minutes / 1440)}d`
      : e.minutes >= 60 ? `in ${Math.round(e.minutes / 60)}h`
        : e.minutes > 0 ? `in ${e.minutes}m`
          : e.minutes > -60 ? `${-e.minutes}m ago`
            : e.minutes > -1440 ? `${Math.round(-e.minutes / 60)}h ago`
              : `${Math.round(-e.minutes / 1440)}d ago`

  // Flip to the left of the cursor when the card would run off the pane.
  const CARD_W = 232
  const left = Math.min(Math.max(4, hit.x + 12), Math.max(4, width - CARD_W - 4))

  const Row = ({ k, children }: { k: string; children: React.ReactNode }) => (
    <div style={{ display: 'flex', gap: 8, marginTop: 3 }}>
      <span className="t-dim" style={{ flex: '0 0 66px', fontSize: 9 }}>{k}</span>
      <span style={{ fontSize: 10 }}>{children}</span>
    </div>
  )

  return (
    <div className="news-card" style={{ left, bottom: 34 }}>
      <div className="news-card-h">
        {e.currency} {e.title}
      </div>
      <Row k="WHEN">
        <span className="mono">{when}</span>
        {e.time_inferred && (
          <span className="t-dim"> · scheduled time</span>
        )}
      </Row>
      <Row k="IMPACT">
        <b className={
          e.impact === 'high' ? 't-down' : e.impact === 'medium' ? 't-warn' : 't-mid'
        }>{(e.impact || '').toUpperCase()}</b>
      </Row>
      {cmp != null && (
        <Row k="SENTIMENT">
          <b className={cmp > 0 ? 't-up' : cmp < 0 ? 't-down' : 't-mid'}>
            {cmp > 0 ? 'POSITIVE' : cmp < 0 ? 'NEGATIVE' : 'FLAT'}
          </b>
          <span className="t-dim"> vs previous</span>
        </Row>
      )}
      {(e.actual || e.forecast || e.previous) && (
        <Row k="ACTUAL">
          <b className="mono">{e.actual || '—'}</b>
          {e.forecast && <span className="t-dim mono"> f/c {e.forecast}</span>}
          {e.previous && <span className="t-dim mono"> prev {e.previous}</span>}
        </Row>
      )}
      {e.source && (
        <div className="t-dim" style={{ fontSize: 8.5, marginTop: 5 }}>
          via {e.source}
        </div>
      )}
    </div>
  )
}

/** The live chart's saved-workspace button (instrument + timeframe). */
export type WorkspaceControl = {
  label: string
  saved: boolean
  /** Saved, but the indicators have changed since. */
  dirty: boolean
  savedAt?: number
  onSave: () => void
  onRevert: () => void
  onForget: () => void
}

function SaveIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinejoin="round" aria-hidden="true">
      <path d="M2.5 2.5h9l2 2v9h-11z" />
      <path d="M5 2.5v3.5h5V2.5" />
      <rect x="5" y="9.5" width="6" height="4" />
    </svg>
  )
}

export function ChartPane({
  bars, snapshot, signal, overlays, digits, money, badge, onNeedHistory, status, onEngine,
  tfMs = 0,
  mtfLines = [], mtfSources = [], layout, news = [], positions = [], legBlocked = [],
  trades, cursorT = null, watermark = null, watermarkStyle = 'center', preferredSpan = null, chartKey, workspace,
  theme = 'glossy', forecast = null,
}: {
  bars: Bar[]
  snapshot: Snapshot | null
  signal: Signal | null
  overlays: Overlays
  digits: number
  /** Contract facts for the money figure on the signal rails. */
  money?: MoneyModel | null
  /** Higher-timeframe trendlines, already in time space. Optional: the
      backtest chart has no live board behind it to project from. */
  mtfLines?: MtfTrendline[]
  /** Which source timeframes to draw; empty means all of them. */
  mtfSources?: string[]
  /** Chart furniture: grid, news marks, position rails. */
  layout: LayoutOpts
  news?: NewsMark[]
  /** Open positions, already filtered to this chart's symbol. */
  positions?: any[]
  /** Signals on this chart the leg gate rejected (Layout -> Leg read). */
  legBlocked?: Signal[]
  /** Trades to mark - the backtest lab's closed and open trades. */
  trades?: TradeMark[]
  /** Replay cursor time: bars after it are dimmed (lab, with Reveal on). */
  cursorT?: number | null
  /** Faint text behind the chart (BACKTEST on the lab's). */
  watermark?: string | null
  /** 'brand' = the DIANURFX word at the left middle (live); 'center' = the lab's. */
  watermarkStyle?: 'center' | 'brand'
  /** The range cone to draw right of a bar (the lab's forecast engine). */
  forecast?: ForecastCone | null
  /** The zoom this chart was saved at, if its workspace is saved. */
  preferredSpan?: number | null
  /** Which chart this is (instrument|timeframe) - a change applies its saved zoom. */
  chartKey?: string
  /** The save-workspace button; omitted on charts that do not save one. */
  workspace?: WorkspaceControl
  /** UI theme name; the canvas mirrors it since it cannot read CSS vars. */
  theme?: string
  badge?: React.ReactNode
  /** Called when the view nears the oldest bar held, to fetch more. */
  onNeedHistory?: () => void
  /** Why the chart is empty, when it is. */
  status?: { state: string; message?: string; detail?: string } | null
  /** Bar length in ms; enables the candle countdown on the price axis. */
  tfMs?: number
  /** Handed the engine once it exists, so an export can re-draw the chart. */
  onEngine?: (e: ChartEngine | null) => void
}) {
  const wrap = useRef<HTMLDivElement>(null)
  const base = useRef<HTMLCanvasElement>(null)
  const top = useRef<HTMLCanvasElement>(null)
  const engine = useRef<ChartEngine | null>(null)
  const [hover, setHover] = useState<HoverInfo>(null)
  const [newsHit, setNewsHit] = useState<NewsHover>(null)
  const [paneW, setPaneW] = useState(0)

  useEffect(() => {
    if (!base.current || !top.current) return
    const e = new ChartEngine(base.current, top.current, overlays)
    e.onHover = setHover
    e.onNewsHover = setNewsHit
    engine.current = e
    onEngine?.(e)

    const ro = new ResizeObserver(() => {
      if (!wrap.current) return
      const r = wrap.current.getBoundingClientRect()
      setPaneW(r.width)
      e.resize(r.width, r.height)
    })
    ro.observe(wrap.current!)

    // A pixel-ratio change (dragging the window to another monitor, browser
    // zoom) can leave the CSS size untouched, so the observer never fires and
    // the canvas stays at the old ratio - blurred. Listen for it directly;
    // the query is re-armed each time because it matches one exact ratio.
    let mq: MediaQueryList | null = null
    const onRatio = () => {
      if (wrap.current) {
        const b = wrap.current.getBoundingClientRect()
        e.resize(b.width, b.height)
      }
      arm()
    }
    const arm = () => {
      mq?.removeEventListener('change', onRatio)
      mq = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`)
      mq.addEventListener('change', onRatio)
    }
    arm()
    const r = wrap.current!.getBoundingClientRect()
    setPaneW(r.width)
    e.resize(r.width, r.height)

    return () => {
      ro.disconnect(); mq?.removeEventListener('change', onRatio)
      e.destroy(); engine.current = null; onEngine?.(null)
    }
    // Engine is created once; overlays are pushed through the effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => { engine.current?.setBars(bars) }, [bars])
  // Re-bound each render so the callback closes over current props rather than
  // the ones captured when the engine was constructed.
  useEffect(() => {
    if (engine.current) engine.current.onNeedHistory = onNeedHistory ?? null
  }, [onNeedHistory])
  useEffect(() => { engine.current?.setSnapshot(snapshot) }, [snapshot])
  useEffect(() => { engine.current?.setSignal(signal) }, [signal])
  useEffect(() => { engine.current?.setMtfTrendlines(mtfLines) }, [mtfLines])
  useEffect(() => { engine.current?.setMtfSources(mtfSources) }, [mtfSources])
  useEffect(() => { engine.current?.setLayout(layout) }, [layout])
  useEffect(() => { engine.current?.setNews(news) }, [news])
  useEffect(() => { engine.current?.setPositions(positions) }, [positions])
  useEffect(() => { engine.current?.setLegBlocked(legBlocked) }, [legBlocked])
  useEffect(() => { engine.current?.setTrades(trades ?? []) }, [trades])
  useEffect(() => { engine.current?.setCursor(cursorT) }, [cursorT])
  useEffect(() => { engine.current?.setWatermark(watermark, watermarkStyle) }, [watermark, watermarkStyle])
  useEffect(() => { engine.current?.setForecast(forecast) }, [forecast])
  // The saved zoom is APPLIED only when a different chart opens; a new save
  // on the same chart just updates what Reset returns to.
  const lastKey = useRef<string | undefined>(undefined)
  useEffect(() => {
    const e = engine.current
    if (!e) return
    const opened = chartKey !== lastKey.current
    lastKey.current = chartKey
    e.setPreferredSpan(preferredSpan ?? null, opened && preferredSpan != null)
  }, [preferredSpan, chartKey])
  const [wsMenu, setWsMenu] = useState(false)
  const wsBox = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!wsMenu) return
    const off = (ev: MouseEvent) => {
      if (wsBox.current && !wsBox.current.contains(ev.target as Node)) setWsMenu(false)
    }
    window.addEventListener('mousedown', off)
    return () => window.removeEventListener('mousedown', off)
  }, [wsMenu])
  useEffect(() => { engine.current?.setTheme(theme) }, [theme])
  useEffect(() => { engine.current?.setOverlays(overlays) }, [overlays])
  useEffect(() => { engine.current?.setDigits(digits) }, [digits])
  useEffect(() => { engine.current?.setMoney(money ?? null) }, [money])
  useEffect(() => { engine.current?.setTimeframe(tfMs) }, [tfMs])

  const b = hover?.bar
  const last = bars.length ? bars[bars.length - 1] : null
  const shown = b ?? last
  const chg = shown ? shown.c - shown.o : 0

  return (
    <div className="chart-wrap" ref={wrap}>
      <canvas ref={base} />
      <canvas ref={top} />
      <div className="chart-legend">
        <div>
          <b>{snapshot?.symbol ?? ''}</b>{'  '}
          <span className="t-mid">{snapshot?.tf ?? ''}</span>{'  '}
          {badge}
        </div>
        {shown && (
          <div>
            <span className="t-dim">O</span> <b>{fmt(shown.o, digits)}</b>{'  '}
            <span className="t-dim">H</span> <b>{fmt(shown.h, digits)}</b>{'  '}
            <span className="t-dim">L</span> <b>{fmt(shown.l, digits)}</b>{'  '}
            <span className="t-dim">C</span> <b>{fmt(shown.c, digits)}</b>{'  '}
            <span className={chg >= 0 ? 't-up' : 't-down'}>
              {chg >= 0 ? '+' : ''}{fmt(chg, digits)}
            </span>{'  '}
            <span className="t-dim">V</span> <b>{fmt(shown.v, 0)}</b>
          </div>
        )}
        {snapshot?.ok && (
          <div className="t-dim" style={{ fontSize: 9.5 }}>
            ATR {fmt(snapshot.atr_points, 0)}pts · {snapshot.levels.length} levels ·{' '}
            {snapshot.trendlines.length} TL · {snapshot.channels.length} channels ·{' '}
            {snapshot.patterns.length} patterns · {snapshot.compute_ms}ms
          </div>
        )}
      </div>
      <NewsCard hit={newsHit} width={paneW} />
      {bars.length === 0 && (
        <div className="chart-status">
          {status?.state !== 'unknown_symbol' && <span className="chart-spin" />}
          <div className="chart-status-msg">
            {status?.message ?? 'Loading chart…'}
          </div>
          {status?.detail && (
            <div className="chart-status-detail">{status.detail}</div>
          )}
        </div>
      )}
      <div className="chart-tools">
        <button title="Jump to latest" onClick={() => engine.current?.scrollToEnd()}>⇥</button>
        <button title={workspace?.saved
          ? 'Reset chart — the saved zoom, latest bars, automatic price scale'
          : 'Reset chart — default zoom, latest bars, automatic price scale'}
          onClick={() => engine.current?.resetChart()}>⟲</button>
        {workspace && (
          <div className="ws-box" ref={wsBox}>
            <button className={`ws-btn ${workspace.saved ? 'saved' : ''}`}
              title={workspace.saved
                ? `Workspace saved for ${workspace.label}${workspace.dirty ? ' - changed since, click to save again' : ''} (Ctrl+S)`
                : `Save this workspace for ${workspace.label}: its indicators and zoom load whenever it opens - layout and theme stay the same on every chart (Ctrl+S)`}
              onClick={() => { setWsMenu(false); workspace.onSave() }}>
              <SaveIcon />
              {workspace.dirty && <i className="ws-dot" />}
            </button>
            {workspace.saved && (
              <button className="ws-more" title="More" onClick={() => setWsMenu((o) => !o)}>▾</button>
            )}
            {wsMenu && (
              <div className="ws-menu" role="menu">
                <div className="ws-menu-h">Workspace · {workspace.label}</div>
                {workspace.savedAt && (
                  <div className="ws-menu-note">saved {new Date(workspace.savedAt).toLocaleString()}</div>
                )}
                <button className="menu-item" onClick={() => { setWsMenu(false); workspace.onSave() }}>
                  Save current setup</button>
                <button className="menu-item" disabled={!workspace.dirty}
                  onClick={() => { setWsMenu(false); workspace.onRevert() }}>Revert to saved</button>
                <button className="menu-item" onClick={() => { setWsMenu(false); workspace.onForget() }}>
                  Forget saved workspace</button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
