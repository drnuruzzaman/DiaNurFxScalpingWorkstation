import React, { useEffect, useMemo, useRef, useState } from 'react'
import { ChartPane } from './chart/ChartPane'
import * as barcache from './lib/barcache'
import * as workspace from './lib/workspace'
import * as snapshot from './lib/snapshot'
import { DEFAULT_LAYOUT, DEFAULT_OVERLAYS, type Bar, type LayoutOpts, type MtfTrendline,
  type NewsMark, type Overlays, type Signal, type Snapshot } from './chart/types'
import { LiveFeed, api, mergeBars, toBars, type Board, type Health, type LivePayload, type Mt5State, type Pnl } from './lib/api'
import { dirClass, fmt, signed } from './lib/format'
import { AgentDock } from './panels/AgentDock'
import { BacktestView, EMPTY_RUN, type BacktestRun } from './panels/BacktestView'
import { BottomDock, DOCK_TABS, type DockTab } from './panels/BottomDock'
import { LeftRail } from './panels/LeftRail'
import { RightRail } from './panels/RightRail'
import { SessionFlyout } from './panels/SessionFlyout'
import { Settings } from './panels/Settings'
import { SymbolPicker } from './panels/SymbolPicker'

const TF_LIST = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '1d']
/**
 * The timeframe a chart opens on. 15m carries enough structure to read a level
 * without the noise that makes 1m look like a signal generator.
 */
const DEFAULT_TF = '15m'

/** Bar length in ms. The source of truth for history windows. */
const TF_MS: Record<string, number> = {
  '1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000,
  '30m': 1_800_000, '1h': 3_600_000, '2h': 7_200_000,
  '4h': 14_400_000, '1d': 86_400_000,
}

/**
 * The indicator catalogue, split the way the chart itself is split: things
 * drawn ON the price pane, and things that get a pane of their own.
 *
 * Each row carries a short hint saying what the overlay actually keys off,
 * because "Channels" and "Liquidity" are not self-explanatory and the cost of
 * guessing wrong is reading a level that means something else.
 */
type OverlayRow = { key: keyof Overlays; label: string; hint?: string }

const OVERLAY_GROUPS: { title: string; rows: OverlayRow[] }[] = [
  {
    title: 'Overlays',
    rows: [
      { key: 'levels', label: 'S/R levels', hint: 'price turned repeatedly' },
      { key: 'trendlines', label: 'Trendlines', hint: 'swing-anchored' },
      { key: 'channels', label: 'Channels', hint: 'parallel corridor' },
      { key: 'patterns', label: 'Patterns', hint: 'wedges, tops, H&S' },
      { key: 'swings', label: 'Swing points', hint: 'HH / HL / LH / LL' },
      { key: 'structure', label: 'BOS / CHoCH', hint: 'structure breaks' },
      { key: 'liquidity', label: 'Liquidity', hint: 'resting stops' },
      { key: 'events', label: 'Events', hint: 'macro releases' },
      { key: 'signal', label: 'Signal levels', hint: 'entry / stop / targets' },
      { key: 'zigzag', label: 'ZigZag line', hint: 'swing skeleton' },
      { key: 'regimeBands', label: 'Regime ribbon', hint: 'trending / ranging runs' },
    ],
  },
  {
    // Bands, not lines - kept in their own group because switching two of
    // them on at once is already a lot of chart, and that is easier to see
    // when they are listed together.
    title: 'Areas',
    rows: [
      { key: 'zones', label: 'Supply / demand', hint: 'bases strong moves left' },
      { key: 'fvg', label: 'Fair value gaps', hint: 'unfilled imbalances' },
      { key: 'fib', label: 'Fibonacci', hint: 'retracement of the last leg' },
    ],
  },
  {
    title: 'Higher timeframe',
    rows: [
      { key: 'mtfTrendlines', label: 'Projected trendlines', hint: 'from coarser frames' },
    ],
  },
  {
    title: 'Panes',
    rows: [
      { key: 'volume', label: 'Volume' },
      { key: 'rsi', label: 'RSI + divergence', hint: 'regular & hidden' },
      { key: 'macd', label: 'MACD', hint: '12 / 26 / 9' },
    ],
  },
]

const OVERLAY_KEYS = OVERLAY_GROUPS.flatMap((g) => g.rows.map((r) => r.key))

const THEMES: { key: string; label: string; hint: string }[] = [
  { key: 'midnight', label: 'Midnight', hint: 'near-black navy' },
  { key: 'navy', label: 'Navy', hint: 'blue, brighter' },
  { key: 'carbon', label: 'Carbon', hint: 'neutral grey' },
  { key: 'glossy', label: 'Glossy', hint: 'Ausloans, lacquered' },
]

const LAYOUT_ROWS: { key: keyof LayoutOpts; label: string; hint: string }[] = [
  { key: 'grid', label: 'Show grid', hint: 'on axis ticks' },
  { key: 'newsMarks', label: 'News marks', hint: 'high impact only' },
  { key: 'positions', label: 'Trade positions', hint: 'open entries' },
]

/**
 * localStorage, with the failure modes handled once.
 *
 * Reads are merged over a fallback rather than trusted wholesale: a stored
 * object written by an older build is missing any key added since, and
 * spreading it raw would leave those undefined - which reads as "off" for a
 * boolean and silently loses an overlay that should have been on.
 */
function loadPref<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    const parsed = JSON.parse(raw)
    return (parsed && typeof parsed === 'object' && !Array.isArray(parsed))
      ? { ...fallback, ...parsed }
      : (parsed as T)
  } catch {
    return fallback
  }
}

function savePref(key: string, value: unknown): void {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* private mode */ }
  // The workspace also lives on the server - see lib/workspace.ts.
  workspace.scheduleSave()
}

/**
 * The slim handle on a rail's inner edge. It sits BESIDE the chart, not over
 * it, so it never hides a level label or the price axis.
 */
function RailHandle({ side, onClose }: { side: 'left' | 'right'; onClose: () => void }) {
  return (
    <button className="rail-handle" onClick={onClose}
      title={`Hide the ${side} panel`} aria-label={`Hide the ${side} panel`}>
      {side === 'left' ? '\u2039' : '\u203A'}
    </button>
  )
}

/** A collapsed rail: a 22px strip with its name, click anywhere to reopen. */
function RailStrip({ side, label, onOpen }: {
  side: 'left' | 'right'; label: string; onOpen: () => void
}) {
  return (
    <button className={`rail-strip ${side}`} onClick={onOpen}
      title={`Show the ${side} panel`} aria-label={`Show the ${side} panel`}>
      <span className="rail-strip-chev">{side === 'left' ? '\u203A' : '\u2039'}</span>
      <span className="rail-strip-label">{label}</span>
    </button>
  )
}

/** One open chart. Indicators and layout are global, so a tab is just a pair. */
type ChartTab = { id: string; symbol: string; tf: string }

export default function App() {
  const [mode, setMode] = useState<'live' | 'backtest'>('live')
  const [health, setHealth] = useState<Health | null>(null)
  // Symbol and timeframe are part of "what loads". The health check below
  // still overrides a stored symbol the broker no longer offers.
  // One tab per instrument. Every tab shares the SAME global indicator and
  // layout selection - those are a considered setup, not a per-chart whim, so
  // a new tab inherits them rather than starting from defaults.
  const [tabs, setTabs] = useState<ChartTab[]>(() => {
    const saved = loadPref<ChartTab[]>('dianur.tabs.v2', [])
    if (Array.isArray(saved) && saved.length) return saved
    // v1 stored a timeframe per tab from before 15m was the default. Keep the
    // instruments - those were chosen - but let the new default apply once,
    // rather than leaving old tabs stuck on a timeframe nobody picked.
    const v1 = loadPref<ChartTab[]>('dianur.tabs', [])
    if (Array.isArray(v1) && v1.length) {
      return v1.map((t, i) => ({ id: t.id ?? `t${i}`, symbol: t.symbol, tf: DEFAULT_TF }))
    }
    return [{
      id: 't0',
      symbol: loadPref('dianur.symbol', 'XAUUSD.a'),
      tf: DEFAULT_TF,
    }]
  })
  const [activeTab, setActiveTab] = useState(() => loadPref('dianur.activeTab', 't0'))

  const active = tabs.find((t) => t.id === activeTab) ?? tabs[0]
  const symbol = active?.symbol ?? 'XAUUSD.a'
  const tf = active?.tf ?? DEFAULT_TF

  /** Change the ACTIVE tab's instrument, replacing it in place. */
  const setSymbol = (next: string) =>
    setTabs((ts) => ts.map((t) => (t.id === active?.id ? { ...t, symbol: next } : t)))
  const setTf = (next: string) =>
    setTabs((ts) => ts.map((t) => (t.id === active?.id ? { ...t, tf: next } : t)))

  /**
   * Open an instrument in its own tab, or focus the tab already showing it.
   *
   * Re-opening something already on screen should take you there, not create a
   * duplicate - that is what every editor and browser does and what the hand
   * expects.
   */
  const openTab = (nextSymbol: string, nextTf?: string) => {
    const want = nextTf ?? DEFAULT_TF
    const existing = tabs.find((t) => t.symbol === nextSymbol && t.tf === want)
    if (existing) { setActiveTab(existing.id); return }
    const id = `t${Date.now().toString(36)}`
    setTabs((ts) => [...ts, { id, symbol: nextSymbol, tf: want }])
    setActiveTab(id)
  }

  const closeTab = (id: string) => {
    setTabs((ts) => {
      if (ts.length <= 1) return ts          // never leave the workspace empty
      const i = ts.findIndex((t) => t.id === id)
      const next = ts.filter((t) => t.id !== id)
      if (id === activeTab) {
        setActiveTab((next[Math.max(0, i - 1)] ?? next[0]).id)
      }
      return next
    })
  }
  const [conn, setConn] = useState<'connecting' | 'live' | 'down'>('connecting')

  const [bars, setBars] = useState<Bar[]>([])
  const [dataStatus, setDataStatus] = useState<string>('ok')
  const [snap, setSnap] = useState<Snapshot | null>(null)
  const [signals, setSignals] = useState<Signal[]>([])
  const [quote, setQuote] = useState<any>(null)
  const [account, setAccount] = useState<any>(null)
  const [positions, setPositions] = useState<any[]>([])
  const [orders, setOrders] = useState<any[]>([])
  const [bridge, setBridge] = useState<any>(null)
  const [mt5, setMt5] = useState<Mt5State | null>(null)
  const [pnl, setPnl] = useState<Pnl | null>(null)
  const [mtfLines, setMtfLines] = useState<MtfTrendline[]>([])
  // Which higher frames to project.
  //
  // null means "no opinion yet - show whatever the board has", which is the
  // right default before the user has touched it. Once they have, the array is
  // literal, INCLUDING when it is empty: an empty list means none, not all.
  // Overloading [] to mean "all" made turning off the last source turn them
  // all back on.
  const [mtfSources, setMtfSources] = useState<string[] | null>(
    () => loadPref<string[] | null>('dianur.mtfSources', null))
  // Blanking the account figures is for screen-sharing and recordings, so the
  // choice has to survive a reload - discovering mid-stream that a refresh
  // put your balance back on screen is exactly the failure it exists to stop.
  const [hideFigures, setHideFigures] = useState(
    () => localStorage.getItem('dianur.hideFigures') === '1')
  const [board, setBoard] = useState<Board | null>(null)
  const [tradingEnabled, setTradingEnabled] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // The Indicators menu is a considered setup, not a scratch state. It used
  // to be the one menu that did NOT persist, so every reload threw the whole
  // selection away and rebuilt it from DEFAULT_OVERLAYS.
  const [overlays, setOverlaysRaw] = useState<Overlays>(
    () => loadPref('dianur.overlays', DEFAULT_OVERLAYS))

  const setOverlays = (next: Overlays) => {
    setOverlaysRaw(next)
    savePref('dianur.overlays', next)
  }
  // One open menu at a time. Two independent booleans let both dropdowns sit
  // open at once, overlapping each other.
  const [openMenu, setOpenMenu] =
    useState<'indicators' | 'layout' | 'snapshot' | null>(null)
  // Which side panels a snapshot includes. Remembered, because the answer is
  // a house style - whatever you share once you will share again.
  const [snapPanels, setSnapPanels] = useState<snapshot.PanelKey[]>(
    () => {
      // The panel keys changed when the export was redesigned; drop anything
      // saved under a key that no longer exists rather than silently skipping
      // it at draw time.
      const known = snapshot.PANEL_LABELS.map((p) => p.key)
      const saved = loadPref<snapshot.PanelKey[]>('dianur.snapPanels.v2', [])
      const clean = Array.isArray(saved) ? saved.filter((k) => known.includes(k)) : []
      return clean.length ? clean : known
    })
  const [layout, setLayout] = useState<LayoutOpts>(
    () => loadPref('dianur.layout', DEFAULT_LAYOUT))
  const [news, setNews] = useState<NewsMark[]>([])
  const [showPicker, setShowPicker] = useState(false)
  // Which instruments sit on the watchlist. Empty means "not chosen yet",
  // which falls back to whatever has history on disk - the old behaviour.
  const [watchlist, setWatchlist] = useState<string[]>(
    () => loadPref<string[]>('dianur.watchlist', []))
  const [wlQuotes, setWlQuotes] = useState<Record<string, any>>({})
  const [theme, setTheme] = useState<string>(
    () => loadPref('dianur.theme', 'glossy'))
  const [showSettings, setShowSettings] = useState(false)
  const [dockH, setDockH] = useState(180)
  // Which panels are open. Part of the saved workspace, so a layout you chose
  // - chart wide, rails away - comes back the same after a restart.
  const [panels, setPanels] = useState(
    () => loadPref('dianur.panels', { left: true, right: true, bottom: true }))
  // The bottom panel's open tab. Owned here, not in the panel, because when
  // the panel is hidden its tabs are drawn in the footer instead.
  const [dockTab, setDockTab] = useState<DockTab>(() => loadPref('dianur.dockTab', 'signals'))
  const pickDockTab = (t: DockTab) => { setDockTab(t); savePref('dianur.dockTab', t) }
  // Hover preview of a footer tab while the bottom panel is hidden. Opens
  // after a short pause - so sweeping the mouse along the footer does not
  // flash panels - and stays open while the pointer is over the preview, so
  // it can be scrolled and its rows clicked.
  const [peekTab, setPeekTab] = useState<DockTab | null>(null)
  const peekTimer = useRef<number | null>(null)
  const peekLater = (t: DockTab | null, ms: number) => {
    if (peekTimer.current != null) clearTimeout(peekTimer.current)
    peekTimer.current = window.setTimeout(() => setPeekTab(t), ms)
  }
  const peekHold = () => { if (peekTimer.current != null) clearTimeout(peekTimer.current) }
  const togglePanel = (k: 'left' | 'right' | 'bottom') =>
    setPanels((p: any) => {
      const next = { ...p, [k]: !p[k] }
      savePref('dianur.panels', next)
      return next
    })
  const [now, setNow] = useState(Date.now())
  const [toast, setToast] = useState<string | null>(null)
  /** The live chart's engine, handed over by ChartPane once it exists. */
  const chartEngine = useRef<any>(null)

  // Backtest run state is owned here so a run survives switching to LIVE and
  // back. It used to live inside BacktestView, where unmounting the tab killed
  // the poll and lost a completed run's results.
  const [btRun, setBtRun] = useState<BacktestRun>(EMPTY_RUN)
  const patchRun = (p: Partial<BacktestRun>) => setBtRun((r) => ({ ...r, ...p }))

  const menuBar = useRef<HTMLDivElement>(null)
  const feed = useRef<LiveFeed | null>(null)
  const loadingHistory = useRef(false)
  const requestedBefore = useRef(0)

  /**
   * Fetch the window of bars immediately before the oldest one held.
   *
   * The engine calls this while there is still a screenful of runway left, so
   * the drag does not stall waiting for the response.
   */
  const loadOlderBars = async () => {
    if (loadingHistory.current) return
    const first = bars.length ? bars[0].t : 0
    if (!first) return
    // Do not re-request a window we already asked for. Without this a drag
    // that keeps nudging the left edge fires the same fetch repeatedly.
    if (requestedBefore.current && first >= requestedBefore.current) return

    loadingHistory.current = true
    requestedBefore.current = first
    try {
      // Derive the window from the TIMEFRAME, not from bar spacing. Measuring
      // bars[1] - bars[0] looked reasonable and produced from_ms AFTER to_ms
      // whenever those two bars straddled a weekend or a merge boundary - an
      // inverted range that quietly returns nothing.
      const step = TF_MS[tf] ?? 60_000
      const from = first - step * 1200
      const to = first - 1
      if (from >= to) return
      const r = await api.history(symbol, tf, from, to)
      const older = toBars(r.bars).filter((b) => b.t < first)
      if (older.length) setBars((prev) => {
        const next = mergeBars(older, prev)
        barcache.put(symbol, tf, next)
        return next
      })
    } catch { /* leave the chart as it is */ } finally {
      loadingHistory.current = false
    }
  }

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  // A dropdown that only closes by clicking its own button is a trap: you
  // reach for the chart, the menu stays up, and it covers the candles you
  // were trying to look at.
  useEffect(() => {
    if (!openMenu) return
    const away = (e: MouseEvent) => {
      if (!menuBar.current?.contains(e.target as Node)) setOpenMenu(null)
    }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpenMenu(null) }
    window.addEventListener('mousedown', away)
    window.addEventListener('keydown', esc)
    return () => {
      window.removeEventListener('mousedown', away)
      window.removeEventListener('keydown', esc)
    }
  }, [openMenu])

  // The whole terminal is CSS variables, so a theme is one attribute on
  // <html>. The canvas is the exception - it cannot read those - which is why
  // the chart gets told separately.
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    savePref('dianur.theme', theme)
  }, [theme])

  useEffect(() => { savePref('dianur.symbol', symbol) }, [symbol])
  useEffect(() => { savePref('dianur.watchlist', watchlist) }, [watchlist])
  useEffect(() => { savePref('dianur.tf', tf) }, [tf])

  const setLayoutKey = (k: keyof LayoutOpts, v: boolean) => {
    const next = { ...layout, [k]: v }
    setLayout(next)
    savePref('dianur.layout', next)
  }

  // One request for the whole watchlist rather than one per row. Slower than
  // the chart's stream on purpose: these are reference prices, not something
  // anyone trades off tick by tick.
  useEffect(() => {
    const names = Array.from(new Set([...watchlistShown, symbol])).filter(Boolean)
    if (!names.length) return
    let stop = false
    const pull = () => api.quotes(names)
      .then((r) => { if (!stop) setWlQuotes(r.quotes || {}) })
      .catch(() => { /* the rail falls back to dashes */ })
    pull()
    const t = setInterval(pull, 2000)
    return () => { stop = true; clearInterval(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchlist.join(','), symbol])

  // High-impact releases only, on a slow clock. The calendar is a static file
  // behind the bridge, so polling it hard buys nothing.
  useEffect(() => {
    // Not gated on layout.newsMarks any more: that toggle now only governs the
    // dashed lines, and the labels it leaves behind still need the data.
    let stop = false
    const pull = () => api.calendar(336, 72, true)
      .then((r) => { if (!stop) setNews(r.events.filter((e) => e.impact === 'high')) })
      .catch(() => { /* marks are optional furniture */ })
    pull()
    const t = setInterval(pull, 600_000)
    return () => { stop = true; clearInterval(t) }
  }, [])

  // Poll a running backtest regardless of which tab is on screen.
  useEffect(() => {
    const id = btRun.runId
    if (!id || btRun.result) return
    let stop = false
    const timer = setInterval(async () => {
      if (stop) return
      try {
        const st = await api.backtestStatus(id)
        patchRun({ status: st })
        if (st.status === 'done') {
          clearInterval(timer)
          patchRun({ result: await api.backtestResult(id) })
        } else if (st.status === 'error') {
          clearInterval(timer)
          patchRun({ error: st.error ?? 'backtest failed' })
        }
      } catch { /* transient; keep polling */ }
    }, 900)
    return () => { stop = true; clearInterval(timer) }
  }, [btRun.runId, btRun.result])

  // One forced sweep on load: the background worker refreshes each timeframe
  // on its own cadence, so without this the slow rows (4h, 1d) would be blank
  // for the first couple of minutes.
  useEffect(() => {
    let cancelled = false
    api.signalBoard(symbol, true)
      .then((b) => { if (!cancelled) setBoard(b) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [symbol])

  useEffect(() => {
    api.health().then((h) => {
      setHealth(h)
      setMt5(h.mt5 ?? null)
      setPnl(h.pnl ?? null)
      setTradingEnabled(h.trading_enabled)
    }).catch(() => {})

    // Validate the stored symbol against the BROKER's list, not the disk one.
    // health.symbols only reports instruments with bar history saved locally -
    // one of thirteen here - so checking against it snapped the chart back to
    // gold on every reload the moment the picker let you choose anything else.
    // A live chart needs no disk history; only a backtest does.
    api.symbols().then((r) => {
      const names = r.symbols.map((x) => x.name)
      if (names.length && !names.includes(symbol)) {
        setSymbol(r.on_disk[0] ?? names[0])   // seeds the active tab
      }
    }).catch(() => { /* leave the stored symbol alone if we cannot check */ })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // --- live stream --------------------------------------------------- //
  useEffect(() => {
    if (mode !== 'live') {
      feed.current?.close()
      feed.current = null
      return
    }
    // A different symbol or timeframe is a different series; carrying the old
    // buffer forward would merge two instruments into one chart. But bars we
    // already downloaded for THIS series are still good, so seed from the
    // cache: switching back to a tab repaints instead of showing a spinner
    // while MetaTrader re-sends what we already had.
    const cached = barcache.get(symbol, tf)
    setBars(cached)
    // A fresh symbol starts as 'loading', never as 'no data'. MetaTrader
    // downloads history on demand, so an empty chart a second after a
    // symbol switch is normal and should not read like a failure.
    setDataStatus('loading')
    loadingHistory.current = false
    requestedBefore.current = 0

    /**
     * Paint the candles without waiting for the analysis.
     *
     * The live socket sends bars and snapshot in ONE frame, and the server
     * builds that frame by running the whole engine first - levels, patterns,
     * trendlines, the multi-timeframe read. Measured on this machine that is
     * 100ms on a hot symbol and up to 2.5s on one MetaTrader has not charted
     * before, while /api/bars answers in about 45ms.
     *
     * So the first sight of a new instrument used to cost a full engine pass
     * for information the candles do not need. This asks for the bars
     * directly, in parallel, and draws them the moment they land; the
     * overlays arrive a beat later when the socket's first frame does.
     *
     * MERGED, not assigned: if the socket wins the race its bars are already
     * in state and are the fresher of the two, and mergeBars is the same
     * reconciliation used for scroll-back history.
     */
    let alive = true
    if (!cached.length) {
      api.bars(symbol, tf, 600, true).then((r) => {
        if (!alive) return            // switched away again before it landed
        const early = toBars(r.bars as any)
        if (!early.length) return
        setBars((prev) => {
          const next = mergeBars(prev, early)
          barcache.put(symbol, tf, next)
          return next
        })
      }).catch(() => { /* the socket is still coming; nothing to report */ })
    }

    const f = new LiveFeed(symbol, tf)
    f.onState = setConn
    f.onMessage = (p: LivePayload) => {
      // Merge rather than replace: the socket sends a fixed tail, and
      // replacing would discard any older bars the user scrolled back to.
      setBars((prev) => {
        const next = mergeBars(prev, toBars(p.bars))
        barcache.put(symbol, tf, next)
        return next
      })
      setSnap(p.snapshot)
      setDataStatus(
        (p as any).data_status
        ?? (p.snapshot?.ok ? 'ok' : (p.snapshot as any)?.status ?? 'loading'))
      setSignals(p.signals ?? [])
      setQuote(p.quote)
      setAccount(p.account)
      setPositions(p.positions ?? [])
      setOrders(p.orders ?? [])
      setBridge(p.bridge)
      setMt5(p.mt5 ?? null)
      if (p.pnl) setPnl(p.pnl)
      setMtfLines(p.mtf_trendlines ?? [])
      if (p.board) setBoard(p.board)
      setTradingEnabled(!!(p as any).trading_enabled)
    }
    f.connect()
    feed.current = f
    return () => { alive = false; f.close(); feed.current = null }
  }, [mode, symbol, tf])

  // Keep the selected signal valid across ticks: signal ids are stable per bar,
  // so re-select by playbook+side when the bar rolls rather than losing focus.
  const selected = useMemo(() => {
    if (!signals.length) return null
    const exact = signals.find((s) => s.id === selectedId)
    if (exact) return exact
    if (!selectedId) return signals.find((s) => s.status === 'qualified') ?? signals[0] ?? null
    const [pb, side] = selectedId.split(':')
    return signals.find((s) => s.playbook === pb && s.side === side) ?? signals[0] ?? null
  }, [signals, selectedId])

  const placeOrder = async (sig: Signal) => {
    const ok = window.confirm(
      `Send this FINAL signal to MT5?\n\n` +
      `${sig.side.toUpperCase()} ${sig.symbol} ${sig.tf}  (0.01-0.03 lots)\n` +
      `Entry ${fmt(sig.entry)}  Stop ${fmt(sig.stop)}\n` +
      `At TP1 ${fmt(sig.tp1)} the stop locks +${sig.exit_plan?.lock_r ?? 0.5}R, ` +
      `then trails ${sig.exit_plan?.trail_atr ?? 1} ATR. No fixed target.\n\n` +
      `Market if price is near the entry, otherwise a pending order at it.\n` +
      `This places a real order on account ${account?.login ?? '?'}.`,
    )
    if (!ok) return
    try {
      const r = await api.placeOrder({ signal_id: sig.id, confirm: true })
      setToast(r.ok ? `Sent: ${r.message}` : `Not sent: ${r.message ?? r.error ?? 'unknown'}`)
    } catch (e: any) {
      setToast(`Not sent: ${e.message}`)
    }
    setTimeout(() => setToast(null), 6000)
  }

  // The badge answers one question: is MT5 attached right now?
  //
  // It used to blend two unrelated states - the websocket to this app, and the
  // broker session behind it - into one amber "HISTORY ONLY", which said
  // nothing about which half was down. They are reported separately now, and
  // MT5 is the one that gets the green light.
  // ONE ribbon, coloured by whether MT5 is actually attached: green means the
  // account numbers on screen are real, red means nothing below is live. The
  // account type stays in the text, and LIVE is tinted inside the ribbon so
  // "this is real money" survives the colour now meaning something else.
  const mt5Up = !!mt5?.connected
  const connColour = mt5Up ? 'var(--bull)' : 'var(--bear)'
  const acctKind = mt5?.account_type ?? 'UNKNOWN'

  /**
   * What to say when the chart has no bars.
   *
   * "no data" was the old text and it is the one answer that is usually wrong:
   * MetaTrader only holds history it has been asked for, so a blank chart after
   * picking a symbol means the download has just been triggered.
   */
  const chartStatus = useMemo(() => {
    if (bars.length) return null
    if (dataStatus === 'unknown_symbol') {
      return {
        state: 'unknown_symbol',
        message: `${symbol} is not an instrument this broker offers.`,
        detail: 'Pick another symbol from the watchlist.',
      }
    }
    if (!mt5Up) {
      return {
        state: 'offline',
        message: `No local history for ${symbol} ${tf}, and MetaTrader is offline.`,
        detail: 'Start the bridge and the terminal, then this chart fills itself.',
      }
    }
    return {
      state: 'loading',
      message: `Downloading ${symbol} ${tf} history from MetaTrader…`,
      detail: 'MT5 fetches history on demand, so the first load of an instrument '
        + 'it has not charted before takes a moment. The chart fills itself.',
    }
  }, [bars.length, dataStatus, symbol, tf, mt5Up])

  // The mirror is written on a debounce, so a close mid-window would lose the
  // last few seconds. pagehide is the one event that reliably fires on close.
  useEffect(() => {
    const flush = () => barcache.flushNow()
    window.addEventListener('pagehide', flush)
    return () => { window.removeEventListener('pagehide', flush); flush() }
  }, [])

  useEffect(() => { savePref('dianur.tabs.v2', tabs) }, [tabs])
  useEffect(() => { savePref('dianur.activeTab', activeTab) }, [activeTab])

  useEffect(() => { savePref('dianur.snapPanels.v2', snapPanels) }, [snapPanels])

  /**
   * Build the image and either copy or save it.
   *
   * The panels are DRAWN from the same data the rails render, not screenshotted
   * from the DOM - so the output is a designed card that stays legible when
   * someone views it on a phone, and it needs no html2canvas.
   */
  const takeSnapshot = (withPanels: boolean, save: boolean) => {
    // Note: NOT async on the save path. Saving has to stay inside the click's
    // user gesture, and an await before the download hands the browser an
    // excuse to drop it.
    const done = (msg: string) => {
      setToast(msg)
      setOpenMenu(null)
      setTimeout(() => setToast(null), 5000)
    }
    let canvas: HTMLCanvasElement
    const name = `dianurfx-${symbol}-${tf}-${
      new Date().toISOString().replace(/[:.]/g, '-').slice(0, 16)}.png`
    try {
      const made = snapshot.compose({
        wrap: document.querySelector('.centre .chart-wrap') as HTMLElement | null,
        engine: chartEngine.current,
        panels: withPanels ? snapPanels : [],
        symbol, tf, snap, signal: selected, digits,
      })
      if (!made) { done('Nothing to capture yet - the chart has no data.'); return }
      canvas = made
    } catch (e: any) {
      done(`Snapshot failed: ${e?.message ?? e}`)
      return
    }

    if (save) {
      // Not awaited here: saveAs opens the file dialog synchronously, which is
      // what keeps it inside the click's user gesture. Awaiting first would
      // spend the gesture and the dialog would be refused.
      snapshot.saveAs(canvas, name).then((how) => {
        done(how === 'dialog'
          ? `Saved ${name}`
          : `Saved ${name} to your downloads folder`)
      }).catch((e: any) => {
        // Cancelling the dialog is a decision, not a failure.
        if (e?.name === 'AbortError') { done('Snapshot cancelled'); return }
        done(`Could not save: ${e?.message ?? e}`)
      })
      return
    }

    // Copy is allowed to be async: the clipboard API expects a promise, and a
    // refusal there is reported rather than silently falling through.
    snapshot.copy(canvas).then((ok) => {
      if (ok) { done('Snapshot copied to the clipboard'); return }
      snapshot.saveAs(canvas, name)
        .then(() => done(`Clipboard unavailable - saved ${name} instead`))
        .catch((e: any) => {
          if (e?.name === 'AbortError') { done('Snapshot cancelled'); return }
          done(`Clipboard unavailable and save failed: ${e?.message ?? e}`)
        })
    })
  }

  const acct = account ?? {}
  const symbols = health?.symbols ?? [symbol]
  const onCount = OVERLAY_KEYS.filter((k) => overlays[k]).length
  // An explicit watchlist wins; before one is chosen, fall back to whatever
  // has history on disk so the rail is never empty on a fresh install.
  const watchlistShown = watchlist.length ? watchlist : (health?.symbols ?? [symbol])

  // The server generates signals for the watchlist only, and keeps scanning
  // it with no browser open so alerts still reach Telegram. So the list it
  // holds has to be the one you see here - pushed on every change.
  const watchKey = watchlistShown.join(',')
  useEffect(() => {
    if (!watchlistShown.length) return
    api.setWatchlist(watchlistShown).catch(() => { /* retried on next change */ })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchKey])
  // Price precision for the charted instrument. The stream's quote does not
  // always carry `digits`, and defaulting to 2 drew US30 as 51,770.00 and
  // would render any FX pair three decimals short. The polled quote always
  // has it, so it backs the stream up.
  const digits = quote?.digits ?? wlQuotes[symbol]?.digits ?? 2

  /**
   * The size the chart prices its labels at.
   *
   * A fixed reference size, NOT the size an order would actually be sent at -
   * that comes from the risk model and changes with the stop. The point of
   * the money figure on a rail is to make "how far is this target" concrete
   * in the unit a person thinks in, and a number that moved with every
   * recalculated stop would not be comparable between one signal and the next.
   *
   * Gold is a fifth of the size because one lot is 100 ounces against a
   * major's 100,000 units, so the two carry money per pip at very different
   * rates; 0.01 of gold and 0.05 of a major are the same order of magnitude.
   */
  const chartLots = /^xau/i.test(symbol) ? 0.01 : 0.05

  const money = useMemo(() => {
    const q = quote ?? wlQuotes[symbol]
    if (!q?.tick_size || !q?.tick_value) return null
    // Anything without a well-known symbol falls back to its code: "$" on an
    // AUD account is a lie that costs nothing to avoid.
    const code = acct?.currency ?? ''
    const SYMBOLS: Record<string, string> = {
      USD: '$', AUD: 'A$', NZD: 'NZ$', CAD: 'C$',
      EUR: '€', GBP: '£', JPY: '¥',
    }
    return {
      tickSize: q.tick_size,
      tickValue: q.tick_value,
      lots: chartLots,
      symbol: SYMBOLS[code] ?? (code ? `${code} ` : '$'),
    }
  }, [quote, wlQuotes, symbol, acct?.currency, chartLots])
  // Offer only frames the board has actually scanned, rather than a hardcoded
  // ladder that lists timeframes with nothing behind them.
  const mtfAvailable = useMemo(() => {
    const seen: string[] = []
    for (const m of mtfLines) if (!seen.includes(m.tf)) seen.push(m.tf)
    return seen.sort((a, b) =>
      (mtfLines.find((m) => m.tf === a)?.tf_seconds ?? 0)
      - (mtfLines.find((m) => m.tf === b)?.tf_seconds ?? 0))
  }, [mtfLines])
  // Resolve "no opinion" to everything available. This is the single place
  // that decides what "all" means, so the chart and the chips cannot disagree.
  const shownSources = mtfSources ?? mtfAvailable

  /**
   * A PERFORMANCE figure: profit today, this month.
   *
   * Never masked. Hiding the account is about not showing strangers the size
   * of the account, and a day's P&L does not reveal that - while blanking it
   * removes the one thing usually worth leaving on screen when sharing.
   */
  const perf = (v: any) => signed(v, 2)

  const toggleFigures = () => setHideFigures((v) => {
    const next = !v
    try { localStorage.setItem('dianur.hideFigures', next ? '1' : '0') } catch { /* private mode */ }
    workspace.scheduleSave()
    return next
  })

  return (
    <div className="app">
      {/* ------------------------------------------------------ top bar */}
      <header className="topbar">
        <div className="brand">
          {/* Rebuilt from logo.png as vector rather than shipping the file:
              the PNG is 117x40 with an opaque navy plate baked in, which would
              paste a light-coloured block onto a dark topbar and go soft on a
              HiDPI screen. The colours below are sampled from it. */}
          <svg className="brand-mark" viewBox="0 0 22 22" role="img" aria-label="DiaNurFx">
            <rect x="0" y="0" width="10" height="10" rx="1" fill="#171c8f" />
            <rect x="12" y="0" width="10" height="10" rx="1" fill="#ff9e1b" />
            <rect x="0" y="12" width="10" height="10" rx="1" fill="#93c90f" />
            <rect x="12" y="12" width="10" height="10" rx="1" fill="#e31c79" />
            <rect x="6" y="6" width="10" height="10" rx="1" fill="#b1b3b3" />
          </svg>
          <div className="brand-name">DIANUR<em>FX</em></div>
        </div>

        <button className="symbol-pill" onClick={() => setShowPicker(true)}
          title="Change instrument, or edit the watchlist">
          <span>{symbol}</span>
          {/* Digits from the instrument, not a constant: 2 is right for gold
              and wrong for every FX pair, which quote 5. */}
          <span className={dirClass(quote?.bid != null
            ? quote.bid - (snap?.ema_slow ?? quote.bid) : 0)}>
            {quote?.bid != null ? fmt(quote.bid, digits)
              : snap?.price != null ? fmt(snap.price, digits) : '—'}
          </span>
          <span className="t-dim" style={{ fontSize: 9 }}>▾</span>
        </button>

        <div className="tf-group">
          {TF_LIST.map((t) => (
            <button key={t} className={`tf-btn ${t === tf ? 'on' : ''}`}
              onClick={() => setTf(t)}>{t.toUpperCase()}</button>
          ))}
        </div>

        <div style={{ position: 'relative', display: 'flex', gap: 6 }} ref={menuBar}>
          <button className={`tool-btn ${openMenu === 'indicators' ? 'on' : ''}`}
            onClick={() => setOpenMenu((v) => (v === 'indicators' ? null : 'indicators'))}
            aria-expanded={openMenu === 'indicators'} aria-haspopup="menu">
            ƒ Indicators
            {onCount > 0 && <span className="tool-count">{onCount}</span>}
          </button>

          <button className={`tool-btn ${openMenu === 'layout' ? 'on' : ''}`}
            onClick={() => setOpenMenu((v) => (v === 'layout' ? null : 'layout'))}
            aria-expanded={openMenu === 'layout'} aria-haspopup="menu">
            ▦ Layout
          </button>

          {openMenu === 'layout' && (
            <div className="menu" role="menu" style={{ left: 'auto', right: 0, minWidth: 236 }}>
              <div className="menu-head">Chart</div>
              {LAYOUT_ROWS.map((r) => {
                const on = layout[r.key]
                return (
                  <button key={r.key} className={`menu-item ${on ? 'on' : ''}`}
                    role="menuitemcheckbox" aria-checked={on}
                    onClick={() => setLayoutKey(r.key, !on)}>
                    <span className="menu-check">{on ? '✓' : ''}</span>
                    <span className="menu-label">{r.label}</span>
                    <span className="menu-hint">{r.hint}</span>
                  </button>
                )
              })}
              <div className="menu-head">Theme</div>
              {THEMES.map((t) => (
                <button key={t.key} className={`menu-item ${theme === t.key ? 'on' : ''}`}
                  role="menuitemradio" aria-checked={theme === t.key}
                  onClick={() => setTheme(t.key)}>
                  <span className="menu-check">{theme === t.key ? '\u2713' : ''}</span>
                  <span className="menu-label">{t.label}</span>
                  <span className="menu-hint">{t.hint}</span>
                </button>
              ))}

              <div className="menu-note">
                Saved and reloaded with the chart.{' '}
                {layout.newsMarks && !news.length
                  ? 'No high-impact releases in range.'
                  : `${news.length} high-impact release${news.length === 1 ? '' : 's'} in the last 14d.`}
              </div>
            </div>
          )}

          <div style={{ position: 'relative' }}>
            <button className={`tool-btn ${openMenu === 'snapshot' ? 'on' : ''}`}
              onClick={() => setOpenMenu(openMenu === 'snapshot' ? null : 'snapshot')}
              title="Capture the chart, with or without the side panels">
              &#128247; Snapshot <span style={{ opacity: 0.6 }}>&#9662;</span>
            </button>
            {openMenu === 'snapshot' && (
              /* One flat menu rather than a submenu: with the toggles in
                 front of you, "chart only" is just clearing all four, so the
                 two parent entries were a level of nesting that bought
                 nothing. */
              <div className="menu snap" role="menu">
                <div className="menu-head">Include from the side panel</div>
                {snapshot.PANEL_LABELS.map((pl) => {
                  const on = snapPanels.includes(pl.key)
                  return (
                    <button key={pl.key} className="menu-item"
                      role="menuitemcheckbox" aria-checked={on}
                      onClick={() => setSnapPanels((cur) => (on
                        ? cur.filter((k) => k !== pl.key)
                        : [...cur, pl.key]))}>
                      <span className="menu-tick">{on ? '\u2713' : ''}</span>
                      {pl.label}
                    </button>
                  )
                })}

                <div className="menu-sep" />
                <button className="menu-item" onClick={() => takeSnapshot(true, false)}>
                  Copy image
                </button>
                <button className="menu-item" onClick={() => takeSnapshot(true, true)}>
                  Save image
                </button>
              </div>
            )}
          </div>

          {openMenu === 'indicators' && (
            <div className="menu" role="menu">
              {OVERLAY_GROUPS.map((g) => (
                <React.Fragment key={g.title}>
                  <div className="menu-head">{g.title}</div>
                  {g.rows.map((r) => {
                    const on = overlays[r.key]
                    return (
                      <button key={r.key} className={`menu-item ${on ? 'on' : ''}`}
                        role="menuitemcheckbox" aria-checked={on}
                        onClick={() => setOverlays({ ...overlays, [r.key]: !on })}>
                        <span className="menu-check">{on ? '✓' : ''}</span>
                        <span className="menu-label">{r.label}</span>
                        {r.hint && <span className="menu-hint">{r.hint}</span>}
                      </button>
                    )
                  })}
                </React.Fragment>
              ))}
              {overlays.mtfTrendlines && (
                <>
                  <div className="menu-head">Sources</div>
                  <div className="menu-tfs">
                    {mtfAvailable.map((t) => {
                      const on = shownSources.includes(t)
                      return (
                        <button key={t} className={`tf-chip ${on ? 'on' : ''}`}
                          onClick={() => {
                            const next = on
                              ? shownSources.filter((x) => x !== t)
                              : [...shownSources, t]
                            setMtfSources(next)
                            savePref('dianur.mtfSources', next)
                          }}>{t.toUpperCase()}</button>
                      )
                    })}
                  </div>
                  <div className="menu-note">
                    {!mtfAvailable.length
                      ? 'No coarser frame above this one.'
                      : !shownSources.length
                        ? 'No sources selected — nothing is being projected.'
                        : `${tf.toUpperCase()} is this chart's own frame; the rest project down.`}
                  </div>
                </>
              )}

              <div className="menu-sep" />
              <div className="menu-note">
                This selection is saved and reloads with the chart.
              </div>
              <div className="menu-foot">
                <button className="menu-action" onClick={() => setOverlays(
                  OVERLAY_KEYS.reduce((a, k) => ({ ...a, [k]: false }), {} as Overlays))}>
                  Remove all studies
                </button>
                {/* Removing everything with no way back is a one-way door -
                    the defaults are a specific, considered set. */}
                <button className="menu-action dim"
                  title="Discard this setup and go back to the built-in selection"
                  onClick={() => setOverlays(DEFAULT_OVERLAYS)}>Reset</button>
              </div>
            </div>
          )}
        </div>

        <div className="spacer" />


        <div
          className="conn acct"
          title={mt5?.detail ?? ''}
          style={{ color: connColour, borderColor: connColour,
                   background: `${connColour}14` }}
        >
          <span
            className={mt5?.recovering ? 'dot pulse' : 'dot'}
            style={{ background: connColour, boxShadow: `0 0 6px ${connColour}` }}
          />
          {/* The SERVER, not the login.
              A bare account number tells you nothing you can act on - you
              cannot read "is this the right broker" or "am I on the live box"
              off eight digits. The server name says both. The number itself
              is still one click away, in Settings > Risk & Gates, where it is
              wanted for reconciling a statement rather than glanced at.

              DEMO/LIVE stays. Most server names carry it, but not all do, and
              this is the one label on screen that says whether an order
              spends real money - it does not get dropped on the assumption
              that a broker named their server helpfully. */}
          {mt5Up ? (
            <>
              <span style={{ color: acctKind === 'LIVE' ? 'var(--bear)' : undefined }}>
                {acctKind}
              </span>
              {mt5?.server ? <span className="mono" style={{ opacity: 0.8, fontWeight: 400 }}>
                {' '}{mt5.server}
              </span> : null}
            </>
          ) : (
            <>
              MT5 OFFLINE
              {mt5?.server ? <span className="mono" style={{ opacity: 0.6, fontWeight: 400 }}>
                {' '}{acctKind} {mt5.server}
              </span> : null}
            </>
          )}
        </div>

        {conn !== 'live' && (
          <span className="chip chip-warn">
            {conn === 'down' ? 'STREAM DOWN' : 'CONNECTING'}
          </span>
        )}

        <div className="mode-group">
          <button className={`mode-btn live ${mode === 'live' ? 'on' : ''}`}
            onClick={() => setMode('live')}>LIVE</button>
          <button className={`mode-btn bt ${mode === 'backtest' ? 'on' : ''}`}
            onClick={() => setMode('backtest')}>BACKTEST</button>
        </div>


        <button className="tool-btn" onClick={() => setShowSettings(true)}
          title="Alerts, destinations, risk and gates">&#9881; Settings</button>
      </header>

      {/* ---------------------------------------------------------- body */}
      {mode === 'live' ? (
        <div className="body" style={{
          // Collapsed rails keep a 22px strip; open ones add a 10px handle.
          gridTemplateColumns: `${panels.left ? 'calc(var(--rail-left) + 10px)' : '22px'} 1fr `
            + `${panels.right ? 'calc(var(--rail-right) + 10px)' : '22px'}`,
        }}>
          {!panels.left ? (
            <RailStrip side="left" label="Watchlist · Fear & Greed · AI analyst"
              onOpen={() => togglePanel('left')} />
          ) : (
          <div className="rail-wrap">
          {/* Watchlist and Fear & Greed on top, the AI Analyst filling the
              rest. It moved here from the right rail, which now gives its
              full height to signals and patterns - the things you act on. */}
          <div className="rail" style={{ display: 'flex', flexDirection: 'column' }}>
            <div style={{ flex: '0 0 auto', maxHeight: '55%', overflowY: 'auto' }}>
              <LeftRail
                snap={snap} quote={quote} symbols={watchlistShown}
                quotes={wlQuotes} onEditWatchlist={() => setShowPicker(true)}
                symbol={symbol} onSymbol={openTab}
              />
            </div>
            <div style={{ flex: '1 1 auto', minHeight: 0, background: 'var(--bg-panel)', borderTop: '1px solid var(--line)' }}>
              <AgentDock snap={snap} signal={selected} symbol={symbol} tf={tf} />
            </div>
          </div>
          <RailHandle side="left" onClose={() => togglePanel('left')} />
          </div>
          )}

          <div className="centre">
            <ChartPane
              bars={bars} snapshot={snap} signal={overlays.signal ? selected : null}
              overlays={overlays} digits={digits} money={money}
              onNeedHistory={loadOlderBars} status={chartStatus}
              onEngine={(e) => { chartEngine.current = e }}
              tfMs={TF_MS[tf] ?? 0}
              mtfLines={mtfLines} mtfSources={shownSources}
              layout={layout} news={news} theme={theme}
              positions={positions.filter((p: any) => p.symbol === symbol)}
              badge={
                <>
                  {/* The trend read - direction and strength - not the regime
                      label: "TRENDING DOWN · WEAK" says which way and how hard,
                      where "RANGE" alone did not. Same read as the right panel. */}
                  {/* Plain bold text in the direction's colour, like the chart's
                      own channel labels - a small chip in a box was hard to read. */}
                  {snap?.trend?.state && (
                    <span className="trend-read" style={{
                      color: snap.trend.state.includes('up') ? 'var(--bull)'
                        : snap.trend.state.includes('down') ? 'var(--bear)'
                          : 'var(--info)',
                    }}>
                      {String(snap.trend.state).toUpperCase()}
                      {snap.trend.strength ? ` · ${String(snap.trend.strength).toUpperCase()}` : ''}
                    </span>
                  )}{' '}
                  {snap?.source?.includes('disk') && <span className="chip chip-warn">DISK</span>}
                </>
              }
            />
            <div className="chart-tabs">
              {tabs.map((t) => (
                <div
                  key={t.id}
                  className={`chart-tab ${t.id === activeTab ? 'on' : ''}`}
                  onClick={() => setActiveTab(t.id)}
                  title={`${t.symbol} ${t.tf}`}
                >
                  <span className="chart-tab-sym">{t.symbol}</span>
                  <span className="chart-tab-tf">{t.tf}</span>
                  {tabs.length > 1 && (
                    <button
                      className="chart-tab-x"
                      title="Close"
                      onClick={(e) => { e.stopPropagation(); closeTab(t.id) }}
                    >&times;</button>
                  )}
                </div>
              ))}
              <button className="chart-tab-add" title="Open another instrument"
                onClick={() => setShowPicker(true)}>+</button>
            </div>
            {panels.bottom && (
              <BottomDock
                snap={snap} signals={signals} board={board}
                positions={positions} orders={orders}
                height={dockH} onHeight={setDockH}
                onToggle={() => togglePanel('bottom')}
                tab={dockTab} onTab={pickDockTab}
                activeTf={tf} onTf={setTf}
                activeSymbol={symbol} onOpen={openTab}
              />
            )}
          </div>

          {!panels.right ? (
            <RailStrip side="right" label="Signals · Patterns · Trend"
              onOpen={() => togglePanel('right')} />
          ) : (
          <div className="rail-wrap">
          <RailHandle side="right" onClose={() => togglePanel('right')} />
          <div className="rail" style={{ display: 'flex', flexDirection: 'column' }}>
            <div style={{ flex: '1 1 auto', minHeight: 0, overflowY: 'auto' }}>
              <RightRail
                snap={snap} signals={signals} selectedId={selected?.id ?? null}
                onSelect={setSelectedId} onPlace={placeOrder}
                tradingEnabled={tradingEnabled}
              />
            </div>
          </div>
          </div>
          )}
        </div>
      ) : (
        <BacktestView symbol={symbol} timeframes={TF_LIST} overlays={overlays}
          run={btRun} onRun={patchRun} />
      )}

      {/* --------------------------------------------------- status bar */}
      <footer className={`statusbar ${mt5Up ? 'acct-up' : 'acct-down'}`}
        title="Double-click to show or hide the bottom panel"
        onDoubleClick={(e) => {
          // Not on the footer's own buttons: a quick double-tap on the eye or
          // a tab would otherwise toggle the panel as a side effect.
          if ((e.target as HTMLElement).closest('button')) return
          if (mode === 'live') togglePanel('bottom')
        }}>
        {/* Sessions, at the left-hand end. A hover card rather than a panel:
            which session is open is standing context, not something acted on,
            so the trigger carries the live dots and the detail is one hover
            away instead of holding rail height all day. */}
        <SessionFlyout snap={snap} nowMs={now} />
        {/* The bottom panel is hidden: its tabs live here instead. Clicking
            one opens the panel on that tab. */}
        {mode === 'live' && !panels.bottom && (
          <div className="foot-tabs">
            {DOCK_TABS.map((t) => (
              <button key={t.key} className={`foot-tab ${peekTab === t.key ? 'on' : ''}`}
                onClick={() => { setPeekTab(null); pickDockTab(t.key); togglePanel('bottom') }}
                onMouseEnter={() => peekLater(t.key, peekTab ? 0 : 180)}
                onMouseLeave={() => peekLater(null, 250)}>
                {t.label}
                {t.key === 'positions' && positions?.length ? ` (${positions.length})` : ''}
                {t.key === 'orders' && orders?.length ? ` (${orders.length})` : ''}
                {t.key === 'signals' && board?.signals.length ? ` (${board.signals.length})` : ''}
              </button>
            ))}
          </div>
        )}
        {/* Account figures sit at the right-hand end of the bar. */}
        <div className="spacer" />
        {/* Account figures come from MT5 and only exist when MT5 is attached.
            Showing five silent em-dashes made a disconnected terminal look
            like an account with no money in it, so the bar says which it is. */}
        {mt5Up ? (
          <>
            {/* The whole field goes, label included - not just the digits.
                A row of "BALANCE ••••" still announces that there IS a
                balance worth hiding, which is the opposite of discreet. The
                trailing separator travels with the group so nothing is left
                dangling in front of PROFIT TODAY. */}
            {!hideFigures && (
              <>
                <span>BALANCE <b>{acct.currency ?? ''} {fmt(acct.balance, 2)}</b></span>
                <span className="sep" />
                <span>FLOATING <b className={dirClass(acct.profit)}>
                  {signed(acct.profit, 2)}</b></span>
                <span className="sep" />
                <span>FREE MARGIN <b>{fmt(acct.margin_free, 2)}</b></span>
                {/* Realised only. The open swing is FLOATING, two fields to
                    the left; adding it here would double-count it and make
                    "today" tick while nothing has been banked. */}
                <span className="sep" />
              </>
            )}
            <span title={pnl ? `${pnl.today_trades} closed today (broker day)` : ''}>
              PROFIT TODAY <b className={dirClass(pnl?.today ?? 0)}>
                {perf(pnl?.today)}</b>
            </span>
            <span className="sep" />
            <span title={pnl ? `${pnl.month_trades} closed this month (broker month)` : ''}>
              THIS MONTH <b className={dirClass(pnl?.month ?? 0)}>
                {perf(pnl?.month)}</b>
            </span>
            <button className="eye-btn" onClick={toggleFigures}
              title={hideFigures
                ? 'Show balance, floating and free margin'
                : 'Hide balance, floating and free margin'}
              aria-pressed={hideFigures}>
              {hideFigures ? '🙈' : '👁'}
            </button>
            {acct.trade_allowed === false && (
              <>
                <span className="sep" />
                <span className="t-warn">ALGO TRADING OFF IN TERMINAL</span>
              </>
            )}
          </>
        ) : (
          <>
            <span className="acct-flag"><span className="dot" />MT5 OFFLINE</span>
            <span className="sep" />
            <span className="t-mid">
              {mt5?.recovering
                ? `terminal reachable (${mt5.symbols} symbols) — bridge session latched, restart the bridge`
                : 'no account data — start the bridge to populate balance, equity and margin'}
            </span>
            <span className="sep" />
            <span className="t-dim">charts running on disk history</span>
          </>
        )}

      </footer>

      {mode === 'live' && !panels.bottom && peekTab && (
        <div className="dock-peek" onMouseEnter={peekHold}
          onMouseLeave={() => peekLater(null, 250)}>
          <BottomDock
            peek tab={peekTab}
            snap={snap} signals={signals} board={board}
            positions={positions} orders={orders}
            height={Math.max(dockH, 240)} onHeight={() => {}}
            activeTf={tf} onTf={setTf}
            activeSymbol={symbol} onOpen={openTab}
          />
        </div>
      )}

      {showPicker && (
        <SymbolPicker
          current={symbol} watchlist={watchlistShown}
          onPick={openTab} onWatchlist={setWatchlist}
          onClose={() => setShowPicker(false)}
        />
      )}

      {showSettings && <Settings onClose={() => setShowSettings(false)} liveTf={tf}
        watchlist={watchlistShown} mt5={mt5} />}

      {toast && (
        <div className="panel" style={{
          position: 'fixed', right: 16, bottom: 48, zIndex: 100, padding: '10px 14px',
          borderColor: 'var(--info-dim)', boxShadow: '0 12px 32px #000a', maxWidth: 380,
        }}>
          <div className="t-hi" style={{ fontSize: 11 }}>{toast}</div>
        </div>
      )}
    </div>
  )
}
