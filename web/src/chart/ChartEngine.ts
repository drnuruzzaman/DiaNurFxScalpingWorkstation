/**
 * ChartEngine - a purpose-built canvas candlestick renderer.
 *
 * Written rather than adopted because the annotation layer IS the product.
 * Off-the-shelf chart libraries draw candles beautifully and then fight you
 * over filled pattern polygons, projected target boxes, sloped channels with
 * shaded interiors, and per-swing labels - which is most of what an
 * Autochartist-style read consists of. Owning the raster loop makes all of
 * that ordinary drawing code.
 *
 * Architecture:
 *   - TWO stacked canvases. The base holds candles, grid and overlays and is
 *     redrawn only when data or viewport changes. The top holds the crosshair
 *     and hover readout and is redrawn on every mouse move. Without that split,
 *     moving the mouse re-rasterises three hundred candles plus every overlay
 *     at 60fps, which pins a core for no reason.
 *   - Everything is laid out in BAR INDEX space and projected to pixels at draw
 *     time, so pan and zoom never mutate the data.
 *   - Price scale is computed from the visible slice only, with padding, so
 *     zooming into a quiet stretch actually fills the pane.
 */

import type {
  Bar, LayoutOpts, MoneyModel, MtfTrendline, NewsHover, NewsMark, Overlays, PriceArea,
  Snapshot, Signal, Viewport,
} from './types'

export type ChartTheme = {
  bg: string
  grid: string
  gridStrong: string
  text: string
  textDim: string
  up: string
  down: string
  info: string
  warn: string
  accent: string
  crosshair: string
  line: string
  /**
   * Base rgb for the translucent plates behind labels drawn over candles, as
   * 'r,g,b'. Was hard-coded to the midnight near-black, which turned every
   * label on the light export into a dark smear.
   */
  plateRgb: string
  /** Ink for text sitting ON a saturated chip (price tag, crosshair label). */
  chipInk: string
  /**
   * Light the candle bodies instead of flat-filling them.
   *
   * Opt-in per theme rather than always on. A gradient across a 6px body is
   * decoration on most palettes; it only reads as lacquer when the surface
   * behind it is deep enough for the highlight to have somewhere to fall.
   */
  gloss?: boolean
  /**
   * The price-axis strip, when it should differ from the translucent plate.
   *
   * Left undefined, the axis keeps the old behaviour - a wash of plateRgb
   * over the chart background, which reads as the same surface as the candles
   * rather than as the ruler beside them.
   */
  axisFill?: string
  axisLine?: string
}

/**
 * Chart palettes, one per UI theme.
 *
 * The canvas cannot read CSS variables, so the surface colours are mirrored
 * here. Only backgrounds, grid and crosshair change between them - up/down/
 * info/warn/accent are identical everywhere, for the same reason the CSS
 * accents are: those four carry meaning, not decoration.
 */
export const CHART_THEMES: Record<string, ChartTheme> = {
  midnight: {
    bg: '#080b14',
    grid: 'rgba(26,34,54,0.55)',
    gridStrong: '#1a2236',
    text: '#b6c1d6',
    textDim: '#55617a',
    up: '#9fe547',
    down: '#ff2d78',
    info: '#35d6ef',
    warn: '#ffb02e',
    accent: '#c264ff',
    crosshair: '#6b7896',
    line: '#1b2337',
    plateRgb: '8,11,20',
    chipInk: '#05070e',
  },
  navy: {
    bg: '#0a1730',
    grid: 'rgba(35,64,110,0.55)',
    gridStrong: '#23406e',
    text: '#b9cbe6',
    textDim: '#5a6d8c',
    up: '#9fe547',
    down: '#ff2d78',
    info: '#35d6ef',
    warn: '#ffb02e',
    accent: '#c264ff',
    crosshair: '#7e93b4',
    line: '#1d3a63',
    plateRgb: '8,11,20',
    chipInk: '#05070e',
  },
  carbon: {
    bg: '#100f12',
    grid: 'rgba(48,47,55,0.55)',
    gridStrong: '#302f37',
    text: '#c3c1c9',
    textDim: '#66646e',
    up: '#9fe547',
    down: '#ff2d78',
    info: '#35d6ef',
    warn: '#ffb02e',
    accent: '#c264ff',
    crosshair: '#8b8994',
    line: '#2a2930',
    plateRgb: '8,11,20',
    chipInk: '#05070e',
  },
  // Glossy is the one theme that moves the directional colours: Ausloans
  // Green for up, Ausloans Pink for down. The pairing a trader reads is
  // unchanged - green up, pink-red down - so the chart still says the same
  // thing. Amber follows to Ausloans Orange; cyan and violet stay, because
  // the brand has no equivalent of either.
  glossy: {
    bg: '#041e42',
    grid: 'rgba(27,68,113,0.55)',
    gridStrong: '#1b4471',
    text: '#d9d9d6',
    textDim: '#7089a8',
    up: '#93c90f',
    down: '#e31c79',
    info: '#35d6ef',
    warn: '#ff9e1b',
    accent: '#c264ff',
    crosshair: '#9db3cf',
    line: '#1b4471',
    plateRgb: '2,16,31',
    chipInk: '#02101f',
    gloss: true,
    // The axis is a separate object from the chart, so it gets its own face:
    // a lighter navy panel, lit from the left the way every other raised
    // surface in the terminal is, and divided from the candles by a line in
    // the brand navy tint rather than the same grey as the grid.
    axisFill: 'rgba(9,45,86,0.94)',
    axisLine: 'rgba(108,116,232,0.55)',
  },
}

/**
 * Light palette, used only for exported images.
 *
 * The note above says up/down/info/warn/accent stay constant across themes
 * because they carry meaning. That holds between the three DARK themes. It
 * cannot hold here: #9fe547 on white is a highlighter pen, not a candle. So
 * the hues are kept - green up, crimson down, blue info, amber warn, violet
 * accent - and only the luminance is moved far enough to read on paper.
 */
export const PAPER_THEME: ChartTheme = {
  bg: '#ffffff',
  grid: 'rgba(20,32,56,0.05)',
  gridStrong: 'rgba(20,32,56,0.10)',
  text: '#1d2532',
  textDim: '#8a94a6',
  up: '#4c7a1e',
  down: '#c2185b',
  info: '#0f6fa8',
  warn: '#b45309',
  accent: '#7c3aed',
  crosshair: '#8a94a6',
  line: '#dfe4ec',
  plateRgb: '255,255,255',
  chipInk: '#ffffff',
}

export const DARK_THEME: ChartTheme = {
  bg: '#080b14',
  grid: 'rgba(26,34,54,0.55)',
  gridStrong: '#1a2236',
  text: '#b6c1d6',
  textDim: '#55617a',
  up: '#9fe547',
  down: '#ff2d78',
  info: '#35d6ef',
  warn: '#ffb02e',
  accent: '#c264ff',
  crosshair: '#6b7896',
  line: '#1b2337',
  plateRgb: '8,11,20',
  chipInk: '#05070e',
}

/**
 * Short tag for a calendar release.
 *
 * The calendar's own titles are prose ("Non-Farm Employment Change") and do
 * not fit under a candle. These are the releases that actually move gold, so
 * they get the abbreviation a trader would say out loud; anything else falls
 * back to a clipped title rather than being dropped.
 */
const EVENT_TAGS: [RegExp, string][] = [
  [/non-?farm|nfp/i, 'NFP'],
  [/\bFOMC\b|federal funds|fed (?:interest )?rate/i, 'FOMC'],
  [/\bCPI\b|consumer price/i, 'CPI'],
  [/\bPPI\b|producer price/i, 'PPI'],
  [/\bGDP\b/i, 'GDP'],
  // FRED's names are long and formal - "State Unemployment Insurance Weekly
  // Claims Report" clipped to "State Unemp…", which says nothing.
  [/jobless|unemployment insurance|weekly claims/i, 'CLAIMS'],
  [/unemployment rate/i, 'JOBS'],
  [/personal income|PCE/i, 'PCE'],
  [/job openings|jolts/i, 'JOLTS'],
  [/retail sales/i, 'RETAIL'],
  [/\bPMI\b/i, 'PMI'],
  [/powell|fed chair/i, 'POWELL'],
  [/interest rate/i, 'RATE'],
]

function shortEvent(e: { title: string; currency: string }): string {
  for (const [re, tag] of EVENT_TAGS) if (re.test(e.title)) return tag
  return e.title.length > 12 ? `${e.title.slice(0, 11)}…` : e.title
}

export type HoverInfo = {
  x: number
  y: number
  barIndex: number
  bar: Bar | null
  price: number
  time: number
} | null

/**
 * Candles visible when the chart first loads, and what Reset returns to.
 *
 * Kept in step with the engine's tl_lookback_max_bars: the geometry window is
 * capped at the same figure so a trendline's anchors are on screen at this
 * zoom, rather than off the left edge where they cannot be checked.
 */
const DEFAULT_SPAN = 300

/**
 * Chart typefaces. Canvas text gets no ClearType, so a thin face at 8px reads
 * as a smudge on a dark background however sharp the canvas is. Segoe UI and
 * Consolas are hinted for Windows screens at exactly these sizes; the
 * fallbacks cover everything else.
 */
const UI_FONT = "'Segoe UI', Inter, system-ui, sans-serif"
/**
 * A hex colour moved toward white (amount > 0) or black (amount < 0).
 *
 * Deliberately a straight sRGB lerp rather than a perceptual one. The output
 * is the two ends of a gradient across a few pixels of candle body, where
 * what matters is that the step looks even - and sRGB is what the eye is
 * already calibrated to on a screen full of sRGB candles.
 */
function shade(hex: string, amount: number): string {
  const h = hex.replace('#', '')
  const n = parseInt(h.length === 3 ? h.replace(/./g, (c) => c + c) : h, 16)
  if (!Number.isFinite(n)) return hex
  const to = amount >= 0 ? 255 : 0
  const k = Math.abs(amount)
  const mix = (v: number) => Math.round(v + (to - v) * k)
  const r = mix((n >> 16) & 255), g = mix((n >> 8) & 255), b = mix(n & 255)
  return `rgb(${r},${g},${b})`
}

const NUM_FONT = "Consolas, 'Cascadia Mono', ui-monospace, monospace"

const PRICE_AXIS_W = 74

/** Height of a price chip on the axis. */
const TAG_H = 16
/**
 * Minimum distance between two chip centres.
 *
 * TAG_H alone lets two sit exactly flush, and two flush chips of the same
 * colour - a stop and a support, both bullish green - render as one tall
 * block with two numbers in it. The extra two pixels keep a line of axis
 * showing between them, so a chip always reads as one price.
 */
const TAG_GAP = TAG_H + 2
const TIME_AXIS_H = 22
const PANE_GAP = 6

export class ChartEngine {
  private base: HTMLCanvasElement
  private top: HTMLCanvasElement
  private bctx: CanvasRenderingContext2D
  private tctx: CanvasRenderingContext2D
  private dpr = 1

  private bars: Bar[] = []
  private snap: Snapshot | null = null
  private signal: Signal | null = null
  /**
   * chartIndex = snapshotIndex + snapOffset.
   *
   * The engine analyses a fixed tail (bars_for_analysis, 600) and every index
   * it reports - swing.idx, trendline.x1, event.idx - addresses THAT array.
   * The chart's array is a different length: scrolling back prepends up to
   * 1200 more bars, after which snapshot index 0 is no longer chart index 0
   * and every one of those overlays draws in the wrong place.
   *
   * Recovered from any anchor carrying both an index and a timestamp - swings
   * carry both - so it needs no cooperation from the payload.
   */
  private snapOffset = 0

  private layoutOpts: LayoutOpts = { grid: true, newsMarks: true, positions: true }
  private news: NewsMark[] = []
  /**
   * Screen boxes for the news labels drawn this frame, for hit testing.
   *
   * Rebuilt by drawNewsMarks rather than recomputed on mouse move: the label
   * positions already account for the 34px collision thinning, so re-deriving
   * them would have to repeat that logic and could disagree with what is
   * actually painted.
   */
  private newsHits: { x0: number; x1: number; y0: number; y1: number; e: NewsMark }[] = []
  /** Only fire the hover callback when the label under the cursor CHANGES. */
  private lastNewsHit: NewsMark | null = null
  private positions: any[] = []

  private mtfLines: MtfTrendline[] = []
  /** Source timeframes to draw. Exactly these - an empty list draws none. */
  private mtfSources: string[] = []
  private overlays: Overlays
  private theme: ChartTheme = DARK_THEME
  private digits = 2
  /**
   * What a price distance is worth, and in what.
   *
   * Null until the quote carrying the contract facts arrives - the label then
   * shows the pip count alone rather than inventing a number. A wrong money
   * figure on a trading chart is worse than no money figure.
   */
  private money: MoneyModel | null = null
  /** Bar length in ms, for the candle countdown. 0 = no countdown. */
  private tfMs = 0
  /** 1 Hz repaint while a countdown is showing. */
  private clock: number | null = null

  private view: Viewport = { start: 0, span: 180 }
  private width = 0
  private height = 0

  /** main price pane rect, in CSS pixels */
  private pane = { x: 0, y: 0, w: 0, h: 0 }
  private subPanes: { key: string; y: number; h: number }[] = []

  private priceMin = 0
  private priceMax = 1
  private hover: HoverInfo = null

  /**
   * Price scaling. `auto` fits the visible bars, which is right until the
   * moment you want to look at something specific - then it fights you by
   * re-fitting on every tick. Any vertical drag or price-axis drag switches to
   * manual; double-clicking the axis hands it back.
   */
  private priceAuto = true
  private priceCentre = 0
  private priceSpan = 1

  private dragging = false
  private dragMode: 'pan' | 'scale' = 'pan'
  private dragStartX = 0
  private dragStartY = 0
  private dragStartView = 0
  private dragStartCentre = 0
  private dragStartSpan = 1
  private rafBase = 0
  private rafTop = 0

  /**
   * Empty bar-widths between the last candle and the price axis.
   *
   * Wide enough that a projected target box or a signal rail has somewhere
   * to be drawn without colliding with the axis labels, and that the live
   * candle keeps clear air around it rather than sitting under the price tag
   * and its countdown.
   */
  private rightPadBars = 20

  /** Set while a history request is in flight, so we ask once, not every frame. */
  private loadingHistory = false

  onHover: ((h: HoverInfo) => void) | null = null
  onNewsHover: ((h: NewsHover) => void) | null = null
  onViewChange: ((v: Viewport) => void) | null = null
  /** Fired when the view approaches the oldest bar we hold. */
  onNeedHistory: (() => void) | null = null

  constructor(base: HTMLCanvasElement, top: HTMLCanvasElement, overlays: Overlays) {
    this.base = base
    this.top = top
    this.bctx = base.getContext('2d')!
    this.tctx = top.getContext('2d')!
    this.overlays = overlays
    this.attach()
  }

  // ------------------------------------------------------------------ //
  // public API                                                         //
  // ------------------------------------------------------------------ //
  setBars(bars: Bar[], keepView = true) {
    const wasAtRight = this.isAtRightEdge()
    const prevLen = this.bars.length
    const prevFirstT = prevLen ? this.bars[0].t : 0
    this.bars = bars

    if (!keepView || prevLen === 0) {
      const span = Math.min(DEFAULT_SPAN, bars.length || DEFAULT_SPAN)
      this.view = { start: Math.max(0, bars.length - span + this.rightPadBars), span }
    } else if (bars.length > prevLen && prevFirstT && bars[0].t < prevFirstT) {
      // History was PREPENDED. Every existing index shifted right by however
      // many bars arrived, so the viewport has to shift with them or the chart
      // appears to teleport backwards the instant older data loads.
      let added = 0
      while (added < bars.length && bars[added].t < prevFirstT) added++
      this.view.start += added
      this.loadingHistory = false
    } else if (wasAtRight) {
      // Follow the live edge only if the user was already there; otherwise
      // their scroll position is respected.
      this.view.start = Math.max(0, bars.length - this.view.span + this.rightPadBars)
    }
    this.recomputeSnapOffset()
    this.scheduleBase()
  }

  setSnapshot(s: Snapshot | null) {
    this.snap = s
    if (s && typeof (s as any).digits === 'number') this.digits = (s as any).digits
    this.recomputeSnapOffset()
    this.scheduleBase()
  }

  /** chart index for an exact bar timestamp, or -1. */
  private barIndexAt(t: number): number {
    let lo = 0, hi = this.bars.length - 1
    while (lo <= hi) {
      const mid = (lo + hi) >> 1
      const v = this.bars[mid].t
      if (v === t) return mid
      if (v < t) lo = mid + 1; else hi = mid - 1
    }
    return -1
  }

  /**
   * Re-derive the snapshot-to-chart index offset.
   *
   * Uses the LAST swing rather than the first: the tail of both arrays is the
   * same live data, while the head of the chart's array is whatever history
   * has been scrolled into so far.
   */
  private recomputeSnapOffset() {
    const sw = this.snap?.swings
    if (!sw?.length || !this.bars.length) { this.snapOffset = 0; return }
    for (let k = sw.length - 1; k >= 0; k--) {
      const at = this.barIndexAt(sw[k].t)
      if (at >= 0) { this.snapOffset = at - sw[k].idx; return }
    }
    // No swing timestamp is in the chart's range: leave the offset alone
    // rather than snapping overlays to a guess.
  }

  /** x pixel for a SNAPSHOT-space bar index. */
  private snapX(i: number): number {
    return this.xOf(i + this.snapOffset)
  }

  setSignal(sig: Signal | null) {
    this.signal = sig
    this.scheduleBase()
  }

  setLayout(l: LayoutOpts) {
    this.layoutOpts = l
    this.scheduleBase()
  }

  setNews(n: NewsMark[]) {
    this.news = n ?? []
    this.scheduleBase()
  }

  /** Open positions for THIS chart's symbol; the caller filters. */
  setPositions(p: any[]) {
    this.positions = p ?? []
    this.scheduleBase()
  }

  setMtfTrendlines(lines: MtfTrendline[]) {
    this.mtfLines = lines ?? []
    this.scheduleBase()
  }

  /**
   * Which source timeframes to draw.
   *
   * Strict: the list is the whole truth, and an empty one draws nothing. An
   * earlier version treated empty as "all", which meant switching off the last
   * remaining source turned every source back on.
   */
  setMtfSources(tfs: string[]) {
    this.mtfSources = tfs ?? []
    this.scheduleBase()
  }

  setOverlays(o: Overlays) {
    this.overlays = o
    this.scheduleBase()
  }

  /**
   * Where the plot ends and the first indicator pane begins, in DEVICE pixels
   * of the current canvas.
   *
   * The export writes its two strap lines in the gap above the sub-panes, and
   * it draws onto the raw bitmap, so it needs device pixels rather than the
   * CSS units the layout is kept in. `subTop` is null when no indicator pane
   * is on, and the caller falls back to the bottom of the chart.
   */
  exportGeometry(): { plotW: number; subTop: number | null; bottom: number } {
    return {
      plotW: Math.round(this.pane.w * this.dpr),
      subTop: this.subPanes.length
        ? Math.round((this.subPanes[0].y - PANE_GAP) * this.dpr) : null,
      bottom: Math.round((this.height - TIME_AXIS_H) * this.dpr),
    }
  }

  /** Swap the canvas palette to match the UI theme. */
  setTheme(name: string) {
    this.theme = CHART_THEMES[name] ?? CHART_THEMES.glossy
    this.scheduleBase()
    this.scheduleTop()
  }

  /**
   * Paint the base layer once in another palette, hand it to `fn`, then put
   * the original back.
   *
   * Synchronous on purpose. Both draws and the caller's copy happen inside a
   * single task, so the browser never composites the intermediate frame and
   * the user sees no flash of a light chart. Restoring in a `finally` means a
   * throwing caller cannot leave the chart stuck in the wrong palette.
   */
  withTheme(name: string, fn: (base: HTMLCanvasElement) => void,
            size?: { w: number; h: number }): void {
    const prev = this.theme
    const next = name === 'paper' ? PAPER_THEME : CHART_THEMES[name]
    if (!next) { fn(this.base); return }
    const pw = this.width
    const ph = this.height
    this.theme = next
    try {
      // An export wants a different shape from the on-screen pane, which is
      // wide and short. Re-drawing at the export size gives real pixels
      // instead of an upscaled screen grab - the difference between a crisp
      // sheet and a blurry one.
      if (size && size.w > 0 && size.h > 0) this.resize(size.w, size.h)
      this.drawBase()
      fn(this.base)
    } finally {
      this.theme = prev
      if (size && (pw !== this.width || ph !== this.height)) this.resize(pw, ph)
      this.drawBase()
    }
  }

  /**
   * Tell the engine the bar length so it can count the forming candle down.
   *
   * Bars arrive already corrected to UTC, and a bar closes at its open time
   * plus the bar length whatever the broker's clock is - so the countdown is
   * `t + tfMs - now` with no timezone arithmetic, correct on 4h and daily bars
   * where a UTC+3 broker session does not line up with UTC boundaries.
   */
  setTimeframe(ms: number) {
    this.tfMs = ms > 0 ? ms : 0
    if (this.tfMs && this.clock == null) {
      this.clock = window.setInterval(() => {
        if (this.bars.length) this.scheduleBase()
      }, 1000)
    } else if (!this.tfMs && this.clock != null) {
      clearInterval(this.clock)
      this.clock = null
    }
    this.scheduleBase()
  }

  setMoney(m: MoneyModel | null) {
    this.money = m
    this.scheduleBase()
  }

  setDigits(d: number) {
    this.digits = d
    this.scheduleBase()
  }

  resize(w: number, h: number) {
    // Crisp text needs the canvas to map ONE-TO-ONE onto screen pixels. Three
    // things used to break that, each of which blurred every label:
    //   - the pane's width is fractional (873.6 CSS px); flooring width x
    //     ratio gave a backing store the browser then resampled to fit
    //   - the ratio was capped at 2, so a 4K screen at 250-300% was upscaled
    //   - Windows scaling of 125% / 150% makes the ratio 1.25 / 1.5
    // So: whole CSS pixels, a backing store rounded from them, and a CSS size
    // and transform derived back from THAT store - exact by construction.
    const ratio = Math.min(Math.max(window.devicePixelRatio || 1, 1), 3)
    const cw = Math.max(1, Math.floor(w))
    const ch = Math.max(1, Math.floor(h))
    const bw = Math.max(1, Math.round(cw * ratio))
    const bh = Math.max(1, Math.round(ch * ratio))
    this.dpr = bw / cw
    this.width = cw
    this.height = ch
    for (const c of [this.base, this.top]) {
      c.width = bw
      c.height = bh
      c.style.width = `${cw}px`
      c.style.height = `${ch}px`
    }
    this.bctx.setTransform(bw / cw, 0, 0, bh / ch, 0, 0)
    this.tctx.setTransform(bw / cw, 0, 0, bh / ch, 0, 0)
    this.layout()
    this.scheduleBase()
  }

  getViewport(): Viewport { return { ...this.view } }

  /** Hand price scaling back to the chart. */
  resetPriceScale() {
    this.priceAuto = true
    this.scheduleBase()
  }

  isPriceAuto(): boolean { return this.priceAuto }

  /** Called by the host once older bars have been merged in. */
  historyLoaded() { this.loadingHistory = false }

  /**
   * Put the chart back the way it started: default span, latest bars, and
   * price scaling back under automatic control.
   *
   * One button rather than three, because after a session of dragging the
   * thing you want is rarely "zoom out one notch" - it is "undo all of that".
   */
  resetChart() {
    this.view.span = Math.min(DEFAULT_SPAN, this.bars.length || DEFAULT_SPAN)
    this.view.start = Math.max(0, this.bars.length - this.view.span + this.rightPadBars)
    this.priceAuto = true
    this.clampView()
    this.scheduleBase()
    this.onViewChange?.(this.getViewport())
  }

  scrollToEnd() {
    // Keep the same right-hand margin the clamp enforces, otherwise the one
    // button whose whole job is "go to the live candle" is also the one that
    // parks it against the price axis.
    this.view.start = Math.max(0, this.bars.length - this.view.span + this.rightPadBars)
    this.clampView()
    this.scheduleBase()
    this.onViewChange?.(this.getViewport())
  }

  zoom(factor: number, anchorBar?: number) {
    const anchor = anchorBar ?? this.view.start + this.view.span / 2
    const span = Math.max(30, Math.min(1200, Math.round(this.view.span * factor)))
    const ratio = (anchor - this.view.start) / this.view.span
    this.view.span = span
    this.view.start = Math.round(anchor - ratio * span)
    this.clampView()
    this.scheduleBase()
    this.onViewChange?.(this.getViewport())
  }

  destroy() {
    cancelAnimationFrame(this.rafBase)
    cancelAnimationFrame(this.rafTop)
    if (this.clock != null) { clearInterval(this.clock); this.clock = null }
    this.detach()
  }

  // ------------------------------------------------------------------ //
  // geometry                                                           //
  // ------------------------------------------------------------------ //
  private layout() {
    const subs: { key: string; weight: number }[] = []
    if (this.overlays.volume) subs.push({ key: 'volume', weight: 0.10 })
    if (this.overlays.rsi) subs.push({ key: 'rsi', weight: 0.16 })
    if (this.overlays.macd) subs.push({ key: 'macd', weight: 0.16 })

    const usable = this.height - TIME_AXIS_H
    const subTotal = subs.reduce((a, b) => a + b.weight, 0)
    const mainH = usable * (1 - subTotal) - PANE_GAP * subs.length

    this.pane = { x: 0, y: 0, w: this.width - PRICE_AXIS_W, h: Math.max(60, mainH) }
    let y = this.pane.h + PANE_GAP
    this.subPanes = subs.map((s) => {
      const h = usable * s.weight
      const rect = { key: s.key, y, h }
      y += h + PANE_GAP
      return rect
    })
  }

  private clampView() {
    const n = this.bars.length
    // The right limit keeps a fixed gap of empty space after the last bar, so
    // the live candle never sits against the price axis with nowhere to move.
    const maxStart = Math.max(0, n - this.view.span + this.rightPadBars)
    // The left limit stops one screen before the oldest bar, so there is
    // always somewhere to drag to while more history loads.
    const minStart = -Math.floor(this.view.span * 0.05)
    this.view.start = Math.max(minStart, Math.min(maxStart, this.view.start))

    // Ask for more history while there is still a screenful of runway left,
    // not at the instant we run out - a fetch takes a moment and the drag
    // should not stall against a wall.
    if (!this.loadingHistory && this.onNeedHistory &&
        this.view.start < this.view.span * 1.5) {
      this.loadingHistory = true
      this.onNeedHistory()
    }
  }

  private isAtRightEdge(): boolean {
    if (!this.bars.length) return true
    return this.view.start + this.view.span >= this.bars.length + this.rightPadBars - 2
  }

  private barW(): number {
    return this.pane.w / this.view.span
  }

  /** bar index -> x pixel (centre of the candle) */
  private xOf(i: number): number {
    return (i - this.view.start + 0.5) * this.barW()
  }

  /** x pixel -> fractional bar index */
  private barAt(x: number): number {
    return this.view.start + x / this.barW() - 0.5
  }

  /** price -> y pixel within a pane */
  private yOf(p: number, top = this.pane.y, h = this.pane.h): number {
    const span = this.priceMax - this.priceMin || 1
    return top + h - ((p - this.priceMin) / span) * h
  }

  private priceAt(y: number): number {
    const span = this.priceMax - this.priceMin || 1
    return this.priceMin + ((this.pane.y + this.pane.h - y) / this.pane.h) * span
  }

  /** timestamp -> x pixel, by locating the bar. Overlays are time-anchored. */
  private xOfTime(t: number): number {
    const n = this.bars.length
    if (!n) return 0
    // Binary search on the bar timestamps.
    let lo = 0, hi = n - 1
    if (t <= this.bars[0].t) return this.xOf(0)
    if (t >= this.bars[n - 1].t) {
      // Extrapolate beyond the last bar using the bar interval, so a projected
      // target box can extend into empty space on the right.
      const step = n > 1 ? this.bars[n - 1].t - this.bars[n - 2].t : 60000
      return this.xOf(n - 1 + (t - this.bars[n - 1].t) / Math.max(step, 1))
    }
    while (lo < hi - 1) {
      const mid = (lo + hi) >> 1
      if (this.bars[mid].t <= t) lo = mid; else hi = mid
    }
    const span = this.bars[hi].t - this.bars[lo].t || 1
    return this.xOf(lo + (t - this.bars[lo].t) / span)
  }

  private computeScale() {
    const from = Math.max(0, Math.floor(this.view.start))
    const to = Math.min(this.bars.length, Math.ceil(this.view.start + this.view.span))
    let lo = Infinity, hi = -Infinity
    for (let i = from; i < to; i++) {
      const b = this.bars[i]
      if (b.l < lo) lo = b.l
      if (b.h > hi) hi = b.h
    }
    if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1 }

    // Let a live signal's stop and targets pull the scale, otherwise the levels
    // that matter most sit off-screen exactly when you need them.
    if (this.signal && this.overlays.signal) {
      for (const p of [this.signal.stop, this.signal.tp1, this.signal.tp2, this.signal.entry]) {
        if (Number.isFinite(p)) { lo = Math.min(lo, p); hi = Math.max(hi, p) }
      }
    }
    const pad = (hi - lo) * 0.08 || 1

    if (this.priceAuto) {
      this.priceMin = lo - pad
      this.priceMax = hi + pad
      // Keep the manual values shadowing the automatic ones, so taking manual
      // control starts exactly where the eye already is rather than jumping.
      this.priceCentre = (this.priceMin + this.priceMax) / 2
      this.priceSpan = this.priceMax - this.priceMin
    } else {
      this.priceSpan = Math.max(this.priceSpan, 1e-9)
      this.priceMin = this.priceCentre - this.priceSpan / 2
      this.priceMax = this.priceCentre + this.priceSpan / 2
    }
  }

  // ------------------------------------------------------------------ //
  // scheduling                                                         //
  // ------------------------------------------------------------------ //
  private scheduleBase() {
    cancelAnimationFrame(this.rafBase)
    this.rafBase = requestAnimationFrame(() => this.drawBase())
  }
  private scheduleTop() {
    cancelAnimationFrame(this.rafTop)
    this.rafTop = requestAnimationFrame(() => this.drawTop())
  }

  // ------------------------------------------------------------------ //
  // base layer                                                         //
  // ------------------------------------------------------------------ //
  private drawBase() {
    const ctx = this.bctx
    const { width: W, height: H } = this
    ctx.clearRect(0, 0, W, H)
    ctx.fillStyle = this.theme.bg
    ctx.fillRect(0, 0, W, H)
    if (!this.bars.length || this.pane.w <= 0) { this.drawEmpty(); return }

    this.layout()
    this.clampView()
    this.computeScale()

    // Grid first: it is background, and anything drawn over it should win.
    if (this.layoutOpts.grid) this.drawGrid()
    // Always called. The toggle governs the DASHED LINES, not the labels -
    // see drawNewsMarks.
    this.drawNewsMarks()

    if (this.overlays.regimeBands) this.drawRegimeBand()

    if (this.overlays.channels) this.drawChannels()
    // Areas sit UNDER everything structural. They are the widest marks on
    // the chart, and a band drawn over a trendline hides the line.
    if (this.overlays.zones) this.drawAreas(this.snap?.zones, false)
    if (this.overlays.fvg) this.drawAreas(this.snap?.fvg, true)
    if (this.overlays.fib) this.drawFib()
    if (this.overlays.patterns) this.drawPatterns()
    if (this.overlays.levels) this.drawLevels()
    if (this.overlays.liquidity) this.drawLiquidity()
    if (this.overlays.mtfTrendlines) this.drawMtfTrendlines()
    if (this.overlays.trendlines) this.drawTrendlines()
    if (this.overlays.zigzag) this.drawZigZag()

    this.drawCandles()

    if (this.overlays.swings) this.drawSwings()
    if (this.overlays.structure) this.drawStructureBreaks()
    if (this.overlays.events) this.drawEvents()
    if (this.overlays.signal) this.drawSignal()
    if (this.layoutOpts.positions) this.drawPositions()

    this.drawSubPanes()
    this.drawPriceAxis()
    this.drawTimeAxis()
    this.drawLastPrice()
  }

  /**
   * An empty chart paints nothing but the background.
   *
   * It used to write "no data", which is the one thing that is usually NOT
   * true: MetaTrader downloads history on demand, so an empty series almost
   * always means "still arriving". ChartPane renders the real explanation as
   * an HTML overlay, where it can carry a spinner and a retry.
   */
  private drawEmpty() { /* background only */ }

  /**
   * Gridlines on the tick positions the axes already label.
   *
   * Derived from priceTicks()/timeTicks() rather than an independent spacing,
   * so every line has a number against it. A grid whose lines do not line up
   * with the axis labels is worse than none - it invites reading a level off a
   * line that means nothing.
   */
  private drawGrid() {
    const ctx = this.bctx
    ctx.save()
    ctx.lineWidth = 1

    ctx.strokeStyle = this.theme.grid
    ctx.beginPath()
    for (const { p } of this.priceTicks()) {
      const y = Math.round(this.yOf(p)) + 0.5
      if (y < this.pane.y || y > this.pane.y + this.pane.h) continue
      ctx.moveTo(0, y); ctx.lineTo(this.pane.w, y)
    }
    ctx.stroke()

    // Verticals run the FULL height, through the sub-panes, so a spike in RSI
    // or MACD can be tied back to the candle that caused it.
    const bottom = this.height - TIME_AXIS_H
    for (const t of this.timeTicks()) {
      const x = Math.round(this.xOf(t.i)) + 0.5
      if (x < 0 || x > this.pane.w) continue
      ctx.strokeStyle = t.major ? this.theme.gridStrong : this.theme.grid
      ctx.setLineDash(t.major ? [] : [2, 4])
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, bottom); ctx.stroke()
    }
    ctx.setLineDash([])
    ctx.restore()
  }

  /**
   * Vertical marks for macro releases, labelled at the foot of the chart.
   *
   * Only high-impact events are drawn. The calendar carries dozens of low
   * items a day; marking all of them would paper the chart over and bury the
   * three that actually move gold.
   *
   * The `newsMarks` toggle hides the full-height dashed LINES while leaving
   * the labels along the foot in place. Those lines cut through every pane and
   * are the noisy half; the labels cost almost nothing and still answer "what
   * happened here", so switching the marks off should not also blind you to
   * the release sitting under the candle you are looking at.
   */
  private drawNewsMarks() {
    if (!this.news.length || !this.bars.length) return
    const ctx = this.bctx
    const bottom = this.height - TIME_AXIS_H
    const firstT = this.bars[0].t
    const lastT = this.bars[this.bars.length - 1].t

    ctx.save()
    ctx.font = `600 9.5px ${UI_FONT}`
    ctx.textBaseline = 'bottom'
    const placed: number[] = []
    this.newsHits = []

    for (const e of this.news) {
      if (e.ts < firstT || e.ts > lastT + 86_400_000) continue
      const x = this.xOfTime(e.ts)
      if (x < 0 || x > this.pane.w) continue
      // Labels collide badly around a release cluster; keep the first and drop
      // anything within 34px of it.
      if (placed.some((p) => Math.abs(p - x) < 34)) continue
      placed.push(x)

      // Amber whether it has happened or not. A release that already landed
      // still explains the candle sitting on it, so greying it out hid the
      // very thing the mark exists to point at; only the line weight
      // separates what is coming from what has been.
      const future = e.ts > Date.now()
      const col = this.theme.warn
      if (this.layoutOpts.newsMarks) {
        ctx.globalAlpha = future ? 0.85 : 0.5
        ctx.strokeStyle = col
        ctx.lineWidth = 1
        ctx.setLineDash([3, 4])
        ctx.beginPath()
        ctx.moveTo(Math.round(x) + 0.5, 0)
        ctx.lineTo(Math.round(x) + 0.5, bottom)
        ctx.stroke()
        ctx.setLineDash([])
      } else {
        // Without its line a label floats. A short stub keeps it anchored to
        // the bar it belongs to.
        ctx.globalAlpha = 0.55
        ctx.strokeStyle = col
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.moveTo(Math.round(x) + 0.5, bottom - 11)
        ctx.lineTo(Math.round(x) + 0.5, bottom)
        ctx.stroke()
      }

      ctx.globalAlpha = 1
      ctx.fillStyle = col
      ctx.textAlign = 'left'
      const tag = shortEvent(e)
      const lx = Math.round(x) + 3
      ctx.fillText(tag, lx, bottom - 3)
      // A few pixels of slack either side: the labels are 8px tall and asking
      // for pixel-exact aim on one is a poor way to read a chart.
      this.newsHits.push({
        x0: lx - 3, x1: lx + ctx.measureText(tag).width + 4,
        y0: bottom - 15, y1: bottom + 2, e,
      })
    }
    ctx.restore()
  }

  /** The news label under (x, y), if any. */
  private newsAt(x: number, y: number): NewsMark | null {
    for (const h of this.newsHits) {
      if (x >= h.x0 && x <= h.x1 && y >= h.y0 && y <= h.y1) return h.e
    }
    return null
  }

  /**
   * Open broker positions, as entry rails across the chart.
   *
   * Drawn from the account rather than from any signal: this is where money
   * actually is, which is a different question from where the engine thinks it
   * should be.
   */
  private drawPositions() {
    if (!this.positions.length) return
    const ctx = this.bctx
    ctx.save()
    ctx.beginPath(); ctx.rect(0, this.pane.y, this.pane.w, this.pane.h); ctx.clip()
    ctx.font = `600 10.5px ${UI_FONT}`
    ctx.textBaseline = 'middle'

    const placed: number[] = []
    for (const p of this.positions) {
      const entry = Number(p.price_open ?? p.open)
      if (!Number.isFinite(entry)) continue
      const y = this.yOf(entry)
      if (y < this.pane.y - 2 || y > this.pane.y + this.pane.h + 2) continue
      const buy = (p.side ?? p.type) === 'buy' || p.type === 0
      const col = buy ? this.theme.up : this.theme.down
      // A book of 21 positions in one symbol stacks many rails within a few
      // pixels; one line per price band keeps it readable.
      const dup = placed.some((q) => Math.abs(q - y) < 3)
      placed.push(y)

      ctx.globalAlpha = dup ? 0.25 : 0.6
      ctx.strokeStyle = col
      ctx.lineWidth = 1
      ctx.setLineDash([1, 3])
      ctx.beginPath()
      ctx.moveTo(0, Math.round(y) + 0.5)
      ctx.lineTo(this.pane.w, Math.round(y) + 0.5)
      ctx.stroke()
      ctx.setLineDash([])
      if (dup) continue

      const pl = Number(p.profit)
      // BUY/SELL, matching the words the terminal and the order ticket use.
      // LONG/SHORT is the same idea in a different dialect, and having the
      // chart speak one while every other panel speaks the other is friction
      // for no gain.
      const tag = `${buy ? 'BUY' : 'SELL'} ${Number(p.volume).toFixed(2)}`
        + (Number.isFinite(pl) ? `  ${pl >= 0 ? '+' : ''}${pl.toFixed(2)}` : '')
      ctx.textAlign = 'left'
      const w = ctx.measureText(tag).width + 8
      ctx.globalAlpha = 0.9
      ctx.fillStyle = `rgba(${this.theme.plateRgb},0.82)`
      ctx.fillRect(2, y - 6, w, 12)
      ctx.globalAlpha = 1
      ctx.fillStyle = col
      ctx.fillText(tag, 6, y)
    }
    ctx.restore()
  }

  // --- grid ---------------------------------------------------------- //
  /**
   * The price ladder, with the round levels marked.
   *
   * `major` is the same idea the time axis already uses: a tick that lands on
   * a rounder multiple than its neighbours. On gold that is 4,300 and 4,350
   * against 4,310 and 4,320 - the numbers a trader actually quotes, and the
   * ones worth being able to find without reading the whole column.
   *
   * The major step is chosen to land one every few labels whatever the zoom:
   * with a 1x step the fifth tick is round, with 2x the fifth, and with 5x
   * every second one is already a .00 or .000.
   */
  private priceTicks(): { p: number; major: boolean }[] {
    const span = this.priceMax - this.priceMin
    if (span <= 0) return []
    const target = Math.max(3, Math.floor(this.pane.h / 52))
    const raw = span / target
    const mag = Math.pow(10, Math.floor(Math.log10(raw)))
    const norm = raw / mag
    const mult = norm >= 5 ? 5 : norm >= 2 ? 2 : 1
    const step = mult * mag
    const majorStep = (mult === 1 ? 5 : 10) * mag
    // Half a step of slack. The ladder is built by repeated addition, so a
    // tick that should be exactly 4350 arrives as 4349.999999999999 and an
    // equality test would call it minor. The tolerance is far smaller than
    // the gap between ticks, so it cannot promote the wrong one.
    const eps = step * 1e-6
    const out: { p: number; major: boolean }[] = []
    for (let p = Math.ceil(this.priceMin / step) * step; p <= this.priceMax; p += step) {
      const r = Math.abs(p / majorStep - Math.round(p / majorStep)) * majorStep
      out.push({ p, major: r <= eps })
    }
    return out
  }

  private timeTicks(): { i: number; label: string; major: boolean }[] {
    const out: { i: number; label: string; major: boolean }[] = []
    const from = Math.max(0, Math.floor(this.view.start))
    const to = Math.min(this.bars.length, Math.ceil(this.view.start + this.view.span))
    if (to - from < 2) return out
    const minPx = 78
    const stepBars = Math.max(1, Math.ceil(minPx / this.barW()))
    let lastDay = -1
    for (let i = from; i < to; i += stepBars) {
      const d = new Date(this.bars[i].t)
      const day = d.getUTCDate()
      const major = day !== lastDay
      lastDay = day
      const label = major
        ? `${String(day).padStart(2, '0')} ${d.toLocaleString('en', { month: 'short', timeZone: 'UTC' })}`
        : `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
      out.push({ i, label, major })
    }
    return out
  }

    /** A slim regime ribbon along the very top, like the reference design. */
  private drawRegimeBand() {
    const s = this.snap
    if (!s?.regime) return
    const ctx = this.bctx
    const label = String(s.regime.label || '')
    const colour = label.includes('UP') ? this.theme.up
      : label.includes('DOWN') ? this.theme.down
      : label === 'RANGE' ? this.theme.info
      : label === 'SQUEEZE' ? this.theme.accent
      : this.theme.warn
    ctx.save()
    ctx.fillStyle = colour
    ctx.globalAlpha = 0.55
    ctx.fillRect(0, 0, this.pane.w, 2)
    ctx.globalAlpha = 1
    ctx.font = `600 9px ${UI_FONT}`
    ctx.fillStyle = colour
    ctx.textAlign = 'right'
    ctx.textBaseline = 'top'
    ctx.fillText(`${label}  ${s.regime.confidence ?? ''}`, this.pane.w - 8, 7)
    ctx.restore()
  }

  // --- candles -------------------------------------------------------- //
  private drawCandles() {
    const ctx = this.bctx
    const from = Math.max(0, Math.floor(this.view.start))
    const to = Math.min(this.bars.length, Math.ceil(this.view.start + this.view.span))
    const bw = this.barW()
    const body = Math.max(1, Math.min(14, bw * 0.68))
    const thin = bw < 2.2

    ctx.save()
    ctx.beginPath()
    ctx.rect(0, 0, this.pane.w, this.pane.h)
    ctx.clip()

    for (let i = from; i < to; i++) {
      const b = this.bars[i]
      const up = b.c >= b.o
      const col = up ? this.theme.up : this.theme.down
      const x = this.xOf(i)
      const yO = this.yOf(b.o), yC = this.yOf(b.c)
      const yH = this.yOf(b.h), yL = this.yOf(b.l)

      if (thin) {
        // Below ~2px per bar, bodies are meaningless - draw the range only.
        ctx.strokeStyle = col
        ctx.lineWidth = Math.max(0.6, bw * 0.7)
        ctx.beginPath(); ctx.moveTo(x, yH); ctx.lineTo(x, yL); ctx.stroke()
        continue
      }

      ctx.strokeStyle = col
      ctx.lineWidth = 1
      const px = Math.round(x) + 0.5
      ctx.beginPath(); ctx.moveTo(px, yH); ctx.lineTo(px, yL); ctx.stroke()

      const top = Math.min(yO, yC)
      const hgt = Math.max(1, Math.abs(yC - yO))
      const bx = Math.round(x - body / 2)
      const bwid = Math.round(body)
      const bh = Math.round(hgt)

      if (this.theme.gloss && bwid >= 3) {
        // Lit from the left, like every raised surface in the terminal. The
        // gradient is built once per width in candle-local coordinates and
        // reused for every bar by translating the canvas onto it - a fresh
        // CanvasGradient for each of three hundred candles, every frame, is
        // allocation the raster loop does not need.
        ctx.save()
        ctx.translate(bx, 0)
        ctx.fillStyle = this.bodyGloss(up, bwid)
        ctx.fillRect(0, Math.round(top), bwid, bh)
        // A single bright pixel along the top edge. This is what separates a
        // lacquered body from a body with a gradient on it: the eye reads the
        // hard highlight as a surface catching the light, and the gradient
        // below it as the curve falling away.
        if (bh >= 3) {
          ctx.fillStyle = 'rgba(255,255,255,0.30)'
          ctx.fillRect(0, Math.round(top), bwid, 1)
        }
        ctx.restore()
        continue
      }

      ctx.fillStyle = col
      ctx.fillRect(bx, Math.round(top), bwid, bh)
    }
    ctx.restore()
  }

  /**
   * The lit face of a candle body, in local coordinates 0..w.
   *
   * Cached on width because that is the only thing it depends on - the caller
   * translates the canvas rather than rebuilding the gradient at each bar's x.
   * Two gradients are held, up and down, and both are dropped the moment the
   * width or the palette changes.
   */
  private glossCache: { w: number; theme: ChartTheme;
                        up: CanvasGradient; down: CanvasGradient } | null = null

  private bodyGloss(up: boolean, w: number): CanvasGradient {
    const c = this.glossCache
    if (c && c.w === w && c.theme === this.theme) return up ? c.up : c.down
    const make = (col: string) => {
      const g = this.bctx.createLinearGradient(0, 0, w, 0)
      g.addColorStop(0, shade(col, 0.42))
      g.addColorStop(0.30, shade(col, 0.10))
      g.addColorStop(0.62, col)
      g.addColorStop(1, shade(col, -0.30))
      return g
    }
    const next = { w, theme: this.theme, up: make(this.theme.up),
                   down: make(this.theme.down) }
    this.glossCache = next
    return up ? next.up : next.down
  }

  // --- levels --------------------------------------------------------- //
  private drawLevels() {
    const s = this.snap
    if (!s?.levels) return
    const ctx = this.bctx

    // Draw the strongest levels only, and label fewer still.
    //
    // The engine legitimately finds fourteen; rendering all fourteen with a
    // label each produced a wall of stacked "SUPPORT x2" text down the left
    // edge that buried the price action it was meant to annotate. The chart
    // shows the eight that matter and labels those whose label would not
    // collide with one already placed.
    const shown = [...s.levels]
      .filter((lv) => lv.score >= 50)
      .sort((a, b) => b.score - a.score)
      .slice(0, 8)

    const placed: number[] = []
    ctx.save()
    for (const lv of shown) {
      const y = this.yOf(lv.price)
      if (y < -20 || y > this.pane.h + 20) continue
      const strong = lv.score >= 70
      const col = lv.kind === 'resistance' ? this.theme.down
        : lv.kind === 'support' ? this.theme.up
        : this.theme.warn
      const alpha = Math.max(0.2, Math.min(0.85, lv.score / 100))

      // The band, not just the line - a level is an area. Except flips: they
      // are the most numerous level and several stack together, so their
      // shaded bands piled into a dark smear across the chart. A flip is
      // drawn as its line alone.
      if (lv.kind !== 'flip') {
        // 60% of the level's measured half-width: the full width read as a
        // slab across the chart rather than a band around a price.
        const bandPx = Math.max(1.5, 0.6 * Math.abs(this.yOf(lv.price - lv.width) - y))
        ctx.globalAlpha = alpha * 0.09
        ctx.fillStyle = col
        ctx.fillRect(0, y - bandPx, this.pane.w, bandPx * 2)
      }

      ctx.globalAlpha = alpha * 0.85
      ctx.strokeStyle = col
      ctx.lineWidth = strong ? 1.3 : 1
      ctx.setLineDash(lv.broken ? [3, 5] : [])
      const x0 = Math.max(0, this.xOfTime(lv.first_t))
      ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(this.pane.w, y); ctx.stroke()
      ctx.setLineDash([])

      // Labels sit at the LEFT. The right-hand margin is where the live price
      // tag, the signal rails and the projected-timeframe tags all land, and
      // stacking level names into the same strip buried the one number that
      // has to stay readable. Skipped when one is already within 11px.
      if (placed.some((p) => Math.abs(p - y) < 11)) continue
      placed.push(y)
      ctx.globalAlpha = 1
      ctx.font = `700 8.5px ui-sans-serif, system-ui`
      const kind = lv.kind.charAt(0).toUpperCase() + lv.kind.slice(1)
      const tag = `${kind} ×${lv.touches}${lv.is_round ? ' ◆' : ''}`
      const tw = ctx.measureText(tag).width
      ctx.fillStyle = `rgba(${this.theme.plateRgb},0.8)`
      ctx.fillRect(2, y - 15, tw + 8, 14)
      ctx.fillStyle = col
      ctx.textAlign = 'left'
      ctx.textBaseline = 'middle'
      ctx.fillText(tag, 6, y - 8)
    }
    ctx.restore()
  }

  private drawLiquidity() {
    const s = this.snap
    if (!s?.liquidity) return
    const ctx = this.bctx
    ctx.save()
    for (const p of s.liquidity) {
      if (p.swept || p.pull < 25) continue
      const y = this.yOf(p.price)
      if (y < 0 || y > this.pane.h) continue
      const col = p.side === 'buyside' ? this.theme.up : this.theme.down
      ctx.globalAlpha = 0.5
      ctx.strokeStyle = col
      ctx.lineWidth = 1
      ctx.setLineDash([1, 3])
      ctx.beginPath(); ctx.moveTo(this.pane.w * 0.55, y); ctx.lineTo(this.pane.w, y); ctx.stroke()
      ctx.setLineDash([])
      ctx.globalAlpha = 0.9
      ctx.font = `9px ui-sans-serif, system-ui`
      ctx.fillStyle = col
      ctx.textAlign = 'right'
      ctx.textBaseline = 'middle'
      ctx.fillText(`≋ ${p.label}`, this.pane.w - 4, y - 5)
    }
    ctx.restore()
  }

  /**
   * Price AREAS: fair value gaps and supply/demand bases.
   *
   * One routine for both, because they are the same shape and are read the
   * same way - a band that starts where it formed and runs to the right edge
   * until price eats it. Only the colour and the tag differ.
   *
   * Two details that matter more than they look:
   *
   * FADED BY FILL. A zone price has half traded through is half the zone it
   * was, and drawing it at full strength alongside an untouched one says they
   * are equally interesting. Opacity carries `filled` so the chart ranks them
   * without a number.
   *
   * SNAPSHOT SPACE. `idx` counts bars in the snapshot's series, which is not
   * this chart's series - scroll back far enough and the two diverge. snapX()
   * is the existing correction; using xOf() directly would slide every box.
   */
  private drawAreas(list: PriceArea[] | undefined, fvg: boolean) {
    if (!list?.length) return
    const ctx = this.bctx
    ctx.save()
    ctx.beginPath(); ctx.rect(0, 0, this.pane.w, this.pane.h); ctx.clip()
    ctx.textBaseline = 'middle'
    ctx.font = '9px ui-sans-serif, system-ui'

    for (const a of list) {
      const yHi = this.yOf(a.high)
      const yLo = this.yOf(a.low)
      if (yLo < -40 || yHi > this.pane.h + 40) continue
      const bullish = a.side === 'bullish' || a.side === 'demand'
      const col = bullish ? this.theme.up : this.theme.down
      const x0 = Math.max(0, this.snapX(a.to_idx ?? a.idx))
      const w = this.pane.w - x0
      if (w <= 0) continue
      const h = Math.max(1, yLo - yHi)

      // An untouched band is worth looking at; one mostly eaten is almost
      // gone. The floor keeps a nearly-filled zone faintly visible rather
      // than vanishing, because "it held once" is still information.
      const live = Math.max(0.12, 1 - a.filled)
      ctx.globalAlpha = (fvg ? 0.13 : 0.16) * live
      ctx.fillStyle = col
      ctx.fillRect(x0, yHi, w, h)

      ctx.globalAlpha = 0.55 * live + 0.2
      ctx.strokeStyle = col
      ctx.lineWidth = 1
      // The gap is bounded by two prices that were never traded between, so
      // its edges are real. A base is an area of interest, not a boundary -
      // dashed, so it is not read as a level.
      ctx.setLineDash(fvg ? [] : [4, 3])
      ctx.strokeRect(Math.round(x0) + 0.5, Math.round(yHi) + 0.5,
                     Math.round(w) - 1, Math.round(h))
      ctx.setLineDash([])

      // Label only where there is room for it, at the left edge where the
      // band begins. A tag on a 3px sliver is unreadable and covers the
      // candles either side of it.
      if (h >= 11) {
        // A TEST COUNT, in the same idiom the levels already use ("SUPPORT
        // x2"). The impulse that formed the zone is why it exists, but it is
        // one old number; how often price has come back says how well known
        // the band is now, which is what you are deciding against. "fresh"
        // rather than "x0", because a count of nothing reads as a mistake.
        const n = a.touches ?? 0
        const tag = fvg
          ? `FVG ${a.size_atr ?? ''}`
          : `${a.side === 'demand' ? 'DEMAND' : 'SUPPLY'} ${n ? `×${n}` : 'fresh'}`
        ctx.globalAlpha = 0.85 * live + 0.15
        ctx.fillStyle = col
        ctx.textAlign = 'left'
        ctx.fillText(tag.trim(), x0 + 4, yHi + h / 2)
      }
    }
    ctx.restore()
  }

  /**
   * Retracement of the current impulse leg.
   *
   * The engine already measures this - it is what the pullback playbook
   * reasons over - so the chart draws the SAME numbers rather than fitting
   * its own. A Fibonacci overlay that disagreed with the signal that cites
   * it would be worse than none.
   *
   * The 0.618-0.786 band is shaded because that is the one the playbook calls
   * the pocket; the rest are hairlines. Drawing seven equally weighted lines
   * would bury the only two that carry a rule.
   */
  private drawFib() {
    const f = this.snap?.fib
    if (!f?.valid || !f.levels) return
    const ctx = this.bctx
    const x0 = Math.max(0, this.xOfTime(f.from_t))
    const x1 = this.pane.w
    if (x1 - x0 <= 0) return

    const up = f.direction === 'up'
    const col = up ? this.theme.up : this.theme.down
    ctx.save()
    ctx.beginPath(); ctx.rect(0, 0, this.pane.w, this.pane.h); ctx.clip()

    const y618 = this.yOf(f.levels['0.618'])
    const y786 = this.yOf(f.levels['0.786'])
    if (Number.isFinite(y618) && Number.isFinite(y786)) {
      ctx.globalAlpha = f.in_pocket ? 0.16 : 0.08
      ctx.fillStyle = this.theme.warn
      ctx.fillRect(x0, Math.min(y618, y786), x1 - x0, Math.abs(y786 - y618))
    }

    ctx.font = '9px ui-sans-serif, system-ui'
    ctx.textAlign = 'left'
    ctx.textBaseline = 'middle'
    for (const [k, v] of Object.entries(f.levels as Record<string, number>)) {
      const y = this.yOf(v)
      if (y < 0 || y > this.pane.h) continue
      // 0 and 1 are the leg itself; the pocket edges carry the rule. The
      // rest are context and are drawn as such.
      const strong = k === '0.618' || k === '0.786' || k === '0.5'
      ctx.globalAlpha = strong ? 0.75 : 0.35
      ctx.strokeStyle = strong ? this.theme.warn : col
      ctx.lineWidth = 1
      ctx.setLineDash(strong ? [] : [2, 4])
      const py = Math.round(y) + 0.5
      ctx.beginPath(); ctx.moveTo(x0, py); ctx.lineTo(x1, py); ctx.stroke()
      ctx.setLineDash([])
      ctx.globalAlpha = strong ? 0.95 : 0.5
      ctx.fillStyle = strong ? this.theme.warn : col
      ctx.fillText(k, x0 + 4, y - 6)
    }
    ctx.restore()
  }

  /**
   * Higher-timeframe trendlines, drawn in TIME rather than bar index.
   *
   * Everything else on this chart is positioned by bar index, which is the
   * only sane coordinate within one timeframe and a meaningless one across
   * several. These come through as (anchor time, price) plus a slope per
   * millisecond, and xOfTime() maps that onto this chart's collapsed axis -
   * which also handles weekends, since a gap the lower timeframe does not
   * contain simply maps to the same x as the bar beside it.
   *
   * They are drawn thinner, dimmer and dashed-by-source so they read as
   * context rather than competing with the chart's own lines.
   */
  private drawMtfTrendlines() {
    if (!this.mtfLines.length || !this.bars.length) return
    const want = this.mtfSources
    const rows = this.mtfLines.filter((m) => want.includes(m.tf))
    if (!rows.length) return

    const ctx = this.bctx
    ctx.save()
    ctx.beginPath(); ctx.rect(0, 0, this.pane.w, this.pane.h); ctx.clip()

    // Right edge of the drawable area, in TIME. Extrapolated past the last bar
    // so a projected line crosses the empty right margin instead of stopping
    // dead at the live candle.
    const n = this.bars.length
    const lastT = this.bars[n - 1].t
    const step = n > 1 ? (lastT - this.bars[n - 2].t) || 60_000 : 60_000
    const edgeIdx = this.view.start + this.view.span
    const edgeT = lastT + Math.max(0, edgeIdx - (n - 1)) * step

    for (const m of rows) {
      const col = m.kind === 'support' ? this.theme.up : this.theme.down
      const x1 = this.xOfTime(m.t1)
      const x2 = this.xOfTime(edgeT)
      const y1 = this.yOf(m.y1)
      const y2 = this.yOf(m.y1 + m.slope_ms * (edgeT - m.t1))
      if (!Number.isFinite(y1) || !Number.isFinite(y2)) continue

      // A 1d line on a 5m chart is background; a 15m line is nearly local.
      // Weight by score but keep every projection quieter than a native one.
      ctx.globalAlpha = m.broken ? 0.18 : Math.max(0.22, Math.min(0.5, m.score / 200))
      ctx.strokeStyle = col
      ctx.lineWidth = 1
      ctx.setLineDash(m.broken ? [2, 5] : [7, 4])
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke()
      ctx.setLineDash([])

      // Label at the right, where the line meets the axis and the eye already
      // is. Without the source timeframe on it a projected line is just an
      // unexplained diagonal.
      if (y2 > this.pane.y - 6 && y2 < this.pane.y + this.pane.h + 6) {
        ctx.globalAlpha = m.broken ? 0.45 : 0.85
        ctx.font = `500 10px ${UI_FONT}`
        ctx.fillStyle = col
        ctx.textAlign = 'right'
        ctx.textBaseline = 'middle'
        ctx.fillText(m.tf.toUpperCase(), this.pane.w - 3, y2 - 5)
      }
    }
    ctx.restore()
  }

  // --- trendlines and channels ---------------------------------------- //
  private drawTrendlines() {
    const s = this.snap
    if (!s?.trendlines) return
    const ctx = this.bctx
    ctx.save()
    ctx.beginPath(); ctx.rect(0, 0, this.pane.w, this.pane.h); ctx.clip()
    const lines = [...s.trendlines].sort((a, b) => b.score - a.score).slice(0, 5)
    for (const tl of lines) {
      const col = tl.kind === 'support' ? this.theme.up : this.theme.down
      const x1 = this.snapX(tl.x1)
      // Project the line to the right edge of the pane. `slope` is per bar in
      // SNAPSHOT space, so the edge has to be expressed there too - mixing a
      // chart-space index into that multiply tilts every line once any
      // history has been loaded.
      const lastIdx = this.view.start + this.view.span
      const lastSnapIdx = lastIdx - this.snapOffset
      const y1 = this.yOf(tl.y1)
      const y2 = this.yOf(tl.y1 + tl.slope * (lastSnapIdx - tl.x1))
      const x2 = this.xOf(lastIdx)

      ctx.globalAlpha = tl.broken ? 0.3 : Math.max(0.35, tl.score / 100)
      ctx.strokeStyle = col
      ctx.lineWidth = tl.score >= 60 ? 1.5 : 1
      ctx.setLineDash(tl.broken ? [4, 4] : [])
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke()
      ctx.setLineDash([])

      // Mark the anchors the line was validated on.
      ctx.globalAlpha = 0.9
      ctx.fillStyle = col
      for (const ti of tl.touch_idx) {
        const tx = this.snapX(ti)
        if (tx < -10 || tx > this.pane.w + 10) continue
        const ty = this.yOf(tl.y1 + tl.slope * (ti - tl.x1))
        ctx.beginPath(); ctx.arc(tx, ty, 2.2, 0, Math.PI * 2); ctx.fill()
      }
    }
    ctx.restore()
  }

  private drawChannels() {
    const s = this.snap
    if (!s?.channels) return
    const ctx = this.bctx
    ctx.save()
    ctx.beginPath(); ctx.rect(0, 0, this.pane.w, this.pane.h); ctx.clip()
    for (const ch of s.channels) {
      const col = ch.kind === 'ascending' ? this.theme.up
        : ch.kind === 'descending' ? this.theme.down
        : this.theme.info
      const ux1 = this.snapX(ch.upper.x1), uy1 = this.yOf(ch.upper.y1)
      const ux2 = this.snapX(ch.upper.x2), uy2 = this.yOf(ch.upper.y2)
      const lx1 = this.snapX(ch.lower.x1), ly1 = this.yOf(ch.lower.y1)
      const lx2 = this.snapX(ch.lower.x2), ly2 = this.yOf(ch.lower.y2)

      ctx.globalAlpha = 0.07
      ctx.fillStyle = col
      ctx.beginPath()
      ctx.moveTo(ux1, uy1); ctx.lineTo(ux2, uy2)
      ctx.lineTo(lx2, ly2); ctx.lineTo(lx1, ly1)
      ctx.closePath(); ctx.fill()

      ctx.globalAlpha = 0.6
      ctx.strokeStyle = col
      ctx.lineWidth = 1.2
      ctx.beginPath(); ctx.moveTo(ux1, uy1); ctx.lineTo(ux2, uy2); ctx.stroke()
      ctx.beginPath(); ctx.moveTo(lx1, ly1); ctx.lineTo(lx2, ly2); ctx.stroke()

      ctx.globalAlpha = 1
      ctx.font = `700 9px ui-sans-serif, system-ui`
      ctx.fillStyle = col
      ctx.textAlign = 'left'
      ctx.textBaseline = 'top'
      ctx.fillText(
        `${ch.kind.toUpperCase()} CHANNEL  ${Math.round(ch.containment * 100)}%`,
        ux1 + 6, Math.min(uy1, uy2) + 4,
      )
    }
    ctx.restore()
  }

  // --- patterns: the Autochartist signature ---------------------------- //
  private drawPatterns() {
    const s = this.snap
    if (!s?.patterns) return
    const ctx = this.bctx
    ctx.save()
    ctx.beginPath(); ctx.rect(0, 0, this.pane.w, this.pane.h); ctx.clip()

    // Draw the least relevant first so the best pattern ends up on top, and cap
    // how many get the full treatment: six shaded envelopes plus six projected
    // target boxes overlapped into an unreadable wash of translucent colour.
    const ranked = [...s.patterns].sort((a, b) => b.relevance - a.relevance)
    const featured = new Set(ranked.slice(0, 2).map((p) => p.label + p.end_t))
    const ordered = [...s.patterns].sort((a, b) => a.relevance - b.relevance)
    for (const p of ordered) {
      const bull = p.direction === 'bullish'
      const col = p.direction === 'neutral' ? this.theme.info : bull ? this.theme.up : this.theme.down
      const pts = p.points.map((pt) => ({ x: this.xOfTime(pt.t), y: this.yOf(pt.price), role: pt.role }))
      if (pts.length < 2) continue
      const prominent = featured.has(p.label + p.end_t) && p.actionable

      // 1. the shaded envelope of the formation
      if (prominent && p.zone && (p.zone.price_top !== undefined || p.zone.upper_now !== undefined)) {
        const top = p.zone.price_top ?? p.zone.upper_now
        const bot = p.zone.price_bottom ?? p.zone.lower_now
        const t1 = p.zone.t1 ?? p.start_t
        const t2 = p.zone.t2 ?? p.end_t
        if (Number.isFinite(top) && Number.isFinite(bot)) {
          const x1 = this.xOfTime(t1), x2 = this.xOfTime(t2)
          ctx.globalAlpha = 0.09
          ctx.fillStyle = col
          ctx.fillRect(x1, this.yOf(top), x2 - x1, this.yOf(bot) - this.yOf(top))
        }
      }

      // 2. the polyline through the turning points
      ctx.globalAlpha = prominent ? 0.95 : 0.22
      ctx.strokeStyle = col
      ctx.lineWidth = prominent ? 1.8 : 1
      ctx.setLineDash(p.status === 'forming' ? [5, 3] : [])
      ctx.beginPath()
      pts.forEach((pt, i) => (i ? ctx.lineTo(pt.x, pt.y) : ctx.moveTo(pt.x, pt.y)))
      ctx.stroke()
      ctx.setLineDash([])

      // 3. the turning points themselves
      ctx.fillStyle = col
      for (const pt of pts) {
        ctx.beginPath(); ctx.arc(pt.x, pt.y, prominent ? 3 : 2, 0, Math.PI * 2); ctx.fill()
      }

      if (!prominent) { ctx.globalAlpha = 1; continue }

      // 4. break level and the projected target box
      const xEnd = this.xOfTime(p.end_t)
      const xProj = Math.min(this.pane.w, xEnd + this.barW() * 28)
      const yBreak = this.yOf(p.break_level)
      const yTarget = this.yOf(p.target)

      ctx.globalAlpha = 0.85
      ctx.strokeStyle = col
      ctx.lineWidth = 1
      ctx.setLineDash([4, 3])
      ctx.beginPath(); ctx.moveTo(pts[0].x, yBreak); ctx.lineTo(xProj, yBreak); ctx.stroke()
      ctx.setLineDash([])

      ctx.globalAlpha = 0.12
      ctx.fillStyle = col
      ctx.fillRect(xEnd, Math.min(yBreak, yTarget), xProj - xEnd, Math.abs(yTarget - yBreak))
      ctx.globalAlpha = 0.7
      ctx.strokeStyle = col
      ctx.setLineDash([3, 3])
      ctx.strokeRect(xEnd, Math.min(yBreak, yTarget), xProj - xEnd, Math.abs(yTarget - yBreak))
      ctx.setLineDash([])

      // 5. the badge
      ctx.globalAlpha = 1
      const label = p.label.toUpperCase()
      ctx.font = `700 9px ui-sans-serif, system-ui`
      const tw = ctx.measureText(label).width
      const bx = Math.max(2, Math.min(this.pane.w - tw - 18, pts[0].x))
      const by = Math.min(...pts.map((q) => q.y)) - 20
      ctx.fillStyle = `rgba(${this.theme.plateRgb},0.9)`
      ctx.fillRect(bx, by, tw + 14, 17)
      ctx.fillStyle = col
      ctx.fillRect(bx, by, 2.5, 17)
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle'
      ctx.fillText(label, bx + 7, by + 9)

      ctx.font = `9px ui-sans-serif, system-ui`
      ctx.fillStyle = this.theme.textDim
      ctx.fillText(`Q${p.quality} · ${p.status}`, bx + 7, by + 26)
    }
    ctx.restore()
  }

  // --- swings, structure, events --------------------------------------- //
  private drawSwings() {
    const s = this.snap
    if (!s?.swings) return
    const ctx = this.bctx

    // Significant swings get a ring with a solid dot; the rest - lower-
    // timeframe wiggles - an empty ring. No HH/HL text: the colour already
    // says up or down, and a label on every pivot buried the candles.
    //
    // "Significant" is the top 30% by leg size (strength, in ATR). A fixed
    // ATR cut-off would mean different things on different charts; the
    // percentile lands at ~2.7 ATR on 5m, 15m and 1h alike (measured).
    const legs = s.swings.map((w) => w.strength ?? 0).sort((a, b) => a - b)
    const cut = legs.length ? legs[Math.floor(legs.length * 0.7)] : Infinity

    ctx.save()
    for (const sw of s.swings) {
      const x = this.snapX(sw.idx)
      if (x < -20 || x > this.pane.w + 20) continue
      const high = sw.kind === 'high'
      const y = this.yOf(sw.price) + (high ? -6 : 6)
      const col = sw.label === 'HH' || sw.label === 'HL' ? this.theme.up
        : sw.label === 'LH' || sw.label === 'LL' ? this.theme.down
        : this.theme.textDim
      const major = (sw.strength ?? 0) >= cut

      // A swept swing is history: dimmed, still visible.
      ctx.globalAlpha = sw.swept ? 0.45 : 1
      ctx.strokeStyle = col
      if (major) {
        ctx.lineWidth = 1.5
        ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2); ctx.stroke()
        ctx.fillStyle = col
        ctx.beginPath(); ctx.arc(x, y, 2.2, 0, Math.PI * 2); ctx.fill()
      } else {
        ctx.globalAlpha *= 0.8
        ctx.lineWidth = 1
        ctx.beginPath(); ctx.arc(x, y, 3.2, 0, Math.PI * 2); ctx.stroke()
      }
    }
    ctx.restore()
  }

  private drawStructureBreaks() {
    const s = this.snap
    if (!s?.breaks) return
    const ctx = this.bctx
    ctx.save()
    ctx.font = `700 8.5px ui-sans-serif, system-ui`
    for (const b of s.breaks) {
      const x = this.snapX(b.idx)
      if (x < -30 || x > this.pane.w + 30) continue
      const y = this.yOf(b.price)
      const col = b.kind === 'CHoCH' ? this.theme.warn
        : b.direction === 'up' ? this.theme.up : this.theme.down
      ctx.globalAlpha = 0.85
      ctx.strokeStyle = col
      ctx.lineWidth = 1
      ctx.setLineDash([2, 2])
      ctx.beginPath(); ctx.moveTo(x - this.barW() * 6, y); ctx.lineTo(x, y); ctx.stroke()
      ctx.setLineDash([])
      // Full strength on a plate: these sit ON the candles, and a label drawn
      // at 85% straight over wicks was where "blurry" was worst.
      ctx.globalAlpha = 1
      const bw = ctx.measureText(b.kind).width
      ctx.fillStyle = `rgba(${this.theme.plateRgb},0.78)`
      ctx.fillRect(x + 1, y - 7, bw + 5, 14)
      ctx.fillStyle = col
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle'
      ctx.fillText(b.kind, x + 3.5, y)
    }
    ctx.restore()
  }

  private drawEvents() {
    const s = this.snap
    if (!s?.events) return
    const ctx = this.bctx
    ctx.save()
    const glyph: Record<string, string> = {
      sweep: '↯', rejection: '✕', breakout: '▲', retest: '◎', false_break: '⊘',
    }
    for (const e of s.events) {
      if (e.bars_since > 90) continue
      const x = this.snapX(e.idx)
      if (x < -10 || x > this.pane.w + 10) continue
      const y = this.yOf(e.extreme)
      const col = e.bias === 'bullish' ? this.theme.up : this.theme.down
      const up = e.direction === 'up'
      ctx.globalAlpha = Math.max(0.35, e.strength / 100)
      ctx.fillStyle = col
      ctx.font = `500 11px ${UI_FONT}`
      ctx.textAlign = 'center'
      ctx.textBaseline = up ? 'bottom' : 'top'
      ctx.fillText(glyph[e.kind] ?? '•', x, y + (up ? -3 : 3))
    }
    ctx.restore()
  }

  // --- the live signal -------------------------------------------------- //
  /**
   * A price distance in pips.
   *
   * A pip is ten points: 0.1 on a 2-digit quote like gold, 0.0001 on a
   * 5-digit FX pair, 0.01 on a 3-digit yen pair. Derived from the
   * instrument's own digits rather than assumed, because a constant 0.0001
   * would report gold in units a hundred times too small.
   */
  private pips(distance: number): string {
    const pip = Math.pow(10, -(Math.max(1, this.digits) - 1))
    const v = distance / pip
    return (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10).toLocaleString('en-US')
  }

  /**
   * What that distance is worth at the size this chart assumes, or ''.
   *
   * Worked from the broker's own tick_value, which is quoted per lot IN THE
   * ACCOUNT CURRENCY. That one fact is what lets a single line of arithmetic
   * cover gold, a yen cross and an index without a table of contract sizes
   * or a cross rate - the broker has already done the conversion.
   *
   * Returns empty rather than guessing when the contract facts have not
   * arrived. A plausible-looking wrong number is the failure worth avoiding
   * here; a missing one is merely unhelpful.
   */
  private cash(distance: number): string {
    const m = this.money
    if (!m || !(m.tickSize > 0) || !(m.tickValue > 0) || !(m.lots > 0)) return ''
    const v = (distance / m.tickSize) * m.tickValue * m.lots
    if (!Number.isFinite(v)) return ''
    const n = v >= 100 ? v.toFixed(0) : v.toFixed(2)
    return `${m.symbol}${n}`
  }

  private drawSignal() {
    const sig = this.signal
    if (!sig) return
    const ctx = this.bctx
    const buy = sig.side === 'buy'
    const col = buy ? this.theme.up : this.theme.down
    const x0 = this.xOfTime(sig.bar_time_ms)
    const x1 = this.pane.w

    const yE = this.yOf(sig.entry)
    const yS = this.yOf(sig.stop)
    const yT1 = this.yOf(sig.tp1)
    const yT2 = this.yOf(sig.tp2)

    ctx.save()
    ctx.beginPath(); ctx.rect(0, 0, this.pane.w, this.pane.h); ctx.clip()

    // No shaded risk and reward blocks.
    //
    // They tinted a third of the chart to say something the four rails
    // already say, and they said it by colouring the CANDLES - the one part
    // of the chart that should carry no wash at all. What is between entry
    // and stop is the space between two labelled lines; it does not need to
    // be painted to be seen, and the price axis now tags all four prices.

    const rail = (y: number, colour: string, label: string, dashed = false) => {
      ctx.globalAlpha = 0.95
      ctx.strokeStyle = colour
      ctx.lineWidth = 1.2
      ctx.setLineDash(dashed ? [5, 4] : [])
      ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke()
      ctx.setLineDash([])
      ctx.font = `700 9px ui-sans-serif, system-ui`
      const tw = ctx.measureText(label).width
      ctx.fillStyle = `rgba(${this.theme.plateRgb},0.92)`
      ctx.fillRect(x1 - tw - 12, y - 8, tw + 10, 16)
      ctx.fillStyle = colour
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle'
      ctx.fillText(label, x1 - tw - 7, y)
    }

    // DISTANCE, not price. The axis chip beside each rail already carries the
    // price; repeating it here spent the label on a number you can read two
    // inches to the right. What the label can say that the axis cannot is how
    // far away it is - which is the question actually being asked of a target.
    const away = (name: string, p: number, sign: string) => {
      const d = Math.abs(p - sig.entry)
      const cash = this.cash(d)
      return `${name} ${sign}${this.pips(d)}p${cash ? `  ${sign}${cash}` : ''}`
    }
    rail(yT2, col, away('TP2', sig.tp2, '+'), true)
    rail(yT1, col, away('TP1', sig.tp1, '+'), true)
    rail(yE, this.theme.warn, 'ENTRY')
    rail(yS, buy ? this.theme.down : this.theme.up, away('SL', sig.stop, '-'))

    // direction marker at the signal bar
    ctx.globalAlpha = 1
    ctx.fillStyle = col
    ctx.beginPath()
    if (buy) {
      ctx.moveTo(x0, yE + 12); ctx.lineTo(x0 - 5, yE + 21); ctx.lineTo(x0 + 5, yE + 21)
    } else {
      ctx.moveTo(x0, yE - 12); ctx.lineTo(x0 - 5, yE - 21); ctx.lineTo(x0 + 5, yE - 21)
    }
    ctx.closePath(); ctx.fill()
    ctx.restore()
  }

  // --- sub panes -------------------------------------------------------- //
  private drawSubPanes() {
    for (const p of this.subPanes) {
      if (p.key === 'volume') this.drawVolume(p.y, p.h)
      if (p.key === 'rsi') this.drawRsi(p.y, p.h)
      if (p.key === 'macd') this.drawMacd(p.y, p.h)
    }
  }

  private drawVolume(top: number, h: number) {
    const ctx = this.bctx
    const from = Math.max(0, Math.floor(this.view.start))
    const to = Math.min(this.bars.length, Math.ceil(this.view.start + this.view.span))
    let vmax = 0
    for (let i = from; i < to; i++) vmax = Math.max(vmax, this.bars[i].v)
    if (vmax <= 0) return
    const bw = this.barW()
    const body = Math.max(1, Math.min(14, bw * 0.68))
    ctx.save()
    ctx.globalAlpha = 0.5
    for (let i = from; i < to; i++) {
      const b = this.bars[i]
      const bh = (b.v / vmax) * (h - 4)
      ctx.fillStyle = b.c >= b.o ? this.theme.up : this.theme.down
      ctx.fillRect(Math.round(this.xOf(i) - body / 2), top + h - bh, Math.round(body), bh)
    }
    ctx.globalAlpha = 1
    ctx.font = `500 10px ${UI_FONT}`
    ctx.fillStyle = this.theme.textDim
    ctx.textAlign = 'left'; ctx.textBaseline = 'top'
    ctx.fillText('VOLUME', 4, top + 2)
    ctx.restore()
  }

  /**
   * RSI is recomputed client-side rather than shipped in the snapshot: the
   * snapshot carries the latest value only, and a single value cannot be drawn
   * as a line. The period matches the server's default so the pane and the
   * momentum panel never disagree.
   */
  /** Wilder RSI over the chart's own bars. */
  private rsiSeries(period = 14): Float64Array {
    const n = this.bars.length
    const rsi = new Float64Array(n).fill(NaN)
    if (n <= period) return rsi
    let ag = 0, al = 0
    for (let i = 1; i <= period && i < n; i++) {
      const d = this.bars[i].c - this.bars[i - 1].c
      if (d > 0) ag += d; else al -= d
    }
    ag /= period; al /= period
    rsi[period] = al === 0 ? 100 : 100 - 100 / (1 + ag / al)
    for (let i = period + 1; i < n; i++) {
      const d = this.bars[i].c - this.bars[i - 1].c
      const g = d > 0 ? d : 0
      const l = d < 0 ? -d : 0
      ag = (ag * (period - 1) + g) / period
      al = (al * (period - 1) + l) / period
      rsi[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al)
    }
    return rsi
  }

  /** EMA of a series that may open with NaNs. */
  private emaOf(src: ArrayLike<number>, period: number): Float64Array {
    const n = src.length
    const out = new Float64Array(n).fill(NaN)
    const k = 2 / (period + 1)
    let prev = NaN
    for (let i = 0; i < n; i++) {
      const v = src[i]
      if (Number.isNaN(v)) continue
      prev = Number.isNaN(prev) ? v : v * k + prev * (1 - k)
      out[i] = prev
    }
    return out
  }

  /**
   * The engine's divergence verdict, drawn rather than asserted.
   *
   * Pivots come from the snapshot so the picture and the words in the right
   * rail are the same conclusion - recomputing it here would eventually
   * produce a chart that disagrees with its own analyst. Hidden divergence is
   * drawn dashed: it means continuation, the opposite trade to the regular
   * kind, and a reader who confuses the two has it exactly backwards.
   */
  private drawDivergence(rsi: Float64Array, yv: (v: number) => number,
                         top: number, h: number) {
    const div: any = (this.snap as any)?.momentum?.divergence
    if (!div?.kind || !div.pivots) return
    const a = div.pivots.from, b = div.pivots.to
    if (a?.idx == null || b?.idx == null) return

    const ctx = this.bctx
    const hidden = String(div.kind).endsWith('hidden')
    const col = div.bias === 'bullish' ? this.theme.up : this.theme.down
    const ax = this.snapX(a.idx), bx = this.snapX(b.idx)
    if (bx < -40 || ax > this.pane.w + 40) return

    // On the RSI pane, between the two RSI readings.
    ctx.globalAlpha = 0.95
    ctx.strokeStyle = col
    ctx.lineWidth = 1.2
    ctx.setLineDash(hidden ? [3, 3] : [])
    ctx.beginPath()
    ctx.moveTo(ax, yv(a.rsi)); ctx.lineTo(bx, yv(b.rsi))
    ctx.stroke()
    ctx.setLineDash([])
    for (const [x, v] of [[ax, a.rsi], [bx, b.rsi]] as [number, number][]) {
      ctx.fillStyle = col
      ctx.beginPath(); ctx.arc(x, yv(v), 2.2, 0, Math.PI * 2); ctx.fill()
    }

    ctx.font = `600 10.5px ${UI_FONT}`
    ctx.fillStyle = col
    ctx.textAlign = 'right'
    ctx.textBaseline = 'bottom'
    ctx.fillText(hidden ? 'hidden div' : 'div', this.pane.w - 3, top + h - 2)

    // And on the PRICE pane, between the two extremes it contradicts. Half
    // the point of divergence is that the two lines tilt opposite ways, which
    // is invisible if only one of them is drawn.
    ctx.save()
    ctx.beginPath(); ctx.rect(0, this.pane.y, this.pane.w, this.pane.h); ctx.clip()
    ctx.globalAlpha = 0.8
    ctx.strokeStyle = col
    ctx.lineWidth = 1.2
    ctx.setLineDash(hidden ? [3, 3] : [])
    ctx.beginPath()
    ctx.moveTo(ax, this.yOf(a.price)); ctx.lineTo(bx, this.yOf(b.price))
    ctx.stroke()
    ctx.setLineDash([])
    ctx.restore()
  }

  /**
   * MACD: histogram, line and signal, on the standard 12/26/9.
   *
   * Scaled to the largest absolute value IN VIEW rather than over the whole
   * buffer, so a quiet stretch still fills the pane instead of flatlining
   * under one old spike.
   */
  private drawMacd(top: number, h: number) {
    const ctx = this.bctx
    const n = this.bars.length
    if (n < 40) return
    const close = new Float64Array(n)
    for (let i = 0; i < n; i++) close[i] = this.bars[i].c
    const fast = this.emaOf(close, 12)
    const slow = this.emaOf(close, 26)
    const line = new Float64Array(n).fill(NaN)
    for (let i = 25; i < n; i++) line[i] = fast[i] - slow[i]
    const sig = this.emaOf(line, 9)

    const from = Math.max(0, Math.floor(this.view.start))
    const to = Math.min(n, Math.ceil(this.view.start + this.view.span))
    let peak = 0
    for (let i = from; i < to; i++) {
      for (const v of [line[i], sig[i], line[i] - sig[i]]) {
        if (!Number.isNaN(v)) peak = Math.max(peak, Math.abs(v))
      }
    }
    if (peak <= 0) return
    const mid = top + h / 2
    const yv = (v: number) => mid - (v / peak) * (h / 2 - 3)

    ctx.save()
    ctx.beginPath(); ctx.rect(0, top, this.pane.w, h); ctx.clip()

    const body = Math.max(1, this.barW() * 0.6)
    for (let i = from; i < to; i++) {
      const hv = line[i] - sig[i]
      if (Number.isNaN(hv)) continue
      // Fading a bar whose momentum is shrinking is the whole signal in a
      // MACD histogram, so rising and falling get different weight.
      const prev = i > 0 ? line[i - 1] - sig[i - 1] : hv
      const growing = Math.abs(hv) >= Math.abs(prev)
      ctx.globalAlpha = growing ? 0.85 : 0.4
      ctx.fillStyle = hv >= 0 ? this.theme.up : this.theme.down
      const y = yv(hv)
      ctx.fillRect(Math.round(this.xOf(i) - body / 2), Math.min(y, mid),
                   Math.round(body), Math.max(1, Math.abs(y - mid)))
    }

    ctx.globalAlpha = 0.45
    ctx.strokeStyle = this.theme.line
    ctx.lineWidth = 1
    ctx.beginPath(); ctx.moveTo(0, Math.round(mid) + 0.5)
    ctx.lineTo(this.pane.w, Math.round(mid) + 0.5); ctx.stroke()

    const stroke = (src: ArrayLike<number>, col: string, w: number) => {
      ctx.globalAlpha = 1
      ctx.strokeStyle = col
      ctx.lineWidth = w
      ctx.beginPath()
      let started = false
      for (let i = from; i < to; i++) {
        const v = src[i]
        if (Number.isNaN(v)) continue
        const x = this.xOf(i), y = yv(v)
        if (!started) { ctx.moveTo(x, y); started = true } else ctx.lineTo(x, y)
      }
      ctx.stroke()
    }
    stroke(sig, this.theme.warn, 1)
    stroke(line, this.theme.info, 1.3)

    ctx.globalAlpha = 0.75
    ctx.font = `500 10px ${UI_FONT}`
    ctx.fillStyle = this.theme.textDim
    ctx.textAlign = 'left'; ctx.textBaseline = 'top'
    ctx.fillText('MACD 12 26 9', 3, top + 2)
    ctx.restore()
  }

  /**
   * ZigZag: the swing skeleton, joined up.
   *
   * Same swings the structure labels already use, so the line cannot tell a
   * different story from the HH/HL markers sitting on it.
   */
  private drawZigZag() {
    const sw = this.snap?.swings
    if (!sw?.length) return
    const ctx = this.bctx
    ctx.save()
    ctx.beginPath(); ctx.rect(0, 0, this.pane.w, this.pane.h); ctx.clip()
    ctx.globalAlpha = 0.65
    ctx.strokeStyle = this.theme.accent
    ctx.lineWidth = 1.1
    ctx.setLineDash([])
    ctx.beginPath()
    let started = false
    for (const p of sw) {
      const x = this.snapX(p.idx), y = this.yOf(p.price)
      if (!started) { ctx.moveTo(x, y); started = true } else ctx.lineTo(x, y)
    }
    // Carry the last leg to the live price so the skeleton does not stop
    // short of the candle everyone is actually looking at.
    const last = this.bars[this.bars.length - 1]
    if (started && last) ctx.lineTo(this.xOf(this.bars.length - 1), this.yOf(last.c))
    ctx.stroke()
    ctx.restore()
  }

  private drawRsi(top: number, h: number) {
    const ctx = this.bctx
    const n = this.bars.length
    if (n < 20) return
    const period = 14
    const rsi = this.rsiSeries(period)
    // A short EMA of RSI. Momentum crossing its own average is the readable
    // part of an RSI pane; the raw line alone whipsaws through 50 constantly.
    const sig = this.emaOf(rsi, 9)

    const yv = (v: number) => top + h - (v / 100) * h
    ctx.save()
    ctx.beginPath(); ctx.rect(0, top, this.pane.w, h); ctx.clip()

    // 80/20 rather than 70/30: on a 5m scalping chart 70 is reached so often
    // it carries no information, and the bands are what the signal gates use.
    ctx.globalAlpha = 0.06
    ctx.fillStyle = this.theme.info
    ctx.fillRect(0, yv(80), this.pane.w, yv(20) - yv(80))

    const bands: [number, string, string][] = [
      [80, this.theme.down, 'overbought 80'],
      [50, this.theme.textDim, '50'],
      [20, this.theme.up, 'oversold 20'],
    ]
    ctx.lineWidth = 1
    ctx.font = `500 10px ${UI_FONT}`
    ctx.textAlign = 'left'
    ctx.textBaseline = 'bottom'
    for (const [lvl, col, label] of bands) {
      const y = Math.round(yv(lvl)) + 0.5
      ctx.globalAlpha = lvl === 50 ? 0.4 : 0.55
      ctx.strokeStyle = col
      ctx.setLineDash(lvl === 50 ? [2, 3] : [4, 3])
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(this.pane.w, y); ctx.stroke()
      ctx.globalAlpha = 0.85
      ctx.fillStyle = col
      ctx.fillText(label, 3, y - 1)
    }
    ctx.setLineDash([])

    const from = Math.max(period, Math.floor(this.view.start))
    const to = Math.min(n, Math.ceil(this.view.start + this.view.span))
    const stroke = (src: ArrayLike<number>, col: string, w: number, a: number) => {
      ctx.globalAlpha = a
      ctx.strokeStyle = col
      ctx.lineWidth = w
      ctx.beginPath()
      let started = false
      for (let i = from; i < to; i++) {
        const v = src[i]
        if (Number.isNaN(v)) continue
        const x = this.xOf(i), y = yv(v)
        if (!started) { ctx.moveTo(x, y); started = true } else ctx.lineTo(x, y)
      }
      ctx.stroke()
    }
    stroke(sig, this.theme.warn, 1, 0.85)
    stroke(rsi, this.theme.accent, 1.3, 1)

    this.drawDivergence(rsi, yv, top, h)

    // Readout on the RIGHT, by the axis. The band labels own the left margin
    // now, and `overbought 80` sits at exactly the height this used to.
    ctx.globalAlpha = 0.9
    ctx.font = `500 10px ${UI_FONT}`
    ctx.fillStyle = this.theme.textDim
    ctx.textAlign = 'right'; ctx.textBaseline = 'top'
    const last = rsi[n - 1]
    ctx.fillText(`RSI 14   ${Number.isNaN(last) ? '' : last.toFixed(1)}`,
                 this.pane.w - 3, top + 2)
    ctx.restore()
  }

  // --- axes ------------------------------------------------------------- //
  private drawPriceAxis() {
    const ctx = this.bctx
    const x = this.pane.w
    ctx.save()
    ctx.fillStyle = this.theme.axisFill ?? `rgba(${this.theme.plateRgb},0.75)`
    ctx.fillRect(x, 0, PRICE_AXIS_W, this.height)
    ctx.strokeStyle = this.theme.axisLine ?? this.theme.line
    ctx.lineWidth = 1
    ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, this.height); ctx.stroke()

    // Two weights, not one.
    //
    // Every label at full strength is a solid column of digits that all look
    // equally important, and finding "where is 4,300" means reading the lot.
    // The round levels are drawn bright and slightly heavier; the ticks
    // between them drop to the dim ink and act as a scale you measure
    // against rather than text you read. Nothing is hidden - a number you
    // need is still there, it just stops competing.
    // The prices that MEAN something get a chip; the ladder behind them does
    // not. Placed before the tick labels so a label sitting under a chip can
    // be dropped rather than drawn through it.
    const tags = this.axisTags()

    ctx.textAlign = 'left'; ctx.textBaseline = 'middle'
    for (const { p, major } of this.priceTicks()) {
      const y = this.yOf(p)
      if (y < 8 || y > this.pane.h - 4) continue
      // A chip already says what this row is, and more usefully.
      if (tags.some((t) => Math.abs(t.y - y) < TAG_H)) continue
      ctx.font = major ? `600 11px ${NUM_FONT}` : `11px ${NUM_FONT}`
      ctx.fillStyle = major ? this.theme.text : this.theme.textDim
      ctx.fillText(p.toFixed(this.digits), x + 7, y)
    }

    for (const t of tags) {
      ctx.fillStyle = t.colour
      ctx.fillRect(x, t.y - TAG_H / 2, PRICE_AXIS_W, TAG_H)
      ctx.fillStyle = this.theme.chipInk
      ctx.font = `700 10.5px ${NUM_FONT}`
      ctx.fillText(t.price.toFixed(this.digits), x + 7, t.y)
    }
    // No MANUAL badge. It sat permanently over the lowest price labels to
    // explain a gesture you only need once; double-clicking anywhere on the
    // chart now restores automatic scaling.
    ctx.restore()
  }

  /**
   * The prices worth reading off the axis, as chips.
   *
   * A price axis is a ruler, and a ruler tells you nothing about which of its
   * marks matter. The ones that do are not round numbers at all - they are
   * the levels the chart has already drawn a line at: where the signal enters
   * and stops out, where it is aiming, and the support and resistance the
   * engine scored. Those are the numbers you would otherwise read off a line
   * by eye and then hunt for in the column beside it.
   *
   * Everything here is already ON the chart as a line. This puts its price
   * where prices live, in the colour it was drawn in, so the line and the
   * number are the same object.
   */
  private axisTags(): { y: number; price: number; colour: string }[] {
    const out: { y: number; price: number; colour: string }[] = []
    const want: { price: number; colour: string }[] = []

    // Order is priority: the first one placed at a given height wins it.
    // A signal's own levels beat generic structure, because they are the
    // four numbers a decision is actually made on.
    const sig = this.overlays.signal ? this.signal : null
    if (sig) {
      const buy = sig.side === 'buy'
      const side = buy ? this.theme.up : this.theme.down
      want.push({ price: sig.entry, colour: this.theme.warn })
      want.push({ price: sig.stop, colour: buy ? this.theme.down : this.theme.up })
      want.push({ price: sig.tp1, colour: side })
      want.push({ price: sig.tp2, colour: side })
    }

    if (this.overlays.levels) {
      // The same set drawLevels renders, in the same order and the same
      // colours - so a chip can never appear for a line that is not there.
      const shown = [...(this.snap?.levels ?? [])]
        .filter((lv: any) => lv.score >= 50)
        .sort((a: any, b: any) => b.score - a.score)
        .slice(0, 8)
      for (const lv of shown) {
        want.push({
          price: lv.price,
          colour: lv.kind === 'resistance' ? this.theme.down
            : lv.kind === 'support' ? this.theme.up : this.theme.warn,
        })
      }
    }

    // The live price chip is drawn last of all, over the top of this axis.
    // Its row is claimed up front so a tag is never placed where it will be
    // buried - which would read as a level silently missing.
    const last = this.bars.length ? this.bars[this.bars.length - 1] : null
    const taken: number[] = []
    if (last) {
      const ly = this.yOf(last.c)
      if (ly >= 0 && ly <= this.pane.h) {
        taken.push(ly)
        // The bar countdown rides directly under the price tag - or over it
        // near the foot of the pane - and is painted after this too. Both of
        // its possible homes are claimed, because a level buried under a
        // clock is worse than a level that simply moved down the axis.
        if (this.tfMs) taken.push(ly + 24 > this.pane.h ? ly - 16 : ly + 16)
      }
    }

    for (const w of want) {
      if (!Number.isFinite(w.price)) continue
      const y = this.yOf(w.price)
      // Half a chip of margin at each end, so one is never clipped by the
      // top of the pane or the time axis.
      if (y < TAG_H / 2 || y > this.pane.h - TAG_H / 2) continue
      if (taken.some((t) => Math.abs(t - y) < TAG_GAP)) continue
      taken.push(y)
      out.push({ y, price: w.price, colour: w.colour })
    }
    return out
  }

  private drawTimeAxis() {
    const ctx = this.bctx
    const y = this.height - TIME_AXIS_H
    ctx.save()
    ctx.strokeStyle = this.theme.axisLine ?? this.theme.line
    ctx.lineWidth = 1
    ctx.beginPath(); ctx.moveTo(0, y + 0.5); ctx.lineTo(this.width, y + 0.5); ctx.stroke()
    ctx.font = `11px ${NUM_FONT}`
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
    for (const t of this.timeTicks()) {
      const x = this.xOf(t.i)
      if (x < 22 || x > this.pane.w - 22) continue
      ctx.fillStyle = t.major ? this.theme.text : this.theme.textDim
      ctx.fillText(t.label, x, y + TIME_AXIS_H / 2)
    }
    ctx.restore()
  }

  private drawLastPrice() {
    if (!this.bars.length) return
    const ctx = this.bctx
    const last = this.bars[this.bars.length - 1]
    const y = this.yOf(last.c)
    if (y < 0 || y > this.pane.h) return
    const up = last.c >= last.o
    const col = up ? this.theme.up : this.theme.down
    ctx.save()
    // Solid and full strength: the one line on the chart that is always
    // current. Dashed at 50% it looked like just another level. Snapped to
    // the pixel grid so a 1px line lands on one row of pixels, not two faint ones.
    const py = Math.round(y) + 0.5
    ctx.strokeStyle = col
    ctx.globalAlpha = 1
    ctx.lineWidth = 1
    ctx.setLineDash([])
    ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(this.pane.w, py); ctx.stroke()
    ctx.fillStyle = col
    ctx.fillRect(this.pane.w, y - 8, PRICE_AXIS_W, 16)
    ctx.fillStyle = this.theme.chipInk
    ctx.font = `700 11px ${NUM_FONT}`
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle'
    ctx.fillText(last.c.toFixed(this.digits), this.pane.w + 7, y)

    // Candle countdown, in a second chip under the price. Hidden when the
    // bar is long overdue - that is a closed market or a stalled feed, and a
    // clock reading 00:00 forever would claim a close that is not coming.
    const left = this.tfMs ? last.t + this.tfMs - Date.now() : NaN
    if (Number.isFinite(left) && left > -this.tfMs) {
      const s = Math.max(0, Math.floor(left / 1000))
      const hh = Math.floor(s / 3600)
      const mm = Math.floor((s % 3600) / 60)
      const ss = s % 60
      const pad = (n: number) => String(n).padStart(2, '0')
      const label = hh > 0 ? `${hh}:${pad(mm)}:${pad(ss)}` : `${pad(mm)}:${pad(ss)}`
      // Below the price tag, or above it when that would leave the pane.
      const ty = y + 24 > this.pane.h ? y - 24 : y + 8
      // Its own colour, not the price tag's: green/red there means up/down
      // on the bar, and a timer in the same colour reads as a second price.
      ctx.fillStyle = this.theme.warn
      ctx.fillRect(this.pane.w, ty, PRICE_AXIS_W, 16)
      ctx.fillStyle = this.theme.chipInk
      ctx.font = `700 11px ${NUM_FONT}`
      ctx.fillText(label, this.pane.w + 7, ty + 8)
    }
    ctx.restore()
  }

  // ------------------------------------------------------------------ //
  // top layer: crosshair                                               //
  // ------------------------------------------------------------------ //
  private drawTop() {
    const ctx = this.tctx
    ctx.clearRect(0, 0, this.width, this.height)
    const h = this.hover
    if (!h) return
    ctx.save()
    ctx.strokeStyle = this.theme.crosshair
    ctx.globalAlpha = 0.55
    ctx.lineWidth = 1
    ctx.setLineDash([3, 3])
    ctx.beginPath()
    ctx.moveTo(Math.round(h.x) + 0.5, 0)
    ctx.lineTo(Math.round(h.x) + 0.5, this.height - TIME_AXIS_H)
    ctx.stroke()
    if (h.y < this.pane.h) {
      ctx.beginPath()
      ctx.moveTo(0, Math.round(h.y) + 0.5)
      ctx.lineTo(this.pane.w, Math.round(h.y) + 0.5)
      ctx.stroke()
    }
    ctx.setLineDash([])
    ctx.globalAlpha = 1

    if (h.y < this.pane.h) {
      const label = h.price.toFixed(this.digits)
      ctx.font = `700 11px ${NUM_FONT}`
      ctx.fillStyle = this.theme.crosshair
      ctx.fillRect(this.pane.w, h.y - 8, PRICE_AXIS_W, 16)
      ctx.fillStyle = this.theme.chipInk
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle'
      ctx.fillText(label, this.pane.w + 7, h.y)
    }

    if (h.bar) {
      const d = new Date(h.bar.t)
      const label = `${String(d.getUTCDate()).padStart(2, '0')} ${d.toLocaleString('en', { month: 'short', timeZone: 'UTC' })} ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
      ctx.font = `11px ${NUM_FONT}`
      const tw = ctx.measureText(label).width
      const bx = Math.max(0, Math.min(this.pane.w - tw - 12, h.x - tw / 2 - 6))
      ctx.fillStyle = this.theme.crosshair
      ctx.fillRect(bx, this.height - TIME_AXIS_H + 3, tw + 12, 16)
      ctx.fillStyle = this.theme.chipInk
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle'
      ctx.fillText(label, bx + 6, this.height - TIME_AXIS_H + 11)
    }
    ctx.restore()
  }

  // ------------------------------------------------------------------ //
  // input                                                              //
  // ------------------------------------------------------------------ //
  /** Is this x inside the price axis strip on the right? */
  private onPriceAxis(x: number): boolean {
    return x >= this.pane.w
  }

  private onMove = (e: MouseEvent) => {
    const r = this.top.getBoundingClientRect()
    const x = e.clientX - r.left
    const y = e.clientY - r.top

    if (this.dragging && this.dragMode === 'scale') {
      // Dragging the price axis stretches or compresses the candles. Down
      // expands the range (candles shrink), up contracts it (candles grow),
      // which is the direction every charting package uses.
      const dy = y - this.dragStartY
      const factor = Math.exp(dy / 220)
      this.priceAuto = false
      this.priceCentre = this.dragStartCentre
      this.priceSpan = Math.max(1e-6, this.dragStartSpan * factor)
      this.scheduleBase()
    } else if (this.dragging) {
      const dx = x - this.dragStartX
      const dy = y - this.dragStartY
      this.view.start = this.dragStartView - dx / this.barW()
      if (Math.abs(dy) > 2) {
        // Any vertical movement takes manual control of the price scale;
        // otherwise the next tick would re-fit and undo the drag.
        this.priceAuto = false
        const perPixel = this.priceSpan / Math.max(this.pane.h, 1)
        this.priceCentre = this.dragStartCentre + dy * perPixel
      }
      this.clampView()
      this.scheduleBase()
      this.onViewChange?.(this.getViewport())
    }
    if (!this.dragging) {
      this.top.style.cursor = this.onPriceAxis(x) ? 'ns-resize' : 'crosshair'
    }
    const bi = Math.round(this.barAt(x))
    const bar = bi >= 0 && bi < this.bars.length ? this.bars[bi] : null
    this.hover = {
      x, y, barIndex: bi, bar,
      price: this.priceAt(y),
      time: bar?.t ?? 0,
    }
    this.scheduleTop()
    this.onHover?.(this.hover)

    const ev = this.newsAt(x, y)
    if (ev !== this.lastNewsHit) {
      this.lastNewsHit = ev
      this.onNewsHover?.(ev ? { event: ev, x, y } : null)
    }
  }

  private onLeave = () => {
    if (this.lastNewsHit) {
      this.lastNewsHit = null
      this.onNewsHover?.(null)
    }
    this.hover = null
    this.dragging = false
    this.scheduleTop()
    this.onHover?.(null)
  }

  private onDown = (e: MouseEvent) => {
    const r = this.top.getBoundingClientRect()
    const x = e.clientX - r.left
    const y = e.clientY - r.top
    this.dragging = true
    this.dragMode = this.onPriceAxis(x) ? 'scale' : 'pan'
    this.dragStartX = x
    this.dragStartY = y
    this.dragStartView = this.view.start
    this.dragStartCentre = (this.priceMin + this.priceMax) / 2
    this.dragStartSpan = this.priceMax - this.priceMin
    this.top.style.cursor = this.dragMode === 'scale' ? 'ns-resize' : 'grabbing'
  }

  private onUp = () => {
    this.dragging = false
    this.top.style.cursor = 'crosshair'
  }

  /** Double-clicking the price axis returns it to automatic. */
  /**
   * Double click anywhere - chart or axis - hands price scaling back to the
   * chart. It used to work only over the price axis, which is a thin target
   * and had to be advertised with a permanent badge to be discoverable.
   */
  private onDouble = () => {
    this.resetPriceScale()
  }

  private onWheel = (e: WheelEvent) => {
    e.preventDefault()
    const r = this.top.getBoundingClientRect()
    const x = e.clientX - r.left
    if (this.onPriceAxis(x)) {
      // Wheel over the axis scales price, matching the drag there.
      this.priceAuto = false
      this.priceSpan *= e.deltaY > 0 ? 1.1 : 0.91
      this.scheduleBase()
      return
    }
    this.zoom(e.deltaY > 0 ? 1.15 : 0.87, this.barAt(x))
  }

  private attach() {
    this.top.style.cursor = 'crosshair'
    this.top.addEventListener('mousemove', this.onMove)
    this.top.addEventListener('mouseleave', this.onLeave)
    this.top.addEventListener('mousedown', this.onDown)
    this.top.addEventListener('dblclick', this.onDouble)
    window.addEventListener('mouseup', this.onUp)
    this.top.addEventListener('wheel', this.onWheel, { passive: false })
  }

  private detach() {
    this.top.removeEventListener('mousemove', this.onMove)
    this.top.removeEventListener('mouseleave', this.onLeave)
    this.top.removeEventListener('mousedown', this.onDown)
    this.top.removeEventListener('dblclick', this.onDouble)
    window.removeEventListener('mouseup', this.onUp)
    this.top.removeEventListener('wheel', this.onWheel)
  }
}
