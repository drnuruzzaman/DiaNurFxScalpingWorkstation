import React, { useEffect, useRef, useState } from 'react'
import type { Signal, Snapshot } from '../chart/types'
import { api } from '../lib/api'
import { fmt } from '../lib/format'
import { Empty } from './common'

type Mode = 'analysis' | 'challenge' | 'risk' | 'backtest' | 'ask'

const MODES: { key: Mode; label: string }[] = [
  { key: 'analysis', label: 'ANALYSIS' },
  { key: 'challenge', label: 'CHALLENGE' },
  { key: 'risk', label: 'RISK' },
  { key: 'backtest', label: 'BACKTEST' },
  { key: 'ask', label: 'ASK' },
]

/**
 * The analyst panel.
 *
 * When a signal is selected it shows that signal's brief in the chosen mode.
 * With no signal it falls back to the standing market narrative, because an
 * empty analyst panel on a quiet market is a wasted pane - the reason no signal
 * exists is itself the most useful thing to read.
 */
export function AgentDock({ snap, signal, symbol, tf }: {
  snap: Snapshot | null
  signal: Signal | null
  symbol: string
  tf: string
}) {
  const [mode, setMode] = useState<Mode>('analysis')
  const [brief, setBrief] = useState<any>(null)
  const [narrative, setNarrative] = useState<any>(null)
  const [question, setQuestion] = useState('')
  const [chat, setChat] = useState<{ q: string; a: string; src: string }[]>([])
  const [busy, setBusy] = useState(false)
  const chatEnd = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    if (signal) {
      api.brief(signal.id, symbol, tf)
        .then((b) => { if (!cancelled) setBrief(b) })
        .catch(() => { if (!cancelled) setBrief(null) })
    } else {
      setBrief(null)
    }
    return () => { cancelled = true }
  }, [signal?.id, symbol, tf])

  useEffect(() => {
    if (snap?.ok) setNarrative((snap as any).__narrative ?? null)
  }, [snap?.bar_time_ms])

  useEffect(() => {
    let cancelled = false
    if (!signal) {
      api.narrative(symbol, tf)
        .then((n) => { if (!cancelled) setNarrative(n) })
        .catch(() => {})
    }
    return () => { cancelled = true }
  }, [signal?.id, symbol, tf, snap?.bar_time_ms])

  useEffect(() => { chatEnd.current?.scrollIntoView({ behavior: 'smooth' }) }, [chat.length])

  const ask = async () => {
    const q = question.trim()
    if (!q || busy) return
    setBusy(true)
    setQuestion('')
    try {
      const r = await api.ask(q, symbol, tf, signal?.id)
      setChat((c) => [...c, { q, a: r.answer, src: r.source }])
    } catch (e: any) {
      setChat((c) => [...c, { q, a: `Could not answer: ${e.message}`, src: 'error' }])
    } finally {
      setBusy(false)
    }
  }

  const body = () => {
    if (mode === 'ask') {
      return (
        <>
          {chat.length === 0 && (
            <div className="t-dim" style={{ fontSize: 10.5, lineHeight: 1.6 }}>
              Ask about anything on the chart. Questions are answered from the
              engine's computed facts — levels, patterns, gates, sizing, backtest
              stats. Try:
              <div style={{ marginTop: 6 }}>
                {['why is there no signal?', 'where is the nearest resistance?',
                  'what is blocking this trade?', 'how has this playbook performed?'].map((s) => (
                    <div key={s} style={{ marginTop: 3 }}>
                      <button className="tool-btn" style={{ textTransform: 'none', letterSpacing: 0 }}
                        onClick={() => setQuestion(s)}>{s}</button>
                    </div>
                  ))}
              </div>
            </div>
          )}
          {chat.map((c, i) => (
            <div key={i} style={{ marginBottom: 12 }}>
              <div style={{
                fontSize: 10.5, fontWeight: 700, color: 'var(--info)',
                marginBottom: 4,
              }}>› {c.q}</div>
              <div className="t-mid" style={{ whiteSpace: 'pre-wrap', fontSize: 10.5 }}>{c.a}</div>
              <div className="t-dim" style={{ fontSize: 8.5, marginTop: 3 }}>
                answered by {c.src === 'llm' ? 'language model over engine facts' : 'engine'}
              </div>
            </div>
          ))}
          {busy && <div className="t-dim pulse" style={{ fontSize: 10.5 }}>thinking…</div>}
          <div ref={chatEnd} />
        </>
      )
    }

    if (!signal) {
      if (!narrative) return <Empty>Waiting for the first analysis…</Empty>
      return (
        <>
          <h4>Market read</h4>
          <p style={{ color: 'var(--ink-hi)', fontWeight: 600 }}>{narrative.headline}</p>
          {narrative.paragraphs?.map((p: string, i: number) => <p key={i}>{p}</p>)}
          {narrative.bullets?.length > 0 && (
            <>
              <h4>On the chart</h4>
              {narrative.bullets.map((b: string, i: number) => (
                <div key={i} className="t-mid" style={{ fontSize: 10.5, padding: '2px 0' }}>· {b}</div>
              ))}
            </>
          )}
          <div className="t-dim" style={{ fontSize: 9.5, marginTop: 12, lineHeight: 1.5 }}>
            No signal is selected. Select one from LIVE SIGNALS to get the full
            four-mode brief, or use ASK.
          </div>
        </>
      )
    }

    if (!brief) return <Empty>Loading brief…</Empty>

    if (mode === 'analysis') {
      const a = brief.analysis
      return (
        <>
          <p style={{ color: 'var(--ink-hi)', fontWeight: 600 }}>{a.thesis}</p>
          <h4>Why this exists</h4>
          {a.why.map((w: any, i: number) => (
            <div key={i} className="evidence-row">
              <span className="ic t-up">+</span>
              <span>
                <span className="t-mid">{w.text}</span>{' '}
                <span className="t-dim" style={{ fontSize: 9 }}>[{w.kind} +{w.weight}]</span>
              </span>
            </div>
          ))}
          <h4>Mechanics</h4>
          {a.mechanics.map((m: string, i: number) => (
            <div key={i} className="t-mid" style={{ fontSize: 10.5, padding: '2px 0' }}>· {m}</div>
          ))}
        </>
      )
    }

    if (mode === 'challenge') {
      const c = brief.challenge
      const col = c.verdict === 'stand aside' ? 'var(--bear)'
        : c.verdict === 'size down' ? 'var(--warn)' : 'var(--bull)'
      return (
        <>
          <div style={{
            padding: '7px 9px', borderRadius: 'var(--r-sm)', marginBottom: 9,
            border: `1px solid ${col}`, background: `${col}18`,
          }}>
            <div style={{ color: col, fontSize: 11, fontWeight: 800, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
              {c.verdict}
            </div>
            <div className="t-mid" style={{ fontSize: 10.5, marginTop: 3 }}>{c.summary}</div>
          </div>
          {c.objections.length === 0 ? (
            <div className="t-mid" style={{ fontSize: 10.5 }}>No objection found.</div>
          ) : c.objections.map((o: any, i: number) => (
            <div key={i} className={`objection ${o.severity}`}>
              <span className="sev" style={{
                color: o.severity === 'severe' ? 'var(--bear)'
                  : o.severity === 'moderate' ? 'var(--warn)' : 'var(--ink-dim)',
              }}>{o.severity}</span>
              <span>
                <span className="t-mid">{o.text}</span>{' '}
                <span className="t-dim" style={{ fontSize: 8.5 }}>({o.source})</span>
              </span>
            </div>
          ))}
          {signal.gates?.length > 0 && (
            <>
              <h4>Gate ledger</h4>
              {signal.gates.map((g, i) => (
                <div className="gate-row" key={i}>
                  <span className="t-dim" style={{ fontSize: 9.5 }}>{g.name}</span>
                  <span className={`gate-v gate-${g.verdict}`}>{g.verdict}</span>
                  <span className="t-mid" style={{ fontSize: 10 }}>{g.detail}</span>
                </div>
              ))}
            </>
          )}
        </>
      )
    }

    if (mode === 'risk') {
      const r = brief.risk
      return (
        <>
          {r.lines.map((l: string, i: number) => (
            <div key={i} className="t-mid" style={{ fontSize: 10.5, padding: '3px 0' }}>· {l}</div>
          ))}
          <h4>Management plan</h4>
          {r.management.map((m: string, i: number) => (
            <div key={i} className="evidence-row">
              <span className="ic t-info">{i + 1}</span>
              <span className="t-mid">{m}</span>
            </div>
          ))}
        </>
      )
    }

    const b = brief.backtest
    return (
      <>
        {b.lines.map((l: string, i: number) => (
          <div key={i} className="t-mid" style={{ fontSize: 10.5, padding: '3px 0' }}>· {l}</div>
        ))}
        {!b.stat && (
          <div className="t-dim" style={{ fontSize: 9.5, marginTop: 10, lineHeight: 1.5 }}>
            Backtest figures are quoted only when a run exists for this
            configuration. The panel will not estimate them.
          </div>
        )}
      </>
    )
  }

  return (
    <div className="agent">
      <div className="panel-head">
        <span>AI Analyst</span>
        {signal
          ? <span className={signal.side === 'buy' ? 'chip chip-up' : 'chip chip-down'}>
            {signal.label} {signal.side}
          </span>
          : <span className="chip chip-mute">market read</span>}
      </div>
      <div className="agent-modes">
        {MODES.map((m) => (
          <button key={m.key}
            className={`agent-mode ${mode === m.key ? 'on' : ''}`}
            onClick={() => setMode(m.key)}>{m.label}</button>
        ))}
      </div>
      <div className="agent-body">{body()}</div>
      {mode === 'ask' && (
        <div className="agent-ask">
          <input
            value={question}
            placeholder="ask about this chart…"
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') ask() }}
          />
          <button className="tool-btn" onClick={ask} disabled={busy}>Send</button>
        </div>
      )}
    </div>
  )
}
