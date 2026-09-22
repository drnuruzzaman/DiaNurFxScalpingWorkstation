import React from 'react'
import type { Snapshot } from '../chart/types'
import { clockUTC, dirClass, fmt, pct, signed } from '../lib/format'
import { Empty, KV, Meter, Panel, ScoreRow } from './common'

/**
 * The Fear/Greed dial.
 *
 * Drawn as an SVG arc rather than a canvas or an image so it scales with the
 * rail and inherits theme colours. The needle angle maps 0..100 onto a 160
 * degree sweep, which leaves the extremes visually distinct from "nearly
 * extreme" - a full 270 sweep made 88 and 96 look identical.
 *
 * Each band carries its own name along the arc, so the number is readable
 * without first learning what 0..100 means here. The band the needle is in is
 * held at full strength and the rest are dimmed, which is what makes the dial
 * answer "where are we" at a glance rather than after a read.
 */
type Band = {
  lo: number
  hi: number
  col: string
  /** Split across two lines - a two-word name does not fit one 32deg arc. */
  lines: string[]
  /** Ink that survives on this band's fill. */
  ink: string
}

const BANDS: Band[] = [
  { lo: 0, hi: 20, col: '#ff2d78', lines: ['EXTREME', 'FEAR'], ink: '#1a0710' },
  { lo: 20, hi: 40, col: '#d4456f', lines: ['FEAR'], ink: '#fdeef4' },
  { lo: 40, hi: 60, col: '#6b7896', lines: ['NEUTRAL'], ink: '#0b0f18' },
  { lo: 60, hi: 80, col: '#8fbf4a', lines: ['GREED'], ink: '#0d1405' },
  { lo: 80, hi: 100, col: '#9fe547', lines: ['EXTREME', 'GREED'], ink: '#0d1405' },
]

const SESSION_ROWS = [
  { key: 'sydney', label: 'Sydney', open: 21, close: 6 },
  { key: 'tokyo', label: 'Tokyo', open: 0, close: 9 },
  { key: 'london', label: 'London', open: 7, close: 16 },
  { key: 'newyork', label: 'New York', open: 12, close: 21 },
]

const bandFor = (v: number): Band =>
  BANDS.find((b) => v >= b.lo && v <= b.hi) ?? BANDS[2]

function Gauge({ value, label }: { value: number; label: string }) {
  const W = 206, H = 126, CX = W / 2, CY = 104, R = 72, BW = 23
  const START = 190, SWEEP = 160

  const clamped = Math.max(0, Math.min(100, value))
  const deg = (v: number) => START + (Math.max(0, Math.min(100, v)) / 100) * SWEEP
  const polar = (d: number, r: number) => {
    const rad = (d * Math.PI) / 180
    return [CX + r * Math.cos(rad), CY + r * Math.sin(rad)]
  }
  /** Arc path, always the short way round - every band here is under 180deg. */
  const path = (from: number, to: number, r: number) => {
    const [x1, y1] = polar(from, r)
    const [x2, y2] = polar(to, r)
    return `M ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2}`
  }

  const active = bandFor(clamped)
  const needle = deg(clamped)
  const [nx, ny] = polar(needle, R - BW / 2 - 4)

  return (
    <div className="gauge-wrap">
      <div className="gauge-chip" style={{
        color: active.col, borderColor: active.col, background: `${active.col}1f`,
      }}>{active.lines.join(' ')}</div>

      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={`Fear and greed ${clamped} of 100, ${active.lines.join(' ')}`}>
        <defs>
          {BANDS.map((b, i) => (
            <React.Fragment key={`d${i}`}>
              {/* One text baseline per line, offset either side of the band's
                  centre line so a two-word name stacks instead of overflowing
                  into its neighbours. */}
              {b.lines.map((_, li) => {
                // Baselines sit ON the path and glyphs grow AWAY from the
                // centre, so the first line needs the LARGER radius or a
                // stacked name reads bottom-up.
                const offset = b.lines.length === 1
                  ? -3
                  : (li === 0 ? 3.5 : -4.5)
                return (
                  <path key={`p${i}-${li}`} id={`fg-arc-${i}-${li}`} fill="none"
                    d={path(deg(b.lo) + 1.2, deg(b.hi) - 1.2, R + offset)} />
                )
              })}
            </React.Fragment>
          ))}
        </defs>

        {BANDS.map((b, i) => {
          const on = b === active
          return (
            <path key={`b${i}`} d={path(deg(b.lo) + 1.2, deg(b.hi) - 1.2, R)}
              stroke={b.col} strokeWidth={on ? BW + 3 : BW} fill="none"
              strokeLinecap="butt" opacity={on ? 1 : 0.42} />
          )
        })}

        {BANDS.map((b, i) => b.lines.map((line, li) => (
          <text key={`t${i}-${li}`} fill={b.ink} fontSize="6.4" fontWeight="800"
            letterSpacing="0.06em" opacity={b === active ? 1 : 0.72}>
            <textPath href={`#fg-arc-${i}-${li}`} startOffset="50%"
              textAnchor="middle">{line}</textPath>
          </text>
        )))}

        {[0, 25, 50, 75, 100].map((v) => {
          const [tx, ty] = polar(deg(v), R - BW / 2 - 9)
          return (
            <text key={v} x={tx} y={ty} fill="#55617a" fontSize="7.5"
              textAnchor="middle" dominantBaseline="middle">{v}</text>
          )
        })}

        <line x1={CX} y1={CY} x2={nx} y2={ny} stroke="#e8edf7"
          strokeWidth="2" strokeLinecap="round" />
        <circle cx={CX} cy={CY} r="5" fill="#e8edf7" />
        <circle cx={CX} cy={CY} r="2" fill="#0b0f18" />
      </svg>

      {/* No band name under the number: the chip above the dial already
          says it, and the needle points at it a third time. */}
      <div className="gauge-value mono" style={{ color: active.col }}>{clamped}</div>
    </div>
  )
}

export function LeftRail({
  snap, quote, account, symbols, symbol, onSymbol, tradingEnabled, nowMs,
  autoExec = false, onToggleAuto,
  quotes = {}, onEditWatchlist,
}: {
  snap: Snapshot | null
  quote: any
  account: any
  symbols: string[]
  symbol: string
  onSymbol: (s: string) => void
  tradingEnabled: boolean
  autoExec?: boolean
  onToggleAuto?: () => void
  nowMs: number
  /** Live bid/ask per symbol, so every row has a price - not just the active one. */
  quotes?: Record<string, any>
  onEditWatchlist?: () => void
}) {
  const gauge = snap?.gauge
  const vol = snap?.regime?.volatility
  const active: string[] = snap?.session?.active ?? []

  return (
    <aside className="rail">
      <Panel title="Watchlist" right={
        <button className="link-btn" onClick={onEditWatchlist}>edit</button>
      }>
        {symbols.length === 0 ? (
          <Empty>Nothing on the watchlist yet — open the symbol picker to add some.</Empty>
        ) : (
          <table className="tabular">
            <thead>
              <tr><th>Symbol</th><th className="num">Bid</th><th className="num">Spread</th></tr>
            </thead>
            <tbody>
              {symbols.map((s) => {
                const on = s === symbol
                // The charted symbol has a tick-by-tick quote from the stream;
                // every other row comes from the shared poll. Prefer the
                // stream where it exists, so the active row is never behind
                // the price in the header.
                // Pick whichever source actually HAS a price. Testing the
                // stream quote for truthiness alone was the bug: it arrives as
                // an object with no bid while a symbol is still spinning up,
                // which is truthy, so it short-circuited the polled fallback
                // and the row sat on a dash with a good price available.
                const streamQ = on && quote?.bid != null ? quote : null
                const q = streamQ || quotes[s] || null
                // The snapshot carries digits at runtime but it is not on the
                // Snapshot type, so read it through the quote or fall back.
                const digits = q?.digits ?? 2
                const sp = q?.bid && q?.ask
                  ? ((q.ask - q.bid) / (q.point || Math.pow(10, -digits))).toFixed(0)
                  : snap && on && snap.spread_points != null ? snap.spread_points.toFixed(0) : '—'
                return (
                  <tr key={s} onClick={() => onSymbol(s)}
                    style={{ cursor: 'pointer', background: on ? 'var(--bear-glow)' : undefined }}>
                    <td style={{ color: on ? 'var(--bear)' : undefined, fontWeight: on ? 700 : 400 }}>{s}</td>
                    <td className="num t-hi">{q?.bid != null ? fmt(q.bid, digits) : '—'}</td>
                    <td className="num t-mid">{sp}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title="Fear & Greed" right={<span className="chip chip-mute">{snap?.tf ?? '—'}</span>}>
        {gauge ? (
          <>
            <Gauge value={gauge.value} label={gauge.label} />
            <div style={{ marginTop: 4 }}>
              <ScoreRow label="Direction" value={gauge.direction} />
              <ScoreRow label="Volatility" value={gauge.volatility} signed={false} />
              <ScoreRow label="Participation" value={gauge.participation} signed={false} />
            </div>
            <div className="t-dim" style={{ fontSize: 9.5, marginTop: 6, lineHeight: 1.45 }}>
              Composite of trend direction, RSI push and volatility stress —
              extreme volatility reads as fear regardless of direction.
            </div>
          </>
        ) : <Empty>Waiting for analysis…</Empty>}
      </Panel>

      <Panel title="Volatility">
        {vol ? (
          <>
            <KV k="State" v={<span className={
              vol.state === 'extreme' || vol.state === 'high' ? 't-warn' : 't-info'
            }>{vol.label}</span>} />
            <KV k="ATR" v={`${fmt(vol.atr_points, 0)} pts`} />
            <KV k="Percentile" v={`${fmt(vol.atr_percentile, 0)}th`} />
            <div style={{ margin: '4px 0 6px' }}><Meter value={vol.atr_percentile} signed={false} /></div>
            <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
              {vol.squeeze && <span className="chip chip-warn">Squeeze</span>}
              {vol.expanding && <span className="chip chip-up">Expanding</span>}
              {vol.contracting && <span className="chip chip-mute">Contracting</span>}
            </div>
          </>
        ) : <Empty>—</Empty>}
      </Panel>

      <Panel title="Execution Control">
        <KV k="Order routing" v={
          tradingEnabled
            ? <span className="t-warn">ARMED</span>
            : <span className="t-up">DISABLED</span>
        } />
        <KV k="Mode" v={
          <button className={`chip ${autoExec ? 'chip-warn' : 'chip-mute'}`}
            style={{ cursor: 'pointer', border: 0 }} onClick={onToggleAuto}
            title="Switch between sending FINAL signals automatically and confirming each">
            {autoExec ? 'AUTO' : 'confirm each'}
          </button>
        } />
        <KV k="Lots" v={<span className="mono">0.01 - 0.03</span>} />
        <KV k="Exit" v={<span className="mono" style={{ fontSize: 9.5 }}>TP1 &rarr; +0.5R, trail 1 ATR</span>} />
        <KV k="Account" v={<span className="mono">{account?.login ?? '—'}</span>} />
        <KV k="Server" v={<span className="mono" style={{ fontSize: 9.5 }}>{account?.server ?? '—'}</span>} />
        <div className="t-dim" style={{ fontSize: 9.5, marginTop: 7, lineHeight: 1.5 }}>
          {!tradingEnabled
            ? 'The bridge is read-only. It has no order_send path active, so nothing here can move money.'
            : autoExec
              // The per-slot cap is configurable now, so this no longer
              // states a number it cannot know. Settings shows the live value.
              ? 'AUTO: every FINAL, qualified watchlist signal is sent without asking, up to the per-slot cap in Settings.'
              : 'The bridge is armed. Each FINAL signal needs its own confirmation.'}
        </div>
      </Panel>

      {/* Session sits last: it is standing context you glance at, not
          something you act on, so it belongs below the signals and the read. */}
      <Panel title="Session" right={<span className="mono t-dim">{clockUTC(nowMs)} UTC</span>}>
        {SESSION_ROWS.map((s) => {
          const on = active.includes(s.key)
          return (
            <div className="kv" key={s.key}>
              <span className="kv-k" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span className="dot" style={{
                  background: on ? 'var(--bull)' : 'var(--ink-dim)',
                  boxShadow: on ? '0 0 6px var(--bull)' : 'none',
                }} />
                <span style={{ color: on ? 'var(--ink-hi)' : undefined }}>{s.label}</span>
              </span>
              <span className="kv-v t-dim" style={{ fontSize: 10 }}>
                {String(s.open).padStart(2, '0')}:00–{String(s.close).padStart(2, '0')}:00
              </span>
            </div>
          )
        })}
        {/* snap?.session, not snap. A snapshot that failed to analyse - too
            few bars while the engine is starting, say - carries only {ok,
            reason}, and reaching into .session on one of those threw during
            render and blanked the ENTIRE app, rails and chart included. */}
        {snap?.session && (
          <div style={{ marginTop: 7, paddingTop: 7, borderTop: '1px solid var(--line-soft)' }}>
            <KV k="Primary" v={snap.session.primary} />
            <div style={{ marginTop: 4 }}>
              <div className="kv-k" style={{ marginBottom: 3 }}>Tradeability</div>
              <Meter value={snap.session.quality * 100} />
            </div>
          </div>
        )}
      </Panel>

    </aside>
  )
}
