/**
 * MarketData - keep the history on disk current (server/marketdata.py).
 *
 * Three parts: the history on disk; the download & forecast engine - which
 * symbols (any the broker offers), which timeframes, how much history a
 * timeframe not on disk yet gets, the forecast engine per symbol, and whether
 * the tables are rebuilt and their gates rescored; and the monthly routine.
 * `compact` is the one-line version for the lab's start page.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import { SymbolPicker } from './SymbolPicker'

const DAY = 86_400_000
const TF_ORDER = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '1d', '1w']
const TF_MS: Record<string, number> = {
  '1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
  '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000, '1d': DAY, '1w': 7 * DAY,
}
const YEARS = [1, 2, 3, 5, 10, 20, 30]
const STEP_LABEL: Record<string, string> = {
  download: 'Download from MT5', releases: 'Release history', forecast: 'Rebuild forecast tables',
  rescore: 'Rescore the gates',
}

type UpdateBody = {
  symbols?: string[]; tfs?: string[]; years?: number
  rebuild: boolean; rescore: boolean; download?: boolean; enable_forecast?: string[]
}
/** Where an answer is shown: beside the button that asked for it. */
type Where = 'compact' | 'update' | 'routine'
type Notice = { where: Where; text: string; kind: 'force' | 'error' | 'info' }
type Needs = { tfs: string[]; from_year: number; years: number }

const day = (ms?: number | null) => (ms ? new Date(ms).toISOString().slice(0, 10) : '—')
const stamp = (ms?: number | null) => (ms ? new Date(ms).toLocaleString() : '—')
const dur = (s: number) => {
  s = Math.max(0, Math.round(s))
  return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`
    : s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`
}
/** What a symbol's latest rescore promoted, per output. */
const passed = (p: any) => !p ? '—'
  : [['range', p.range], ['regime', p.regime], ['direction', p.direction]]
    .map(([k, v]) => `${k} ${(v as string[]).length ? (v as string[]).join(' ') : 'none'}`)
    .join(' · ') + (p.trade ? ` · trade odds ${p.trade}` : '')
/**
 * A timeframe's newest closed bar is behind when it opened more than three days
 * plus two of its own bars ago: a weekly bar that opened 13 days back is the
 * newest a week can have closed, not a gap.
 */
const behind = (tf: string, ms?: number | null) =>
  ms != null && Date.now() - ms > 3 * DAY + 2 * (TF_MS[tf] ?? 0)

/**
 * The update's progress: one bar for the whole job (the steps weighted by how
 * long they take - server/marketdata.WEIGHTS), what it is doing - or, once
 * stopped, the last thing it did - and the time. The estimate to finish is
 * shown only once there is enough done to base one on.
 */
export function UpdateProgress({ job, compact = false, aside }: {
  job: any; compact?: boolean; aside?: React.ReactNode
}) {
  const done = !job?.running
  const failed = done && job?.phase === 'failed'
  // A finished, successful job is 100% whatever the steps' own fractions say.
  const pct = done && !failed ? 100 : Math.max(0, Math.min(100, Number(job?.pct ?? 0)))
  const elapsed = ((done ? job?.finished_ms : Date.now()) - (job?.started_ms ?? Date.now())) / 1000
  const eta = !done && pct >= 5 && pct < 100 ? elapsed * (100 - pct) / pct : null
  const step = STEP_LABEL[job?.phase] ?? job?.phase ?? ''
  const error = failed && job?.error && job.error !== 'cancelled' ? String(job.error) : null
  return (
    <div className={`md-progress ${compact ? 'compact' : ''}`} role="progressbar"
      aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
      <div className="md-progress-top">
        <b className={failed ? 't-down' : done ? 't-up' : 't-info'}>
          {failed ? 'Stopped' : done ? 'Finished' : step}
        </b>
        {job?.detail && !(done && !failed) ? <span className="mono t-dim md-progress-detail">{job.detail}</span> : null}
        <span className="spacer" />
        <span className="mono">{pct.toFixed(pct < 10 && !done ? 1 : 0)}%</span>
      </div>
      <div className="md-progress-track">
        <i className={failed ? 'failed' : done ? 'done' : ''} style={{ width: `${pct}%` }} />
      </div>
      <div className="md-progress-sub t-dim">
        {error ? <span className="t-down">{error}</span> : null}
        <span className="spacer" />
        <span>{done ? `took ${dur(elapsed)}` : `${dur(elapsed)} so far`}
          {eta != null ? ` · about ${dur(eta)} to go` : ''}</span>
        {aside}
      </div>
    </div>
  )
}

/** The footer's words for each step - the settings' own are too long for it. */
const FOOT_LABEL: Record<string, string> = {
  download: 'Downloading', releases: 'Release history', forecast: 'Building forecast',
  rescore: 'Rescoring gates',
}
/** How long a finished update stays on the footer. */
const FOOT_KEEP_MS = 5 * 60_000

/**
 * The update on the footer - what it is doing, a bar and the percentage - so it
 * can be followed with Settings closed. It polls the job alone (no disk reads):
 * every 2 s while an update runs, every 15 s otherwise, which also catches one
 * the monthly routine or the lab's start page began. A finished update stays a
 * few minutes; a click opens Settings on Market data.
 */
export function FooterJob({ onOpen }: { onOpen: () => void }) {
  const [job, setJob] = useState<any>(null)
  useEffect(() => {
    let stop = false
    let t: number | undefined
    const tick = () => {
      api.dataJob()
        .then((j) => { if (!stop) setJob(j); return j })
        // An API from before this endpoint: nothing to show, and nothing to say.
        .catch(() => null)
        .then((j: any) => { if (!stop) t = window.setTimeout(tick, j?.running ? 2000 : 15_000) })
    }
    tick()
    return () => { stop = true; window.clearTimeout(t) }
  }, [])
  if (!job?.started_ms) return null
  const done = !job.running
  if (done && !(job.finished_ms && Date.now() - job.finished_ms < FOOT_KEEP_MS)) return null
  const failed = done && job.phase === 'failed'
  const pct = done && !failed ? 100 : Math.max(0, Math.min(100, Number(job.pct ?? 0)))
  const elapsed = ((done ? job.finished_ms : Date.now()) - job.started_ms) / 1000
  const eta = !done && pct >= 5 && pct < 100 ? elapsed * (100 - pct) / pct : null
  const label = failed ? 'Update stopped' : done ? 'Update finished'
    : `${FOOT_LABEL[job.phase] ?? 'Updating'}`
  const detail = failed ? (job.error === 'cancelled' ? 'cancelled' : job.error)
    : done ? (job.symbols ?? []).join(', ') : job.detail
  return (
    <>
      <span className="sep" />
      <button className={`foot-job ${failed ? 'failed' : done ? 'done' : ''}`} onClick={onOpen}
        title={[`${label}${detail ? ` - ${detail}` : ''}`,
          done ? `took ${dur(elapsed)}` : `${dur(elapsed)} so far${eta != null ? `, about ${dur(eta)} to go` : ''}`,
          'Click to open Settings › Market data'].join('\n')}>
        <span className="foot-job-text">{label}{detail ? ` · ${detail}` : ''}</span>
        <span className="foot-job-track" aria-hidden><i style={{ width: `${pct}%` }} /></span>
        <span>{pct.toFixed(pct < 10 && !done ? 1 : 0)}%{eta != null ? ` · ${dur(eta)} left` : ''}</span>
      </button>
    </>
  )
}

export function MarketData({ compact = false }: { compact?: boolean }) {
  const [st, setSt] = useState<any>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [busy, setBusy] = useState(false)
  const [switching, setSwitching] = useState(false)
  // What the last update asked for, so "Run anyway" repeats exactly that.
  const [last, setLast] = useState<{ where: Where; body: UpdateBody } | null>(null)
  // The choice. Symbols: null = every symbol on disk; any other the broker
  // offers is added with the search. Timeframes: topped up where a symbol has
  // them, downloaded with `years` of history where it does not.
  const [sel, setSel] = useState<string[] | null>(null)
  const [tfs, setTfs] = useState<string[]>(TF_ORDER)
  const [years, setYears] = useState(5)
  const [download, setDownload] = useState(true)
  const [rebuild, setRebuild] = useState(true)
  const [rescore, setRescore] = useState(false)
  // Symbols whose forecast engine is switched on once this update's download
  // gives them the history for it (it cannot be switched on before).
  const [pendingOn, setPendingOn] = useState<string[]>([])
  const [picking, setPicking] = useState(false)
  const [details, setDetails] = useState(false)

  const load = useCallback(() => {
    api.dataStatus().then((d) => { setSt(d); setLoadErr(null) }).catch((e) => {
      const m = String(e?.message ?? e)
      // The endpoints arrive with an API restart. Until then the old API answers
      // 404, or its web page (HTML, not JSON) - say what is needed, not either of those.
      const missing = m.startsWith('404') || /not valid JSON|Unexpected token/.test(m)
      setLoadErr(missing ? 'market data updates need a restart of the API to switch on' : m)
    })
  }, [])
  useEffect(() => { load() }, [load])
  // Poll while an update runs; slowly otherwise.
  useEffect(() => {
    const t = window.setInterval(load, st?.job?.running ? 2000 : 30_000)
    return () => window.clearInterval(t)
  }, [load, st?.job?.running])

  const run = (where: Where, body: UpdateBody, force = false) => {
    setBusy(true)
    setNotice(null)
    setLast({ where, body })
    api.dataUpdate({ ...body, force })
      .then((r: any) => {
        const asked = body.symbols ?? []
        if (r.ok && asked.length && !asked.every((x) => (r.symbols ?? []).includes(x))) {
          // An API from before symbol downloads ignores the request and tops up what
          // is on disk instead - say so rather than report success for the wrong thing.
          setNotice({ where, kind: 'error', text: `The running API is an older version: it updated `
            + `${(r.symbols ?? []).join(', ') || 'the symbols on disk'} instead of ${asked.join(', ')}. `
            + 'Restart the API, then run it again.' })
          load()
          return
        }
        if (r.ok) {
          setPendingOn([])
          load()
        } else setNotice({ where, kind: r.needs_force ? 'force' : 'error', text: r.error })
      })
      .catch((e) => setNotice({ where, kind: 'error', text: String(e?.message ?? e) }))
      .finally(() => setBusy(false))
  }

  /** The answer to the button pressed at `where`, shown right under it. */
  const say = (where: Where) => (notice && notice.where === where ? (
    <div className={`set-note md-notice ${notice.kind === 'force' ? 't-warn'
      : notice.kind === 'error' ? 't-down' : 't-up'}`}>
      {notice.text}
      {notice.kind === 'force' && last && (
        <button className="tool-btn" onClick={() => run(last.where, last.body, true)}>Run anyway</button>)}
    </div>
  ) : null)

  if (!st) return <div className="t-dim" style={{ fontSize: 10.5 }}>{loadErr ?? 'Reading the history on disk…'}</div>
  const job = st.job ?? {}
  const gap = st.gap_days
  const stale = gap != null && gap > 3
  const r = st.routine ?? {}

  if (compact) {
    return (
      <div className="md-compact">
        <span className={stale ? 't-warn' : 't-dim'}>
          History on disk to <b className="mono">{day(st.newest_ms)}</b>
          {gap != null && ` · ${gap < 1 ? 'current' : `${Math.floor(gap)} days behind`}`}
        </span>
        {!job.running && (
          <button className="lab-link" onClick={() => run('compact', { rebuild: true, rescore: false })}
            disabled={busy}
            title="Top up every timeframe from MT5, then rebuild the forecast tables">update now</button>)}
        {notice?.where === 'compact' && (
          <span className={notice.kind === 'force' ? 't-warn' : 't-down'}> · {notice.text}{' '}
            {notice.kind === 'force' && last && (
              <button className="lab-link danger" onClick={() => run(last.where, last.body, true)}>
                run anyway</button>)}</span>
        )}
        {loadErr && <span className="t-down"> · {loadErr}</span>}
        {job.running && <UpdateProgress job={job} compact />}
      </div>
    )
  }

  const syms: string[] = Object.keys(st.coverage ?? {})
  const cols = TF_ORDER.filter((t) => syms.some((s) => st.coverage[s]?.[t]))
  // An API from before per-symbol forecasts sends no `forecast`: gold's alone then.
  const fc = st.forecast ?? null
  const per: Record<string, any> = fc?.per_symbol ?? {}
  const needs: Needs | null = fc?.needs ?? null
  const fcOn = (s: string) => (fc ? !!per[s]?.on : s === 'XAUUSD.a')
  const readyNow = (s: string) => !!per[s] && !per[s].why
  const fcYears = needs ? (YEARS.find((y) => y >= needs.years) ?? YEARS[YEARS.length - 1]) : 10
  const chosen = sel ?? syms
  /** Why the forecast engine cannot run for `s` even after this update's download, or null. */
  const planWhy = (s: string): string | null => {
    if (!needs) return 'needs a restart of the API'
    const cov = st.coverage[s] ?? {}
    const missing: string[] = []
    const late: string[] = []
    let short = false
    for (const t of needs.tfs) {
      const c = cov[t]
      if (c) {
        if (c.first_year > needs.from_year) late.push(`${t} from ${c.first_year}`)
      } else if (!download || !tfs.includes(t)) missing.push(t)
      else if (years < needs.years) short = true
    }
    if (missing.length) return `needs ${missing.join(' ')} as well`
    if (late.length) return `needs history from ${needs.from_year}; on disk ${late.join(', ')} - updates only add newer bars`
    if (short) return `needs history from ${needs.from_year} - choose ${fcYears} years or more`
    return null
  }
  const pending = pendingOn.filter((s) => chosen.includes(s) && !fcOn(s) && !readyNow(s) && planWhy(s) == null)
  const canForecast = chosen.some((s) => fcOn(s) || pending.includes(s))
  const rescoreOn = canForecast && rescore
  const rebuildOn = canForecast && (rebuild || rescore)
  const allOnDisk = chosen.length === syms.length && syms.every((s) => chosen.includes(s))
  const allNew = chosen.length > 0 && chosen.every((s) => !syms.includes(s))
  const nothing = !chosen.length || (download ? !tfs.length : !rebuildOn)
  const body: UpdateBody = {
    symbols: chosen, tfs: download ? tfs : undefined, years, rebuild: rebuildOn, rescore: rescoreOn,
    download, enable_forecast: pending,
  }
  const label = !download ? (rescoreOn ? 'Rebuild + rescore' : 'Rebuild forecast')
    : `${allNew ? 'Download' : 'Update'} ${chosen.length === 1 ? chosen[0]
      : allOnDisk ? 'all' : `${chosen.length} symbols`}`

  const add = (s: string) => setSel((cur) => {
    const c = cur ?? syms
    return c.includes(s) ? c : [...c, s]
  })
  const drop = (s: string) => {
    setSel(chosen.filter((x) => x !== s))
    setPendingOn((p) => p.filter((x) => x !== s))
  }
  const toggleTf = (tf: string) => setTfs((cur) =>
    cur.includes(tf) ? cur.filter((x) => x !== tf) : TF_ORDER.filter((x) => cur.includes(x) || x === tf))
  /** A symbol on disk that can carry the engine is switched now; one waiting on this download, after it. */
  const engine = (s: string, on: boolean) => {
    if (fcOn(s) || readyNow(s)) {
      flip(s, on)
      return
    }
    setPendingOn((p) => (on ? [...p.filter((x) => x !== s), s] : p.filter((x) => x !== s)))
  }
  const flip = (s: string, on: boolean) => {
    // Off is a standing change - updates stop rebuilding the tables and they go
    // stale - so it is asked, never taken from a stray click.
    if (!on && !window.confirm(`Switch the forecast engine off for ${s}? Its tables stay, but updates `
      + 'stop rebuilding them, so the lab shows them as stale.')) return
    setSwitching(true)
    setNotice(null)
    api.dataForecast(s, on)
      .then((res: any) => {
        if (res.ok === false) setNotice({ where: 'update', kind: 'error', text: res.error })
        else setSt(res)
      })
      .catch((e) => setNotice({ where: 'update', kind: 'error', text: String(e?.message ?? e) }))
      .finally(() => setSwitching(false))
  }
  const engineNote = (s: string) => {
    const f = per[s]
    if (fcOn(s)) {
      return f?.built_ms
        ? `tables ${day(f.built_ms)} · gates ${f.scored_ms ? day(f.scored_ms) : 'not scored yet'}`
          + (f.promoted ? ` · passed: ${passed(f.promoted)}` : '')
        : 'on - its tables are built by the next update with "rebuild forecast tables"'
    }
    if (pending.includes(s)) {
      return `switched on after the download, then built - every bar since ${needs?.from_year ?? 2018}, `
        + 'an hour or more'
    }
    if (readyNow(s)) return 'off'
    const why = planWhy(s)
    return why ? `off - ${why}` : 'off - tick to switch it on once this download brings its history'
  }

  return (
    <div className="md">
      {/* ------------------------------------------------ 1. history on disk */}
      <section className="md-card">
        <div className="set-head">History on disk</div>
        <div className="set-note">
          The backtest lab, the forecast tables and the shadow log read the history on disk. An update
          tops up each timeframe from the newest bar already there, on the MT5 bridge's own session -
          then refreshes the release history and rebuilds the forecast tables. On a trading day live
          quotes and orders wait while MT5 downloads (seconds for a month); at the weekend nothing is quoted.
        </div>
        {syms.length > 0 && (
          <table className="md-table mono md-hist">
            <thead><tr><th />{cols.map((t) => <th key={t}>{t}</th>)}</tr></thead>
            <tbody>
              {syms.map((s) => {
                const cov = st.coverage[s]
                return (
                  <React.Fragment key={s}>
                    <tr className="md-hist-sym"><td colSpan={cols.length + 1}>{s}</td></tr>
                    <tr>
                      <td className="t-dim">from</td>
                      {cols.map((t) => <td key={t}>{cov[t]?.first_year ?? ''}</td>)}
                    </tr>
                    <tr>
                      <td className="t-dim">to</td>
                      {cols.map((t) => {
                        const ms = cov[t]?.last_ms
                        return <td key={t} className={behind(t, ms) ? 't-warn' : ''}
                          title={ms ? new Date(ms).toISOString().replace('T', ' ').slice(0, 16) + ' broker time' : ''}>
                          {cov[t] ? day(ms) : ''}</td>
                      })}
                    </tr>
                  </React.Fragment>
                )
              })}
            </tbody>
          </table>
        )}
      </section>

      {/* ------------------------------------- 2. download & forecast engine */}
      <section className="md-card">
        <div className="set-head">Download &amp; forecast engine</div>
        <div className="set-row md-wrap">
          <span className="t-dim md-lbl">symbols</span>
          {chosen.map((s) => (
            <button key={s} className="pill on" disabled={job.running} onClick={() => drop(s)}
              title={`${s}${fcOn(s) ? ' - forecast engine on (◆)' : ''}${syms.includes(s) ? '' : ' - new'}: click to leave it out`}>
              {s}{fcOn(s) ? ' ◆' : ''}<span className="md-x">×</span></button>
          ))}
          <button className="tool-btn" disabled={job.running} onClick={() => setPicking(true)}>🔍 Search symbols…</button>
        </div>
        <div className="set-row md-wrap">
          <span className="t-dim md-lbl">timeframes</span>
          {TF_ORDER.map((tf) => (
            <button key={tf} className={`pill ${tfs.includes(tf) ? 'on' : ''}`} disabled={job.running || !download}
              onClick={() => toggleTf(tf)}>{tf.toUpperCase()}</button>
          ))}
          <label className="t-dim md-years"
            title="For a timeframe not on disk yet. One already there is topped up from its newest bar - its history stays as deep as its first download.">
            history
            <select value={years} disabled={job.running || !download} onChange={(e) => setYears(Number(e.target.value))}>
              {YEARS.map((y) => <option key={y} value={y}>{y} year{y > 1 ? 's' : ''}</option>)}
            </select>
          </label>
        </div>
        <div className="set-row md-wrap">
          <span className="t-dim md-lbl">steps</span>
          <label className="set-toggle" title="Off: leave MT5 alone and only rebuild or rescore the forecast tables">
            <input type="checkbox" checked={download} disabled={job.running}
              onChange={(e) => setDownload(e.target.checked)} /> download from MT5
          </label>
          <label className={`set-toggle ${canForecast ? '' : 'md-off'}`}
            title={canForecast ? 'The forecast tables of the chosen symbols whose engine is on'
              : 'None of the chosen symbols has its forecast engine on - see the forecast row'}>
            <input type="checkbox" checked={rebuildOn} disabled={job.running || !canForecast || rescoreOn}
              onChange={(e) => setRebuild(e.target.checked)} /> rebuild forecast tables
          </label>
          <label className={`set-toggle ${canForecast ? '' : 'md-off'}`}
            title="Re-run the promotion gates on the longer history. A gate can change what the lab shows - off by default so that is a decision, not a side effect. Adds a minute or two per symbol.">
            <input type="checkbox" checked={rescoreOn} disabled={job.running || !canForecast}
              onChange={(e) => setRescore(e.target.checked)} /> rescore the forecast gates
          </label>
        </div>
        {chosen.length > 0 && (
          <div className="set-row md-fcrow">
            <span className="t-dim md-lbl">forecast</span>
            <div className="md-fclist">
              {chosen.map((s) => {
                const on = fcOn(s)
                const can = on || readyNow(s) || planWhy(s) == null
                return (
                  <div key={s} className="md-fc">
                    <label className={`set-toggle ${can ? '' : 'md-off'}`}
                      title={on ? 'Forecast engine on - untick to switch it off'
                        : can ? 'Switch the forecast engine on' : planWhy(s) ?? ''}>
                      <input type="checkbox" checked={on || pending.includes(s)}
                        disabled={switching || job.running || !can}
                        onChange={(e) => engine(s, e.target.checked)} />
                      <span className="mono">{s}</span>
                    </label>
                    <span className="t-dim">{engineNote(s)}</span>
                  </div>
                )
              })}
            </div>
          </div>
        )}
        <div className="set-row">
          {job.running
            ? <button className="tool-btn" onClick={() => api.dataCancel().then(load)}>Cancel</button>
            : <button className="tool-btn primary" onClick={() => run('update', body)} disabled={busy || nothing}>
              {busy && last?.where === 'update' ? 'Starting…' : label}</button>}
        </div>
        {say('update')}
        {(job.running || job.finished_ms) && (
          <>
            <UpdateProgress job={job} aside={
              <button className="md-link" onClick={() => setDetails((d) => !d)}>
                {details ? 'hide details' : 'details'}</button>} />
            {details && (
              <div className="md-details">
                {(job.symbols ?? []).length > 0 && (
                  <div className="t-dim">
                    {job.symbols.join(', ')}{(job.new_symbols ?? []).length ? ` · new: ${job.new_symbols.join(', ')}` : ''}
                    {(job.forecast ?? []).length ? ` · forecast: ${job.forecast.join(', ')}` : ''}
                    {' · '}{job.running ? `running since ${stamp(job.started_ms)}`
                      : `${job.phase === 'failed' ? 'stopped' : 'finished'} ${stamp(job.finished_ms)}`}
                    {job.reason ? ` · ${job.reason}` : ''}
                  </div>
                )}
                <div className="md-steps">
                  {(job.steps ?? []).map((s: any) => (
                    <span key={s.name} className={`md-step ${s.state}`} title={s.note || s.state}>
                      {s.state === 'done' ? '✓' : s.state === 'running' ? '…' : s.state === 'failed' ? '✕'
                        : s.state === 'skipped' ? '–' : '·'} {STEP_LABEL[s.name] ?? s.name}
                      {s.note ? <span className="t-dim"> ({s.note})</span> : null}
                    </span>
                  ))}
                </div>
                <pre className="md-log">{(job.log ?? []).slice(-40).join('\n') || ' '}</pre>
              </div>
            )}
          </>
        )}
        {picking && (
          <SymbolPicker current="" onPick={(s) => { add(s); setPicking(false) }}
            // The bridge answers an empty list when it is slow; ask once more before
            // saying "nothing matches".
            load={() => api.symbols().then((x) => x.symbols.length ? x.symbols
              : new Promise<void>((ok) => window.setTimeout(ok, 1500))
                .then(() => api.symbols()).then((y) => y.symbols))}
            onClose={() => setPicking(false)}
            hint="Pick an instrument to add to this update: one on disk is topped up, a new one gets the chosen history." />
        )}
      </section>

      {/* ------------------------------------------------- 3. monthly routine */}
      <section className="md-card">
        <div className="set-head">Monthly routine</div>
        <label className="set-toggle">
          <input type="checkbox" checked={!!r.auto}
            onChange={(e) => api.dataRoutine(e.target.checked).then(setSt)
              .catch((x) => setNotice({ where: 'routine', kind: 'error', text: String(x?.message ?? x) }))} />
          Update automatically once a month
        </label>
        {say('routine')}
        <div className="set-note" style={{ marginTop: 6 }}>
          Runs {r.rule ?? 'at the weekend, once a month'} - when the market is shut, so nothing waits on MT5 -
          tops up every symbol on disk and rebuilds the forecast tables of each whose engine is on (gates
          are not rescored automatically). Last update {stamp(r.last_ok_ms)}; {r.due ? 'due now' : `next due ${day(r.next_due_ms)}`}.
        </div>
        {(st.history ?? []).length > 0 && (
          <table className="md-table mono">
            <thead><tr><th>when</th><th>how</th><th>symbols</th><th>result</th></tr></thead>
            <tbody>
              {[...st.history].reverse().slice(0, 6).map((h: any) => (
                <tr key={h.at_ms}><td>{stamp(h.at_ms)}</td><td>{h.reason}</td>
                  <td>{(h.symbols ?? []).join(', ')}</td>
                  <td className={h.ok ? 't-up' : 't-down'}>{h.ok ? 'ok' : h.error}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
