import React, { useEffect, useRef, useState } from 'react'
import { PATTERN_TOOLS, STYLE_COLOR, STYLE_LINE, TOOL_LABEL, type Drawing, type LineDash, type ToolId } from './drawings'

/**
 * The left drawing toolbar (2026-11-12), TradingView-style: pick a tool, click
 * the chart to place its points (or press-drag-release for two-point tools).
 * Escape cancels, Delete removes the selected drawing, double-click a text to
 * edit it. Magnet snaps points to the bar's open / high / low / close.
 */

const I = (d: React.ReactNode) => (
  <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor"
    strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>
)

export const TOOL_ICON: Record<ToolId | 'cursor', React.ReactNode> = {
  cursor: I(<><path d="M9 2v14M2 9h14" /></>),
  trend: I(<><path d="M3.5 14.5l11-11" /><circle cx="3.5" cy="14.5" r="1.4" /><circle cx="14.5" cy="3.5" r="1.4" /></>),
  hline: I(<><path d="M2 9h14" /><circle cx="9" cy="9" r="1.4" /></>),
  vline: I(<><path d="M9 2v14" /><circle cx="9" cy="9" r="1.4" /></>),
  fib: I(<><path d="M2 3.5h14M2 7h14M2 10.5h14M2 14.5h14" /><path d="M3 14.5L15 3.5" strokeDasharray="1.5 2" /></>),
  ruler: I(<><rect x="1.8" y="6" width="14.4" height="6" rx="1" transform="rotate(-35 9 9)" /><path d="M6 7.6l.9 1.3M8.3 6l.9 1.3M10.6 4.4l.9 1.3" /></>),
  text: I(<><path d="M4 4h10M9 4v11M7 15h4" /></>),
  brush: I(<><path d="M2.5 13c2-4 4 1 6-3s3.5-5 7-6" /></>),
  long: I(<><rect x="3" y="3" width="12" height="6" fill="currentColor" fillOpacity=".18" /><rect x="3" y="9" width="12" height="5" /><path d="M9 12.5V5.5M6.8 7.6L9 5.5l2.2 2.1" /></>),
  short: I(<><rect x="3" y="9" width="12" height="6" fill="currentColor" fillOpacity=".18" /><rect x="3" y="4" width="12" height="5" /><path d="M9 5.5v7M6.8 10.4L9 12.5l2.2-2.1" /></>),
  xabcd: I(<><path d="M2 13l3.5-9 3 6 3-4 3.5 8" /><circle cx="2" cy="13" r="1" /><circle cx="15" cy="14" r="1" /></>),
  cypher: I(<><path d="M2 12l3-8 3 5 4-7 4 12" /></>),
  abcd: I(<><path d="M2 14l5-10 3 5 5-6" /></>),
  hs: I(<><path d="M1.5 13l3-5 2 3 2.5-7 2.5 7 2-3 3 5" /><path d="M2 11.5h14" strokeDasharray="1.5 2" /></>),
  triangle: I(<><path d="M2 3l4 10 3-7 3 5 3-3" /><path d="M2 3l14 7M6 13l10-4" strokeDasharray="1.5 2" /></>),
  threedrives: I(<><path d="M1.5 15l3-5 2 2 3-5 2 2 4.5-6" /></>),
}

const ICON_MAGNET = I(<><path d="M4 3v6a5 5 0 0010 0V3" /><path d="M4 6h3M11 6h3" /><path d="M7 3v6a2 2 0 004 0V3" /></>)
const ICON_LOCK = (on: boolean) => I(<><rect x="4" y="8" width="10" height="7.5" rx="1.2" />
  <path d={on ? 'M6 8V5.5a3 3 0 016 0V8' : 'M6 8V5.5a3 3 0 015.8-1'} /></>)
const ICON_EYE = (hidden: boolean) => I(<><path d="M1.8 9S4.5 4 9 4s7.2 5 7.2 5-2.7 5-7.2 5-7.2-5-7.2-5z" /><circle cx="9" cy="9" r="2.2" />
  {hidden && <path d="M3 15L15 3" />}</>)
const ICON_TRASH = I(<><path d="M3 5h12M7 5V3.5h4V5M5 5l.8 10h6.4L13 5" /></>)

export type DrawFlags = { magnet: boolean; locked: boolean; hidden: boolean }

const FLAGS_KEY = 'dianurfx.drawflags.v1'
export function loadFlags(): DrawFlags {
  try {
    const f = JSON.parse(localStorage.getItem(FLAGS_KEY) || '{}')
    return { magnet: !!f.magnet, locked: !!f.locked, hidden: !!f.hidden }
  } catch { return { magnet: false, locked: false, hidden: false } }
}
export function saveFlags(f: DrawFlags) {
  try { localStorage.setItem(FLAGS_KEY, JSON.stringify(f)) } catch { /* per-session only */ }
}

export function DrawToolbar({ tool, count, flags, onTool, onFlags, onClear }: {
  tool: ToolId | null
  count: number
  flags: DrawFlags
  onTool: (t: ToolId | null) => void
  onFlags: (f: DrawFlags) => void
  onClear: () => void
}) {
  const [menu, setMenu] = useState<'patterns' | null>(null)
  const [lastPattern, setLastPattern] = useState<ToolId>('xabcd')
  const [armClear, setArmClear] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!menu) return
    const off = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setMenu(null) }
    window.addEventListener('mousedown', off)
    return () => window.removeEventListener('mousedown', off)
  }, [menu])
  useEffect(() => {
    if (!armClear) return
    const t = window.setTimeout(() => setArmClear(false), 3000)
    return () => window.clearTimeout(t)
  }, [armClear])

  const Btn = ({ t }: { t: ToolId | null }) => (
    <button className={`draw-btn ${tool === t ? 'on' : ''}`}
      title={t ? TOOL_LABEL[t] : 'Cursor'}
      onClick={() => onTool(tool === t ? null : t)}>
      {TOOL_ICON[t ?? 'cursor']}
    </button>
  )
  const patternOn = tool != null && PATTERN_TOOLS.includes(tool)

  return (
    <div className="draw-bar" ref={box}>
      <Btn t={null} />
      <Btn t="trend" />
      <Btn t="hline" />
      <Btn t="vline" />
      <Btn t="fib" />
      <div className="draw-group">
        <button className={`draw-btn ${patternOn ? 'on' : ''}`}
          title={`${TOOL_LABEL[patternOn ? tool! : lastPattern]} - more patterns ▸`}
          onClick={() => setMenu(menu === 'patterns' ? null : 'patterns')}>
          {TOOL_ICON[patternOn ? tool! : lastPattern]}
          <i className="draw-more" />
        </button>
        {menu === 'patterns' && (
          <div className="draw-menu" role="menu">
            <div className="draw-menu-h">Patterns</div>
            {PATTERN_TOOLS.map((p) => (
              <button key={p} className={`draw-menu-item ${tool === p ? 'on' : ''}`}
                onClick={() => { setLastPattern(p); setMenu(null); onTool(p) }}>
                {TOOL_ICON[p]}<span>{TOOL_LABEL[p]}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      <Btn t="long" />
      <Btn t="short" />
      <Btn t="brush" />
      <Btn t="text" />
      <Btn t="ruler" />
      <div className="draw-sep" />
      <button className={`draw-btn ${flags.magnet ? 'on' : ''}`}
        title={`Magnet ${flags.magnet ? 'on' : 'off'} - snap points to the bar's open, high, low or close`}
        onClick={() => onFlags({ ...flags, magnet: !flags.magnet })}>{ICON_MAGNET}</button>
      <button className={`draw-btn ${flags.locked ? 'on' : ''}`}
        title={flags.locked ? 'Drawings locked - click to unlock' : 'Lock all drawings'}
        onClick={() => onFlags({ ...flags, locked: !flags.locked })}>{ICON_LOCK(flags.locked)}</button>
      <button className={`draw-btn ${flags.hidden ? 'on' : ''}`}
        title={flags.hidden ? 'Drawings hidden - click to show' : 'Hide all drawings'}
        onClick={() => onFlags({ ...flags, hidden: !flags.hidden })}>{ICON_EYE(flags.hidden)}</button>
      <button className={`draw-btn ${armClear ? 'danger' : ''}`} disabled={!count}
        title={armClear ? `Click again to delete all ${count} drawings on this symbol` : 'Delete all drawings on this symbol'}
        onClick={() => { if (armClear) { setArmClear(false); onClear() } else setArmClear(true) }}>{ICON_TRASH}</button>
    </div>
  )
}

// ------------------------------------------------------------------ //
// the bar over a selected drawing: colour, width, line style, delete  //
// ------------------------------------------------------------------ //
const SWATCHES = ['#2962ff', '#35d6ef', '#9fe547', '#089981', '#ffb02e', '#ff9800',
  '#ff2d78', '#f23645', '#c264ff', '#ffffff', '#9aa4b8', '#55617a']
const WIDTHS = [1, 2, 3, 4]
const DASHES: { key: LineDash; label: string; arr: string }[] = [
  { key: 'solid', label: 'Solid', arr: '' },
  { key: 'dashed', label: 'Dashed', arr: '6 4' },
  { key: 'dotted', label: 'Dotted', arr: '1.5 3' },
]
const DEFAULT_COLOR: Partial<Record<ToolId, string>> = { brush: '#c264ff', text: '#b6c1d6' }
const PATTERN_DEFAULT = '#c264ff'

const dashLine = (arr: string, w = 1.6) => (
  <svg width="22" height="10" viewBox="0 0 22 10" aria-hidden="true">
    <path d="M1 5h20" stroke="currentColor" strokeWidth={w} strokeDasharray={arr} strokeLinecap="round" />
  </svg>
)

export function SelectionBar({ drawing, onStyle, onDelete }: {
  drawing: Drawing
  onStyle: (p: Partial<Pick<Drawing, 'color' | 'width' | 'dash'>>) => void
  onDelete: () => void
}) {
  const [open, setOpen] = useState<'color' | 'width' | 'dash' | null>(null)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const off = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(null) }
    window.addEventListener('mousedown', off)
    return () => window.removeEventListener('mousedown', off)
  }, [open])
  useEffect(() => { setOpen(null) }, [drawing.id])

  const t = drawing.tool
  const hasColor = STYLE_COLOR.includes(t)
  const hasLine = STYLE_LINE.includes(t)
  const color = drawing.color ?? DEFAULT_COLOR[t] ?? (PATTERN_TOOLS.includes(t) ? PATTERN_DEFAULT : '#35d6ef')
  const width = drawing.width ?? 2
  const dash = drawing.dash ?? 'solid'
  const dashArr = DASHES.find((d) => d.key === dash)?.arr ?? ''

  return (
    <div className="sel-bar" ref={box} onMouseDown={(e) => e.stopPropagation()}>
      {hasColor && (
        <div className="sel-item">
          <button className={`sel-btn ${open === 'color' ? 'on' : ''}`} title="Colour"
            onClick={() => setOpen(open === 'color' ? null : 'color')}>
            {I(<><path d="M12.5 2.5l3 3-8 8H4.5v-3z" /></>)}
            <i className="sel-swatch-bar" style={{ background: color }} />
          </button>
          {open === 'color' && (
            <div className="sel-pop sel-swatches">
              {SWATCHES.map((c) => (
                <button key={c} className={`sel-swatch ${c === drawing.color ? 'on' : ''}`} style={{ background: c }}
                  title={c} onClick={() => { onStyle({ color: c }); setOpen(null) }} />
              ))}
            </div>
          )}
        </div>
      )}
      {hasLine && (
        <div className="sel-item">
          <button className={`sel-btn wide ${open === 'width' ? 'on' : ''}`} title="Line width"
            onClick={() => setOpen(open === 'width' ? null : 'width')}>
            {dashLine('', Math.min(4, width))}<span>{width}px</span>
          </button>
          {open === 'width' && (
            <div className="sel-pop">
              {WIDTHS.map((w) => (
                <button key={w} className={`sel-row ${w === width ? 'on' : ''}`}
                  onClick={() => { onStyle({ width: w }); setOpen(null) }}>
                  {dashLine('', w)}<span>{w}px</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
      {hasLine && (
        <div className="sel-item">
          <button className={`sel-btn ${open === 'dash' ? 'on' : ''}`} title="Line style"
            onClick={() => setOpen(open === 'dash' ? null : 'dash')}>
            {dashLine(dashArr)}
          </button>
          {open === 'dash' && (
            <div className="sel-pop">
              {DASHES.map((d) => (
                <button key={d.key} className={`sel-row ${d.key === dash ? 'on' : ''}`}
                  onClick={() => { onStyle({ dash: d.key }); setOpen(null) }}>
                  {dashLine(d.arr)}<span>{d.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
      <button className="sel-btn" title="Remove (Delete)" onClick={onDelete}>{ICON_TRASH}</button>
    </div>
  )
}
