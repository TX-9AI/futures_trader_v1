# STRATEGIES.md — futures_trader_v1
**v0.1 — 2026-07-25 — the strategy roster, the edge thesis behind each one, and the falsification test that decides whether it lives.**

---

## The premise

You asked what I would build to win. The honest answer starts with what the options book already proved: **the entries were never the problem.** 99 sweep trades, 75% win rate, −$3,444 net. Entry quality was demonstrably real and the book still bled, because the payoff was upside-down and nothing in the engine was allowed to say *"this trade's reward does not pay for its risk."*

So the roster below is built on three structural commitments, all of which are already live in the Phase-1 code:

1. **Every strategy declares a stop AND a target before it is sized.** `MIN_RRR` gates. A setup with no defensible target is not a trade, it is a hope.
2. **Every position with ≥2 contracts banks a piece at +1R and runs the rest on structure.** Options sizing could not express this; futures sizing can, and it converts the observed "winners give back a third of MFE" problem into a mechanical solution rather than a smarter trailing stop.
3. **Nothing new gates anything.** New signal dimensions ship at weight 0 and earn their weight from realized edge over an epoch. This is the single practice that most consistently protected the options fleet from confident nonsense.

Each strategy below states its **edge thesis** (why the money exists), its **mechanics**, and its **falsification test** — the specific measurement that retires it. A strategy with no falsification test is a belief, and beliefs are how a 75% win rate loses money for two months.

---

## Mode → strategy map

One box, one symbol, one mode. Modes are not risk settings; they are different businesses with different holding periods, different margin treatment and different reasons for existing.

| Mode | Holding period | Margin basis | Strategies | Flatten |
|---|---|---|---|---|
| **SCALP** | seconds–minutes | day rate | S1 Absorption Reversal · S2 Killzone Micro-Continuation | cash close |
| **DAY** | minutes–hours | day rate | D1 Opening Drive Break+Retest · D2 Liquidity Sweep Reversal · D3 Trend Continuation | cash close |
| **SWING** | days–weeks | **initial** rate | W1 HTF PD-Array Swing · W2 Value Migration Fade · W3 Calendar Spread | never (structure only) |
| **HEDGE** | indefinite | **initial** rate | H1 Beta-Weighted Portfolio Hedge | never (rebalance/roll only) |

---

## DAY mode

### D1 — Opening Drive Break + Retest
**Edge thesis.** The opening range is the session's first genuine auction, and the participants who break it have committed capital at a price everyone can see. The retest is the *falsification step* of the break hypothesis: a level that was never tested produced no evidence, and a level whose retest closed back through it was tested and failed. This is the options flagship ported intact — it is the single most validated mechanic in the whole lineage, and the futures version is strictly better because the stop is a price, not a premium, so the risk is knowable to the tick.

**Mechanics.**
- Range = the contract's first 5 minutes of RTH (09:30–09:35 for index; the registry supplies each contract's own RTH open, so gold's range is 08:20–08:25).
- **BREAK** = a 1m candle that *opens inside* the range and *closes outside* it. Opens-inside is definitional: a candle that began life outside never broke out, it was already out.
- **RETEST** = any subsequent 1m candle within 12 bars whose *wick* enters the range and whose *body* stays entirely outside. **Bars, not ticks** — deduped on candle timestamp. (otv3 counted 15-second poll ticks as bars, inflating 4× and killing a live armed window in three minutes.)
- **No tolerances anywhere.** No break buffer, no grace band. The retest *is* the noise filter.
- **Stop** = beyond the impulsive (break) candle's wick. **Target** = 1× range projection, then trail.
- **Futures-native additions:** the break must carry **CVD alignment** (aggressive flow in the break direction — a break on declining cumulative delta is a liquidity grab wearing a breakout's clothes); and the break is **refused if a top-tier level sits within 0.5R of entry in the path** — that is not a target, it is a wall.

**Falsification.** Expectancy in R, bucketed by retest depth (already instrumented as a distribution in the options book but never graded). If expectancy ≤ 0 net of commission across ≥60 trades in a frozen epoch, D1 is demoted to shadow.

### D2 — Liquidity Sweep Reversal
**Edge thesis.** Stops cluster above obvious highs and below obvious lows. Price runs them, fills size against the resulting liquidity, and reverses. The money is in distinguishing a **raid** (penetrate, absorb, reject, reclaim) from **acceptance** (penetrate and hold) — and the options book proves the detection works. What it did not have was a level hierarchy or a payoff rule.

**Mechanics.**
- **Location** is graded, not boolean. Straight from the 2026-07-24 observation: Overnight High/Low and PDH/PDL are the top tier; historic multi-day S/R and naked POCs mid; individual session extremes below that; self-defined equal highs/lows lowest. `LEVEL_TIERS` in config carries the value, and it is the heaviest single scoring dimension (0.25).
- **Penetration** past the level, then **rejection**: reclaim and hold, confirmed by a 1m close back inside.
- **Absorption confirm (new, futures-only).** A genuine raid shows *delta divergence* — a new price extreme on weaker cumulative delta in the direction of the push. Sellers pressed and price did not follow: they are being absorbed. This is the highest-information confirmation available in futures and it has no options analogue at all.
- **Stop** just beyond the sweep extreme — structurally tight, which is what makes the R:R work. **Target** = the opposing liquidity pool, which is a *named level*, not a percentage.
- **Scale 50% at +1R, trail the rest on structure.** This is the direct fix for the observed asymmetry (losers MFE +12% before a wide stop; winners booked at +25% off a +60% peak).
- **Washout guard.** The options data showed losses clustering into whole washout days (0/5, 0/2, 5 straight stops). D2 requires the regime label to not be trending against the reversal direction, and the box's own net daily P&L breaker stops the bleed.

**Falsification.** Win rate is *not* the metric — expectancy in R is, sliced by level tier. If the top tier does not out-expect the bottom tier by a clear margin, the hierarchy is decoration and the whole level-strength dimension goes to weight 0.

### D3 — Trend Continuation on PD Retracement
**Edge thesis.** Trends resume more often than they reverse, but only a pullback gives an entry with a tight invalidation. The regime engine is *stingy* about calling trend, so a trending label is itself the high-conviction signal.

**Mechanics.** Trending regime + price retraces into the 0.62–0.79 zone of the impulse leg, ideally coinciding with a 5m FVG or an order block. Entry on a micro-BOS back in the trend direction. Stop past the retracement extreme. Targets: prior leg extension, then the next HTF liquidity pool. **Exit on regime flip** — the trade is *defined* by the trend, so a flip kills the thesis regardless of P&L. Plus exhaustion: extension beyond 2× ATR from the anchor tightens the trail; momentum divergence at a new extreme exits.

**Falsification.** Compare against a naive "enter at the midline touch" control. If the PD/FVG confluence adds no expectancy over the naive version, drop the confluence requirement and keep the simpler trade.

---

## SCALP mode

### S1 — Absorption Reversal at a Level
**Edge thesis.** This is the purest order-flow trade and it cannot be built without tick data, which is precisely why it is worth building — it is not available to anyone reading candles. Price arrives at a graded level; delta pushes hard in one direction; **price does not go.** Someone large is filling passively. When the aggressor gives up, the snap back is fast and the invalidation is inches away.

**Mechanics.** Price within N ticks of a tier-1 or tier-2 level, cumulative delta over the last N bars strongly one-sided, and net price progress under `ABSORPTION_DELTA_TICKS`. Entry on the first flip of delta sign. Stop beyond the absorption zone (typically 6–15 ticks). Target 1.5R minimum, usually the nearest opposing micro-pool. **Time stop:** if the trade has not progressed within N minutes, it is out — absorption that does not resolve quickly was not absorption. Killzone-gated by default.

**Falsification.** This one is the most likely to fail, and it should be held to the strictest standard: it needs a positive expectancy *after* commission and one tick of slippage on both sides, because a scalp's cost structure eats a marginal edge alive. If it cannot clear that bar, it dies without ceremony.

### S2 — Killzone Micro-Continuation
**Edge thesis.** Session opens produce directional displacement. Inside a killzone, the first pullback after a confirmed 1m break of structure is a mechanical continuation with a clear invalidation.

**Mechanics.** Inside an enabled killzone (London open, NY AM, Silver Bullet), after a 1m BOS with CVD confirm, enter on the first retrace into the resulting FVG. Stop past the FVG origin. Target 1R–2R. High frequency, small size, hard time-boxed to the killzone.

**Falsification.** Expectancy by killzone. If only one of the three windows carries the edge, disable the others rather than averaging them into a mediocre composite — the options book's biggest analytical mistake was pooling sessions that ran different engines.

---

## SWING mode

### W1 — HTF PD-Array Swing
**Edge thesis.** The higher timeframe pays for patience. A daily-bias-aligned entry into a daily FVG or order block located in the discount half of the dealing range gives a multi-day hold with an invalidation that is one HTF structure level away. Held overnight, so it collects the move that intraday trading structurally cannot.

**Mechanics.** Daily bias from HTF trend + weekly range position. Wait for a 4h/1h retracement into a *daily* array in premium (short) or discount (long). Confirm with a 15m CHoCH. Stop beyond the daily array. Targets = successive HTF liquidity pools. **Sized on the overnight initial margin rate, always** — and the overnight gate runs at 16:30 so a position that cannot be carried is reduced on our terms, not liquidated on someone else's.

**Falsification.** Because holds are long, n accumulates slowly — this strategy is judged over epochs, not sessions, and its first honest read is Epoch 3 at the earliest. Interim measure: MAE distribution. If the stop is being hit on noise rather than structure, the array selection is wrong.

### W2 — Value Migration Fade
**Edge thesis.** Markets spend most of their time rotating inside a balanced auction. When value is stable and today's value area overlaps yesterday's, the edges of value are high-probability rejection points and the POC is a magnet.

**Mechanics.** Balanced regime + overlapping value areas. Fade VAH/VAL back toward POC. Stop beyond the prior session's extreme. **Abort condition is the whole trade:** two consecutive 30m closes accepted outside value means the auction is no longer balanced and the premise is void — exit immediately, do not wait for the stop. This is the market-profile analogue of the condor's regime-flip exit, and it is the discipline that keeps a range trade from becoming a trend loss.

**Falsification.** Expectancy conditioned on the regime label at entry. If it is positive only when the regime engine already said BALANCED, the trade is fine; if it is positive regardless, the regime engine is not adding anything and that is a *finding about the regime engine*.

### W3 — Calendar Spread
**Edge thesis.** Honest framing: this is a small, low-margin, low-variance trade, not an alpha engine. The front/back basis has a stable relationship driven by carry, storage and rates. Inside the roll window, order flow from everyone else's mechanical rolling distorts it. When the spread deviates from its N-session mean by more than 2σ during that window, it tends to revert — and spread margin is a fraction of outright margin, so the return on capital can be respectable even when the return on risk is modest.

**Mechanics.** Spread = front − back, tracked daily. Enter on a 2σ deviation inside the roll window, exit at the mean or at the roll deadline. Sized in spread units. **The same spread primitives execute the mechanical roll** — building the strategy and building the roll are the same work, which is why it is in the roster at all.

**Falsification.** Run it in shadow for two full roll cycles before it is allowed to size. If the 2σ deviation does not revert more often than chance, it never trades. I would rather state plainly that this one might not survive contact than dress it up.

---

## HEDGE mode

### H1 — Beta-Weighted Portfolio Hedge
**Edge thesis.** None, and that is the point. A hedge is not supposed to make money; it is supposed to make a *specific* amount of money in a *specific* scenario. It is measured against the exposure it was bought to offset, never against a P&L target, and the reporting keeps it in a separate ledger so it can never contaminate the trading expectancy.

**Inputs (prompted by `configure.sh`, per your spec that hedge is standalone and user-shaped):**
portfolio value · beta (or a holdings list to derive it) · target hedge ratio · instrument · maximum contracts · always-on vs conditional · rebalance drift band · roll behaviour.

**Mechanics.**
`contracts = (portfolio_value × beta × hedge_ratio) / (index_price × multiplier)`, rounded to whole contracts with the residual reported so the operator sees the rounding error in dollars. Rebalances when realized drift exceeds the band (default 10%) — *not* continuously, because a continuously rebalanced hedge is a commission generator. Conditional mode arms the hedge only when the HTF regime turns risk-off and disarms when it normalizes, with hysteresis so it cannot flicker. Rolls on the same volume-crossover machinery as everything else, as a calendar spread, so the hedge is never absent for a moment.

**Falsification.** Hedge effectiveness ratio: variance of (portfolio + hedge) vs variance of portfolio alone, over the epoch. A hedge that does not reduce variance is failing at its only job, whatever its standalone P&L says.

---

## The cross-cutting layer: what actually makes these "prescient"

The strategies above are the *vehicles*. The intelligence is the scoring layer they all pass through, ported from the three-layer architecture that took the options project four months to get right:

**Layer 1 — graded evidence, not booleans.** Each regime gets a hard veto, soft-necessary conditions and weighted corroborators. Futures evidence terms: ADX/EMA structure, ATR expansion vs contraction, value-area migration, CVD alignment with price, position within the day's developing range, session phase, and volatility relative to the contract's own recent regime.

**Layer 2 — persistence.** The leaky conviction integrator: conviction rises on agreement, decays on disagreement with decay resistance scaled by banked conviction, dt-aware, always-argmax with hysteresis. This is *the* answer to your "persistence and prescience" requirement, and it is the component that fixed the options engine's worst pathology — a memoryless classifier dropping to UNKNOWN mid-trend at ADX 29, vetoing trades during the strongest conditions it would ever see. Persistence is not smoothing; it is the refusal to un-learn something on one tick of contrary evidence.

**Layer 3 — conviction bars.** Gates stay wide open until the tape says where to put them. Bars are placed at the **fee-and-slippage-adjusted expectancy zero crossing**, per strategy, per mode, on held-out data. Not before Epoch 4.

**SMT divergence (ships at weight 0).** When ES makes a new high and NQ does not, one of them is lying. This is a genuine cross-contract signal with no options analogue, and the fleet is already structured to deliver it: the control layer broadcasts a peer-state file exactly as the options fleet broadcasts `brief_flags.json`. Log-only in Epoch 1, scored in Epoch 2, weighted only if it earns it.

---

## What I deliberately did not include

- **Anything requiring a prediction of a price level at a future time.** The system reads state and reacts; it does not forecast.
- **Martingale, averaging down, or any position sizing that grows into a loss.** No exceptions, no "scaling into value."
- **News/event trading.** No modeled edge, enormous slippage.
- **Sub-second latency plays.** Not reachable from an EC2 box on a retail API, and pretending otherwise builds a backtest that cannot exist live.
- **Optimized parameters chosen before there is tape.** Every threshold in the config is a *prior* labeled as such, and the epoch ladder exists to replace priors with measurements.
