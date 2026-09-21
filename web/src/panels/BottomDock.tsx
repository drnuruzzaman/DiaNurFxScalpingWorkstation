import React, { useEffect, useState } from 'react'
import type { Signal, Snapshot } from '../chart/types'
import { api, type Board, type CalEvent, type DealsPayload } from '../lib/api'
import { ago, dateUTC, dirClass, fmt, signed, stageChip } from '../lib/format'
import { Empty } from './common'
import { scheduleSave } from '../lib/workspace'

type BoardKey = 'tf' | 'symbol' | 'playbook' | 'side' | 'stage' | 'conf' | 'rr' | 'netr'

// Lifecycle order, so sorting by stage reads start-to-finish.
const STAGE_ORDER: Record<string, number> = {
  FORMING: 0, FINAL: 1, SENT: 2, FILLED: 3, CLOSED: 4, EXPIRED: 5, CANCELLED: 6, REVERSED: 7,
}

/** The value each sortable column sorts on. Numbers sort as numbers. */
function boardValue(s: any, k: BoardKey): number | string {
  switch (k) {
    case 'tf': return s.tf_seconds ?? 0
    case 'symbol': return s.symbol ?? ''
    case 'playbook': return s.label ?? s.playbook ?? ''
    case 'side': return s.side ?? ''
    case 'stage': return STAGE_ORDER[s.stage] ?? 99
    case 'conf': return s.confidence ?? -1
    case 'rr': return s.rr2 ?? -1
    case 'netr': return s.sizing?.net_rr2 ?? -99
  }
}

// Numbers start highest-first - the best setup is usually what you are after.
const NUMERIC: BoardKey[] = ['conf', 'rr', 'netr']

const TF_SECONDS: Record<string, number> = {
  '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600,
  '2h': 7200, '4h': 14400, '1d': 86400,
}

/** Same columns for a signal-store record (the Execution tab's live table). */
function execValue(r: any, k: BoardKey): number | string {
  switch (k) {
    case 'tf': return TF_SECONDS[r.tf] ?? 0
    case 'symbol': return r.symbol ?? ''
    case 'playbook': return r.playbook ?? ''
    case 'side': return r.side ?? ''
    case 'stage': return STAGE_ORDER[r.stage] ?? 99
    case 'conf': return r.qual?.confidence ?? r.signal?.confidence ?? -1
    case 'rr': return r.signal?.rr2 ?? -1
    case 'netr': return r.qual?.net_rr2 ?? -99
  }
}

type Sort = { key: BoardKey; dir: 1 | -1 }

/** Sort rows on a copy; ties keep the incoming order. */
function sortRows<T>(rows: T[], sort: Sort, value: (r: T, k: BoardKey) => number | string): T[] {
  return rows
    .map((r, i) => ({ r, i }))
    .sort((a, b) => {
      const va = value(a.r, sort.key)
      const vb = value(b.r, sort.key)
      const c = typeof va === 'number' && typeof vb === 'number'
        ? va - vb : String(va).localeCompare(String(vb))
      return c !== 0 ? c * sort.dir : a.i - b.i
    })
    .map((x) => x.r)
}

/** A clickable header: sorts on first click, reverses on the next. */
function SortTh({ k, label, num, sort, onSort }: {
  k: BoardKey; label: string; num?: boolean; sort: Sort; onSort: (k: BoardKey) => void
}) {
  return (
    <th className={`sortable ${num ? 'num' : ''} ${sort.key === k ? 'sorted' : ''}`}
      onClick={() => onSort(k)} title={`Sort by ${label}`}>
      {label}{sort.key === k ? (sort.dir === 1 ? ' \u25B2' : ' \u25BC') : ''}
    </th>
  )
}

/** A sort that is remembered in localStorage and the saved workspace. */
function useSavedSort(key: string): [Sort, (k: BoardKey) => void] {
  const [sort, setSort] = useState<Sort>(() => {
    try {
      const v = JSON.parse(localStorage.getItem(key) || 'null')
      if (v && v.key && (v.dir === 1 || v.dir === -1)) return v
    } catch { /* fall through */ }
    return { key: 'tf', dir: 1 }
  })
  const by = (k: BoardKey) => setSort((cur) => {
    const next: Sort = cur.key === k
      ? { key: k, dir: cur.dir === 1 ? -1 : 1 }
      : { key: k, dir: NUMERIC.includes(k) ? -1 : 1 }
    try { localStorage.setItem(key, JSON.stringify(next)) } catch { /* private mode */ }
    scheduleSave()
    return next
  })
  return [sort, by]
}

export type DockTab = 'positions' | 'orders' | 'history' | 'calendar' | 'execution'
  | 'signals' | 'matrix' | 'levels' | 'events' | 'liquidity'

export const DOCK_TABS: { key: DockTab; label: string }[] = [
  { key: 'positions', label: 'POSITIONS' },
  { key: 'orders', label: 'ORDERS' },
  { key: 'history', label: 'HISTORY' },
  { key: 'calendar', label: 'CALENDAR' },
  { key: 'signals', label: 'SIGNAL BOARD' },
  { key: 'execution', label: 'EXECUTION' },
  { key: 'matrix', label: 'TF MATRIX' },
  { key: 'levels', label: 'LEVELS' },
  { key: 'events', label: 'EVENTS' },
  { key: 'liquidity', label: 'LIQUIDITY' },
]

/** How a position actually ended. MT5 reports this on the closing deal. */
const CLOSE_REASON: Record<number, string> = {
  0: 'manual', 1: 'mobile', 2: 'web', 3: 'expert',
  4: 'stop loss', 5: 'take profit', 6: 'stop out',
  7: 'rollover', 8: 'margin', 9: 'split',
}

const HISTORY_WINDOWS = [1, 7, 30, 90]

function impactClass(impact: string): string {
  if (impact === 'high') return 't-down'
  if (impact === 'medium') return 't-warn'
  if (impact === 'holiday') return 't-dim'
  return 't-mid'
}

export function BottomDock({
  snap, signals, board, positions, orders, height, onHeight, activeTf, onTf,
  activeSymbol, onOpen, onToggle, tab: tabProp, onTab, peek = false,
}: {
  snap: Snapshot | null
  signals: Signal[]
  board: Board | null
  positions: any[]
  orders: any[]
  height: number
  onHeight: (h: number) => void
  activeTf: string
  onTf: (tf: string) => void
  /** The symbol on the chart, to mark its rows. */
  activeSymbol?: string
  /** Open a symbol + timeframe - the board lists every watchlist symbol. */
  onOpen?: (symbol: string, tf: string) => void
  /** Hide the panel (double-click the tab bar) - its tabs move to the footer. */
  onToggle?: () => void
  /** The open tab, owned by App so the footer can show and switch it. */
  tab?: DockTab
  onTab?: (t: DockTab) => void
  /** Peek mode: just one tab's content, for the footer's hover preview. */
  peek?: boolean
}) {
  const [ownTab, setOwnTab] = useState<DockTab>('signals')
  const [esort, execSortBy] = useSavedSort('dianur.execSort')
  // Signal board sort. Saved with the workspace so it survives a restart.
  const [bsort, setBsort] = useState<{ key: BoardKey; dir: 1 | -1 }>(() => {
    try {
      const v = JSON.parse(localStorage.getItem('dianur.boardSort') || 'null')
      if (v && v.key && (v.dir === 1 || v.dir === -1)) return v
    } catch { /* fall through */ }
    return { key: 'tf', dir: 1 }
  })
  const sortBy = (key: BoardKey) => setBsort((cur) => {
    const next = cur.key === key
      ? { key, dir: (cur.dir === 1 ? -1 : 1) as 1 | -1 }
      : { key, dir: (NUMERIC.includes(key) ? -1 : 1) as 1 | -1 }
    try { localStorage.setItem('dianur.boardSort', JSON.stringify(next)) } catch { /* private mode */ }
    scheduleSave()
    return next
  })
  const tab = tabProp ?? ownTab
  const setTab = (t: DockTab) => (onTab ? onTab(t) : setOwnTab(t))
  // The order ledger, fetched only while its tab is open: it is an audit
  // view, not something that needs to stream.
  const [exec, setExec] = useState<any>(null)
  useEffect(() => {
    if (tab !== 'execution') return
    let live = true
    const pull = () => api.execution().then((r) => live && setExec(r)).catch(() => {})
    pull()
    const id = window.setInterval(pull, 3000)
    return () => { live = false; clearInterval(id) }
  }, [tab])
  const [days, setDays] = useState(7)
  const [hist, setHist] = useState<DealsPayload | null>(null)
  const [cal, setCal] = useState<CalEvent[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadErr, setLoadErr] = useState<string | null>(null)

  // History and calendar are fetched only while their tab is on screen, and
  // on a slow clock. Both are expensive reads on the bridge - deals walks a
  // month of MT5 history and asks for the orders behind every position in it
  // - so polling them at the stream's 1s cadence would fight the chart for
  // MT5_LOCK and stall the whole terminal.
  useEffect(() => {
    if (tab !== 'history' && tab !== 'calendar') return
    let stop = false

    const pull = async () => {
      setLoading(true)
      try {
        if (tab === 'history') setHist(await api.deals(days))
        // A full week back and a full week forward. ForexFactory publishes a
        // week at a time, and a 6-hour rear window hid most of it - by Friday
        // the tab was showing a handful of rows out of seventy-five.
        else setCal((await api.calendar(168, 168)).events)
        if (!stop) setLoadErr(null)
      } catch (e: any) {
        if (!stop) setLoadErr(e?.message ?? 'could not load')
      } finally {
        if (!stop) setLoading(false)
      }
    }

    pull()
    const t = setInterval(pull, tab === 'history' ? 30_000 : 300_000)
    return () => { stop = true; clearInterval(t) }
  }, [tab, days])

  const drag = (e: React.MouseEvent) => {
    e.preventDefault()
    const startY = e.clientY
    const startH = height
    const move = (ev: MouseEvent) =>
      onHeight(Math.max(74, Math.min(460, startH - (ev.clientY - startY))))
    const up = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  const content = () => {
    if (tab === 'history') {
      if (loadErr) return <Empty>Could not load history — {loadErr}</Empty>
      // null means "not fetched yet"; a genuinely empty window comes back as
      // an empty trades array below. Treating the two the same flashed
      // "No history yet" for a frame every time the tab was opened.
      if (!hist) return <Empty>Reading closed trades…</Empty>
      const sm = hist.summary
      return (
        <>
          <div className="dock-strip">
            <div className="win-group">
              {HISTORY_WINDOWS.map((d) => (
                <button key={d} className={`win-btn ${d === days ? 'on' : ''}`}
                  onClick={() => setDays(d)}>{d}D</button>
              ))}
            </div>
            <span className="sep" />
            <span>TRADES <b>{sm.count}</b></span>
            <span>WIN RATE <b>{sm.win_rate != null ? `${sm.win_rate}%` : '—'}</b></span>
            <span>NET <b className={dirClass(sm.net)}>{signed(sm.net, 2)}</b></span>
            <span>PF <b>{sm.profit_factor != null ? fmt(sm.profit_factor, 2) : '—'}</b></span>
            <span>BEST <b className="t-up">{signed(sm.best, 2)}</b></span>
            <span>WORST <b className="t-down">{signed(sm.worst, 2)}</b></span>
            {loading && <span className="t-dim">refreshing…</span>}
          </div>
          {!hist.trades.length ? <Empty>No closed trades in the last {days}D.</Empty> : (
            <table className="tabular">
              <thead><tr>
                <th>Closed</th><th>Symbol</th><th>Side</th><th className="num">Volume</th>
                <th className="num">Entry</th><th className="num">Exit</th>
                <th className="num">Gross</th><th className="num">Comm</th>
                <th className="num">Swap</th><th className="num">Net</th><th>Why</th>
              </tr></thead>
              <tbody>
                {hist.trades.map((t) => (
                  <tr key={t.position_id}>
                    <td className="t-mid">{t.close_ms ? dateUTC(t.close_ms) : '—'}</td>
                    <td className="t-hi">{t.symbol}</td>
                    <td className={t.side === 'buy' ? 't-up' : 't-down'}>
                      {(t.side ?? '—').toUpperCase()}
                    </td>
                    <td className="num">{fmt(t.volume, 2)}</td>
                    <td className="num">{t.entry != null ? fmt(t.entry) : '—'}</td>
                    <td className="num">{t.exit != null ? fmt(t.exit) : '—'}</td>
                    <td className="num">{signed(t.profit, 2)}</td>
                    <td className="num t-dim">{fmt(t.commission, 2)}</td>
                    <td className="num t-dim">{fmt(t.swap, 2)}</td>
                    <td className={`num ${dirClass(t.net)}`}><b>{signed(t.net, 2)}</b></td>
                    <td className="t-mid">
                      {t.reason != null ? CLOSE_REASON[t.reason] ?? `code ${t.reason}` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )
    }

    if (tab === 'calendar') {
      if (loadErr) return <Empty>Could not load the calendar — {loadErr}</Empty>
      if (!cal) return <Empty>Reading the calendar…</Empty>
      if (!cal.length) return <Empty>Nothing scheduled in this window.</Empty>
      return (
        <table className="tabular">
          <thead><tr>
            <th>When</th><th>Time</th><th>Ccy</th><th>Impact</th>
            <th>Event</th><th className="num">Forecast</th><th className="num">Previous</th>
          </tr></thead>
          <tbody>
            {cal.map((e, i) => {
              // A release that has already landed is history; the one you are
              // about to be run over by is the reason this tab exists, so the
              // next few are the ones that get the highlight.
              const past = e.minutes < 0
              const imminent = !past && e.minutes <= 60
              return (
                <tr key={`${e.ts}-${i}`} className={past ? 't-dim' : ''}>
                  <td className={imminent ? 't-warn' : 't-mid'}>
                    {e.time_known === false ? <span className="t-dim">—</span>
                      : past ? `${ago(e.ts)} ago`
                        : e.minutes < 60 ? `in ${e.minutes}m`
                          : `in ${Math.round(e.minutes / 60)}h`}
                  </td>
                  {/* A date-only row has no time of day. Printing 00:00
                      would invent one, and this table sits next to rows whose
                      times are real. */}
                  <td className="mono t-mid">
                    {e.time_known === false
                      ? <>{dateUTC(e.ts).slice(0, 6)} <span className="t-dim">date only</span></>
                      : dateUTC(e.ts)}
                  </td>
                  <td className="t-hi">{e.currency}</td>
                  <td className={impactClass(e.impact)}>{e.impact}</td>
                  <td className={past ? '' : 't-hi'}>{e.title}</td>
                  <td className="num">{e.forecast || '—'}</td>
                  <td className="num t-dim">{e.previous || '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )
    }

    if (tab === 'positions') {
      if (!positions?.length) return <Empty>No open positions.</Empty>
      const vol = positions.reduce((a: number, p: any) => a + (p.volume || 0), 0)
      const pl = positions.reduce((a: number, p: any) => a + (p.profit || 0), 0)
      const sw = positions.reduce((a: number, p: any) => a + (p.swap || 0), 0)
      // How many are running without a stop. This is the one thing about a
      // book of 21 open trades that is worth knowing before anything else.
      const naked = positions.filter((p: any) => !p.sl).length
      return (
        <>
          <div className="dock-strip">
            <span>OPEN <b>{positions.length}</b></span>
            <span>VOLUME <b>{fmt(vol, 2)}</b></span>
            <span>FLOATING <b className={dirClass(pl)}>{signed(pl, 2)}</b></span>
            <span>SWAP <b className={dirClass(sw)}>{signed(sw, 2)}</b></span>
            {naked > 0 && (
              <span className="t-warn">NO STOP <b className="t-warn">{naked}</b></span>
            )}
          </div>
          <table className="tabular">
            <thead><tr>
              <th>Symbol</th><th>Side</th><th className="num">Volume</th>
              <th className="num">Open</th><th className="num">Current</th>
              <th className="num">SL</th><th className="num">TP</th>
              <th className="num">Swap</th>
              <th className="num">P/L</th><th>Opened</th>
            </tr></thead>
            <tbody>
              {positions.map((p: any, i: number) => {
                // The bridge calls this `side`; `type` is the raw MT5 enum and
                // is not sent at all. Reading only `type` printed UNDEFINED in
                // every row - invisible until the tab had rows to print.
                const raw = p.side ?? p.type
                const buy = raw === 'buy' || raw === 0
                const label = typeof raw === 'number'
                  ? (raw === 0 ? 'BUY' : 'SELL')
                  : String(raw ?? '—').toUpperCase()
                return (
                  <tr key={p.ticket ?? i}>
                    <td className="t-hi">{p.symbol}</td>
                    <td className={buy ? 't-up' : 't-down'}>{label}</td>
                    <td className="num">{fmt(p.volume, 2)}</td>
                    <td className="num">{fmt(p.price_open ?? p.open)}</td>
                    <td className="num">{fmt(p.price_current ?? p.current)}</td>
                    <td className={`num ${p.sl ? 't-down' : 't-warn'}`}>
                      {p.sl ? fmt(p.sl) : 'none'}
                    </td>
                    <td className="num t-up">{p.tp ? fmt(p.tp) : '—'}</td>
                    <td className={`num ${p.swap ? dirClass(p.swap) : 't-dim'}`}>
                      {fmt(p.swap, 2)}
                    </td>
                    <td className={`num ${dirClass(p.profit)}`}><b>{signed(p.profit, 2)}</b></td>
                    <td className="t-dim">{p.time_ms ? dateUTC(p.time_ms) : '—'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </>
      )
    }

    if (tab === 'orders') {
      if (!orders?.length) return <Empty>No pending orders.</Empty>
      return (
        <table className="tabular">
          <thead><tr>
            <th>Symbol</th><th>Type</th><th className="num">Volume</th>
            <th className="num">Price</th><th className="num">SL</th>
            <th className="num">TP</th><th>Placed</th>
          </tr></thead>
          <tbody>
            {orders.map((o: any, i: number) => (
              <tr key={o.ticket ?? i}>
                <td className="t-hi">{o.symbol}</td>
                {/* Same field mismatch as positions: the bridge sends `side`
                    ('buy_limit', 'sell_stop', ...), never `type`. */}
                <td className={String(o.side ?? o.type).startsWith('buy') ? 't-up' : 't-down'}>
                  {String(o.side ?? o.type ?? '—').toUpperCase().replace('_', ' ')}
                </td>
                <td className="num">{fmt(o.volume, 2)}</td>
                <td className="num">{fmt(o.price_open ?? o.price)}</td>
                <td className="num t-down">{o.sl ? fmt(o.sl) : '—'}</td>
                <td className="num t-up">{o.tp ? fmt(o.tp) : '—'}</td>
                <td className="t-dim">{o.time_ms ? dateUTC(o.time_ms) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )
    }

    if (tab === 'signals') {
      // The board covers EVERY watched timeframe, not just the one on the
      // chart. Taking a scalp against an opposing higher-timeframe setup is one
      // of the most common ways these systems lose money, and that conflict is
      // invisible on a board showing only the timeframe you happen to be on.
      const rows = board?.signals ?? []
      if (!rows.length) {
        return (
          <Empty>
            {board
              ? `No signals on the ${board.watchlist?.length ?? 1} watchlist `
                + `symbol${(board.watchlist?.length ?? 1) === 1 ? '' : 's'} across `
                + `${board.timeframes.length} timeframes.`
              : 'Scanning the watchlist\u2026'}
          </Empty>
        )
      }

      // Sorted on a copy; ties keep the server's order (timeframe, symbol,
      // status), so equal values stay grouped the way the board meant them.
      const sorted = rows
        .map((s, i) => ({ s, i }))
        .sort((a, b) => {
          const va = boardValue(a.s, bsort.key)
          const vb = boardValue(b.s, bsort.key)
          const c = typeof va === 'number' && typeof vb === 'number'
            ? va - vb : String(va).localeCompare(String(vb))
          return c !== 0 ? c * bsort.dir : a.i - b.i
        })
        .map((x) => x.s)
      const Th = ({ k, label, num }: { k: BoardKey; label: string; num?: boolean }) => (
        <th className={`sortable ${num ? 'num' : ''} ${bsort.key === k ? 'sorted' : ''}`}
          onClick={() => sortBy(k)} title={`Sort by ${label}`}>
          {label}{bsort.key === k ? (bsort.dir === 1 ? ' \u25B2' : ' \u25BC') : ''}
        </th>
      )

      let lastTf = ''
      return (
        <table className="tabular">
          <thead><tr>
            <Th k="tf" label="TF" /><Th k="symbol" label="Symbol" />
            <Th k="playbook" label="Playbook" /><Th k="side" label="Side" />
            <Th k="stage" label="Stage" /><th>Status</th>
            <Th k="conf" label="Conf" num /><th>Regime</th>
            <th className="num">Entry</th><th className="num">Stop</th>
            <th className="num">TP1</th><th className="num">TP2</th>
            <Th k="rr" label="R:R" num /><Th k="netr" label="Net R" num />
            <th className="num">Lots</th><th className="num">Age</th><th>Reason</th>
          </tr></thead>
          <tbody>
            {sorted.map((s) => {
              // The timeframe hairline only means something when the rows are
              // actually grouped by timeframe.
              const newTf = bsort.key === 'tf' && s.tf !== lastTf
              lastTf = s.tf
              const onChart = s.tf === activeTf && (!activeSymbol || s.symbol === activeSymbol)
              return (
                <tr
                  key={`${s.symbol}:${s.tf}:${s.id}`}
                  onClick={() => (onOpen && s.symbol !== activeSymbol
                    ? onOpen(s.symbol, s.tf) : onTf(s.tf))}
                  title={`Show ${s.symbol} ${s.tf} on the chart`}
                  style={{
                    cursor: 'pointer',
                    // Hairline between timeframe groups: the ordering is the
                    // whole point of this table, so make it visible.
                    borderTop: newTf ? '1px solid var(--line-strong)' : undefined,
                    background: onChart ? 'var(--bear-glow)' : undefined,
                  }}
                >
                  <td style={{ fontWeight: 700, color: onChart ? 'var(--bear)' : 'var(--info)' }}>
                    {s.tf.toUpperCase()}
                  </td>
                  <td className="t-hi" style={{ fontWeight: 700 }}>{s.symbol}</td>
                  <td className="t-hi">{s.label}</td>
                  <td className={s.side === 'buy' ? 't-up' : 't-down'}>{s.side.toUpperCase()}</td>
                  <td><span className={stageChip(s.stage)}>{s.stage || '—'}</span></td>
                  <td className={
                    s.status === 'qualified' ? 't-up'
                      : s.status === 'watch' ? 't-warn'
                        : s.status === 'conflicted' ? 't-mid' : 't-down'
                  }>{s.status}</td>
                  <td className="num">{s.confidence}</td>
                  <td className="t-mid" style={{ fontSize: 9.5 }}>{s.regime ?? '—'}</td>
                  <td className="num">{fmt(s.entry)}</td>
                  <td className="num t-down">{fmt(s.stop)}</td>
                  <td className="num t-up">{fmt(s.tp1)}</td>
                  <td className="num t-up">{fmt(s.tp2)}</td>
                  <td className="num">{s.rr2}</td>
                  <td className="num">{s.sizing?.net_rr2 ?? '—'}</td>
                  <td className="num">{s.sizing?.lots ?? '—'}</td>
                  <td className="num t-dim">{ago(s.scanned_ms)}</td>
                  {/* One line, full text on hover: a wrapping reason doubled
                      every row's height, which is what made the board feel
                      crowded more than the font size did. */}
                  <td className="t-dim" title={s.reason}
                    style={{ maxWidth: 340, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {s.reason}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )
    }

    if (tab === 'execution') {
      if (!exec) return <Empty>Loading the order ledger…</Empty>
      const orders: any[] = exec.orders ?? []
      const live: any[] = sortRows((exec.signals ?? []).filter((r: any) =>
        ['FINAL', 'SENT', 'FILLED'].includes(r.stage)), esort, execValue)
      const H = (p: { k: BoardKey; label: string; num?: boolean }) =>
        <SortTh {...p} sort={esort} onSort={execSortBy} />
      return (
        <div>
          <div className="t-dim" style={{ fontSize: 10, padding: '6px 8px' }}>
            Auto <b className={exec.auto ? 't-warn' : 't-mid'}>{exec.auto ? 'ON' : 'off'}</b>
            {' · '}bridge <b className={exec.trading_enabled ? 't-warn' : 't-up'}>
              {exec.trading_enabled ? 'ARMED' : 'read-only'}</b>
            {' · '}{live.length} live signal{live.length === 1 ? '' : 's'}
            {' · '}lots {exec.settings?.lots?.join('-')}
            {' · '}entry tolerance {exec.settings?.entry_tolerance_atr} ATR
          </div>
          {live.length > 0 && (
            <table className="tabular">
              <thead><tr>
                <H k="tf" label="TF" /><H k="symbol" label="Symbol" />
                <H k="playbook" label="Playbook" /><H k="side" label="Side" />
                <H k="stage" label="Stage" /><H k="conf" label="Conf" num />
                <th className="num">Entry</th><th className="num">Stop</th>
                <th className="num">TP1</th>
                <H k="rr" label="R:R" num /><H k="netr" label="Net R" num />
                <th>Expires</th><th>Note</th>
              </tr></thead>
              <tbody>
                {live.map((r: any) => {
                  const note = r.note || r.history?.[r.history.length - 1]?.[2]
                  const conf = r.qual?.confidence ?? r.signal?.confidence
                  return (
                    <tr key={r.id}>
                      <td style={{ fontWeight: 700, color: 'var(--info)' }}>{String(r.tf).toUpperCase()}</td>
                      <td className="t-hi" style={{ fontWeight: 700 }}>{r.symbol}</td>
                      <td>{r.playbook.replace(/_/g, ' ')}</td>
                      <td className={r.side === 'buy' ? 't-up' : 't-down'}>{r.side.toUpperCase()}</td>
                      <td><span className={stageChip(r.stage)}>{r.stage}</span></td>
                      <td className="num">{conf ?? '\u2014'}</td>
                      <td className="num">{fmt(r.signal?.entry)}</td>
                      <td className="num t-down">{fmt(r.signal?.stop)}</td>
                      <td className="num t-up">{fmt(r.signal?.tp1)}</td>
                      <td className="num">{r.signal?.rr2 ?? '\u2014'}</td>
                      <td className="num">{r.qual?.net_rr2 ?? '\u2014'}</td>
                      <td className="t-dim">{dateUTC(r.expires_ms)}</td>
                      <td className="t-dim" title={note}
                        style={{ maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {note}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
          {!orders.length ? <Empty>No orders yet. The ledger records every order this system sends.</Empty> : (
            <table className="tabular">
              <thead><tr>
                <th>Updated</th><th>State</th><th>Symbol</th><th>TF</th><th>Side</th>
                <th>Kind</th><th className="num">Lots</th><th className="num">Ticket</th>
                <th className="num">Fill</th><th className="num">Close</th>
                <th>Outcome</th><th className="num">R</th><th>Last event</th>
              </tr></thead>
              <tbody>
                {orders.map((o: any) => (
                  <tr key={o.id}>
                    <td className="t-dim">{dateUTC(o.updated_ms)}</td>
                    <td className={o.state === 'refused' || o.state === 'unknown' ? 't-down'
                      : o.state === 'filled' ? 't-up' : 't-mid'}>{o.state}</td>
                    <td className="t-hi">{o.symbol}</td><td>{o.tf}</td>
                    <td className={o.side === 'buy' ? 't-up' : 't-down'}>{(o.side || '').toUpperCase()}</td>
                    <td>{o.kind}</td><td className="num">{o.lots}</td>
                    <td className="num">{o.ticket ?? '—'}</td>
                    <td className="num">{o.fill_price != null ? fmt(o.fill_price) : '—'}</td>
                    <td className="num">{o.close_price != null ? fmt(o.close_price) : '—'}</td>
                    <td>{o.outcome ?? '—'}</td>
                    <td className={`num ${(o.r ?? 0) >= 0 ? 't-up' : 't-down'}`}>
                      {o.r != null ? `${o.r >= 0 ? '+' : ''}${o.r.toFixed(2)}` : '—'}</td>
                    <td className="t-dim" style={{ whiteSpace: 'normal', maxWidth: 280 }}>
                      {o.events?.[o.events.length - 1]?.[2]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )
    }

    if (tab === 'matrix') {
      const rows = board?.rows ?? []
      if (!rows.length) return <Empty>Scanning timeframes…</Empty>
      return (
        <table className="tabular">
          <thead><tr>
            <th>TF</th><th>Regime</th><th className="num">Conf</th>
            <th>Trend</th><th className="num">MTF</th><th className="num">Momentum</th>
            <th className="num">ATR pts</th><th className="num">Signals</th>
            <th className="num">Age</th>
          </tr></thead>
          <tbody>
            {rows.map((r) => {
              const onChart = r.tf === activeTf
              const q = r.signals.filter((x) => x.status === 'qualified').length
              return (
                <tr key={r.tf} onClick={() => onTf(r.tf)} style={{
                  cursor: 'pointer',
                  background: onChart ? 'var(--bear-glow)' : undefined,
                }}>
                  <td style={{ fontWeight: 700, color: onChart ? 'var(--bear)' : 'var(--info)' }}>
                    {r.tf.toUpperCase()}
                  </td>
                  {!r.ok ? (
                    <td colSpan={8} className="t-down">{r.error ?? 'unavailable'}</td>
                  ) : (
                    <>
                      <td className={
                        r.regime?.includes('UP') ? 't-up'
                          : r.regime?.includes('DOWN') ? 't-down'
                            : r.regime === 'RANGE' ? 't-info' : 't-warn'
                      }>{r.regime}</td>
                      <td className="num">{r.regime_confidence}</td>
                      <td className={
                        r.trend?.includes('up') ? 't-up'
                          : r.trend?.includes('down') ? 't-down' : 't-mid'
                      }>{r.trend} · {r.trend_strength}</td>
                      <td className={`num ${dirClass(r.mtf_score)}`}>{signed(r.mtf_score)}</td>
                      <td className={`num ${dirClass(r.momentum)}`}>{signed(r.momentum)}</td>
                      <td className="num t-mid">{fmt(r.atr_points, 0)}</td>
                      <td className="num">
                        {r.signals.length}
                        {q > 0 && <span className="t-up"> ({q}Q)</span>}
                      </td>
                      <td className="num t-dim">{ago(r.scanned_ms)}</td>
                    </>
                  )}
                </tr>
              )
            })}
          </tbody>
        </table>
      )
    }

    if (tab === 'levels') {
      if (!snap?.levels?.length) return <Empty>No levels.</Empty>
      return (
        <table className="tabular">
          <thead><tr>
            <th className="num">Price</th><th>Kind</th><th className="num">Score</th>
            <th className="num">Touches</th><th className="num">Dist (ATR)</th>
            <th>Broken</th><th>Built from</th>
          </tr></thead>
          <tbody>
            {snap.levels.map((l, i) => (
              <tr key={i}>
                <td className="num t-hi">{fmt(l.price)}</td>
                <td className={l.kind === 'support' ? 't-up' : l.kind === 'resistance' ? 't-down' : 't-warn'}>
                  {l.kind}
                </td>
                <td className="num">{l.score}</td>
                <td className="num">{l.touches}</td>
                <td className="num">{signed(l.distance_atr, 2)}</td>
                <td className={l.broken ? 't-dim' : 't-mid'}>{l.broken ? 'yes' : 'no'}</td>
                <td className="t-dim" style={{ whiteSpace: 'normal' }}>{l.sources.join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )
    }

    if (tab === 'events') {
      if (!snap?.events?.length) return <Empty>No recent events.</Empty>
      return (
        <table className="tabular">
          <thead><tr>
            <th>Event</th><th>Dir</th><th>Implies</th><th className="num">Level</th>
            <th className="num">Extreme</th><th className="num">Strength</th>
            <th>Confirmed</th><th className="num">Bars ago</th><th>Notes</th>
          </tr></thead>
          <tbody>
            {snap.events.map((e, i) => (
              <tr key={i}>
                <td className="t-hi">{e.kind.replace(/_/g, ' ')}</td>
                <td className="t-mid">{e.direction}</td>
                <td className={e.bias === 'bullish' ? 't-up' : 't-down'}>{e.bias}</td>
                <td className="num">{fmt(e.price)}</td>
                <td className="num">{fmt(e.extreme)}</td>
                <td className="num">{e.strength}</td>
                <td className={e.confirmed ? 't-up' : 't-warn'}>{e.confirmed ? 'yes' : 'pending'}</td>
                <td className="num">{e.bars_since}</td>
                <td className="t-dim" style={{ whiteSpace: 'normal', maxWidth: 380 }}>
                  {e.notes.join(' · ')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )
    }

    if (!snap?.liquidity?.length) return <Empty>No liquidity pools identified.</Empty>
    return (
      <table className="tabular">
        <thead><tr>
          <th className="num">Price</th><th>Side</th><th className="num">Pull</th>
          <th className="num">Count</th><th className="num">Dist (ATR)</th>
          <th>Status</th><th>Label</th>
        </tr></thead>
        <tbody>
          {snap.liquidity.map((p, i) => (
            <tr key={i}>
              <td className="num t-hi">{fmt(p.price)}</td>
              <td className={p.side === 'buyside' ? 't-up' : 't-down'}>{p.side}</td>
              <td className="num">{p.pull}</td>
              <td className="num">{p.count}</td>
              <td className="num">{fmt(p.distance_atr, 2)}</td>
              <td className={p.swept ? 't-dim' : 't-warn'}>{p.swept ? 'swept' : 'resting'}</td>
              <td className="t-mid">{p.label}</td>
            </tr>
          ))}
        </tbody>
      </table>
    )
  }

  if (peek) {
    const label = DOCK_TABS.find((t) => t.key === tab)?.label ?? ''
    return (
      <div className="dock dock-peek-inner" style={{ height }}>
        <div className="dock-peek-head">{label}</div>
        <div className="dock-body">{content()}</div>
      </div>
    )
  }

  return (
    <div className="dock" style={{ height }}>
      <div onMouseDown={drag}
        style={{ height: 4, cursor: 'ns-resize', background: 'transparent', flex: '0 0 auto' }} />
      {/* Double-click anywhere on the bar - a tab or the empty space - to hide
          the panel; its tabs then move to the footer, where a double-click
          brings it back. */}
      <div className="dock-tabs" onDoubleClick={() => onToggle?.()}
        title="Double-click to hide the bottom panel">
        {DOCK_TABS.map((t) => (
          <button key={t.key} className={`dock-tab ${tab === t.key ? 'on' : ''}`}
            onClick={() => setTab(t.key)}>
            {t.label}
            {t.key === 'positions' && positions?.length ? ` (${positions.length})` : ''}
            {t.key === 'orders' && orders?.length ? ` (${orders.length})` : ''}
            {t.key === 'history' && hist?.summary.count ? ` (${hist.summary.count})` : ''}
            {t.key === 'signals' && board?.signals.length ? ` (${board.signals.length})` : ''}
            {t.key === 'matrix' && board && !board.complete ? ' \u2026' : ''}
          </button>
        ))}
        <span className="spacer" />
      </div>
      <div className="dock-body">{content()}</div>
    </div>
  )
}
