/**
 * LabCharts - the lab's small analytical charts: equity curve, R histogram,
 * MAE/MFE scatter. Canvas, sized to their box, coloured from the theme.
 */
import React, { useEffect, useRef } from 'react'
import type { LabTrade } from './types'

function css(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

/** A canvas that redraws on resize and whenever `deps` change. */
function useCanvas(draw: (g: CanvasRenderingContext2D, w: number, h: number) => void,
                   deps: React.DependencyList) {
  const ref = useRef<HTMLCanvasElement>(null)
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
      g.clearRect(0, 0, r.width, r.height)
      draw(g, r.width, r.height)
    }
    paint()
    const ro = new ResizeObserver(paint)
    ro.observe(c)
    return () => ro.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return ref
}

const FONT = "9px 'Segoe UI', Inter, system-ui, sans-serif"

/** Balance after each closed trade, with the drawdown shaded under the peak. */
export function EquityCurve({ points, balance0, onPick, cursorT }: {
  points: [number, number][]
  balance0: number
  onPick?: (t: number) => void
  cursorT?: number
}) {
  const ref = useCanvas((g, w, h) => {
    const up = css('--bull', '#9fe547'), down = css('--bear', '#ff2d78')
    const ink = css('--ink-low', '#55617a'), line = css('--line', '#1b2337')
    const pad = { l: 46, r: 8, t: 8, b: 16 }
    g.font = FONT
    if (points.length < 2) {
      g.fillStyle = ink
      g.fillText('No closed trades yet.', pad.l, h / 2)
      return
    }
    const ys = points.map((p) => p[1])
    let lo = Math.min(balance0, ...ys), hi = Math.max(balance0, ...ys)
    if (hi - lo < 1e-9) { hi += 1; lo -= 1 }
    const span = hi - lo
    lo -= span * 0.08; hi += span * 0.08
    const X = (i: number) => pad.l + (i / (points.length - 1)) * (w - pad.l - pad.r)
    const Y = (v: number) => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b)
    // grid + labels
    g.strokeStyle = line; g.lineWidth = 1; g.fillStyle = ink
    for (let k = 0; k <= 3; k++) {
      const v = lo + (hi - lo) * (k / 3)
      const y = Math.round(Y(v)) + 0.5
      g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke()
      g.textAlign = 'right'; g.textBaseline = 'middle'
      g.fillText(v.toFixed(0), pad.l - 4, y)
    }
    // start balance
    g.setLineDash([3, 3]); g.strokeStyle = ink
    g.beginPath(); g.moveTo(pad.l, Y(balance0)); g.lineTo(w - pad.r, Y(balance0)); g.stroke()
    g.setLineDash([])
    // drawdown under the running peak
    let peak = -Infinity
    g.fillStyle = down; g.globalAlpha = 0.16
    for (let i = 0; i < points.length; i++) {
      peak = Math.max(peak, points[i][1])
      if (i && peak > points[i][1]) {
        g.fillRect(X(i - 1), Y(peak), X(i) - X(i - 1), Y(points[i][1]) - Y(peak))
      }
    }
    g.globalAlpha = 1
    // the curve
    const end = points[points.length - 1][1]
    g.strokeStyle = end >= balance0 ? up : down
    g.lineWidth = 1.6
    g.beginPath()
    points.forEach((p, i) => (i ? g.lineTo(X(i), Y(p[1])) : g.moveTo(X(i), Y(p[1]))))
    g.stroke()
    // where the view is
    if (cursorT != null) {
      let k = -1
      for (let i = 0; i < points.length; i++) if (points[i][0] <= cursorT) k = i
      if (k >= 0) {
        g.fillStyle = css('--warn', '#ffb02e')
        g.beginPath(); g.arc(X(k), Y(points[k][1]), 3, 0, Math.PI * 2); g.fill()
      }
    }
  }, [points, balance0, cursorT])

  return (
    <canvas ref={ref} className="lab-canvas" style={{ cursor: onPick ? 'pointer' : 'default' }}
      onClick={(e) => {
        if (!onPick || points.length < 2) return
        const r = (e.target as HTMLCanvasElement).getBoundingClientRect()
        const f = Math.max(0, Math.min(1, (e.clientX - r.left - 46) / (r.width - 54)))
        onPick(points[Math.round(f * (points.length - 1))][0])
      }} />
  )
}

/** Distribution of trade results in R. */
export function RHistogram({ bins }: { bins: [number, number][] }) {
  const ref = useCanvas((g, w, h) => {
    const up = css('--bull', '#9fe547'), down = css('--bear', '#ff2d78')
    const ink = css('--ink-low', '#55617a')
    g.font = FONT
    if (!bins.length || bins.every((b) => !b[1])) {
      g.fillStyle = ink; g.fillText('No closed trades yet.', 8, h / 2); return
    }
    const pad = { l: 8, r: 8, t: 8, b: 16 }
    const max = Math.max(...bins.map((b) => b[1]))
    const bw = (w - pad.l - pad.r) / bins.length
    bins.forEach(([edge, n], i) => {
      const bh = (n / max) * (h - pad.t - pad.b)
      g.fillStyle = edge >= 0 ? up : down
      g.globalAlpha = 0.8
      g.fillRect(pad.l + i * bw + 1, h - pad.b - bh, bw - 2, bh)
    })
    g.globalAlpha = 1
    g.fillStyle = ink; g.textAlign = 'center'; g.textBaseline = 'top'
    bins.forEach(([edge], i) => {
      if (Number.isInteger(edge)) g.fillText(`${edge}R`, pad.l + i * bw, h - pad.b + 3)
    })
  }, [bins])
  return <canvas ref={ref} className="lab-canvas" />
}

/**
 * Each trade's worst excursion against (MAE) and best in favour (MFE), in R.
 * Winners far to the right of their MFE say targets leave money behind;
 * losers with a large MFE say the stop or the exit gave it back.
 */
export function ExcursionScatter({ trades, selected, onPick }: {
  trades: LabTrade[]
  selected?: string | null
  onPick?: (t: LabTrade) => void
}) {
  const ref = useCanvas((g, w, h) => {
    const up = css('--bull', '#9fe547'), down = css('--bear', '#ff2d78')
    const ink = css('--ink-low', '#55617a'), line = css('--line', '#1b2337')
    const warn = css('--warn', '#ffb02e')
    g.font = FONT
    const pad = { l: 26, r: 8, t: 8, b: 18 }
    if (!trades.length) { g.fillStyle = ink; g.fillText('No closed trades yet.', pad.l, h / 2); return }
    const mx = Math.max(1.5, ...trades.map((t) => t.mae_r)) * 1.05
    const my = Math.max(2, ...trades.map((t) => t.mfe_r)) * 1.05
    const X = (v: number) => pad.l + (v / mx) * (w - pad.l - pad.r)
    const Y = (v: number) => h - pad.b - (v / my) * (h - pad.t - pad.b)
    g.strokeStyle = line
    g.beginPath(); g.moveTo(X(1), pad.t); g.lineTo(X(1), h - pad.b); g.stroke()
    g.beginPath(); g.moveTo(pad.l, Y(1)); g.lineTo(w - pad.r, Y(1)); g.stroke()
    g.fillStyle = ink; g.textAlign = 'center'; g.textBaseline = 'top'
    g.fillText('MAE (R) →', w / 2, h - pad.b + 4)
    g.save(); g.translate(9, h / 2); g.rotate(-Math.PI / 2); g.fillText('MFE (R) →', 0, -4); g.restore()
    for (const t of trades) {
      g.fillStyle = t.profit > 0 ? up : down
      g.globalAlpha = 0.75
      g.beginPath(); g.arc(X(t.mae_r), Y(t.mfe_r), t.id === selected ? 4.5 : 2.6, 0, Math.PI * 2); g.fill()
      if (t.id === selected) {
        g.globalAlpha = 1; g.strokeStyle = warn; g.lineWidth = 1.5; g.stroke()
      }
    }
    g.globalAlpha = 1
  }, [trades, selected])
  return (
    <canvas ref={ref} className="lab-canvas" style={{ cursor: onPick ? 'pointer' : 'default' }}
      onClick={(e) => {
        if (!onPick || !trades.length) return
        const r = (e.target as HTMLCanvasElement).getBoundingClientRect()
        const mx = Math.max(1.5, ...trades.map((t) => t.mae_r)) * 1.05
        const my = Math.max(2, ...trades.map((t) => t.mfe_r)) * 1.05
        const px = e.clientX - r.left, py = e.clientY - r.top
        let best: LabTrade | null = null, bd = 64
        for (const t of trades) {
          const x = 26 + (t.mae_r / mx) * (r.width - 34)
          const y = r.height - 18 - (t.mfe_r / my) * (r.height - 26)
          const d = (x - px) ** 2 + (y - py) ** 2
          if (d < bd) { bd = d; best = t }
        }
        if (best) onPick(best)
      }} />
  )
}
