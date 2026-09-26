/**
 * LabSide - the lab's right-hand panel.
 *
 *   NOW       the simulated account at the bar on screen, its open trades and
 *             pending orders, a manual ticket (at the frontier), and every
 *             signal the engine produced here with its full gate ledger
 *   TRADE     one trade, inspected: levels, excursions, and the decision
 *             trail from the signal to the exit - plus a tag and a note
 *   STRATEGY  this session's settings against the live ones; applying a
 *             change starts a new run
 *   FORECAST  the forecast engine at the bar on screen - LabForecast.tsx
 */
import React, { useEffect, useMemo, useState } from 'react'
import type { Bar, Signal } from '../chart/types'
import { fmt, signed } from '../lib/format'
import { Meter } from '../panels/common'
import { ForecastSide } from './LabForecast'
import type { LabEvent, LabForecast, LabFrame, LabSession, LabTrade, SchemaRow } from './types'

export type SideTab = 'now' | 'trade' | 'strategy' | 'forecast'

const when = (ms: number) => {
  const d = new Date(ms)
  return `${d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: '2-digit', timeZone: 'UTC' })} `
    + `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
}

const TAGS = ['good', 'bad', 'mistake', 'lesson', 'news']

export function LabSide(p: {
  tab: SideTab
  onTab: (t: SideTab) => void
  session: LabSession | null
  frame: LabFrame | null
  events: LabEvent[]
  atFrontier: boolean
  digits: number
  selectedTrade: LabTrade | null
  tags: Record<string, { tag?: string; note?: string }>
  selectedSignal: string | null
  onSelectSignal: (id: string | null) => void
  onCommand: (cmd: Record<string, any>) => void
  onSeekBar: (i: number) => void
  onTag: (id: string, tag?: string, note?: string) => void
  onNote: (text: string) => void
  schema: SchemaRow[]
  live: Record<string, Record<string, any>>
  playbooks: string[]
  onConfigure: (patch: Record<string, any>) => void
  bars: Bar[]
  pinned: LabForecast | null
  onPin: () => void
  coneOn: boolean
  onCone: (on: boolean) => void
  onSeekTime?: (t: number) => void
}) {
  return (
    <div className="lab-side">
      <div className="lab-side-tabs">
        {(['now', 'forecast', 'trade', 'strategy'] as SideTab[]).map((t) => (
          <button key={t} className={`lab-tab ${p.tab === t ? 'on' : ''}`} onClick={() => p.onTab(t)}>
            {t === 'now' ? 'This bar' : t === 'forecast' ? 'Forecast' : t === 'trade' ? 'Trade' : 'Strategy'}
          </button>
        ))}
      </div>
      <div className="lab-side-body">
        {!p.session || !p.frame ? (
          <div className="lab-empty">Start or open a session to see the simulated account here.</div>
        ) : p.tab === 'now' ? <NowTab {...p} />
          : p.tab === 'forecast' ? (
            <ForecastSide forecast={p.frame.forecast ?? null} digits={p.digits} bars={p.bars}
              cursorV={p.frame.cursor.v} pinned={p.pinned} onPin={p.onPin}
              coneOn={p.coneOn} onCone={p.onCone} onSeekTime={p.onSeekTime} />
          )
            : p.tab === 'trade' ? <TradeTab {...p} />
              : <StrategyTab {...p} />}
      </div>
    </div>
  )
}

// --------------------------------------------------------------------- now
function NowTab(p: Parameters<typeof LabSide>[0]) {
  const f = p.frame!
  const a = f.account
  const snap: any = f.snapshot
  const dp = p.digits
  const ret = a.balance0 ? (a.equity / a.balance0 - 1) * 100 : 0
  const behind = f.cursor.k - f.cursor.v
  return (
    <>
      <div className="lab-clock">
        <div className="mono t-hi" style={{ fontSize: 13, fontWeight: 800 }}>{when(f.cursor.t)} <span className="t-dim" style={{ fontSize: 9 }}>UTC</span></div>
        <div className="mono" style={{ fontSize: 12 }}>{fmt(snap?.price, dp)}</div>
      </div>
      <div className={`lab-where ${behind ? 'past' : ''}`}>
        {behind ? `Reviewing the past - ${behind} bar${behind === 1 ? '' : 's'} before the latest simulated bar. The account shown is as it was here.`
          : 'Latest simulated bar - the market moves on when you step forward.'}
      </div>

      <div className="lab-sec">Account</div>
      <div className="lab-kpis">
        <Kpi k="Equity" v={fmt(a.equity, 2)} cls={a.equity >= a.balance0 ? 't-up' : 't-down'} />
        <Kpi k="Balance" v={fmt(a.balance, 2)} />
        <Kpi k="Floating" v={signed(a.floating, 2)} cls={a.floating >= 0 ? 't-up' : 't-down'} />
        <Kpi k="Return" v={`${signed(ret, 2)}%`} cls={ret >= 0 ? 't-up' : 't-down'} />
      </div>

      <div className="lab-sec">Open trades <span className="t-dim">{f.positions.length}</span></div>
      {!f.positions.length ? <div className="lab-none">Flat.</div> : f.positions.map((pos) => {
        const buy = pos.side === 'buy'
        const risk = Math.abs(pos.price_open - (pos.initial_sl || pos.sl || pos.price_open))
        const r = risk ? ((pos.price_current - pos.price_open) * (buy ? 1 : -1)) / risk : 0
        const manual = String(pos.comment || '').startsWith('LAB')
        return (
          <div key={pos.ticket} className={`lab-pos ${pos.side}`}>
            <div className="lab-pos-h">
              <span className={buy ? 't-up' : 't-down'} style={{ fontWeight: 800 }}>{buy ? 'BUY' : 'SELL'}</span>
              <span className="mono">{pos.volume}</span>
              <span className="chip chip-mute">{manual ? 'manual' : 'system'}</span>
              <span style={{ flex: 1 }} />
              <span className={`mono ${pos.profit >= 0 ? 't-up' : 't-down'}`} style={{ fontWeight: 700 }}>
                {signed(pos.profit, 2)}
              </span>
              <span className={`mono ${r >= 0 ? 't-up' : 't-down'}`}>{signed(r, 2)}R</span>
            </div>
            <div className="lab-pos-g mono">
              <span>entry <b>{fmt(pos.price_open, dp)}</b></span>
              <span>SL <b className="t-down">{pos.sl ? fmt(pos.sl, dp) : '—'}</b></span>
              <span>TP <b className="t-up">{pos.tp ? fmt(pos.tp, dp) : '—'}</b></span>
            </div>
            {p.atFrontier && (
              <button className="tool-btn lab-mini" onClick={() => p.onCommand({ op: 'close', ticket: pos.ticket })}>
                Close at market
              </button>
            )}
          </div>
        )
      })}
      {f.orders.length > 0 && (
        <>
          <div className="lab-sec">Pending orders <span className="t-dim">{f.orders.length}</span></div>
          {f.orders.map((o) => (
            <div key={o.ticket} className="lab-order mono">
              <span className={o.side === 'buy' ? 't-up' : 't-down'}>{o.side.toUpperCase()} {o.kind}</span>
              <span>at <b>{fmt(o.price_open, dp)}</b></span>
              <span className="t-dim">SL {fmt(o.sl, dp)}</span>
              {p.atFrontier && (
                <button className="lab-x" title="Cancel this order"
                  onClick={() => p.onCommand({ op: 'cancel', ticket: o.ticket })}>✕</button>
              )}
            </div>
          ))}
        </>
      )}

      <Ticket {...p} />

      <div className="lab-sec">Signals at this bar <span className="t-dim">{f.signals.length}</span></div>
      {!f.signals.length ? <div className="lab-none">The engine had nothing live here.</div>
        : f.signals.map((s) => (
          <SignalBox key={s.id} sig={s} dp={dp} selected={p.selectedSignal === s.id}
            onSelect={() => p.onSelectSignal(p.selectedSignal === s.id ? null : s.id)}
            canTake={p.atFrontier && p.session?.cfg.mode === 'manual'}
            onTake={() => p.onCommand({ op: 'take', id: s.id })} />
        ))}

      <MarketRead snap={snap} />
      <BarNote onNote={p.onNote} />
    </>
  )
}

function BarNote({ onNote }: { onNote: (text: string) => void }) {
  const [text, setText] = useState('')
  const add = () => { if (text.trim()) { onNote(text.trim()); setText('') } }
  return (
    <>
      <div className="lab-sec">Note on this bar</div>
      <div className="lab-row" style={{ marginTop: 0 }}>
        <input className="lab-noteline" value={text} placeholder="What do you see here? (journal)"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') add() }} />
        <button className="tool-btn" disabled={!text.trim()} onClick={add}>Add</button>
      </div>
    </>
  )
}

function Kpi({ k, v, cls }: { k: string; v: React.ReactNode; cls?: string }) {
  return (
    <div className="lab-kpi">
      <div className="lab-kpi-k">{k}</div>
      <div className={`lab-kpi-v mono ${cls ?? ''}`}>{v}</div>
    </div>
  )
}

/** A manual market order at the frontier, sized and bracketed off the ATR. */
function Ticket(p: Parameters<typeof LabSide>[0]) {
  const f = p.frame!
  const snap: any = f.snapshot
  const [slAtr, setSlAtr] = useState(1.5)
  const [tpR, setTpR] = useState(2)
  if (!p.atFrontier) return null
  const px = Number(snap?.price) || 0
  const atr = Number(snap?.atr) || 0
  const lots = p.session?.settings?.execution?.lots_gold ?? 0.01
  const levels = (side: 'buy' | 'sell') => {
    const d = side === 'buy' ? 1 : -1
    const sl = px - d * slAtr * atr
    return { sl: +sl.toFixed(p.digits), tp: +(px + d * tpR * slAtr * atr).toFixed(p.digits) }
  }
  const b = levels('buy'), s = levels('sell')
  return (
    <>
      <div className="lab-sec">Manual trade <span className="t-dim">practice - recorded</span></div>
      <div className="lab-ticket">
        <label>SL <input type="number" step={0.1} min={0.2} value={slAtr}
          onChange={(e) => setSlAtr(Math.max(0.1, +e.target.value || 0))} /> ATR</label>
        <label>TP <input type="number" step={0.25} min={0.25} value={tpR}
          onChange={(e) => setTpR(Math.max(0.1, +e.target.value || 0))} /> R</label>
        <span className="t-dim mono">{lots} lots</span>
      </div>
      <div className="lab-ticket-btns">
        <button className="lab-buy" disabled={!atr}
          onClick={() => p.onCommand({ op: 'order', side: 'buy', sl: b.sl, tp: b.tp })}>
          BUY <span className="mono">SL {fmt(b.sl, p.digits)} · TP {fmt(b.tp, p.digits)}</span>
        </button>
        <button className="lab-sell" disabled={!atr}
          onClick={() => p.onCommand({ op: 'order', side: 'sell', sl: s.sl, tp: s.tp })}>
          SELL <span className="mono">SL {fmt(s.sl, p.digits)} · TP {fmt(s.tp, p.digits)}</span>
        </button>
      </div>
    </>
  )
}

function SignalBox({ sig, dp, selected, onSelect, canTake, onTake }: {
  sig: Signal; dp: number; selected: boolean; onSelect: () => void
  canTake: boolean; onTake: () => void
}) {
  const buy = sig.side === 'buy'
  const blocks = sig.gates.filter((g) => g.verdict === 'BLOCK')
  const warns = sig.gates.filter((g) => g.verdict === 'WARN')
  const chip = sig.status === 'qualified' ? 'chip-up' : sig.status === 'watch' ? 'chip-warn' : 'chip-down'
  // Once a signal is an order or a position, today's verdict no longer
  // decides anything for it: a FILLED trade belongs to its stop, and a
  // pending order is only re-judged on the next closed bar. Say where it is
  // in its life, and show the fresh verdict as information.
  const live = sig.stage === 'FILLED' || sig.stage === 'SENT'
  return (
    <div className={`lab-sig ${sig.side} ${selected ? 'sel' : ''}`} onClick={onSelect}>
      <div className="lab-sig-h">
        <span className={buy ? 't-up' : 't-down'} style={{ fontWeight: 800 }}>{buy ? 'BUY' : 'SELL'}</span>
        <span className="t-hi" style={{ fontWeight: 700 }}>{sig.label || sig.playbook}</span>
        <span style={{ flex: 1 }} />
        {live ? (
          <>
            <span className="t-dim" style={{ fontSize: 9 }} title="The verdict on this bar - it no longer decides a filled trade">now {sig.status}</span>
            <span className="chip chip-info">{sig.stage === 'FILLED' ? 'in trade' : 'pending'}</span>
          </>
        ) : (
          <>
            {sig.stage && <span className="chip chip-mute">{sig.stage}</span>}
            <span className={`chip ${chip}`}>{sig.status}</span>
          </>
        )}
      </div>
      <div className="lab-sig-conf">
        <span className="t-dim">conf</span>
        <div style={{ flex: 1 }}><Meter value={sig.confidence} /></div>
        <span className="mono t-hi">{sig.confidence}</span>
      </div>
      <div className="lab-sig-lv mono">
        <span>E <b>{fmt(sig.entry, dp)}</b></span>
        <span>SL <b className="t-down">{fmt(sig.stop, dp)}</b></span>
        <span>TP1 <b className="t-up">{fmt(sig.tp1, dp)}</b></span>
        <span>TP2 <b className="t-up">{fmt(sig.tp2, dp)}</b></span>
      </div>
      {blocks.map((g, i) => (
        <div key={`b${i}`} className="lab-gate block"><b>{g.name}</b> {g.detail}</div>
      ))}
      {selected && warns.map((g, i) => (
        <div key={`w${i}`} className="lab-gate warn"><b>{g.name}</b> {g.detail} <span className="t-dim">−{g.penalty}</span></div>
      ))}
      {selected && sig.evidence.filter((e) => e.weight > 0).slice(0, 4).map((e, i) => (
        <div key={`e${i}`} className="lab-ev"><span className="t-up">+</span> {e.text}</div>
      ))}
      {!selected && warns.length > 0 && (
        <div className="t-dim" style={{ fontSize: 9, marginTop: 3 }}>{warns.length} warning{warns.length === 1 ? '' : 's'} · click for the full ledger</div>
      )}
      {canTake && sig.status === 'qualified' && sig.stage === 'FINAL' && (
        <button className="tool-btn lab-mini" onClick={(e) => { e.stopPropagation(); onTake() }}>
          Take this signal (sim)
        </button>
      )}
    </div>
  )
}

function MarketRead({ snap }: { snap: any }) {
  if (!snap?.ok) return null
  const leg = snap.leg_gate ?? snap.leg
  return (
    <>
      <div className="lab-sec">Market read</div>
      <div className="lab-read">
        <div><span className="t-dim">regime</span> <b>{snap.regime?.label}</b> <span className="t-dim">{snap.regime?.confidence}</span></div>
        <div><span className="t-dim">structure</span> <b>{snap.trend?.state}</b> <span className="t-dim">{snap.trend?.strength}</span></div>
        <div><span className="t-dim">timeframes</span> <b>{snap.mtf?.verdict}</b> <span className="t-dim">{snap.mtf?.score}</span></div>
        {leg && (
          <div>
            <span className="t-dim">leg</span>{' '}
            <b className={leg.dir === 1 ? 't-up' : 't-down'}>{leg.dir === 1 ? 'up' : 'down'} {Number(leg.ext_atr).toFixed(1)} ATR</b>{' '}
            <span className="t-dim">{Number(leg.pull_atr).toFixed(1)} back</span>
            {leg.fade_block && <div className="lab-gate warn" style={{ marginTop: 3 }}>fade {leg.fade_side}: {leg.fade_block}</div>}
          </div>
        )}
        <div><span className="t-dim">ATR</span> <b className="mono">{snap.atr_points}</b> <span className="t-dim">pts · spread {snap.spread_points ?? '—'}</span></div>
      </div>
    </>
  )
}

// ------------------------------------------------------------------- trade
function TradeTab(p: Parameters<typeof LabSide>[0]) {
  const t = p.selectedTrade
  const tag = t ? p.tags[t.id] : undefined
  const [note, setNote] = useState(tag?.note ?? '')
  useEffect(() => { setNote(tag?.note ?? '') }, [t?.id, tag?.note])
  const trail = useMemo(() => (t ? p.events.filter((e) => e.data?.id === t.id) : []), [t, p.events])
  if (!t) {
    return <div className="lab-empty">Pick a trade - in the Trades list, on the equity curve, or on the MAE/MFE chart - to inspect it here.</div>
  }
  const dp = p.digits
  const won = t.profit > 0
  return (
    <>
      <div className={`lab-trade-h ${won ? 'win' : 'loss'}`}>
        <span className="mono t-dim">#{t.n}</span>
        <span className={t.side === 'buy' ? 't-up' : 't-down'} style={{ fontWeight: 800 }}>{t.side.toUpperCase()}</span>
        <span className="t-hi" style={{ fontWeight: 700 }}>{t.playbook}</span>
        <span style={{ flex: 1 }} />
        <span className={`chip ${won ? 'chip-up' : 'chip-down'}`}>{t.outcome}</span>
      </div>
      <div className="lab-kpis">
        <Kpi k="Result" v={`${signed(t.r, 2)}R`} cls={won ? 't-up' : 't-down'} />
        <Kpi k="P&L" v={signed(t.profit, 2)} cls={won ? 't-up' : 't-down'} />
        <Kpi k="MFE" v={`${t.mfe_r.toFixed(2)}R`} />
        <Kpi k="MAE" v={`${t.mae_r.toFixed(2)}R`} />
      </div>
      <div className="lab-kv mono">
        <div><span>entry</span><b>{fmt(t.entry, dp)}</b><span className="t-dim">{when(t.entry_t)}</span></div>
        <div><span>exit</span><b>{fmt(t.exit, dp)}</b><span className="t-dim">{when(t.exit_t)}</span></div>
        <div><span>stop</span><b className="t-down">{fmt(t.stop0, dp)}</b><span className="t-dim">final {t.sl_final ? fmt(t.sl_final, dp) : '—'}</span></div>
        <div><span>targets</span><b className="t-up">{t.tp1 ? fmt(t.tp1, dp) : '—'}</b><span className="t-dim">TP2 {t.tp2 ? fmt(t.tp2, dp) : '—'}</span></div>
        <div><span>held</span><b>{t.bars} bars</b><span className="t-dim">{t.kind ?? ''} {t.confidence != null ? `· conf ${t.confidence}` : ''}</span></div>
      </div>
      <div className="lab-row">
        <button className="tool-btn lab-mini" onClick={() => p.onSeekBar(t.entry_i)}>◀ Go to entry</button>
        <button className="tool-btn lab-mini" onClick={() => p.onSeekBar(t.exit_i)}>Go to exit ▶</button>
      </div>
      <div className="lab-sec">Review</div>
      <div className="lab-tags">
        {TAGS.map((g) => (
          <button key={g} className={`lab-tagbtn ${tag?.tag === g ? 'on' : ''}`}
            onClick={() => p.onTag(t.id, tag?.tag === g ? '' : g, undefined)}>{g}</button>
        ))}
      </div>
      <textarea className="lab-note" placeholder="What did this trade teach? (saved with the session)"
        value={note} onChange={(e) => setNote(e.target.value)}
        onBlur={() => { if (note !== (tag?.note ?? '')) p.onTag(t.id, undefined, note) }} />
      <div className="lab-sec">Decision trail</div>
      {!trail.length ? <div className="lab-none">{t.manual ? 'A manual trade.' : 'No recorded steps.'}</div>
        : trail.map((e) => (
          <div key={e.seq} className="lab-trail" onClick={() => p.onSeekBar(e.i)}>
            <span className="mono t-dim">{when(e.t)}</span>
            <span className={`lab-kind k-${e.kind}`}>{e.kind}</span>
            <span className="t-mid">{e.text.replace(/^(BUY|SELL) [a-z_]+: /, '')}</span>
          </div>
        ))}
    </>
  )
}

// ---------------------------------------------------------------- strategy
function StrategyTab(p: Parameters<typeof LabSide>[0]) {
  const s = p.session!
  const [draft, setDraft] = useState<Record<string, Record<string, any>>>(() => JSON.parse(JSON.stringify(s.settings)))
  const [mode, setMode] = useState(s.cfg.mode)
  const [name, setName] = useState(s.cfg.name)
  const [notes, setNotes] = useState(s.cfg.notes)
  const [record, setRecord] = useState(s.cfg.record)
  useEffect(() => {
    setDraft(JSON.parse(JSON.stringify(s.settings)))
    setMode(s.cfg.mode); setName(s.cfg.name); setNotes(s.cfg.notes); setRecord(s.cfg.record)
  }, [s.meta.id, s.meta.run_no])

  const set = (g: string, k: string, v: any) => setDraft((d) => ({ ...d, [g]: { ...d[g], [k]: v } }))
  const overrides = useMemo(() => {
    const out: Record<string, Record<string, any>> = {}
    for (const row of p.schema) {
      const a = draft[row.group]?.[row.key]
      const b = p.live[row.group]?.[row.key]
      if (JSON.stringify(a) !== JSON.stringify(b)) (out[row.group] ??= {})[row.key] = a
    }
    return out
  }, [draft, p.schema, p.live])
  const nChanged = Object.values(overrides).reduce((n, g) => n + Object.keys(g).length, 0)
  const dirty = JSON.stringify(overrides) !== JSON.stringify(s.cfg.overrides ?? {})
    || mode !== s.cfg.mode || record !== s.cfg.record
  const apply = () => {
    if (s.trades.length && !window.confirm('Apply these settings? The session starts a new run from its first bar; this run\'s trades are replaced.')) return
    p.onConfigure({ overrides, mode, name, notes, record })
  }

  return (
    <>
      <div className="lab-sec">Session</div>
      <div className="lab-form">
        <label>Name <input value={name} onChange={(e) => setName(e.target.value)}
          onBlur={() => { if (name !== s.cfg.name) p.onConfigure({ name, keepRun: true }) }} /></label>
        <label>Trading
          <span className="lab-seg">
            <button className={mode === 'auto' ? 'on' : ''} onClick={() => setMode('auto')}>System (auto)</button>
            <button className={mode === 'manual' ? 'on' : ''} onClick={() => setMode('manual')}>Me (manual)</button>
          </span>
        </label>
        <label className="lab-check"><input type="checkbox" checked={record} onChange={(e) => setRecord(e.target.checked)} /> Record this session to disk</label>
        <label>Notes <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2}
          onBlur={() => { if (notes !== s.cfg.notes) p.onConfigure({ notes, keepRun: true }) }} /></label>
      </div>

      <div className="lab-sec">Strategy settings <span className="t-dim">{nChanged ? `${nChanged} differ from live` : 'same as live'}</span></div>
      <div className="lab-form">
        {p.schema.map((row) => {
          const v = draft[row.group]?.[row.key]
          const lv = p.live[row.group]?.[row.key]
          const diff = JSON.stringify(v) !== JSON.stringify(lv)
          if (row.type === 'playbooks') {
            const off: string[] = v ?? []
            return (
              <div key={row.key} className={`lab-field ${diff ? 'diff' : ''}`}>
                <span className="lab-field-l">{row.label}</span>
                <div className="lab-pbs">
                  {p.playbooks.map((pb) => {
                    const on = !off.includes(pb)
                    return (
                      <button key={pb} className={`lab-pb ${on ? 'on' : ''}`}
                        onClick={() => set(row.group, row.key, on ? [...off, pb] : off.filter((x) => x !== pb))}>
                        {pb}
                      </button>
                    )
                  })}
                </div>
              </div>
            )
          }
          return (
            <div key={row.key} className={`lab-field ${diff ? 'diff' : ''}`} title={row.hint}>
              <span className="lab-field-l">{row.label}</span>
              {row.type === 'bool' ? (
                <input type="checkbox" checked={!!v} onChange={(e) => set(row.group, row.key, e.target.checked)} />
              ) : row.type === 'enum' ? (
                <select value={v} onChange={(e) => set(row.group, row.key, e.target.value)}>
                  {(row.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              ) : (
                <input type="number" value={v ?? ''} step={row.type === 'int' ? 1 : 0.01}
                  min={row.lo} max={row.hi}
                  onChange={(e) => set(row.group, row.key, row.type === 'int'
                    ? Math.round(+e.target.value) : +e.target.value)} />
              )}
              {diff && <span className="lab-live" title="The live setting">live {String(Array.isArray(lv) ? lv.length : lv)}</span>}
            </div>
          )
        })}
      </div>
      <div className="lab-row" style={{ position: 'sticky', bottom: 0, background: 'var(--bg-panel)', padding: '8px 0' }}>
        <button className="tool-btn" disabled={!dirty} onClick={apply}
          style={{ borderColor: dirty ? 'var(--warn-dim)' : undefined, color: dirty ? 'var(--warn)' : undefined }}>
          Apply & start a new run
        </button>
        <button className="tool-btn" onClick={() => setDraft(JSON.parse(JSON.stringify(p.live)))}
          title="Put every setting back to what the live system uses">Reset to live</button>
      </div>
      <div className="t-dim" style={{ fontSize: 9.5, lineHeight: 1.5 }}>
        These settings exist only in this session. The live system's settings are never changed from here.
      </div>
    </>
  )
}
