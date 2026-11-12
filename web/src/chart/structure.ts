/**
 * Swings and BOS / CHoCH over the WHOLE chart (2026-11-08).
 *
 * The engine's snapshot only carries structure for its own analysis window
 * (600 bars, the latest 60 swings, the latest 30 breaks), so everything to the
 * left of that was bare. This is a line-for-line port of
 * server/engine/structure.py find_swings() and structure_breaks() with the
 * engine's settings (fractal k 2, leg >= 0.55 x ATR, Wilder ATR 14), run on
 * every bar the chart holds. The chart uses it only BEFORE the snapshot's
 * first swing / break: inside the engine's window the engine's own output is
 * drawn, because that is what the signals read.
 *
 * Display only. Keep in step with structure.py (tools/test_structure_port.py).
 */
import type { Bar } from './types'

export type HSwing = {
  idx: number
  t: number
  price: number
  kind: 'high' | 'low'
  confirmed_at: number
  strength: number
  label: '' | 'HH' | 'HL' | 'LH' | 'LL'
  swept: boolean
}

export type HBreak = {
  idx: number
  t: number
  price: number
  kind: 'BOS' | 'CHoCH'
  direction: 'up' | 'down'
  from_swing: number
}

const K = 2
const ATR_MULT = 0.55
const ATR_N = 14

const round2 = (x: number) => Math.round(x * 100) / 100

/** engine/indicators.atr: Wilder smoothing of the true range; NaN until n bars. */
export function wilderAtr(b: Bar[], n = ATR_N): number[] {
  const len = b.length
  const out = new Array<number>(len).fill(NaN)
  if (len < n) return out
  const tr = b.map((x, i) => {
    const prev = i === 0 ? b[0].c : b[i - 1].c
    return Math.max(x.h - x.l, Math.max(Math.abs(x.h - prev), Math.abs(x.l - prev)))
  })
  let acc = 0
  for (let i = 0; i < n; i++) acc += tr[i]
  acc /= n
  out[n - 1] = acc
  for (let i = n; i < len; i++) {
    acc = (acc * (n - 1) + tr[i]) / n
    out[i] = acc
  }
  return out
}

export function findSwings(b: Bar[], k = K, atrMult = ATR_MULT): HSwing[] {
  const n = b.length
  if (n < 2 * k + 3) return []
  const a = wilderAtr(b)
  const fin = a.filter((x) => !Number.isNaN(x)).sort((x, y) => x - y)
  let median = 0
  if (fin.length) {
    const m = fin.length >> 1
    median = fin.length % 2 ? fin[m] : (fin[m - 1] + fin[m]) / 2
  }
  if (median <= 0) {
    let s = 0
    for (const x of b) s += x.h - x.l
    median = s / n || 1e-6
  }

  // stage 1: raw fractals - the FIRST max/min of the 2k+1 window is the centre
  const raw: [number, number, 'high' | 'low'][] = []
  for (let i = k; i < n - k; i++) {
    let isH = true, isL = true
    for (let j = i - k; j <= i + k; j++) {
      if (j === i) continue
      if (j < i ? b[j].h >= b[i].h : b[j].h > b[i].h) isH = false
      if (j < i ? b[j].l <= b[i].l : b[j].l < b[i].l) isL = false
    }
    if (isH) raw.push([i, b[i].h, 'high'])
    if (isL) raw.push([i, b[i].l, 'low'])
  }

  // stage 2: ATR filter + alternation
  const sw: HSwing[] = []
  const mk = (idx: number, price: number, kind: 'high' | 'low', strength: number): HSwing =>
    ({ idx, t: b[idx].t, price, kind, confirmed_at: idx + k, strength, label: '', swept: false })
  for (const [idx, price, kind] of raw) {
    let local = idx < a.length && !Number.isNaN(a[idx]) ? a[idx] : median
    if (local <= 0) local = median
    if (!sw.length) { sw.push(mk(idx, price, kind, 0)); continue }
    const prev = sw[sw.length - 1]
    if (prev.kind === kind) {
      const better = kind === 'high' ? price > prev.price : price < prev.price
      if (better) {
        const leg = sw.length >= 2 ? Math.abs(price - sw[sw.length - 2].price) / local : 0
        sw[sw.length - 1] = mk(idx, price, kind, round2(leg))
      }
      continue
    }
    const leg = Math.abs(price - prev.price)
    if (leg < atrMult * local) {
      if (sw.length >= 2) {
        const before = sw[sw.length - 2]
        const extends_ = (kind === 'high' && price > before.price) || (kind === 'low' && price < before.price)
        if (extends_) {
          const anchor = sw.length >= 3 ? sw[sw.length - 3] : before
          sw[sw.length - 2] = mk(idx, price, kind, round2(Math.abs(price - anchor.price) / local))
          sw.pop()
        }
      }
      continue
    }
    sw.push(mk(idx, price, kind, round2(leg / local)))
  }

  // labels and swept
  for (const kind of ['high', 'low'] as const) {
    const seq = sw.filter((s) => s.kind === kind)
    for (let i = 1; i < seq.length; i++) {
      const up = seq[i].price > seq[i - 1].price
      seq[i].label = kind === 'high' ? (up ? 'HH' : 'LH') : (up ? 'HL' : 'LL')
    }
  }
  // swept: anything after it traded through it (suffix extremes, one pass)
  const sufH = new Array<number>(n + 1).fill(-Infinity)
  const sufL = new Array<number>(n + 1).fill(Infinity)
  for (let i = n - 1; i >= 0; i--) {
    sufH[i] = Math.max(sufH[i + 1], b[i].h)
    sufL[i] = Math.min(sufL[i + 1], b[i].l)
  }
  for (const s of sw) {
    s.swept = s.kind === 'high' ? sufH[s.idx + 1] > s.price : sufL[s.idx + 1] < s.price
  }
  return sw
}

/** engine/structure.structure_breaks: close-based, CHoCH through the protected level. */
export function structureBreaks(sw: HSwing[], b: Bar[]): HBreak[] {
  if (sw.length < 3) return []
  const known = [...sw].sort((x, y) => x.confirmed_at - y.confirmed_at || x.idx - y.idx)
  const out: HBreak[] = []
  let state = 0
  let candH: HSwing | null = null, candL: HSwing | null = null
  let prot: [number, number] | null = null
  let lastUp = -1, lastDn = -1
  let ptr = 0
  let start = Infinity
  for (const s of sw) start = Math.min(start, s.idx)
  for (let j = start; j < b.length; j++) {
    while (ptr < known.length && known[ptr].confirmed_at <= j) {
      const p = known[ptr]
      if (p.kind === 'high' && p.idx >= lastUp) candH = p
      else if (p.kind === 'low' && p.idx >= lastDn) candL = p
      ptr++
    }
    const cj = b[j].c
    let up: [HBreak['kind'], number, number] | null = null
    if (state === -1 && prot && cj > prot[0]) up = ['CHoCH', prot[0], prot[1]]
    else if (state >= 0 && candH && cj > candH.price) up = ['BOS', candH.price, candH.idx]
    if (up) {
      const li: number = up[2]
      let lo: number = li
      for (let q = li; q <= j; q++) if (b[q].l < b[lo].l) lo = q
      prot = [b[lo].l, lo]
      state = 1
      out.push({ idx: j, t: b[j].t, price: up[1], kind: up[0], direction: 'up', from_swing: li })
      candH = null; lastUp = j
      continue
    }
    let dn: [HBreak['kind'], number, number] | null = null
    if (state === 1 && prot && cj < prot[0]) dn = ['CHoCH', prot[0], prot[1]]
    else if (state <= 0 && candL && cj < candL.price) dn = ['BOS', candL.price, candL.idx]
    if (dn) {
      const li: number = dn[2]
      let hi: number = li
      for (let q = li; q <= j; q++) if (b[q].h > b[hi].h) hi = q
      prot = [b[hi].h, hi]
      state = -1
      out.push({ idx: j, t: b[j].t, price: dn[1], kind: dn[0], direction: 'down', from_swing: li })
      candL = null; lastDn = j
    }
  }
  return out
}
