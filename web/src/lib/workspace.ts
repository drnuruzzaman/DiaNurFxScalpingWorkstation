/**
 * The workspace - open charts (instrument + timeframe per tab, which tab is
 * active), the indicator and layout selection, watchlist, theme - kept on the
 * SERVER, not just in the browser.
 *
 * Everything below was already remembered in localStorage, and that turned out
 * not to be enough: localStorage belongs to an origin, so 127.0.0.1:5180 and
 * localhost:5180 are two unrelated stores, and a new browser or cleared site
 * data starts from nothing. The workspace looked "lost" when it was only ever
 * in the other box.
 *
 * So localStorage stays the working copy - every component keeps reading it
 * synchronously exactly as before - and this module mirrors it to
 * configs/workspace.json:
 *
 *   startup  hydrate() pulls the server copy INTO localStorage before React
 *            renders, so every useState initializer reads the saved workspace.
 *   change   scheduleSave() pushes the lot a moment after the last edit.
 *   close    a beacon flushes anything still pending.
 */

/** What makes up a workspace. Deliberately explicit: a stray key must not ride along. */
const KEYS = [
  'dianur.tabs.v2',        // open charts: [{ id, symbol, tf }]
  'dianur.activeTab',      // which of them is in front
  'dianur.overlays',       // indicators
  'dianur.layout',         // chart furniture
  'dianur.mtfSources',     // which higher-timeframe lines are drawn
  'dianur.hideFigures',
  'dianur.watchlist',
  'dianur.theme',
  'dianur.snapPanels.v2',
  'dianur.panels',         // which of left / right / bottom are open
  'dianur.dockTab',        // the bottom panel's open tab
  'dianur.boardSort',      // signal board column sort
  'dianur.execSort',       // execution tab column sort
  'dianur.chartPresets',   // saved workspace per instrument + timeframe
  'dianur.lab.overlays',   // the backtest lab keeps its own studies
  'dianur.lab.dock',       // the lab's open research tab
  'dianur.lab.dockH',      // and how tall it is
]

const URL = '/api/workspace'

function collect(): Record<string, unknown> {
  const values: Record<string, unknown> = {}
  for (const k of KEYS) {
    try {
      const raw = localStorage.getItem(k)
      if (raw == null) continue
      values[k] = JSON.parse(raw)
    } catch { /* unreadable or private mode: leave it out */ }
  }
  return values
}

/**
 * Load the saved workspace into localStorage. Resolves quickly either way:
 * with the server down (or still running a build without this endpoint) the
 * app starts from whatever this browser already had, rather than not at all.
 */
export async function hydrate(timeoutMs = 2000): Promise<'server' | 'seeded' | 'local'> {
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), timeoutMs)
  try {
    const r = await fetch(URL, { signal: ctl.signal })
    if (!r.ok) return 'local'
    const body = await r.json()
    const values = body && typeof body.values === 'object' ? body.values : null
    if (!values || !Object.keys(values).length) {
      // First run against a server with nothing saved: this browser's
      // workspace becomes the saved one.
      saveNow()
      return 'seeded'
    }
    for (const k of KEYS) {
      if (!(k in values)) continue
      try { localStorage.setItem(k, JSON.stringify(values[k])) } catch { /* private mode */ }
    }
    return 'server'
  } catch {
    // Not JSON (an older API serves the app's HTML here), offline, or timed out.
    return 'local'
  } finally {
    clearTimeout(timer)
  }
}

let timer: number | null = null

/** Push the workspace shortly after the last change; edits in a burst coalesce. */
export function scheduleSave(delayMs = 800): void {
  if (timer != null) clearTimeout(timer)
  timer = window.setTimeout(() => { timer = null; saveNow() }, delayMs)
}

export function saveNow(): void {
  fetch(URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values: collect() }),
  }).catch(() => { /* next change retries */ })
}

// A pending save must survive the tab closing. sendBeacon is the one request a
// page is allowed to make on its way out.
window.addEventListener('pagehide', () => {
  if (timer == null) return
  clearTimeout(timer)
  timer = null
  try {
    navigator.sendBeacon(URL, new Blob([JSON.stringify({ values: collect() })],
      { type: 'application/json' }))
  } catch { /* nothing else can be done at this point */ }
})
