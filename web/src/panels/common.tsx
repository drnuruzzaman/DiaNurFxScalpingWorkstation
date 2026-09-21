/** Small shared presentational pieces used by several panels. */

import React from 'react'
import { dirClass, fmt, signed } from '../lib/format'

export function Panel({
  title, right, children, grow, bodyClass,
}: {
  title: string
  right?: React.ReactNode
  children: React.ReactNode
  grow?: boolean
  bodyClass?: string
}) {
  return (
    <section className={grow ? 'grow' : undefined}>
      <div className="panel-head">
        <span>{title}</span>
        {right}
      </div>
      <div className={bodyClass ?? 'panel-body'} style={grow ? { flex: '1 1 auto', minHeight: 0, overflowY: 'auto' } : undefined}>
        {children}
      </div>
    </section>
  )
}

export function KV({ k, v, cls }: { k: string; v: React.ReactNode; cls?: string }) {
  return (
    <div className="kv">
      <span className="kv-k">{k}</span>
      <span className={`kv-v ${cls ?? ''}`}>{v}</span>
    </div>
  )
}

/**
 * A meter that understands signed scores.
 *
 * Signed values grow out from the centre in the direction of their sign, which
 * is the only way a -100..100 momentum reading is legible at a glance; an
 * unsigned bar would show -80 and +80 identically.
 */
export function Meter({ value, signed: isSigned = false, max = 100 }: {
  value: number
  signed?: boolean
  max?: number
}) {
  const v = Math.max(-max, Math.min(max, value || 0))
  if (!isSigned) {
    const w = Math.max(0, Math.min(100, (v / max) * 100))
    const col = v >= 66 ? 'var(--bull)' : v >= 33 ? 'var(--warn)' : 'var(--bear)'
    return (
      <div className="meter">
        <div className="meter-fill" style={{ left: 0, width: `${w}%`, background: col }} />
      </div>
    )
  }
  const half = Math.abs(v) / max * 50
  const col = v > 0 ? 'var(--bull)' : 'var(--bear)'
  return (
    <div className="meter">
      <div
        className="meter-fill"
        style={{
          left: v >= 0 ? '50%' : `${50 - half}%`,
          width: `${half}%`,
          background: col,
        }}
      />
      <div style={{
        position: 'absolute', left: '50%', top: 0, bottom: 0,
        width: 1, background: 'var(--ink-dim)', opacity: 0.6,
      }} />
    </div>
  )
}

export function ScoreRow({ label, value, signed: isSigned = true }: {
  label: string
  value: number
  signed?: boolean
}) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '68px 1fr 40px', gap: 8, alignItems: 'center', padding: '3px 0' }}>
      <span className="kv-k">{label}</span>
      <Meter value={value} signed={isSigned} />
      <span className={`kv-v ${isSigned ? dirClass(value) : ''}`} style={{ fontSize: 10 }}>
        {isSigned ? signed(value) : fmt(value, 0)}
      </span>
    </div>
  )
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>
}
