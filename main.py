"""
futures_trader_v1/main.py — v0.3
v0.3 — 2026-07-25 — BUYING-POWER GATE in the entry path, read fresh from the
        broker at order time. Because all boxes share one account, the broker's
        buying power is already fleet-aware, so this single check does the work
        a pushed fleet-margin file would have done — with nothing to go stale.
        Inert in paper; fails CLOSED if the account cannot be read; pages once
        per exhaustion episode rather than on every rejected signal.
v0.2 — 2026-07-25 — Journal payloads are NAMESPACED rather than **-merged. The
        signal dict and the score dict both carry "reason", so the merge raised
        TypeError in the entry path — on the first real signal, on a live box.
        Found by the end-to-end loop test.
v0.1 — 2026-07-25 — Initial build. The bot. One box, one symbol, one mode.

THE TICK, IN ORDER, AND THE ORDER IS LOAD-BEARING:

    read tape -> analyse -> L1 evidence -> L2 committed regime
      -> MANAGE the open position          (always first — an open position is
                                            real money; a signal is an opinion)
      -> roll check                        (maintenance, never skipped)
      -> if flat: dispatch -> validate -> score -> size -> margin -> enter
      -> journal

MANAGING BEFORE ENTERING IS NOT COSMETIC. The options engine spent a session
with unmanaged positions after a deploy missed one file, and the lesson stuck:
nothing in the entry path may ever run before the exit path has had its turn.

FAIL LOUD, DEGRADE NEVER SILENTLY. A stale feed produces no analysis and no
orders — not analysis on old bars. Every skipped tick states its reason, because
an unexplained no-trade day is indistinguishable from a broken engine.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import config as C
from analysis import liquidity as LQ
from analysis import market_structure as MS
from analysis import opening_range as OR
from analysis import orderflow as OF
from analysis import profile as PF
from analysis import trend as TR
from analysis import volatility as VOL
from analysis.conviction_integrator import ConvictionIntegrator
from analysis.regime_confluence import ConfluenceScorer
from analysis.signal_journal import SignalJournal
from data.contract_registry import front_and_back, get_spec
from data.market_data import MarketData
from database.trade_logger import TradeLogger, TradeRecord
from execution import broker as BR
from execution.entry_engine import EntryEngine
from execution.exit_engine import (FIXED, HEDGE, RUNNER, ManagedPosition)
from execution.margin_manager import AccountSnapshot, MarginManager
from execution.position_manager import PositionManager
from execution.roll_manager import RollLedger, RollManager
from notifications.alerts import Alerts
from risk import setup_scorer
from risk.eligibility import box_viable
from risk.risk_manager import RiskManager
from strategy import day_mode, hedge_mode, scalp_mode, swing_mode
from strategy.base import LONG
from utils import sessions as S

log = logging.getLogger("futuresbot")

DISPATCH = {"DAY": day_mode.dispatch, "SCALP": scalp_mode.dispatch,
            "SWING": swing_mode.dispatch, "HEDGE": hedge_mode.dispatch}
PROFILE_FOR = {"DAY": RUNNER, "SCALP": FIXED, "SWING": RUNNER, "HEDGE": HEDGE}
TIME_STOP_FOR = {"S1_ABSORPTION": 8.0, "S2_KILLZONE_CONT": 12.0}
REGIME_DEFINED = {"D3_CONTINUATION", "W2_VALUE_FADE"}


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler("bot.log")])


@dataclass
class BotState:
    running: bool = True
    session: Optional[date] = None
    orb: Optional[OR.ORBState] = None
    last_regime: str = ""
    last_conviction: float = 0.0
    ticks: int = 0
    skipped: dict = field(default_factory=dict)
    halted: bool = False

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


class Bot:
    def __init__(self):
        self.spec = get_spec(C.SYMBOL)
        self.state = BotState()
        self.md = MarketData()
        self.alerts = Alerts(C.SYMBOL, C.MODE, C.PAPER_TRADING)
        self.journal = SignalJournal(C.JOURNAL_DIR, C.SYMBOL, C.MODE)
        self.trades = TradeLogger(C.TRADES_DB, C.PAPER_TRADING,
                                  self.spec.tick_value, self.spec.tick_size)
        self.broker = BR.build(C.PAPER_TRADING, lambda: self.md.mark(C.SYMBOL),
                               self.spec.tick_size,
                               slippage_ticks=C.PAPER_SLIPPAGE_TICKS)
        self.margin = MarginManager(self.spec, C.MODE, C.MARGIN_UTILIZATION_MAX,
                                    C.MARGIN_BUFFER_MULT, C.USE_BROKER_MARGIN)
        self.risk = RiskManager(self.spec, C.MODE, C.RISK_PER_TRADE_USD,
                                C.MAX_CONTRACTS, C.DAILY_LOSS_LIMIT_USD,
                                C.min_rrr(), C.MIN_STOP_TICK_MULT,
                                C.MAX_STOP_ATR_MULT, C.GRADE_SIZE_MULTIPLIER,
                                C.COMMISSION_PER_CONTRACT_RT)
        self.entries = EntryEngine(self.spec, C.PAPER_TRADING,
                                   place=self.broker.place, poll=self.broker.poll,
                                   cancel=self.broker.cancel,
                                   slippage_ticks=C.PAPER_SLIPPAGE_TICKS)
        self.positions = PositionManager(self.spec, C.MODE, C.PAPER_TRADING,
                                         trade_logger=self.trades,
                                         journal=self.journal,
                                         place=self.broker.place,
                                         poll=self.broker.poll,
                                         cancel=self.broker.cancel,
                                         slippage_ticks=C.PAPER_SLIPPAGE_TICKS,
                                         alert=self.alerts.attention)
        self.rolls = RollManager(C.ROLL_CONFIRM_SESSIONS, C.ROLL_HARD_DEADLINE_DAYS,
                                 C.ROLL_AS_CALENDAR_SPREAD, C.ROLL_AUTO,
                                 C.ROLL_ONLY_WHEN_FLAT, RollLedger(),
                                 place_spread=self.broker.place_spread,
                                 place_single=None, alert=self.alerts.roll)
        self.l1 = ConfluenceScorer()
        self.l2 = ConvictionIntegrator()
        self._bp_alerted = False
        self._integ_path = os.path.join("data", "integrator_state.json")
        self.l2.load(self._integ_path)

    # ── boot ─────────────────────────────────────────────────────────────────
    def boot(self) -> bool:
        problems = C.validate()
        if problems:
            for p in problems:
                log.error("CONFIG: %s", p)
            self.alerts.attention("config invalid: " + "; ".join(problems))
            return False

        equity = (C.PAPER_EQUITY_DEFAULT if C.PAPER_TRADING
                  else C.ACCOUNT_EQUITY_DEFAULT)
        ok, why = box_viable(C.SYMBOL, equity)
        if not ok:
            log.error("BOX NOT VIABLE: %s", why)
            self.alerts.attention(f"box not viable — {why}")
            return False

        if not C.PAPER_TRADING:
            try:
                acct = self.broker.account()
                self.margin.apply_account(AccountSnapshot(
                    net_liq=acct.get("net_liq", 0.0),
                    buying_power=acct.get("buying_power", 0.0),
                    as_of=S.now_et(), source="broker"))
            except NotImplementedError as e:
                log.error("LIVE mode with an unverified broker adapter: %s", e)
                self.alerts.attention("live mode blocked — broker adapter unverified")
                return False
        else:
            self.margin.apply_account(AccountSnapshot(
                net_liq=C.PAPER_EQUITY_DEFAULT, as_of=S.now_et(), source="paper"))

        self._recover_position()
        front, _ = front_and_back(C.SYMBOL, S.session_date())
        self.alerts.startup(f"{front.code} · {why} · equity "
                            f"${self.margin.account.net_liq:,.0f}")
        log.info("booted %s on %s", C.BOX_NAME, front.code)
        return True

    def _recover_position(self) -> None:
        """Adopt whatever the database says is open. A restart must never leave
        a real position unmanaged — the row is the plan, and the plan survives
        the process."""
        for row in self.trades.get_open_trades():
            pos = ManagedPosition(
                trade_id=row["trade_id"], strategy=row["strategy"],
                direction=row["direction"], entry=row["entry_price"],
                stop=row["trail_stop"] or row["stop_price"],
                initial_stop=row["stop_price"], target=row["target_price"] or 0.0,
                contracts_open=row["contracts_open"] or row["contracts"],
                contracts_initial=row["contracts"],
                profile=PROFILE_FOR.get(C.MODE, RUNNER),
                regime_at_entry=row["regime"] or "",
                regime_defined=(row["strategy"] in REGIME_DEFINED),
                scaled=bool(row["scaled_out"]),
                trail_stop=row["trail_stop"])
            self.positions.adopt(pos)
            log.warning("adopted open position %s (%s %s x%d)", pos.trade_id,
                        pos.strategy, pos.direction, pos.contracts_open)
            self.alerts.attention(f"adopted open position on restart: "
                                  f"{pos.strategy} {pos.direction} x{pos.contracts_open}")
            break

    # ── one tick ─────────────────────────────────────────────────────────────
    def tick(self) -> None:
        self.state.ticks += 1
        now = S.now_et()
        sess = S.session_date(now)
        if self.state.session != sess:
            self.state.session = sess
            self.state.orb = None
            self.risk.session_losses = 0

        if not S.market_is_open(now):
            self.state.skip("market closed")
            return

        healthy, why = self.md.healthy()
        if not healthy:
            self.state.skip(f"feed: {why}")
            log.warning("skipping tick — %s", why)
            return

        tape = self.md.tape(C.SYMBOL)
        c1, c5 = tape.get("1m"), tape.get("5m")
        price = self.md.mark(C.SYMBOL)
        if price is None or c1 is None or c5 is None:
            self.state.skip("incomplete tape")
            return

        ctx = self._analyse(tape, c1, c5, price, now)

        # 1. MANAGE FIRST — always.
        must_flat = S.must_be_flat(C.MODE, self.spec, now, C.FLATTEN_LEAD_MIN)
        res = self.positions.manage(price, now=now, regime=ctx["regime"],
                                    structure=ctx["structure"], vol=ctx["vol"],
                                    flow=ctx["flow"], profile=ctx["profile"],
                                    must_flatten=must_flat)
        if res and res.executed and res.decision.closes:
            self.alerts.exit(res.decision.reason, res.decision.contracts,
                             price, res.decision.r_at_decision, res.realized)
            if res.realized < 0:
                self.risk.register_loss()

        # 2. ROLL — maintenance, and it does not wait for a flat book.
        self._roll_check(sess)

        # 3. ENTER — only if flat and allowed.
        if not self.positions.flat:
            return
        allowed, areason = S.entries_allowed(C.MODE, self.spec, now,
                                             C.ENTRY_CUTOFF_MIN,
                                             C.ENABLED_SESSIONS)
        if not allowed:
            self.state.skip(areason)
            return

        pnl = self.trades.realized_pnl_today(sess)
        halted, hwhy = self.risk.is_halted(pnl, sess)
        if halted:
            if not self.state.halted:
                self.state.halted = True
                self.alerts.halt(hwhy)
            self.state.skip("daily loss halt")
            return

        self._try_entry(ctx, price, now, sess, pnl)

    # ── analysis ─────────────────────────────────────────────────────────────
    def _analyse(self, tape, c1, c5, price, now) -> dict:
        vol = VOL.analyze(c5, C.ATR_PERIOD, C.BB_PERIOD, C.BB_STD)
        trend = TR.analyze(tape)
        structure = MS.analyze(c1, self.spec.tick_size)
        structure_htf = MS.analyze(tape.get("1h") or c5, self.spec.tick_size)
        bar_trades = self.md.bar_trades(C.SYMBOL, c1)
        flow = OF.build(c1, bar_trades, C.CVD_LOOKBACK_BARS)
        prof = PF.analyze(c5, self.spec.tick_size)
        liq = LQ.build(c1, self.spec.tick_size, C.LEVEL_TIERS,
                       value_area=((prof.today.vah, prof.today.val)
                                   if prof.warm and prof.today.vah else None),
                       naked_pocs=prof.naked_pocs)

        ev = self.l1.score(c1.close, vol, trend, structure, flow, prof)
        st = self.l2.update(time.time(), ev.vector())
        self.l2.save(self._integ_path)
        self.state.last_regime = st.regime or ""
        self.state.last_conviction = st.conviction

        if self.state.orb is None and C.MODE in ("DAY", "SCALP"):
            self.state.orb = OR.build_range(c1, self.spec, 5, self.state.session)
        if self.state.orb and self.state.orb.established:
            self.state.orb = OR.update(self.state.orb, c1, self.spec,
                                       session=self.state.session)

        self.journal.regime(l1=ev.vector(),
                            l2={"regime": st.regime, "conviction": st.conviction,
                                "stale": st.stale, "trigger": st.trigger})

        kz = S.active_killzones(now)
        return {"spec": self.spec, "price": price, "now": now, "tape": tape,
                "c1": c1, "c5": c5, "vol": vol, "trend": trend,
                "structure": structure, "structure_htf": structure_htf,
                "liquidity": liq, "profile": prof, "flow": flow,
                "orb": self.state.orb,
                "regime": st.regime if not st.stale else "",
                "conviction": st.conviction,
                "session_phase": S.session_phase(now),
                "killzone": next((k for k in kz if k in C.KILLZONES_ENABLED), ""),
                "hedge_contracts_open": (self.positions.position.contracts_open
                                         if self.positions.position else 0)}

    # ── entry ────────────────────────────────────────────────────────────────
    def _try_entry(self, ctx, price, now, sess, pnl) -> None:
        sig = DISPATCH[C.MODE](ctx)
        if sig is None:
            return

        ok, why = sig.validate(self.spec, self.spec.min_stop_ticks * C.MIN_STOP_TICK_MULT)
        if not ok:
            self.journal.disposition("invalid_signal", reason=why, **sig.journal())
            log.info("signal rejected: %s", why)
            return

        gated = getattr(DISPATCH[C.MODE], "geometry_gated", False) or \
            sig.strategy.startswith("D1")
        sc = setup_scorer.score(sig, ctx, geometry_gated=gated)
        # NAMESPACED, not merged: both dicts carry a "reason" key and **-merging
        # them raises TypeError. It would have thrown on the FIRST real signal,
        # inside the entry path, on a live box.
        self.journal.scored(signal=sig.journal(), score=sc.journal())
        if not sc.fires:
            self.journal.disposition("score_rejected", reason=sc.reason)
            return

        cap = self.margin.capacity()
        sized = self.risk.size(sig.entry, sig.stop, sig.target, sc.grade,
                               atr=ctx["vol"].atr, margin_capacity=cap.max_contracts,
                               realized_pnl_today=pnl, session=sess)
        if not sized.approved:
            self.journal.disposition("sizing_rejected", reason=sized.reason,
                                     detail=sized.detail)
            log.info("sizing rejected: %s — %s", sized.reason, sized.detail)
            return

        # THE FLEET-EXPOSURE GATE. Read the broker's buying power NOW, not from
        # a cached snapshot: it already has every other box's margin netted out
        # of it, so this one call is the whole fleet check. Skipped in paper.
        bp = self._buying_power_gate(sized.contracts)
        if not bp.allowed:
            self.journal.disposition("buying_power_rejected", reason=bp.reason,
                                     required=bp.required, available=bp.available)
            log.warning("BUYING POWER: %s", bp.reason)
            if not self._bp_alerted:
                self._bp_alerted = True
                self.alerts.attention(f"buying power exhausted — {bp.reason}")
            return
        self._bp_alerted = False

        if C.MODE in S.OVERNIGHT_MODES:
            gate = self.margin.overnight_gate(sized.contracts)
            if not gate.allowed:
                self.journal.disposition("overnight_margin_rejected", reason=gate.reason)
                log.info("overnight margin refusal: %s", gate.reason)
                return

        plan_targets = self.risk.scale_plan(sized.contracts, sig.entry, sig.stop,
                                            C.SCALE_OUT_AT_R, C.SCALE_OUT_FRACTION)
        res = self.entries.enter(sig, sized.contracts, sc.grade,
                                 sized.risk_dollars, price, plan_targets)
        if not res.filled:
            self.journal.disposition("entry_unfilled", reason=res.reason)
            return

        fill_px = res.fill.fill_price
        qty = res.fill.filled_qty
        rec = TradeRecord(
            trade_id=res.plan.trade_id, root=C.SYMBOL,
            contract_code=front_and_back(C.SYMBOL, sess)[0].code,
            mode=C.MODE, strategy=sig.strategy, direction=sig.direction,
            contracts=qty, entry_price=fill_px, stop_price=sig.stop,
            target_price=sig.target, stop_ticks=sized.stop_ticks,
            risk_dollars=sized.risk_dollars, planned_rrr=sized.rrr,
            grade=sc.grade, setup_score=sc.total,
            entry_time=now.isoformat(), session_date=sess.isoformat(),
            session_phase=sig.session_phase, killzone=sig.killzone,
            regime=sig.regime, regime_conviction=sig.regime_conviction,
            adx_at_entry=ctx["trend"].adx or 0.0, atr_at_entry=ctx["vol"].atr or 0.0,
            level_tier=sig.level_tier, level_name=sig.level_name,
            cvd_at_entry=sig.cvd, delta_divergence=sig.delta_divergence,
            pd_position=sig.pd_position or 0.0, notes=sig.reason,
            paper_trade=1 if C.PAPER_TRADING else 0,
            order_id=res.fill.order_id or "")
        self.trades.open_trade(rec, confirmed_fill=True)

        self.positions.adopt(ManagedPosition(
            trade_id=rec.trade_id, strategy=sig.strategy, direction=sig.direction,
            entry=fill_px, stop=sig.stop, initial_stop=sig.stop,
            target=sig.target, contracts_open=qty, contracts_initial=qty,
            profile=PROFILE_FOR.get(C.MODE, RUNNER),
            regime_at_entry=sig.regime,
            regime_defined=(sig.strategy in REGIME_DEFINED),
            opened_at=now,
            time_stop_min=TIME_STOP_FOR.get(sig.strategy)))
        self.journal.disposition("fired", trade_id=rec.trade_id,
                                 contracts=qty, fill=fill_px)
        self.alerts.entry(sig, qty, sc.grade, fill_px)
        log.info("ENTERED %s %s x%d @ %.4f (%s)", sig.strategy, sig.direction,
                 qty, fill_px, sized.detail)

    def _buying_power_gate(self, contracts: int):
        """Fresh broker read at order time. One call per ENTRY ATTEMPT, not per
        tick — the only moment the number actually has to be right."""
        from execution.margin_manager import BuyingPowerDecision
        if C.PAPER_TRADING or not C.BP_GATE_ENABLED:
            return BuyingPowerDecision(True, False,
                                       reason="paper — buying power not checked")
        try:
            acct = self.broker.account()
        except Exception as e:                               # noqa: BLE001
            # Fail CLOSED. Not knowing the balance is not permission to use it.
            return BuyingPowerDecision(False, True, reason=
                                       f"could not read the account: {e}")
        self.margin.apply_account(AccountSnapshot(
            net_liq=acct.get("net_liq", 0.0),
            buying_power=acct.get("buying_power", 0.0),
            maintenance_used=acct.get("maintenance_used", 0.0),
            as_of=S.now_et(), source="broker"))
        return self.margin.buying_power_gate(
            contracts, acct, paper=False,
            min_headroom_pct=C.BP_MIN_HEADROOM_PCT)

    # ── roll ─────────────────────────────────────────────────────────────────
    def _roll_check(self, sess) -> None:
        held = (self.positions.position.contracts_open
                if self.positions.position else 0)
        direction = (self.positions.position.direction
                     if self.positions.position else "FLAT")
        plan = self.rolls.plan(C.SYMBOL, sess, volume_history=None,
                               open_contracts=held, direction=direction)
        if plan.kind == "no_roll_needed":
            return
        result = self.rolls.execute(plan)
        self.trades.record_roll(f"{C.SYMBOL}-{plan.to_code}-{sess}", C.SYMBOL,
                                plan.from_code, plan.to_code, held, plan.kind,
                                result.status, result.fill_price, result.message)
        self.alerts.roll(f"{plan.describe()} -> {result.status}")

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> int:
        if not self.boot():
            return 1
        while self.state.running:
            try:
                self.tick()
            except Exception as e:                           # noqa: BLE001
                log.exception("tick failed: %s", e)
                self.alerts.attention(f"tick error: {e}")
            time.sleep(C.POLL_INTERVAL_SECONDS)
        self.alerts.shutdown(f"{self.state.ticks} ticks")
        return 0

    def stop(self, *_):
        self.state.running = False


def main() -> int:
    _setup_logging()
    bot = Bot()
    signal.signal(signal.SIGTERM, bot.stop)
    signal.signal(signal.SIGINT, bot.stop)
    return bot.run()


if __name__ == "__main__":
    raise SystemExit(main())
