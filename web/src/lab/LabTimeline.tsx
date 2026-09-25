/**
 * LabTimeline - the whole session as one strip, like a video scrubber.
 *
 * The simulated stretch is shaded and carries the price line and a dot per
 * trade (green made money, red did not). Past the frontier the strip is
 * blank unless Reveal is on - the scrubber must not leak the future the chart
 * is hiding. Click or drag to move the view; release beyond the frontier and
 * the market is simulated up to that point.
 */
import React, { useEffect, useRef, useState } from 'react'
import type { Bar } from '../chart/types'
import type { LabTrade } from './types'

function css(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

export function LabTimeline({ bars, first, last, k, v, reveal, trades, onSeek, busy }: {
  bars: Bar[]
  first: number
  last: number
  k: number
  v: number
  reveal: boolean
  trades: LabTrade[]
  onSeek: (i: number) => void
  busy: boolean
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  const [drag, setDrag] = useState<number | null>(null)
  const [hover, setHover] = useState<{ x: number; i: number } | null>(null)
  const shownV = drag ?? v

  useEffect(() => {
    const c = ref.current
    if (!c) return
    const paint = () => {
      const r = c.getBoundingClientRect()
      const dpr = window.devicePixelRatio || 1
      c.width = Math.max(1, Math.round(r.width * dpr))
      c.height = Math.max(1, Math.round(r.height * dpr))
      const g = c.getContext('2d')!
      g.setTransform(dpr, 0, 0, dpr, 0, 0)
      const W = r.width, H = r.height
      g.clearRect(0, 0, W, H)
      if (!bars.length || last <= first) return
      const X = (i: number) => ((i - first) / (last - first)) * (W - 2) + 1
      const upto = reveal ? last : k
      // simulated stretch
      g.fillStyle = css('--info-glow', 'rgba(53,214,239,0.12)')
      g.fillRect(X(first), 0, X(k) - X(first), H)
      // price line over what may be shown
      let lo = Infinity, hi = -Infinity
      for (let i = first; i <= upto; i++) { lo = Math.min(lo, bars[i].l); hi = Math.max(hi, bars[i].h) }
      if (hi > lo) {
        const Y = (p: number) => 4 + (1 - (p - lo) / (hi - lo)) * (H - 12)
        const step = Math.max(1, Math.floor((upto - first) / W))
        g.strokeStyle = css('--ink-mid', '#7d8aa5')
        g.globalAlpha = 0.8
        g.lineWidth = 1
        g.beginPath()
        for (let i = first; i <= upto; i += step) {
          const x = X(i), y = Y(bars[i].c)
          if (i === first) g.moveTo(x, y); else g.lineTo(x, y)
        }
        g.stroke()
        g.globalAlpha = 1
      }
      // trades
      const up = css('--bull', '#9fe547'), down = css('--bear', '#ff2d78')
      for (const t of trades) {
        if (!reveal && t.exit_i > k) continue
        g.fillStyle = t.profit > 0 ? up : down
        g.fillRect(Math.round(X(t.entry_i)) - 1, H - 6, 3, 5)
      }
      // frontier
      g.strokeStyle = css('--info', '#35d6ef')
      g.lineWidth = 1
      g.beginPath(); g.moveTo(Math.round(X(k)) + 0.5, 0); g.lineTo(Math.round(X(k)) + 0.5, H); g.stroke()
      // view cursor
      const xv = Math.round(X(shownV)) + 0.5
      g.strokeStyle = css('--warn', '#ffb02e')
      g.lineWidth = 2
      g.beginPath(); g.moveTo(xv, 0); g.lineTo(xv, H); g.stroke()
      g.fillStyle = css('--warn', '#ffb02e')
      g.beginPath(); g.moveTo(xv - 5, 0); g.lineTo(xv + 5, 0); g.lineTo(xv, 6); g.closePath(); g.fill()
    }
    paint()
    const ro = new ResizeObserver(paint)
    ro.observe(c)
    return () => ro.disconnect()
  }, [bars, first, last, k, shownV, reveal, trades])

  const indexAt = (clientX: number) => {
    const r = ref.current!.getBoundingClientRect()
    const f = Math.max(0, Math.min(1, (clientX - r.left - 1) / (r.width - 2)))
    return Math.round(first + f * (last - first))
  }
  const label = (i: number) => {
    const b = bars[i]
    if (!b) return ''
    const d = new Date(b.t)
    return `${d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: '2-digit', timeZone: 'UTC' })} `
      + `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
  }

  return (
    <div className={`lab-timeline ${busy ? 'busy' : ''}`}>
      <span className="lab-tl-edge mono">{label(first)}</span>
      <div className="lab-tl-strip">
        <canvas ref={ref}
          onMouseDown={(e) => { if (!busy) setDrag(indexAt(e.clientX)) }}
          onMouseMove={(e) => {
            const i = indexAt(e.clientX)
            const r = ref.current!.getBoundingClientRect()
            setHover({ x: e.clientX - r.left, i })
            if (drag != null) setDrag(i)
          }}
          onMouseLeave={() => { setHover(null) }}
          onMouseUp={(e) => {
            if (drag == null) return
            const i = indexAt(e.clientX)
            setDrag(null)
            onSeek(i)
          }} />
        {hover && (
          <div className="lab-tl-tip" style={{ left: Math.max(40, Math.min(hover.x, (ref.current?.clientWidth ?? 0) - 40)) }}>
            {label(hover.i)}{hover.i > k ? ' · not simulated yet' : ''}
          </div>
        )}
      </div>
      <span className="lab-tl-edge mono">{label(last)}</span>
    </div>
  )
}
