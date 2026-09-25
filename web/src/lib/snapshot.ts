import type { Signal, Snapshot } from '../chart/types'

/**
 * The shareable image.
 *
 * Deliberately NOT a DOM screenshot. html2canvas and friends re-implement a
 * browser's layout engine badly, pull in a large dependency and still get
 * canvas content wrong. This draws the rail from the SAME data the panels
 * render from, so the output is a designed sheet rather than a picture of a
 * window - and the chart itself is re-rendered by the real chart engine in a
 * light palette rather than screen-grabbed dark.
 *
 * Everything here is drawn from values the engine actually computed. Where a
 * field does not exist the row is omitted rather than filled with a plausible
 * number: an exported image outlives the session it came from, and a made-up
 * statistic on it is a lie with a long shelf life.
 */

const MONO = "'SF Mono','JetBrains Mono','Fira Code','Cascadia Mono',Consolas,monospace"
const SANS = "'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif"

/** Paper palette. Mirrors PAPER_THEME in the chart engine. */
const C = {
  bg: '#ffffff',
  ink: '#161d29',
  mid: '#4a5568',
  dim: '#8a94a6',
  faint: '#c3cad6',
  rule: '#e3e8ef',
  up: '#4c7a1e',
  down: '#c2185b',
  info: '#0f6fa8',
  warn: '#b45309',
  accent: '#7c3aed',
  brand: '#e31c79',        // sampled from the logo, not guessed
  markBlue: '#171c8f',
  markAmber: '#ff9e1b',
  markLime: '#93c90f',
  markGrey: '#b1b3b3',
}

export type PanelKey = 'signal' | 'patterns' | 'gauge' | 'trend' | 'evidence'

/**
 * The side panels, IN THE ORDER THEY ARE ALWAYS DRAWN. The order you tick
 * them in the menu does not matter - a shared image reads the same way
 * every time: the trade, what the chart shows, the mood, the trend, and why.
 */
export const PANEL_LABELS: { key: PanelKey; label: string }[] = [
  { key: 'signal', label: 'Live signals' },
  { key: 'patterns', label: 'Detected patterns' },
  { key: 'gauge', label: 'Fear & Greed index' },
  { key: 'trend', label: 'Trend read' },
  { key: 'evidence', label: 'Evidence' },
]

export type SnapshotOpts = {
  /** The live chart's wrapper, whose canvases are re-drawn for the export. */
  wrap: HTMLElement | null
  /** The chart engine, when one is mounted. Without it the export falls back
      to copying the on-screen (dark) canvas. */
  engine?: PaperEngine | null
  panels: PanelKey[]
  symbol: string
  tf: string
  snap: Snapshot | null
  signal: Signal | null
  digits?: number
  /** A pill in the header, e.g. BACKTEST - so a replay image is never read as live. */
  badge?: string
  /** Replaces "now" in the strap: a replay snapshot is dated by its bar. */
  when?: string
}

// ---------------------------------------------------------------- capture

/**
 * Flatten the chart's canvases into one bitmap.
 *
 * `engine.withTheme` re-draws the base layer in the paper palette for exactly
 * as long as this runs, so the exported chart is light without the on-screen
 * chart ever appearing to flicker.
 */
type ChartGeometry = { plotW: number; subTop: number | null; bottom: number }

type PaperEngine = {
  withTheme(name: string, fn: (c: HTMLCanvasElement) => void,
            size?: { w: number; h: number }): void
  exportGeometry(): ChartGeometry
}

/** Widest and narrowest the exported chart is allowed to get, width : height. */
const ASPECT_MAX = 2.45
const ASPECT_MIN = 1.35

/**
 * Re-draw the chart in the paper palette at a given size and hand back a
 * flat bitmap.
 *
 * `targetH` is in DEVICE pixels, because the caller has already worked out
 * how tall the sheet's body is from the rail. Passing CSS pixels here was the
 * first version and it silently halved the height on a retina screen.
 */
type Painted = { canvas: HTMLCanvasElement; geom: ChartGeometry | null }

function paintChart(
  wrap: HTMLElement | null,
  engine: PaperEngine | null | undefined,
  targetH: number,
): Painted | null {
  const canvases = wrap
    ? Array.from(wrap.querySelectorAll('canvas')) as HTMLCanvasElement[]
    : Array.from(document.querySelectorAll('.chart-wrap canvas')) as HTMLCanvasElement[]
  if (!canvases.length) return null
  const first = canvases[0]
  if (!first.width || !first.height) return null

  const out = document.createElement('canvas')
  const ctx = out.getContext('2d')!
  const paint = (src: HTMLCanvasElement) => {
    out.width = src.width
    out.height = src.height
    ctx.fillStyle = C.bg
    ctx.fillRect(0, 0, out.width, out.height)
    ctx.drawImage(src, 0, 0)
  }

  if (!engine) {
    // No engine: copy what is on screen, dark palette and all. Better than
    // refusing to export.
    paint(first)
    return { canvas: out, geom: null }
  }

  const dpr = Math.min(Math.max(window.devicePixelRatio || 1, 1), 3)
  const cssW = Math.max(760, Math.round(first.width / dpr))
  const cssH = Math.round(
    Math.min(cssW / ASPECT_MIN, Math.max(cssW / ASPECT_MAX, targetH / dpr)))

  // Only the BASE layer is taken. The top canvas is the crosshair, and a
  // stray cursor line through an exported chart looks like a drawn level.
  //
  // The geometry is read INSIDE the callback: withTheme restores the original
  // size on the way out, so asking afterwards would describe the on-screen
  // pane rather than the one just drawn.
  let geom: ChartGeometry | null = null
  engine.withTheme('paper', (base) => {
    paint(base)
    geom = engine.exportGeometry()
  }, { w: cssW, h: cssH })
  return out.width ? { canvas: out, geom } : null
}

/** Kept for callers that only want the chart bitmap. */
export function captureChart(
  wrap: HTMLElement | null, engine?: PaperEngine | null,
): HTMLCanvasElement | null {
  return paintChart(wrap, engine, 0)?.canvas ?? null
}

// ------------------------------------------------------------- primitives

function roundRect(g: CanvasRenderingContext2D, x: number, y: number,
                   w: number, h: number, r: number): void {
  g.beginPath()
  g.moveTo(x + r, y)
  g.arcTo(x + w, y, x + w, y + h, r)
  g.arcTo(x + w, y + h, x, y + h, r)
  g.arcTo(x, y + h, x, y, r)
  g.arcTo(x, y, x + w, y, r)
  g.closePath()
}

/** Greedy wrap. */
function wrapText(g: CanvasRenderingContext2D, text: string, max: number): string[] {
  const words = String(text ?? '').split(/\s+/).filter(Boolean)
  const lines: string[] = []
  let line = ''
  for (const w of words) {
    const next = line ? line + ' ' + w : w
    if (g.measureText(next).width <= max) { line = next; continue }
    if (line) lines.push(line)
    line = w
  }
  if (line) lines.push(line)
  return lines
}

const num = (v: any, d = 2): string =>
  v == null || !Number.isFinite(Number(v)) ? '\u2014' : Number(v).toFixed(d)

/**
 * A rail column with a cursor. Every section appends downward and the column
 * reports how tall it ended up, which is what sizes the sheet.
 */
class Rail {
  y: number
  constructor(
    private g: CanvasRenderingContext2D,
    private x: number,
    private w: number,
    top: number,
  ) { this.y = top }

  gap(n: number) { this.y += n }

  /** Uppercase section title with a rule under it. */
  head(title: string, right?: string, rightColour = C.mid) {
    const g = this.g
    g.font = '700 11.5px ' + MONO
    g.fillStyle = C.brand
    g.textAlign = 'left'
    g.fillText(title.toUpperCase(), this.x, this.y + 10)
    if (right) {
      g.font = '700 10px ' + MONO
      g.fillStyle = rightColour
      g.textAlign = 'right'
      g.fillText(right, this.x + this.w, this.y + 10)
      g.textAlign = 'left'
    }
    this.y += 16
    g.strokeStyle = C.rule
    g.lineWidth = 1
    g.beginPath()
    g.moveTo(this.x, this.y + 0.5)
    g.lineTo(this.x + this.w, this.y + 0.5)
    g.stroke()
    this.y += 9
  }

  /** label .......... value */
  row(label: string, value: string, colour = C.ink, bold = false) {
    const g = this.g
    g.font = '400 10px ' + MONO
    g.fillStyle = C.mid
    g.textAlign = 'left'
    g.fillText(label, this.x, this.y + 9)
    g.font = (bold ? '700 ' : '400 ') + '10px ' + MONO
    g.fillStyle = colour
    g.textAlign = 'right'
    g.fillText(value, this.x + this.w, this.y + 9)
    g.textAlign = 'left'
    this.y += 14
  }

  /** Small wrapped explanatory text. */
  note(text: string, colour = C.dim, size = 9) {
    const g = this.g
    g.font = '400 ' + size + 'px ' + MONO
    g.fillStyle = colour
    g.textAlign = 'left'
    for (const ln of wrapText(g, text, this.w)) {
      g.fillText(ln, this.x, this.y + 8)
      this.y += size + 3
    }
  }

  /**
   * A 0..100 track with a marker and two end labels.
   * The marker is a segment rather than a dot so it reads at print size.
   */
  track(label: string, value: number, lo: string, hi: string, shown: string) {
    const g = this.g
    g.font = '400 9.5px ' + MONO
    g.fillStyle = C.mid
    g.textAlign = 'left'
    g.fillText(label, this.x, this.y + 8)

    const bx = this.x + 104
    const bw = this.w - 104 - 34
    g.fillStyle = '#eef1f6'
    roundRect(g, bx, this.y + 3, bw, 6, 3); g.fill()

    const t = Math.max(0, Math.min(100, value)) / 100
    g.fillStyle = value >= 50 ? C.up : C.down
    roundRect(g, bx + Math.max(0, t * bw - 9), this.y + 2, 9, 8, 3); g.fill()

    g.font = '700 9.5px ' + MONO
    g.fillStyle = C.ink
    g.textAlign = 'right'
    g.fillText(shown, this.x + this.w, this.y + 8)
    this.y += 12

    g.font = '400 8px ' + MONO
    g.fillStyle = C.faint
    g.textAlign = 'left'
    g.fillText(lo, bx, this.y + 6)
    g.textAlign = 'right'
    g.fillText(hi, bx + bw, this.y + 6)
    g.textAlign = 'left'
    this.y += 12
  }

  /** Centre-zero bar: evidence for to the right, against to the left. */
  diverging(label: string, value: number, max: number) {
    const g = this.g
    g.font = '400 9.5px ' + MONO
    g.fillStyle = C.mid
    g.textAlign = 'left'
    g.fillText(label, this.x, this.y + 8)

    const bx = this.x + 104
    const bw = this.w - 104 - 34
    const mid = bx + bw / 2
    g.strokeStyle = C.rule
    g.lineWidth = 1
    g.beginPath(); g.moveTo(mid + 0.5, this.y); g.lineTo(mid + 0.5, this.y + 11); g.stroke()

    const t = max > 0 ? Math.max(-1, Math.min(1, value / max)) : 0
    const len = Math.abs(t) * (bw / 2)
    g.fillStyle = value >= 0 ? C.up : C.down
    if (len > 0.5) {
      roundRect(g, value >= 0 ? mid : mid - len, this.y + 2.5, len, 6, 2)
      g.fill()
    }

    g.font = '700 9.5px ' + MONO
    g.fillStyle = value >= 0 ? C.up : C.down
    g.textAlign = 'right'
    g.fillText(value > 0 ? '+' + value : String(value), this.x + this.w, this.y + 8)
    g.textAlign = 'left'
    this.y += 14
  }

  /** The fear & greed arc. */
  gauge(value: number, label: string) {
    const g = this.g
    const cx = this.x + this.w / 2
    const cy = this.y + 70
    const r = 53
    const START = Math.PI * 0.82
    const END = Math.PI * 2.18

    const bands: [number, number, string][] = [
      [0, 20, '#b0114f'], [20, 40, '#e0568c'],
      [40, 60, '#e7ebf1'], [60, 80, '#9ac96a'], [80, 100, '#4c7a1e'],
    ]
    g.lineWidth = 17
    g.lineCap = 'butt'
    for (const band of bands) {
      g.strokeStyle = band[2]
      g.beginPath()
      g.arc(cx, cy, r, START + (END - START) * (band[0] / 100),
            START + (END - START) * (band[1] / 100))
      g.stroke()
    }

    g.font = '700 7.5px ' + MONO
    g.fillStyle = C.dim
    g.textAlign = 'center'
    for (const m of [0, 50, 100]) {
      const a = START + (END - START) * (m / 100)
      g.fillText(String(m), cx + Math.cos(a) * (r + 15), cy + Math.sin(a) * (r + 15) + 3)
    }

    const a = START + (END - START) * (Math.max(0, Math.min(100, value)) / 100)
    g.strokeStyle = C.ink
    g.lineWidth = 2.5
    g.lineCap = 'round'
    g.beginPath()
    g.moveTo(cx, cy)
    g.lineTo(cx + Math.cos(a) * (r + 3), cy + Math.sin(a) * (r + 3))
    g.stroke()
    g.fillStyle = C.ink
    g.beginPath(); g.arc(cx, cy, 4.5, 0, Math.PI * 2); g.fill()

    g.font = '700 23px ' + MONO
    g.fillStyle = C.ink
    g.fillText(String(value), cx, cy + 32)
    g.font = '700 9px ' + MONO
    g.fillStyle = C.mid
    g.fillText(label.toUpperCase(), cx, cy + 46)
    g.textAlign = 'left'
    this.y = cy + 52
  }
}

// ------------------------------------------------------------------ prose

/**
 * The paragraph under the signal rows.
 *
 * Assembled from the signal's own fields, not from a language model, so the
 * image cannot claim something the engine did not compute. A pending entry
 * says what arms it and when it expires, because a stop order that quietly
 * cancels is the detail people forget.
 */
/** Sentence case with a full stop. The engine's strings are fragments. */
function sentence(t: any): string {
  const s = String(t ?? '').trim()
  if (!s) return ''
  const head = s.charAt(0).toUpperCase() + s.slice(1)
  return /[.!?]$/.test(head) ? head : head + '.'
}

function signalProse(sig: any, snap: Snapshot | null): string {
  const out: string[] = []
  const buy = sig.side === 'buy'

  const ev = (sig.evidence || []).slice()
    .sort((a: any, b: any) => (b.weight || 0) - (a.weight || 0))[0]
  if (ev && ev.text) out.push(sentence(ev.text))

  if (sig.entry_type === 'stop' || sig.entry_type === 'limit') {
    out.push('Fills only if price trades ' + (buy ? 'up through ' : 'down through ')
      + num(sig.trigger || sig.entry, 2)
      + (sig.expiry_bars ? ' within ' + sig.expiry_bars + ' bars, else it is cancelled.' : '.'))
  } else {
    out.push('Entry is at market on this bar.')
  }

  if (sig.risk_points) {
    const atr = sig.atr_at_signal
      ? ' (' + num(sig.risk_points / sig.atr_at_signal, 1) + ' ATR)' : ''
    out.push('Stop ' + num(sig.risk_points, 2) + ' away' + atr + ' \u2014 that is 1R.')
  }
  if (sig.rr1 || sig.rr2) {
    const plan = sig.exit_plan || {}
    if (plan.mode === 'trail') {
      out.push('At ' + num(sig.rr1, 1) + 'R the stop locks +' + plan.lock_r
        + 'R, then trails ' + plan.trail_atr + ' ATR; ' + num(sig.rr2, 1) + 'R is the objective.')
    } else {
      out.push('Targets sit at ' + num(sig.rr1, 1) + 'R and ' + num(sig.rr2, 1) + 'R.')
    }
  }
  if (sig.invalidation) out.push(sentence(sig.invalidation))
  if (snap && (snap as any).spread_points != null) {
    out.push('Spread at capture ' + num((snap as any).spread_points, 0) + ' pts.')
  }
  return out.join(' ')
}

// ------------------------------------------------------------------- rail

/**
 * Lay the rail out into `g`, in DESIGN units starting at (0, 0).
 *
 * The caller sets the transform, so every size in here and in Rail stays a
 * plain number instead of being multiplied by a scale factor at each use -
 * which is where this kind of drawing code usually goes wrong.
 */
function layoutRail(g: CanvasRenderingContext2D, o: SnapshotOpts, w: number): number {
  const snap: any = o.snap
  const signal: any = o.signal
  const dp = o.digits ?? 2
  const r = new Rail(g, 0, w, 0)

  const order = PANEL_LABELS.map((p) => p.key).filter((k) => o.panels.includes(k))
  for (const key of order) {
    if (key === 'patterns') continue       // drawn on the chart, see compose()
    if (key === 'signal') {
      if (!signal) {
        r.head('Live signal', 'NONE', C.dim)
        r.note('No qualifying setup at capture time.')
        r.gap(16)
        continue
      }
      const buy = signal.side === 'buy'
      const col = buy ? C.up : C.down
      const kind = signal.entry_type === 'stop' ? ' STOP'
        : signal.entry_type === 'limit' ? ' LIMIT' : ''
      const status = String(signal.status || '').toUpperCase()
      r.head(String(signal.playbook || 'signal').replace(/_/g, ' '), status,
             signal.status === 'qualified' ? C.up
               : signal.status === 'rejected' ? C.down : C.warn)
      r.row((buy ? 'BUY' : 'SELL') + kind, num(signal.entry, dp), col, true)
      r.row('SL', num(signal.stop, dp), C.down)
      r.row('TP1', num(signal.tp1, dp) + '   ' + num(signal.rr1, 2) + 'R', C.up)
      r.row('TP2', num(signal.tp2, dp) + '   ' + num(signal.rr2, 2) + 'R', C.up)
      if (signal.risk_points) {
        const atr = signal.atr_at_signal
          ? ' (' + num(signal.risk_points / signal.atr_at_signal, 1) + ' ATR)' : ''
        r.row('risk', num(signal.risk_points, 2) + atr, C.mid)
      }
      r.row('confidence', String(signal.confidence ?? '\u2014'), C.ink, true)
      r.gap(6)
      r.note(signalProse(signal, o.snap), C.mid, 8.5)
      r.gap(16)
    }

    if (key === 'gauge') {
      const gg = snap && snap.gauge
      if (!gg) continue
      r.head('Fear & Greed index')
      r.gauge(Number(gg.value ?? 50), String(gg.label || ''))
      r.gap(8)
      // direction is -100..100; the track is 0..100, so centre it.
      r.track('Direction', (Number(gg.direction || 0) + 100) / 2,
              'selling', 'buying', String(gg.direction ?? 0))
      r.track('Volatility', Number(gg.volatility || 0),
              'calm', 'violent', String(gg.volatility ?? 0))
      r.track('Participation', Number(gg.participation || 0),
              'thin', 'busy', String(gg.participation ?? 0))
      r.gap(4)
      r.note('A composite of direction, volatility and participation. '
        + 'Context, not a call: no reading here gates a trade.', C.dim, 8.5)
      r.gap(16)
    }

    if (key === 'trend') {
      const t = snap && snap.trend
      if (!t) continue
      const st = String(t.state || '')
      const col = st.indexOf('up') >= 0 ? C.up : st.indexOf('down') >= 0 ? C.down : C.info
      r.head('Trend read',
             st.indexOf('up') >= 0 ? 'UPTREND'
               : st.indexOf('down') >= 0 ? 'DOWNTREND' : 'RANGE', col)
      r.row('State', (st.replace(/_/g, ' ') + ' \u00b7 ' + (t.strength || '')).trim(),
            col, true)
      if (t.structure) r.row('Structure', String(t.structure), C.ink)
      if (t.invalidation != null) r.row('Invalidation', num(t.invalidation, dp), C.warn)
      const vol = snap && snap.regime && snap.regime.volatility
      if (vol && vol.label) r.row('Volatility', String(vol.label), C.mid)

      const mtf = snap && snap.mtf
      const rows: any[] = (mtf && mtf.rows) || []
      if (rows.length) {
        r.gap(6)
        const agree = mtf.agreement != null
          ? Math.round(Number(mtf.agreement) * 100) + '% agree' : ''
        r.note('TIMEFRAMES   ' + agree, C.dim, 8.5)
        for (const v of rows.slice(0, 6)) {
          const s2 = String(v.state || '')
          const c2 = s2.indexOf('up') >= 0 ? C.up
            : s2.indexOf('down') >= 0 ? C.down : C.dim
          r.row(String(v.tf),
                (s2.replace(/_/g, ' ') + ' \u00b7 ' + (v.strength || '')).trim(), c2)
        }
      }
      r.gap(16)
    }

    if (key === 'evidence') {
      const forE = (signal && signal.evidence) || []
      const agE = (signal && signal.against) || []
      if (!forE.length && !agE.length) continue
      r.head('Evidence',
             signal && signal.confidence != null ? signal.confidence + '/100' : undefined,
             C.ink)
      // Grouped by kind and summed, so each bar is one component of the
      // score rather than one sentence. Three separate 'pattern' rows say
      // less than a single pattern contribution does.
      const by = new Map<string, number>()
      const add = (e: any, sign: number) => {
        const k = String(e.kind || 'other')
        by.set(k, (by.get(k) || 0) + sign * Math.abs(Number(e.weight) || 0))
      }
      for (const e of forE) add(e, 1)
      for (const e of agE) add(e, -1)
      const all = [...by.entries()]
        .map(([t, w]) => ({ t, w: Math.round(w) }))
        .filter((e) => e.w !== 0)
        .sort((a, b) => Math.abs(b.w) - Math.abs(a.w))
      let max = 1
      for (const e of all) max = Math.max(max, Math.abs(e.w))
      for (const e of all.slice(0, 7)) {
        r.diverging(e.t.replace(/_/g, ' ').slice(0, 13), e.w, max)
      }
      r.gap(5)
      r.note('The engine\u2019s own confidence contributions, counter-evidence '
        + 'included. Nothing here is a forecast.', C.dim, 8.5)
      r.gap(16)
    }
  }
  return r.y
}

// ---------------------------------------------------------------- compose

export function compose(o: SnapshotOpts): HTMLCanvasElement | null {
  const snap: any = o.snap
  const dp = o.digits ?? 2

  const probeCanvas = o.wrap
    ? o.wrap.querySelector('canvas') as HTMLCanvasElement | null
    : document.querySelector('.chart-wrap canvas') as HTMLCanvasElement | null
  if (!probeCanvas || !probeCanvas.width) return null

  const dpr = Math.min(Math.max(window.devicePixelRatio || 1, 1), 3)
  const chartW = Math.max(760, Math.round(probeCanvas.width / dpr)) * dpr

  // Everything but the chart is drawn in design units and scaled to match the
  // chart's pixel density, so the two never disagree about how big a pixel is.
  const S = Math.max(1, Math.min(2.5, chartW / 900))

  const RAIL_D = 300                       // rail width, design units
  const PAD = Math.round(18 * S)
  // Detected patterns draw ON the chart; only the other panels need a rail.
  const railPanels = o.panels.filter((k) => k !== 'patterns').length
  const GUT = railPanels ? Math.round(22 * S) : 0
  const RAIL = railPanels ? Math.round(RAIL_D * S) : 0
  const HEAD = Math.round(66 * S)
  // Both strap lines moved inside the chart, so the foot is bare margin.
  const FOOT = Math.round(14 * S)

  // Measure the rail FIRST, then draw the chart to that height. Sizing the
  // chart first and letting the rail run past it is what left a band of empty
  // paper under the candles.
  const probe = document.createElement('canvas')
  probe.width = 8; probe.height = 8
  const railH = railPanels
    ? Math.ceil(layoutRail(probe.getContext('2d')!, o, RAIL_D) * S) : 0

  const painted = paintChart(o.wrap, o.engine, railH)
  if (!painted) return null
  const chart = painted.canvas

  const bodyH = Math.max(chart.height, railH)
  const W = PAD + chart.width + GUT + RAIL + PAD
  const H = HEAD + bodyH + FOOT

  const out = document.createElement('canvas')
  out.width = W
  out.height = H
  const g = out.getContext('2d')!
  g.fillStyle = C.bg
  g.fillRect(0, 0, W, H)
  g.textBaseline = 'alphabetic'

  // ------------------------------------------------------------- header
  // LEFT: who made it - the mark, the wordmark and the strapline under it.
  // RIGHT: when - the moment of capture (or the replay bar), and the risk
  // line under it. No price: the chart's own axis carries that.
  const U = (24 * S) / 22                    // one unit of the 22x22 viewBox
  const markTop = Math.round(13 * S)
  const sq = (ux: number, uy: number, fill: string) => {
    g.fillStyle = fill
    roundRect(g, PAD + ux * U, markTop + uy * U, 10 * U, 10 * U, 1.2 * U)
    g.fill()
  }
  sq(0, 0, C.markBlue)
  sq(12, 0, C.markAmber)
  sq(0, 12, C.markLime)
  sq(12, 12, C.brand)
  sq(6, 6, C.markGrey)
  const tx = PAD + 22 * U + Math.round(11 * S)
  g.textAlign = 'left'
  g.font = '800 ' + Math.round(21 * S) + 'px ' + SANS
  g.fillStyle = C.ink
  g.fillText('DIANUR', tx, Math.round(35 * S))
  g.fillStyle = C.brand
  g.fillText('FX', tx + g.measureText('DIANUR').width, Math.round(35 * S))
  g.font = '600 ' + Math.round(10 * S) + 'px ' + SANS
  g.fillStyle = C.mid
  g.fillText('Where Data Meets Discipline, Analysis, Execution.', PAD, Math.round(56 * S))

  // Local time, not UTC: the person reading this is the person who took it.
  // The zone is named so a shared image is not ambiguous about which local.
  let when: string = o.when ?? ''
  if (!when) {
    const d = new Date()
    try {
      when = d.toLocaleString(undefined, {
        day: '2-digit', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
      })
    } catch {
      when = d.toString()
    }
  }
  const right = W - PAD
  g.textAlign = 'right'
  g.font = '700 ' + Math.round(11.5 * S) + 'px ' + MONO
  g.fillStyle = C.ink
  g.fillText(when, right, Math.round(35 * S))
  g.font = '400 ' + Math.round(10 * S) + 'px ' + MONO
  g.fillStyle = C.dim
  g.fillText('Trading is risky. Do not over trade. You might lose your funds.', right,
             Math.round(56 * S))
  g.textAlign = 'left'

  // -------------------------------------------------------------- chart
  g.drawImage(chart, PAD, HEAD)
  g.strokeStyle = C.rule
  g.lineWidth = 1
  g.strokeRect(PAD + 0.5, HEAD + 0.5, chart.width - 1, chart.height - 1)

  const geom = painted.geom
  const inset = Math.round(12 * S)

  // ---- watermark: the brand, in colour but faint, at the LEFT MIDDLE of
  // the price pane. It is held to the left third of the plot so it never
  // sits over the middle of the chart, where the latest price action is
  // read. Drawn before the labels so their plates cover it, not the reverse.
  {
    const plotW = geom ? geom.plotW : chart.width
    const paneH = geom ? (geom.subTop ?? geom.bottom) : chart.height
    let mark = Math.max(36 * S, Math.min(84 * S, paneH * 0.16))
    const wordOf = (m: number) => {
      g.font = '800 ' + Math.round(m * 0.6) + 'px ' + SANS
      return g.measureText('DIANURFX').width
    }
    // Shrink until mark + gap + word fits in the left third.
    while (mark > 24 * S && mark * 1.35 + wordOf(mark) > plotW / 3) mark *= 0.9
    const wordW = wordOf(mark)
    const gap = mark * 0.35
    const x0 = PAD + Math.max(inset * 2, Math.min(plotW / 3 - mark - gap - wordW, plotW * 0.06))
    const y0 = HEAD + paneH / 2 - mark / 2
    const u = mark / 22                          // same 22x22 grid as the header mark
    g.save()
    g.globalAlpha = 0.14
    const wsq = (ux: number, uy: number, fill: string) => {
      g.fillStyle = fill
      roundRect(g, x0 + ux * u, y0 + uy * u, 10 * u, 10 * u, 1.2 * u)
      g.fill()
    }
    wsq(0, 0, C.markBlue)
    wsq(12, 0, C.markAmber)
    wsq(0, 12, C.markLime)
    wsq(12, 12, C.brand)
    wsq(6, 6, C.markGrey)
    g.font = '800 ' + Math.round(mark * 0.6) + 'px ' + SANS
    g.textAlign = 'left'
    g.textBaseline = 'middle'
    const wx = x0 + mark + gap
    const wy = y0 + mark / 2
    g.fillStyle = C.ink
    g.fillText('DIANUR', wx, wy)
    g.fillStyle = C.brand
    g.fillText('FX', wx + g.measureText('DIANUR').width, wy)
    g.restore()
  }
  const plate = (x: number, y: number, w: number, h: number) => {
    g.fillStyle = 'rgba(255,255,255,0.9)'
    roundRect(g, x, y, w, h, 4 * S)
    g.fill()
  }

  // ---- top left of the chart: what it is of
  {
    const x0 = PAD + inset
    const base = HEAD + Math.round(30 * S)
    g.font = '800 ' + Math.round(18 * S) + 'px ' + SANS
    const symW = g.measureText(o.symbol).width
    g.font = '700 ' + Math.round(12 * S) + 'px ' + MONO
    const tfW = g.measureText(o.tf.toUpperCase()).width
    const tr = snap && snap.trend
    const st = String((tr && tr.state) || '')
    const trendText = st
      ? (st.replace(/_/g, ' ') + (tr.strength ? ' \u00b7 ' + tr.strength : '')).toUpperCase()
      : String((snap && snap.regime && snap.regime.label) || '').toUpperCase()
    g.font = '700 ' + Math.round(11 * S) + 'px ' + MONO
    const pills = [trendText, o.badge || ''].filter(Boolean)
    const pillW = pills.map((t) => g.measureText(t).width + Math.round(16 * S))
    const gap = Math.round(10 * S)
    const total = symW + Math.round(8 * S) + tfW + pillW.reduce((n, w) => n + w + gap, 0)
    plate(x0 - 6 * S, base - 22 * S, total + 12 * S, 30 * S)
    g.textAlign = 'left'
    g.font = '800 ' + Math.round(18 * S) + 'px ' + SANS
    g.fillStyle = C.ink
    g.fillText(o.symbol, x0, base)
    g.font = '700 ' + Math.round(12 * S) + 'px ' + MONO
    g.fillStyle = C.mid
    g.fillText(o.tf.toUpperCase(), x0 + symW + Math.round(8 * S), base)
    let px = x0 + symW + Math.round(8 * S) + tfW + gap
    pills.forEach((t, i) => {
      const col = i === 1 ? C.warn : t.indexOf('UP') >= 0 ? C.up
        : t.indexOf('DOWN') >= 0 ? C.down : t.indexOf('SQUEEZE') >= 0 ? C.accent : C.info
      g.font = '700 ' + Math.round(11 * S) + 'px ' + MONO
      g.fillStyle = col
      g.globalAlpha = 0.12
      roundRect(g, px, base - 15 * S, pillW[i], 20 * S, 3 * S); g.fill()
      g.globalAlpha = 1
      g.textAlign = 'center'
      g.fillText(t, px + pillW[i] / 2, base)
      g.textAlign = 'left'
      px += pillW[i] + gap
    })
  }

  // ---- bottom left of the price pane: the live patterns, each name once
  if (o.panels.includes('patterns')) {
    const live: any[] = ((snap && snap.patterns) || []).filter((p: any) => p.actionable)
    const seen = new Set<string>()
    const rows: { name: string; status: string; col: string }[] = []
    for (const p of live) {
      const name = String(p.label || p.kind)
      const status = String(p.status || '').toUpperCase()
      const k = name + '|' + status
      if (seen.has(k)) continue
      seen.add(k)
      rows.push({ name, status,
        col: p.direction === 'bullish' ? C.up : p.direction === 'bearish' ? C.down : C.info })
    }
    if (rows.length) {
      // Sits on the gap between the price plot and the first indicator pane.
      const band = geom && geom.subTop != null ? geom.subTop
        : (geom ? geom.bottom : chart.height - Math.round(10 * S))
      const rowH = Math.round(13 * S)
      const headH = Math.round(21 * S)
      const bottom = HEAD + band - Math.round(8 * S)
      const top = bottom - headH - rows.length * rowH
      const x0 = PAD + inset
      g.font = '400 ' + Math.round(9 * S) + 'px ' + MONO
      const nameW = Math.max(...rows.map((r) => g.measureText(r.name).width))
      g.font = '700 ' + Math.round(9 * S) + 'px ' + MONO
      const statW = Math.max(...rows.map((r) => g.measureText(r.status).width))
      const boxW = Math.max(nameW + statW + Math.round(20 * S), Math.round(150 * S))
      plate(x0 - 8 * S, top - 6 * S, boxW + 16 * S, bottom - top + 10 * S)
      g.textAlign = 'left'
      g.font = '800 ' + Math.round(9.5 * S) + 'px ' + SANS
      g.fillStyle = C.brand
      g.fillText('DETECTED PATTERNS', x0, top + Math.round(10 * S))
      g.strokeStyle = C.rule
      g.beginPath()
      g.moveTo(x0, top + Math.round(16 * S) + 0.5)
      g.lineTo(x0 + boxW, top + Math.round(16 * S) + 0.5)
      g.stroke()
      rows.forEach((r, i) => {
        const y = top + headH + (i + 1) * rowH - Math.round(4 * S)
        g.textAlign = 'left'
        g.font = '400 ' + Math.round(9 * S) + 'px ' + MONO
        g.fillStyle = C.ink
        g.fillText(r.name, x0, y)
        g.textAlign = 'right'
        g.font = '700 ' + Math.round(9 * S) + 'px ' + MONO
        g.fillStyle = r.col
        g.fillText(r.status, x0 + boxW, y)
      })
      g.textAlign = 'left'
    }
  }

  // --------------------------------------------------------------- rail
  if (railPanels) {
    g.save()
    g.translate(PAD + chart.width + GUT, HEAD)
    g.scale(S, S)
    layoutRail(g, o, RAIL_D)
    g.restore()
  }

  return out
}

// ------------------------------------------------------------------ output

/**
 * Save the canvas as a PNG.
 *
 * Deliberately SYNCHRONOUS. The obvious implementation is toBlob() + an object
 * URL, but toBlob defers its callback to a later task, and by then the click
 * that started this is no longer the current user gesture - browsers treat the
 * resulting download as programmatic and can drop it with no error and no
 * prompt, which is exactly the "save does nothing" symptom. toDataURL runs
 * inline, so the anchor click is still inside the gesture the user made.
 */
export function download(canvas: HTMLCanvasElement, name: string): void {
  const click = (href: string, revoke?: () => void) => {
    const a = document.createElement('a')
    a.href = href
    a.download = name
    a.rel = 'noopener'
    a.style.display = 'none'
    document.body.appendChild(a)   // Firefox ignores a detached anchor
    a.click()
    a.remove()
    if (revoke) setTimeout(revoke, 60000)
  }
  let url: string
  try {
    url = canvas.toDataURL('image/png')
  } catch (e: any) {
    throw new Error('the chart canvas could not be read (' + (e && e.name) + ')')
  }
  if (!url || url === 'data:,') throw new Error('the chart produced an empty image')
  if (url.length < 8000000) { click(url); return }
  canvas.toBlob((blob) => {
    if (!blob) return
    const obj = URL.createObjectURL(blob)
    click(obj, () => URL.revokeObjectURL(obj))
  }, 'image/png')
}

/**
 * Save through the browser's own file dialog, so the person picks the folder
 * and the name.
 *
 * Three things have to hold for that to work, and they fail independently:
 *
 *  1. showSaveFilePicker has to exist.
 *  2. It has to be reached while the click that started this is still the
 *     active user gesture - so it is called before ANY await. The blob is
 *     made afterwards, once the dialog is already open.
 *  3. The platform has to allow writing to the handle it just handed back.
 *     Embedded browsers (the Claude desktop pane, Electron shells, some
 *     webviews) return a perfectly good handle and then refuse
 *     createWritable with "not allowed by the user agent or the platform".
 *
 * Only a cancel is treated as a failure worth surfacing. Everything else
 * falls back to a plain download, because the person asked for an image and
 * should get one even when the nice path is unavailable.
 */
const PICKER_BLOCKED = 'dianur.savePickerBlocked'

/**
 * Has the file dialog already proved useless in this browser?
 *
 * Remembered rather than re-discovered, because discovering it costs the
 * person a dialog that cannot save anything. See saveAs.
 */
function pickerBlocked(): boolean {
  try {
    if (localStorage.getItem(PICKER_BLOCKED) === '1') return true
  } catch { /* private mode: fall through to the hint below */ }
  // A HINT, not a gate.
  //
  // Learning from the failure still costs one wasted dialog, and the shells
  // this fails in are the ones this app actually runs in. Naming them skips
  // that first bad dialog too. Being wrong is cheap in both directions: a
  // false positive means a plain download instead of a folder prompt, and a
  // false negative just falls back to the remembered failure a moment later.
  return /\bElectron\/|\bClaude\//i.test(navigator.userAgent)
}

export async function saveAs(
  canvas: HTMLCanvasElement, name: string,
): Promise<'dialog' | 'downloads'> {
  const w = window as any
  // TWO DIALOGS, ONE SAVE.
  //
  // In an embedded browser - the Claude desktop pane, an Electron shell -
  // showSaveFilePicker exists and opens a real dialog, hands back a real
  // handle, and then refuses createWritable. The fallback below is a plain
  // anchor download, which in those same shells opens the shell's OWN save
  // dialog. So the person picked a file, nothing was written, and then a
  // second dialog appeared that did work.
  //
  // It cannot be detected in advance: the picker is present, and even a probe
  // through OPFS succeeds because that is a different permission path. The
  // only reliable signal is the failure itself - so it is remembered, and the
  // dead dialog is never shown twice on the same machine. Clearing site data
  // makes it try again, which is the right escape hatch if a shell is fixed.
  if (typeof w.showSaveFilePicker !== 'function' || pickerBlocked()) {
    download(canvas, name)
    return 'downloads'
  }

  let handle: any
  try {
    handle = await w.showSaveFilePicker({
      suggestedName: name,
      types: [{ description: 'PNG image', accept: { 'image/png': ['.png'] } }],
    })
  } catch (e: any) {
    // A cancel is a decision; anything else means no usable dialog here.
    if (e && e.name === 'AbortError') throw e
    download(canvas, name)
    return 'downloads'
  }

  try {
    // Harmless where permission is already implied, and fixes the case where
    // the handle needs an explicit grant before it will accept a write.
    if (typeof handle.queryPermission === 'function') {
      const st = await handle.queryPermission({ mode: 'readwrite' })
      if (st !== 'granted' && typeof handle.requestPermission === 'function') {
        await handle.requestPermission({ mode: 'readwrite' })
      }
    }
    const blob: Blob | null = await new Promise((r) => canvas.toBlob(r, 'image/png'))
    if (!blob) throw new Error('the chart produced an empty image')
    const stream = await handle.createWritable()
    await stream.write(blob)
    await stream.close()
    return 'dialog'
  } catch {
    // The handle is unusable here. Record it so the next save skips straight
    // to the download, and fall back using the name the person actually
    // chose in the dialog rather than the one suggested to them.
    try { localStorage.setItem(PICKER_BLOCKED, '1') } catch { /* private mode */ }
    download(canvas, handle?.name || name)
    return 'downloads'
  }
}

export async function copy(canvas: HTMLCanvasElement): Promise<boolean> {
  try {
    const blob: Blob | null = await new Promise((r) => canvas.toBlob(r, 'image/png'))
    if (!blob || !navigator.clipboard || !('write' in navigator.clipboard)) return false
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
    return true
  } catch {
    return false
  }
}
