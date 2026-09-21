/** Shared formatting + small presentational helpers used across panels. */

export const fmt = (n: number | null | undefined, d = 2): string =>
  n === null || n === undefined || !Number.isFinite(n)
    ? '—'
    : n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })

export const signed = (n: number | null | undefined, d = 0): string =>
  n === null || n === undefined || !Number.isFinite(n) ? '—' : (n > 0 ? '+' : '') + fmt(n, d)

export const pct = (n: number | null | undefined, d = 1): string =>
  n === null || n === undefined || !Number.isFinite(n) ? '—' : `${fmt(n, d)}%`

/** Colour class for a signed number. */
export const dirClass = (n: number | null | undefined): string =>
  !Number.isFinite(n as number) || n === 0 ? 't-mid' : (n as number) > 0 ? 't-up' : 't-down'

export const clockUTC = (ms: number): string => {
  const d = new Date(ms)
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}:${String(d.getUTCSeconds()).padStart(2, '0')}`
}

export const dateUTC = (ms: number): string => {
  const d = new Date(ms)
  return `${String(d.getUTCDate()).padStart(2, '0')} ${d.toLocaleString('en', { month: 'short', timeZone: 'UTC' })} ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
}

export const ago = (ms: number): string => {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.round(s / 60)}m`
  if (s < 86400) return `${Math.round(s / 3600)}h`
  return `${Math.round(s / 86400)}d`
}

/** Colour for a bull/bear/neutral direction word. */
export const biasClass = (b?: string): string =>
  b === 'bullish' || b === 'buy' || b === 'up' ? 't-up'
    : b === 'bearish' || b === 'sell' || b === 'down' ? 't-down'
      : 't-mid'

export const chipFor = (b?: string): string =>
  b === 'bullish' || b === 'buy' || b === 'up' ? 'chip chip-up'
    : b === 'bearish' || b === 'sell' || b === 'down' ? 'chip chip-down'
      : 'chip chip-mute'

/**
 * Profit factor, which is genuinely undefined (not infinite) when a bucket has
 * no losing trades. The backend sends null for that case.
 */
export const profitFactor = (n: number | null | undefined): string =>
  n === null || n === undefined ? '∞' : fmt(n, 2)

/**
 * Chip class for a signal's lifecycle stage. FORMING is deliberately muted:
 * it is on an open bar and may vanish at the close, so it must not look
 * like something to act on.
 */
export function stageChip(stage?: string): string {
  switch (stage) {
    case 'FINAL': return 'chip chip-info'
    case 'SENT': return 'chip chip-warn'
    case 'FILLED': return 'chip chip-up'
    case 'CLOSED': return 'chip chip-mid'
    case 'EXPIRED': case 'CANCELLED': case 'REVERSED': return 'chip chip-down'
    default: return 'chip chip-mute'
  }
}
