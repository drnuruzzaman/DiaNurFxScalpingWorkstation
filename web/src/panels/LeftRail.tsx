import React from 'react'
import type { Snapshot } from '../chart/types'
import { fmt, signed } from '../lib/format'
import { Empty, Panel, ScoreRow } from './common'

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

const bandFor = (v: number): Band =>
  BANDS.find((b) => v >= b.lo && v <= b.hi) ?? BANDS[2]

/** The current band as a coloured chip, for the panel header. */
function BandChip({ value }: { value: number }) {
  const b = bandFor(Math.max(0, Math.min(100, value)))
  return (
    <span className="gauge-chip" style={{
      color: b.col, borderColor: b.col, background: `${b.col}1f`,
    }}>{b.lines.join(' ')}</span>
  )
}

function Gauge({ value, label }: { value: number; label: string }) {
  const W = 206, H = 126, CX = W / 2, CY = 104, R = 72, BW = 23
  const START = 190, SWEEP = 160
  // Drawn larger than its own coordinate space. The rail widened to 272 for
  // the AI Analyst, and the dial had stayed at 206px with 6.4px band names -
  // legible only if you leaned in. Scaling the whole SVG keeps every angle,
  // arc and label position exactly as designed; only the pixels grow.
  const SCALE = 1.18
  // Top of the drawing: the outermost arc edge, less a unit of breathing room.
  const TOP = Math.floor(CY - R - (BW + 3) / 2) - 1

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
      {/* The band chip that sat here moved to the panel header. */}

      {/* Cropped at the top. The arc's outer edge - the active band is drawn
          3 units thicker - sits TOP units below the old origin, and that band
          of nothing was showing as a gap under the header. */}
      <svg width={Math.round(W * SCALE)} height={Math.round((H - TOP) * SCALE)}
        viewBox={`0 ${TOP} ${W} ${H - TOP}`} role="img"
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
                  : (li === 0 ? 4 : -5)
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
          <text key={`t${i}-${li}`} fill={b.ink} fontSize="7.2" fontWeight="800"
            letterSpacing="0.06em" opacity={b === active ? 1 : 0.72}>
            <textPath href={`#fg-arc-${i}-${li}`} startOffset="50%"
              textAnchor="middle">{line}</textPath>
          </text>
        )))}

        {[0, 25, 50, 75, 100].map((v) => {
          const [tx, ty] = polar(deg(v), R - BW / 2 - 9)
          return (
            <text key={v} x={tx} y={ty} fill="#7d8aa5" fontSize="8.5"
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
  snap, quote, symbols, symbol, onSymbol,
  quotes = {}, onEditWatchlist,
}: {
  snap: Snapshot | null
  quote: any
  symbols: string[]
  symbol: string
  onSymbol: (s: string) => void
  /** Live bid/ask per symbol, so every row has a price - not just the active one. */
  quotes?: Record<string, any>
  onEditWatchlist?: () => void
}) {
  const gauge = snap?.gauge

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

      {/* The band in the header, not the timeframe. The timeframe is already
          on the chart, the toolbar and the tab; the band is the one-word
          answer this panel exists to give, so it sits where the eye lands. */}
      <Panel title="Fear & Greed" right={gauge ? <BandChip value={gauge.value} /> : null}>
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


    </aside>
  )
}
