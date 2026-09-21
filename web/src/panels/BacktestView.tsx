import React, { useEffect, useRef, useState } from 'react'
import { ChartPane } from '../chart/ChartPane'
import type { Bar, LayoutOpts, Overlays, Signal, Snapshot } from '../chart/types'

/*
 * The backtest chart is historical. Position rails show the CURRENT book and
 * news marks are anchored to now, so both would be describing today over a run
 * that finished days ago. Grid only.
 *
 * Module-level for a stable reference: an inline literal is a new object every
 * render, which would retrigger the engine effect and redraw on every tick.
 */
const BT_LAYOUT: LayoutOpts = { grid: true, newsMarks: false, positions: false }
import { api, toBars } from '../lib/api'
import { dateUTC, dirClass, fmt, pct, profitFactor, signed } from '../lib/format'
import { Empty } from './common'

/**
 * The BACKTEST tab: its own chart, its own data, its own engine state.
 *
 * Deliberately a separate chart instance from LIVE rather than a mode switch on
 * one. Sharing a chart meant scrubbing history quietly mutated the live view's
 * scroll position and overlay set, and twice made it ambiguous which market
 * state was on screen - which is exactly the kind of ambiguity a trading
 * terminal cannot afford.
 */
export type BacktestRun = {
  runId: string | null
  status: any
  result: any
  error: string | null
}

export const EMPTY_RUN: BacktestRun = { runId: null, status: null, result: null, error: null }

export function BacktestView({ symbol, timeframes, overlays, run, onRun }: {
  symbol: string
  timeframes: string[]
  overlays: Overlays
  run: BacktestRun
  onRun: (patch: Partial<BacktestRun>) => void
}) {
  const [tf, setTf] = useState('5m')
  const [start, setStart] = useState('2026-01-01')
  const [end, setEnd] = useState('2026-04-01')
  const [equity, setEquity] = useState(25000)
  const [step, setStep] = useState(3)
  const [respectRegime, setRespectRegime] = useState(true)
  const [useM1, setUseM1] = useState(true)

  // Run state lives in the shell, not here. Owning it locally meant switching
  // to the LIVE tab unmounted this component and orphaned the poll: the run
  // kept going server-side and its result was never collected, so a five
  // minute backtest silently vanished if you glanced at the live chart.
  const { runId, status, result, error } = run

  const [bars, setBars] = useState<Bar[]>([])
  const [snap, setSnap] = useState<Snapshot | null>(null)
  const [sig, setSig] = useState<Signal | null>(null)
  const [cursor, setCursor] = useState(0)

  // Load the chart for the selected window so there is something to look at
  // before a run finishes.
  useEffect(() => {
    const from = new Date(start + 'T00:00:00Z').getTime()
    const to = new Date(end + 'T00:00:00Z').getTime()
    if (!Number.isFinite(from) || !Number.isFinite(to)) return
    api.history(symbol, tf, from, to)
      .then((r) => { setBars(toBars(r.bars)); setCursor(r.bars.length - 1) })
      .catch(() => setBars([]))
  }, [symbol, tf, start, end])

  const launch = async () => {
    onRun({ error: null, result: null, status: null })
    try {
      const { run_id } = await api.backtestStart({
        symbol, tf, start, end, equity, step,
        respect_regime: respectRegime, use_m1_resolution: useM1,
      })
      onRun({ runId: run_id })
    } catch (e: any) { onRun({ error: e.message }) }
  }

  // The poll itself now lives in App so it keeps running across tab switches;
  // see useBacktestPoll there.

  /** Scrub the chart to a trade and re-run analysis AS OF that bar. */
  const gotoTrade = async (t: any) => {
    const idx = bars.findIndex((b) => b.t >= t.entry_t)
    if (idx >= 0) setCursor(idx)
    try {
      const r = await api.replayAnalyse(symbol, tf, t.entry_t)
      setSnap(r.snapshot)
      const match = r.signals.find((s) => s.playbook === t.playbook && s.side === t.side)
      setSig(match ?? r.signals[0] ?? null)
      setBars(toBars(r.bars))
    } catch { /* leave the chart as-is */ }
  }

  const s = result?.summary
  const running = status && ['queued', 'running'].includes(status.status)

  return (
    <div className="bt-layout">
      <div className="bt-config">
        <div className="panel-head" style={{ padding: '0 0 8px', borderBottom: '1px solid var(--line-soft)', marginBottom: 9 }}>
          Run configuration
        </div>

        <div className="bt-field">
          <label>Timeframe</label>
          <select value={tf} onChange={(e) => setTf(e.target.value)}>
            {timeframes.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div className="bt-field">
          <label>From</label>
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </div>
        <div className="bt-field">
          <label>To</label>
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </div>
        <div className="bt-field">
          <label>Starting equity</label>
          <input type="number" value={equity} onChange={(e) => setEquity(+e.target.value)} />
        </div>
        <div className="bt-field">
          <label>Bar step (1 = every bar)</label>
          <input type="number" min={1} max={20} value={step} onChange={(e) => setStep(+e.target.value)} />
          <div className="t-dim" style={{ fontSize: 9, marginTop: 3, lineHeight: 1.4 }}>
            Higher steps sweep a long period faster at the cost of missing
            setups that appear and resolve between evaluations.
          </div>
        </div>
        <div className="bt-field">
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, textTransform: 'none', letterSpacing: 0 }}>
            <input type="checkbox" checked={respectRegime} style={{ width: 'auto' }}
              onChange={(e) => setRespectRegime(e.target.checked)} />
            <span style={{ fontSize: 10.5, color: 'var(--ink)' }}>Respect regime filter</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 5, textTransform: 'none', letterSpacing: 0 }}>
            <input type="checkbox" checked={useM1} style={{ width: 'auto' }}
              onChange={(e) => setUseM1(e.target.checked)} />
            <span style={{ fontSize: 10.5, color: 'var(--ink)' }}>Settle ambiguous bars on M1</span>
          </label>
        </div>

        <button className="btn-primary" onClick={launch} disabled={!!running}>
          {running ? `Running… ${status?.progress ?? 0}%` : 'Run backtest'}
        </button>

        {running && (
          <div style={{ marginTop: 8 }}>
            <div className="meter"><div className="meter-fill"
              style={{ left: 0, width: `${status?.progress ?? 0}%`, background: 'var(--info)' }} /></div>
            <div className="t-dim mono" style={{ fontSize: 9.5, marginTop: 4 }}>
              {status?.trades ?? 0} trades so far
            </div>
          </div>
        )}
        {error && (
          <div style={{ marginTop: 8, padding: '6px 8px', borderRadius: 'var(--r-sm)', background: 'var(--bear-glow)', border: '1px solid var(--bear-dim)' }}>
            <div className="t-down" style={{ fontSize: 10 }}>{error}</div>
          </div>
        )}

        {result?.integrity && (
          <div style={{ marginTop: 12, paddingTop: 9, borderTop: '1px solid var(--line-soft)' }}>
            <div className="panel-head" style={{ padding: 0, borderBottom: 'none' }}>Integrity</div>
            <div className="t-mid" style={{ fontSize: 9.5, marginTop: 5, lineHeight: 1.5 }}>
              {result.integrity.ambiguous_bars} of {s?.trades} trades hit stop and
              target inside one bar ({result.integrity.ambiguous_pct}%).{' '}
              {result.integrity.note}
            </div>
            <div className="t-dim" style={{ fontSize: 9.5, marginTop: 6, lineHeight: 1.5 }}>
              Funnel: {result.funnel?.detected} detected →{' '}
              {result.funnel?.qualified} qualified, {result.funnel?.watch} watch,{' '}
              {result.funnel?.rejected} rejected.
            </div>
          </div>
        )}
      </div>

      <div className="bt-main">
        {s && (
          <div className="stat-grid">
            <div className="stat-cell">
              <div className="l">Trades</div><div className="v">{s.trades}</div>
              <div className="s">{result.bars.toLocaleString()} bars</div>
            </div>
            <div className="stat-cell">
              <div className="l">Win rate</div>
              <div className="v" style={{ color: s.win_rate >= 50 ? 'var(--bull)' : 'var(--warn)' }}>
                {pct(s.win_rate)}
              </div>
              <div className="s">{s.avg_win_r}R / {s.avg_loss_r}R</div>
            </div>
            <div className="stat-cell">
              <div className="l">Profit factor</div>
              <div className="v" style={{
                color: s.profit_factor === null || s.profit_factor >= 1.3 ? 'var(--bull)'
                  : s.profit_factor >= 1 ? 'var(--warn)' : 'var(--bear)',
              }}>
                {profitFactor(s.profit_factor)}
              </div>
              <div className="s">gross win / gross loss</div>
            </div>
            <div className="stat-cell">
              <div className="l">Expectancy</div>
              <div className={`v ${dirClass(s.expectancy_r)}`}>{signed(s.expectancy_r, 3)}R</div>
              <div className="s">per trade</div>
            </div>
            <div className="stat-cell">
              <div className="l">Return</div>
              <div className={`v ${dirClass(s.return_pct)}`}>{signed(s.return_pct, 2)}%</div>
              <div className="s">{fmt(s.start_equity, 0)} → {fmt(s.end_equity, 0)}</div>
            </div>
            <div className="stat-cell">
              <div className="l">Max drawdown</div>
              <div className="v t-down">{pct(s.max_drawdown_pct)}</div>
              <div className="s">peak to trough</div>
            </div>
          </div>
        )}

        <ChartPane
          bars={bars} snapshot={snap} signal={sig} overlays={overlays} digits={2}
          layout={BT_LAYOUT}
          badge={<span className="chip chip-info">BACKTEST</span>}
        />

        <div style={{ height: 210, minHeight: 0, background: 'var(--bg-panel)', borderTop: '1px solid var(--line)', display: 'flex', flexDirection: 'column' }}>
          {!result ? (
            <Empty>
              Configure a window and run a backtest.<br />
              It replays the identical analyse → generate → qualify path the live
              tab uses, on an expanding window, with costs charged.
            </Empty>
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1, background: 'var(--line)', flex: 1, minHeight: 0 }}>
              <div style={{ background: 'var(--bg-panel)', overflow: 'auto', minHeight: 0 }}>
                <div className="panel-head">By playbook</div>
                <table className="tabular">
                  <thead><tr>
                    <th>Playbook</th><th className="num">n</th><th className="num">Win</th>
                    <th className="num">Exp R</th><th className="num">PF</th><th className="num">P/L</th>
                  </tr></thead>
                  <tbody>
                    {Object.entries(result.by_playbook ?? {})
                      .sort((a: any, b: any) => b[1].trades - a[1].trades)
                      .map(([k, v]: any) => (
                        <tr key={k}>
                          <td className="t-hi">{k.replace(/_/g, ' ')}</td>
                          <td className="num">{v.trades}</td>
                          <td className="num">{pct(v.win_rate, 0)}</td>
                          <td className={`num ${dirClass(v.expectancy_r)}`}>{signed(v.expectancy_r, 3)}</td>
                          <td className="num">{profitFactor(v.profit_factor)}</td>
                          <td className={`num ${dirClass(v.net_pnl)}`}>{signed(v.net_pnl, 0)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
                <div className="panel-head">By regime</div>
                <table className="tabular">
                  <thead><tr>
                    <th>Regime</th><th className="num">n</th><th className="num">Win</th>
                    <th className="num">Exp R</th><th className="num">P/L</th>
                  </tr></thead>
                  <tbody>
                    {Object.entries(result.by_regime ?? {})
                      .sort((a: any, b: any) => b[1].trades - a[1].trades)
                      .map(([k, v]: any) => (
                        <tr key={k}>
                          <td className="t-hi">{k}</td>
                          <td className="num">{v.trades}</td>
                          <td className="num">{pct(v.win_rate, 0)}</td>
                          <td className={`num ${dirClass(v.expectancy_r)}`}>{signed(v.expectancy_r, 3)}</td>
                          <td className={`num ${dirClass(v.net_pnl)}`}>{signed(v.net_pnl, 0)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>

                {/* The cross-tab is the diagnostic that matters: a playbook can
                    look flat overall while being strongly positive in one
                    regime and negative in another. */}
                <div className="panel-head">Playbook × regime</div>
                <table className="tabular">
                  <thead><tr>
                    <th>Playbook / regime</th><th className="num">n</th>
                    <th className="num">Win</th><th className="num">Exp R</th><th className="num">P/L</th>
                  </tr></thead>
                  <tbody>
                    {Object.entries(result.by_playbook_regime ?? {})
                      .filter(([, v]: any) => v.trades >= 3)
                      .sort((a: any, b: any) => b[1].expectancy_r - a[1].expectancy_r)
                      .map(([k, v]: any) => {
                        const [pb, rg] = k.split('/')
                        return (
                          <tr key={k}>
                            <td>
                              <span className="t-hi">{pb.replace(/_/g, ' ')}</span>
                              <span className="t-dim"> / {rg}</span>
                            </td>
                            <td className="num">{v.trades}</td>
                            <td className="num">{pct(v.win_rate, 0)}</td>
                            <td className={`num ${dirClass(v.expectancy_r)}`}>{signed(v.expectancy_r, 3)}</td>
                            <td className={`num ${dirClass(v.net_pnl)}`}>{signed(v.net_pnl, 0)}</td>
                          </tr>
                        )
                      })}
                  </tbody>
                </table>

                <div className="panel-head">By session</div>
                <table className="tabular">
                  <thead><tr>
                    <th>Session</th><th className="num">n</th><th className="num">Win</th><th className="num">Exp R</th>
                  </tr></thead>
                  <tbody>
                    {Object.entries(result.by_session ?? {}).map(([k, v]: any) => (
                      <tr key={k}>
                        <td className="t-hi">{k}</td>
                        <td className="num">{v.trades}</td>
                        <td className="num">{pct(v.win_rate, 0)}</td>
                        <td className={`num ${dirClass(v.expectancy_r)}`}>{signed(v.expectancy_r, 3)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div style={{ background: 'var(--bg-panel)', overflow: 'auto', minHeight: 0 }}>
                <div className="panel-head">
                  Trades <span className="t-dim">click a row to replay that bar</span>
                </div>
                <table className="tabular">
                  <thead><tr>
                    <th>Entry</th><th>Playbook</th><th>Side</th>
                    <th className="num">In</th><th className="num">Out</th>
                    <th>Outcome</th><th className="num">R</th><th className="num">P/L</th>
                    <th className="num">MAE</th><th className="num">MFE</th>
                  </tr></thead>
                  <tbody>
                    {(result.trades ?? []).slice(0, 600).map((t: any, i: number) => (
                      <tr key={i} style={{ cursor: 'pointer' }} onClick={() => gotoTrade(t)}>
                        <td className="t-dim">{dateUTC(t.entry_t)}</td>
                        <td>{t.playbook.replace(/_/g, ' ')}</td>
                        <td className={t.side === 'buy' ? 't-up' : 't-down'}>{t.side}</td>
                        <td className="num">{fmt(t.entry_price)}</td>
                        <td className="num">{fmt(t.exit_price)}</td>
                        <td className={
                          t.outcome === 'tp2' ? 't-up' : t.outcome === 'tp1' ? 't-info'
                            : t.outcome === 'stop' ? 't-down' : 't-mid'
                        }>{t.outcome}</td>
                        <td className={`num ${dirClass(t.r_multiple)}`}>{signed(t.r_multiple, 2)}</td>
                        <td className={`num ${dirClass(t.pnl)}`}>{signed(t.pnl, 2)}</td>
                        <td className="num t-dim">{fmt(t.mae_r, 2)}</td>
                        <td className="num t-dim">{fmt(t.mfe_r, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
