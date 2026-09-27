import type { Bar } from '../chart/types'

/**
 * Bars already fetched, kept per symbol+timeframe so that switching tabs
 * repaints instead of re-downloading.
 *
 * Two layers, on purpose:
 *
 *   - An in-memory Map. This is what actually fixes tab switching, which is a
 *     within-session action. It is exact, costs nothing to write, and never
 *     needs serialising.
 *   - A localStorage mirror, so the first paint after a reload is warm too.
 *     Written on a debounce because serialising a few thousand bars on every
 *     socket tick would cost more than the download it saves.
 *
 * The cache is only ever a HEAD START. Bars from it are merged with whatever
 * the live socket sends, exactly like bars fetched by scrolling back, so a
 * stale tail is corrected within a tick rather than believed.
 */

// v3/v4: series cached before 2026-09-26 could hold disk bars in broker time
// beside UTC live ones (3 hours apart, one day drawn twice), and a timeframe
// switched away from could land its bars in the next one's series (a 4H chart
// of 1-minute candles, or a stray block of older bars scrolled in from another
// chart's dates). A new key drops them rather than drawing them again.
const KEY = 'dianur.bars.v4'
const OLD_KEYS = ['dianur.bars.v1', 'dianur.bars.v2', 'dianur.bars.v3']

/** Per series. Enough for a deep scroll-back; beyond this, re-fetch. */
const MAX_BARS = 1500
/** Distinct series kept. A few more than anyone keeps tabs open. */
const MAX_SERIES = 12
/**
 * Past this, a persisted series is dropped rather than drawn. Not because the
 * bars go wrong - old bars stay true - but because a day-old tail next to a
 * live price reads as a gap the user has to explain to themselves.
 */
const MAX_AGE_MS = 12 * 60 * 60 * 1000

type Entry = { bars: Bar[]; at: number }

const mem = new Map<string, Entry>()

export const keyFor = (symbol: string, tf: string) => `${symbol}|${tf}`

let loaded = false

function hydrate(): void {
  if (loaded) return
  loaded = true
  try { for (const k of OLD_KEYS) localStorage.removeItem(k) } catch { /* private mode */ }
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return
    const obj = JSON.parse(raw) as Record<string, Entry>
    const now = Date.now()
    for (const [k, e] of Object.entries(obj)) {
      if (!e || !Array.isArray(e.bars) || !e.bars.length) continue
      if (now - (e.at ?? 0) > MAX_AGE_MS) continue
      mem.set(k, e)
    }
  } catch {
    // A corrupt or oversized blob is not worth recovering from; the socket
    // will refill it within a second.
    try { localStorage.removeItem(KEY) } catch { /* private mode */ }
  }
}

/**
 * Only the bars that belong to a series of `stepMs` bars: on the newest bar's
 * grid. Every bar of one series opens a whole number of steps from every
 * other; a bar off that grid is from another timeframe, or on another clock.
 */
export function onGrid(bars: Bar[], stepMs: number): Bar[] {
  if (!stepMs || bars.length < 2) return bars
  const last = bars[bars.length - 1].t
  const kept = bars.filter((b) => (last - b.t) % stepMs === 0)
  return kept.length === bars.length ? bars : kept
}

/** Bars held for this series, or an empty array. Never null - callers paint it. */
export function get(symbol: string, tf: string): Bar[] {
  hydrate()
  const e = mem.get(keyFor(symbol, tf))
  if (!e) return []
  if (Date.now() - e.at > MAX_AGE_MS) { mem.delete(keyFor(symbol, tf)); return [] }
  return e.bars
}

export function put(symbol: string, tf: string, bars: Bar[]): void {
  if (!bars.length) return
  hydrate()
  mem.set(keyFor(symbol, tf), {
    bars: bars.length > MAX_BARS ? bars.slice(-MAX_BARS) : bars,
    at: Date.now(),
  })
  schedule()
}

let timer: number | null = null

function schedule(): void {
  if (timer != null) return
  timer = window.setTimeout(() => { timer = null; flush() }, 4000)
}

/** Write the mirror. Quota failures shrink the cache rather than throw. */
function flush(): void {
  // Newest series win the space.
  const entries = [...mem.entries()].sort((a, b) => b[1].at - a[1].at)
  let keep = entries.slice(0, MAX_SERIES)
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      localStorage.setItem(KEY, JSON.stringify(Object.fromEntries(keep)))
      return
    } catch {
      // QuotaExceeded. Halve what we try to persist and go again; the
      // in-memory map is untouched, so tab switching still works.
      keep = keep.slice(0, Math.floor(keep.length / 2))
      if (!keep.length) {
        try { localStorage.removeItem(KEY) } catch { /* private mode */ }
        return
      }
    }
  }
}

/** Persist immediately - used when the page is going away. */
export function flushNow(): void {
  if (timer != null) { clearTimeout(timer); timer = null }
  flush()
}
