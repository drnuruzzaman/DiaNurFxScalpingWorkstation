/**
 * LabDock - the lab's research panel, under the chart.
 *
 *   TRADES       every closed trade; click one to put the chart on its entry
 *   JOURNAL      everything the engine and the executor decided, bar by bar
 *   PERFORMANCE  the numbers, the equity curve, R distribution, MAE/MFE and
 *                breakdowns by playbook, session, hour, side and exit
 *   SESSIONS     saved runs: reopen (re-runs and checks it still matches),
 *                compare two, export, delete
 *   SNAPSHOTS    chart images taken in this session, each tied to its bar
 */
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { fmt, signed } from '../lib/format'
import { EquityCurve, ExcursionScatter, RHistogram } from './LabCharts'
import { lab } from './labApi'
import type { LabEvent, LabMeta, LabNote, LabSession, LabSnap, LabStats, LabTrade } from './types'

export type DockTab = 'trades' | 'journal' | 'performance' | 'sessions' | 'snapshots'

const when = (ms: number) => {
  const d = new Date(ms)
  return `${d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', timeZone: 'UTC' })} `
    + `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
}
const day = (ms: number) => new Date(ms).toLocaleDateString('en-GB', {
  day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' })

const JOURNAL_KINDS: { key: string; label: string; on: boolean }[] = [
  { key: 'signal', label: 'Signals', on: true },
  { key: 'order', label: 'Orders', on: true },
  { key: 'fill', label: 'Fills', on: true },
  { key: 'stop', label: 'Stops', on: true },
  { key: 'exit', label: 'Exits', on: true },
  { key: 'cancel', label: 'Cancels', on: true },
  { key: 'manual', label: 'Manual', on: true },
  { key: 'note', label: 'Notes', on: true },
  { key: 'decision', label: 'Decisions', on: false },
  { key: 'session', label: 'Session', on: true },
  { key: 'error', label: 'Errors', on: true },
]

export function LabDock(p: {
  tab: DockTab
  onTab: (t: DockTab) => void
  session: LabSession | null
  trades: LabTrade[]
  events: LabEvent[]
  notes: LabNote[]
  snaps: LabSnap[]
  stats: LabStats | null
  tags: Record<string, { tag?: string; note?: string }>
  cursorI: number
  cursorT: number
  selectedTrade: string | null
  onPickTrade: (t: LabTrade) => void
  onSeekBar: (i: number) => void
  onSeekTime: (t: number) => void
  onOpen: (sid: string) => void
  onNew: () => void
  onSnapshot: () => void
  digits: number
  reveal: boolean
  refreshKey: number
}) {
  const tabs: { key: DockTab; label: string; count?: number }[] = [
    { key: 'trades', label: 'Trades', count: p.trades.length },
    { key: 'journal', label: 'Journal' },
    { key: 'performance', label: 'Performance' },
    { key: 'sessions', label: 'Sessions' },
    { key: 'snapshots', label: 'Snapshots', count: p.snaps.length },
  ]
  return (
    <div className="lab-dock">
      <div className="lab-dock-tabs">
        {tabs.map((t) => (
          <button key={t.key} className={`lab-tab ${p.tab === t.key ? 'on' : ''}`} onClick={() => p.onTab(t.key)}>
            {t.label}{t.count ? <span className="lab-count">{t.count}</span> : null}
          </button>
        ))}
      </div>
      <div className="lab-dock-body">
        {p.tab === 'trades' && <TradesTab {...p} />}
        {p.tab === 'journal' && <JournalTab {...p} />}
        {p.tab === 'performance' && <PerformanceTab {...p} />}
        {p.tab === 'sessions' && <SessionsTab {...p} />}
        {p.tab === 'snapshots' && <SnapshotsTab {...p} />}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ trades
type SortKey = 'n' | 'r' | 'profit' | 'bars' | 'mfe_r' | 'mae_r' | 'playbook'

function TradesTab(p: Parameters<typeof LabDock>[0]) {
  const [sort, setSort] = useState<{ k: SortKey; dir: 1 | -1 }>({ k: 'n', dir: -1 })
  const rows = useMemo(() => {
    const shown = p.reveal ? p.trades : p.trades.filter((t) => t.exit_i <= p.cursorI)
    return [...shown].sort((a, b) => {
      const x = a[sort.k] as any, y = b[sort.k] as any
      return (x > y ? 1 : x < y ? -1 : 0) * sort.dir
    })
  }, [p.trades, p.cursorI, p.reveal, sort])
  if (!p.session) return <div className="lab-empty">No session open.</div>
  if (!p.trades.length) return <div className="lab-empty">No closed trades yet - step or play forward.</div>
  const H = ({ k, label, num }: { k: SortKey; label: string; num?: boolean }) => (
    <th className={`sortable ${sort.k === k ? 'sorted' : ''} ${num ? 'num' : ''}`}
      onClick={() => setSort((s) => ({ k, dir: s.k === k ? (-s.dir as 1 | -1) : -1 }))}>
      {label}{sort.k === k ? (sort.dir > 0 ? ' ▲' : ' ▼') : ''}
    </th>
  )
  const tot = rows.reduce((a, t) => ({ r: a.r + t.r, p: a.p + t.profit, w: a.w + (t.profit > 0 ? 1 : 0) }), { r: 0, p: 0, w: 0 })
  return (
    <table className="tabular lab-table">
      <thead><tr>
        <H k="n" label="#" /><th>Entry (UTC)</th><th>Side</th><H k="playbook" label="Playbook" /><th>Kind</th>
        <th className="num">Entry</th><th className="num">Stop</th><th className="num">Exit</th><th>Exit by</th>
        <H k="r" label="R" num /><H k="profit" label="P&L" num /><H k="bars" label="Bars" num />
        <H k="mfe_r" label="MFE" num /><H k="mae_r" label="MAE" num /><th>Tag</th>
      </tr></thead>
      <tbody>
        {rows.map((t) => (
          <tr key={t.id} className={p.selectedTrade === t.id ? 'sel' : ''} onClick={() => p.onPickTrade(t)}>
            <td className="t-dim">{t.n}</td>
            <td>{when(t.entry_t)}</td>
            <td className={t.side === 'buy' ? 't-up' : 't-down'}>{t.side.toUpperCase()}</td>
            <td>{t.playbook}</td>
            <td className="t-dim">{t.kind ?? ''}</td>
            <td className="num">{fmt(t.entry, p.digits)}</td>
            <td className="num t-dim">{fmt(t.stop0, p.digits)}</td>
            <td className="num">{fmt(t.exit, p.digits)}</td>
            <td><span className={`chip ${t.outcome === 'target' ? 'chip-up' : t.outcome === 'trail' ? 'chip-info' : t.outcome === 'stop' ? 'chip-down' : 'chip-mute'}`}>{t.outcome}</span></td>
            <td className={`num ${t.r >= 0 ? 't-up' : 't-down'}`}>{signed(t.r, 2)}</td>
            <td className={`num ${t.profit >= 0 ? 't-up' : 't-down'}`}>{signed(t.profit, 2)}</td>
            <td className="num">{t.bars}</td>
            <td className="num t-mid">{t.mfe_r.toFixed(2)}</td>
            <td className="num t-mid">{t.mae_r.toFixed(2)}</td>
            <td className="t-warn">{p.tags[t.id]?.tag ?? ''}{p.tags[t.id]?.note ? ' ✎' : ''}</td>
          </tr>
        ))}
      </tbody>
      <tfoot><tr>
        <td colSpan={9} className="t-dim">{rows.length} trades · {rows.length ? Math.round(tot.w / rows.length * 100) : 0}% won{!p.reveal && rows.length < p.trades.length ? ` · ${p.trades.length - rows.length} after this bar hidden` : ''}</td>
        <td className={`num ${tot.r >= 0 ? 't-up' : 't-down'}`}>{signed(tot.r, 2)}</td>
        <td className={`num ${tot.p >= 0 ? 't-up' : 't-down'}`}>{signed(tot.p, 2)}</td>
        <td colSpan={4} />
      </tr></tfoot>
    </table>
  )
}

// ----------------------------------------------------------------- journal
function JournalTab(p: Parameters<typeof LabDock>[0]) {
  const [kinds, setKinds] = useState<Record<string, boolean>>(
    () => Object.fromEntries(JOURNAL_KINDS.map((k) => [k.key, k.on])))
  const [q, setQ] = useState('')
  const box = useRef<HTMLDivElement>(null)
  const rows = useMemo(() => {
    const notes: LabEvent[] = p.notes.map((n, i) => ({ seq: 1e9 + i, t: n.t, i: n.i, kind: 'note', text: n.text }))
    const needle = q.trim().toLowerCase()
    return [...p.events, ...notes]
      .filter((e) => kinds[e.kind] ?? true)
      .filter((e) => !needle || e.text.toLowerCase().includes(needle))
      .sort((a, b) => a.i - b.i || a.seq - b.seq)
      .slice(-3000)
  }, [p.events, p.notes, kinds, q])
  // Keep the bar on screen in view.
  useEffect(() => {
    const el = box.current?.querySelector<HTMLElement>('.lab-j.now')
    el?.scrollIntoView({ block: 'nearest' })
  }, [p.cursorI, rows.length])
  if (!p.session) return <div className="lab-empty">No session open.</div>
  const lastAtOrBefore = rows.reduce((k, e, idx) => (e.i <= p.cursorI ? idx : k), -1)
  return (
    <div className="lab-journal">
      <div className="lab-jbar">
        {JOURNAL_KINDS.map((k) => (
          <button key={k.key} className={`lab-kchip k-${k.key} ${kinds[k.key] ? 'on' : ''}`}
            onClick={() => setKinds((s) => ({ ...s, [k.key]: !s[k.key] }))}>{k.label}</button>
        ))}
        <input className="lab-search" placeholder="filter…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="lab-jlist" ref={box}>
        {!rows.length ? <div className="lab-empty">Nothing recorded for these filters yet.</div>
          : rows.map((e, idx) => (
            <div key={`${e.kind}-${e.seq}`}
              className={`lab-j ${e.i > p.cursorI ? 'future' : ''} ${idx === lastAtOrBefore ? 'now' : ''}`}
              onClick={() => p.onSeekBar(e.i)} title="Put the chart on this bar">
              <span className="mono t-dim">{when(e.t)}</span>
              <span className={`lab-kind k-${e.kind}`}>{e.kind}</span>
              <span className="lab-jtext">{e.text}</span>
            </div>
          ))}
      </div>
    </div>
  )
}

// ------------------------------------------------------------- performance
function PerformanceTab(p: Parameters<typeof LabDock>[0]) {
  const s = p.stats
  if (!p.session || !s) return <div className="lab-empty">No session open.</div>
  const b0 = s.balance0 || 1
  const ret = ((s.end_balance ?? b0) / b0 - 1) * 100
  const K = ({ k, v, cls, sub }: { k: string; v: React.ReactNode; cls?: string; sub?: string }) => (
    <div className="lab-tile"><div className="lab-tile-k">{k}</div><div className={`lab-tile-v mono ${cls ?? ''}`}>{v}</div>{sub && <div className="lab-tile-s">{sub}</div>}</div>
  )
  return (
    <div className="lab-perf">
      <div className="lab-tiles">
        <K k="Net P&L" v={signed(s.net, 2)} cls={s.net >= 0 ? 't-up' : 't-down'} sub={`${signed(ret, 2)}% on ${fmt(b0, 0)}`} />
        <K k="Trades" v={s.n} sub={s.n ? `${s.win ?? 0}% won` : '—'} />
        <K k="Profit factor" v={s.pf ?? '—'} cls={(s.pf ?? 0) >= 1 ? 't-up' : 't-down'} />
        <K k="Expectancy" v={s.n ? `${signed(s.exp_r ?? 0, 3)}R` : '—'} cls={(s.exp_r ?? 0) >= 0 ? 't-up' : 't-down'} sub={`${signed(s.sum_r, 2)}R total`} />
        <K k="Avg win / loss" v={s.n ? `${(s.avg_win_r ?? 0).toFixed(2)} / ${(s.avg_loss_r ?? 0).toFixed(2)}` : '—'} sub="in R" />
        <K k="Max drawdown" v={s.n ? fmt(s.max_dd ?? 0, 2) : '—'} cls="t-down" sub={s.n ? `${(s.max_dd_pct ?? 0).toFixed(2)}% · losing streak ${s.max_losing_streak ?? 0}` : ''} />
        <K k="Avg hold" v={s.n ? `${s.avg_bars ?? 0} bars` : '—'} />
        <K k="Best / worst" v={s.n ? `${signed(s.best ?? 0, 0)} / ${signed(s.worst ?? 0, 0)}` : '—'} />
      </div>
      <div className="lab-charts">
        <div className="lab-chartbox wide">
          <div className="lab-chartbox-h">Equity <span className="t-dim">click to jump</span></div>
          <EquityCurve points={s.equity} balance0={b0} cursorT={p.cursorT} onPick={(t) => p.onSeekTime(t)} />
        </div>
        <div className="lab-chartbox">
          <div className="lab-chartbox-h">R per trade</div>
          <RHistogram bins={s.hist} />
        </div>
        <div className="lab-chartbox">
          <div className="lab-chartbox-h">MAE / MFE <span className="t-dim">click a trade</span></div>
          <ExcursionScatter trades={p.trades} selected={p.selectedTrade} onPick={p.onPickTrade} />
        </div>
      </div>
      <div className="lab-breaks">
        {(['playbook', 'session', 'side', 'outcome', 'hour'] as const).map((k) => (
          <Breakdown key={k} title={k === 'hour' ? 'Entry hour (UTC)' : k} rows={s.by?.[k] ?? {}} />
        ))}
      </div>
    </div>
  )
}

function Breakdown({ title, rows }: { title: string; rows: Record<string, any> }) {
  const keys = Object.keys(rows)
  if (!keys.length) return null
  return (
    <div className="lab-break">
      <div className="lab-chartbox-h">{title}</div>
      <table className="tabular">
        <thead><tr><th /><th className="num">n</th><th className="num">win</th><th className="num">E[R]</th><th className="num">ΣR</th><th className="num">P&L</th><th className="num">PF</th></tr></thead>
        <tbody>
          {keys.map((k) => {
            const r = rows[k]
            return (
              <tr key={k}>
                <td>{k}</td><td className="num">{r.n}</td><td className="num">{r.win}%</td>
                <td className={`num ${r.exp_r >= 0 ? 't-up' : 't-down'}`}>{signed(r.exp_r, 2)}</td>
                <td className={`num ${r.sum_r >= 0 ? 't-up' : 't-down'}`}>{signed(r.sum_r, 1)}</td>
                <td className={`num ${r.net >= 0 ? 't-up' : 't-down'}`}>{signed(r.net, 0)}</td>
                <td className="num">{r.pf ?? '—'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------- sessions
function SessionsTab(p: Parameters<typeof LabDock>[0]) {
  const [rows, setRows] = useState<LabMeta[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [pick, setPick] = useState<string[]>([])
  const load = () => lab.sessions().then((r) => { setRows(r.sessions); setErr(null) })
    .catch((e) => setErr(e?.message ?? 'lab offline'))
  useEffect(() => { load() }, [p.refreshKey])
  const compare = (rows ?? []).filter((r) => pick.includes(r.id))
  return (
    <div className="lab-sessions">
      <div className="lab-jbar">
        <button className="tool-btn" onClick={p.onNew}>+ New session</button>
        <button className="tool-btn" onClick={load}>Refresh</button>
        <span className="t-dim" style={{ fontSize: 9.5 }}>Opening a session re-runs it with today's engine and tells you whether the result still matches what was recorded.</span>
      </div>
      {err ? <div className="lab-empty">Could not list sessions - {err}</div>
        : !rows ? <div className="lab-empty">Loading…</div>
          : !rows.length ? <div className="lab-empty">No saved sessions yet.</div> : (
            <table className="tabular lab-table">
              <thead><tr>
                <th title="Tick two to compare">vs</th><th>Name</th><th>Market</th><th>Range (UTC)</th><th>Mode</th>
                <th className="num">Done</th><th className="num">Trades</th><th className="num">P&L</th><th className="num">PF</th>
                <th className="num">E[R]</th><th>Settings</th><th>Engine</th><th>Saved</th><th />
              </tr></thead>
              <tbody>
                {rows.map((m) => {
                  const nChanged = Object.values(m.changed ?? {}).reduce((n, g) => n + Object.keys(g).length, 0)
                  return (
                    <tr key={m.id} className={m.active ? 'sel' : ''}>
                      <td><input type="checkbox" checked={pick.includes(m.id)} onChange={(e) =>
                        setPick((s) => e.target.checked ? [...s, m.id].slice(-2) : s.filter((x) => x !== m.id))} /></td>
                      <td className="t-hi">{m.name}{m.active ? <span className="chip chip-info" style={{ marginLeft: 6 }}>open</span> : null}</td>
                      <td>{m.symbol} {m.tf}</td>
                      <td>{day(m.start)} → {day(m.end)}</td>
                      <td>{m.mode}</td>
                      <td className="num">{m.bars_total ? Math.round(m.bars_done / m.bars_total * 100) : 0}%</td>
                      <td className="num">{m.summary?.n ?? 0}</td>
                      <td className={`num ${(m.summary?.net ?? 0) >= 0 ? 't-up' : 't-down'}`}>{signed(m.summary?.net ?? 0, 2)}</td>
                      <td className="num">{m.summary?.pf ?? '—'}</td>
                      <td className="num">{m.summary?.exp_r != null ? signed(m.summary.exp_r, 3) : '—'}</td>
                      <td className={nChanged ? 't-warn' : 't-dim'}>{nChanged ? `${nChanged} changed` : 'live'}</td>
                      <td className="t-dim">{m.engine_rev}</td>
                      <td className="t-dim">{new Date(m.updated_ms).toLocaleString()}</td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        <button className="lab-link" onClick={() => p.onOpen(m.id)}>open</button>
                        <a className="lab-link" href={lab.exportUrl(m.id)}>export</a>
                        <button className="lab-link danger" onClick={() => {
                          if (window.confirm(`Delete "${m.name}" and its snapshots? This cannot be undone.`)) {
                            lab.remove(m.id).then(load)
                          }
                        }}>delete</button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
      {compare.length === 2 && <Compare a={compare[0]} b={compare[1]} />}
    </div>
  )
}

function Compare({ a, b }: { a: LabMeta; b: LabMeta }) {
  const rows: [string, (m: LabMeta) => any, number][] = [
    ['Trades', (m) => m.summary?.n ?? 0, 0],
    ['Net P&L', (m) => m.summary?.net ?? 0, 2],
    ['Win %', (m) => m.summary?.win ?? 0, 1],
    ['Profit factor', (m) => m.summary?.pf ?? 0, 2],
    ['Expectancy R', (m) => m.summary?.exp_r ?? 0, 3],
    ['Total R', (m) => m.summary?.sum_r ?? 0, 2],
    ['Max drawdown', (m) => m.summary?.max_dd ?? 0, 2],
  ]
  const settings = new Set([...Object.entries(a.changed ?? {}), ...Object.entries(b.changed ?? {})]
    .flatMap(([g, kv]) => Object.keys(kv).map((k) => `${g}.${k}`)))
  return (
    <div className="lab-compare">
      <div className="lab-chartbox-h">Compare</div>
      <table className="tabular">
        <thead><tr><th /><th className="num">{a.name}</th><th className="num">{b.name}</th><th className="num">Δ</th></tr></thead>
        <tbody>
          {rows.map(([label, f, d]) => {
            const x = Number(f(a)), y = Number(f(b))
            return (
              <tr key={label}><td>{label}</td><td className="num">{x.toFixed(d)}</td><td className="num">{y.toFixed(d)}</td>
                <td className={`num ${y - x >= 0 ? 't-up' : 't-down'}`}>{signed(y - x, d)}</td></tr>
            )
          })}
          {[...settings].map((key) => {
            const [g, k] = key.split('.')
            const va = a.changed?.[g]?.[k], vb = b.changed?.[g]?.[k]
            const live = (va ?? vb)?.live
            const show = (v: any) => JSON.stringify(v ?? live)
            return (
              <tr key={key} className="t-mid"><td>{key}</td><td className="num">{show(va?.session)}</td><td className="num">{show(vb?.session)}</td><td /></tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// --------------------------------------------------------------- snapshots
function SnapshotsTab(p: Parameters<typeof LabDock>[0]) {
  if (!p.session) return <div className="lab-empty">No session open.</div>
  const sid = p.session.meta.id
  return (
    <div className="lab-snaps">
      <div className="lab-jbar">
        <button className="tool-btn" onClick={p.onSnapshot}>📷 Save a snapshot of this bar</button>
        <span className="t-dim" style={{ fontSize: 9.5 }}>Saved inside the session folder (runs/lab/{sid}/snapshots). Shortcut: S.</span>
      </div>
      {!p.snaps.length ? <div className="lab-empty">No snapshots in this session yet.</div> : (
        <div className="lab-snapgrid">
          {[...p.snaps].reverse().map((s) => (
            <div key={s.file} className="lab-snap" onClick={() => p.onSeekBar(s.i)} title="Put the chart on this bar">
              <img src={lab.snapshotUrl(sid, s.file)} alt={s.file} loading="lazy" />
              <div className="lab-snap-f">
                <span className="mono">{when(s.t)}</span>
                <a href={lab.snapshotUrl(sid, s.file)} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>open ↗</a>
              </div>
              {s.note && <div className="lab-snap-n">{s.note}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
