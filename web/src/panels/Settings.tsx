import React, { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { dateUTC, fmt } from '../lib/format'
import { Empty } from './common'

type Tab = 'alerts' | 'news' | 'destinations' | 'risk' | 'log'

const TABS: { key: Tab; label: string }[] = [
  { key: 'alerts', label: 'ALERTS' },
  { key: 'news', label: 'NEWS' },
  { key: 'destinations', label: 'DESTINATIONS' },
  { key: 'risk', label: 'RISK & GATES' },
  { key: 'log', label: 'LOG' },
]

type AlertsPayload = {
  config: any
  status: any
  symbols: string[]
  timeframes: string[]
  matrix: Record<string, Record<string, boolean>>
  has_data: Record<string, boolean>
  kinds: string[]
  watched_symbols: string[]
  bots: string[]
  bot_info: Record<string, { ok: boolean; username?: string; error?: string }>
}

/**
 * The settings modal.
 *
 * The instrument x timeframe grid is the centrepiece: alerting is a decision
 * per cell, not per instrument, because a setup worth a message on 1h is
 * usually noise on 1m. Row and column headers toggle whole lines so filling it
 * in does not take forty clicks.
 */
export function Settings({ onClose, liveTf, watchlist = [] }: {
  onClose: () => void
  liveTf?: string
  /** Signals only exist for these, so they are the only rows worth showing. */
  watchlist?: string[]
}) {
  const [tab, setTab] = useState<Tab>('alerts')
  const [data, setData] = useState<AlertsPayload | null>(null)
  const [engine, setEngine] = useState<any>(null)
  const [log, setLog] = useState<any[]>([])
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  // Edits live in a draft until Save. The grid used to write on every click,
  // which meant there was no way to back out of a change and no Cancel that
  // meant anything.
  const [draft, setDraft] = useState<any>(null)
  const [dirty, setDirty] = useState(false)
  const [resolved, setResolved] = useState<Record<string, any>>({})
  const [news, setNews] = useState<any>(null)

  const load = async () => {
    try {
      const [a, s] = await Promise.all([api.alertsConfig(), api.settings()])
      setData(a)
      setDraft(JSON.parse(JSON.stringify(a.config)))
      setDirty(false)
      setEngine(s)
      for (const d of a.config.destinations ?? []) if (d.target) resolve(d.target, d.bot)
    } catch (e: any) { setToast(e.message) }
  }

  /**
   * Ask Telegram what a chat is AND whether this bot can post to it.
   *
   * Keyed by target+bot because the answer depends on which bot is asking: a
   * public channel is readable by any bot and postable only by one that has
   * been added to it.
   */
  const rkey = (target: string, bot?: string) => `${bot || 'default'}|${target}`
  const resolve = async (target: string, bot?: string) => {
    if (!target) return
    try {
      const r = await api.alertsResolve(target, bot)
      setResolved((m) => ({ ...m, [rkey(target, bot)]: r }))
    } catch { /* leave it unresolved */ }
  }

  useEffect(() => { load() }, [])
  useEffect(() => {
    if (tab === 'log') api.alertsLog().then((r) => setLog(r.entries)).catch(() => {})
    if (tab === 'news') fetchNews()
  }, [tab])

  const fetchNews = async () => {
    try { setNews(await api.news(draft?.news?.impact ?? 'high')) }
    catch (e: any) { flash(e.message) }
  }

  const flash = (m: string) => { setToast(m); setTimeout(() => setToast(null), 4000) }

  /** Edit the draft. Nothing reaches disk until Save. */
  const patch = (p: any) => {
    setDraft((d: any) => ({ ...d, ...p }))
    setDirty(true)
  }

  const toggleCell = (symbol: string, tf: string, enabled: boolean) => {
    setData((d) => d && ({
      ...d,
      matrix: { ...d.matrix, [symbol]: { ...d.matrix[symbol], [tf]: enabled } },
    }))
    setDraft((d: any) => {
      const watch = [...(d.watch ?? [])]
      const i = watch.findIndex((c: any) => c.symbol === symbol && c.tf === tf)
      if (i >= 0) watch[i] = { ...watch[i], enabled }
      else watch.push({ symbol, tf, enabled, note: '' })
      return { ...d, watch }
    })
    setDirty(true)
  }

  const toggleRow = (symbol: string, on: boolean) => {
    if (!data) return
    for (const tf of data.timeframes) toggleCell(symbol, tf, on)
  }

  const save = async () => {
    setBusy(true)
    try {
      const r = await api.alertsSave(draft)
      setData((d) => (d ? { ...d, config: r.config, status: r.status } : d))
      setDirty(false)
      flash('Saved to configs/alerts.json')
    } catch (e: any) { flash(e.message) } finally { setBusy(false) }
  }

  const cancel = () => {
    if (dirty && !window.confirm('Discard unsaved changes?')) return
    onClose()
  }

  const testOne = async (d: any) => {
    setBusy(true)
    try {
      const r = await api.alertsTest(d.target, d.bot)
      flash(r.ok ? `Test sent to ${d.label || d.target}`
        : `Failed: ${r.results?.[0]?.error ?? 'unknown'}`)
    } catch (e: any) { flash(e.message) } finally { setBusy(false) }
  }

  const cfg = draft
  const st = data?.status
  // Counted over the watchlist only. A cell left on for a symbol since
  // removed from the watchlist can never fire - no signal is generated for
  // it - so counting it would overstate what is actually live.
  const onWatch = (s: string) => !watchlist.length || watchlist.includes(s)
  const watchedCount = (draft?.watch ?? [])
    .filter((c: any) => c.enabled && onWatch(c.symbol)).length

  const body = () => {
    if (!data || !draft) return <Empty>Loading settings…</Empty>

    if (tab === 'alerts') {
      // Instruments you have configured lead, then anything with local
      // history, then the rest. Sixteen alphabetical cards buried XAUUSD.a -
      // the only one actually set up - at position sixteen.
      const rank = (s: string) => {
        const on = data.timeframes.some((tf) => data.matrix[s]?.[tf])
        if (on) return 0
        return data.has_data[s] ? 1 : 2
      }
      // Watchlist symbols only, in watchlist order: signals are generated for
      // nothing else, so an alert cell for any other instrument could never
      // fire. Symbols on the watchlist that the alert config has not seen yet
      // still get a row, so they can be switched on here.
      const shown = watchlist.length
        ? [...watchlist]
        : [...data.symbols].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
      return (
        <>
          <div className="set-row">
            <label className="set-toggle" style={{ cursor: 'pointer' }}
              onClick={() => patch({ enabled: !cfg.enabled })}>
              <span className={`switch ${cfg.enabled ? 'on' : ''}`}><i /></span>
              <span className={cfg.enabled ? 't-up' : 't-dim'}
                style={{ fontWeight: 800, letterSpacing: '0.08em' }}>
                {cfg.enabled ? 'ALERTING ON' : 'ALERTING OFF'}
              </span>
            </label>
            <span className="spacer" />
            {!st?.telegram_ready && (
              <span className="chip chip-warn">TELEGRAM_BOT_TOKEN not set</span>
            )}
            <span className="chip chip-mute">{watchedCount} on</span>
          </div>

          <div className="set-note">
            Click a timeframe to switch it on or off. Only{' '}
            <b>{(cfg.statuses ?? []).join(' + ') || 'qualified'}</b> signals at
            confidence <b>{cfg.min_confidence}</b> or above are sent, at most one
            per instrument every <b>{cfg.cooldown_minutes}</b> min.
          </div>

          {shown.map((sym) => {
            const row = data.matrix[sym] ?? {}
            const on = data.timeframes.filter((tf) => row[tf]).length
            return (
              <div className="inst-card" key={sym}>
                <div className="inst-head">
                  <span className="inst-name">{sym}</span>
                  <span className="spacer" />
                  {on > 0 && (
                    <button className="inst-x" title={`Switch every timeframe off for ${sym}`}
                      onClick={() => toggleRow(sym, false)}>&times;</button>
                  )}
                </div>
                <div className="pill-row">
                  {data.timeframes.map((tf) => (
                    <button key={tf}
                      className={`pill ${row[tf] ? 'on' : ''} ${tf === liveTf ? 'live' : ''}`}
                      title={tf === liveTf ? 'on the chart right now' : undefined}
                      onClick={() => toggleCell(sym, tf, !row[tf])}>{tf}</button>
                  ))}
                </div>
                <div className="inst-foot">
                  {on} of {data.timeframes.length} announcing
                  {!data.has_data[sym] && <span> &middot; no local history, live only</span>}
                </div>
              </div>
            )
          })}

          <div className="set-head">Thresholds</div>
          <div className="set-grid">
            <Field label="Minimum confidence" hint="below this, nothing is sent">
              <input type="number" min={0} max={100} value={cfg.min_confidence}
                onChange={(e) => patch({ min_confidence: +e.target.value })} />
            </Field>
            <Field label="Cooldown (minutes)" hint="per instrument + timeframe">
              <input type="number" min={0} max={240} value={cfg.cooldown_minutes}
                onChange={(e) => patch({ cooldown_minutes: +e.target.value })} />
            </Field>
            <Field label="Statuses" hint="which verdicts are worth a message">
              <select value={(cfg.statuses ?? []).join(',')}
                onChange={(e) => patch({ statuses: e.target.value.split(',') })}>
                <option value="qualified">qualified only</option>
                <option value="qualified,watch">qualified + watch</option>
              </select>
            </Field>
            <Field label="Quiet hours (UTC)" hint="suppress overnight">
              <div style={{ display: 'flex', gap: 5, alignItems: 'center' }}>
                <input type="checkbox" style={{ width: 'auto' }}
                  checked={!!cfg.quiet_hours_utc?.enabled}
                  onChange={(e) => patch({
                    quiet_hours_utc: { ...cfg.quiet_hours_utc, enabled: e.target.checked },
                  })} />
                <input type="number" min={0} max={23} style={{ width: 52 }}
                  value={cfg.quiet_hours_utc?.from ?? 22}
                  onChange={(e) => patch({
                    quiet_hours_utc: { ...cfg.quiet_hours_utc, from: +e.target.value },
                  })} />
                <span className="t-dim">to</span>
                <input type="number" min={0} max={23} style={{ width: 52 }}
                  value={cfg.quiet_hours_utc?.to ?? 6}
                  onChange={(e) => patch({
                    quiet_hours_utc: { ...cfg.quiet_hours_utc, to: +e.target.value },
                  })} />
              </div>
            </Field>
          </div>

          <label className="set-toggle" style={{ marginTop: 10 }}>
            <input type="checkbox" checked={!!cfg.include_challenge}
              onChange={(e) => patch({ include_challenge: e.target.checked })} />
            <span>Include the counter-argument in each message</span>
          </label>
          <div className="set-note">
            Recommended. An alert that only argues one side trains you to stop
            thinking, and arguing both is the point of this system.
          </div>
        </>
      )
    }

    if (tab === 'destinations') {
      const dests = cfg.destinations ?? []
      const update = (i: number, p: any) =>
        patch({ destinations: dests.map((d: any, j: number) => (i === j ? { ...d, ...p } : d)) })
      const toggleKind = (i: number, kind: string) => {
        const cur = dests[i].kinds ?? []
        update(i, {
          kinds: cur.includes(kind) ? cur.filter((k: string) => k !== kind) : [...cur, kind],
        })
      }
      return (
        <>
          <div className="set-note">
            Tick what each destination receives, and choose which bot sends it.
            Posting rights are per bot: a bot that administrates one channel is
            a stranger to the next.
          </div>
          {(data.bots ?? []).length > 0 && (
            <div className="set-note t-dim">
              Configured senders:{' '}
              {(data.bots ?? []).map((b: string) => (
                <span key={b} className="chip chip-mute" style={{ marginRight: 4 }}>
                  {data.bot_info?.[b]?.username
                    ? `@${data.bot_info[b].username}` : b}
                  {b === 'default' ? ' (primary)' : ''}
                </span>
              ))}
            </div>
          )}
          {dests.length === 0 && (
            <div className="set-note t-dim" style={{ padding: '8px 0' }}>
              No destinations yet.
            </div>
          )}
          {dests.map((d: any, i: number) => {
            const info = resolved[rkey(d.target, d.bot)]
            const kinds = d.kinds ?? []
            return (
              <div className={`dest ${d.enabled ? 'on' : ''}`} key={d.id ?? i}>
                <div className="dest-top">
                  <span className={`switch ${d.enabled ? 'on' : ''}`}
                    onClick={() => update(i, { enabled: !d.enabled })}><i /></span>
                  <select value={d.bot ?? 'default'} title="which bot sends"
                    onChange={(e) => {
                      const bot = e.target.value === 'default' ? undefined : e.target.value
                      update(i, { bot })
                      if (d.target) resolve(d.target, bot)
                    }}>
                    {(data.bots ?? ['default']).map((b: string) => (
                      <option key={b} value={b}>
                        {data.bot_info?.[b]?.username
                          ? `@${data.bot_info[b].username}` : b}
                      </option>
                    ))}
                  </select>
                  <input value={d.label ?? ''} placeholder="label" style={{ width: 130 }}
                    onChange={(e) => update(i, { label: e.target.value })} />
                  <input value={d.target ?? ''} placeholder="chat id or @channel"
                    className="mono" style={{ flex: 1, minWidth: 120 }}
                    onChange={(e) => update(i, { target: e.target.value })}
                    onBlur={(e) => resolve(e.target.value, d.bot)} />
                  <button className="tool-btn" disabled={busy}
                    onClick={() => testOne(d)}>Test</button>
                  <button className="inst-x" title="Remove"
                    onClick={() => patch({
                      destinations: dests.filter((_: any, j: number) => j !== i),
                    })}>&times;</button>
                </div>
                <div className="pill-row" style={{ marginTop: 6 }}>
                  {(data.kinds ?? ['signals', 'news', 'scalper']).map((k: string) => (
                    <button key={k} className={`pill ${kinds.includes(k) ? 'on' : ''}`}
                      onClick={() => toggleKind(i, k)}>{k}</button>
                  ))}
                </div>
                <div className="inst-foot">
                  {kinds.length === 0
                    ? <span className="t-warn">receives nothing &mdash; no kind is ticked</span>
                    : <span>receives {kinds.join(', ')}</span>}
                  {info && (info.ok
                    ? <span> &middot; {info.detail}{info.public && (
                      <span className="t-warn"> &middot; PUBLIC</span>)}</span>
                    : <span className="t-down"> &middot; {info.error}</span>)}
                  {/* getChat succeeding says nothing about posting: a public
                      channel is readable by every bot and postable only by one
                      that was added to it. */}
                  {info?.can_post && (info.can_post.ok
                    ? <span className="t-up"> &middot; can post ({info.can_post.status})</span>
                    : <span className="t-down"> &middot; CANNOT POST: {info.can_post.error}</span>)}
                </div>
              </div>
            )
          })}
          <button className="tool-btn" style={{ marginTop: 8 }}
            onClick={() => patch({
              destinations: [...dests, {
                id: `d${Date.now()}`, channel: 'telegram', label: 'New destination',
                target: '', enabled: false, kinds: [],
              }],
            })}>+ Add destination&hellip;</button>

          <div className="set-note t-warn" style={{ marginTop: 14 }}>
            <b>Before ticking `signals` on a public channel.</b> These signals are
            not validated out of sample &mdash; profit factor was 1.09 on 2026 and
            1.00 on 2025. Broadcasting them to subscribers is publishing trading
            calls to other people, which is a different act from alerting yourself.
          </div>
        </>
      )
    }

    if (tab === 'news') {
      const n = cfg.news ?? {}
      return (
        <>
          <label className="set-toggle">
            <span className={`switch ${n.enabled ? 'on' : ''}`}
              onClick={() => patch({ news: { ...n, enabled: !n.enabled } })}><i /></span>
            <span className={n.enabled ? 't-up' : 't-dim'}
              style={{ fontWeight: 800, letterSpacing: '0.08em' }}>
              MACRO RELEASE BLACKOUT
            </span>
          </label>
          <div className="set-note">
            When on, a signal inside the blackout window around a release is
            <b> rejected</b>, not just flagged — it is the news gate in the
            qualification ledger, so what this page shows and what actually
            vetoes a trade are the same number.
          </div>

          <div className="set-grid">
            <Field label="Blackout (minutes)" hint="either side of the release">
              <input type="number" min={0} max={120}
                value={n.lead_minutes ?? 10}
                onChange={(e) => patch({ news: { ...n, lead_minutes: +e.target.value } })} />
            </Field>
            <Field label="Impact" hint="which releases count">
              <select value={n.impact ?? 'high'}
                onChange={(e) => patch({ news: { ...n, impact: e.target.value } })}>
                <option value="high">high only</option>
                <option value="medium">medium and above</option>
                <option value="low">low and above</option>
              </select>
            </Field>
          </div>

          <div className="set-head">
            Upcoming
            <button className="tool-btn" style={{ marginLeft: 10 }}
              onClick={fetchNews}>Refresh</button>
          </div>

          {!news ? <div className="set-note t-dim">Loading…</div> : (
            <>
              <div className="set-note">
                {news.next
                  ? <>Next: <b>{news.next.currency} {news.next.title}</b> in{' '}
                    <b className={news.minutes_to_next < 30 ? 't-warn' : ''}>
                      {news.minutes_to_next} min
                    </b></>
                  : <>Nothing at this impact level in the next 24 hours.</>}
                {' '}<span className="t-dim">
                  ({news.total_events} events)
                </span>
                {/* Which providers are actually answering. A calendar that has
                    quietly lost a source still looks full, so the roster is
                    shown rather than a single source name. */}
                {news.sources && (
                  <div className="t-dim" style={{ fontSize: 9.5, marginTop: 3 }}>
                    sources:{' '}
                    {Object.entries(news.sources).map(([k, on]) => (
                      <span key={k} className={on ? 't-up' : 't-dim'}
                        style={{ marginRight: 7 }}>
                        {on ? '✓' : '✕'} {k}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              {news.events?.length > 0 && (
                <table className="tabular">
                  <thead><tr>
                    <th>When</th><th>Ccy</th><th>Impact</th><th>Event</th>
                    <th className="num">Forecast</th><th className="num">Previous</th>
                  </tr></thead>
                  <tbody>
                    {news.events.slice(0, 14).map((e: any, i: number) => (
                      <tr key={i}>
                        <td className={e.minutes_away < 0 ? 't-dim' : 't-hi'}>
                          {e.minutes_away < 0
                            ? 'passed'
                            : e.minutes_away < 90
                              ? `${Math.round(e.minutes_away)} min`
                              : `${(e.minutes_away / 60).toFixed(1)} h`}
                        </td>
                        <td className="t-mid">{e.currency}</td>
                        <td className={
                          e.impact === 'high' ? 't-down'
                            : e.impact === 'medium' ? 't-warn' : 't-dim'
                        }>{e.impact}</td>
                        <td>{e.title}</td>
                        <td className="num t-mid">{e.forecast || '—'}</td>
                        <td className="num t-dim">{e.previous || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}

          <div className="set-head">Scanner</div>
          <div className="set-note">
            There is no separate scheduler to register. The service scans every
            watched timeframe continuously while it is running — each one on a
            cadence matched to its own bar — and alerts fire from that scan. If
            the service is up, alerting is live.
          </div>
        </>
      )
    }

    if (tab === 'risk') {
      if (!engine) return <Empty>Loading…</Empty>
      // Risk decides how big a trade is, gates decide whether there is a trade
      // at all, execution decides how it reaches MT5. One tab, three panels.
      // Every change is validated and saved on the server (configs/settings.json),
      // so it survives a restart - nothing here needs a console flag.
      const r = engine.risk
      const g = engine.gates
      const x = engine.execution ?? {}
      const playbooks: string[] = engine.playbooks ?? []
      const disabled: string[] = g.disabled_playbooks ?? []
      const save = (group: string) => (p: any) =>
        api.saveSettings({ [group]: p }).then((res: any) => {
          if (res?.errors?.length) flash(`Not saved: ${res.errors.join('; ')}`)
          else flash('Saved')
          load()
        }).catch((e) => flash(e.message))
      const saveRisk = save('risk')
      const saveGates = save('gates')
      const saveExec = save('execution')
      const togglePlaybook = (name: string) => {
        const next = disabled.includes(name)
          ? disabled.filter((d) => d !== name)
          : [...disabled, name]
        saveGates({ disabled_playbooks: next })
      }
      const toggleAuto = () => {
        if (!x.auto && !window.confirm(
          'Turn AUTO trading on?\n\nEvery FINAL, qualified signal on a watchlist symbol will be ' +
          `sent to MT5 without asking - ${x.min_lots}-${x.max_lots} lots, ` +
          `${x.one_per_symbol_tf ? 'one per symbol and timeframe, ' : ''}` +
          `stop to +${r.trail_lock_r}R at TP1 then trailing ${r.trail_atr} ATR.`)) return
        saveExec({ auto: !x.auto })
      }
      return (
        <>
          <section className="set-block">
            <div className="set-block-head">
              <span className="set-block-title">Execution</span>
              <span className="set-block-sub">how a FINAL signal reaches MT5</span>
            </div>
            <div className="set-row" style={{ marginBottom: 12 }}>
              {/* The whole label is the target, not just the 30px pill: the big
                  "AUTO TRADING OFF" text reads as the button, and clicking it
                  used to do nothing. */}
              <label className="set-toggle" onClick={toggleAuto} style={{ cursor: 'pointer' }}>
                <span className={`switch ${x.auto ? 'on' : ''}`}><i /></span>
                <span className={x.auto ? 't-warn' : 't-dim'}
                  style={{ fontWeight: 800, letterSpacing: '0.08em' }}>
                  {x.auto ? 'AUTO TRADING ON' : 'AUTO TRADING OFF'}
                </span>
              </label>
              <span className="spacer" />
              <span className={`chip ${engine.trading_enabled ? 'chip-warn' : 'chip-mute'}`}
                title={engine.trading_enabled
                  ? 'The bridge was started with --enable-trading'
                  : 'The bridge was started read-only - nothing can be sent until it is armed'}>
                bridge {engine.trading_enabled ? 'ARMED' : 'read-only'}
              </span>
            </div>
            <div className="set-grid">
              <Field label="Min lots" hint="every order is at least this">
                <input type="number" step={0.01} min={0.01} defaultValue={x.min_lots}
                  onBlur={(e) => saveExec({ min_lots: +e.target.value })} />
              </Field>
              <Field label="Max lots" hint="every order is at most this, whatever sizing says">
                <input type="number" step={0.01} min={0.01} defaultValue={x.max_lots}
                  onBlur={(e) => saveExec({ max_lots: +e.target.value })} />
              </Field>
              <Field label="Entry tolerance (ATR)"
                hint="within this of the entry: market order; further: pending at the entry">
                <input type="number" step={0.05} min={0} defaultValue={x.entry_tolerance_atr}
                  onBlur={(e) => saveExec({ entry_tolerance_atr: +e.target.value })} />
              </Field>
              <Field label="One per symbol + timeframe" hint="a second signal waits for the slot">
                <select defaultValue={x.one_per_symbol_tf ? 'yes' : 'no'}
                  onChange={(e) => saveExec({ one_per_symbol_tf: e.target.value === 'yes' })}>
                  <option value="yes">yes</option>
                  <option value="no">no</option>
                </select>
              </Field>
              <Field label="Exit" hint="trail: whole position, no fixed target">
                <select defaultValue={r.exit_mode}
                  onChange={(e) => saveRisk({ exit_mode: e.target.value })}>
                  <option value="trail">trail after TP1</option>
                  <option value="partial">50% at TP1, rest to TP2</option>
                </select>
              </Field>
              <Field label="Take-profit at the broker"
                hint="held by MT5 itself - protects a trade while this system is off">
                <select defaultValue={x.broker_tp ?? 'tp2'}
                  onChange={(e) => saveExec({ broker_tp: e.target.value })}>
                  <option value="tp1">TP1 (all out at 1R - trailing never acts)</option>
                  <option value="tp2">TP2 (caps winners at TP2)</option>
                  <option value="cap">Safety cap (far target)</option>
                  <option value="none">None (stop only)</option>
                </select>
              </Field>
              {x.broker_tp === 'cap' && (
                <Field label="Safety cap (R)" hint="distance from entry, in multiples of the risk">
                  <input type="number" step={0.5} min={1.5} max={10} defaultValue={x.broker_tp_r}
                    onBlur={(e) => saveExec({ broker_tp_r: +e.target.value })} />
                </Field>
              )}
              <Field label="Lock at TP1 (R)" hint="the stop moves here when TP1 prints">
                <input type="number" step={0.1} min={0} defaultValue={r.trail_lock_r}
                  onBlur={(e) => saveRisk({ trail_lock_r: +e.target.value })} />
              </Field>
              <Field label="Trail distance (ATR)" hint="behind the best closed-bar price">
                <input type="number" step={0.1} min={0.2} defaultValue={r.trail_atr}
                  onBlur={(e) => saveRisk({ trail_atr: +e.target.value })} />
              </Field>
              <Field label="Max concurrent" hint="live orders from this system, all symbols">
                <input type="number" min={1} defaultValue={r.max_concurrent}
                  onBlur={(e) => saveRisk({ max_concurrent: +e.target.value })} />
              </Field>
            </div>
            <div className="set-note">
              {engine.trading_enabled
                ? 'The bridge is armed. With auto on, qualified FINAL signals are sent as soon as they appear.'
                : 'The bridge is read-only, so nothing is sent whatever these say. Arming it is the one ' +
                  'switch kept outside this page, on purpose - see the note below.'}
            </div>
          </section>

          <section className="set-block">
            <div className="set-block-head">
              <span className="set-block-title">Risk</span>
              <span className="set-block-sub">how big each trade is</span>
            </div>
            <div className="set-grid">
              <Field label="Equity" hint="overridden by the live account when connected">
                <input type="number" defaultValue={r.equity}
                  onBlur={(e) => saveRisk({ equity: +e.target.value })} />
              </Field>
              <Field label="Risk per trade %" hint="of equity, per idea (then clamped to the lot range)">
                <input type="number" step={0.05} defaultValue={r.risk_per_trade_pct}
                  onBlur={(e) => saveRisk({ risk_per_trade_pct: +e.target.value })} />
              </Field>
              <Field label="Max daily loss %" hint="stops the day">
                <input type="number" step={0.1} defaultValue={r.max_daily_loss_pct}
                  onBlur={(e) => saveRisk({ max_daily_loss_pct: +e.target.value })} />
              </Field>
              <Field label="Max daily trades">
                <input type="number" defaultValue={r.max_daily_trades}
                  onBlur={(e) => saveRisk({ max_daily_trades: +e.target.value })} />
              </Field>
              <Field label="Minimum R:R" hint="net to TP2, after costs">
                <input type="number" step={0.1} defaultValue={r.min_rr}
                  onBlur={(e) => saveRisk({ min_rr: +e.target.value })} />
              </Field>
              <Field label="Min stop (ATR)" hint="the noise floor; below this a stop samples noise">
                <input type="number" step={0.1} defaultValue={r.min_stop_atr}
                  onBlur={(e) => saveRisk({ min_stop_atr: +e.target.value })} />
              </Field>
            </div>
          </section>

          <section className="set-block">
            <div className="set-block-head">
              <span className="set-block-title">Gates</span>
              <span className="set-block-sub">whether a signal qualifies at all</span>
            </div>
            <div className="set-grid">
              <Field label="Min confidence" hint="below this a signal is 'watch', not 'qualified'">
                <input type="number" defaultValue={g.min_confidence}
                  onBlur={(e) => saveGates({ min_confidence: +e.target.value })} />
              </Field>
              <Field label="Max spread (points)">
                <input type="number" defaultValue={g.max_spread_points}
                  onBlur={(e) => saveGates({ max_spread_points: +e.target.value })} />
              </Field>
              <Field label="ATR cost multiple" hint="ATR must be worth this many round trips">
                <input type="number" step={0.5} defaultValue={g.min_atr_cost_multiple}
                  onBlur={(e) => saveGates({ min_atr_cost_multiple: +e.target.value })} />
              </Field>
              <Field label="Max ATR percentile" hint="above this is news-spike territory">
                <input type="number" defaultValue={g.max_atr_percentile}
                  onBlur={(e) => saveGates({ max_atr_percentile: +e.target.value })} />
              </Field>
              <Field label="Min MTF score" hint="0-100, agreement required">
                <input type="number" defaultValue={g.min_mtf_score}
                  onBlur={(e) => saveGates({ min_mtf_score: +e.target.value })} />
              </Field>
              <Field label="Cooldown (bars)" hint="between signals on one playbook">
                <input type="number" defaultValue={g.cooldown_bars}
                  onBlur={(e) => saveGates({ cooldown_bars: +e.target.value })} />
              </Field>
            </div>
            <div className="set-field" style={{ marginTop: 12 }}>
              <label>Playbooks</label>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
                {playbooks.map((name) => {
                  const on = !disabled.includes(name)
                  return (
                    <button key={name} className={`pill ${on ? 'on' : ''}`}
                      onClick={() => togglePlaybook(name)}
                      title={on ? 'Generating signals - click to switch off'
                        : 'Switched off - click to switch on'}>
                      {name.replace(/_/g, ' ')}
                    </button>
                  )
                })}
              </div>
              <div className="set-hint">
                Off by default: sweep reversal, breakout retest, range fade - negative across
                2024-2026 in testing. Switching one back on takes effect from the next bar.
              </div>
            </div>
            <div className="set-field" style={{ marginTop: 12 }}>
              <label>Auto-trading timeframes</label>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
                {(engine.timeframes ?? []).map((tf: string) => {
                  const allowed: string[] = x.auto_timeframes ?? []
                  const on = allowed.includes(tf)
                  return (
                    <button key={tf} className={`pill ${on ? 'on' : ''}`}
                      onClick={() => saveExec({
                        auto_timeframes: on ? allowed.filter((t) => t !== tf) : [...allowed, tf],
                      })}
                      title={on ? 'Auto mode sends FINAL signals from this timeframe'
                        : 'Auto mode ignores this timeframe'}>
                      {tf.toUpperCase()}
                    </button>
                  )
                })}
              </div>
              <div className="set-hint">
                Which timeframes AUTO trading may send from. Signals on the others still appear,
                alert and can be sent with the Place button - this limits automation only.
              </div>
            </div>
            <div className="set-note">
              Changing a gate re-evaluates every signal immediately. These are the
              same numbers the qualification ledger quotes, so a change here shows
              up as a different reason in the signal board.
            </div>
          </section>

          <div className="set-note" style={{ marginTop: 12 }}>
            Saved to <code>{engine.saved_to ?? 'configs/settings.json'}</code> and re-applied at every
            start. The one switch not on this page is arming the bridge itself
            (<code>--enable-trading</code>): it is the process that can move money, and keeping
            its on-switch outside the web page means no bug, stale tab or anyone else on this
            page can arm live trading.
          </div>
        </>
      )
    }

    // log
    if (!log.length) return <Empty>Nothing sent or suppressed yet.</Empty>
    return (
      <table className="tabular">
        <thead><tr>
          <th>When</th><th>Action</th><th>Symbol</th><th>TF</th>
          <th className="num">Conf</th><th>Reason / result</th>
        </tr></thead>
        <tbody>
          {log.map((e, i) => (
            <tr key={i}>
              <td className="t-dim">{dateUTC(e.ts)}</td>
              <td className={
                e.action === 'sent' ? 't-up' : e.action === 'failed' ? 't-down' : 't-mid'
              }>{e.action}</td>
              <td className="t-hi">{e.symbol ?? '—'}</td>
              <td>{e.tf ?? '—'}</td>
              <td className="num">{e.confidence ?? '—'}</td>
              <td className="t-dim" style={{ whiteSpace: 'normal' }}>
                {e.action === 'sent'
                  ? (e.results ?? []).map((r: any) =>
                    `${r.label}: ${r.ok ? 'ok' : r.error}`).join(' · ')
                  : e.reason}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    )
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span style={{ fontWeight: 800, letterSpacing: '0.1em', fontSize: 11 }}>
            SETTINGS
          </span>
          <span className="t-dim mono" style={{ fontSize: 9.5 }}>
            {st?.config_path}
          </span>
          <span className="spacer" />
          <button className="tool-btn" onClick={cancel}>Close</button>
        </div>
        <div className="modal-tabs">
          {TABS.map((t) => {
            const n = t.key === 'alerts' ? watchedCount
              : t.key === 'destinations'
                ? (draft?.destinations ?? []).filter((d: any) => d.enabled).length
                : null
            return (
              <button key={t.key} className={`dock-tab ${tab === t.key ? 'on' : ''}`}
                onClick={() => setTab(t.key)}>
                {t.label}
                {n !== null && <span className="tab-count">{n} on</span>}
              </button>
            )
          })}
        </div>
        <div className="modal-body">{body()}</div>
        {toast && <div className="modal-toast">{toast}</div>}
        <div className="modal-foot">
          {dirty && <span className="t-warn" style={{ fontSize: 10.5 }}>
            unsaved changes
          </span>}
          <span className="spacer" />
          <button className="tool-btn" onClick={cancel}>Cancel</button>
          <button className="tool-btn primary" onClick={save} disabled={busy || !dirty}>
            Save
          </button>
        </div>
      </div>
    </div>
  )
}

function Field({ label, hint, children }: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="set-field">
      <label>{label}</label>
      {children}
      {hint && <div className="set-hint">{hint}</div>}
    </div>
  )
}
