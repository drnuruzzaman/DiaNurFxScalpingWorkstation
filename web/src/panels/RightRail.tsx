import React from 'react'
import type { Pattern, Signal, Snapshot } from '../chart/types'
import { FEATURED_PATTERNS } from '../chart/ChartEngine'
import { biasClass, chipFor, dirClass, fmt, signed, stageChip } from '../lib/format'
import { Empty, KV, Meter, Panel, ScoreRow } from './common'

function SignalCard({ sig, onSelect, selected, onPlace, tradingEnabled }: {
  sig: Signal
  selected: boolean
  onSelect: () => void
  onPlace: () => void
  tradingEnabled: boolean
}) {
  const buy = sig.side === 'buy'
  const statusChip =
    sig.status === 'qualified' ? 'chip chip-up'
      : sig.status === 'watch' ? 'chip chip-warn'
        : sig.status === 'conflicted' ? 'chip chip-mute'
          : 'chip chip-down'

  return (
    <div className={`signal-card ${sig.side}`} onClick={onSelect}
      style={{ cursor: 'pointer', outline: selected ? '1px solid var(--info)' : 'none' }}>
      <div className="signal-head">
        <span className={`signal-side ${buy ? 't-up' : 't-down'}`}>{buy ? 'BUY' : 'SELL'}</span>
        <span className="signal-pb">{sig.label}</span>
        <span style={{ flex: 1 }} />
        <span className={statusChip}>{sig.status}</span>
      </div>

      <div style={{ padding: '5px 9px', display: 'flex', alignItems: 'center', gap: 8 }}>
        <span className="kv-k">confidence</span>
        <div style={{ flex: 1 }}><Meter value={sig.confidence} /></div>
        <span className="mono t-hi" style={{ fontSize: 11, fontWeight: 700 }}>{sig.confidence}</span>
      </div>

      <div className="price-grid">
        <div className="price-cell"><div className="l">Entry</div><div className="v">{fmt(sig.entry)}</div></div>
        <div className="price-cell"><div className="l">Stop</div><div className="v t-down">{fmt(sig.stop)}</div></div>
        <div className="price-cell"><div className="l">TP1</div><div className="v t-up">{fmt(sig.tp1)}</div></div>
        <div className="price-cell"><div className="l">TP2</div><div className="v t-up">{fmt(sig.tp2)}</div></div>
      </div>

      <div style={{ padding: '6px 9px' }}>
        <div style={{ display: 'flex', gap: 10, marginBottom: 5, flexWrap: 'wrap' }}>
          <span className="mono t-mid" style={{ fontSize: 10 }}>
            R:R <b className="t-hi">{sig.rr1}</b> / <b className="t-hi">{sig.rr2}</b>
          </span>
          {sig.sizing?.lots != null && (
            <span className="mono t-mid" style={{ fontSize: 10 }}>
              size <b className="t-hi">{sig.sizing.lots}</b> lot
            </span>
          )}
          {sig.sizing?.net_rr2 != null && (
            <span className="mono t-mid" style={{ fontSize: 10 }}>
              net <b className="t-hi">{sig.sizing.net_rr2}R</b>
            </span>
          )}
          <span className="chip chip-mute">{sig.entry_type}</span>
          {sig.stage && <span className={stageChip(sig.stage)}
            title={sig.stage === 'FORMING'
              ? 'On a bar that is still open - it can vanish at the close. Not tradeable yet.'
              : `Lifecycle: ${sig.stage}`}>{sig.stage}</span>}
        </div>

        {sig.evidence.filter((e) => e.weight > 0).slice(0, 4).map((e, i) => (
          <div className="evidence-row" key={i}>
            <span className="ic t-up">+</span>
            <span className="t-mid">{e.text}</span>
          </div>
        ))}
        {sig.against.slice(0, 2).map((a, i) => (
          <div className="evidence-row" key={`a${i}`}>
            <span className="ic t-down">−</span>
            <span className="t-dim">{a}</span>
          </div>
        ))}

        {sig.reason && (
          <div className="t-dim" style={{ fontSize: 9.5, marginTop: 5, fontStyle: 'italic' }}>
            {sig.reason}
          </div>
        )}

        {sig.status === 'qualified' && sig.stage === 'FORMING' && (
          <div className="t-dim" style={{ fontSize: 9.5, marginTop: 6 }}>
            Forming - becomes tradeable if the setup survives the bar close.
          </div>
        )}
        {sig.status === 'qualified' && (!sig.stage || sig.stage === 'FINAL') && (
          <button
            className="tool-btn"
            style={{
              width: '100%', marginTop: 7, justifyContent: 'center',
              borderColor: tradingEnabled ? 'var(--warn-dim)' : 'var(--line-strong)',
              color: tradingEnabled ? 'var(--warn)' : 'var(--ink-dim)',
            }}
            disabled={!tradingEnabled}
            onClick={(e) => { e.stopPropagation(); onPlace() }}
            title={tradingEnabled
              ? 'Send this order to MT5 via the bridge'
              : 'Execution is disabled — start the bridge with --enable-trading'}
          >
            {tradingEnabled ? 'Place order…' : 'Execution disabled'}
          </button>
        )}
      </div>
    </div>
  )
}

function PatternCard({ p, onChart }: { p: Pattern; onChart?: boolean }) {
  const col = p.direction === 'bullish' ? 'var(--bull)'
    : p.direction === 'bearish' ? 'var(--bear)' : 'var(--info)'
  return (
    // No opacity for `actionable` any more: the panel filters on it, so every
    // card that reaches here is live and they all draw at full strength.
    <div className="pattern-card" style={{ borderLeftColor: col }}>
      <div className="pattern-top">
        <span className="pattern-name" style={{ color: col }}>{p.label}</span>
        {/* Which of these the chart has actually named. Without it the two
            read as contradicting each other: the panel lists five, the chart
            labels two, and nothing says they are the same ranking. */}
        {onChart && <span className="chip chip-mute" title="Named on the chart">on chart</span>}
        <span className={p.status === 'confirmed' ? 'chip chip-up' : 'chip chip-warn'}>
          {p.status}
        </span>
      </div>
      <div className="pattern-meta">
        <span>quality <b>{p.quality}</b></span>
        <span>break <b>{fmt(p.break_level)}</b></span>
        <span>target <b>{fmt(p.target)}</b></span>
      </div>
      <div className="pattern-meta" style={{ marginTop: 2 }}>
        <span>R:R <b>{p.rr}</b></span>
        <span>{signed(p.distance_to_break_atr, 1)} ATR away</span>
        <span>{p.age_bars} bars old</span>
      </div>
      <div className="t-dim" style={{ fontSize: 9, marginTop: 4, lineHeight: 1.4 }}>
        {p.notes.slice(0, 2).join(' · ')}
      </div>
    </div>
  )
}

export function RightRail({
  snap, signals, selectedId, onSelect, onPlace, tradingEnabled,
}: {
  snap: Snapshot | null
  signals: Signal[]
  selectedId: string | null
  onSelect: (id: string) => void
  onPlace: (sig: Signal) => void
  tradingEnabled: boolean
}) {
  const live = signals.filter((s) => s.status !== 'rejected')
  /**
   * Patterns you can still do something about.
   *
   * `actionable` is false once a pattern is confirmed AND more than
   * CONFIRM_WINDOW bars have passed since it completed - it broke, the move
   * ran, and what is left is history. Those used to be listed at 62%
   * opacity, which asks the panel to be read twice: once to see the cards,
   * again to work out which of them still mean anything. The count in the
   * header was counting them too, so "8" could be eight patterns none of
   * which were live.
   *
   * They are dropped rather than collapsed behind a toggle. A pattern that
   * has already played out is not a thing you go looking for in a side
   * panel; the level it left behind is already carried by levels and zones.
   */
  const livePatterns = (snap?.patterns ?? []).filter((p: Pattern) => p.actionable)
  const mom = snap?.momentum
  const mtf = snap?.mtf
  const trend = snap?.trend
  const rev = snap?.reversal

  return (
    <aside className="rail">
      <Panel title="Live Signals" right={
        <span className={live.length ? 'chip chip-down pulse' : 'chip chip-mute'}>
          {live.length} active
        </span>
      }>
        {live.length === 0 ? (
          <Empty>
            No setup qualifies right now.<br />
            The engine is watching — signals appear here the moment a playbook
            and its gates both agree.
          </Empty>
        ) : live.map((s) => (
          <SignalCard
            key={s.id} sig={s} selected={s.id === selectedId}
            onSelect={() => onSelect(s.id)} onPlace={() => onPlace(s)}
            tradingEnabled={tradingEnabled}
          />
        ))}
      </Panel>

      <Panel title="Detected Patterns" right={
        <span className="chip chip-mute">{livePatterns.length}</span>
      }>
        {!livePatterns.length ? (
          <Empty>
            {snap?.patterns?.length
              ? 'Nothing live - every pattern found has already played out.'
              : 'No pattern above the quality floor.'}
          </Empty>
        ) : livePatterns.slice(0, 5).map((p, i) => (
          // The same ordering the chart features from, so the first
          // FEATURED_PATTERNS rows here are exactly the ones badged there.
          <PatternCard key={i} p={p} onChart={i < FEATURED_PATTERNS} />
        ))}
      </Panel>

      <Panel title="Trend Read">
        {!trend ? <Empty>—</Empty> : (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 7 }}>
              <span className={chipFor(trend.state?.includes('up') ? 'up' : trend.state?.includes('down') ? 'down' : '')}>
                {trend.state}
              </span>
              <span className="chip chip-mute">{trend.strength}</span>
              <span style={{ flex: 1 }} />
              <span className={`mono ${dirClass(trend.score)}`} style={{ fontSize: 11 }}>
                {signed(trend.score)}
              </span>
            </div>

            {snap?.regime && (
              <div style={{ marginBottom: 7 }}>
                <KV k="Regime" v={<span className="t-info">{snap.regime.label}</span>} />
                <KV k="Confidence" v={snap.regime.confidence} />
                <div className="t-dim" style={{ fontSize: 9.5, marginTop: 3, lineHeight: 1.45 }}>
                  {snap.regime.guidance}
                </div>
              </div>
            )}

            {rev?.detected && (
              <div style={{
                padding: '5px 7px', marginBottom: 7, borderRadius: 'var(--r-sm)',
                background: rev.bias === 'bullish' ? 'var(--bull-glow)' : 'var(--bear-glow)',
                border: `1px solid ${rev.bias === 'bullish' ? 'var(--bull-dim)' : 'var(--bear-dim)'}`,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
                  <span className={`${biasClass(rev.bias)}`} style={{ fontSize: 9.5, fontWeight: 800, letterSpacing: '0.08em' }}>
                    {rev.confirmed ? 'REVERSAL CONFIRMED' : 'REVERSAL FORMING'}
                  </span>
                  <span className="mono t-hi" style={{ fontSize: 10 }}>{rev.score}</span>
                </div>
                {rev.components?.slice(0, 3).map((c: string, i: number) => (
                  <div key={i} className="t-mid" style={{ fontSize: 9.5 }}>· {c}</div>
                ))}
              </div>
            )}

            <div className="panel-head" style={{ padding: '4px 0', borderBottom: 'none' }}>Timeframes</div>
            {mtf?.rows?.length ? mtf.rows.map((r: any) => (
              <div className="kv" key={r.tf}>
                <span className="kv-k mono">{r.tf}</span>
                <span className={`kv-v ${biasClass(r.state?.includes('up') ? 'up' : r.state?.includes('down') ? 'down' : '')}`}
                  style={{ fontSize: 10 }}>
                  {r.state} · {r.strength}
                </span>
              </div>
            )) : <div className="t-dim" style={{ fontSize: 10 }}>not evaluated</div>}

            {mtf && (
              <div style={{ marginTop: 6, paddingTop: 6, borderTop: '1px solid var(--line-soft)' }}>
                <ScoreRow label="Alignment" value={mtf.score ?? 0} />
                <div className="t-dim" style={{ fontSize: 9.5 }}>{mtf.verdict}</div>
              </div>
            )}

            <div style={{ marginTop: 7, paddingTop: 6, borderTop: '1px solid var(--line-soft)' }}>
              <KV k="Structure" v={trend.structure} />
              <KV k="Invalidation" v={fmt(trend.invalidation)} cls="t-warn" />
            </div>
          </>
        )}
      </Panel>

      <Panel title="Momentum">
        {!mom ? <Empty>—</Empty> : (
          <>
            <ScoreRow label="RSI" value={mom.rsi_score} />
            <ScoreRow label="MACD" value={mom.macd_score} />
            <ScoreRow label="ROC" value={mom.roc_score} />
            <ScoreRow label="Candles" value={mom.candles_score} />
            <ScoreRow label="Flow" value={mom.flow_score} />
            <div style={{ marginTop: 5, paddingTop: 5, borderTop: '1px solid var(--line-soft)' }}>
              <ScoreRow label="Total" value={mom.total} />
            </div>
            {mom.divergence?.kind && (
              <div style={{
                marginTop: 6, padding: '5px 7px', borderRadius: 'var(--r-sm)',
                background: 'var(--warn-glow)', border: '1px solid var(--warn-dim)',
              }}>
                <div className="t-warn" style={{ fontSize: 9, fontWeight: 800, letterSpacing: '0.08em' }}>
                  {mom.divergence.kind.replace(/_/g, ' ').toUpperCase()}
                </div>
                <div className="t-mid" style={{ fontSize: 9.5, marginTop: 2 }}>
                  {mom.divergence.note}
                </div>
              </div>
            )}
          </>
        )}
      </Panel>

    </aside>
  )
}
