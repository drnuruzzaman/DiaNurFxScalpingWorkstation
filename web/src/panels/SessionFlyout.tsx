import React, { useRef, useState } from 'react'
import type { Snapshot } from '../chart/types'
import { clockUTC } from '../lib/format'
import { KV, Meter } from './common'

/**
 * The trading sessions, as a hover card on the status bar.
 *
 * It used to be a panel at the foot of the left rail. Nothing there is acted
 * on - which session is open, and how tradeable it is - and it held a fixed
 * block of rail height permanently to say something you glance at a few times
 * a day. The rail is where the things you work with live, so this moved to the
 * bar and only appears when asked for.
 *
 * The trigger still carries the live state without being opened: a dot per
 * session, lit when it is open. That is the part worth seeing at all times,
 * and it costs one line of the bar instead of a sixth of the rail.
 */

const SESSION_ROWS = [
  { key: 'sydney', label: 'Sydney', open: 21, close: 6 },
  { key: 'tokyo', label: 'Tokyo', open: 0, close: 9 },
  { key: 'london', label: 'London', open: 7, close: 16 },
  { key: 'newyork', label: 'New York', open: 12, close: 21 },
]

export function SessionFlyout({ snap, nowMs }: { snap: Snapshot | null; nowMs: number }) {
  const [open, setOpen] = useState(false)
  const leave = useRef<number | null>(null)

  /**
   * A short grace period on the way out.
   *
   * The card sits ABOVE the trigger, and the gap between the two is a place
   * the pointer passes through. Closing on the first mouseleave made the card
   * vanish while you were moving onto it. Opening is immediate - that one has
   * no such problem.
   */
  const show = () => {
    if (leave.current) { clearTimeout(leave.current); leave.current = null }
    setOpen(true)
  }
  const hide = () => {
    if (leave.current) clearTimeout(leave.current)
    leave.current = window.setTimeout(() => setOpen(false), 160)
  }

  const active: string[] = snap?.session?.active ?? []
  const primary = snap?.session?.primary

  return (
    <div className="sess" onMouseEnter={show} onMouseLeave={hide}>
      <button className="sess-trigger" aria-expanded={open} aria-haspopup="dialog">
        <span className="sess-label">SESSION</span>
        {/* One dot per session, in clock order. Lit is open. */}
        {SESSION_ROWS.map((s) => (
          <span key={s.key} className="dot" title={s.label} style={{
            background: active.includes(s.key) ? 'var(--bull)' : 'var(--ink-dim)',
            boxShadow: active.includes(s.key) ? '0 0 5px var(--bull)' : 'none',
          }} />
        ))}
        {primary && <b className="sess-primary">{primary}</b>}
      </button>

      {open && (
        <div className="sess-card" role="dialog" aria-label="Trading sessions">
          <div className="sess-card-h">
            <span>SESSIONS</span>
            <span className="spacer" />
            <span className="mono t-dim">{clockUTC(nowMs)} UTC</span>
          </div>
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
              render and blanked the ENTIRE app. */}
          {snap?.session && (
            <div style={{ marginTop: 7, paddingTop: 7, borderTop: '1px solid var(--line-soft)' }}>
              <KV k="Primary" v={snap.session.primary} />
              <div style={{ marginTop: 4 }}>
                <div className="kv-k" style={{ marginBottom: 3 }}>Tradeability</div>
                <Meter value={snap.session.quality * 100} />
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
