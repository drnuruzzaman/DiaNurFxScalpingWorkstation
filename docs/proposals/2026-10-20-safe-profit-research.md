# XAUUSD robot: ways to trade more safely and profitably (research run, 2026-10-20)

## 1. Bottom line

- **Proven:** none of the 16 tests found a new edge that holds up out of sample. Every edge or selection test failed once the reviewer's corrections were applied. The best result is a real but small round-number effect that does not survive stressed costs.
- **Promising, but only for the shadow book:** pattern_break 1h is the one robust cell. Its commission-corrected net R is +0.150 (dev) and +0.123 (confirm), with 9 of 9 years positive (proposal §5.3). Two other ideas belong in shadow logging and nothing more: the $50 round-level context, and the HTF confidence-cap veto, which failed on one playbook check.
- **Safety levers that did measurable work:**
  - a drawdown breaker of the CUSUM type (it needs recalibration first);
  - fixed-fractional sizing from the stop instead of a fixed 0.01 lot (the formal label is "rejected", but that comes from how the criterion was defined);
  - a swap check read from MT5 before any 1h-4h buy cell is licensed;
  - a cost/R hygiene envelope;
  - a release blackout for 5m-30m only, in observe mode.
- **Not working:** passive limit entries, the governor caps, USDJPY state vetoes, the folklore session/trend filters, turn-of-month, volatility-shock abstain, and picking cells by backtest rank.
- **Realistic path to "safe and profitable":** start with NO TRADE as the default. Put the safety levers into the governor and the envelope. Run all 49 cells in the shadow book. Licence only cells with forward evidence, starting with pattern_break 1h at pilot size, after swap and cost are read from the live account. Expect small, slow gains, not a lot of trading.

Three cross-cutting findings change how the old numbers should be read:

1. **The lab's r_multiple leaves out the 6-point commission.** Net tables built on it were about 0.004-0.035R too optimistic: 3m 0.035, 5m 0.029, 15m 0.018, 30m 0.012, 1h 0.0085, 2h 0.006, 4h 0.004. Every result below uses net = r − 6/risk_pts. After that correction, pattern_break 1h still holds at +0.150 / +0.123.
2. **The lab fills some orders at a stale broker 00:00 price inside the daily break.** This affects about 0.25% of trades and is worth about +0.09R on them.
3. **The lab charges no swap.** The bridge is now attached to a Pepperstone LIVE account, so the real swap_long and swap_short must be read from MT5. They are not on disk, and the config says swap_free=True.

## 2. Safety levers to adopt

These change risk or cost, not edge. Each one is listed with what was measured and which part of the proposal it belongs to.

| Lever | Measured effect | Scope | Proposal component | Status after review |
|---|---|---|---|---|
| **CUSUM drawdown breaker** (T08, design C; reference 0.037; h = 19.0 / 30.8 / 39.5 / 44.5R at 39 / 120 / 218 / 325 trades a year) | Detects losing books: P(trip within 3 years) is 0.53 / 0.69 at mu = −0.013 and 0.73 / 0.89 at mu = −0.047 (120 / 218 trades a year). Caps the 5-year p95 drawdown at f = 0.5% to 12.5-13%, against 15.6-34% with no breaker (120/yr). Fixed 4%/8% breakers depend on f: at 1% they false-trip 69-97% of the time. | Book level, at 120 to 218 trades a year. Not for a single licence: power is 0.33-0.44 at 39 trades a year. | Governor | **Weakened.** It was calibrated against mu0 = +0.074. At the edge the dev data actually shows (+0.04), it falsely trips 23-26% of the time within 3 years and 44-49% within 5. Recalibrate h against about +0.04, or accept that false-stop rate explicitly. Neither replay ever tripped, so the real-data evidence is empty. |
| **Fixed-fractional sizing from the stop** (T07, V2) | Risk-adjusted: drawdown in R per unit of annual R is lower than fixed 0.01 lot on every profitable confirm book (T1+T2 0.76 vs 1.26; PB1h 1.80 vs 2.26). Yearly $ results match the R sign in at least 8 of 9 years, while fixed lot shows only 7 of 9. Fixed lot even shows a positive $ Calmar (0.64) on ALL49, a book that lost 409R. Over 2018-26, T1+T2 earns CAGR 6.6% with max DD 8.5% (V2), against 1.6% / 2.4% for fixed lot. | Every TF. A minimum-lot block rule at f = 0.25% blocks 6.9% of trades (25% on 4h); at 0.5% it blocks 0%. | Envelope (sizing) | **Inconclusive, leaning towards fixed-fractional.** The formal "rejected" comes from a $-Calmar criterion that rewards gold drift. A volatility-target overlay was correctly dropped: Calmar ratio 1.43, CI [0.68, 3.45]. |
| **Swap check before licensing a buy cell** (T04) | At −5.5%/yr long swap, buys lose 0.025R per trade on 1h and 0.053R on 4h (dev holds). That is about the trading cost on 1h and 3.5 times it on 4h. mtf_pullback 4h buys go from +0.030 to −0.025, with breakeven at −3%/yr. pattern_break 1h buys go from +0.199 to +0.163 and stay positive. For MT5, −5.5%/yr is about −61 points per lot per night at a gold price of 4000. | 1h-4h buys. The 15m and 30m flags are noise. | Licence (EV_net input) | **Conditional.** The arithmetic was independently verified (0.0245 and 0.0525). The rates are assumed, so read the real ones from MT5. |
| **Cost/R envelope, C_R ≤ 0.03** (T01) | Kept minus random veto: +0.033 [+0.009, +0.057] in dev and +0.023 [+0.010, +0.037] in confirm. The whole effect comes from cost (cost part +0.039, gross part −0.005). | In practice 3m/5m and tight stops. On 15m+ it has almost no effect: in 2026, 98-100% of trades are kept. | Envelope | **Weakened.** Cost hygiene only, with no edge credit. Twin excess fails in dev (−0.043). |
| **Pre-release blackout scaled to hold time** (T05, V3; NFP/CPI/FOMC/PCE/PPI inside the expected hold) | Blocks 6.5% of trades. Mean R moves by −0.0015 (dev) and −0.0009 (confirm). Worst-1% R under assumed event slippage: −1.393 vs −1.503 (dev). | 5m-30m only, in observe mode. On 1h-4h it gives up about 27R of 117R in confirm with no tail gain. | Envelope | **Weakened.** Without the assumed event slippage, the dev tail beats only 11% of random blocks, and drawdown never reaches the 90% bar (63% / 81%). Validate it with real fills around releases. |
| **Rollover block** (T02, V2) | −0.071 [−0.136, −0.001] in dev; −0.062 [−0.153, +0.024] in confirm, with 2025 at +0.158. | 3m/5m, which are already NO TRADE | Envelope (cosmetic) | **Weakened, effectively a fail.** Give it zero edge credit. The Friday last-2h entry block removed about +46R of winners in confirm. A weekend tail should be handled by an exposure rule for positions already open. |
| **Execution latency** (T03 side check) | At live p75 latency (n = 82 demo orders), the pooled leak is about −0.004R. On 5m alone it is about −0.016R (n = 27). | Every TF | Governor (monitoring) | **Provisional.** Re-measure on the live account. |

## 3. Candidate edges for the shadow book / forward test

Ranked. None of these is approved for live money.

1. **pattern_break 1h, the existing playbook cell (proposal tier 1).**
   - **Rule:** the frozen pattern_break playbook on 1h.
   - **Dev / confirm:** commission-corrected +0.150 / +0.123, 9 of 9 years positive. Buy side: dev +0.199 [+0.060, +0.341], confirm +0.190 [−0.003, +0.368].
   - **Stress:** swap at −5.5%/yr takes the buy side to +0.163 (dev) and +0.167 (confirm). Breakeven long swap is about −30%/yr.
   - **Review:** none specific to this cell in this run. The figures come from the corrected tier table and the swap review.
   - **Caveats:** this is the only robust cell. It is long-heavy during the gold rally, so compare it with a same-side baseline. 2021 was negative. Choosing it with both periods makes the equity figures optimistic.

2. **$50 round-level cascade continuation (T13), as a context/shadow feature only.**
   - **Rule:** after price crosses a $50 level, enter in the direction of the cross with a 15-minute time exit.
   - **Dev:** net +0.082R; difference against a non-round control grid +0.141 [+0.092, +0.190], 6 of 6 years.
   - **Confirm:** net +0.019; difference +0.048 [+0.019, +0.076], positive in all 3 years. On the USDJPY holdout the gross difference is +0.034 / +0.068, but net is −0.11 / −0.07.
   - **Stress:** −0.008 (2024 −0.023).
   - **Review:** the tester said "promising"; the reviewer corrected it to **FAIL as a strategy** under the prereg's own stressed-net criterion. The mechanism is real but small.
   - **Caveats:** about 300 ideation looks. The edge shrank about 3x out of sample. Exit slippage is optimistic. Events get denser as the gold price rises.

3. **HTF confidence-cap veto (T12 V1): no 1h-4h trade when confidence ≥ 79.**
   - **Dev:** kept minus vetoed +0.158 [+0.069, +0.249], positive on both sides.
   - **Confirm:** none clean; the 2024-26 figure (+0.128) was already spent in ideation.
   - **Stress:** kept net +0.018.
   - **Review:** **fail holds.** It failed the per-playbook check (mtf_pullback −0.123), and 2022 was adverse. Dropping mtf_pullback to rescue it would be a post-hoc variant.
   - **Caveats:** forward shadow data only. It has no effect on 15m/30m (−0.028).

4. **Re-test at decision time inside the shadow book:** the signal-flow consensus veto (from not-tested). The shadow book logs all 49 cells, so this vote can be computed forward without lookahead.

Not an edge, but listed as the brief allows: a **beta allocation** to long gold. Over 2018-26, buy-and-hold returns +14.6%/yr swap-free but +8.4%/yr with −5.5% carry. Over 2008-17 the figures are +4.3% and −1.4%. Max drawdown is −58.1% with carry. A 10% vol target returns +7.4%/yr over 2018-26 with carry, with a −17.5% drawdown. Over 2011-15 every version lost: buy-and-hold ended at 0.566 with carry. This is beta, not timing skill. The Asia and overnight sleeves showed no excess beyond drift; the overnight one is a reopen-quote artifact and returns −25.5% under stress.

## 4. Tested and failed

- **Passive limit entry, 0.2R (T03):** dev +0.0113, below the +0.015 bar. Confirm −0.0088, 0 of 3 years positive. The 39% of signals it missed averaged +0.305R. On 1h it lost −0.178 per signal.
- **Governor caps, heat cap and cooldown (T09):** no configuration passed the equivalence test. Drawdown cuts matched random skips at the same rate, and V1 on T1+T2 lost −0.036R per trade in confirm.
- **Licence designs (T10):** the pooled-family design (b) is safe at zero edge (−0.012%), but at positive edge P(drawdown ≥ 8%) is 0.89. That comes from uncapped sizing of up to 1% per trade, which was not modelled with the proposal's caps. The per-cell design (a) lost −14.1% on the dev replay.
- **USDJPY state vetoes (T11):** confirm kept minus vetoed is −0.037 / −0.000 / +0.020 / −0.065, with Holm p = 1.0. The V1 veto share jumped from 19% to 51%.
- **Engine vetoes (T12):** the confluence veto is +0.072 with a CI that includes 0. The widened-stop veto is +0.029 and turns negative (−0.018) under stress.
- **Round-level journal tilt (T13 V3):** +0.034 [−0.021, +0.083], 3 of 6 years.
- **Turn-of-month long (T14):** on the 1998-2017 holdout the excess is +0.017 [−0.031, +0.065], 11 of 20 years.
- **Picking cells by backtest rank (T15):** selected minus random is positive in only 2 of 4 dev years. The empirical-Bayes calibration slope is −1.93 (dev) and −0.54 (confirm).
- **Folklore filters (T16):**
  - 12-16 UTC gate on low TFs: twin excess −0.029, so the lift belongs to the hour, not the signal.
  - 07-16 UTC gate on 15m-4h: +0.009 in dev.
  - Trade only with the D1 trend: −0.115 in confirm; the counter-trend trades it vetoed earned +0.217.
- **Volatility-shock abstain (T06):** 0 of 4 tail metrics were worse inside the shock state. In confirm the blocked trades did better (+0.061).
- **Rollover pending-order cancel and Friday last-2h block (T02):** the cancel did not replicate (−0.019 ± 0.21, n = 81). The Friday block removed +46R in confirm.
- **From ideation:**
  - equity-curve switch: within ±0.013R;
  - USDJPY leads gold: |corr| < 0.015;
  - Asia-minus-COMEX: gross +1.65 bps against about 2.4-2.8 bps of cost;
  - London fix: under 1.5 bps;
  - calendar folklore: |t| < 1;
  - USD-beta hedge: breakeven needs an edge above about 0.2R;
  - prior-day-range context: it is daily momentum, i.e. beta.

## 5. Not tested yet

- **Broker cost audit:** IC Markets vs Pepperstone all-in cost and where the spread feed comes from. This needs bars from both servers, /ticks windows and a demo round trip, all with your OK. Switch accounts if all-in C_R differs by more than 0.005R at 1h or 0.02R at 5m.
- **Swap terms:** read swap_long, swap_short and the triple-swap day from MT5 on the live account.
- **Real slippage around releases:** live or forward fills at NFP, CPI and FOMC, needed to validate T05.
- **Shock-time spread widening:** needs tick or live spread data. T06 used minimum bar spreads, so it says nothing on this.
- **Rates/USD factor:** real yields, 2y/10y and DXY are not on disk; downloading them needs your OK.
- **Positioning data:** COT history, ETF holdings and GVZ. Only the current COT report is available.
- **Release first-move drift or fade (proposal H5):** needs your OK; signs flip across periods.
- **FOMC positioning unwind:** only about 8 events a year. Parked.
- **Hedging-demand close momentum:** overlaps the excluded late-NY IBS window. Parked; needs your OK.
- **USDJPY playbooks:** a one-strike falsification and a diversification test both need about 1.5h of lab runs per TF and your OK. Existing USDJPY v4 evidence is −0.240R per trade.
- **Routing each family by forecast regime:** a near-duplicate of the regime filters already excluded. Parked.

## 6. Fixes to the research tools that the run uncovered

1. **Commission:** lab r_multiple excludes the 6-point commission. Use r − 6/risk_pts everywhere, or fix it at the source.
2. **Stale fills:** the lab fills at a stale broker 00:00 price inside the daily break, on about 0.25% of trades.
3. **Swap:** the lab charges zero swap. Add a per-side swap term with the Wednesday triple.
4. **Spread:** costs use the minimum 1m bar spread, which understates real spread, especially at the 01:00 reopen (mean 36 points vs 20 at 01:05) and during shocks.
5. **USDJPY mechanism check:** T03's s5_mech.py has a scaling or cost bug for USDJPY (implausible +0.17R gains).
6. **Look-ahead on stop-kind signals:** T03's V3 anchored them before their later trigger. Check any limit or delay simulator for the same issue.
7. **Time stamps in two places:**
   - The order-ledger ids are UTC bar opens, not broker time.
   - Quarter and year buckets used broker milliseconds read as UTC, so trades within 2-3h of a boundary can land in the wrong bucket.
8. **Criterion definitions:**
   - A $-Calmar comparison against a fixed lot rewards gold drift. Use drawdown in R per unit of annual R.
   - Calmar on a book with negative annual R is meaningless, so it cannot be used to choose variants.
9. **Breakers and licence simulations:**
   - Calibrate breakers against the edge actually measured (+0.04), not the target edge.
   - Licence simulations must include the proposal's 0.5% cap and the breaker.
10. **Data:** no MTF-LS v4 backtest trade list exists on disk.

## 7. How this slots into the Permission-to-Trade plan

- **P0, measurement:** apply the commission correction, fix the stale-fill bug, add swap to the lab, and read swap from MT5.
- **P1, shadow book:** log all 49 cells plus the shadow features (the round-level context, the confidence-cap veto and the consensus vote) at decision time.
- **P2, governor and envelope:**
  - fixed-fractional sizing at about 0.25-0.5%;
  - the CUSUM breaker, recalibrated against about +0.04, at book level;
  - the C_R ≤ 0.03 hygiene envelope;
  - the 5m-30m release blackout in observe mode;
  - 3m/5m kept at NO TRADE.
- **P3, licences:** licences come from forward evidence only. T15 showed that backtest rank does not persist, so cell rank cannot be the licence. The first pilot candidate is pattern_break 1h, net of real swap.
- **P4, live scaling:** only after forward evidence, plus the broker cost audit.

**First three things to do:**

1. Read swap_long, swap_short and the triple-swap day for XAUUSD from MT5 on the Pepperstone live account, and re-price the 1h-4h buy cells with them.
2. Patch or correct the lab for commission, swap and the 00:00 stale fill. Then rebuild the §5.3 tier table and confirm that pattern_break 1h holds.
3. Start the 49-cell shadow book with fixed-fractional sizing and a CUSUM breaker recalibrated against about +0.04, so that forward evidence starts building towards a pattern_break 1h pilot licence.

## 8. Note

This is research on your own trading system. It is not financial advice. Every figure is a backtest or simulation with known cost understatements. Past results do not guarantee future returns, and gold's strong rise in 2024-26 flatters any long-biased number.