/**
 * DeleteSession / TrashConfirm - delete something, confirmed in place.
 *
 * window.confirm() is not used: embedded browsers (the desktop app's pane,
 * some webviews) suppress it and it returns false at once, so a button built
 * on it silently does nothing. The first click arms the button; a second click
 * on "yes, delete" within 10 seconds deletes. Failures are shown, not swallowed.
 */
import React, { useEffect, useState } from 'react'
import { lab } from './labApi'

export function TrashIcon({ size = 13 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ display: 'block' }}>
      <path d="M3 6h18" /><path d="M8 6V4h8v2" /><path d="M19 6l-1 14H6L5 6" />
      <path d="M10 11v6" /><path d="M14 11v6" />
    </svg>
  )
}

export function DownloadIcon({ size = 13 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ display: 'block' }}>
      <path d="M12 3v12" /><path d="M7 10l5 5 5-5" /><path d="M5 21h14" />
    </svg>
  )
}

/** A delete button that asks twice in place. onConfirm does the delete. */
export function TrashConfirm({ title, onConfirm, compact = false }: {
  title: string
  onConfirm: () => Promise<unknown>
  compact?: boolean
}) {
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    if (!armed) return
    const t = window.setTimeout(() => setArmed(false), 10_000)
    return () => window.clearTimeout(t)
  }, [armed])

  const stop = (e: React.MouseEvent) => { e.stopPropagation(); e.preventDefault() }
  const go = (e: React.MouseEvent) => {
    stop(e)
    setBusy(true)
    setErr(null)
    onConfirm()
      .catch((x) => setErr(String(x?.message ?? x)))
      .finally(() => { setBusy(false); setArmed(false) })
  }

  if (!armed) {
    return (
      <span className="lab-del">
        <button className={`lab-link danger ${compact ? 'lab-del-x' : ''}`} title={title} disabled={busy}
          onClick={(e) => { stop(e); setErr(null); setArmed(true) }}>
          {compact ? <TrashIcon /> : 'delete'}
        </button>
        {err && <span className="t-down lab-del-err" title={err}>failed: {err}</span>}
      </span>
    )
  }
  return (
    <span className="lab-del" onClick={stop}>
      <button className="lab-link danger lab-del-yes" onClick={go} disabled={busy}
        title="This cannot be undone">
        {busy ? 'deleting…' : 'yes, delete'}
      </button>
      <button className="lab-link" onClick={(e) => { stop(e); setArmed(false) }} disabled={busy}>cancel</button>
    </span>
  )
}

export function DeleteSession({ sid, name, onDeleted, compact = false }: {
  sid: string
  name: string
  onDeleted: () => void
  compact?: boolean
}) {
  return (
    <TrashConfirm compact={compact} title={`Delete "${name}" and its snapshots`}
      onConfirm={() => lab.remove(sid).then((r) => {
        if (!r.ok) throw new Error('not found on disk')
        onDeleted()
      })} />
  )
}
