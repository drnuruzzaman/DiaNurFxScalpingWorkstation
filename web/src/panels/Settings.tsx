import React, { useEffect, useState, useRef } from 'react'
import { api } from '../lib/api'
import { dateUTC, fmt } from '../lib/format'
import { Empty } from './common'
import { MarketData } from './MarketData'

type Tab = 'alerts' | 'news' | 'destinations' | 'risk' | 'data' | 'log'
export type SettingsTab = Tab

/**
 * What each exit plan actually does, and what the measurements said.
 *
 * The numbers are from tools/exp_exits.py, run 2026-09-21 with spread charged
 * over the same entries. They are quoted here because "which exit should I
 * use" is exactly the question this dropdown asks, and the answer is already
 * known. See RiskSettings.exit_mode in server/config.py.
 */
const EXIT_HINT: Record<string, string> = {
  trail:
    'at TP1 the stop locks in, then follows price by the trail distance on '
    + 'closed bars. Whole position, no fixed target — TP2 becomes an '
    + 'objective, not an order',
  partial:
    'half closed at TP1, stop to break-even, the rest runs to TP2. Measured '
    + 'worse than trail in 2026 and level in 2025 — never better',
}

/**
 * The take-profit sent WITH the order, so MT5 holds it.
 *
 * The distinction that matters: the trailing stop runs in THIS server, and a
 * position keeps only what MT5 holds if the server or the PC is off. Every
 * option here is a different answer to "what happens if this is not running
 * while the trade is".
 */
const BROKER_TP_HINT: Record<string, string> = {
  tp1:
    'all out at 1R, so the trailing stop never gets to act — MT5 closes '
    + 'first. The worst exit measured in both 2025 and 2026: only ~36% of '
    + 'trades reach +1R',
  tp2:
    'banks the objective even with this system off, at the cost of capping '
    + 'every winner at TP2',
  cap:
    'a far target that is rarely hit, so it barely changes the tested exit '
    + 'while still covering a disconnect',
  none:
    'stop only — exactly the tested exit, but nothing is taken if this '
    + 'system is off when the trade runs',
}

const TABS: { key: Tab; label: string }[] = [
  { key: 'alerts', label: 'ALERTS' },
  { key: 'news', label: 'NEWS' },
  { key: 'destinations', label: 'DESTINATIONS' },
  { key: 'risk', label: 'RISK & GATES' },
  { key: 'data', label: 'MARKET DATA' },
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
export function Settings({ onClose, liveTf, watchlist = [], mt5, initialTab }: {
  onClose: () => void
  /** The tab to open on - Market data when opened from the footer's update. */
  initialTab?: Tab
  liveTf?: string
  /** Signals only exist for these, so they are the only rows worth showing. */
  watchlist?: string[]
  /**
   * Which MT5 account these settings will actually drive.
   *
   * The ribbon shows the server, because that is what you glance at. The
   * number belongs here: this is the screen where you arm auto trading and
   * set a lot size, and "which account am I about to do that to" is a
   * question worth answering on the same screen rather than from memory.
   */
  mt5?: { login: number | null; server: string | null;
          account_type?: string; connected?: boolean } | null
}) {
  const [tab, setTab] = useState<Tab>(initialTab ?? 'alerts')
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
      // Destinations are NOT resolved here. Each one is three Telegram round
      // trips - getChat, getChatMember, getMe - and takes about four seconds;
      // firing one per destination the moment this modal opens put ~12s of
      // outbound calls in front of whatever you actually came here to do.
      // Anything clicked in that window queued behind them, which is how the
      // AUTO switch came to look broken: the request was fine, it was just
      // waiting. They are resolved when the Destinations tab is opened.
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
  // Keys with a lookup already in the air. `resolved` only fills in when an
  // answer lands, and each of these takes about four seconds - long enough
  // for the effect below to run again and ask a second time for the same
  // chat. Without this the Destinations tab fired six Telegram round trips
  // for three destinations.
  const inFlight = useRef<Set<string>>(new Set())

  const resolve = async (target: string, bot?: string) => {
    if (!target) return
    const key = rkey(target, bot)
    if (inFlight.current.has(key)) return
    inFlight.current.add(key)
    try {
      const r = await api.alertsResolve(target, bot)
      setResolved((m) => ({ ...m, [key]: r }))
    } catch { /* leave it unresolved */ }
    finally { inFlight.current.delete(key) }
  }

  useEffect(() => { load() }, [])
  useEffect(() => {
    if (tab === 'log') api.alertsLog().then((r) => setLog(r.entries)).catch(() => {})
    if (tab === 'news') fetchNews()
    // Resolved on arrival, and only for the ones not already known, so
    // switching back to this tab costs nothing.
    if (tab === 'destinations') {
      for (const d of draft?.destinations ?? []) {
        if (d.target && !resolved[rkey(d.target, d.bot)]) resolve(d.target, d.bot)
      }
    }
    // `resolved` is deliberately not a dependency: it changes as each lookup
    // lands, and listing it would re-run this for every answer received.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, draft])

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

  /**
   * Staged edits to configs/settings.json, per group.
   *
   * Risk & Gates used to POST on every blur, so a half-typed lot size was
   * already live on the server before the field lost focus. It now behaves
   * like the alert tabs: accumulate, show Save, write once.
   */
  const [pending, setPending] = useState<Record<string, any>>({})
  const engineDirty = Object.values(pending).some(
    (g: any) => g && Object.keys(g).length > 0)

  const stage = (group: string) => (p: any) =>
    setPending((cur) => ({ ...cur, [group]: { ...(cur[group] || {}), ...p } }))

  /**
   * Bumped whenever the staged set is dropped or reloaded.
   *
   * The inputs are uncontrolled (defaultValue), so discarding has to REMOUNT
   * them - without a changing key they would keep showing the abandoned text
   * while the server holds the old value.
   */
  const [formKey, setFormKey] = useState(0)

  /**
   * True while the "turn AUTO on" confirmation is showing.
   *
   * This used to be a window.confirm(). Turning auto OFF took one click and
   * always worked; turning it back ON needed the dialog - and a browser that
   * has suppressed dialogs for a page ("prevent this page from creating
   * additional dialogs", which anyone dismissing a few of these eventually
   * ticks) makes confirm() return FALSE without showing anything at all. The
   * code read that as "the user declined" and did nothing, silently. The
   * switch simply stopped working in one direction, with no error and nothing
   * in the console to explain it.
   *
   * An in-page confirmation cannot be suppressed, so the one control that
   * decides whether this machine trades by itself can no longer be disabled
   * by a checkbox in a browser menu.
   */
  const [armingAuto, setArmingAuto] = useState(false)

  /**
   * The state being switched TO while the request is in flight, or null.
   *
   * Not an optimistic update - the switch does not claim to be on before the
   * server says so, because this is the control that decides whether the
   * machine trades by itself. It says "TURNING ON…" instead: the click
   * registered, the answer has not arrived. Without it a request that took a
   * couple of seconds looked exactly like a control that did nothing, which
   * invites a second click on the one switch that should never be clicked
   * twice by accident.
   */
  const [autoBusy, setAutoBusy] = useState<boolean | null>(null)

  const discardEngine = () => {
    setPending({})
    setFormKey((k) => k + 1)
  }

  const saveEngine = async () => {
    const res: any = await api.saveSettings(pending)
    if (res?.errors?.length) {
      flash(`Not saved: ${res.errors.join('; ')}`)
      return false
    }
    // Reload BEFORE clearing the draft and remounting.
    //
    // The other order - clear, remount, then load - batches the remount into
    // the same render as the cleared draft, so the fields rebuild against the
    // engine state that was there BEFORE the save. They are uncontrolled, so
    // whatever they mount with is what stays: a dropdown would sit showing the
    // old value after a successful save, while its own hint (which reads the
    // merged state) showed the new one. Reloading first means the remount can
    // only ever see what the server actually stored.
    await load()
    setPending({})
    setFormKey((k) => k + 1)
    return true
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

  const saveAlerts = async () => {
    const r = await api.alertsSave(draft)
    setData((d) => (d ? { ...d, config: r.config, status: r.status } : d))
    setDirty(false)
    flash('Saved')
  }

  /**
   * Both files, one button.
   *
   * The two tabs write to different places - alerts.json and settings.json -
   * but a single Save is what the footer has always implied, and two buttons
   * doing almost the same thing is how half a change gets left behind.
   */
  const anyDirty = dirty || engineDirty

  const saveAll = async () => {
    setBusy(true)
    try {
      let ok = true
      if (engineDirty) ok = await saveEngine()
      // The alert draft is only written if the settings half succeeded, so a
      // rejected lot size does not leave the two files half-applied.
      if (ok && dirty) await saveAlerts()
      else if (ok) flash('Saved')
    } catch (e: any) {
      flash(e.message)
    } finally { setBusy(false) }
  }

  const cancel = () => {
    setArmingAuto(false)
    if (anyDirty && !window.confirm('Discard unsaved changes?')) return
    discardEngine()
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
    // Market data needs nothing from the alerts or engine settings.
    if (tab === 'data') return <MarketData />
    if (!data || !draft) return <Empty>Loading settings…</Empty>

    if (tab === 'alerts') {
      // The file path belongs here, beside the settings it describes. In the
      // modal header it sat over every tab while naming only one of the two
      // files the dialog writes - Risk & Gates goes to settings.json.
      const pathNote = st?.config_path ? (
        <div className="t-dim mono" style={{ fontSize: 9.5, marginBottom: 8 }}>
          {st.config_path}
        </div>
      ) : null
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
          {pathNote}
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
            This switch is about <b>being told</b>: it sends a Telegram message
            ahead of a release, and the minutes below are how far ahead. It is
            not the number that vetoes a trade — the blackout that rejects a
            signal is <b>Block on high-impact news</b> in Risk &amp; Gates, with
            its own window. This page used to claim they were the same number;
            they never were.
          </div>

          <div className="set-grid">
            <Field label="Alert lead (minutes)" hint="how long before a release to message">
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
      // Staged values win over what the server last returned, so a field the
      // user has edited keeps showing their number rather than snapping back.
      const r = { ...engine.risk, ...(pending.risk || {}) }
      const g = { ...engine.gates, ...(pending.gates || {}) }
      const x = { ...(engine.execution ?? {}), ...(pending.execution || {}) }
      const playbooks: string[] = engine.playbooks ?? []
      const disabled: string[] = g.disabled_playbooks ?? []
      // The bridge's ceiling is a different process's setting; it can only be
      // compared, never edited from here.
      const bridgeMax: number | null = engine.bridge_max_lots ?? null
      // Either fixed size above the bridge's own ceiling would be refused at
      // SEND time with nothing on screen to explain why, so each box says so
      // while the number is still being chosen.
      const overGold = bridgeMax != null && Number(x.lots_gold ?? 0.01) > bridgeMax
      const overOther = bridgeMax != null && Number(x.lots_non_gold ?? 0.05) > bridgeMax

      const saveRisk = stage('risk')
      const saveGates = stage('gates')
      const saveExec = stage('execution')
      const togglePlaybook = (name: string) => {
        const next = disabled.includes(name)
          ? disabled.filter((d) => d !== name)
          : [...disabled, name]
        saveGates({ disabled_playbooks: next })
      }
      // APPLIED AT ONCE, unlike every other control on this tab. Staging it
      // behind Save would let the switch read "AUTO TRADING OFF" while orders
      // were still going out, which is the wrong way round for the one
      // control that decides whether the machine trades by itself.
      const setAuto = (on: boolean) => {
        setArmingAuto(false)
        setAutoBusy(on)
        api.saveSettings({ execution: { auto: on } }).then((res: any) => {
          if (res?.errors?.length) { flash(`Not saved: ${res.errors.join('; ')}`); return }
          flash(on ? 'AUTO trading ON' : 'AUTO trading OFF')
          // Move the switch from the answer the server just gave, rather than
          // waiting on load(). load() refetches the alerts config too, and
          // for those extra seconds the switch sat in its old position after
          // a change that had already succeeded. load() still runs, to
          // reconcile everything else.
          setEngine((e: any) => (e ? { ...e, execution: { ...e.execution, auto: on } } : e))
          load()
        }).catch((e) => flash(`Could not switch: ${e.message}`))
          .finally(() => setAutoBusy(null))
      }

      // Off is immediate - stopping the machine trading should never need a
      // second click. On asks first, in the page.
      const toggleAuto = () => (x.auto ? setAuto(false) : setArmingAuto(true))
      return (
        // Keyed so Cancel/Save REMOUNTS the fields. They are uncontrolled, so
        // without this a discarded edit would stay on screen while the server
        // still held the old value.
        <React.Fragment key={formKey}>
          <section className="set-block">
            <div className="set-block-head">
              <span className="set-block-title">Execution</span>
              <span className="set-block-sub">how a FINAL signal reaches MT5</span>
              <span className="spacer" />
              {/* What "armed" MEANS, on the heading line.
                  It used to sit at the foot of the block, four rows of numbers
                  away from the chip it explains - so the one line telling you
                  orders can leave this machine was the last thing read, if at
                  all. Level with the heading it is read first; the chip below
                  states the state itself. */}
              <span className="set-armed-note">
                {engine.trading_enabled
                  ? 'The bridge is armed. With auto on, qualified FINAL signals are '
                    + 'sent as soon as they appear.'
                  : 'The bridge is read-only, so nothing is sent whatever these say. '
                    + 'Arming it is the one switch kept outside this page, on purpose '
                    + '- see the note below.'}
              </span>
            </div>
            <div className="set-row" style={{ marginBottom: 12 }}>
              {/* The whole label is the target, not just the 30px pill: the big
                  "AUTO TRADING OFF" text reads as the button, and clicking it
                  used to do nothing. */}
              <label className="set-toggle"
                onClick={autoBusy === null ? toggleAuto : undefined}
                style={{ cursor: autoBusy === null ? 'pointer' : 'progress' }}>
                <span className={`switch ${x.auto ? 'on' : ''}`}><i /></span>
                <span className={autoBusy !== null ? 't-mid' : x.auto ? 't-warn' : 't-dim'}
                  style={{ fontWeight: 800, letterSpacing: '0.08em' }}>
                  {autoBusy !== null
                    ? `TURNING ${autoBusy ? 'ON' : 'OFF'}…`
                    : x.auto ? 'AUTO TRADING ON' : 'AUTO TRADING OFF'}
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
            {armingAuto && (
              // Deliberately spells out the settings it is about to hand the
              // machine, because those numbers ARE what "auto" means - and
              // they are on this very page, so they can be checked first.
              <div className="set-arm">
                <div className="set-arm-text">
                  <b className="t-warn">Turn AUTO trading on?</b>{' '}
                  {/* One string, not text interleaved with expressions across
                      several lines: JSX turns each of those line breaks into a
                      space, which left double gaps mid-sentence ("stop to
                      +0.5R at TP1") that no amount of CSS would close. */}
                  {'Every FINAL, qualified signal on a watchlist symbol is sent to MT5 '
                    + `without asking — gold ${x.lots_gold ?? 0.01} lots, `
                    + `other symbols ${x.lots_non_gold ?? 0.05}`
                    + (x.max_per_symbol_tf > 0
                      ? `, at most ${x.max_per_symbol_tf} per symbol and timeframe` : '')
                    + `, stop to +${r.trail_lock_r}R at TP1 then trailing ${r.trail_atr} ATR.`
                    + (engine.trading_enabled ? ''
                      : ' The bridge is read-only, so nothing will be sent until it is armed.')}
                </div>
                {/* No spacer: the text itself now takes the slack, so a
                    second flexible element would only compete with it. */}
                <button className="tool-btn" onClick={() => setArmingAuto(false)}>
                  Cancel
                </button>
                <button className="tool-btn primary" onClick={() => setAuto(true)}>
                  Turn on
                </button>
              </div>
            )}
            <div className="set-grid">
              {/* One FIXED size per instrument class - not a min/max range.
                  The range had been set to 0.01-0.01, a fixed lot expressed as
                  two boxes that had to be kept equal by hand. Two classes,
                  because the contracts are not comparable: 0.01 of gold is
                  1 oz, 0.01 of a major is 1,000 units. */}
              <Field label="Lots (gold)"
                hint={overGold
                  ? `the bridge refuses anything above ${bridgeMax} — relaunch it to raise this`
                  : 'fixed size for every gold order'}>
                <input type="number" step={0.01} min={0.01} defaultValue={x.lots_gold ?? 0.01}
                  className={overGold ? 'bad' : undefined}
                  onBlur={(e) => saveExec({ lots_gold: +e.target.value })} />
              </Field>
              <Field label="Lots (other symbols)"
                hint={overOther
                  ? `the bridge refuses anything above ${bridgeMax} — relaunch it to raise this`
                  : 'fixed size for every non-gold order'}>
                <input type="number" step={0.01} min={0.01} defaultValue={x.lots_non_gold ?? 0.05}
                  className={overOther ? 'bad' : undefined}
                  onBlur={(e) => saveExec({ lots_non_gold: +e.target.value })} />
              </Field>
              <Field label="Entry tolerance (ATR)"
                hint="within this of the entry: market order; further: pending at the entry">
                <input type="number" step={0.05} min={0} defaultValue={x.entry_tolerance_atr}
                  onBlur={(e) => saveExec({ entry_tolerance_atr: +e.target.value })} />
              </Field>
              <Field label="Max per symbol + timeframe"
                hint={x.max_per_symbol_tf > 0
                  ? `further signals wait for a slot · 0 = no cap`
                  : 'no cap — every qualified signal is sent'}>
                <input type="number" min={0} max={20} step={1}
                  defaultValue={x.max_per_symbol_tf}
                  onBlur={(e) => saveExec({ max_per_symbol_tf: +e.target.value })} />
              </Field>
              {/* These three fields describe ONE mechanism between them, and a
                  hint that only names the field leaves you to work out how
                  they interact. Each now explains the option actually
                  selected, so the text changes as the choice does. */}
              <Field label="Exit" hint={EXIT_HINT[r.exit_mode] ?? ''}>
                <select defaultValue={r.exit_mode}
                  onChange={(e) => saveRisk({ exit_mode: e.target.value })}>
                  <option value="trail">trail after TP1</option>
                  <option value="partial">50% at TP1, rest to TP2</option>
                </select>
              </Field>
              <Field label="Take-profit at the broker"
                hint={r.exit_mode === 'partial'
                  // Worth saying plainly: in partial mode the executor sends
                  // TP2 whatever this says. A setting that silently does
                  // nothing is worse than one that is missing.
                  ? 'not used while Exit is "50% at TP1" — that plan always sends TP2 as the order target'
                  : BROKER_TP_HINT[x.broker_tp ?? 'tp2'] ?? ''}>
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
              <Field label="Lock at TP1 (R)"
                hint={r.exit_mode === 'partial'
                  ? 'not used while Exit is "50% at TP1" — that plan moves the stop to break-even instead'
                  : +r.trail_lock_r > 0
                    ? `when price touches TP1 the stop jumps to +${r.trail_lock_r}R above your fill, `
                      + 'so that much is banked however the trade ends'
                    : 'break-even: when price touches TP1 the stop moves to your fill, '
                      + 'so the trade can no longer lose'}>
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
            {/* The news blackout, where the veto lives.
                The NEWS tab has a switch with a similar name, but that one is
                about Telegram messages and its "blackout (minutes)" is the
                alert lead, not this window. This is the one the qualification
                ledger reads. */}
            <div className="set-row" style={{ marginBottom: 12 }}>
              <label className="set-toggle" style={{ cursor: 'pointer' }}
                onClick={() => saveGates({ news_blocks: !g.news_blocks })}>
                <span className={`switch ${g.news_blocks ? 'on' : ''}`}><i /></span>
                <span className={g.news_blocks ? 't-up' : 't-dim'}
                  style={{ fontWeight: 800, letterSpacing: '0.08em' }}>
                  BLOCK ON HIGH-IMPACT NEWS
                </span>
              </label>
              <span className="spacer" />
              <span className="set-armed-note">
                {g.news_blocks
                  ? `A signal within ${g.news_blackout_min} minutes either side of a `
                    + 'high-impact release is rejected outright.'
                  : `A release within ${g.news_blackout_min} minutes is still flagged and `
                    + 'still costs the setup confidence, but no longer vetoes the trade.'}
              </span>
            </div>
            {/* The Phase 0b avoid rules - the 'leg' gate in qualify.py. Read
                as on until the API reports it, so an older server does not
                show a real setting as off. */}
            <div className="set-row" style={{ marginBottom: 12 }}>
              <label className="set-toggle" style={{ cursor: 'pointer' }}
                onClick={() => saveGates({ avoid_fades: g.avoid_fades === false })}>
                <span className={`switch ${g.avoid_fades !== false ? 'on' : ''}`}><i /></span>
                <span className={g.avoid_fades !== false ? 't-up' : 't-dim'}
                  style={{ fontWeight: 800, letterSpacing: '0.08em' }}>
                  AVOID FADING THE LEG
                </span>
              </label>
              <span className="spacer" />
              <span className="set-armed-note">
                {g.avoid_fades !== false
                  ? 'Trades against the leg in progress are rejected 0.5–1 ATR off its '
                    + 'extreme, once it has run 4 ATR, or with the 1h trend behind it.'
                  : 'The leg read is still shown on each signal, but no longer vetoes '
                    + 'a trade against the leg.'}
              </span>
            </div>
            <div className="set-grid">
              <Field label="News blackout (minutes)"
                hint={g.news_blocks ? 'either side of the release'
                  : 'the window is still measured — it just does not block'}>
                <input type="number" min={0} max={120} defaultValue={g.news_blackout_min}
                  onBlur={(e) => saveGates({ news_blackout_min: +e.target.value })} />
              </Field>
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
        </React.Fragment>
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
          <span className="spacer" />
          {/* Which account every tab in here acts on - so it is answered once,
              at the top, rather than per tab.

              Identity, not status: the dot and the colour live in the ribbon,
              and repeating them would be a second source of truth for whether
              MT5 is up. This says only WHICH account, and dims when the bridge
              is not attached so it is never read as a live confirmation. */}
          <span className="set-acct" title={mt5?.connected
            ? 'The account these settings drive'
            : 'Last known account - MT5 is not attached'}
            style={{ opacity: mt5?.connected ? 1 : 0.55 }}>
            <b className={mt5?.account_type === 'LIVE' ? 't-down' : 't-mid'}>
              {mt5?.account_type ?? 'UNKNOWN'}
            </b>
            <span className="mono">{mt5?.login ?? '—'}</span>
            <span className="mono t-dim">{mt5?.server ?? '—'}</span>
          </span>
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
          {anyDirty && <span className="t-warn" style={{ fontSize: 10.5 }}>
            unsaved changes in {[dirty && 'alerts', engineDirty && 'risk & gates']
              .filter(Boolean).join(' and ')}
          </span>}
          <span className="spacer" />
          <button className="tool-btn" onClick={cancel}>Cancel</button>
          <button className="tool-btn primary" onClick={saveAll}
            disabled={busy || !anyDirty}>
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
