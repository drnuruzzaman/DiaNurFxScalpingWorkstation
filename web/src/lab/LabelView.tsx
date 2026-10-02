/**
 * LabelView - Step 0B: a trader's verdict on what the engine draws.
 *
 * Each moment is a fixed bar from history (tools/structure_labels.py picked
 * them before anything was judged). The chart shows the 600 closed bars up to
 * that bar and NOTHING after it - a verdict made with the future on screen
 * would measure hindsight, not the engine.
 *
 * For every trendline, channel and pattern the engine returned:
 *     right   I would draw this
 *     close   I would draw it, with other anchors / points
 *     wrong   I would not draw this
 * Swings in view count as right unless clicked (then wrong). Anything the
 * engine MISSED is drawn in with the tools - that is what recall is made of.
 *
 * Keys: Up/Down pick an object, 1 2 3 = right / close / wrong, Enter saves and
 * moves on, Esc cancels a drawing.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { lab } from './labApi'

type Verdict = 'right' | 'close' | 'wrong'
interface LBars { t: number[]; o: number[]; h: number[]; l: number[]; c: number[] }
interface LSwing { key: string; x: number; kind: 'high' | 'low'; price: number; label: string; strength: number }
interface LLine {
  key: string; kind: 'support' | 'resistance'; x1: number; y1: number; x2: number; y2: number
  slope: number; touches: number; touch_x: number[]; score: number; broken: boolean; on_chart: boolean
}
interface LChannel { key: string; kind: string; x1: number; slope: number; upper: number; lower: number; containment: number; score: number }
interface LPoint { x: number | null; t: number; price: number; role: string }
interface LPattern {
  key: string; kind: string; label: string; direction: string; status: string; quality: number
  points: LPoint[]; break_level: number; target: number; invalidation: number
  actionable: boolean; relevance: number; featured: boolean
}
interface TP { t: number; price: number }
type Missed =
  | { type: 'swing'; kind: 'H' | 'L'; t: number; price: number }
  | { type: 'trendline'; kind: 'support' | 'resistance'; p1: TP; p2: TP }
  | { type: 'pattern'; kind: string; points: TP[] }
interface LabelRec {
  id: string; engine: string; saved_utc: string; skipped: boolean
  verdicts: Record<string, Verdict>; missed: Missed[]; swings_in_view: string[]
  view_bars: number; note: string; seconds: number
}
interface LMoment {
  id: string; tf: string; t: number; symbol: string; atr: number; engine: string
  bars: LBars; swings: LSwing[]; trendlines: LLine[]; channels: LChannel[]; patterns: LPattern[]
  label: LabelRec | null
}
interface QRow { id: string; tf: string; t: number; span: string; year: number; round: number; done: boolean; skipped: boolean }
interface Queue { engine: string | null; prepared: boolean; moments: QRow[]; done: number; skipped: number; hint: string | null }

type Tool = null | { type: 'swing' } | { type: 'support' | 'resistance'; pts: TP[] } | { type: 'pattern'; kind: string; pts: TP[] }

const PATTERN_KINDS = [
  'head_shoulders', 'inverse_head_shoulders', 'double_top', 'double_bottom', 'triple_top',
  'triple_bottom', 'ascending_triangle', 'descending_triangle', 'symmetrical_triangle',
  'rising_wedge', 'falling_wedge', 'bull_flag', 'bear_flag', 'rectangle',
]
const VIEWS = [150, 300, 600]
const RIGHT_PAD = 6          // empty bars right of the moment, so line ends are readable
const AXIS_W = 62
const AXIS_H = 18

const utc = (ms: number) => new Date(ms).toISOString().slice(0, 16).replace('T', ' ')
const nice = (k: string) => k.replace(/_/g, ' ')
const V_LABEL: Record<Verdict, string> = { right: '✓', close: '≈', wrong: '✗' }

function missedText(m: Missed): string {
  if (m.type === 'swing') return `swing ${m.kind === 'H' ? 'high' : 'low'} ${m.price.toFixed(2)} · ${utc(m.t).slice(5)}`
  if (m.type === 'trendline') return `${m.kind} line ${utc(m.p1.t).slice(5)} → ${utc(m.p2.t).slice(5)}`
  return `${nice(m.kind)} · ${m.points.length} points`
}

export function LabelView({ onClose }: { onClose: () => void }) {
  const [queue, setQueue] = useState<Queue | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [idx, setIdx] = useState(0)
  const [m, setM] = useState<LMoment | null>(null)
  const [verdicts, setVerdicts] = useState<Record<string, Verdict>>({})
  const [wrongSwings, setWrongSwings] = useState<Set<string>>(new Set())
  const [missed, setMissed] = useState<Missed[]>([])
  const [note, setNote] = useState('')
  const [view, setView] = useState(300)
  const [hover, setHover] = useState<string | null>(null)
  const [pinned, setPinned] = useState<string | null>(null)
  const [tool, setToolState] = useState<Tool>(null)
  // Clicks read the tool from a ref: two quick clicks must not both see the
  // state from before the first one landed.
  const toolRef = useRef<Tool>(null)
  const setTool = (x: Tool) => { toolRef.current = x; setToolState(x) }
  const [patKind, setPatKind] = useState(PATTERN_KINDS[2])
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  // Leaving a moment with unsaved verdicts asks inline (window.confirm is
  // swallowed by embedded browsers).
  const [leave, setLeave] = useState<null | { go: () => void }>(null)
  const guard = (go: () => void) => { if (dirty) setLeave({ go }); else go() }
  const opened = useRef(Date.now())

  const loadQueue = useCallback((): Promise<Queue> => lab.labels().then((q: Queue) => { setQueue(q); return q }), [])

  useEffect(() => {
    loadQueue().then((q) => {
      const first = q.moments.findIndex((r: QRow) => !r.done && !r.skipped)
      setIdx(first >= 0 ? first : 0)
    }).catch((e) => setErr(String(e.message ?? e)))
  }, [loadQueue])

  const row = queue?.moments[idx]
  useEffect(() => {
    if (!row) return
    let stop = false
    setM(null)
    lab.labelMoment(row.id).then((x: LMoment) => {
      if (stop) return
      setM(x)
      const r = x.label
      setVerdicts(r?.verdicts ?? {})
      setWrongSwings(new Set(Object.entries(r?.verdicts ?? {}).filter(([k, v]) => k.startsWith('sw:') && v === 'wrong').map(([k]) => k)))
      setMissed(r?.missed ?? [])
      setNote(r?.note ?? '')
      if (r?.view_bars) setView(r.view_bars)
      setPinned(null); setHover(null); setTool(null); setDirty(false); setLeave(null)
      opened.current = Date.now()
    }).catch((e) => setErr(String(e.message ?? e)))
    return () => { stop = true }
  }, [row?.id])

  // Objects to judge, in list order - Up/Down walks this.
  const objs = useMemo(() => !m ? [] : [
    ...m.trendlines.map((x) => x.key), ...m.channels.map((x) => x.key), ...m.patterns.map((x) => x.key),
  ], [m])
  const sel = pinned ?? hover
  const n = m?.bars.t.length ?? 0
  const lo = Math.max(0, n - view)
  const swingsInView = useMemo(() => (m ? m.swings.filter((s) => s.x >= lo) : []), [m, lo])
  const unjudged = objs.filter((k) => !verdicts[k]).length

  const setV = (key: string, v: Verdict) => {
    setVerdicts((o) => {
      const next = { ...o }
      if (next[key] === v) delete next[key]; else next[key] = v
      return next
    })
    setDirty(true)
  }
  const toggleSwing = (key: string) => {
    setWrongSwings((s) => { const n2 = new Set(s); if (n2.has(key)) n2.delete(key); else n2.add(key); return n2 })
    setDirty(true)
  }

  const go = (to: number) => {
    if (!queue) return
    guard(() => setIdx(Math.max(0, Math.min(queue.moments.length - 1, to))))
  }
  const nextOpen = (from: number) => {
    if (!queue) return from
    const L = queue.moments.length
    for (let i = 1; i <= L; i++) {
      const j = (from + i) % L
      if (!queue.moments[j].done && !queue.moments[j].skipped) return j
    }
    return Math.min(L - 1, from + 1)
  }

  const save = async (opts: { skipped?: boolean; advance?: boolean } = {}) => {
    if (!m) return
    setSaving(true)
    try {
      const v: Record<string, Verdict> = { ...verdicts }
      for (const k of Object.keys(v)) if (k.startsWith('sw:')) delete v[k]
      wrongSwings.forEach((k) => { v[k] = 'wrong' })
      await lab.labelSave(m.id, {
        verdicts: v, missed, note, skipped: !!opts.skipped,
        swings_in_view: swingsInView.map((s) => s.key), view_bars: view,
        seconds: (Date.now() - opened.current) / 1000,
      })
      setDirty(false)
      const q = await loadQueue()
      setMsg(opts.skipped ? 'Skipped.' : `Saved · ${q.done} of ${q.moments.length} labelled`)
      window.setTimeout(() => setMsg(null), 4000)
      if (opts.advance) {
        const L = q.moments.length
        let j = idx
        for (let i = 1; i <= L; i++) {
          const k = (idx + i) % L
          if (!q.moments[k].done && !q.moments[k].skipped) { j = k; break }
        }
        setIdx(j)
      }
    } catch (e: any) {
      setMsg(`Not saved: ${e.message ?? e}`)
    } finally {
      setSaving(false)
    }
  }

  // Drawing tools: every click snaps to the nearest high or low of the bar under it.
  const onPick = (p: { t: number; price: number; kind: 'H' | 'L' }) => {
    const tool = toolRef.current
    if (!tool) return
    if (tool.type === 'swing') {
      setMissed((x) => [...x, { type: 'swing', kind: p.kind, t: p.t, price: p.price }])
      setTool(null); setDirty(true)
      return
    }
    const pts = [...tool.pts, { t: p.t, price: p.price }]
    if ((tool.type === 'support' || tool.type === 'resistance') && pts.length === 2) {
      const [a, b] = pts[0].t <= pts[1].t ? pts : [pts[1], pts[0]]
      setMissed((x) => [...x, { type: 'trendline', kind: tool.type as 'support' | 'resistance', p1: a, p2: b }])
      setTool(null); setDirty(true)
      return
    }
    setTool({ ...tool, pts } as Tool)
  }
  const finishPattern = () => {
    const tool = toolRef.current
    if (tool?.type !== 'pattern' || tool.pts.length < 2) { setTool(null); return }
    setMissed((x) => [...x, { type: 'pattern', kind: tool.kind, points: [...tool.pts].sort((a, b) => a.t - b.t) }])
    setTool(null); setDirty(true)
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT') return
      if (e.key === 'Escape') { setTool(null); setPinned(null); return }
      if (e.key === 'Enter') {
        e.preventDefault()
        if (tool?.type === 'pattern') finishPattern(); else save({ advance: true })
        return
      }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        if (!objs.length) return
        const i = sel ? objs.indexOf(sel) : -1
        const j = e.key === 'ArrowDown' ? Math.min(objs.length - 1, i + 1) : Math.max(0, i - 1)
        setPinned(objs[j])
        return
      }
      const v = ({ '1': 'right', '2': 'close', '3': 'wrong' } as Record<string, Verdict>)[e.key]
      if (v && sel && objs.includes(sel)) setV(sel, v)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  if (err) {
    return (
      <div className="lbl-empty">
        <p className="t-down">{err}</p>
        <p className="t-dim">Prepare the moments first: <span className="mono">python tools/structure_labels.py --sample --prepare</span>, then restart the lab.</p>
        <button className="tool-btn" onClick={onClose}>Back</button>
      </div>
    )
  }
  if (!queue) return <div className="lbl-empty t-dim">Loading the label queue…</div>
  if (!queue.prepared || !queue.moments.length) {
    return (
      <div className="lbl-empty">
        <p>No label moments yet.</p>
        <p className="t-dim mono">{queue.hint}</p>
        <button className="tool-btn" onClick={onClose}>Back</button>
      </div>
    )
  }

  const drawHint = !tool ? null
    : tool.type === 'swing' ? 'Click the high or low the engine missed.'
      : tool.type === 'pattern' ? `Click the ${nice(tool.kind)}'s points in order (${tool.pts.length} so far) - Enter or Done to finish.`
        : `Click the ${tool.pts.length ? 'second' : 'first'} anchor of the ${tool.type} line.`

  return (
    <div className="lbl">
      <div className="lbl-bar">
        <span className="lab-hero-k">LABEL STRUCTURE</span>
        <span className="mono t-hi">{row?.tf} · {row ? utc(row.t) : ''} UTC</span>
        <span className="t-dim mono">moment {idx + 1}/{queue.moments.length} · round {row?.round}
          {row?.done ? ' · labelled' : row?.skipped ? ' · skipped' : ''}</span>
        <span className="lbl-prog" title={`${queue.done} labelled, ${queue.skipped} skipped`}>
          <i style={{ width: `${(100 * queue.done) / queue.moments.length}%` }} />
        </span>
        <span className="t-dim mono">{queue.done} done</span>
        <span className="lbl-gap" />
        <button className="tool-btn" onClick={() => go(idx - 1)} disabled={idx === 0}>◀</button>
        <button className="tool-btn" onClick={() => go(idx + 1)} disabled={idx >= queue.moments.length - 1}>▶</button>
        <button className="tool-btn" onClick={() => go(nextOpen(idx))}>Next unlabelled</button>
        <span className="lbl-views">
          {VIEWS.map((v) => (
            <button key={v} className={`tool-btn ${view === v ? 'on' : ''}`} onClick={() => setView(v)}>{v}</button>
          ))}
        </span>
        <button className="tool-btn" onClick={() => guard(onClose)}>Close</button>
      </div>
      {leave && (
        <div className="lbl-leave">
          <span>This moment has unsaved verdicts.</span>
          <button className="tool-btn lab-go" onClick={async () => { const g = leave.go; setLeave(null); await save(); g() }}>Save, then go</button>
          <button className="tool-btn" onClick={() => { const g = leave.go; setLeave(null); setDirty(false); g() }}>Discard</button>
          <button className="tool-btn" onClick={() => setLeave(null)}>Stay</button>
        </div>
      )}
      <div className="lbl-body">
        <div className="lbl-chart">
          {m ? (
            <LabelChart m={m} lo={lo} sel={sel} verdicts={verdicts} wrongSwings={wrongSwings}
              missed={missed} tool={tool} onPick={onPick} onSwing={toggleSwing} />
          ) : <div className="lbl-empty t-dim">Loading…</div>}
          {drawHint && (
            <div className="lbl-hint">{drawHint}
              {tool?.type === 'pattern' && <button className="tool-btn" onClick={finishPattern}>Done</button>}
              <button className="tool-btn" onClick={() => setTool(null)}>Cancel</button>
            </div>
          )}
        </div>
        {m && (
          <div className="lbl-side">
            <div className="lbl-help t-dim">
              <b>✓ right</b> I would draw this · <b>≈ close</b> I would, with other anchors · <b>✗ wrong</b> I would not.
              Judge only from what is on screen - nothing after this bar is shown.
            </div>

            <Section title={`Swings · ${swingsInView.length} in view`}
              extra={<button className="tool-btn" onClick={() => setTool({ type: 'swing' })}>+ missed</button>}>
              <div className="t-dim lbl-small">
                Unmarked swings count as right. Click a marker on the chart to mark it wrong
                ({[...wrongSwings].filter((k) => swingsInView.some((s) => s.key === k)).length} marked).
              </div>
            </Section>

            <Section title={`Trendlines · ${m.trendlines.length}`}
              extra={<>
                <button className="tool-btn" onClick={() => setTool({ type: 'support', pts: [] })}>+ support</button>
                <button className="tool-btn" onClick={() => setTool({ type: 'resistance', pts: [] })}>+ resistance</button>
              </>}>
              {m.trendlines.map((x) => (
                <ObjRow key={x.key} k={x.key} sel={sel} v={verdicts[x.key]} onV={setV} onHover={setHover} onPin={setPinned}
                  dot={x.kind === 'support' ? 'bull' : 'bear'}
                  text={`${x.kind} · ${x.touches} touches · score ${x.score}`}
                  tags={[x.on_chart ? 'on chart' : 'not drawn', ...(x.broken ? ['broken'] : [])]} />
              ))}
              {!m.trendlines.length && <div className="t-dim lbl-small">None returned.</div>}
            </Section>

            <Section title={`Channels · ${m.channels.length}`}>
              {m.channels.map((x) => (
                <ObjRow key={x.key} k={x.key} sel={sel} v={verdicts[x.key]} onV={setV} onHover={setHover} onPin={setPinned}
                  dot="info" text={`${x.kind} · contains ${(x.containment * 100).toFixed(0)}% · score ${x.score}`} tags={[]} />
              ))}
              {!m.channels.length && <div className="t-dim lbl-small">None returned.</div>}
            </Section>

            <Section title={`Patterns · ${m.patterns.length}`}
              extra={<>
                <select value={patKind} onChange={(e) => setPatKind(e.target.value)} className="lbl-select">
                  {PATTERN_KINDS.map((k) => <option key={k} value={k}>{nice(k)}</option>)}
                </select>
                <button className="tool-btn" onClick={() => setTool({ type: 'pattern', kind: patKind, pts: [] })}>+ missed</button>
              </>}>
              {m.patterns.map((x) => (
                <ObjRow key={x.key} k={x.key} sel={sel} v={verdicts[x.key]} onV={setV} onHover={setHover} onPin={setPinned}
                  dot={x.direction === 'bullish' ? 'bull' : x.direction === 'bearish' ? 'bear' : 'info'}
                  text={`${x.label} · ${x.status} · q ${x.quality}`}
                  tags={[...(x.featured ? ['featured'] : []), ...(x.actionable ? [] : ['not actionable'])]} />
              ))}
              {!m.patterns.length && <div className="t-dim lbl-small">None returned.</div>}
            </Section>

            <Section title={`Missed · ${missed.length}`}>
              {missed.map((x, i) => (
                <div key={i} className="lbl-missed">
                  <span>{missedText(x)}</span>
                  <button className="tool-btn" title="Remove" onClick={() => { setMissed((a) => a.filter((_, j) => j !== i)); setDirty(true) }}>×</button>
                </div>
              ))}
              {!missed.length && <div className="t-dim lbl-small">Draw in anything a trader would mark that the engine did not.</div>}
            </Section>

            <textarea className="lbl-note" placeholder="Note (optional)" value={note}
              onChange={(e) => { setNote(e.target.value); setDirty(true) }} />

            <div className="lbl-foot">
              <span className={unjudged ? 't-warn' : 't-dim'}>{unjudged ? `${unjudged} not judged` : 'all judged'}</span>
              <span className="lbl-gap" />
              <button className="tool-btn" disabled={saving} onClick={() => save({ skipped: true, advance: true })}
                title="Chart unusable (gap, bad data) - leave it out">Skip</button>
              <button className="tool-btn" disabled={saving} onClick={() => save()}>Save</button>
              <button className="tool-btn lab-go" disabled={saving} onClick={() => save({ advance: true })}>Save &amp; next ⏎</button>
            </div>
            {msg && <div className="lbl-msg">{msg}</div>}
            <div className="t-dim lbl-small mono">engine {m.engine} · ATR {m.atr.toFixed(2)} · keys ↑↓ 1 2 3 ⏎ Esc</div>
          </div>
        )}
      </div>
    </div>
  )
}

function Section({ title, extra, children }: { title: string; extra?: ReactNode; children?: ReactNode }) {
  return (
    <div className="lbl-sec">
      <div className="lbl-sec-h"><span>{title}</span><span className="lbl-gap" />{extra}</div>
      {children}
    </div>
  )
}

function ObjRow({ k, sel, v, onV, onHover, onPin, dot, text, tags }: {
  k: string; sel: string | null; v?: Verdict; onV: (k: string, v: Verdict) => void
  onHover: (k: string | null) => void; onPin: (fn: (p: string | null) => string | null) => void
  dot: string; text: string; tags: string[]
}) {
  return (
    <div className={`lbl-obj ${sel === k ? 'on' : ''}`} onMouseEnter={() => onHover(k)} onMouseLeave={() => onHover(null)}
      onClick={() => onPin((p) => (p === k ? null : k))}>
      <i className={`lbl-dot ${dot}`} />
      <span className="lbl-obj-t">{text}{tags.map((t) => <em key={t}>{t}</em>)}</span>
      {(['right', 'close', 'wrong'] as Verdict[]).map((x) => (
        <button key={x} className={`lbl-v ${x} ${v === x ? 'on' : ''}`} title={x}
          onClick={(e) => { e.stopPropagation(); onV(k, x) }}>{V_LABEL[x]}</button>
      ))}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// the chart: candles to the moment, the engine's objects, the trader's marks  //
// --------------------------------------------------------------------------- //
function LabelChart({ m, lo, sel, verdicts, wrongSwings, missed, tool, onPick, onSwing }: {
  m: LMoment; lo: number; sel: string | null; verdicts: Record<string, Verdict>; wrongSwings: Set<string>
  missed: Missed[]; tool: Tool; onPick: (p: { t: number; price: number; kind: 'H' | 'L' }) => void
  onSwing: (key: string) => void
}) {
  const box = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ w: 900, h: 520 })
  const [snap, setSnap] = useState<{ x: number; price: number; kind: 'H' | 'L' } | null>(null)
  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const { h, l, o, c, t } = m.bars
  const n = t.length
  const W = Math.max(200, size.w - AXIS_W), H = Math.max(160, size.h - AXIS_H)
  const slots = n - lo + RIGHT_PAD
  const bw = W / slots
  const X = (i: number) => (i - lo + 0.5) * bw
  let ymin = Infinity, ymax = -Infinity
  for (let i = lo; i < n; i++) { ymin = Math.min(ymin, l[i]); ymax = Math.max(ymax, h[i]) }
  const pad = (ymax - ymin) * 0.06 || 1
  ymin -= pad; ymax += pad
  const Y = (p: number) => H - ((p - ymin) / (ymax - ymin)) * H
  const P = (y: number) => ymin + ((H - y) / H) * (ymax - ymin)
  const tIndex = useMemo(() => new Map(t.map((x, i) => [x, i])), [t])
  const xOfT = (ms: number) => { const i = tIndex.get(ms); return i === undefined ? null : X(i) }

  const dim = (key: string) => (sel && sel !== key ? 0.12 : 1)
  const lineVal = (y1: number, x1: number, slope: number, i: number) => y1 + slope * (i - x1)
  const ticks = useMemo(() => {
    const out: number[] = []
    const step = niceStep((ymax - ymin) / 6)
    for (let v = Math.ceil(ymin / step) * step; v < ymax; v += step) out.push(v)
    return out
  }, [ymin, ymax])
  const tTicks = useMemo(() => {
    const out: number[] = []
    const every = Math.max(1, Math.round((n - lo) / 6))
    for (let i = n - 1; i >= lo; i -= every) out.push(i)
    return out
  }, [n, lo])

  type Ev = { currentTarget: SVGSVGElement; clientX: number; clientY: number }
  // The nearest high or low of the bar under the pointer - support lines take
  // lows, resistance lines highs, anything else whichever is closer.
  const snapAt = (e: Ev) => {
    if (!tool) return null
    const r = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - r.left, py = e.clientY - r.top
    if (px > W || py > H) return null
    const i = Math.max(lo, Math.min(n - 1, Math.floor(px / bw) + lo))
    const p = P(py)
    let kind: 'H' | 'L' = Math.abs(p - h[i]) <= Math.abs(p - l[i]) ? 'H' : 'L'
    if (tool.type === 'support') kind = 'L'
    if (tool.type === 'resistance') kind = 'H'
    return { x: i, price: kind === 'H' ? h[i] : l[i], kind }
  }
  const onMove = (e: Ev) => setSnap(snapAt(e))
  const onClick = (e: Ev) => {
    const sp = snapAt(e)
    if (sp) onPick({ t: t[sp.x], price: sp.price, kind: sp.kind })
  }

  const vCol = (key: string) => verdicts[key] === 'wrong' ? 'var(--bear)' : verdicts[key] === 'right' ? 'var(--bull)' : verdicts[key] === 'close' ? 'var(--warn)' : null

  return (
    <div className="lbl-svgbox" ref={box}>
      <svg width={size.w} height={size.h} onMouseMove={onMove} onMouseLeave={() => setSnap(null)} onClick={onClick}
        style={{ cursor: tool ? 'crosshair' : 'default' }}>
        <defs><clipPath id="lbl-clip"><rect x={0} y={0} width={W} height={H} /></clipPath></defs>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={0} x2={W} y1={Y(v)} y2={Y(v)} className="lbl-grid" />
            <text x={W + 6} y={Y(v) + 3} className="lbl-axis">{v.toFixed(v >= 100 ? 1 : 3)}</text>
          </g>
        ))}
        {tTicks.map((i) => (
          <text key={i} x={X(i)} y={H + 13} className="lbl-axis" textAnchor="middle">{utc(t[i]).slice(5)}</text>
        ))}
        <line x1={X(n - 1) + bw / 2} x2={X(n - 1) + bw / 2} y1={0} y2={H} className="lbl-now" />
        <g clipPath="url(#lbl-clip)">
          {/* candles */}
          {Array.from({ length: n - lo }, (_, j) => {
            const i = lo + j
            const up = c[i] >= o[i]
            const col = up ? 'var(--bull)' : 'var(--bear)'
            const x = X(i)
            const top = Y(Math.max(o[i], c[i])), bot = Y(Math.min(o[i], c[i]))
            return (
              <g key={i}>
                <line x1={x} x2={x} y1={Y(h[i])} y2={Y(l[i])} stroke={col} strokeWidth={1} opacity={sel ? 0.55 : 0.9} />
                <rect x={x - Math.max(0.5, bw * 0.35)} width={Math.max(1, bw * 0.7)} y={top} height={Math.max(1, bot - top)}
                  fill={col} opacity={sel ? 0.45 : 0.85} />
              </g>
            )
          })}

          {/* channels: only when picked - three of them over the candles is a wash */}
          {m.channels.filter((x) => sel === x.key).map((x) => {
            const a = Math.max(lo, x.x1), b = n - 1
            const u1 = lineVal(x.upper, x.x1, x.slope, a), u2 = lineVal(x.upper, x.x1, x.slope, b)
            const l1 = lineVal(x.lower, x.x1, x.slope, a), l2 = lineVal(x.lower, x.x1, x.slope, b)
            return (
              <g key={x.key}>
                <polygon points={`${X(a)},${Y(u1)} ${X(b)},${Y(u2)} ${X(b)},${Y(l2)} ${X(a)},${Y(l1)}`} fill="var(--info)" opacity={0.08} />
                <line x1={X(a)} x2={X(b)} y1={Y(u1)} y2={Y(u2)} stroke="var(--info)" strokeWidth={1.6} />
                <line x1={X(a)} x2={X(b)} y1={Y(l1)} y2={Y(l2)} stroke="var(--info)" strokeWidth={1.6} />
              </g>
            )
          })}

          {/* trendlines - the ones the live chart draws solid, the rest faint */}
          {m.trendlines.map((x) => {
            const b = n - 1
            const col = x.kind === 'support' ? 'var(--bull)' : 'var(--bear)'
            const on = sel === x.key
            const op = sel ? dim(x.key) : x.on_chart ? 0.85 : 0.3
            return (
              <g key={x.key} opacity={op}>
                <line x1={X(x.x1)} x2={X(b)} y1={Y(x.y1)} y2={Y(lineVal(x.y1, x.x1, x.slope, b))}
                  stroke={vCol(x.key) ?? col} strokeWidth={on ? 2.2 : 1.2}
                  strokeDasharray={x.broken ? '5 4' : x.on_chart ? undefined : '2 3'} />
                {x.touch_x.map((ti) => (
                  <circle key={ti} cx={X(ti)} cy={Y(lineVal(x.y1, x.x1, x.slope, ti))} r={on ? 3.5 : 2.2} fill={col} />
                ))}
              </g>
            )
          })}

          {/* patterns - featured ones faintly by default, any one fully when picked */}
          {m.patterns.filter((p) => sel === p.key || (!sel && p.featured)).map((p) => {
            const col = p.direction === 'bullish' ? 'var(--bull)' : p.direction === 'bearish' ? 'var(--bear)' : 'var(--info)'
            const pts = p.points.map((q) => ({ x: xOfT(q.t), y: Y(q.price), role: q.role })).filter((q) => q.x !== null) as { x: number; y: number; role: string }[]
            const on = sel === p.key
            const x0 = pts.length ? pts[pts.length - 1].x : 0, x1 = X(n - 1)
            return (
              <g key={p.key} opacity={on ? 1 : 0.55}>
                <polyline points={pts.map((q) => `${q.x},${q.y}`).join(' ')} fill="none" stroke={vCol(p.key) ?? col} strokeWidth={on ? 2.2 : 1.4} />
                {pts.map((q, i) => (
                  <g key={i}>
                    <circle cx={q.x} cy={q.y} r={3} fill={col} />
                    {on && <text x={q.x + 4} y={q.y - 5} className="lbl-role">{q.role}</text>}
                  </g>
                ))}
                {on && <>
                  <Level y={Y(p.break_level)} x0={x0} x1={x1} col="var(--warn)" text={`break ${p.break_level.toFixed(2)}`} />
                  <Level y={Y(p.target)} x0={x0} x1={x1} col={col} text={`target ${p.target.toFixed(2)}`} />
                  <Level y={Y(p.invalidation)} x0={x0} x1={x1} col="var(--ink-low)" text={`invalidation ${p.invalidation.toFixed(2)}`} />
                </>}
              </g>
            )
          })}

          {/* swings in view: click to mark wrong */}
          {m.swings.filter((s) => s.x >= lo).map((s) => {
            const hi = s.kind === 'high'
            const x = X(s.x), y = Y(s.price) + (hi ? -7 : 7)
            const wrong = wrongSwings.has(s.key)
            return (
              <g key={s.key} className="lbl-swing" opacity={sel ? 0.35 : 1}
                onClick={(e) => { if (!tool) { e.stopPropagation(); onSwing(s.key) } }}>
                <circle cx={x} cy={y} r={6} fill="transparent" />
                {wrong
                  ? <text x={x} y={y + 3} textAnchor="middle" className="lbl-swing-x">✗</text>
                  : <path d={hi ? `M${x - 3.5},${y - 2} L${x + 3.5},${y - 2} L${x},${y + 3} Z` : `M${x - 3.5},${y + 2} L${x + 3.5},${y + 2} L${x},${y - 3} Z`}
                    fill={hi ? 'var(--bear)' : 'var(--bull)'} />}
                {s.label && bw > 3 && <text x={x} y={hi ? y - 6 : y + 13} textAnchor="middle" className="lbl-swing-l">{s.label}</text>}
              </g>
            )
          })}

          {/* the trader's missed objects */}
          {missed.map((mm, i) => {
            if (mm.type === 'swing') {
              const x = xOfT(mm.t)
              return x === null ? null : <circle key={i} cx={x} cy={Y(mm.price)} r={5} fill="none" stroke="var(--info)" strokeWidth={2} />
            }
            if (mm.type === 'trendline') {
              const a = tIndex.get(mm.p1.t), b = tIndex.get(mm.p2.t)
              if (a === undefined || b === undefined || a === b) return null
              const slope = (mm.p2.price - mm.p1.price) / (b - a)
              return <line key={i} x1={X(a)} x2={X(n - 1)} y1={Y(mm.p1.price)} y2={Y(lineVal(mm.p1.price, a, slope, n - 1))}
                stroke="var(--info)" strokeWidth={1.8} strokeDasharray="6 3" />
            }
            const pts = mm.points.map((q) => ({ x: xOfT(q.t), y: Y(q.price) })).filter((q) => q.x !== null)
            return <polyline key={i} points={pts.map((q) => `${q.x},${q.y}`).join(' ')} fill="none" stroke="var(--info)" strokeWidth={1.8} strokeDasharray="6 3" />
          })}

          {/* drawing in progress */}
          {tool && tool.type !== 'swing' && tool.pts.length > 0 && (
            <polyline points={[...tool.pts.map((q) => `${xOfT(q.t)},${Y(q.price)}`), ...(snap ? [`${X(snap.x)},${Y(snap.price)}`] : [])].join(' ')}
              fill="none" stroke="var(--info)" strokeWidth={1.5} strokeDasharray="3 3" />
          )}
          {tool && snap && <circle cx={X(snap.x)} cy={Y(snap.price)} r={4.5} fill="none" stroke="var(--info)" strokeWidth={2} />}
        </g>
      </svg>
    </div>
  )
}

function Level({ y, x0, x1, col, text }: { y: number; x0: number; x1: number; col: string; text: string }) {
  return (
    <g>
      <line x1={x0} x2={x1} y1={y} y2={y} stroke={col} strokeDasharray="4 3" strokeWidth={1.2} />
      <text x={x0 + 4} y={y - 3} className="lbl-role" fill={col}>{text}</text>
    </g>
  )
}

function niceStep(raw: number): number {
  const e = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-9))))
  const f = raw / e
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * e
}
