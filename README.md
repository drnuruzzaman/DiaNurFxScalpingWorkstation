# DiaNurFx — Gold Multi-Timeframe Scalping Workstation

A local, web-based analysis and signal-generation terminal for XAUUSD, built
around a two-stage pipeline: **detect opportunities**, then **qualify trades**.
An analyst layer explains every signal, argues against it, prices the risk, and
quotes what the same playbook has actually done in backtest.

Everything runs on `127.0.0.1`. Nothing is exposed to the network, and the
component that talks to MetaTrader holds no credentials of its own.

---

## Quick start

Three processes. Run each in its own terminal.

**1. The MT5 bridge** (read-only unless you deliberately arm it)

```bash
python bridge/mt5_bridge.py
```

**2. The analysis service**

```bash
python -m server.main
```

**3. The workstation UI**

```bash
cd web && npm install && npm run dev
```

Then open <http://127.0.0.1:5180>.

To serve the UI from the Python process instead of Vite, build it once with
`cd web && npm run build` — `python -m server.main` picks up `web/dist` automatically
and serves the whole thing on port 8770.

---

## What it does

### Analysis (every bar, ~22 ms)

| Layer | What it produces |
|---|---|
| **Structure** | ATR-filtered swing points, HH/HL/LH/LL labels, BOS and CHoCH, trend state with an explicit invalidation price |
| **Levels** | Support / resistance / polarity-flip bands, scored 0–100 on touches, recency, reaction size, round numbers and volume nodes |
| **Liquidity** | Equal highs/lows, unswept swings, each with a "pull" score — where resting stops are |
| **Geometry** | Validated trendlines (2 anchors + a confirming touch, no closes through), parallel channels measured by containment, a least-squares regression channel |
| **Patterns** | Head & shoulders (and inverse), double/triple tops and bottoms, ascending/descending/symmetrical triangles, rising/falling wedges, flags, pennants, rectangles |
| **Events** | Liquidity sweeps, rejection wicks, breakouts, retests, false breaks, and a composite reversal read |
| **Regime** | TREND / RANGE / TRANSITION / SQUEEZE with confidence and evidence, plus a volatility regime in percentile terms |
| **Momentum** | RSI, MACD, ROC, candle pressure, a volume-lean flow proxy, and regular/hidden divergence |
| **MTF** | Weighted trend agreement across the timeframe ladder |

### Signal generation — seven playbooks

`mtf_pullback` · `sweep_reversal` · `breakout_retest` · `false_break_fade` ·
`pattern_break` · `range_fade` · `flag_continuation`

Each produces a **fully-formed order**: side, entry, stop, TP1, TP2, entry type,
a calibrated confidence, and the evidence that built it — plus the
counter-evidence, recorded honestly.

The regime decides which playbooks are even allowed to fire. A buy and a sell on
the same bar is treated as one unresolved market, not two opportunities: the
stronger side survives and the weaker becomes explicit counter-evidence.

### Chart controls

| Action | Effect |
|---|---|
| Drag left/right | Pan through time. Dragging back past the oldest bar held **loads more history** automatically, a screenful before you run out |
| Drag up/down | Pan the price scale. Takes manual control; the axis shows **MANUAL** |
| Drag the price axis | Stretch or compress the candles vertically |
| Wheel over the chart | Zoom the time axis |
| Wheel over the price axis | Zoom the price scale |
| Double-click the price axis | Hand scaling back to automatic (or use ⇕) |

There is no grid and no session shading — the annotation layer is the signal,
and a grid behind it is noise. A fixed gap of empty bars sits between the last
candle and the axis, so the live bar always has somewhere to move.

History is merged, not replaced. The live socket pushes a fixed tail every
second; replacing state with it would discard anything you had scrolled back
to, and the chart would snap forward under your hand.

### The signal board — every timeframe, ordered by timeframe

The bottom dock's **SIGNAL BOARD** is not the chart's timeframe; it is all eight
watched timeframes (`1m 3m 5m 15m 30m 1h 4h 1d`) at once, sorted fine-to-coarse,
with a hairline between groups. Each row carries side, status, confidence,
regime, the full order, net R after costs, lot size, and the *reason* — including
why it was rejected. Click any row to put that timeframe on the chart.

The **TF MATRIX** tab is the same sweep as a grid: regime, confidence, trend,
MTF score, momentum and ATR per timeframe.

This exists because taking a scalp against an opposing higher-timeframe setup is
one of the most common ways these systems lose money, and that conflict is
invisible on a board showing only the timeframe you happen to be looking at. A
live example from a single sweep: sells on 1m–1h, and a 72-confidence **buy** on
4h.

Scanning is staggered, not swept. Each timeframe refreshes on a cadence scaled to
its own bar (a fifth of the bar length, clamped to 6–150s), in a background task
— so a 4h row is not recomputed every second, and the websocket tick never waits
behind a scan. A full sweep is ~1.6s; the cached read the socket actually uses is
~6ms.

### Trade qualification — eleven gates

Spread · volatility · session · MTF agreement · sizing · net reward after costs ·
room to target · regime fit · exposure · daily limits · news · broker minimums.

Each returns **PASS / WARN / BLOCK** with a reason, so the UI can show the full
ledger. Outcomes are `qualified`, `watch` (no blocks but short of the confidence
floor) or `rejected`.

> Costs are charged properly. This is a Raw Spread Islamic account: no swap,
> but commission per side and the bar's own recorded spread. `min_rr` is
> measured **after** costs.

### The analyst

Four modes over the same computed evidence:

- **ANALYSIS** — why this signal exists, ranked by evidence weight
- **CHALLENGE** — the case against, graded severe / moderate / minor
- **RISK** — size, cash at risk, what five losses in a row costs, management plan
- **BACKTEST** — what this playbook has actually done, or an honest "no run yet"

Plus **ASK** for free text. Answers come from the engine's computed facts by
default. Set `ANTHROPIC_API_KEY` to route phrasing through a language model —
it only ever sees facts the engine produced, and never computes a number.

---

## Backtesting

The BACKTEST tab has its own chart and replays the **identical**
`analyse → generate → qualify` path the live tab uses, on an expanding window.

Guards against the usual ways backtests lie:

- **No lookahead.** The window ends at the bar being decided; fills resolve from
  the next bar. Swing-confirmation lag is modelled inside the detector.
- **Ambiguous bars are counted, not hidden.** When a stop and a target sit inside
  one bar, it re-resolves on M1 data where available and otherwise assumes
  stop-first. The report tells you what fraction were ambiguous.
- **Partial exits are modelled.** Half off at TP1, stop to break-even, remainder
  to TP2 — the plan the signal actually specifies.
- **Costs are charged.** Commission per side, real spread, slippage.

Click any trade row to replay that exact bar: the chart re-runs analysis as of
that moment, so you see the annotations the system would actually have drawn.

---

## Alerts

A signal that qualifies can be pushed to Telegram. Config lives in
`configs/alerts.json` and is written by the **Settings** modal (gear icon, top
right).

The centrepiece is an **instrument x timeframe grid**. Alerting is a decision
per cell, not per instrument, because a setup worth a message on 1h is usually
noise on 1m. Click a symbol or a timeframe header to toggle a whole line.

Three things stop it becoming a spam machine:

- **Dedupe** — a signal id fires once. Ids are stable per playbook+side+bar, so
  the same setup re-detected next tick is not a new alert.
- **Cooldown** — a per-cell floor between messages, regardless of signal id.
- **Quiet hours** — an optional UTC window where nothing is sent.

Every message includes the **counter-argument**. A signal alert that only argues
one side trains you to stop thinking, and arguing both is the point of this
system.

```bash
export TELEGRAM_BOT_TOKEN=...   # from @BotFather
```

> The bot token is never written to `configs/alerts.json`. A chat id is an
> address; a token is a credential that can post as you, and the Settings UI
> rewrites that file. Chat ids live in the config, the token lives in the
> environment.

`logs/alerts.jsonl` records every send and every *notable* suppression — a
missing token, no destination, cooldown, quiet hours. Routine filters (not
watched, wrong status, below confidence) are not logged, or the useful lines
would drown.

Alerting ships **off**, with no destinations and nothing watched.

### Before enabling a public channel

These signals are not validated out of sample — profit factor was 1.09 on 2026
and 1.00 on 2025. Broadcasting them to followers is publishing trading calls to
other people, which is a different act from alerting yourself.

---

## Order execution (EA path)

**Disabled by default.** The bridge has no reachable order path unless you start
it with an explicit flag:

```bash
python bridge/mt5_bridge.py --enable-trading
```

Five independent conditions must hold before an order reaches MT5:

1. the bridge was started with `--enable-trading`
2. the execution module is armed (only that flag does it)
3. the request carries `confirm=true`
4. the account passes the demo guard (`--allow-live` to lift it)
5. the order passes sanity checks — known symbol, sane lots, a stop that exists
   and sits on the correct side

There is no "force" parameter and no way to disable a guard over HTTP. Every
attempt, accepted or refused, is written to `logs/` with its reason.

A stop loss is **mandatory**. The system will not open an unprotected position.

---

## Security

- Both services bind `127.0.0.1` only; `server/main.py` refuses any other host.
- `bridge/mt5_config.json` is gitignored. Prefer environment variables
  (`.env.example`), or best of all supply nothing and leave the terminal logged
  in — the bridge attaches to that session without handling a password.
- The analysis service never sees your credentials.

> **If you have ever committed or shared `mt5_config.json`, rotate those
> passwords in the MT5 terminal.**

---

## Layout

```
bridge/      MT5 ↔ localhost JSON bridge (read-only) + execution.py (disarmed)
sim/         broker server-clock resolution
server/
  config.py      every tunable knob
  datafeed.py    disk history + bridge live feed, unified
  llm.py         optional language layer over engine facts
  main.py        FastAPI service and websocket
  engine/
    indicators.py  vectorised primitives (no lookahead)
    structure.py   swings, BOS/CHoCH, trend, retracement
    levels.py      scored S/R and liquidity pools
    trendlines.py  trendlines and channels
    patterns.py    classical chart patterns
    events.py      sweeps, rejections, breaks, retests
    regime.py      regime, volatility, MTF, fear/greed
    analysis.py    the orchestrator
    signals.py     the seven playbooks
    qualify.py     the eleven gates
    narrator.py    the deterministic analyst
    backtest.py    event-driven replay
web/         Vite + React + TypeScript, custom canvas chart engine
data/        XAUUSD.a/<tf>/<year>.csv.gz
runs/        saved backtest results
logs/        audit log
```

---

## Configuration

`server/config.py` holds the defaults; the `/api/settings` endpoint changes them
at runtime. Two choices worth knowing about:

**Volatility bounds are relative, never absolute points.** Gold ran from 1945
(2023) to 4561 (2026) and its median M5 ATR went from 102 points to 548. A fixed
point band that rejected 1.6% of 2023 rejected 16% of 2026 — it was measuring
the gold price, not the market. The floor is now economic (ATR must be worth
several round trips) and the ceiling is a percentile of the instrument's own ATR.

**Stops have a noise floor** (`min_stop_atr`). A stop closer than ~1 ATR on 5m
gold sits inside normal fluctuation and gets hit whether the idea was right or
wrong. Widening costs nothing in risk terms — position size is derived from the
stop distance.

---

## Honest notes on performance

The backtester is built to be unflattering, and it is. Early runs on Q1 2026 M5
produced a profit factor near 1.0 — roughly break-even after costs. Treat any
figure it prints as a starting point for investigation, not a result.

Two things that measurement exposed:

**Stops inside the noise band.** `breakout_retest` was placing stops ~0.35 ATR
from entry and losing 22 of 35 trades in a median of two bars, with a median
adverse excursion of −1.31R. That is not a bad idea, it is a stop sampling
noise. Adding `min_stop_atr = 1.0` and re-running the identical window:

| | Before | After |
|---|---|---|
| Profit factor | 1.04 | 1.08 |
| Expectancy | +0.031R | +0.046R |
| Return | +6.21% | +10.96% |
| Max drawdown | 15.02% | 9.76% |
| `breakout_retest` | −$33 (45.3% win) | +$633 (48.6% win) |

The playbook the diagnosis pointed at improved most, and drawdown fell by a
third while return rose — which is what a risk fix looks like, as opposed to a
curve fit.

**Expired trades were assumed to be dead weight.** They were the single most
profitable bucket (+20.4R across 91 trades, median hold 19 bars). The
time-based exit is doing useful work; it was never the problem. Worth stating
because the obvious "fix" — extending the expiry — would have made things
worse.

### The pattern_break exemption: tested, and the result was a warning

`pattern_break` is the only playbook allowed to fire in every regime. That
exemption was tested three ways (`tools/exp_pattern_break.py`) over Jan–Aug
2026, 1,866 trades:

| arm | PF | expectancy | return | maxDD |
|---|---|---|---|---|
| A exempt everywhere | 1.09 | +0.045R | +33.8% | 17.2% |
| B dropped entirely | 1.12 | +0.060R | +52.3% | 17.6% |
| C gated to trend+range | **1.15** | **+0.069R** | **+65.4%** | **15.0%** |

Arm C looked decisive, and its regimes were chosen from 2026's own cross-tab —
which is in-sample selection. Re-running the **same fixed rule** on 2025
(`tools/exp_oos.py`) inverted it:

| arm | PF | expectancy | return |
|---|---|---|---|
| A exempt everywhere | **1.00** | **+0.016R** | −1.4% |
| B dropped entirely | 0.98 | +0.009R | −8.1% |
| C gated to trend+range | 0.99 | +0.013R | −5.0% |

Three of the four regime buckets **flipped sign** between years:

| regime | 2026 | 2025 |
|---|---|---|
| trend | +0.141R | +0.069R (consistent) |
| range | +0.085R | −0.085R (flipped) |
| transition | −0.002R | +0.077R (flipped) |
| squeeze | −0.119R | +0.104R (flipped) |

**So the change was not adopted.** A +65% in-sample result that turns into −5%
out-of-sample is noise wearing a convincing costume, and the only reason that
is visible is that the rule was re-tested on data it was not derived from.

The one finding that survives both years is that `pattern_break` is positive in
`trend` — and at +0.069R on 199 trades in 2025, even that is thin.

The bigger and more honest headline: **the system's 2026 performance does not
replicate in 2025** (PF 1.09 → 1.00, +33.8% → −1.4%). Before trusting any
playbook, run walk-forward across several years. The tooling to do that is in
`tools/`; the conclusion it currently supports is "not yet proven".

Sample sizes are small and windows overlap. Nothing here is validated
out-of-sample. Run your own windows before trusting any playbook.

---

## Verify the install

```bash
python tools/selftest.py
```

27 checks, ordered by how badly they would hurt if wrong: causality of every
indicator (the property the backtest rests on), resample fidelity against the
broker's own bars, the full analyse → generate → qualify pipeline, order-level
sanity, the execution guards, and JSON safety. Exit code is 0 only if all pass.

---

## Disclaimer

This is analysis software. It computes and explains; it does not advise. Every
signal is a hypothesis with a stated invalidation, and the CHALLENGE mode exists
because the case against a trade is usually the more useful half. You place the
trades and you own the outcome.
