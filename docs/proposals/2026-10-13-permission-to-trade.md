# Permission to Trade: a gold robot that defaults to NO TRADE and licenses strategies on forward evidence
> Proposal, 2026-10-13. Written from the repo, the research reports and the brief *Trading Robot Considerations*. Drafted by three independent designs, merged, then reviewed by a quant skeptic, a constraints/coverage check and an engineering-feasibility review. Nothing in it has been built or changed.


## 1. The idea in one paragraph

Treat every setup as an **application to trade**, not an order. Permission belongs to a **licence** held by a frozen strategy (playbook × timeframe, one engine version, one exit), and a licence is earned on **forward evidence**: trades on data the rule has never seen, measured as excess R over random entries on the same side in the same quarter, so gold's bull drift is not counted as skill. Uncertainty is handled once, at licence level, by one pre-registered sequential test. Per trade, the engine checks only that today's live cost in R leaves the licence's haircut edge positive (EV_net > 0) and that the market is inside a hard safety envelope; anything else is NO TRADE, the default. It can work because it closes the two measured leaks (cost above edge on 3m/5m; trading unproven strategies) and moves experiments to where being wrong is cheap: at 1h a zero-edge strategy at minimum size loses about its cost, ~0.013R per trade.

**Caveat:** it cannot create edge, and today nothing is licensed. Proving a 1h edge on forward data takes years (§6.3), so for a long time the robot runs in shadow or at pilot size, with P&L near zero.

## 2. Diagnosis: why the current robot does not make money

**Headline (lab 5m, 2018-26):** 9,648 trades, gross +0.012R, cost 0.060R, net −0.047R.
- 2018-23 lost every year: −0.076R per trade, about −80R a year.
- 2024-26 was +0.008R, 90% CI [−0.023, +0.040].
- Entries reach +0.5R 65% of the time vs 49% for random entries, worth only ~+0.02R gross.

That book used the 2026-09 lab settings (breakout_retest off, false_break_fade on, lock 0.5). Those are not today's live settings.

**Gross edge vs cost by timeframe.** Source: lab journals `win_all7-*`, ~56k trades; dev = 2018-23, confirm = 2024-26.

| TF | Gross dev [90% CI] | Gross confirm | Cost dev / confirm |
|---|---|---|---|
| 3m | +0.017 [+0.007,+0.028] | +0.023 | 0.117 / 0.060 |
| 5m | +0.024 [+0.007,+0.041] | +0.057 | 0.100 / 0.049 |
| 15m | +0.019 [−0.007,+0.045] | +0.020 | 0.062 / 0.028 |
| 30m | +0.007 | +0.042 | 0.040 / 0.018 |
| 1h | +0.041 [−0.014,+0.094] | +0.164 | 0.029 / 0.013 |
| 4h | +0.021 [−0.095,+0.138] | +0.287 | 0.015 / 0.008 |

**Provenance.** The rows do not share one configuration:
- the lab-only `no_transition` forecast veto was on for 5m, 15m, 1h and 4h only;
- all 7 playbooks were enabled, which changes `resolve()` merging;
- the trail lock was 0.5;
- the 5m rows mix three settings hashes.

So comparisons across TFs are approximate, and no row describes the live rule.

**What the table shows:**
1. **On 3m and 5m, cost was 4-7× the gross edge in 2018-23.** Even in confirm, 5m cost (0.049) nearly equals 5m gross (+0.057).
2. **The 2024-26 higher-TF gains are mostly long drift.** Buys vs sells: 1h +0.212 vs +0.083; 2h +0.171 vs −0.092; 4h +0.365 vs +0.052. In 2018-23, net on 1h and 4h was about zero (+0.012 / +0.006).
3. **Cost is a reason to stay out, not an edge.** The live gate admits trades costing up to 0.167R.

**The live process has no economics in it:**
- No p, W, L or EV; "confidence" is an uncalibrated evidence sum.
- A fixed 0.01 lot makes a 4h loss (median $93) about 9× a 5m loss ($10).
- Loose limits (5% daily, 100 trades a day, 10 positions), no drawdown limit, a fail-open daily gate, a forward-only news gate, unchecked synthetic quotes, and refused sends retried every 30 s until expiry.

**Execution is not the problem so far.** Bridge text logs since 2026-09-26 record requested and filled prices for 34 market orders: median slippage 0 points, mean −1.3 (favourable), worst +7. The wider "fill vs planned entry" spread (p5 −104.5, p95 +160) is price drift during the wait to requalify (5m median 62 s), not slippage.

**The live record is noise.** There are 185 closes, 25% of them manual. Non-manual trades average +0.082R (n=138), CI [−0.095, +0.255], SD 1.25R.

**What can and cannot be forecast:**
- **Range cone** (promoted 5m/15m) and **regime state** (promoted on every TF, but mostly persistence: 9.1-9.4% of 5m's 12.5-13% skill; regime_agree CIs include 0) are edge-filter candidates only.
- **News** changes move size (FOMC ×5.6, NFP ×2.3, CPI ×1.9), not direction: a tail risk.
- **Direction and barrier odds** are not promoted.

A random 1:1 ATR bracket at today's 5m cost is worth −0.007 to −0.028R.

## 3. Architecture

```
closed bar ─► L1 facts (quote, spread series, ATR, UTC session, news state)
                 │
L3 strategy (licensed config, stand-alone hits) ─► FINAL candidate
                 │
Executor._final ─► requalify() ─► quote ─► permission(cand, quote, book)  [injected: live / lab impl]
                       ┌───────────────────────────────────────────┐
                       │ 1 safety envelope + kill switches [L5]    │
                       │ 2 licence state PILOT/FULL?       [L6]    │
                       │ 3 EV_net = Ĝ − C_R − m > 0        [L4]    │
                       │ 4 open book: no opposite side, caps [L5]  │
                       │ 5 size from stop                  [L5]    │
                       └──────────┬─────────────────┬──────────────┘
                           any NO │                 │ all YES
              NO TRADE + record   │                 │ BUY/SELL + record ─► ledger intent ─► bridge vetoes
SENT pending orders: steps 1-2 re-checked every 2 s, cancel on NO
                                  ▼
 shadow book (every FINAL, frozen levels, live quote) ──┐
 offline frozen forward replay (disk bars, SimBroker) ──┼─► sequential test + monitors ─► licence states
 journal v2 at close ───────────────────────────────────┘
 AI supervisor: reads records only; no path to executor or bridge
```

**Hook.** `permission()` is injected into `Executor` the same way `requalify` already is:
- default: a no-op;
- live: an implementation in `main.py`;
- lab: an implementation in `LabExecutor`, so replays reproduce live decisions without touching live state files.

It runs after `requalify()` (executor.py L159-178), for auto and manual Place. An operator may override an L4 or licence NO (tagged, excluded from statistics), never L5. New telemetry never raises and stays outside the intent→trade→mark→move section (L197-221).

**Portfolio rules** are checked per record against the ledger's open book. There is no cross-candidate arbitration, which a per-record loop under the bridge's 2-second gap cannot do safely.

| Layer | Exists (keep) | New |
|---|---|---|
| L1 | Bid/ask in `State._context_uncached`; bar spread; ATR pct; calendar; commission | Journal v2: executor quote at send, spread at fill/exit, latency, requested price (bridge log). Persist spreads the API already reads (no new bridge polling). Calendar `first_seen_ms` and a title→kind classifier (`history.json` ends 2026-09-24) |
| L2 | `engine/regime.py`; promoted tables (lab) | Nothing goes live before a broker-time/UTC test. The cone is keyed by broker hour, and `forecast/shadow.py` already feeds UTC times into broker-time tables |
| L3 | 7 playbooks; FINAL freeze; v4 forward test | Licensed strategies run stand-alone (`generate(only=[playbook])`), so `resolve()` merging cannot change them |
| L4 | `_net_rr`, `size_position`, `lift_table` | `server/decision.py`: Ĝ, C_R, p/W/L. The variance estimator is new code |
| L5 | Gates, ledger, bridge guards | `server/governor.py`; reconcile tool |
| L6 | Journal, shadow logs, reports, `forward_mtf_ls.py` | Shadow book, forward replays, `configs/licences.json`, sequential test, attribution, `health` alerts |
| Supervisor | `lab/challenge.py`, narrator, `llm.py` | Decision records; stricter `untraced()`; tracing on `/api/ask` |

## 4. The decision engine (L4)

### 4.1 Unit, estimate, rule

**Unit.** A licence covers one strategy × TF, with both sides pooled. Per-side samples are too small: 1h pattern_break had only 19 sells in confirm.

**Edge = drift-neutral excess R.** This is realised R minus the mean of K=20 random twins matched on:
- side, TF and **calendar quarter**;
- UTC session block;
- stop in ATR;
- entry mechanism and exit.

A quarter removes gold's secular drift without erasing skill at picking the 1-2-week trend (a ±5-day window would absorb it). A second baseline enters on the side of the trailing 20-day return, separating trend beta from skill. Gates and monitors use excess R; only the money loss budget uses raw R.

**Rule.** BUY or SELL only if all of these hold. Otherwise NO TRADE, with reason codes.
1. The licence is in PILOT or FULL (§6.3).
2. The safety envelope passes (§4.2).
3. **EV_net = Ĝ − C_R − m > 0**, where:
   - Ĝ is the pre-registered gross excess: half the screen estimate until the forward test passes, then the forward mean;
   - C_R = (live spread + 6-point commission + slippage legs × 3 points) ÷ stop in points, frozen for each forward test;
   - m is a margin per TF for unmodelled costs, set from P0 telemetry.
4. There is no open or pending robot position on the opposite side, and the §6.1 caps pass.

The record also shows p = P(R>0), W and L, and pW − (1−p)L − C. At 1h (median cost 0.013R), rule 3 seldom binds.

**No per-candidate lower bound.** In review simulations, an LCB ≥ +0.03R over 7-level cells passed no known candidate (1h pattern_break: 123 cells, median 2 trades). Conditional cells become **edge-filter candidates** instead: veto-only arms on the ladder (filter test → shadow → live), each required to pass in 2024, 2025 and 2026.

### 4.2 Safety envelope vs edge filters

**Safety rules (L5).** Each is calibrated in OBSERVE mode for at least 2 weeks before it is enforced.
- **Spread:** abstain above 2.5× the trailing 60-minute median, with a 45-point backstop. Until 60 minutes of data exist, the hour-of-week median from history stands in.
- **Volatility:** ATR percentile ≥ 97.
- **Integrity:** feed, clock and tables must check out.
- **Calendar:** no trading in the first 60 minutes after the weekly open or while the market is closed.

**News window.** USD NFP, CPI and FOMC, from 15 minutes before to 30 minutes after. It is a tail-risk control and is never credited as edge. It is only logged until V2 shows that it cuts the worst-1% R or slippage.

**Edge filters, which are not in the live rule:**
- cone P50 ÷ stop;
- ATR-percentile cells;
- the 0.05R cost cap;
- regime-uncertainty damping;
- a transition veto (the evidence conflicts: on 30m "transition" scored −0.066 / −0.065, but the 15m no_transition filter removed winners);
- C1.

### 4.3 What it would decide today

| Candidate | Evidence | Decision |
|---|---|---|
| Live 5m book; 3m/5m playbooks | net −0.047; 3m sweep_reversal cost 0.104 vs gross +0.026 | NO TRADE |
| 15m | net −0.043 / −0.008 | NO TRADE |
| 1h pattern_break (H1) | +0.146 net, n=342, every year positive, but with the no_transition veto, all-7 resolve and lock 0.5 | Re-run stand-alone first; then SHADOW |
| MTF-LS v4 (H2) | +0.143, CI [−0.008, +0.297], 4th variant; forward test 0 of 40 trades | Offline forward test only; not a live playbook |
| 4h/2h cells (n=31-66) | Small samples | Watch list |

**Auto would send nothing.** A settings flip turns it off today (§9.1).

## 5. The edge plan

### 5.1 What "scalping" becomes

With a lab SD of about 1.05R, the smallest edge that 2σ/√n can resolve is:

| Scope | Smallest provable edge |
|---|---|
| 3m, pooled | ~0.02R |
| 5m, pooled | ~0.03R |
| One 15m licence, confirm period | ~0.19R |
| 1h pattern_break, all nine years (n=342) | ~0.11R |

So 15m-1h is not where proof is easy. Its advantage is that **being wrong is cheap**:
- a zero-edge 1h licence at 0.25% risk loses about 0.013R × 38 ≈ 0.5R a year, roughly 0.12% of equity;
- a single 5m playbook loses about 10-13R a year, roughly 2.5-3% of equity.

Decisions therefore move to 15m-1h, and **1m/5m become execution tools** (H4). This follows the brief: 1H regime, 15M location, 5M confirmation, 1M timing.

You accept fewer trades, holds of hours (news and overnight exposure), larger dollar stops, and missed limit fills counted as 0R.

### 5.2 Hypothesis registry

Each hypothesis is logged with its configuration before it runs. **One falsification rule applies to all of them:**
- leave-one-year-out excess must be > 0 for every year;
- under R-stress (cost ×2 plus 0.02R) the result must stay ≥ 0;
- a failed USDJPY run is one strike, and two strikes kill.

| ID | Steps | Kill if |
|---|---|---|
| H1 1h pattern_break | (a) Re-run 2018-26 stand-alone with no forecast filter and the exit from §9.2 (~1.5 h of lab time). A second arm with no_transition is for provenance only. (b) Excess vs twins and the trend-side baseline, by side × year. (c) Frozen offline forward replay, on the `forward_mtf_ls.py` pattern | (a) excess ≤ 0, or the uniform rule fails |
| H2 MTF-LS v4 | Unchanged. Refresh the forward test after each data update. Add a random-**limit** baseline (a new script importing `Book` read-only) | Random-limit reaches ≥ 80% of v4's R, or the forward rule fails. Port to live only after 40 forward trades (2-4 wk) |
| H3 C1 | Read-only re-score on the clean 5m/1h journals; prior only, since confirm was already read | Keep-minus-veto ≤ 0 in dev or in any year |
| H4 Retracement limit | After a 15m-1h decision, rest a limit 0.25 ATR better for 2 bars. Paired replay plus a mechanism control. Parameters were set after v4, so the evidence is dev + forward only. Ships as a default-off switch | Effect ≤ 0 in dev or forward |
| H5 News continuation (**your OK**) | ~318 events, so only edges ≥ ~0.2R are detectable. Otherwise, measure risk only | Uniform rule |
| V1/V2 | Ablate the cost cap; test the news window on tail metrics | — |
| A1 **Gate ablation** | `filtertest` arms on 15m-1h, removing one at a time: min_confidence (including the Fib-pocket +18), leg (210 blocks), room (261), mtf, allowed_playbooks, and every new rule | Keep a gate only if removing it hurts in each year |

**Strategy D** (range_fade): too few trades above 15m; 15m confirm +0.227 (n=54) is post hoc, so registered only if you ask. **Breakout regime:** no classifier has one; breakout setups cover it.

**Not proposed:** 3m/5m rules, exits, filter packages, IBS, sweep/CHoCH variants.

### 5.3 Every playbook is an applicant (added 2026-10-13)

The licence system does not favour any one playbook. The 7 playbooks × 7 timeframes give **49 applicants**, and every one of them is evaluated, including the three disabled live: stand-alone generation (`generate(only=[playbook])`) skips the disabled list. The shadow book paper-trades all 49 from day one at zero cost. That is the fastest way to build forward evidence, because every cell accrues it at the same time.

Research time is spent by tier. The tiers come from net R per trade in the `win_all7` lab journals, 2018-26, on the 2026-09 lab settings, so they are approximate. **Correction, 2026-10-14:** the lab's `r_multiple` excludes commission ([session.py:445](../../server/lab/session.py)), so these numbers subtract 6 points ÷ stop. That moves each cell by -0.004R (4h) to -0.035R (3m). The earlier reports that use `profit` (`timeframe_costs`, `playbook_focus`) were already correct.

| Tier | Cells | What happens |
|---|---|---|
| 1. Positive in both periods, enough trades | pattern_break 1h: n=342, dev +0.150 / confirm +0.123, positive all 9 years, bootstrap 90% lower bound +0.050, t=2.58. false_break_fade 30m: n=134, +0.088 / +0.170, positive 6 of 9 years, buys and sells both positive | Stand-alone re-run, then a frozen forward test (H1, H1b) |
| 2. Positive in both periods, small n | breakout_retest 4h (n=66; sells about 0), mtf_pullback 4h (40), flag_continuation 1h (70; dev +0.001), flag_continuation 2h (31), false_break_fade 1h (19), range_fade 30m (27), range_fade 2h (3) | Watch list: shadow only, re-run when n grows |
| 3a. Positive only in 2024-26 (gold rally plus falling cost) | mtf_pullback 5m/30m/1h; sweep_reversal 5m/1h/2h/4h; breakout_retest 5m/30m/1h; false_break_fade 5m/15m/2h/4h; pattern_break 15m/2h/4h; range_fade 3m/15m/1h; flag_continuation 5m/4h | Shadow only. Forward data decides |
| 3b. Positive only in 2018-23 | pattern_break 30m, flag_continuation 30m, range_fade 4h | Shadow only |
| 4. Negative in both periods | mtf_pullback 3m/15m/2h; sweep_reversal 3m/15m/30m; breakout_retest 3m/15m/2h; false_break_fade 3m; pattern_break 3m/5m; range_fade 5m; flag_continuation 3m/15m | Shadow log only, no research time |

**Mapping to the brief's strategy families:**
- A, trend pullback: mtf_pullback and flag_continuation.
- B, breakout: breakout_retest and pattern_break.
- C, sweep and rejection: sweep_reversal and false_break_fade.
- D, range reversion: range_fade.

**Caveat.** 49 cells were scanned, so the best of them would reach t ≈ 2.2-2.5 by chance alone. pattern_break 1h's 2.58 is only just above that, and no cell gets money until forward data confirms it.

## 6. Survival (L5 + L6)

### 6.1 Risk governor (`server/governor.py`, injected, fails closed)

| Control | Rule |
|---|---|
| Sizing | `size_position` from the stop: PILOT 0.25%, FULL ≤ 0.5%, never above 1%. At ~$20k this is **mostly 0.01 lot** on 1h (median stop $28.5 vs $50 allowed). The min-lot block (above cap × 1.2) drops ~8% of 2024+ 1h trades, so validation replays apply it too |
| Loss | 1% daily (daily.py already counts floating P&L); 2.5% weekly (`realised_pnl` week). An unreadable account blocks |
| Drawdown | Robot P&L is tracked by magic 778899 / DNX tag. At 4%, size halves; at 8%, auto goes off and pending orders are cancelled. **Breakers never change licence states** |
| Exposure | 12 trades a day; at most 2 positions, on the same side, ≤ 0.75% total risk |
| Cooldown | 3 stop-outs within 120 min pauses trading for 120 min, in the lab too. The backtest's `cooldown_bars` is signal spacing, not this rule |
| Execution | Measured in R against the cost model. Latency counts for market orders only; pending orders are judged against the order price at trigger. Thresholds are set after ~100 fills per entry type |
| Refusals | 3 within 10 min, or any terminal refusal: auto off, plus an alert |
| Feed | A synthetic or stale quote, missing bars, bid ≥ ask, or a hash mismatch means abstain. Thresholds come from OBSERVE, with the daily break excluded |
| Pending | Re-checked every step and cancelled on NO |
| Account | Refuse if the account type does not match the licence mode. The bridge log shows the armed bridge attached to **LIVE account 51527502 (Pepperstone)** 20 times, most recently 2026-10-12 22:07 and 2026-10-13 08:52, before switching back to demo 23011660. The 285 refusals on 09-21..23 came from that account. DEMO_ONLY held every time |
| Ledger | 3 rows have sat in `filled` since 09-30, 10-01 and 10-06. They are unverified and may be open positions. Verify them (your OK) and add a reconcile tool with a configurable lookback (`_closed` reads 3 days) |

The bridge cannot flatten positions. At 8% the breaker can tighten stops through tighten-only `modify_sl`, as a safety action only.

### 6.2 Health monitors: honest about speed

Review simulations bootstrapped from the 342 H1 trades show how slowly a dead edge gets flagged:

| Monitor | Time to flag a dead edge |
|---|---|
| R-CUSUM | median 133 trades |
| Entry-skill CUSUM | 53-227 trades |
| Posterior | only 12% flagged within 100 trades |

At about 38 trades a year, that is 3-6 years. Protection therefore comes from five things:
1. **Minimum pilot size.**
2. **A loss budget per licence:** −8R raw (2% of equity) means RETIRED.
3. **The futility boundary** of the sequential test.
4. **Cost monitors updated per fill:** realised cost/R over 20 fills above 1.5× the model means REDUCED; above 2× means SHADOW.
5. **A fixed review** every 12 months or 60 trades.

Thresholds are calibrated jointly against a **haircut null** (half the screen excess), for at most 10% false SHADOW calls a year; ARL0/ARL1 are reported in years (P2). Entry-skill rate stays diagnostic (its 65/49 is from 5m). Manual, probe and operator trades are excluded.

### 6.3 Licence state machine

Licences pin an engine version and **decision-relevant keys only** (editing auto, lots or equity never touches them).

| State | Entry rule | Size |
|---|---|---|
| CANDIDATE | Passes the §7 screen: worth forward-testing, not yet permission | 0 |
| SHADOW | Frozen config, run through the offline forward replay and the live shadow book | 0 |
| PILOT | ≥ 40 forward trades, forward excess > 0, no futility, process checks pass, **your click**. A similar rule passes about 49% of zero-edge strategies, so PILOT is a **bounded-cost experiment**: it adds real fills, not proof | min lot |
| FULL | A mixture SPRT on forward excess rejects ≤ 0 (one-sided α 0.05, Holm across forward-tested hypotheses, power 0.8 at half the screen excess) | ≤ 0.5% |
| REDUCED | Execution monitor triggered | × 0.5 |
| RETIRED | Futility, loss budget, two demotions in 12 months, or the kill rule | Revival needs a new hypothesis |

**Why the bar is not √(2 ln N).** That deflation applies to claims made on reused data. A rule frozen before its forward data is a fresh test, so only the forward-tested family counts.

Even so, n ≈ ((z_α + 0.84) × 1.02 / δ)². That means **~290-420 forward trades at H1's full backtest edge (7-11 years at 1h rates), and about four times as many at half that edge.** FULL is unlikely within five years. Forward trades are the evidence; backtests are only a screen.

**Engine changes** (structure project, fix #3) trigger a lab parity replay: matching trade lists keep the state; otherwise the new version starts its own SHADOW record.

**Shadow book.** This generalises `forecast/shadow.py` and `quality_shadow.py`:
- it logs every FINAL signal with frozen levels and the quote;
- it settles on bridge 1m bars plus the persisted spread, because disk data refreshes only every 28 days;
- the paper R of vetoed trades shows what saying no was worth.

### 6.4 Attribution and supervisor

**Attribution.** Each loss gets the first category that fits: operator; execution (cost/R above 1.5× the model or slow market latency, *and* a replay at modelled cost does better); management (right-then-stopped above the twin rate; a measurement, not an exit lever); regime (the forecast gave the realised regime < 0.20); calibration; deterioration; expected loss. If fewer than 70% of the last 30 losses are expected losses, investigate.

**Supervisor.** There is one deterministic record per (signal_id, judged_bar_ms). It holds:
- evidence;
- counterarguments: the narrator, fed from the licence's validated distribution, plus the live record with its n;
- economics: p, W, L, C_R, EV_net, size;
- every check;
- licence state;
- reasons to reject.

The LLM write-up runs asynchronously. Before go-live, `untraced()` is tightened: it keeps signs, binds numbers to labels, requires matching precision and stops small integers matching anything. `/api/ask` gets the same tracing.

## 7. Validation protocol and promotion gates

**Engine.** Licence evidence comes from `server/lab` only, with settings frozen once and recorded. UTC sessions in qualify need fix #3, so **licences depend on it**.

**Engine validation (brief step 02):**
- capture live tick windows through the existing `/ticks` read (download-aware, with your OK);
- replay those windows on SimBroker and compare fills and stop/target order;
- count same-minute ties;
- check spec and points;
- add rejections and requotes to the stress set.

**Evidence tiers.** 2024-26 is **spent** for H1-H3; for them, only forward data is untouched. New hypotheses get dev plus one logged confirm read. Your each-year 2024/25/26 rule stays for filters. For a 1h strategy it would reject a real 0.10R edge ~70% of the time (review estimate), so pooled CI plus leave-one-year-out replaces it (flagged for you).

**Trial ledger.** Seed it from run directories, counting dev-grid configurations: the 288-group lift scan, short-move grids, gold_strategies, Phase 0/1, Fib/engulfing. The reviewers estimate N at 600-1,000+, so backtest claims selected from scans need t ≥ 3.6-3.7 (FWER 10%). Every new free parameter is logged.

**CANDIDATE screen (all must hold):**
1. Excess > 0 in dev at **contemporaneous** cost and at the p75 historical cost/R. Re-price arithmetically with `timeframe_costs.enrich`, check against a sample of SimBroker re-walks, and leave `labels.py` untouched.
2. New hypotheses: the Holm-adjusted day-block CI of confirm excess is > 0.
3. Stress in R (cost ×2 plus 0.02R, +1 min delay, stop-first ties): net ≥ 0.
4. Leave-one-year-out excess > 0. The trimmed-mean test is dropped: with a TP2 cap it is just a hidden hurdle.
5. Neither side's dev excess is below −0.05 with a CI below 0.
6. A pooled reliability slope/intercept test passes.
7. Report PF, max DD (in R and in money), average win/loss and n with CI, against the twins, the trend baseline, the existing robot and one simple rule.

**Population.** The candidate ledger runs the lab `permission()` inside `LabExecutor` and logs every verdict, so what is estimated is what would be traded.

**Parity.** Weekly lab replay of the live period, matched on signal id; C_R frozen during a forward test.

**Compute.** 15m-1h only (~45k candidates, ~0.9M twin walks), a pre-registered entry rule for unsent candidates, 2-3 full runs budgeted.

## 8. Roadmap (shadow-first)

Effort figures are the engineering review's estimate for one developer with an AI assistant.

| Phase | Deliverables | Acceptance | Effort | Touches live? |
|---|---|---|---|---|
| Now | Auto off (§9.1) | — | minutes | Settings, your choice |
| P0 Diagnose + telemetry | **Trade-level diagnostic report** (the brief's first task): strategy (gross vs twins by playbook/TF), execution (bridge-log requested vs fill, latency, spread), management (exit replay, right-then-stopped vs random). Plus journal v2, spread persistence, decision records, governor in OBSERVE, account check, health alerts. **H1 re-run and its frozen forward test start here**, because forward time is the bottleneck | Report delivered; would-block counts | 2-3 wk + 1 wk soak | API restart (your OK); no order change |
| P1 Safety | Enforce the governor; pending re-check; reconcile tool | No false triggers; 3 rows verified | 1-2 wk | API restart (your OK); removes activity only |
| P2 Evidence | Trial ledger; OC/ARL tables; V1; H1 excess; H3; v4 random-limit baseline; A1 | Kill rules applied | 1-2 wk | No |
| P3 Machinery | **Only if a lead survives P2, after §9.2-9.4:** candidate ledger, twins, stress, engine validation, H4 switch | Screen evaluated once | 6-10 wk + compute | Code lands, switched off |
| P4 Licences | Licences file, shadow book, enforcement | Unlicensed strategies → SHADOW | 4-6 wk | Your switch |
| P5 Pilot | First PILOT | Monitors live | 1-2 wk | Your click |
| P6 FULL | Sequential test passed | — | — | Years away |

At the current rates, 40 forward trades take about 1 year for H1 and about 1.7 years for v4. Expect 4-6 months of build before any PILOT.

## 9. Decisions needed from you

1. **Auto off today?** Optionally keep a demo-only 0.01-lot execution probe, excluded from statistics.
2. **Exit.** The live lock is 0.75, but the evidence and the recorded decision use 0.5. Pick one before the H1 re-run.
3. **Discrepancy fixes.** Licences depend on #3. #1 and #2 restore trust in the fast tools.
4. **Structure project.** Keep the v1 engine files byte-identical (new code behind the switch), or H1's forward test restarts. Which goes first?
5. **Decision TF.** 15m-1h decisions, with 1m/5m used for execution.
6. **Risk caps.** Confirm equity (~$20k?). Proposed: 0.25% pilot, 0.5% FULL, 1% maximum; 1% daily, 2.5% weekly, 4%/8% drawdown; 12 trades a day; 2 positions. Do you want PILOT at all, or SHADOW until FULL?
7. **Demo vs real.** Every phase runs on demo. Real money needs a DEMO_ONLY / `--allow-live` bridge change that only you can make. The armed bridge attached to LIVE account 51527502 20 times (latest 2026-10-13 08:52). DEMO_ONLY blocked every order. Was that intended?
8. **ML.** Tables only; LightGBM or logistic regression later as a challenger, if you want.
9. **Drift** never counts as edge: agree?
10. **H5 and Strategy D:** test them or not?
11. **Bridge.** A close-position endpoint and returning the requested price both change the armed bridge.
12. **Approvals.** API restarts, `/ticks` capture, weekly data refresh, checking the 3 `filled` rows, lab compute.

## 10. What this robot will not do, and realistic expectations

**It will not:**
- trade 3m/5m decision rules;
- use exits or filter packages as levers;
- average down;
- edit fingerprinted files or touch the bridge without you;
- let an LLM invent numbers or send orders;
- count drift as skill.

**Expectations:**
- **Near term:** auto sends nothing and P&L is about zero. That replaces a book that lost −0.076R per trade in 2018-23.
- **If one lead survives at half its backtest edge** (about +0.07R on 40-60 trades a year): about 3-4R a year, under 2% a year at 0.5% risk (illustrative). About one year in three still loses.
- **A dead 1h pilot** costs about 0.1% of equity a year, and its loss budget caps the total at 2%.
- **FULL** is probably more than five years away for any current lead.
- **Stop rule:** if no licence reaches PILOT within 24 months, or 10 registered hypotheses fail, conclude that OHLC data at this broker cannot support an edge. Then invest in tick and bid/ask data, not more rules.

Throughout, it supports your manual trades with cost/R, the cone, regime, the news window and traced challenges, under the governor.
