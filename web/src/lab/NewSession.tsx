/**
 * NewSession - where a backtest starts: market, timeframe, the moment to
 * start from, how far to run, and who trades (the system, or you).
 */
import React, { useMemo, useState } from 'react'
import { SymbolPicker } from '../panels/SymbolPicker'
import type { LabDataInfo, LabSession } from './types'

const TF_MIN: Record<string, number> = { '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '4h': 240 }
const DAY = 86_400_000

/** "YYYY-MM-DDTHH:MM" in UTC, for a datetime-local input read as UTC. */
const toInput = (ms: number) => new Date(ms).toISOString().slice(0, 16)
const fromInput = (s: string) => {
  const t = Date.parse(s + ':00Z')
  return Number.isFinite(t) ? t : NaN
}

export function NewSession({ data, current, onCreate, onClose }: {
  data: LabDataInfo
  current: LabSession | null
  onCreate: (cfg: Record<string, any>) => void
  onClose: () => void
}) {
  const syms = data.symbols
  const [symbol, setSymbol] = useState(current?.cfg.symbol ?? syms[0]?.symbol ?? 'XAUUSD.a')
  const [tf, setTf] = useState(current?.cfg.tf ?? '5m')
  const cover = syms.find((s) => s.symbol === symbol)?.coverage ?? {}
  const range = cover[tf]
  const m1 = cover['1m']
  const lastMs = Math.min(range?.last_ms ?? Date.now(), m1?.last_ms ?? Date.now())
  // The engine needs 600 bars of history before the first decision bar.
  const firstMs = (range?.first_ms ?? 0) + 720 * (TF_MIN[tf] ?? 5) * 60_000 * 1.6
  const [start, setStart] = useState(() => toInput(lastMs - 14 * DAY))
  const [end, setEnd] = useState(() => toInput(lastMs))
  const [mode, setMode] = useState<'auto' | 'manual'>(current?.cfg.mode ?? 'auto')
  const [name, setName] = useState('')
  const [record, setRecord] = useState(true)
  const [copySettings, setCopySettings] = useState(false)
  const [picker, setPicker] = useState(false)

  const s = fromInput(start), e = fromInput(end)
  const estBars = useMemo(() => {
    if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) return 0
    // ~23 trading hours a day, 5 days in 7.
    return Math.round((e - s) / 60_000 / (TF_MIN[tf] ?? 5) * (23 / 24) * (5 / 7))
  }, [s, e, tf])
  const problem = !range ? `No ${tf} history for ${symbol} on disk.`
    : !Number.isFinite(s) ? 'Pick a start.'
      : s < firstMs ? `Start after ${toInput(firstMs).replace('T', ' ')} - the engine needs 600 bars of history first.`
        : s >= lastMs ? `History ends ${toInput(lastMs).replace('T', ' ')} UTC.`
          : Number.isFinite(e) && e <= s ? 'The end must be after the start.'
            : estBars > data.max_bars ? `About ${estBars.toLocaleString()} bars - more than a replay handles (${data.max_bars.toLocaleString()}). Shorten it.`
              : null

  const preset = (days: number, back = true) => {
    const endAt = lastMs
    setEnd(toInput(endAt))
    setStart(toInput(back ? endAt - days * DAY : endAt))
  }
  const randomWeek = () => {
    // Somewhere uniformly between the earliest usable start and a week before
    // the end - a way to test without cherry-picking the week.
    const lo = firstMs, hi = lastMs - 7 * DAY
    if (hi <= lo) return
    const t0 = lo + Math.random() * (hi - lo)
    const monday = new Date(t0)
    monday.setUTCHours(0, 0, 0, 0)
    monday.setUTCDate(monday.getUTCDate() - ((monday.getUTCDay() + 6) % 7))
    setStart(toInput(monday.getTime()))
    setEnd(toInput(monday.getTime() + 5 * DAY))
  }

  const create = () => {
    if (problem) return
    onCreate({
      symbol, tf, start: s, end: Number.isFinite(e) ? e : null, mode, name, record,
      overrides: copySettings && current ? current.cfg.overrides : {},
    })
  }

  return (
    <>
    <div className="modal-back" onMouseDown={onClose}>
      <div className="lab-modal" onMouseDown={(ev) => ev.stopPropagation()}>
        <div className="lab-modal-h">
          <span>New backtest session</span>
          <span className="t-dim" style={{ fontWeight: 400, letterSpacing: 0 }}>history from disk · simulated account · nothing reaches MT5</span>
        </div>
        <div className="lab-modal-b">
          <div className="lab-form2">
            <label>Market
              <button className="tool-btn" onClick={() => setPicker(true)} style={{ justifyContent: 'space-between' }}>
                {symbol} <span className="t-dim">▾</span>
              </button>
            </label>
            <label>Timeframe
              <div className="lab-seg">
                {data.timeframes.map((t) => (
                  <button key={t} className={t === tf ? 'on' : ''} disabled={!cover[t]} onClick={() => setTf(t)}>{t}</button>
                ))}
              </div>
            </label>
            <label>Start (UTC)
              <input type="datetime-local" value={start} onChange={(ev) => setStart(ev.target.value)} />
            </label>
            <label>End (UTC)
              <input type="datetime-local" value={end} onChange={(ev) => setEnd(ev.target.value)} />
            </label>
            <div className="lab-presets">
              <span className="t-dim">quick:</span>
              <button onClick={() => preset(1)}>last day</button>
              <button onClick={() => preset(7)}>last week</button>
              <button onClick={() => preset(14)}>2 weeks</button>
              <button onClick={() => preset(30)}>month</button>
              <button onClick={() => preset(90)}>3 months</button>
              <button onClick={randomWeek} title="A random trading week - test without choosing the week">random week</button>
            </div>
            <label>Who trades
              <div className="lab-seg">
                <button className={mode === 'auto' ? 'on' : ''} onClick={() => setMode('auto')}>The system (auto)</button>
                <button className={mode === 'manual' ? 'on' : ''} onClick={() => setMode('manual')}>Me (manual practice)</button>
              </div>
            </label>
            <div className="t-dim lab-help">
              {mode === 'auto'
                ? 'Signals are sent exactly as the live executor would: same entry tolerance, pending orders re-judged on each closed bar, trail after TP1.'
                : 'The engine still finds and qualifies signals; you decide. Take a signal, or place your own orders from the ticket.'}
            </div>
            <label>Name <input value={name} placeholder="optional" onChange={(ev) => setName(ev.target.value)} /></label>
            <label className="lab-check"><input type="checkbox" checked={record} onChange={(ev) => setRecord(ev.target.checked)} /> Record to disk (reopen, review, compare later)</label>
            {current && Object.keys(current.cfg.overrides ?? {}).length > 0 && (
              <label className="lab-check"><input type="checkbox" checked={copySettings} onChange={(ev) => setCopySettings(ev.target.checked)} /> Use the current session's strategy settings (otherwise: live settings)</label>
            )}
          </div>
          <div className="lab-cover">
            <div className="lab-chartbox-h">History on disk</div>
            {Object.entries(cover).map(([t, r]) => (
              <div key={t} className="mono lab-cover-r"><span>{t}</span><span>{toInput(r.first_ms).slice(0, 10)} → {toInput(r.last_ms).slice(0, 10)}</span></div>
            ))}
            <div className="t-dim" style={{ fontSize: 9.5, marginTop: 6 }}>
              Contract spec: {syms.find((x) => x.symbol === symbol)?.spec_source ?? '—'}
            </div>
            <div className="lab-est">≈ {estBars.toLocaleString()} bars of {tf}</div>
          </div>
        </div>
        <div className="lab-modal-f">
          <span className={problem ? 't-warn' : 't-dim'} style={{ fontSize: 10 }}>{problem ?? 'The engine sees only closed bars up to each moment - never the future.'}</span>
          <span style={{ flex: 1 }} />
          <button className="tool-btn" onClick={onClose}>Cancel</button>
          <button className="tool-btn lab-go" disabled={!!problem} onClick={create}>Start backtest</button>
        </div>
      </div>
    </div>
      {picker && (
        <SymbolPicker current={symbol} onClose={() => setPicker(false)}
          onPick={(sym) => setSymbol(sym)}
          load={async () => syms.map((x) => ({
            name: x.symbol, group: Object.keys(x.coverage).join(' · '), digits: null,
            visible: true, on_disk: true,
          }))}
          loadingText="Reading the history on disk…"
          hint="Instruments with 1-minute history on disk - the lab fills orders on the M1 path." />
      )}
    </>
  )
}
