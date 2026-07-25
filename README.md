# futures_trader_v1 — Vertigo Capital

**Modular futures trading engine · one box = one symbol = one mode · TastyTrade/DXFeed · standalone or fleet-controlled · paper-first**

---

## ⚠️ STATE OF THE BUILD — read this first

This repo is at **Phase 1**. What exists is the *futures-native foundation*: the layer that has no analogue in `options_trader_v3` and that every later module is generated against. It is tested and proven; it does not yet trade.

| Layer | State |
|---|---|
| Contract registry (specs, front month, expiry rules) | ✅ built, 62/62 tests |
| Roll state machine (volume crossover, deadlines) | ✅ built |
| Session clock (Globex, break, holidays, killzones, mode flatten) | ✅ built |
| Contract sizing + R:R gate + net daily halt | ✅ built |
| Margin manager (day vs overnight rates, fleet publication) | ✅ built |
| Roll manager (calendar spread, granularity, half-complete paging) | ✅ built |
| Trade log (futures schema, R-native, mode-scoped) | ✅ built |
| Strategy design + epoch ladder | ✅ documented (`docs/`) |
| Analysis stack (structure, liquidity, profile, order flow, L1/L2) | ✅ built, 76/76 tests |
| Strategies (the roster in `docs/STRATEGIES.md`) | ✅ built, 75/75 tests |
| Execution engines (entry, exit, position manager) | ✅ built |
| Feed producer + store + reader | ✅ built |
| `main.py` loop, `status.py` | ✅ built, end-to-end tested |
| Unattended install (`install`/`setup_ec2`/`bootstrap`/`configure`) | ✅ built |
| Broker + DXLink adapters | ⚠️ **skeletons that refuse** — verify in Epoch 0 |
| Control plane (`control/` fleet, orchestrator, EOD chain) | ⬜ Phase 5 |
| Capacity calculator / tick chart + eligibility policy | ✅ built |
| `devtools.sh` (capacity, config, tests) · `check_versions.sh` | ✅ built |
| Installer / configure.sh / fleet control | ⬜ Phase 4 |

**`FT_PAPER_TRADING` defaults to `True` and must never default otherwise in this file.**

## Unattended install

```bash
source bootstrap.sh && curl -fsSL https://raw.githubusercontent.com/TX-9AI/futures_trader_v1/main/install.sh -o install.sh && bash install.sh
```

`bootstrap.sh` (gitignored, copied from `bootstrap.example.sh`) exports the
credentials; `setup_ec2.sh` sees them, skips every prompt, builds the venv,
installs `futures-feed.service` and `futuresbot.service` (feed first), hardens
the host against mid-session restarts, then **shreds `bootstrap.sh`** and
removes the deploy checkout. **Installs are always paper** — going live is a
separate, deliberate act in `configure.sh` that requires typing `LIVE`.

## What is NOT verified

`TastyTradeBroker` and `DXLinkTransport` are **skeletons that raise rather than
guess**. Futures symbology, the aggressor-side field, signed vs sided order
prices, and which buying-power number to size against all need confirming
against the live SDK on the tiny account. A skeleton that returns a plausible
fill is more dangerous than one that refuses — that is the
submission-vs-fill defect family wearing a different hat. Use `devtools` item 25
(`--sim`) to exercise the full pipeline without a market.

---

## The one-line architecture

One box runs one contract root in one mode under `futuresbot.service`, fed by a single `futures-feed.service` that owns the box's only DXFeed subscription and writes a WAL SQLite store. Every other process on the box is a *reader*. Control tooling lives in this repo (`control/`) and is driven from this repo's own devtools menu — `day_trader_pro` is not involved.

---

## Design commitments, and the failure each one prevents

Every rule below is inherited from a specific, expensive `options_trader_v3` incident. They are not style preferences.

1. **No percentage of price, anywhere.** Distances are ticks or ATR multiples. *(A 0.05% break buffer meant $0.49 on one symbol and $3.00 on another; the entire tolerance-bug family came from this.)*
2. **Slice the dataframe, never increment a per-call counter.** *(`bars_since_break` counted 15s poll ticks as bars, inflated 4×, and killed a live armed window in three minutes.)*
3. **Nothing is booked on submission.** Entries and exits require a confirmed fill; an unconfirmed close leaves the row OPEN for the retry loop. *(Defects N/O/P — booked ghosts, fabricated prices, a rolled position that existed only in the database.)*
4. **Mode isolation at the schema level.** *(Paper P&L gating the live breaker, defect Q.)*
5. **Fail loud, never fall back silently.** A guarded import that swallows a failure runs an entire module on defaults for a week before anyone notices.
6. **Guard every divisor.** *(VWAP on a zero-volume instrument produced NaN, no exception, and a false directional signal all session.)*
7. **New signal dimensions ship at weight 0** and earn their weight from realized edge.
8. **Version header + changelog + title line on every edit, and the title must equal the newest changelog entry.**
9. **Win rate is not an edge.** Every performance report carries n, win%, avg win R, avg loss R and expectancy R together — because a 75% win rate lost $3,444 and a win-rate-only report hid it.

---

## Modes

| Mode | Holds overnight | Margin basis | Flatten |
|---|---|---|---|
| SCALP | no | day rate | cash close |
| DAY | no | day rate | cash close |
| SWING | **yes** | **initial** | structure only |
| HEDGE | **yes** | **initial** | rebalance / roll only |

The rule "day and scalp do not carry past the cash session; swing and hedge always carry" lives in exactly one function: `utils.sessions.must_be_flat`.

---

## Contract coverage

37 roots across index, energy, metals, rates, FX, agriculture and crypto — minis and micros paired, each with tick size, tick value, multiplier, listing months, expiry rule, RTH window, minimum stop in ticks, roll lead time and seed margins.

**Margin values in the registry are SEED PRIORS.** They exist so the engine can size before a broker session exists. `MarginManager.apply_broker_rates()` replaces them at every session start and logs the delta, so a drifted seed is visible rather than silent. Never quote a margin number without saying where it came from.

---

## Running the proof

```bash
bash check_versions.sh      # header parity + canaries + suite (the push gate)
python3 tests/test_foundation.py
./devtools.sh               # menu — item 1 is the tick chart
```

**83 assertions** over real dates and real contract specs. No broker, no network, no environment.

## Sizing at a glance

`devtools.sh` → **1) TICK CHART** renders the currently selected symbol at the
balance resolved *at call time*, per mode, with the day and gap-adjusted
overnight stop ladders. Three states, and the distinction is load-bearing:

```
n = lots allowed
0 = permitted, unaffordable at this balance   (moves as the account grows)
X = excluded by policy                        (will not change)
```

**Paper equity is a firm $25,000 and the broker is never consulted for it.** The
bot holds a broker session in paper too, so a broker-first resolver would let a
live net-liq leak into paper sizing the moment the account is funded — silently
changing every table a dial was calibrated against.

## Manifest

| File | v | Purpose |
|---|---|---|
| `config.py` | 0.4 | every tunable, `FT_*`; risk = 1% of equity |
| `data/contract_registry.py` | 0.1 | 37 roots, front month, roll state machine |
| `utils/sessions.py` | 0.1 | Globex clock, killzones, flatten authority |
| `risk/risk_manager.py` | 0.1 | contract sizing, R:R gate, net daily halt |
| `risk/eligibility.py` | 0.1 | X vs 0 — mode policy per root |
| `risk/capacity.py` | 0.3 | tick chart, universe matrix |
| `execution/margin_manager.py` | 0.1 | day vs overnight rates, overnight gate |
| `execution/roll_manager.py` | 0.1 | calendar-spread roll, granularity |
| `database/trade_logger.py` | 0.1 | R-native schema, mode-scoped |
| `data/series.py` | 0.1 | stdlib tape container — no pandas in the analysis layer |
| `analysis/volatility.py` | 0.1 | ATR, Bollinger (width as **percentile**), guarded VWAP |
| `analysis/trend.py` | 0.1 | EMA stacks, ADX from 5m, **renormalized** TF vote |
| `analysis/market_structure.py` | 0.1 | swings, BOS/CHoCH, FVG, order blocks, PD position |
| `analysis/liquidity.py` | 0.1 | tiered level map incl. **overnight H/L** |
| `analysis/profile.py` | 0.1 | POC, value area, migration, naked POCs |
| `analysis/orderflow.py` | 0.1 | CVD, divergence, absorption (declares approximation) |
| `analysis/regime_confluence.py` | 0.1 | **L1** — ported grammar + calibrated dials |
| `analysis/conviction_integrator.py` | 0.1 | **L2** — persistence, hysteresis, staleness |
| `analysis/signal_journal.py` | 0.1 | log-only capture, never fatal |
| `analysis/opening_range.py` | 0.1 | the ORB state machine, definitions intact |
| `strategy/base.py` | 0.1 | Signal contract — entry + stop + target or no trade |
| `strategy/day_mode.py` | 0.1 | D1 break+retest · D2 sweep · D3 continuation |
| `strategy/scalp_mode.py` | 0.1 | S1 absorption · S2 killzone continuation |
| `strategy/swing_mode.py` | 0.1 | W1 PD-array · W2 value fade |
| `strategy/hedge_mode.py` | 0.1 | H1 beta-weighted hedge |
| `risk/setup_scorer.py` | 0.1 | weighted grade; geometry gate bypasses it |
| `execution/order_confirm.py` | 0.1 | FillResult — nothing books on submission |
| `execution/entry_engine.py` | 0.1 | mark-limit entries, sized to actual fills |
| `execution/exit_engine.py` | 0.2 | the R ladder — scale, ratchet, trail, exhaustion |
| `execution/position_manager.py` | 0.1 | anti-orphan, optional-kwarg tolerant |
| `main.py` | 0.2 | the loop — manage, roll, then enter |
| `data/feed_store.py` | 0.1 | one writer, many readers, heartbeat |
| `data/market_data.py` | 0.2 | pure reader, fails loud on staleness |
| `data/futures_feed.py` | 0.1 | the single producer (+ `--sim`) |
| `execution/broker.py` | 0.1 | PaperBroker works; TastyTrade refuses |
| `notifications/alerts.py` | 0.1 | few events, never fatal |
| `status.py` | 0.1 | snapshot; every number states its source |
| `install.sh` · `setup_ec2.sh` · `bootstrap.example.sh` | 0.1 | unattended install |
| `configure.sh` | 0.1 | runtime settings; go-live is loud |
| `devtools.sh` | 0.4 | operator menu |
| `check_versions.sh` | 0.5 | the push gate |

---

## Documents

- `docs/STRATEGIES.md` — the strategy roster, each with its edge thesis and its falsification test.
- `docs/ROADMAP.md` — the epoch ladder (Epoch 0 pre-flight → Epoch 5 live), and what resets an epoch.
