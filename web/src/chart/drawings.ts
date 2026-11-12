/**
 * Your own chart drawings (2026-11-12): trend / horizontal / vertical lines,
 * Fibonacci retracement, ruler, text, brush, long / short position and the
 * harmonic + classic patterns (XABCD, ABCD, Cypher, Head & Shoulders,
 * Triangle, Three drives).
 *
 * Kept out of ChartEngine.ts on purpose: the engine draws what the ENGINE
 * found; this draws what YOU drew. They share one coordinate system - every
 * point is (time ms, price), so a drawing stays on its candle whatever the
 * zoom, and shows on every timeframe of the same symbol.
 *
 * Storage is the browser's localStorage, per scope (live / lab) and symbol.
 */

export type ToolId =
  | 'trend' | 'hline' | 'vline' | 'fib' | 'ruler' | 'text' | 'brush'
  | 'long' | 'short'
  | 'xabcd' | 'abcd' | 'cypher' | 'hs' | 'triangle' | 'threedrives'

export type DPoint = { t: number; p: number }

export type LineDash = 'solid' | 'dashed' | 'dotted'

export type Drawing = {
  id: string
  tool: ToolId
  points: DPoint[]
  text?: string
  /** Set from the selection bar; unset = the tool's default look. */
  color?: string
  width?: number
  dash?: LineDash
}

/** Which tools the selection bar offers colour / width + style for. */
export const STYLE_COLOR: ToolId[] = ['trend', 'hline', 'vline', 'brush', 'text',
  'xabcd', 'abcd', 'cypher', 'hs', 'triangle', 'threedrives']
export const STYLE_LINE: ToolId[] = ['trend', 'hline', 'vline', 'brush',
  'xabcd', 'abcd', 'cypher', 'hs', 'triangle', 'threedrives']

const DASH: Record<LineDash, number[]> = { solid: [], dashed: [7, 4], dotted: [1.5, 3.5] }

/** What the engine hands the drawing code: its coordinate system and looks. */
export interface DrawHost {
  x(t: number): number
  y(p: number): number
  t(x: number): number
  p(y: number): number
  pane: { x: number; y: number; w: number; h: number }
  theme: { up: string; down: string; info: string; warn: string; accent: string;
    text: string; textDim: string; plateRgb: string; chipInk: string; crosshair: string }
  digits: number
  pips(d: number): string
  cash(d: number): string
  /** Whole bars between two times on this chart. */
  barsBetween(t0: number, t1: number): number
  font: string
}

/** Clicks each tool takes. Brush is a drag; the 1-click tools finish at once. */
export const TOOL_POINTS: Record<ToolId, number> = {
  trend: 2, hline: 1, vline: 1, fib: 2, ruler: 2, text: 1, brush: 0,
  long: 1, short: 1,
  xabcd: 5, abcd: 4, cypher: 5, hs: 7, triangle: 4, threedrives: 6,
}

export const TOOL_LABEL: Record<ToolId, string> = {
  trend: 'Trend line', hline: 'Horizontal line', vline: 'Vertical line',
  fib: 'Fib retracement', ruler: 'Measure', text: 'Text', brush: 'Brush',
  long: 'Long position', short: 'Short position',
  xabcd: 'XABCD pattern', abcd: 'ABCD pattern', cypher: 'Cypher pattern',
  hs: 'Head and shoulders', triangle: 'Triangle pattern', threedrives: 'Three drives pattern',
}

export const PATTERN_TOOLS: ToolId[] = ['xabcd', 'cypher', 'abcd', 'hs', 'triangle', 'threedrives']

const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.618]
const FIB_COLOURS = ['#9aa4b8', '#f23645', '#ff9800', '#4caf50', '#089981', '#00bcd4', '#9aa4b8', '#2962ff']

let _seq = 0
export const newId = () => `d${Date.now().toString(36)}${(_seq++).toString(36)}`

// ------------------------------------------------------------------ //
// storage                                                            //
// ------------------------------------------------------------------ //
const KEY = (scope: string, symbol: string) => `dianurfx.drawings.v1.${scope}.${symbol}`

export function loadDrawings(scope: string, symbol: string): Drawing[] {
  if (!symbol) return []
  try {
    const raw = localStorage.getItem(KEY(scope, symbol))
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr.filter((d) => d && d.tool && Array.isArray(d.points)) : []
  } catch {
    return []
  }
}

export function saveDrawings(scope: string, symbol: string, list: Drawing[]): void {
  if (!symbol) return
  try {
    localStorage.setItem(KEY(scope, symbol), JSON.stringify(list.filter((d) => d.tool !== 'ruler')))
  } catch { /* storage full or blocked: drawings live for this session only */ }
}

// ------------------------------------------------------------------ //
// geometry helpers                                                   //
// ------------------------------------------------------------------ //
type XY = [number, number]

function distToSeg(px: number, py: number, a: XY, b: XY): number {
  const dx = b[0] - a[0], dy = b[1] - a[1]
  const L = dx * dx + dy * dy
  let u = L ? ((px - a[0]) * dx + (py - a[1]) * dy) / L : 0
  u = Math.max(0, Math.min(1, u))
  const x = a[0] + u * dx, y = a[1] + u * dy
  return Math.hypot(px - x, py - y)
}

const inRect = (px: number, py: number, x0: number, y0: number, x1: number, y1: number) =>
  px >= Math.min(x0, x1) && px <= Math.max(x0, x1) && py >= Math.min(y0, y1) && py <= Math.max(y0, y1)

const ratio = (num: number, den: number) => (Math.abs(den) > 1e-12 ? Math.abs(num / den) : 0)

/** Pixel position of each point. */
const px = (d: Drawing, h: DrawHost): XY[] => d.points.map((q) => [h.x(q.t), h.y(q.p)])

/** Long / short: points = entry, stop (t = right edge), target (t = right edge). */
function positionBox(d: Drawing, h: DrawHost) {
  const [e, s, g] = d.points
  return { x0: h.x(e.t), x1: h.x(s.t), ye: h.y(e.p), ys: h.y(s.p), yg: h.y(g.p) }
}

/**
 * The grab points of a drawing, in pixels. `idx` is what the drag edits; for
 * a position, 3 is the right edge.
 */
export function handles(d: Drawing, h: DrawHost): XY[] {
  const P = px(d, h)
  switch (d.tool) {
    case 'hline': return [[Math.max(30, Math.min(h.pane.w - 30, P[0][0])), P[0][1]]]
    case 'vline': return [[P[0][0], h.pane.y + h.pane.h / 2]]
    case 'brush': case 'text': case 'ruler': return []
    case 'long': case 'short': {
      const b = positionBox(d, h)
      return [[b.x0, b.ye], [b.x0, b.ys], [b.x0, b.yg], [b.x1, b.ye]]
    }
    default: return P
  }
}

/** Text drawings: their box, for hit testing and the double-click edit. */
function textBox(ctx: CanvasRenderingContext2D, d: Drawing, h: DrawHost) {
  ctx.save()
  ctx.font = `600 12px ui-sans-serif, system-ui`
  const lines = (d.text || 'Text').split('\n')
  const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 10
  ctx.restore()
  const [x, y] = [h.x(d.points[0].t), h.y(d.points[0].p)]
  return { x, y: y - 9, w, h: lines.length * 15 + 4, lines }
}

export type Hit = { kind: 'handle'; idx: number } | { kind: 'body' } | null

/** What the cursor is on, for one drawing. */
export function hitTest(ctx: CanvasRenderingContext2D, d: Drawing, h: DrawHost, mx: number, my: number): Hit {
  const hs = handles(d, h)
  for (let i = 0; i < hs.length; i++) {
    if (Math.hypot(mx - hs[i][0], my - hs[i][1]) <= 7) return { kind: 'handle', idx: i }
  }
  const P = px(d, h)
  const near = (a: XY, b: XY) => distToSeg(mx, my, a, b) <= 5
  switch (d.tool) {
    case 'hline': return Math.abs(my - P[0][1]) <= 5 ? { kind: 'body' } : null
    case 'vline': return Math.abs(mx - P[0][0]) <= 5 ? { kind: 'body' } : null
    case 'text': {
      const b = textBox(ctx, d, h)
      return inRect(mx, my, b.x, b.y, b.x + b.w, b.y + b.h) ? { kind: 'body' } : null
    }
    case 'fib': {
      const [a, b] = P
      return inRect(mx, my, a[0], a[1], b[0], b[1]) || near(a, b) ? { kind: 'body' } : null
    }
    case 'long': case 'short': {
      const b = positionBox(d, h)
      return inRect(mx, my, b.x0, b.ys, b.x1, b.yg) ? { kind: 'body' } : null
    }
    default:
      for (let i = 0; i + 1 < P.length; i++) if (near(P[i], P[i + 1])) return { kind: 'body' }
      if (d.tool === 'triangle' && P.length === 4 && (near(P[0], P[2]) || near(P[1], P[3]))) return { kind: 'body' }
      return null
  }
}

// ------------------------------------------------------------------ //
// rendering                                                          //
// ------------------------------------------------------------------ //
function chip(ctx: CanvasRenderingContext2D, h: DrawHost, text: string, x: number, y: number,
              colour: string, align: 'left' | 'center' | 'right' = 'center', filled = false) {
  ctx.save()
  ctx.font = `600 10px ${h.font}`
  const lines = text.split('\n')
  const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 10
  const hh = lines.length * 13 + 4
  const x0 = align === 'center' ? x - w / 2 : align === 'right' ? x - w : x
  const y0 = y - hh / 2
  ctx.fillStyle = filled ? colour : `rgba(${h.theme.plateRgb},0.88)`
  ctx.strokeStyle = colour
  ctx.lineWidth = 1
  ctx.beginPath(); ctx.roundRect(x0, y0, w, hh, 3); ctx.fill(); if (!filled) ctx.stroke()
  ctx.fillStyle = filled ? h.theme.chipInk : colour
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle'
  lines.forEach((l, i) => ctx.fillText(l, x0 + 5, y0 + 2 + 6.5 + i * 13))
  ctx.restore()
}

function line(ctx: CanvasRenderingContext2D, a: XY, b: XY, colour: string, width = 1.5, dash: number[] = []) {
  ctx.strokeStyle = colour; ctx.lineWidth = width; ctx.setLineDash(dash)
  ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke()
  ctx.setLineDash([])
}

function fillPoly(ctx: CanvasRenderingContext2D, pts: XY[], colour: string, alpha: number) {
  if (pts.length < 3) return
  ctx.save(); ctx.globalAlpha = alpha; ctx.fillStyle = colour
  ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1])
  for (const q of pts.slice(1)) ctx.lineTo(q[0], q[1])
  ctx.closePath(); ctx.fill(); ctx.restore()
}

const fmtP = (h: DrawHost, p: number) => p.toFixed(h.digits)

/** A point's letter, above a high and below a low. */
function pointLabel(ctx: CanvasRenderingContext2D, h: DrawHost, P: XY[], i: number, text: string, colour: string) {
  if (!text || !P[i]) return
  const prev = P[i - 1]?.[1] ?? P[i + 1]?.[1] ?? P[i][1]
  const next = P[i + 1]?.[1] ?? prev
  const high = P[i][1] <= Math.min(prev, next)
  chip(ctx, h, text, P[i][0], P[i][1] + (high ? -14 : 14), colour)
}

function ratioOn(ctx: CanvasRenderingContext2D, h: DrawHost, a: XY, b: XY, r: number, colour: string) {
  line(ctx, a, b, colour, 1, [4, 4])
  if (r > 0) chip(ctx, h, r.toFixed(3), (a[0] + b[0]) / 2, (a[1] + b[1]) / 2, colour)
}

function drawPattern(ctx: CanvasRenderingContext2D, d: Drawing, h: DrawHost, P: XY[], colour: string) {
  const pr = d.points.map((q) => q.p)
  const n = P.length
  const lw = d.width ?? 2, dash = DASH[d.dash ?? 'solid']
  const poly = () => { for (let i = 0; i + 1 < n; i++) line(ctx, P[i], P[i + 1], colour, lw, dash) }
  switch (d.tool) {
    case 'xabcd': case 'cypher': {
      if (n >= 3) fillPoly(ctx, P.slice(0, 3), colour, 0.12)
      if (n >= 5) fillPoly(ctx, P.slice(2, 5), colour, 0.12)
      poly()
      const [X, A, B, C, D] = pr
      if (n >= 3) ratioOn(ctx, h, P[0], P[2], ratio(A - B, A - X), colour)
      if (d.tool === 'xabcd') {
        if (n >= 4) ratioOn(ctx, h, P[1], P[3], ratio(C - B, A - B), colour)
        if (n >= 5) ratioOn(ctx, h, P[2], P[4], ratio(D - C, C - B), colour)
        if (n >= 5) ratioOn(ctx, h, P[0], P[4], ratio(A - D, A - X), colour)
      } else {
        if (n >= 4) ratioOn(ctx, h, P[0], P[3], ratio(C - X, A - X), colour)
        if (n >= 5) ratioOn(ctx, h, P[0], P[4], ratio(D - C, C - X), colour)
      }
      ;['X', 'A', 'B', 'C', 'D'].forEach((t, i) => pointLabel(ctx, h, P, i, t, colour))
      break
    }
    case 'abcd': {
      poly()
      const [A, B, C, D] = pr
      if (n >= 3) ratioOn(ctx, h, P[0], P[2], ratio(C - B, B - A), colour)
      if (n >= 4) ratioOn(ctx, h, P[1], P[3], ratio(D - C, C - B), colour)
      ;['A', 'B', 'C', 'D'].forEach((t, i) => pointLabel(ctx, h, P, i, t, colour))
      break
    }
    case 'hs': {
      for (let i = 0; i + 2 < n; i += 2) fillPoly(ctx, P.slice(i, i + 3), colour, 0.12)
      poly()
      if (n >= 5) {
        // Neckline through the two troughs (peaks for an inverse), carried
        // across the whole pattern.
        const [a, b] = [P[2], P[4]]
        const k = (b[1] - a[1]) / ((b[0] - a[0]) || 1)
        const xa = P[0][0], xb = P[n - 1][0] + (n >= 7 ? 20 : 0)
        line(ctx, [xa, a[1] + k * (xa - a[0])], [xb, a[1] + k * (xb - a[0])], colour, 1.2, [6, 4])
      }
      const names = ['', 'Left shoulder', '', 'Head', '', 'Right shoulder', '']
      names.forEach((t, i) => pointLabel(ctx, h, P, i, t, colour))
      break
    }
    case 'triangle': {
      if (n === 4) fillPoly(ctx, [P[0], P[2], P[3], P[1]], colour, 0.1)
      poly()
      if (n >= 3) line(ctx, P[0], P[2], colour, 1.2, [6, 4])
      if (n >= 4) line(ctx, P[1], P[3], colour, 1.2, [6, 4])
      ;['A', 'B', 'C', 'D'].forEach((t, i) => pointLabel(ctx, h, P, i, t, colour))
      break
    }
    case 'threedrives': {
      poly()
      const [S, D1, A, D2, B, D3] = pr
      if (n >= 3) ratioOn(ctx, h, P[0], P[2], ratio(D1 - A, D1 - S), colour)
      if (n >= 4) ratioOn(ctx, h, P[1], P[3], ratio(D2 - A, D1 - A), colour)
      if (n >= 5) ratioOn(ctx, h, P[2], P[4], ratio(D2 - B, D2 - A), colour)
      if (n >= 6) ratioOn(ctx, h, P[3], P[5], ratio(D3 - B, D2 - B), colour)
      ;['', 'Drive 1', 'A', 'Drive 2', 'B', 'Drive 3'].forEach((t, i) => pointLabel(ctx, h, P, i, t, colour))
      break
    }
  }
}

function fmtSpan(ms: number): string {
  const m = Math.round(Math.abs(ms) / 60000)
  if (m < 60) return `${m}m`
  const hr = Math.floor(m / 60)
  if (hr < 48) return `${hr}h ${m % 60}m`
  return `${Math.floor(hr / 24)}d ${hr % 24}h`
}

/** Draw one drawing. `selected` adds its grab handles. */
export function renderDrawing(ctx: CanvasRenderingContext2D, d: Drawing, h: DrawHost, selected = false) {
  const th = h.theme
  const P = px(d, h)
  const col = d.color ?? th.info
  const lw = d.width ?? 2
  const dash = DASH[d.dash ?? 'solid']
  ctx.save()
  ctx.beginPath(); ctx.rect(h.pane.x, h.pane.y, h.pane.w, h.pane.h); ctx.clip()
  ctx.lineCap = 'round'; ctx.lineJoin = 'round'
  switch (d.tool) {
    case 'trend':
      if (P.length >= 2) line(ctx, P[0], P[1], col, lw, dash)
      break
    case 'hline': {
      const y = P[0][1]
      line(ctx, [h.pane.x, y], [h.pane.x + h.pane.w, y], col, lw, dash)
      chip(ctx, h, fmtP(h, d.points[0].p), h.pane.x + h.pane.w - 4, y, col, 'right', true)
      break
    }
    case 'vline': {
      const x = P[0][0]
      line(ctx, [x, h.pane.y], [x, h.pane.y + h.pane.h], col, lw, dash)
      const dt = new Date(d.points[0].t)
      const lab = `${String(dt.getUTCDate()).padStart(2, '0')} ${dt.toLocaleString('en', { month: 'short', timeZone: 'UTC' })} ` +
        `${String(dt.getUTCHours()).padStart(2, '0')}:${String(dt.getUTCMinutes()).padStart(2, '0')}`
      chip(ctx, h, lab, x, h.pane.y + h.pane.h - 12, col, 'center', true)
      break
    }
    case 'fib': {
      if (P.length < 2) break
      const [a, b] = d.points
      const xl = Math.min(P[0][0], P[1][0]), xr = Math.max(P[0][0], P[1][0])
      line(ctx, P[0], P[1], th.textDim, 1, [4, 4])
      let prevY: number | null = null
      FIB_LEVELS.forEach((lv, i) => {
        const price = b.p + (a.p - b.p) * lv
        const y = h.y(price)
        if (prevY != null) {
          ctx.save(); ctx.globalAlpha = 0.07; ctx.fillStyle = FIB_COLOURS[i]
          ctx.fillRect(xl, Math.min(y, prevY), xr - xl, Math.abs(y - prevY)); ctx.restore()
        }
        prevY = y
        line(ctx, [xl, y], [xr, y], FIB_COLOURS[i], 1.2)
        ctx.font = `600 10px ${h.font}`; ctx.fillStyle = FIB_COLOURS[i]
        ctx.textAlign = 'right'; ctx.textBaseline = 'middle'
        ctx.fillText(`${lv} (${fmtP(h, price)})`, xl - 4, y)
      })
      break
    }
    case 'ruler': {
      if (P.length < 2) break
      const [a, b] = d.points
      const up = b.p >= a.p
      const col = up ? th.info : th.down
      ctx.save(); ctx.globalAlpha = 0.16; ctx.fillStyle = col
      ctx.fillRect(Math.min(P[0][0], P[1][0]), Math.min(P[0][1], P[1][1]),
        Math.abs(P[1][0] - P[0][0]), Math.abs(P[1][1] - P[0][1]))
      ctx.restore()
      const mx = (P[0][0] + P[1][0]) / 2, my = (P[0][1] + P[1][1]) / 2
      line(ctx, [mx, P[0][1]], [mx, P[1][1]], col, 1.2)
      line(ctx, [P[0][0], my], [P[1][0], my], col, 1.2)
      const dp = b.p - a.p
      const pct = a.p ? (dp / a.p) * 100 : 0
      const bars = h.barsBetween(a.t, b.t)
      const cash = h.cash(Math.abs(dp))
      const txt = `${dp >= 0 ? '+' : '-'}${fmtP(h, Math.abs(dp))} (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%) ${h.pips(Math.abs(dp))}p` +
        `${cash ? `  ${dp >= 0 ? '+' : '-'}${cash}` : ''}\n${bars} bars, ${fmtSpan(b.t - a.t)}`
      chip(ctx, h, txt, mx, up ? Math.min(P[0][1], P[1][1]) - 22 : Math.max(P[0][1], P[1][1]) + 22, col, 'center', true)
      break
    }
    case 'text': {
      const b = textBox(ctx, d, h)
      ctx.font = `600 12px ui-sans-serif, system-ui`
      ctx.fillStyle = d.color ?? th.text; ctx.textAlign = 'left'; ctx.textBaseline = 'top'
      b.lines.forEach((l, i) => ctx.fillText(l, b.x + 5, b.y + 2 + i * 15))
      if (selected) { ctx.strokeStyle = th.info; ctx.setLineDash([3, 3]); ctx.strokeRect(b.x, b.y, b.w, b.h); ctx.setLineDash([]) }
      break
    }
    case 'brush': {
      if (P.length < 2) break
      ctx.strokeStyle = d.color ?? th.accent; ctx.lineWidth = d.width ?? 2; ctx.setLineDash(dash)
      ctx.beginPath(); ctx.moveTo(P[0][0], P[0][1])
      for (const q of P.slice(1)) ctx.lineTo(q[0], q[1])
      ctx.stroke(); ctx.setLineDash([])
      break
    }
    case 'long': case 'short': {
      if (d.points.length < 3) break
      const b = positionBox(d, h)
      const [e, s, g] = d.points
      const x0 = Math.min(b.x0, b.x1), w = Math.abs(b.x1 - b.x0)
      ctx.save()
      ctx.globalAlpha = 0.2
      ctx.fillStyle = th.up; ctx.fillRect(x0, Math.min(b.ye, b.yg), w, Math.abs(b.yg - b.ye))
      ctx.fillStyle = th.down; ctx.fillRect(x0, Math.min(b.ye, b.ys), w, Math.abs(b.ys - b.ye))
      ctx.restore()
      line(ctx, [x0, b.ye], [x0 + w, b.ye], th.textDim, 1.2)
      const risk = Math.abs(e.p - s.p), reward = Math.abs(g.p - e.p)
      const rr = risk > 0 ? reward / risk : 0
      const pct = (v: number) => (e.p ? ((v / e.p) * 100).toFixed(2) : '0')
      const cashR = h.cash(reward), cashS = h.cash(risk)
      chip(ctx, h, `Target ${fmtP(h, reward)} (${pct(reward)}%) ${h.pips(reward)}p${cashR ? ` ${cashR}` : ''}`,
        x0 + w / 2, b.yg + (b.yg < b.ye ? -11 : 11), th.up, 'center', true)
      chip(ctx, h, `Stop ${fmtP(h, risk)} (${pct(risk)}%) ${h.pips(risk)}p${cashS ? ` ${cashS}` : ''}`,
        x0 + w / 2, b.ys + (b.ys < b.ye ? -11 : 11), th.down, 'center', true)
      chip(ctx, h, `${d.tool === 'long' ? 'Long' : 'Short'} ${fmtP(h, e.p)}  R:R ${rr.toFixed(2)}`,
        x0 + w / 2, b.ye, th.text)
      break
    }
    default:
      drawPattern(ctx, d, h, P, d.color ?? th.accent)
  }
  if (selected) {
    for (const [x, y] of handles(d, h)) {
      ctx.fillStyle = `rgb(${th.plateRgb})`; ctx.strokeStyle = th.info; ctx.lineWidth = 1.5
      ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke()
    }
  }
  ctx.restore()
}

/**
 * Move one grab point. Each tool decides what a handle may change: a
 * horizontal line only its price, a position's stop and target only their
 * price, its right edge only its end time.
 */
export function moveHandle(d: Drawing, idx: number, q: DPoint): Drawing {
  const pts = d.points.map((x) => ({ ...x }))
  switch (d.tool) {
    case 'hline': pts[0] = { t: q.t, p: q.p }; break
    case 'vline': pts[0] = { t: q.t, p: q.p }; break
    case 'long': case 'short': {
      if (idx === 0) {
        const dp = q.p - pts[0].p
        pts[0] = { t: q.t, p: q.p }
        pts[1].p += dp; pts[2].p += dp
      } else if (idx === 1) pts[1].p = q.p
      else if (idx === 2) pts[2].p = q.p
      else if (idx === 3) { pts[1].t = q.t; pts[2].t = q.t }
      break
    }
    default: if (pts[idx]) pts[idx] = { ...q }
  }
  return { ...d, points: pts }
}

/** A new long / short at `q`: stop one `risk` away, target 2R, 30 bars wide. */
export function newPosition(tool: 'long' | 'short', q: DPoint, risk: number, endT: number): Drawing {
  const s = tool === 'long' ? -1 : 1
  return {
    id: newId(), tool,
    points: [{ ...q }, { t: endT, p: q.p + s * risk }, { t: endT, p: q.p - s * 2 * risk }],
  }
}
