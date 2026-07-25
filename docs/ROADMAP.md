# ROADMAP.md — futures_trader_v1
**v0.1 — 2026-07-25 — the epoch ladder: priors → measurements → frozen dials → live size.**

The options project reached L2 in four months and is still short of L3. This one should be faster, and you named the reason: no Greeks, no chain, no premium-relative exit surface. A futures position's risk is a price distance. That collapses most of the complexity that made the options calibration slow.

**The ladder rule, inherited and non-negotiable:** *one variable at a time, and any engine change that touches trading behaviour RESETS the current epoch's clock.* The options project lost a clean baseline twice by shipping two changes in one deploy (the 07-22 pass bundled ramp de-saturation with the mark-limit execution rework, confounding every fill-dependent statistic and costing a week added to the back end of the window). Data collected across an engine change is not a longer sample; it is two shorter samples pooled into a lie.

---

## Epoch 0 — Pre-flight (~3 sessions, no trading)

**Purpose:** prove the plumbing before any decision depends on it.

**Exit criteria — all must be green:**
- Feed integrity: one DXFeed subscription per box, store heartbeat healthy, zero-volume and NaN guards proven on a real ETH session (the SPX VWAP-on-zero-volume bug pinned a false signal for a whole session and raised nothing).
- Contract registry cross-check: every deployed root's front month, tick value and margin reconciled against the broker's own numbers. **Any seed off by >10% is corrected in the registry, not tolerated.**
- Roll dry-run: `assess_roll` executed against real daily volume for every root; the window and crossover dates printed and eyeballed.
- Session clock: RTH/ETH/break/holiday behaviour verified against the actual tape, including one early close.
- Paper fill model: entries and exits book at plausible prices with one tick of slippage; no path books on submission.
- `check_versions.sh`: zero red.

**Frozen:** everything. Nothing trades.

---

## Epoch 1 — Evidence (2 weeks)

**Purpose:** generate labeled tape. Gates wide open, every strategy paper, journal everything.

**Live:** all strategies at 1 contract, paper. L1 evidence computed and logged. L2 integrator running but its label does *not* drive dispatch yet (v1.3-equivalent boolean regime does). Conviction number logged, gating nothing. SMT and profile dimensions at weight 0, log-only.

**Collected:** per-tick L1 evidence vectors · every scored signal including rejects, with the quote context at signal time · every disposition (fired / sized-out / rejected and why) · full excursion in ticks and R · order-flow snapshots at every decision.

**Exit criteria:** ≥10 clean sessions with no engine change; ≥150 paper trades across the fleet; regime label distribution stable enough to characterize; the daily EOD chain running unattended without a manual step.

**The trap to avoid, named in advance:** the temptation to fix a strategy mid-epoch because a session looked bad. n=7 finds mechanisms; n=99 finds truths. Log it in OBSERVATIONS.md and let it stack.

---

## Epoch 2 — L1 calibration and freeze (2 weeks)

**Purpose:** make the evidence layer honest, then stop touching it.

**Work:** re-fit every ramp bound from the accumulated tick pool (the options analogue: RANGING was saturating at p90=1.0 and colliding with TRENDING on 14–25% of ticks; re-fitting from 60k ticks cut it to 4.3%). Run the co-occurrence check — two regimes scoring high on the same tick is either genuine cross-horizon overlap or a saturated ramp, and the difference matters. Validate labels against **independently derived** session labels from raw price action only — labels that import nothing from the regime stack, so they remain ground truth rather than a restatement of the thing being tested.

**Decision point:** SMT and profile dimensions either earn a nonzero weight from measured edge or stay at zero. No hand-tuning.

**Exit criteria:** L1 bounds frozen and version-pinned in `check_versions.sh`; regime distribution reproducible on replay; a written REGIME_TRUTHS for futures with the discriminator matrix filled in from data.

**Frozen at exit:** L1. Any later change to it restarts Epoch 3.

---

## Epoch 3 — L2 conviction integrator live (2 weeks)

**Purpose:** the committed label drives dispatch; conviction stays observe-only.

**Work:** flip `FT_REGIME_ENGINE=l2`. Calibrate θ_hold / θ_commit / displacement / half-life against label churn — the metric is *switches vs raw argmax flips*, which measures exactly how much the integrator is actually holding. Too much hysteresis and it holds a dead trend; too little and it is a memoryless classifier with extra steps.

**Exit criteria:** label churn inside the target band across ≥10 sessions; no regime-flip exit firing on transient noise; L2 weights frozen.

**This is the real gate.** A new conviction dimension can only be calibrated against a *stable* baseline; calibrating one against a moving target is impossible. The options project's pitchfork build has been waiting on exactly this since July.

---

## Epoch 4 — L3 conviction bars (2 weeks)

**Purpose:** turn the conviction number from a logged observation into a gate.

**Work:** for each strategy × mode, bin realized expectancy in R against conviction decile, **net of commission and one tick of slippage**, and place the bar at the zero crossing. Holdout enforced: the bar is fitted on one slice and validated on another that was never looked at. If a strategy has no crossing — expectancy is negative everywhere, or flat — that strategy does not get a bar, it gets retired to shadow.

**Exit criteria:** bars placed and validated out-of-sample; `FT_CONVICTION_GATES=True`; every strategy's falsification test from STRATEGIES.md has an answer.

**Overfitting warning, stated up front:** strategy grid × dial grid × regime buckets over a few weeks of tape manufactures spurious optima with great enthusiasm. Prefer fewer, coarser buckets with real n over fine buckets that look precise.

---

## Epoch 5 — Live capital ladder (ongoing)

Paper → **1 micro contract, live** → validate fills against paper assumptions → scale.

The go-live gate is not a passing test suite. It is a **tiny-account live shakedown** that proves the things paper structurally cannot: real fill rates on mark-limit entries, real slippage on forced exits, real margin numbers from the broker, and one complete roll executed live on a position that exists.

Order of scaling: contracts before symbols, symbols before modes. Add one thing at a time and let it run an epoch.

**Never skipped:** the first live roll and the first live overnight hold each get their own supervised session. Both are irreversible in the way that a bad fill is not.

---

## Parallel tracks (ungated, may run any time)

- **P1 — Order-flow archive.** Tick trade data with aggressor side cannot be reconstructed after the session, exactly as option chains could not. Archive from day one on every box and harvest nightly. The options project discovered this exposure late and had ~29 boxes accumulating an irreplaceable dataset with no copy on control. Do not repeat it.
- **P2 — SMT peer broadcast.** Control publishes per-symbol state so correlated boxes can see each other. Log-only first.
- **P3 — Level-hierarchy validation.** Does the tier ranking actually predict? Offline, no bot code.
- **P4 — Conditional probability tables.** Fold into the EOD chain from the start, never as a manual job. Your standing directive: anything that needs running daily is an EOD phase, not something to remember.

---

## What resets an epoch

Any change to: regime evidence terms, conviction weights, strategy entry logic, stop/target construction, sizing rules, or the fill model. Observability, logging, docs, and ops tooling do **not** reset it — that distinction is what keeps the ladder from being unclimbable.
