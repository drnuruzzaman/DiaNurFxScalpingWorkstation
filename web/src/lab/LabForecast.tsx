/**
 * LabForecast - the forecast engine in the backtest lab (Phase B).
 *
 *   ForecastSide   the right panel's FORECAST tab: the range forecast at the
 *                  bar on screen (model where it passed its promotion gate,
 *                  baseline where it did not - always labelled), why the cone
 *                  is wide or narrow, how the model scored, a pinned cone
 *                  scored against the bars since, baseline trade odds, and
 *                  the higher timeframes
 *   ForecastDock   the dock's FORECAST tab: the batch scorecard (gates, decay,
 *                  where the model helps) and this session's settled forecasts
 *
 * Every number shown here is a measurement against a baseline, not advice.
 */
import React, { useEffect, useMemo, useState } from 'react'
import type { Bar } from '../chart/types'
import { fmt } from '../lib/format'
import { lab } from './labApi'
import type { LabBarrierOdds, LabChallenge, LabConfidence, LabCondGate, LabForecast, LabForecastSettled,
  LabForecastSession, LabForecastSummary, LabGate, Q3 } from './types'

const TF_MIN: Record<string, number> = { '5m': 5, '15m': 15, '1h': 60, '4h': 240 }
const STATES = ['uptrend', 'downtrend', 'range', 'transition', 'squeeze']
const SESSION_LABEL: Record<string, string> = {
  closed: 'the daily break', sydney: 'the Sydney session', tokyo: 'the Tokyo session',
  london: 'the London session', newyork: 'the New York session', overlap: 'the London / New York overlap',
}

const pc = (x: number | null | undefined, d = 1) =>
  x == null || !Number.isFinite(x) ? '—' : `${(x * 100).toFixed(d)}%`
const spc = (x: number | null | undefined, d = 1) =>
  x == null || !Number.isFinite(x) ? '—' : `${x > 0 ? '+' : ''}${(x * 100).toFixed(d)}%`
const atr = (x: number | null | undefined, d = 2) =>
  x == null || !Number.isFinite(x) ? '—' : x.toFixed(d)
const horizonText = (tf: string, h: number) => {
  const m = (TF_MIN[tf] ?? 0) * h
  return m >= 60 ? `${+(m / 60).toFixed(1)}h` : `${m} min`
}
const when = (ms: number) => {
  const d = new Date(ms)
  return `${d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', timeZone: 'UTC' })} `
    + `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
}
/** Where a travelled distance sits against its [P20, P50, P80]. */
const zone = (x: number, q: Q3) =>
  x < q[0] ? 'short of P20' : x <= q[1] ? 'P20-P50' : x <= q[2] ? 'P50-P80' : 'beyond P80'

function GateTable({ gate }: { gate: LabGate }) {
  const years = Object.keys(gate.years).sort()
  return (
    <table className="fc-table mono">
      <thead><tr><th /><th>up skill</th><th>down skill</th><th>P20-80 held</th></tr></thead>
      <tbody>
        {years.map((y) => {
          const r = gate.years[y]
          const cell = (s?: { skill: number | null; lo: number | null; hi: number | null }) => {
            if (!s || s.skill == null) return <td>—</td>
            const ok = (s.lo ?? -1) > 0
            return <td className={ok ? 't-up' : 't-dim'} title={`90% interval ${spc(s.lo)} to ${spc(s.hi)}`}>
              {spc(s.skill)}</td>
          }
          return (
            <tr key={y}>
              <td className="t-dim">{y}</td>{cell(r.up)}{cell(r.dn)}
              <td>{pc(r.up?.cov, 0)} / {pc(r.dn?.cov, 0)}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function OddsRow({ name, o }: { name: string; o?: LabBarrierOdds }) {
  if (!o || o.target == null) return null
  // The conditions' version replaces the baseline only where it was promoted;
  // otherwise it is shown as a note - numbers visible, not used.
  const m = o.model
  const use = m && m.promoted ? m : null
  const shown = use ?? { target: o.target, stop: o.stop ?? 0, neither: o.neither ?? 0 }
  const liftPp = m ? (m.target - (o.target ?? 0)) * 100 : null
  return (
    <>
      <div className="fc-odds">
        <span className="t-dim">{name} <span className="mono">{atr(o.target_atr, 2)}</span></span>
        <div className="fc-bar" title={`target first ${pc(shown.target)} · stop first ${pc(shown.stop)} · neither in 6h ${pc(shown.neither)}`}>
          <i className="up" style={{ width: pc(shown.target, 2) }} />
          <i className="down" style={{ width: pc(shown.stop, 2) }} />
          <i className="none" style={{ width: pc(shown.neither, 2) }} />
        </div>
        <span className="mono"><b className="t-up">{pc(shown.target, 0)}</b> · <b className="t-down">{pc(shown.stop, 0)}</b> · <span className="t-dim">{pc(shown.neither, 0)}</span></span>
        {!o.inside && <span className="t-warn" title="Outside the measured stop/target grid (0.25-6 ATR): clamped to its edge">!</span>}
      </div>
      {m && liftPp != null && Math.abs(liftPp) >= 0.5 && (
        <div className="fc-sub t-dim" title={`${m.template} · ${m.why.join(' · ')} · ${Math.round(m.neff)} effective samples`}>
          conditions {liftPp > 0 ? '+' : ''}{liftPp.toFixed(1)}pp target-first ({m.why.join(', ')})
          {use ? '' : ' - not promoted, baseline kept'}
        </div>
      )}
    </>
  )
}

const CONF_LABEL: Record<LabConfidence, string> = { high: 'HIGH', moderate: 'MODERATE', baseline: '≈ BASELINE' }
function Conf({ c, promoted }: { c: LabConfidence; promoted: boolean }) {
  const cls = c === 'high' ? 'chip-up' : c === 'moderate' ? 'chip-info' : 'chip-warn'
  return (
    <span className={`chip ${cls}`} title={
      c === 'high' ? 'The 90% interval excludes the baseline, the cell has 500+ effective samples, and this output passed its promotion gate'
        : c === 'moderate' ? `The interval excludes the baseline, but ${promoted ? 'the cell has under 500 effective samples' : 'this output did not pass its promotion gate'}`
          : 'The interval includes the baseline - no edge worth reading here'}>
      {CONF_LABEL[c]}
    </span>
  )
}

function GateLine({ gate }: { gate: LabCondGate | null }) {
  if (!gate) return null
  const ys = Object.keys(gate.years).sort()
  return (
    <div className="fc-note">scored {ys.map((y) => {
      const r = gate.years[y]
      return <span key={y} className={(r.lo ?? -1) > 0 ? 't-up' : 't-dim'} title={`90% interval ${spc(r.lo, 2)} to ${spc(r.hi, 2)}`}>
        {' '}{y} {spc(r.skill, 1)}</span>
    })} · {gate.promoted ? 'promoted' : 'not promoted - the baseline is what counts'}</div>
  )
}

/**
 * Phase E: the supervisor argues AGAINST the read at the bar on screen. The engine
 * computes every number and the findings; a language model (if configured) only
 * writes them up, and its reply is withheld if it uses a number the engine did not.
 */
function ChallengePanel({ barT }: { barT?: number }) {
  const [res, setRes] = useState<LabChallenge | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { setRes(null); setErr(null) }, [barT])
  const ask = (fresh: boolean) => {
    setBusy(true)
    setErr(null)
    lab.forecastChallenge(undefined, fresh).then(setRes)
      .catch((e) => setErr(String(e?.message ?? e))).finally(() => setBusy(false))
  }
  return (
    <>
      <div className="lab-sec">Challenge <span className="t-dim">Phase E · where this read is weak</span></div>
      {!res && (
        <button className="tool-btn lab-mini" onClick={() => ask(false)} disabled={busy}
          title="The engine's rules (and the AI supervisor, if a key is configured) argue against the forecast and any live or qualified signal on this bar">
          {busy ? 'Challenging…' : '⚖ Challenge this read'}
        </button>
      )}
      {res && (
        <div className="fc-pin">
          <div className="fc-note" style={{ marginTop: 0 }}>
            <span className={`chip ${res.source === 'llm' ? 'chip-info' : 'chip-warn'}`}>
              {res.source === 'llm' ? `AI · ${res.model}` : 'ENGINE RULES'}</span>
            {' '}{res.subject ? `${res.subject.side.toUpperCase()} ${res.subject.label}` : 'the forecast only - no live or qualified signal'}
          </div>
          <div className="fc-challenge">{res.text}</div>
          {res.note && <div className={`fc-note ${res.withheld ? 't-warn' : ''}`}>{res.note}</div>}
          <details className="fc-note">
            <summary>fact sheet - {res.facts.length} lines the engine computed</summary>
            <ul className="fc-facts">{res.facts.map((x, i) => <li key={i}>{x}</li>)}</ul>
          </details>
          <button className="tool-btn lab-mini" onClick={() => ask(true)} disabled={busy}>
            {busy ? 'Challenging…' : 'Ask again'}</button>
        </div>
      )}
      {err && <div className="fc-note t-down">{err}</div>}
    </>
  )
}

export function ForecastSide(p: {
  forecast: LabForecast | null
  digits: number
  bars: Bar[]
  cursorV: number
  pinned: LabForecast | null
  onPin: () => void
  coneOn: boolean
  onCone: (on: boolean) => void
  onSeekTime?: (t: number) => void
}) {
  const f = p.forecast
  const dp = p.digits
  // A pinned cone, scored against the bars since its bar - never past the view.
  const pinRead = useMemo(() => {
    const pf = p.pinned
    if (!pf || !pf.available || pf.t == null || !pf.up || !pf.dn || !pf.base_up || !pf.base_dn) return null
    const i0 = p.bars.findIndex((b) => b.t === pf.t)
    if (i0 < 0) return null
    const H = pf.horizon ?? pf.up.length
    const end = Math.min(p.cursorV, i0 + H)
    if (end <= i0) return { waiting: true, pf, steps: 0, H }
    let hi = -Infinity, lo = Infinity
    for (let i = i0 + 1; i <= end; i++) { hi = Math.max(hi, p.bars[i].h); lo = Math.min(lo, p.bars[i].l) }
    const up = (hi - pf.close!) / pf.atr!, dn = (pf.close! - lo) / pf.atr!
    const k = end - i0
    const qu = (pf.promoted ? pf.up : pf.base_up)[k - 1], qd = (pf.promoted ? pf.dn : pf.base_dn)[k - 1]
    return { waiting: false, pf, steps: k, H, up, dn, qu, qd }
  }, [p.pinned, p.bars, p.cursorV])

  if (!f) return <div className="lab-empty">Step the replay to see the forecast at the bar on screen.</div>
  if (!f.available || !f.up || !f.dn || !f.base_up || !f.base_dn) {
    return (
      <div className="lab-empty">
        No range forecast here: {f.reason ?? 'unavailable'}.
        {f.mtf?.length ? null : <><br />Build it with <span className="mono">python tools/forecast_build.py</span>.</>}
      </div>
    )
  }
  const H = f.horizon ?? f.up.length
  const model = f.promoted
  const qU = (model ? f.up : f.base_up)[H - 1]
  const qD = (model ? f.dn : f.base_dn)[H - 1]
  const bU = f.base_up[H - 1], bD = f.base_dn[H - 1]
  const c = f.cell
  const why: string[] = []
  if (c) {
    why.push(`${String(c.hour).padStart(2, '0')}:00 server time, ${SESSION_LABEL[c.session ?? ''] ?? 'this hour'}`)
    why.push(c.news ? `a ${c.news_next ?? 'tier-one'} release is due inside the horizon (in ${Math.round(c.news_next_min)} min)`
      : c.news_next && c.news_next_min < 1440 ? `next release ${c.news_next} in ${Math.round(c.news_next_min)} min - after the horizon`
        : 'no tier-one release due')
    why.push(c.vol_name + (c.vol >= 3 ? ' - compressed, ranges tend to expand' : c.vol <= 1 ? ' - stretched, ranges tend to shrink back' : ''))
  }
  return (
    <>
      <div className="fc-head">
        <div>
          <div className="lab-sec" style={{ margin: 0 }}>Range forecast</div>
          <div className="t-dim" style={{ fontSize: 9.5 }}>
            next {H} × {f.tf} ({horizonText(f.tf, H)}) from the close of the {when(f.t!)} bar · {fmt(f.close, dp)} · ATR {fmt(f.atr, dp)}
          </div>
        </div>
        <span className={`chip ${model ? 'chip-up' : 'chip-warn'}`}
          title={model ? 'This timeframe\'s model beat the baseline in every scored year - its cone is drawn'
            : 'This timeframe\'s model did not beat the baseline in every scored year - the baseline cone is drawn'}>
          {model ? 'MODEL · PROMOTED' : 'BASELINE'}
        </span>
      </div>
      {f.stale && <div className="lab-gate warn">Built before the latest data or engine change - rebuild to extend: python tools/forecast_build.py</div>}

      <table className="fc-table fc-q mono">
        <thead><tr><th /><th>P20</th><th>P50</th><th>P80</th><th className="t-dim">usual P50</th></tr></thead>
        <tbody>
          <tr><td className="t-up">up</td>
            <td>+{atr(qU[0])}</td><td><b>+{atr(qU[1])}</b></td><td>+{atr(qU[2])}</td><td className="t-dim">+{atr(bU[1])}</td></tr>
          <tr><td /><td className="t-dim">{fmt(f.close! + qU[0] * f.atr!, dp)}</td><td className="t-dim">{fmt(f.close! + qU[1] * f.atr!, dp)}</td>
            <td className="t-dim">{fmt(f.close! + qU[2] * f.atr!, dp)}</td><td /></tr>
          <tr><td className="t-down">down</td>
            <td>-{atr(qD[0])}</td><td><b>-{atr(qD[1])}</b></td><td>-{atr(qD[2])}</td><td className="t-dim">-{atr(bD[1])}</td></tr>
          <tr><td /><td className="t-dim">{fmt(f.close! - qD[0] * f.atr!, dp)}</td><td className="t-dim">{fmt(f.close! - qD[1] * f.atr!, dp)}</td>
            <td className="t-dim">{fmt(f.close! - qD[2] * f.atr!, dp)}</td><td /></tr>
        </tbody>
      </table>
      <div className="fc-note">ATR multiples of the move from this close within the horizon: 60% of outcomes fall between P20 and P80.</div>

      {model && f.ratio != null && (
        <div className="fc-ratio">
          <b className={f.ratio >= 1 ? 't-warn' : 't-info'}>×{f.ratio.toFixed(2)}</b> the usual range for this timeframe
        </div>
      )}
      {why.length > 0 && (
        <div className="lab-read" style={{ marginTop: 6 }}>
          {why.map((w, i) => <div key={i}><span className="t-dim">·</span> {w}</div>)}
          {f.news && (
            <div title={`Each ${f.news.kind} release's range over the next ${H} bars against the same hour's no-release bars in the year before it. Only releases whose horizon closed before this bar count. Direction share is measured, not a tested forecast.`}>
              <span className="t-dim">·</span> around the {f.news.n} {f.news.kind} releases before this bar, the next {horizonText(f.tf, H)} ran
              {' '}<b className="t-warn">×{f.news.x.toFixed(2)}</b> a quiet hour <span className="t-dim">(middle half ×{f.news.x_p25.toFixed(2)}-×{f.news.x_p75.toFixed(2)}; {pc(f.news.p_up, 0)} closed up)</span>
            </div>
          )}
          {c && <div className="t-dim" style={{ fontSize: 9.5 }}>its cell has {Math.round(c.neff)} effective samples; {pc(c.w, 0)} of the number is the cell, the rest its parents (hour, then all hours)</div>}
        </div>
      )}

      <div className="lab-row">
        <button className={`tool-btn lab-mini ${p.pinned ? 'on' : ''}`} onClick={p.onPin}
          title="Keep this cone at its bar while the replay moves on, and score it against the bars since (P)">
          {p.pinned ? `📌 Unpin (${when(p.pinned.t ?? 0)})` : '📌 Pin cone here'}
        </button>
        <button className={`tool-btn lab-mini ${p.coneOn ? 'on' : ''}`} onClick={() => p.onCone(!p.coneOn)}>
          {p.coneOn ? 'Cone shown' : 'Cone hidden'}
        </button>
      </div>
      {pinRead && (
        <div className="fc-pin">
          {pinRead.waiting ? (
            <span className="t-dim">Pinned at {when(pinRead.pf.t!)} - step forward to see how price moves through it.</span>
          ) : (
            <>
              <div className="t-dim">Pinned at {when(pinRead.pf.t!)} · {pinRead.steps} of {pinRead.H} bars played</div>
              <div>high <b className="t-up mono">+{atr(pinRead.up)}</b> ATR - <span className="mono">{zone(pinRead.up!, pinRead.qu!)}</span> of step {pinRead.steps}</div>
              <div>low <b className="t-down mono">-{atr(pinRead.dn)}</b> ATR - <span className="mono">{zone(pinRead.dn!, pinRead.qd!)}</span> of step {pinRead.steps}</div>
            </>
          )}
        </div>
      )}

      {f.gate && (
        <>
          <div className="lab-sec">How this model scored <span className="t-dim">batch {f.run_id ?? '—'}</span></div>
          <GateTable gate={f.gate} />
          <div className="fc-note">Skill = pinball loss vs the baseline at the horizon (green: interval clears zero). {model
            ? 'It beat the baseline in every year on both sides and stayed calibrated, so its cone is drawn.'
            : 'It did not clear zero in every year, so the baseline cone is drawn instead.'}</div>
        </>
      )}

      {f.trades.length > 0 && (
        <>
          <div className="lab-sec">Trade odds <span className="t-dim">baseline · at this bar's cost</span></div>
          {f.trades.map((t) => (
            <div key={t.id} className="fc-trade">
              <div><b className={t.side === 'buy' ? 't-up' : 't-down'}>{t.side.toUpperCase()}</b>{' '}
                {t.kind === 'signal' ? (t.label ?? 'signal') : 'open position, from here'}{' '}
                <span className="t-dim">stop {atr(t.stop_atr)} ATR</span></div>
              {t.tp1 && <OddsRow name="TP1" o={t.tp1} />}
              {t.tp2 && <OddsRow name="TP2" o={t.tp2} />}
              {t.tp && <OddsRow name="TP" o={t.tp} />}
            </div>
          ))}
          <div className="fc-note">target first · stop first · neither within 72 × 5m - fixed barriers, if filled at entry.
            The live exit trails after TP1; only the lab's own trades show what that does.</div>
        </>
      )}

      {f.regime && (
        <>
          <div className="lab-sec">Regime in {H} bars <Conf c={f.regime.confidence} promoted={f.regime.promoted} /></div>
          <table className="fc-table mono">
            <thead><tr><th /><th>forecast</th><th className="t-dim">baseline</th></tr></thead>
            <tbody>
              {f.regime.p.map((x, i) => {
                const shown = f.regime!.promoted ? x : f.regime!.base[i]
                return (
                  <tr key={i} className={i === f.regime!.top ? 'fc-top' : ''}>
                    <td>{STATES[i]}{i === f.state_now ? <span className="t-dim"> · now</span> : null}</td>
                    <td><span className="fc-pbar"><i style={{ width: pc(shown, 1) }} /></span> {pc(shown, 0)}
                      {i === f.regime!.top && f.regime!.promoted ? <span className="t-dim"> [{pc(f.regime!.lo, 0)}-{pc(f.regime!.hi, 0)}]</span> : null}</td>
                    <td className="t-dim">{pc(f.regime!.base[i], 0)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div className="fc-note">in {f.regime.why.join(' · ')}: {Math.round(f.regime.neff)} effective samples,
            {' '}{pc(f.regime.w, 0)} weight on the cell</div>
          <GateLine gate={f.regime.gate} />
        </>
      )}

      {f.direction && (
        <div className={f.direction.confidence === 'baseline' || !f.direction.promoted ? 'fc-greyed' : ''}>
          <div className="lab-sec">Direction <Conf c={f.direction.confidence} promoted={f.direction.promoted} /></div>
          <div className="lab-read">
            <div><span className="t-dim">P(close higher in {H} bars)</span>{' '}
              <b className="mono">{pc(f.direction.p[H - 1])}</b>{' '}
              <span className="t-dim mono">[{pc(f.direction.lo)}-{pc(f.direction.hi)}] · baseline {pc(f.direction.base[H - 1])}</span></div>
            <div className="t-dim" style={{ fontSize: 9.5 }}>in {f.direction.why.join(' · ')} · {Math.round(f.direction.neff)} effective samples</div>
          </div>
          <GateLine gate={f.direction.gate} />
          {!f.direction.promoted && <div className="fc-note">Direction tables did not beat the baseline in every year - shown for context, not for decisions.</div>}
        </div>
      )}

      {f.analogs && f.analogs.n_eff > 0 && (
        <>
          <div className="lab-sec">Similar past moments
            {f.analogs.agreement && <span className={`chip ${f.analogs.agreement === 'agree' ? 'chip-up' : f.analogs.agreement === 'disagree' ? 'chip-down' : 'chip-info'}`}
              title="Do these moments point the same way as the direction table, against the baseline?">{f.analogs.agreement}</span>}</div>
          <div className="lab-read">
            <div className="t-dim" style={{ fontSize: 9.5 }}>{f.analogs.why?.join(' · ')} - {f.analogs.n.toLocaleString()} bars since {f.analogs.first_ms ? new Date(f.analogs.first_ms).getUTCFullYear() : '—'}, thinned to <b>{f.analogs.n_eff}</b> independent (one per {H} bars)</div>
            <div>closed higher <b className="mono">{pc(f.analogs.p_up, 0)}</b>
              {f.direction && <span className="t-dim"> (baseline {pc(f.direction.base[H - 1], 0)})</span>} · travelled up <b className="mono t-up">+{atr(f.analogs.up_med)}</b> down <b className="mono t-down">-{atr(f.analogs.dn_med)}</b> ATR (medians)</div>
            {(f.analogs.tp_buy != null || f.analogs.tp_sell != null) && (
              <div className="t-dim">1 ATR / 1R target first: buy {pc(f.analogs.tp_buy, 0)} · sell {pc(f.analogs.tp_sell, 0)}</div>
            )}
          </div>
          <table className="fc-table mono">
            <thead><tr><th>when</th><th>up</th><th>down</th><th>closed</th><th>regime after</th></tr></thead>
            <tbody>
              {f.analogs.rows.map((r) => (
                <tr key={r.t} className={r.in_session && p.onSeekTime ? 'fc-click' : ''}
                  title={r.in_session ? 'Go to this bar' : 'Before this session - start a session there to replay it'}
                  onClick={() => { if (r.in_session && p.onSeekTime) p.onSeekTime(r.t) }}>
                  <td className="t-dim">{new Date(r.t).toISOString().slice(0, 16).replace('T', ' ')}</td>
                  <td className="t-up">+{atr(r.up)}</td><td className="t-down">-{atr(r.dn)}</td>
                  <td className={r.ret_up ? 't-up' : 't-down'}>{r.ret_up ? 'higher' : 'lower'}</td>
                  <td className="t-dim">{STATES[r.state_h] ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="fc-note">A diagnostic, not a forecast: the same cell the tables use, outcomes known before this bar.</div>
        </>
      )}

      {f.mtf.length > 0 && (
        <>
          <div className="lab-sec">Timeframes <span className="t-dim">latest closed bar each</span></div>
          <table className="fc-table mono">
            <thead><tr><th>tf</th><th>next</th><th>P50 up</th><th>P50 dn</th><th>vs usual</th><th>P(up)</th><th /></tr></thead>
            <tbody>
              {f.mtf.map((r) => {
                const u = r.promoted ? r.up : r.base_up, d = r.promoted ? r.dn : r.base_dn
                return (
                  <tr key={r.tf}>
                    <td>{r.tf}</td><td className="t-dim">{horizonText(r.tf, r.horizon)}</td>
                    <td className="t-up">+{atr(u[1])}</td><td className="t-down">-{atr(d[1])}</td>
                    <td>{r.promoted ? `×${r.ratio.toFixed(2)}` : '—'}</td>
                    <td className="t-dim" title="direction table (not promoted - context only)">{pc(r.p_up_model, 0)}</td>
                    <td className={r.promoted ? 't-up' : 't-dim'} title={r.promoted ? 'range model promoted' : 'baseline range (model not promoted)'}>
                      {r.promoted ? 'model' : 'base'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {f.agreement && (
            <div className="fc-note">timeframes read <b>{f.agreement.label}</b> (P(up) spread {pc(f.agreement.spread, 1)})
              {f.agreement.promoted ? '' : ' - context only: no direction table passed its gate'}</div>
          )}
        </>
      )}
      <ChallengePanel barT={f.t} />
      <div className="fc-note">Measurements against a baseline on history - not trading advice.</div>
    </>
  )
}

// --------------------------------------------------------------------- dock
export function ForecastDock(p: {
  forecast: LabForecast | null
  onSeekBar: (i: number) => void
}) {
  const [sum, setSum] = useState<LabForecastSummary | null>(null)
  const [log, setLog] = useState<{ rows: LabForecastSettled[]; stats: LabForecastSession; tf?: string } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [ft, setFt] = useState<any>(null)
  const [nl, setNl] = useState<any>(null)
  // Every table here is the session symbol's own - never another symbol's gates.
  const symbol = p.forecast?.symbol
  useEffect(() => { lab.forecastNews(symbol).then(setNl).catch(() => {}) }, [symbol])
  const settledN = p.forecast?.session?.n ?? 0
  useEffect(() => { lab.forecastFilterTest(symbol).then(setFt).catch(() => {}) }, [p.forecast?.run_id, symbol])
  useEffect(() => {
    lab.forecastSummary(symbol).then(setSum).catch((e) => setErr(String(e?.message ?? e)))
  }, [p.forecast?.run_id, symbol])
  useEffect(() => {
    const t = window.setTimeout(() => { lab.forecastLog(200).then(setLog).catch(() => {}) }, 250)
    return () => window.clearTimeout(t)
  }, [Math.floor(settledN / 5)])
  const years = (sum?.eval_years ?? []).map(String)
  const s = log?.stats ?? p.forecast?.session
  return (
    <div className="fc-dock">
      <div className="fc-dock-col">
        <div className="lab-sec" style={{ marginTop: 0 }}>Range model scorecard
          <span className="t-dim"> {sum?.run_id ? `batch ${sum.run_id}` : ''} · scored {years.join(', ') || '—'} vs the baseline</span></div>
        {err && <div className="lab-gate warn">{err}</div>}
        {!sum?.range ? <div className="lab-none">No batch run for {symbol ?? 'this symbol'} yet - Settings › Market data:
          choose it, tick "rescore the forecast gates" and run it</div> : (
          <table className="fc-table mono">
            <thead><tr><th>tf</th><th>side</th>{years.map((y) => <th key={y}>{y}</th>)}<th>held</th><th>verdict</th><th>by step k (all years)</th></tr></thead>
            <tbody>
              {Object.entries(sum.range).map(([tf, g]) => (['up', 'dn'] as const).map((side) => (
                <tr key={tf + side}>
                  <td>{side === 'up' ? tf : ''}</td><td className={side === 'up' ? 't-up' : 't-down'}>{side === 'up' ? 'up' : 'down'}</td>
                  {years.map((y) => {
                    const r = g.years[y]?.[side]
                    return <td key={y} className={(r?.lo ?? -1) > 0 ? 't-up' : 't-dim'}
                      title={r ? `90% interval ${spc(r.lo)} to ${spc(r.hi)}` : ''}>{spc(r?.skill)}</td>
                  })}
                  <td className="t-dim">{years.map((y) => pc(g.years[y]?.[side]?.cov, 0)).join(' ')}</td>
                  <td>{side === 'up' ? (g.promoted ? <span className="chip chip-up">PROMOTED</span> : <span className="chip chip-warn">BASELINE</span>) : ''}</td>
                  <td><Spark values={g.decay[side]} /></td>
                </tr>
              )))}
            </tbody>
          </table>
        )}
        {sum?.range_conditions && (
          <div className="fc-note" style={{ marginTop: 6 }}>
            {Object.entries(sum.range_conditions).map(([tf, rows]) => {
              const best = [...rows].sort((a, b) => b.skill - a.skill).slice(0, 2)
              return <div key={tf}><b>{tf}</b> helps most: {best.map((r) => `${r.cond} ${r.group} (${r.side}) ${spc(r.skill)}`).join(', ')}</div>
            })}
          </div>
        )}
        {sum?.cond && (
          <>
            <div className="lab-sec">Conditional tables <span className="t-dim">Phase C · Brier skill vs each baseline</span></div>
            <table className="fc-table mono">
              <thead><tr><th>tf</th><th>output</th>{years.map((y) => <th key={y}>{y}</th>)}<th>verdict</th></tr></thead>
              <tbody>
                {Object.entries(sum.cond).flatMap(([tf, g]) => ([
                  ['state', 'regime @H', g.state], ['direction', 'direction @H', g.direction],
                  ['trade', 'trade odds', g.trade],
                ] as [string, string, LabCondGate | undefined][]).filter((x) => x[2]).map(([key, label, gg], i) => (
                  <tr key={tf + key}>
                    <td>{i === 0 ? tf : ''}</td><td>{label}</td>
                    {years.map((y) => {
                      const r = key === 'trade'
                        ? (g.trade?.templates?.['tpl_buy_1atr_1r']?.years?.[y])
                        : gg!.years[y]
                      return <td key={y} className={(r?.lo ?? -1) > 0 ? 't-up' : 't-dim'}
                        title={r ? `90% interval ${spc(r.lo, 2)} to ${spc(r.hi, 2)}` : ''}>{spc(r?.skill, key === 'state' ? 1 : 2)}</td>
                    })}
                    <td>{gg!.promoted ? <span className="chip chip-up">PROMOTED</span> : <span className="chip chip-warn">BASELINE</span>}</td>
                  </tr>
                )))}
              </tbody>
            </table>
            <div className="fc-note">Trade odds shown for buy 1 ATR / 1R; promotion needs all four 1-ATR templates to pass.</div>
          </>
        )}
        <div className="lab-sec">News layer <span className="t-dim">Phase E · release-aware range model vs what the lab shows{nl?.run_id ? ` · ${nl.run_id}` : ''}</span></div>
        {!nl?.tfs ? (
          <div className="lab-none">Not run for {symbol ?? 'this symbol'} yet - python tools/forecast_news.py --symbol {symbol ?? 'XAUUSD.a'}</div>
        ) : (
          <>
            <table className="fc-table mono">
              <thead><tr><th>release</th>{Object.keys(nl.tfs).map((tf) => <th key={tf}>{tf}</th>)}<th>closed up (15m)</th></tr></thead>
              <tbody>
                <tr><td className="t-dim">gate</td>{Object.entries(nl.tfs as Record<string, any>).map(([tf, r]) => (
                  <td key={tf}>{r.use ? <span className="chip chip-up">PROMOTED</span> : <span className="chip chip-warn" title="The release-aware model did not beat what the lab shows in every year - the cone is unchanged">kept</span>}</td>
                ))}<td /></tr>
                {Object.keys((Object.values(nl.tfs)[0] as any)?.reactions ?? {}).filter((k) => !k.startsWith('_')).map((kind) => (
                  <tr key={kind}><td>{kind}</td>
                    {Object.entries(nl.tfs as Record<string, any>).map(([tf, r]) => {
                      const x = r.reactions?.[kind]
                      return <td key={tf} title={x ? `${x.n} releases · by year ${Object.entries(x.x_years ?? {}).map(([y, v]) => `${y} ×${(v as number).toFixed(2)}`).join(', ')}` : ''}>
                        {x?.x_median != null ? `×${x.x_median.toFixed(2)}` : '—'}</td>
                    })}
                    <td className="t-dim">{pc(nl.tfs['15m']?.reactions?.[kind]?.p_up, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="fc-note">How much wider the horizon ran after each release than the same hour without one (median, each against the year before it). The cone keeps Phase B's release flag - spelling the releases out did not beat it in every year. Direction shares are measured, not tested forecasts.</div>
          </>
        )}
        <div className="lab-sec">Filter test <span className="t-dim">Phase D · through the real executor{ft?.run_id ? ` · ${ft.run_id}` : ''}</span></div>
        {!ft?.results ? (
          <div className="lab-none">{symbol && symbol !== 'XAUUSD.a' ? `The filter test was run on XAUUSD.a only - none for ${symbol}`
            : 'Not run yet - python tools/forecast_filtertest.py'}</div>
        ) : (
          <>
            <table className="fc-table mono">
              <thead><tr><th>tf</th><th>filter</th>{Object.keys(Object.values(ft.results)[0] as any).sort().map((y) => <th key={y}>{y} R/trade</th>)}<th>verdict</th></tr></thead>
              <tbody>
                {Object.entries(ft.results as Record<string, any>).flatMap(([tf, years]) => {
                  const ys = Object.keys(years).sort()
                  const ffs = Object.keys(years[ys[0]].filters)
                  return [
                    <tr key={tf + 'base'}><td>{tf}</td><td className="t-dim">live strategy</td>
                      {ys.map((y) => <td key={y}>{fmt(years[y].baseline.exp_r, 3)} <span className="t-dim">({years[y].baseline.n})</span></td>)}<td /></tr>,
                    ...ffs.map((ff) => (
                      <tr key={tf + ff}><td /><td>{ff}</td>
                        {ys.map((y) => {
                          const s = years[y].filters[ff]
                          return <td key={y} className={s.d_exp_r > 0 ? 't-up' : 't-down'}
                            title={`${s.n} trades · total ${s.d_sum_r >= 0 ? '+' : ''}${fmt(s.d_sum_r, 1)}R · net ${s.d_net >= 0 ? '+' : ''}${fmt(s.d_net, 2)} · removed ${s.removed_n} at ${fmt(s.removed_r, 3)}R`}>
                            {s.d_exp_r >= 0 ? '+' : ''}{fmt(s.d_exp_r, 3)}</td>
                        })}
                        <td>{ft.verdicts?.[tf]?.[ff] === 'PASS' ? <span className="chip chip-up">PASS</span> : <span className="chip chip-warn">fail</span>}</td>
                      </tr>
                    )),
                  ]
                })}
              </tbody>
            </table>
            <div className="fc-note">Change in R per trade vs the live strategy (hover for totals). PASS needs better R/trade, total R and net P&amp;L in every year.</div>
          </>
        )}
        {sum?.reference && (
          <div className="fc-note">Scorecard check - regime persistence, a known effect, scores {Object.entries(sum.reference)
            .map(([tf, r]) => `${tf} ${spc(r[years[years.length - 1]]?.[0], 0)}`).join(' · ')} in {years[years.length - 1]}: the scorecard can see skill when it exists.</div>
        )}
      </div>
      <div className="fc-dock-col">
        <div className="lab-sec" style={{ marginTop: 0 }}>This session's forecasts <span className="t-dim">settled as the replay passes each horizon</span></div>
        {!s || !s.n ? <div className="lab-none">None settled yet - each forecast settles once its {p.forecast?.horizon ?? 'H'} bars have played.</div> : (
          <>
            <div className="lab-kpis">
              <div className="lab-kpi"><div className="lab-kpi-k">settled</div><div className="lab-kpi-v mono">{s.n}</div></div>
              <div className="lab-kpi"><div className="lab-kpi-k">P20-80 held · up / down</div><div className="lab-kpi-v mono">{pc(s.cov_up, 0)} / {pc(s.cov_dn, 0)}</div></div>
              <div className="lab-kpi"><div className="lab-kpi-k">baseline held</div><div className="lab-kpi-v mono t-dim">{pc(s.b_cov_up, 0)} / {pc(s.b_cov_dn, 0)}</div></div>
              <div className="lab-kpi"><div className="lab-kpi-k">skill vs baseline</div><div className={`lab-kpi-v mono ${(s.skill ?? 0) >= 0 ? 't-up' : 't-down'}`}>{spc(s.skill)}</div></div>
            </div>
            <div className="fc-note">A session is a small sample - read the batch scorecard for the evidence; this shows the same forecast working (or not) on the bars you are replaying.</div>
            <div className="fc-log">
              <table className="fc-table mono">
                <thead><tr><th>bar</th><th>up got</th><th>P20-P80</th><th>down got</th><th>P20-P80</th></tr></thead>
                <tbody>
                  {(log?.rows ?? []).slice().reverse().slice(0, 60).map((r) => (
                    <tr key={r.i} className="fc-click" onClick={() => p.onSeekBar(r.i)} title="Go to this bar">
                      <td className="t-dim">{when(r.t)}</td>
                      <td className={r.in_up ? '' : 't-warn'}>+{atr(r.up)}</td><td className="t-dim">{atr(r.q_up[0])}-{atr(r.q_up[2])}</td>
                      <td className={r.in_dn ? '' : 't-warn'}>-{atr(r.dn)}</td><td className="t-dim">{atr(r.q_dn[0])}-{atr(r.q_dn[2])}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

/** Skill by step k as a row of small bars (green above zero). */
function Spark({ values }: { values: (number | null)[] }) {
  const vs = values.map((v) => (v == null || !Number.isFinite(v) ? 0 : v))
  const m = Math.max(0.01, ...vs.map((v) => Math.abs(v)))
  return (
    <span className="fc-spark" title={vs.map((v, i) => `k${i + 1} ${spc(v)}`).join('  ')}>
      {vs.map((v, i) => (
        <i key={i} className={v >= 0 ? 'up' : 'down'} style={{ height: `${Math.max(2, (Math.abs(v) / m) * 14)}px` }} />
      ))}
    </span>
  )
}
