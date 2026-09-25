/**
 * ChartMenus - the Indicators and Layout menus, shared by the live chart and
 * the backtest lab so both offer the same studies, drawn the same way.
 *
 * Each chart keeps its OWN selection (the lab's is stored separately), so
 * switching a study on while reviewing history never changes the live view.
 */
import React from 'react'
import { DEFAULT_OVERLAYS, type LayoutOpts, type Overlays } from './types'

/**
 * The indicator catalogue, split the way the chart itself is split: things
 * drawn ON the price pane, and things that get a pane of their own.
 *
 * Each row carries a short hint saying what the overlay actually keys off,
 * because "Channels" and "Liquidity" are not self-explanatory and the cost of
 * guessing wrong is reading a level that means something else.
 */
export type OverlayRow = { key: keyof Overlays; label: string; hint?: string }

export const OVERLAY_GROUPS: { title: string; rows: OverlayRow[] }[] = [
  {
    title: 'Overlays',
    rows: [
      { key: 'levels', label: 'S/R levels', hint: 'price turned repeatedly' },
      { key: 'trendlines', label: 'Trendlines', hint: 'swing-anchored' },
      { key: 'channels', label: 'Channels', hint: 'parallel corridor' },
      { key: 'patterns', label: 'Patterns', hint: 'wedges, tops, H&S' },
      { key: 'swings', label: 'Swing points', hint: 'HH / HL / LH / LL' },
      { key: 'structure', label: 'BOS / CHoCH', hint: 'structure breaks' },
      { key: 'liquidity', label: 'Liquidity', hint: 'resting stops' },
      { key: 'events', label: 'Events', hint: 'macro releases' },
      { key: 'signal', label: 'Signal levels', hint: 'entry / stop / targets' },
      { key: 'zigzag', label: 'ZigZag line', hint: 'swing skeleton' },
      { key: 'regimeBands', label: 'Regime ribbon', hint: 'trending / ranging runs' },
      { key: 'engulfing', label: 'Engulfing candles', hint: 'body covers the prior body' },
    ],
  },
  {
    // Bands, not lines - kept in their own group because switching two of
    // them on at once is already a lot of chart, and that is easier to see
    // when they are listed together.
    title: 'Areas',
    rows: [
      { key: 'zones', label: 'Supply / demand', hint: 'bases strong moves left' },
      { key: 'fvg', label: 'Fair value gaps', hint: 'unfilled imbalances' },
      { key: 'fib', label: 'Fibonacci', hint: 'retracement of the last leg' },
    ],
  },
  {
    title: 'Higher timeframe',
    rows: [
      { key: 'mtfTrendlines', label: 'Projected trendlines', hint: 'from coarser frames' },
    ],
  },
  {
    title: 'Panes',
    rows: [
      { key: 'volume', label: 'Volume' },
      { key: 'rsi', label: 'RSI + divergence', hint: 'regular & hidden' },
      { key: 'macd', label: 'MACD', hint: '12 / 26 / 9' },
    ],
  },
]

export const OVERLAY_KEYS = OVERLAY_GROUPS.flatMap((g) => g.rows.map((r) => r.key))

export const THEMES: { key: string; label: string; hint: string }[] = [
  { key: 'midnight', label: 'Midnight', hint: 'near-black navy' },
  { key: 'navy', label: 'Navy', hint: 'blue, brighter' },
  { key: 'carbon', label: 'Carbon', hint: 'neutral grey' },
  { key: 'glossy', label: 'Glossy', hint: 'Ausloans, lacquered' },
]

export const LAYOUT_ROWS: { key: keyof LayoutOpts; label: string; hint: string }[] = [
  { key: 'grid', label: 'Show grid', hint: 'on axis ticks' },
  { key: 'newsMarks', label: 'News marks', hint: 'high impact only' },
  { key: 'positions', label: 'Trade positions', hint: 'open entries' },
  { key: 'legRead', label: 'Leg read', hint: 'avoid-fade rules' },
]

/** The Indicators dropdown. `children` adds sections above the footer. */
export function IndicatorsMenu({ overlays, onChange, exclude = [], children }: {
  overlays: Overlays
  onChange: (next: Overlays) => void
  /** Studies this chart cannot draw (the lab has no projected HTF lines). */
  exclude?: (keyof Overlays)[]
  children?: React.ReactNode
}) {
  return (
    <div className="menu" role="menu">
      {OVERLAY_GROUPS.map((g) => {
        const rows = g.rows.filter((r) => !exclude.includes(r.key))
        if (!rows.length) return null
        return (
          <React.Fragment key={g.title}>
            <div className="menu-head">{g.title}</div>
            {rows.map((r) => {
              const on = overlays[r.key]
              return (
                <button key={r.key} className={`menu-item ${on ? 'on' : ''}`}
                  role="menuitemcheckbox" aria-checked={on}
                  onClick={() => onChange({ ...overlays, [r.key]: !on })}>
                  <span className="menu-check">{on ? '\u2713' : ''}</span>
                  <span className="menu-label">{r.label}</span>
                  {r.hint && <span className="menu-hint">{r.hint}</span>}
                </button>
              )
            })}
          </React.Fragment>
        )
      })}
      {children}

      <div className="menu-sep" />
      <div className="menu-note">
        This selection is saved and reloads with the chart.
      </div>
      <div className="menu-foot">
        <button className="menu-action" onClick={() => onChange(
          OVERLAY_KEYS.reduce((a, k) => ({ ...a, [k]: false }), { ...overlays } as Overlays))}>
          Remove all studies
        </button>
        {/* Removing everything with no way back is a one-way door -
            the defaults are a specific, considered set. */}
        <button className="menu-action dim"
          title="Discard this setup and go back to the built-in selection"
          onClick={() => onChange(DEFAULT_OVERLAYS)}>Reset</button>
      </div>
    </div>
  )
}

/** The Layout dropdown: chart furniture, then the theme. */
export function LayoutMenu({ layout, onToggle, rows = LAYOUT_ROWS, theme, onTheme, note }: {
  layout: LayoutOpts
  onToggle: (key: keyof LayoutOpts, on: boolean) => void
  rows?: typeof LAYOUT_ROWS
  theme?: string
  onTheme?: (key: string) => void
  note?: React.ReactNode
}) {
  return (
    <div className="menu" role="menu" style={{ left: 'auto', right: 0, minWidth: 236 }}>
      <div className="menu-head">Chart</div>
      {rows.map((r) => {
        const on = layout[r.key]
        return (
          <button key={r.key} className={`menu-item ${on ? 'on' : ''}`}
            role="menuitemcheckbox" aria-checked={on}
            onClick={() => onToggle(r.key, !on)}>
            <span className="menu-check">{on ? '\u2713' : ''}</span>
            <span className="menu-label">{r.label}</span>
            <span className="menu-hint">{r.hint}</span>
          </button>
        )
      })}
      {theme !== undefined && onTheme && (
        <>
          <div className="menu-head">Theme</div>
          {THEMES.map((t) => (
            <button key={t.key} className={`menu-item ${theme === t.key ? 'on' : ''}`}
              role="menuitemradio" aria-checked={theme === t.key}
              onClick={() => onTheme(t.key)}>
              <span className="menu-check">{theme === t.key ? '\u2713' : ''}</span>
              <span className="menu-label">{t.label}</span>
              <span className="menu-hint">{t.hint}</span>
            </button>
          ))}
        </>
      )}
      {note && <div className="menu-note">{note}</div>}
    </div>
  )
}
