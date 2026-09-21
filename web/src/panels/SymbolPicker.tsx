import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api, type BrokerSymbol } from '../lib/api'
import { Empty } from './common'

/**
 * Pick the charted instrument, and choose what sits on the watchlist.
 *
 * Two different actions on one row, deliberately: clicking the row CHARTS the
 * symbol and closes, clicking the star pins it to the watchlist and leaves the
 * dialog open. Wiring both to a single click made pinning three symbols a
 * matter of opening the dialog three times.
 */
export function SymbolPicker({
  current, watchlist, onPick, onWatchlist, onClose,
}: {
  current: string
  watchlist: string[]
  onPick: (symbol: string) => void
  onWatchlist: (symbols: string[]) => void
  onClose: () => void
}) {
  const [rows, setRows] = useState<BrokerSymbol[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [q, setQ] = useState('')
  const [sel, setSel] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let stop = false
    api.symbols()
      .then((r) => { if (!stop) setRows(r.symbols) })
      .catch((e) => { if (!stop) setError(e?.message ?? 'could not load symbols') })
    // Focus lands in the search box: the dialog exists to be typed into.
    input.current?.focus()
    return () => { stop = true }
  }, [])

  // Filtered client-side. The broker exposes a few dozen instruments, so a
  // request per keystroke would add latency to solve a problem nobody has.
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase()
    if (!rows) return []
    if (!needle) return rows
    return rows.filter((r) => r.name.toLowerCase().includes(needle)
      || r.group.toLowerCase().includes(needle))
  }, [rows, q])

  useEffect(() => { setSel(0) }, [q])

  const toggleWatch = (name: string) => {
    onWatchlist(watchlist.includes(name)
      ? watchlist.filter((x) => x !== name)
      : [...watchlist, name])
  }

  const choose = (name: string) => { onPick(name); onClose() }

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') { onClose(); return }
    if (e.key === 'ArrowDown') { e.preventDefault(); setSel((i) => Math.min(shown.length - 1, i + 1)) }
    if (e.key === 'ArrowUp') { e.preventDefault(); setSel((i) => Math.max(0, i - 1)) }
    if (e.key === 'Enter' && shown[sel]) { e.preventDefault(); choose(shown[sel].name) }
  }

  // Keep the keyboard selection in view when arrowing past the fold.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-i="${sel}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [sel])

  return (
    <div className="modal-back" onMouseDown={onClose}>
      <div className="picker" onMouseDown={(e) => e.stopPropagation()} onKeyDown={onKey}>
        <div className="picker-h">
          <input ref={input} className="picker-input" value={q} placeholder="Search symbols…"
            onChange={(e) => setQ(e.target.value)} spellCheck={false} />
          <button className="tool-btn" onClick={onClose}>Close</button>
        </div>

        <div className="picker-list" ref={listRef}>
          {error ? <Empty>Could not load symbols — {error}</Empty>
            : !rows ? <Empty>Reading the broker's instrument list…</Empty>
              : !shown.length ? <Empty>Nothing matches “{q}”.</Empty>
                : shown.map((r, i) => {
                  const pinned = watchlist.includes(r.name)
                  return (
                    <div key={r.name} data-i={i}
                      className={`picker-row ${i === sel ? 'sel' : ''} ${r.name === current ? 'on' : ''}`}
                      onMouseEnter={() => setSel(i)}
                      onClick={() => choose(r.name)}>
                      <button className={`pin ${pinned ? 'on' : ''}`}
                        title={pinned ? 'Remove from watchlist' : 'Add to watchlist'}
                        onClick={(e) => { e.stopPropagation(); toggleWatch(r.name) }}>
                        {pinned ? '★' : '☆'}
                      </button>
                      <span className="picker-name">{r.name}</span>
                      <span className="picker-group">{r.group}</span>
                      {/* Only disk-backed symbols can be backtested - the live
                          chart works for any of them. Worth saying here rather
                          than letting a backtest fail later. */}
                      {r.on_disk && <span className="chip chip-mute">history</span>}
                    </div>
                  )
                })}
        </div>

        <div className="picker-f">
          <span className="t-dim">
            ★ pins to the watchlist · click a row to chart it · ↑↓ and Enter work too
          </span>
          <span className="t-dim mono">{shown.length}/{rows?.length ?? 0}</span>
        </div>
      </div>
    </div>
  )
}
