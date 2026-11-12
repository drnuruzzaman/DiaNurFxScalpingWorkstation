/**
 * Range detection from SWINGS (2026-11-08, rewrite of the body-band scan).
 *
 * How a trader boxes a range, and what this copies:
 *   1. Boundaries come from swing highs and swing lows that CLUSTER - two
 *      highs at about the same price and two lows at about the same price -
 *      not from the highest high / lowest low over a lookback. So a big box
 *      cannot swallow every move between two distant swing points.
 *   2. A range lives until a CONFIRMED breakout (closes beyond the edge, not
 *      a wick); then it stops extending and stays on the chart as history.
 *      Wicks through an edge that close back inside are sweeps, not breaks.
 *   3. Every swing can start a candidate, so a tighter consolidation forming
 *      inside a big range is found on its own.
 *   4. Overlapping candidates are merged only when their edges agree; a
 *      clearly smaller box inside a bigger one is kept as a nested range.
 *   5. Every range that passed is kept; a breakout that ran at least one
 *      range height within 12 bars is labelled strong.
 *
 * Tiers: major (long, well tested, or the base of a strong breakout),
 * minor (smaller / nested), developing (still active and not major yet).
 * Pure function of the bars - the chart draws it, nothing trades on it.
 */
import type { Bar } from './types'

export type RangeTier = 'major' | 'minor' | 'developing'

export type RangeBox = {
  /** First / last bar of the box, chart indices. */
  i0: number
  i1: number
  t0: number
  t1: number
  top: number
  bot: number
  tier: RangeTier
  /** Still running at the last bar (no confirmed breakout yet). */
  active: boolean
  /** Which way it broke, when it has. */
  breakout: 'up' | 'down' | null
  /** The break carried at least STRONG_MOVE x the range height. */
  strong: boolean
  touchesTop: number
  touchesBot: number
}

// --- tuning --------------------------------------------------------------- //
const PIVOT_K = 3            // a swing high is the highest of k bars either side
const SWING_MIN_ATR = 0.6    // swings closer than this (x ATR) are noise
const MIN_H_ATR = 1.2        // shorter than this is a pause, not a range
const MAX_H_ATR = 6          // taller than this is a market, not a range
const MAX_SLOPE_ATR = 0.15   // max tilt of the range's EDGES (swing regression), x ATR per SLOPE_BARS
const SLOPE_BARS = 20
const MIN_BARS = 15
const FORMING_MIN_BARS = 8  // a range still forming (three edge tests so far)
// A swing is AT an edge within max(EDGE_ATR x ATR, EDGE_H x height). 15% of
// the height (was 30%, 2026-11-08): at 30% a tall box let a sweep 68 pts
// above the real top count as a top test (gold 4h 3 Sep 4510.8 vs 4443), and
// mid-range swings count as edge tests and tilt the edge slope.
const EDGE_ATR = 0.8      // 0.8 (was 0.6, 2026-11-08): gold 1h 23-27 Oct top tests 4034.1 / 4022.3 are 0.6 ATR apart
const EDGE_H = 0.15
const LOOK_SWINGS = 8        // the defining highs/lows must come within this many swings
const BREAK_ATR = 0.3        // TWO closes in a row this far past an edge is a breakout;
                             // a single close outside, however far, is not
const RECLAIM_BARS = 3       // a close back INSIDE within this many bars voids a breakout
const STRONG_BARS = 12       // a strong breakout gets...
const STRONG_MOVE = 1.0      // ...this many range heights (or STRONG_ATR) within STRONG_BARS
const STRONG_ATR = 2.5
const MAJOR_BARS = 48
const MAJOR_TOUCHES = 7
const SIMILAR = 0.25         // edges within this x height are "the same range"
const NESTED = 0.7           // a box this much shorter, inside another, is nested

type Swing = { i: number; p: number; hi: boolean }

export function atrSeries(b: Bar[]): number[] {
  const n = b.length
  const out = new Array<number>(n).fill(0)
  let sum = 0
  const tr = (i: number) => i === 0 ? b[0].h - b[0].l
    : Math.max(b[i].h - b[i].l, Math.abs(b[i].h - b[i - 1].c), Math.abs(b[i].l - b[i - 1].c))
  const q: number[] = []
  for (let i = 0; i < n; i++) {
    const v = tr(i)
    q.push(v); sum += v
    if (q.length > 14) sum -= q.shift()!
    out[i] = sum / q.length
  }
  return out
}

/** Fractal pivots, cleaned into an alternating zigzag with an ATR floor. */
export function swings(b: Bar[], atr: number[]): Swing[] {
  const n = b.length
  const raw: Swing[] = []
  for (let i = PIVOT_K; i < n - PIVOT_K; i++) {
    let isH = true, isL = true
    for (let k = i - PIVOT_K; k <= i + PIVOT_K; k++) {
      if (k === i) continue
      if (b[k].h > b[i].h) isH = false
      if (b[k].l < b[i].l) isL = false
    }
    if (isH) raw.push({ i, p: b[i].h, hi: true })
    if (isL) raw.push({ i, p: b[i].l, hi: false })
  }
  const zz: Swing[] = []
  for (const s of raw) {
    const last = zz[zz.length - 1]
    if (!last) { zz.push(s); continue }
    if (last.hi === s.hi) {
      // Same side twice: keep the more extreme one.
      if (s.hi ? s.p >= last.p : s.p <= last.p) zz[zz.length - 1] = s
      continue
    }
    if (Math.abs(s.p - last.p) < atr[s.i] * SWING_MIN_ATR) continue
    zz.push(s)
  }
  return zz
}

/**
 * Tilt of a range's EDGES, price per bar: one least-squares slope fitted to
 * the swing highs that touch the top and the swing lows that touch the
 * bottom together, each side about its own mean (2026-11-08). The closes
 * were used before, and a flat box whose closes leaned toward the breakout
 * side failed (gold 15m 2 Nov 3893-3915: closes 0.21, edges flat). A side
 * with fewer than two touches adds nothing; no side with two means 0.
 */
function edgeSlope(sw: Swing[], i0: number, i1: number, top: number, bot: number, tol: number): number {
  let num = 0, den = 0
  for (const hi of [true, false]) {
    const pts = sw.filter((x) => x.hi === hi && x.i >= i0 && x.i <= i1 &&
      Math.abs(x.p - (hi ? top : bot)) <= tol)
    if (pts.length < 2) continue
    let xm = 0, ym = 0
    for (const x of pts) { xm += x.i; ym += x.p }
    xm /= pts.length; ym /= pts.length
    for (const x of pts) { num += (x.i - xm) * (x.p - ym); den += (x.i - xm) * (x.i - xm) }
  }
  return den ? num / den : 0
}

export function findRanges(b: Bar[]): RangeBox[] {
  const n = b.length
  if (n < MIN_BARS + 20) return []
  const atr = atrSeries(b)
  const sw = swings(b, atr)
  const cands: RangeBox[] = []

  for (let s = 0; s < sw.length - 2; s++) {
    const a = atr[sw[s].i] || atr[n - 1]
    if (!(a > 0)) continue
    // The SHORTEST run of swings from here whose edges cluster: at least two
    // swing highs near the top and two swing lows near the bottom. Shortest,
    // so a later breakout swing never stretches the box. A run reaching the
    // latest swing with only three edge tests is a range still FORMING.
    let def: { top: number; bot: number; defEnd: number; forming: boolean } | null = null
    for (let len = 3; len <= LOOK_SWINGS && s + len <= sw.length; len++) {
      const win = sw.slice(s, s + len)
      const hs = win.filter((x) => x.hi), ls = win.filter((x) => !x.hi)
      if (!hs.length || !ls.length) continue
      const top = Math.max(...hs.map((x) => x.p)), bot = Math.min(...ls.map((x) => x.p))
      const h = top - bot
      if (h < a * MIN_H_ATR || h > a * MAX_H_ATR) continue
      // Highs AND lows stepping the same way is a channel, not a range.
      if (hs.length >= 2 && ls.length >= 2) {
        const dh = hs[hs.length - 1].p - hs[0].p, dl = ls[ls.length - 1].p - ls[0].p
        if (Math.sign(dh) === Math.sign(dl) && Math.abs(dh) > h * 0.5 && Math.abs(dl) > h * 0.5) continue
      }
      const tol = Math.max(a * EDGE_ATR, h * EDGE_H)
      const nt = hs.filter((x) => top - x.p <= tol), nb = ls.filter((x) => x.p - bot <= tol)
      const defEnd = win[win.length - 1].i
      // The edges must INTERLEAVE: price went back and forth between them -
      // a top test before the last bottom test and a bottom test before the
      // last top test. All bottom tests first, then all top tests, is a
      // staircase (a range, a breakout, a higher range), not one range
      // (gold 15m 4 Nov: 4030-4052 then 4055-4070 had boxed as 4029-4070).
      const interleaved = nt.length && nb.length &&
        nt[0].i < nb[nb.length - 1].i && nb[0].i < nt[nt.length - 1].i
      if (nt.length >= 2 && nb.length >= 2 && interleaved) { def = { top, bot, defEnd, forming: false }; break }
      if (s + len === sw.length && nt.length + nb.length >= 3) def = { top, bot, defEnd, forming: true }
    }
    if (!def) continue
    const { top, bot, defEnd, forming } = def
    const h = top - bot
    const i0 = sw[s].i
    // Nothing may have closed outside while the edges were being drawn.
    let clean = true
    for (let k = i0; k <= defEnd; k++) {
      if (b[k].c > top + a * BREAK_ATR || b[k].c < bot - a * BREAK_ATR) { clean = false; break }
    }
    if (!clean) continue

    // Extend until a confirmed breakout.
    let i1 = n - 1
    let breakout: 'up' | 'down' | null = null
    for (let k = defEnd + 1; k < n; k++) {
      const ak = atr[k] || a
      const upBy = b[k].c - top, dnBy = bot - b[k].c
      const prevUp = k > 0 && b[k - 1].c - top > ak * BREAK_ATR
      const prevDn = k > 0 && bot - b[k - 1].c > ak * BREAK_ATR
      const dir = upBy > ak * BREAK_ATR && prevUp ? 'up' : dnBy > ak * BREAK_ATR && prevDn ? 'down' : null
      if (!dir) continue
      // A FAILED breakout - price closes back inside the box within
      // RECLAIM_BARS - is a false break, and the range carries on (gold 2h
      // 7 Oct: two closes under 4110.9, back inside five bars later, then two
      // more weeks of range). Bars not yet printed cannot reclaim anything,
      // so on the latest bars the break stands until they do.
      let back = -1
      for (let q = k + 1; q <= Math.min(n - 1, k + RECLAIM_BARS); q++) {
        // 'Inside' on the same buffer as the breakout: a close within
        // BREAK_ATR x ATR of the edge is not outside, so it reclaims.
        const aq = atr[q] || a
        if (b[q].c >= bot - aq * BREAK_ATR && b[q].c <= top + aq * BREAK_ATR) { back = q; break }
      }
      if (back >= 0) { k = back; continue }
      breakout = dir; i1 = k - 2; break
    }
    // A forming range that already broke never was one.
    if (forming && breakout) continue
    if (i1 - i0 + 1 < (forming ? FORMING_MIN_BARS : MIN_BARS)) continue
    // A range is FLAT: its edges tilt at most MAX_SLOPE_ATR x ATR per
    // SLOPE_BARS bars, over its whole life.
    const t2 = Math.max(a * EDGE_ATR, h * EDGE_H)
    if (Math.abs(edgeSlope(sw, i0, i1, top, bot, t2)) * SLOPE_BARS > a * MAX_SLOPE_ATR) continue

    let strong = false
    if (breakout) {
      const lim = Math.min(n - 1, i1 + STRONG_BARS)
      let best = 0
      for (let k = i1 + 1; k <= lim; k++) {
        best = Math.max(best, breakout === 'up' ? b[k].c - top : bot - b[k].c)
      }
      strong = best >= Math.min(h * STRONG_MOVE * 2, Math.max(h * STRONG_MOVE, a * STRONG_ATR))
    }

    // Touches: swings inside the box that came back to an edge.
    let touchesTop = 0, touchesBot = 0
    for (const x of sw) {
      if (x.i < i0 || x.i > i1) continue
      if (x.hi && Math.abs(x.p - top) <= t2) touchesTop++
      if (!x.hi && Math.abs(x.p - bot) <= t2) touchesBot++
    }

    cands.push({
      i0, i1, t0: b[i0].t, t1: b[i1].t, top, bot, tier: 'minor',
      active: breakout === null, breakout, strong, touchesTop, touchesBot,
    })
  }

  // --- merge / nest ------------------------------------------------------ //
  const score = (r: RangeBox) => (r.touchesTop + r.touchesBot) * 10 + (r.i1 - r.i0)
  cands.sort((x, y) => score(y) - score(x))
  const kept: RangeBox[] = []
  for (const r of cands) {
    let absorbed = false
    for (const k of kept) {
      const ov = Math.min(k.i1, r.i1) - Math.max(k.i0, r.i0)
      if (ov <= 0) continue
      const hk = k.top - k.bot, hr = r.top - r.bot
      const avg = (hk + hr) / 2
      const similar = Math.abs(k.top - r.top) <= avg * SIMILAR && Math.abs(k.bot - r.bot) <= avg * SIMILAR
      if (similar) {
        // Same range found from another swing: widen the span, keep the edges
        // of the better-tested one, and carry the earlier start.
        if (r.i0 < k.i0) { k.i0 = r.i0; k.t0 = r.t0 }
        absorbed = true
        break
      }
      const nested = hr <= hk * NESTED && r.top <= k.top + hk * 0.1 && r.bot >= k.bot - hk * 0.1
      const contains = hk <= hr * NESTED && k.top <= r.top + hr * 0.1 && k.bot >= r.bot - hr * 0.1
      // Nested only counts when the smaller box lives a different stretch of
      // time; two boxes over (nearly) the same bars are one range drawn twice.
      const same = ov >= 0.8 * Math.max(k.i1 - k.i0, r.i1 - r.i0)
      if ((nested || contains) && !same) continue
      if (same) { absorbed = true; break }
      // Overlapping but neither the same nor nested: the better one wins.
      // A small overlap between neighbours (one range handing over to the
      // next) is trimmed off the weaker one rather than drawn twice.
      const share = ov / Math.min(k.i1 - k.i0, r.i1 - r.i0)
      if (share > 0.3) { absorbed = true; break }
      if (r.i0 >= k.i0) r.i0 = k.i1 + 1
      else r.i1 = k.i0 - 1
      if (r.i1 - r.i0 + 1 < MIN_BARS) { absorbed = true; break }
      r.t0 = b[r.i0].t; r.t1 = b[r.i1].t
    }
    if (!absorbed) kept.push(r)
  }

  // --- tiers and keep rules ---------------------------------------------- //
  // A range inside another one is MINOR by definition (it is the smaller
  // structure); only top-level ranges can be major.
  const inside = (r: RangeBox, m: RangeBox) => {
    const hm = m.top - m.bot
    return m !== r && r.i0 >= m.i0 && r.i1 <= m.i1 && (r.top - r.bot) <= hm * NESTED &&
      r.top <= m.top + hm * 0.1 && r.bot >= m.bot - hm * 0.1
  }
  for (const r of kept) {
    const len = r.i1 - r.i0 + 1
    const touches = r.touchesTop + r.touchesBot
    const nestedIn = kept.some((m) => inside(r, m))
    const major = !nestedIn &&
      ((len >= MAJOR_BARS && touches >= MAJOR_TOUCHES) || (r.strong && len >= MIN_BARS * 2))
    r.tier = major ? 'major' : r.active ? 'developing' : 'minor'
  }
  // Every range that passed detection is kept (2026-11-08). Dropping a
  // completed minor range unless its breakout was "strong" removed real
  // ranges (gold 15m 4 Nov 4029-4052 missed by 3 points); `strong` now only
  // labels the breakout.
  const out = kept
  return out.sort((x, y) => x.i0 - y.i0)
}
