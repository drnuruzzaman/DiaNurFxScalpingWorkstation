# Port the chart's range rules into the signal engine

2026-11-08 · status: **plan, nothing built** · branch `trendlines_channel_improvement`

## Why

The chart now finds ranges with the rules agreed on 2026-11-08
(`web/src/chart/ranges.ts`): swing-clustered edges, height 1.2–6× ATR, a
slope of at most 0.15× ATR per 20 bars, at least 15 bars, at least 2 swing
highs and 2 swing lows at the edges, a breakout only after two closes in a row
more than 0.3× ATR outside, and major/minor/developing tiers.

The engine reads "range" in three other ways, none of which follow those rules:

| Where | What it uses today | Who reads it |
|---|---|---|
| `regime.py:97` `range_high` / `range_low` | highest high / lowest low over `regime_lookback` | `false_break_fade` target (`signals.py:542`), `range_fade` edges (`signals.py:666`) |
| `regime.py:128` range_score +14 | engine horizontal channel, ≥ 85% containment | regime state `range` → playbook evidence (`signals.py:461`, `:522`, `:662`) |
| `patterns.py:542` `rectangles` | last 6 swings, mean of highs/lows | pattern list, `pattern_break` |
| `signals.py:697` | horizontal channel | `range_fade` evidence (+10) |

So the chart and the engine can disagree about where a range is.
**`false_break_fade` is the only gold Tier 1 edge, and its target comes from
the lookback high/low.** Any port touches the one edge that is live.

## Constraints

- The forward paper books are frozen on fingerprint `cd53889dd0b5f69d`, which
  covers `regime.py`, `signals.py`, `patterns.py`, `trendlines.py` and
  `analysis.py`. Editing any of them invalidates the books. Adding a NEW,
  unreferenced file does not.
- The forward books are not on this machine.
- Forecast tables need a rebuild after any engine change.
- Nothing goes live without a lab comparison over 2018–2026 and forward
  evidence afterwards.

## Steps

### 1. Python port, no behaviour change (safe now)
- New file `server/engine/ranges.py`, a straight port of `ranges.ts`. It is a
  pure function of the bars and is referenced by nothing yet, so the
  fingerprint is unchanged.
- Point in time: swings need 3 bars after them, so call it on the bars up to
  the decision bar only (it is causal on a prefix). Limit it to the last
  ~600 bars per call to keep the backtests fast.
- Parity test `tools/test_ranges.py`: a golden JSON made by running the TS
  version (esbuild) on fixed gold 1h/4h and EURUSD 1h bars. The Python version
  must return the same boxes.

### 2. Shadow measurement (no signal change)
- A research tool replays history bar by bar. At each bar it records the
  active range from `ranges.py` next to the regime's `range_high`/`range_low`
  and horizontal channel.
- Report: how often they disagree, and, on past `false_break_fade` and
  `range_fade` signals, what the target or edge would have been under the
  new range, and the R that would have produced.
- This decides whether step 3 is worth doing at all.

### 3. Integration points, one at a time, each behind a settings flag (default off)
Each is a separate switch under `settings.range_engine`, tested on its own:

- **a. `false_break_fade` target:** the active range's far edge instead of
  the lookback high/low. Highest value, highest risk: it is the live edge.
- **b. `range_fade`:** fire only inside an active, confirmed (non-developing)
  range, using its edges. Replaces the lookback box and the horizontal-channel
  evidence.
- **c. `breakout_retest`:** a confirmed breakout (two closes) of a major range
  becomes positive evidence, instead of the blanket "range regime penalises
  breakouts".
- **d. Regime `range_score`:** replace the horizontal-channel +14 with "price
  is inside an active range".
- **e. `patterns.rectangles`:** replace it with the range detector, or drop it.

Wiring the flags changes fingerprinted files. Do it in one commit, with every
flag off, and confirm the lab output is byte-identical to the baseline before
anything is switched on.

### 4. Lab comparison
- `tools/market_batch.py`, one `--tag` per variant against the baseline, gold
  and EURUSD, 2018–2026, costs included (lab `r_multiple` excludes commission:
  add it).
- Promote a variant only if net R improves overall AND it does not lose in
  more years than the baseline does.

### 5. Promotion
- Turn on only the flags that passed.
- Re-freeze the forward books (new fingerprint) and rebuild the forecast tables.
- Collect forward evidence before live accounts use it.

## Decisions needed

1. Go ahead with steps 1–2 (no behaviour change, books stay valid)?
2. Which integration point first? Recommended: **(a)**, because it is the live
   edge and step 2 will show directly whether the lookback box is costing it.
3. Where should the forward-book re-freeze happen, since the books are not on
   this machine?
4. A note from an earlier session records a `server/engine/lines.py` that
   already feeds ranges into regime, playbooks and the room gate, with a
   recorded 1h month going from +154 to −121. It is not on this branch. If
   that work lives in another checkout, this port should build on it, or
   replace it, rather than duplicate it.
