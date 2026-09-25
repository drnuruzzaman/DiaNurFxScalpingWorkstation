/**
 * LabView - the backtest lab: a strategy research workstation, separate from
 * the live chart in everything but its components.
 *
 * Separate: its own server process (server/lab), its own data (history on
 * disk only), its own simulated account, its own chart instance, its own
 * saved selection of indicators (Layout and theme are system-wide, the same
 * on every chart). Nothing here reads or writes the
 * live account, and the chart says BACKTEST across it so a screenshot cannot
 * be mistaken for the live one.
 *
 * Shared: the chart engine, the indicator and layout menus, snapshots and
 * the symbol search - so the same studies look the same in both places.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { IndicatorsMenu, LAYOUT_ROWS, LayoutMenu } from '../chart/ChartMenus'
import { ChartPane } from '../chart/ChartPane'
import { DEFAULT_LAYOUT, DEFAULT_OVERLAYS, type Bar, type LayoutOpts, type Overlays,
  type TradeMark } from '../chart/types'
import { toBars } from '../lib/api'
import { fmt, signed } from '../lib/format'
import * as snapshot from '../lib/snapshot'
import * as workspace from '../lib/workspace'
import { LabDock, type DockTab } from './LabDock'
import { LabSide, type SideTab } from './LabSide'
import { LabTimeline } from './LabTimeline'
import { NewSession } from './NewSession'
import { lab, LabSocket, type LabConn } from './labApi'
import type { LabDataInfo, LabEvent, LabFrame, LabJob, LabNote, LabSession, LabSnap,
  LabStats, LabTrade, SchemaRow } from './types'
import './lab.css'

const TF_MS: Record<string, number> = {
  '1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000,
  '30m': 1_800_000, '1h': 3_600_000, '4h': 14_400_000,
}
/** News marks are today's calendar and open positions come from the lab's own trade layer. */
const LAB_LAYOUT_ROWS = LAYOUT_ROWS.filter((r) => r.key !== 'newsMarks' && r.key !== 'positions')

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    const v = JSON.parse(raw)
    return (v && typeof v === 'object' && !Array.isArray(v) && typeof fallback === 'object')
      ? { ...fallback, ...v } : v as T
  } catch { return fallback }
}
function save(key: string, v: unknown): void {
  try { localStorage.setItem(key, JSON.stringify(v)) } catch { /* private mode */ }
  workspace.scheduleSave()
}

const utc = (ms: number) => {
  const d = new Date(ms)
  return `${d.toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' })} `
    + `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
}
const fromInput = (s: string) => { const t = Date.parse(s + ':00Z'); return Number.isFinite(t) ? t : NaN }
const toInput = (ms: number) => new Date(ms).toISOString().slice(0, 16)

type MenuKey = null | 'session' | 'next' | 'indicators' | 'layout' | 'snapshot'

export function LabView({ theme, liveOverlays, layout: globalLayout, onLayout }: {
  theme: string
  liveOverlays: Overlays
  /** The system-wide Layout selection - the same on every chart. */
  layout: LayoutOpts
  onLayout: (k: keyof LayoutOpts, on: boolean) => void
}) {
  const [conn, setConn] = useState<LabConn>('connecting')
  const [data, setData] = useState<LabDataInfo | null>(null)
  const [schema, setSchema] = useState<{ schema: SchemaRow[]; live: Record<string, Record<string, any>> } | null>(null)
  const [session, setSession] = useState<LabSession | null>(null)
  const [bars, setBars] = useState<Bar[]>([])
  const [frame, setFrame] = useState<LabFrame | null>(null)
  const [events, setEvents] = useState<LabEvent[]>([])
  const [trades, setTrades] = useState<LabTrade[]>([])
  const [stats, setStats] = useState<LabStats | null>(null)
  const [notes, setNotes] = useState<LabNote[]>([])
  const [snaps, setSnaps] = useState<LabSnap[]>([])
  const [tags, setTags] = useState<Record<string, { tag?: string; note?: string }>>({})
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(4)
  const [job, setJob] = useState<LabJob>(null)
  const [banner, setBanner] = useState<{ kind: 'ok' | 'warn' | 'err'; text: string } | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [reveal, setReveal] = useState(false)
  const [overlays, setOverlaysRaw] = useState<Overlays>(
    () => load('dianur.lab.overlays', liveOverlays ?? DEFAULT_OVERLAYS))
  // Layout is system-wide (set here or on the live chart, it is the same
  // everywhere). News marks are today's calendar and positions the live
  // account, so on a replay those two are always off.
  const layout = useMemo<LayoutOpts>(
    () => ({ ...DEFAULT_LAYOUT, ...globalLayout, newsMarks: false, positions: false }), [globalLayout])
  const [menu, setMenu] = useState<MenuKey>(null)
  const [showNew, setShowNew] = useState(false)
  const [sideTab, setSideTab] = useState<SideTab>('now')
  const [dockTab, setDockTabRaw] = useState<DockTab>(() => load<DockTab>('dianur.lab.dock', 'trades'))
  const [dockOpen, setDockOpen] = useState(true)
  const [dockH, setDockH] = useState<number>(() => load<number>('dianur.lab.dockH', 272))
  const [selTrade, setSelTrade] = useState<string | null>(null)
  const [selSignal, setSelSignal] = useState<string | null>(null)
  const [gotoText, setGotoText] = useState('')
  const [sessionsKey, setSessionsKey] = useState(0)
  const [recent, setRecent] = useState<any[]>([])
  const sock = useRef<LabSocket | null>(null)
  const engine = useRef<any>(null)
  const centre = useRef<HTMLDivElement>(null)
  const menuBox = useRef<HTMLDivElement>(null)
  const toastTimer = useRef<number | null>(null)

  const setOverlays = (o: Overlays) => { setOverlaysRaw(o); save('dianur.lab.overlays', o) }
  const setLayoutKey = (k: keyof LayoutOpts, on: boolean) => onLayout(k, on)
  const setDockTab = (t: DockTab) => { setDockTabRaw(t); setDockOpen(true); save('dianur.lab.dock', t) }
  const flash = (msg: string) => {
    setToast(msg)
    if (toastTimer.current) window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(null), 4500)
  }

  // ------------------------------------------------------------ messages
  const handler = useRef<(m: any) => void>(() => {})
  handler.current = (m: any) => {
    switch (m.type) {
      case 'state':
        setPlaying(!!m.playing)
        if (m.speed) setSpeed(m.speed)
        setJob(m.job ?? null)
        // The lab came back without the session on screen (it was restarted).
        // A recorded session is reopened - it re-runs to where it was - and an
        // unrecorded one is gone, so the screen stops pretending otherwise.
        if (m.session == null && session && !m.job) {
          if (session.cfg.record) {
            setBanner({ kind: 'warn', text: 'The lab restarted - reopening your session and re-running it to where you were.' })
            sock.current?.send('open', { sid: session.meta.id })
          } else {
            setSession(null); setFrame(null); setBars([])
            setBanner({ kind: 'warn', text: 'The lab restarted and this session was not recorded, so it is gone.' })
          }
        }
        break
      case 'session':
        setSession(m)
        setBars(toBars(m.bars))
        setEvents(m.events ?? [])
        setTrades(m.trades ?? [])
        setStats(m.stats ?? null)
        setNotes(m.notes ?? [])
        setSnaps(m.snaps ?? [])
        setTags(m.tags ?? {})
        setSelTrade(null)
        setSelSignal(null)
        setSessionsKey((k) => k + 1)
        break
      case 'frame':
        setFrame(m)
        if (m.events?.length) setEvents((ev) => [...ev, ...m.events])
        if (m.trades?.length) setTrades((tr) => [...tr, ...m.trades])
        if (m.stats) setStats(m.stats)
        break
      case 'progress':
        setJob({ label: m.label, done: m.done, total: m.total })
        break
      case 'compare':
        setBanner(m.same
          ? { kind: 'ok', text: `Re-ran on engine ${m.engine_now}: identical to the recording - ${m.now_n} trades, ${signed(m.now_net, 2)}.` }
          : { kind: 'warn', text: `Re-ran on engine ${m.engine_now}: different from the recording (engine ${m.engine_then}) - ${m.saved_n} → ${m.now_n} trades, ${signed(m.saved_net, 2)} → ${signed(m.now_net, 2)}. The engine, the settings file or the data changed since it was recorded.` })
        break
      case 'result':
        if (!m.ok) flash(`${m.op}: ${m.error}`)
        else if (m.op === 'order') flash('Filled (simulated)')
        else if (m.op === 'take') flash('Signal sent (simulated)')
        break
      case 'notes': setNotes(m.notes ?? []); break
      case 'tags': setTags(m.tags ?? {}); break
      case 'snaps': setSnaps(m.snaps ?? []); break
      case 'meta':
        setSession((s) => (s ? { ...s, meta: m.meta, cfg: { ...s.cfg, name: m.meta.name, notes: m.meta.notes } } : s))
        if (m.saved) flash('Session saved')
        setSessionsKey((k) => k + 1)
        break
      case 'closed':
        setSession(null); setFrame(null); setBars([]); setEvents([]); setTrades([])
        setStats(null); setNotes([]); setSnaps([]); setTags({})
        break
      case 'error':
        flash(m.message)
        break
    }
  }
  const onMsg = useCallback((m: any) => handler.current(m), [])

  // ------------------------------------------------------------- connect
  useEffect(() => {
    let stop = false
    const s = new LabSocket(onMsg, (c) => { if (!stop) setConn(c) })
    sock.current = s
    ;(async () => {
      const running = await fetch('/api/lab/status').then((r) => r.json())
        .then((d) => !!d.running).catch(() => false)
      if (!running) {
        setConn('starting')
        const r = await lab.start().catch((e) => ({ ok: false, error: String(e?.message ?? e) }))
        if (!r.ok) {
          if (!stop) {
            setConn('offline')
            setBanner({ kind: 'err', text: `The lab server could not be started: ${r.error ?? 'unknown error'}. Run: python -m server.lab` })
          }
          return
        }
      }
      if (stop) return
      s.connect()
      lab.data().then((d) => { if (!stop) setData(d) }).catch(() => {})
      lab.schema().then((d) => { if (!stop) setSchema(d) }).catch(() => {})
      lab.sessions().then((d) => { if (!stop) setRecent(d.sessions.slice(0, 6)) }).catch(() => {})
    })()
    return () => { stop = true; s.close() }
  }, [onMsg])

  // The lab process gone (closed, crashed, machine restarted): ask the live
  // API to start it again. Idempotent - it does nothing when the lab is up -
  // and the socket's own backoff reconnects once it answers.
  useEffect(() => {
    if (conn !== 'down') return
    const t = window.setTimeout(() => { lab.start().catch(() => {}) }, 2500)
    return () => window.clearTimeout(t)
  }, [conn])

  useEffect(() => {
    if (!menu) return
    const off = (e: MouseEvent) => {
      if (menuBox.current && !menuBox.current.contains(e.target as Node)) setMenu(null)
    }
    window.addEventListener('mousedown', off)
    return () => window.removeEventListener('mousedown', off)
  }, [menu])

  // ------------------------------------------------------------ derived
  const cur = frame?.cursor ?? null
  const v = cur?.v ?? 0
  const digits = session?.spec.digits ?? 2
  const atFrontier = !!cur && cur.v === cur.k
  const busy = !!job
  const shownBars = useMemo(
    () => (!cur ? bars : reveal ? bars : bars.slice(0, cur.v + 1)), [bars, cur?.v, reveal])
  const sigs = frame?.signals ?? []
  const shownSignal = useMemo(() => {
    const pick = sigs.find((s) => s.id === selSignal)
    if (pick) return pick
    return sigs.find((s) => s.stage === 'SENT' || s.stage === 'FILLED')
      ?? sigs.find((s) => s.status === 'qualified') ?? null
  }, [sigs, selSignal])
  const legBlocked = useMemo(() => sigs.filter((s) => s.gates?.some((g) => g.name === 'leg' && g.verdict === 'BLOCK')), [sigs])
  const marks = useMemo<TradeMark[]>(() => {
    if (!cur) return []
    const out: TradeMark[] = trades.filter((t) => reveal || t.exit_i <= cur.v).map((t) => ({
      id: t.id, side: t.side, entry_t: t.entry_t, entry: t.entry, exit_t: t.exit_t, exit: t.exit,
      r: t.r, profit: t.profit, outcome: t.outcome, selected: t.id === selTrade,
    }))
    for (const p of frame?.positions ?? []) {
      out.push({ id: `pos-${p.ticket}`, side: p.side, entry_t: p.time_ms, entry: p.price_open,
                 sl: p.sl, tp: p.tp, profit: p.profit, open: true })
    }
    return out
  }, [trades, cur?.v, reveal, selTrade, frame])
  const money = useMemo(() => (session ? {
    tickSize: session.spec.tick_size, tickValue: session.spec.tick_value,
    lots: session.settings?.execution?.lots_gold ?? 0.01, symbol: '',
  } : null), [session])
  const selectedTrade = useMemo(() => trades.find((t) => t.id === selTrade) ?? null, [trades, selTrade])

  // ------------------------------------------------------------ actions
  const send = (op: string, payload: Record<string, any> = {}) => {
    if (!sock.current?.send(op, payload)) flash('The lab is not connected.')
  }
  const step = (n: number) => send('step', { n })
  const togglePlay = () => (playing ? send('pause') : send('play', { speed }))
  const seekBar = (i: number) => send('seek', { i })
  const seekTime = (t: number) => send('seek', { t })
  const pickTrade = (t: LabTrade) => {
    setSelTrade(t.id)
    setSideTab('trade')
    seekBar(t.entry_i)
  }
  const configure = (patch: Record<string, any>) => {
    if (patch.keepRun) {
      if (patch.name !== undefined) send('rename', { name: patch.name })
      if (patch.notes !== undefined) send('annotate', { notes: patch.notes })
      return
    }
    send('configure', { patch })
  }

  const composeSnap = () => {
    if (!session || !frame) return null
    const wrap = centre.current?.querySelector('.chart-wrap') as HTMLElement | null
    return snapshot.compose({
      wrap, engine: engine.current, panels: [], symbol: session.cfg.symbol, tf: session.cfg.tf,
      snap: frame.snapshot, signal: shownSignal, digits,
      badge: 'BACKTEST · SIMULATED', when: `${utc(frame.cursor.t)} UTC · replay`,
    })
  }
  const snapToSession = async () => {
    setMenu(null)
    const c = composeSnap()
    if (!c || !session) { flash('Nothing to capture yet.'); return }
    try {
      await lab.snapshot(session.meta.id, c.toDataURL('image/png'), '')
      flash('Snapshot saved in the session - see the Snapshots tab')
    } catch (e: any) { flash(`Snapshot not saved: ${e?.message ?? e}`) }
  }
  const snapCopy = () => {
    setMenu(null)
    const c = composeSnap()
    if (!c) return
    snapshot.copy(c).then((ok) => flash(ok ? 'Snapshot copied' : 'Clipboard unavailable'))
  }
  const snapFile = () => {
    setMenu(null)
    const c = composeSnap()
    if (!c || !session || !frame) return
    const name = `dianurfx-backtest-${session.cfg.symbol}-${session.cfg.tf}-${toInput(frame.cursor.t).replace(/[:T]/g, '-')}.png`
    snapshot.saveAs(c, name).then(() => flash(`Saved ${name}`)).catch(() => {})
  }

  // ----------------------------------------------------------- keyboard
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      const tag = el?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el?.isContentEditable) return
      if (!session || showNew || e.ctrlKey || e.metaKey || e.altKey) return
      const k = e.key
      if (k === ' ') { e.preventDefault(); togglePlay() }
      else if (k === 'ArrowRight') { e.preventDefault(); step(e.shiftKey ? 10 : 1) }
      else if (k === 'ArrowLeft') { e.preventDefault(); step(e.shiftKey ? -10 : -1) }
      else if (k === 'End') { e.preventDefault(); send('frontier') }
      else if (k === 'Home') { e.preventDefault(); send('first') }
      else if (k === 'n' || k === 'N') send('next', { what: 'signal' })
      else if (k === 't' || k === 'T') send('next', { what: 'trade' })
      else if (k === 'r' || k === 'R') setReveal((x) => !x)
      else if (k === 's' || k === 'S') snapToSession()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // ------------------------------------------------------------- render
  const pct = job && job.total ? Math.min(100, Math.round((job.done / job.total) * 100)) : 0
  const tfMs = session ? TF_MS[session.cfg.tf] ?? 0 : 0

  return (
    <div className="lab">
      <div className="lab-bar" ref={menuBox}>
        <span className="lab-badge" title="Everything here is simulated on history. Nothing reaches MT5.">BACKTEST LAB</span>
        {conn !== 'up' && (
          <span className={`chip ${conn === 'offline' || conn === 'down' ? 'chip-down' : 'chip-warn'}`}>
            {conn === 'starting' ? 'STARTING LAB…' : conn === 'connecting' ? 'CONNECTING' : conn === 'down' ? 'LAB DISCONNECTED' : 'LAB OFFLINE'}
          </span>
        )}
        {session ? (
          <>
            <div className="lab-pop">
              <button className={`tool-btn lab-sess ${menu === 'session' ? 'on' : ''}`}
                onClick={() => setMenu(menu === 'session' ? null : 'session')} title={session.meta.name}>
                <span className="lab-sess-name">{session.meta.name}</span> <span className="t-dim">▾</span>
              </button>
              {menu === 'session' && (
                <div className="menu" role="menu" style={{ minWidth: 220 }}>
                  <button className="menu-item" onClick={() => { setMenu(null); setShowNew(true) }}>New session…</button>
                  <button className="menu-item" onClick={() => { setMenu(null); setDockTab('sessions') }}>Saved sessions…</button>
                  <div className="menu-sep" />
                  <button className="menu-item" onClick={() => { setMenu(null); send('save') }}>Save now</button>
                  <button className="menu-item" onClick={() => {
                    setMenu(null)
                    if (window.confirm('Restart this session from its first bar? This run\'s trades are replaced.')) send('restart')
                  }}>Restart this run</button>
                  <a className="menu-item" href={lab.exportUrl(session.meta.id)} onClick={() => setMenu(null)}>Export (JSON)</a>
                  <div className="menu-sep" />
                  <button className="menu-item" onClick={() => { setMenu(null); send('close') }}>Close session</button>
                </div>
              )}
            </div>
            <span className="lab-mkt mono">{session.cfg.symbol} · {session.cfg.tf}</span>
            <span className={`chip ${session.cfg.mode === 'auto' ? 'chip-info' : 'chip-warn'}`}
              title={session.cfg.mode === 'auto' ? 'The system trades, exactly as live would' : 'You trade; the engine advises'}>
              {session.cfg.mode === 'auto' ? 'SYSTEM' : 'MANUAL'}
            </span>

            <div className="lab-transport">
              <button title="First bar (Home)" onClick={() => send('first')} disabled={busy}>⏮</button>
              <button title="Back 10 bars (Shift+←)" onClick={() => step(-10)} disabled={busy}>◀◀</button>
              <button title="Back one bar (←)" onClick={() => step(-1)} disabled={busy}>◀</button>
              <button className={`lab-play ${playing ? 'on' : ''}`} title="Play / pause (Space)" onClick={togglePlay}
                disabled={busy && !playing}>{playing ? '❚❚' : '▶'}</button>
              <button title="Forward one bar (→)" onClick={() => step(1)} disabled={busy}>▶︎|</button>
              <button title="Forward 10 bars (Shift+→)" onClick={() => step(10)} disabled={busy}>▶▶</button>
              <button title="Latest simulated bar (End)" onClick={() => send('frontier')} disabled={busy}>⏭</button>
            </div>
            <select className="lab-speed" value={speed} title="Play speed, bars per second"
              onChange={(e) => {
                const sp = +e.target.value
                setSpeed(sp)
                send('speed', { speed: sp })
              }}>
              {(data?.speeds ?? [1, 2, 4, 8, 16, 32, 64]).map((s) => (
                <option key={s} value={s}>{s === 64 ? 'max' : `${s}`} bar{s === 1 ? '' : 's'}/s</option>
              ))}
            </select>
            <div className="lab-pop">
              <button className={`tool-btn ${menu === 'next' ? 'on' : ''}`} disabled={busy}
                onClick={() => setMenu(menu === 'next' ? null : 'next')}>Next ▾</button>
              {menu === 'next' && (
                <div className="menu" role="menu" style={{ minWidth: 190 }}>
                  {([['signal', 'Next signal', 'N'], ['order', 'Next order'], ['fill', 'Next fill'],
                     ['exit', 'Next exit'], ['trade', 'Next trade event', 'T']] as string[][]).map(([w, label, key]) => (
                    <button key={w} className="menu-item" onClick={() => { setMenu(null); send('next', { what: w }) }}>
                      <span className="menu-label">{label}</span>{key && <span className="menu-hint">{key}</span>}
                    </button>
                  ))}
                  <div className="menu-sep" />
                  <button className="menu-item" onClick={() => { setMenu(null); send('end') }}>
                    <span className="menu-label">Run to the end</span><span className="menu-hint">fast, no frames</span>
                  </button>
                </div>
              )}
            </div>
            <div className="lab-goto">
              <input type="datetime-local" value={gotoText} title="Go to a date/time (UTC)"
                onChange={(e) => setGotoText(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && Number.isFinite(fromInput(gotoText))) seekTime(fromInput(gotoText)) }} />
              <button className="tool-btn" disabled={busy || !Number.isFinite(fromInput(gotoText))}
                onClick={() => seekTime(fromInput(gotoText))}>Go</button>
            </div>
            <button className={`tool-btn ${reveal ? 'on' : ''}`} title="Show the bars after the cursor, dimmed (R). The engine never sees them."
              onClick={() => setReveal((x) => !x)}>{reveal ? '◉' : '○'} Future</button>
            <div className="lab-pop">
              <button className={`tool-btn ${menu === 'indicators' ? 'on' : ''}`}
                onClick={() => setMenu(menu === 'indicators' ? null : 'indicators')}>ƒ Indicators</button>
              {menu === 'indicators' && (
                <IndicatorsMenu overlays={overlays} onChange={setOverlays} exclude={['mtfTrendlines']} />
              )}
            </div>
            <div className="lab-pop">
              <button className={`tool-btn ${menu === 'layout' ? 'on' : ''}`}
                onClick={() => setMenu(menu === 'layout' ? null : 'layout')}>▦ Layout</button>
              {menu === 'layout' && (
                <LayoutMenu layout={layout} onToggle={setLayoutKey} rows={LAB_LAYOUT_ROWS}
                  note="Layout is the same on every chart, live and backtest." />
              )}
            </div>
            <div className="lab-pop">
              <button className={`tool-btn ${menu === 'snapshot' ? 'on' : ''}`}
                onClick={() => setMenu(menu === 'snapshot' ? null : 'snapshot')}>📷 Snapshot ▾</button>
              {menu === 'snapshot' && (
                <div className="menu" role="menu" style={{ minWidth: 200, left: 'auto', right: 0 }}>
                  <button className="menu-item" onClick={snapToSession}><span className="menu-label">Save in this session</span><span className="menu-hint">S</span></button>
                  <button className="menu-item" onClick={snapCopy}>Copy image</button>
                  <button className="menu-item" onClick={snapFile}>Save image file</button>
                </div>
              )}
            </div>
            <span className="spacer" />
            {cur && (
              <div className="lab-status">
                {session.cfg.record && <span className="lab-rec" title="Recording to runs/lab">● REC</span>}
                <span className="mono t-hi">{utc(cur.t)}</span>
                <span className="t-dim mono">bar {(cur.v - cur.first + 1).toLocaleString()} / {(cur.last - cur.first + 1).toLocaleString()}</span>
                {cur.k > cur.v && <span className="chip chip-warn" title="Viewing the past; the simulation is further on">−{cur.k - cur.v}</span>}
              </div>
            )}
          </>
        ) : (
          <>
            <span className="t-dim" style={{ fontSize: 10.5 }}>Replay history bar by bar through the live strategy - simulated account, history from disk.</span>
            <span className="spacer" />
            <button className="tool-btn lab-go" disabled={!data} onClick={() => setShowNew(true)}>+ New backtest</button>
          </>
        )}
      </div>

      {banner && (
        <div className={`lab-banner ${banner.kind}`}>
          <span>{banner.text}</span>
          <button onClick={() => setBanner(null)} title="Dismiss">✕</button>
        </div>
      )}

      <div className="lab-main" style={session ? undefined : { gridTemplateColumns: '1fr' }}>
        <div className="lab-centre" ref={centre}>
          <div className="lab-chart">
            {session ? (
              <ChartPane
                bars={shownBars} snapshot={frame?.snapshot ?? null}
                signal={overlays.signal ? shownSignal : null}
                overlays={overlays} digits={digits} money={money}
                onEngine={(e) => { engine.current = e }}
                tfMs={0} layout={layout} news={[]} positions={[]}
                legBlocked={legBlocked} trades={marks}
                cursorT={reveal && cur ? cur.t : null}
                watermark="BACKTEST" theme={theme}
                status={null}
                badge={<span className="lab-chip-sim">SIMULATED · {session.cfg.symbol} {session.cfg.tf}</span>}
              />
            ) : (
              <Welcome conn={conn} data={data} recent={recent}
                onNew={() => setShowNew(true)} onOpen={(sid) => send('open', { sid })} />
            )}
            {job && (
              <div className="lab-job">
                <div className="lab-job-t">{job.label}{job.total > 1 ? ` · ${pct}%` : '…'}</div>
                {job.total > 1 && <div className="lab-job-bar"><i style={{ width: `${pct}%` }} /></div>}
                <button className="tool-btn" onClick={() => send('cancel')}>Stop</button>
              </div>
            )}
          </div>
          {session && cur && (
            <LabTimeline bars={bars} first={cur.first} last={cur.last} k={cur.k} v={cur.v}
              reveal={reveal} trades={trades} busy={busy} onSeek={seekBar} />
          )}
          {session && (
            <div className={`lab-dock-wrap ${dockOpen ? '' : 'shut'}`}
              style={dockOpen ? { flexBasis: dockH } : undefined}>
              {dockOpen && (
                <div className="lab-split" title="Drag to resize"
                  onMouseDown={(e) => {
                    e.preventDefault()
                    const y0 = e.clientY, h0 = dockH
                    let h = h0
                    const move = (ev: MouseEvent) => {
                      h = Math.max(120, Math.min(window.innerHeight * 0.7, h0 + (y0 - ev.clientY)))
                      setDockH(h)
                    }
                    const up = () => {
                      window.removeEventListener('mousemove', move)
                      window.removeEventListener('mouseup', up)
                      save('dianur.lab.dockH', Math.round(h))
                    }
                    window.addEventListener('mousemove', move)
                    window.addEventListener('mouseup', up)
                  }} />
              )}
              <button className="lab-dock-toggle" onClick={() => setDockOpen((x) => !x)}
                title={dockOpen ? 'Hide the research panel' : 'Show the research panel'}>{dockOpen ? '▾' : '▴'}</button>
              {dockOpen && (
                <LabDock tab={dockTab} onTab={setDockTab} session={session} trades={trades}
                  events={events} notes={notes} snaps={snaps} stats={stats} tags={tags}
                  cursorI={v} cursorT={cur?.t ?? 0} selectedTrade={selTrade}
                  onPickTrade={pickTrade} onSeekBar={seekBar} onSeekTime={seekTime}
                  onOpen={(sid) => send('open', { sid })} onNew={() => setShowNew(true)}
                  onSnapshot={snapToSession} digits={digits} reveal={reveal} refreshKey={sessionsKey} />
              )}
            </div>
          )}
        </div>
        {session && (
          <LabSide tab={sideTab} onTab={setSideTab} session={session} frame={frame} events={events}
            atFrontier={atFrontier} digits={digits} selectedTrade={selectedTrade} tags={tags}
            selectedSignal={selSignal} onSelectSignal={setSelSignal}
            onCommand={(cmd) => send('command', { cmd })} onSeekBar={seekBar}
            onTag={(id, tag, note) => send('tag', { id, tag, note })}
            onNote={(text) => { send('note', { text }); flash('Note added to the journal') }}
            schema={schema?.schema ?? []} live={schema?.live ?? {}} playbooks={data?.playbooks ?? []}
            onConfigure={configure} />
        )}
      </div>

      {showNew && data && (
        <NewSession data={data} current={session}
          onCreate={(cfg) => { setShowNew(false); setBanner(null); send('create', { cfg }) }}
          onClose={() => setShowNew(false)} />
      )}
      {toast && <div className="lab-toast">{toast}</div>}
    </div>
  )
}

function Welcome({ conn, data, recent, onNew, onOpen }: {
  conn: LabConn
  data: LabDataInfo | null
  recent: any[]
  onNew: () => void
  onOpen: (sid: string) => void
}) {
  const sym = data?.symbols[0]
  return (
    <div className="lab-welcome">
      <div className="lab-hero">
        <div className="lab-hero-k">BACKTEST LAB</div>
        <h2>Test the strategy the way it trades.</h2>
        <p>
          Walk gold's history one closed bar at a time through the live engine and the live
          executor - same signals, same gates, same entries, same trailing stop - on a simulated
          account. Step, play, rewind, take over by hand, and keep every decision on record.
        </p>
        <div className="lab-hero-btns">
          <button className="tool-btn lab-go" disabled={!data} onClick={onNew}>
            {conn === 'starting' ? 'Starting the lab…' : !data ? 'Connecting…' : '+ Start a backtest'}
          </button>
        </div>
        {sym && (
          <div className="t-dim mono" style={{ fontSize: 10, marginTop: 10 }}>
            {sym.symbol}: 1m {new Date(sym.coverage['1m']?.first_ms ?? 0).getUTCFullYear()} → {new Date(sym.coverage['1m']?.last_ms ?? 0).toISOString().slice(0, 10)} on disk
          </div>
        )}
        <div className="lab-keys">
          <span><b>Space</b> play/pause</span><span><b>← →</b> step</span><span><b>Shift</b> ×10</span>
          <span><b>N</b> next signal</span><span><b>T</b> next trade</span><span><b>R</b> reveal future</span>
          <span><b>S</b> snapshot</span><span><b>Home/End</b> first/latest</span>
        </div>
      </div>
      {recent.length > 0 && (
        <div className="lab-recent">
          <div className="lab-chartbox-h">Recent sessions</div>
          {recent.map((m) => (
            <button key={m.id} className="lab-recent-r" onClick={() => onOpen(m.id)}>
              <span className="t-hi">{m.name}</span>
              <span className="t-dim mono">{m.symbol} {m.tf} · {m.summary?.n ?? 0} trades · {signed(m.summary?.net ?? 0, 2)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
