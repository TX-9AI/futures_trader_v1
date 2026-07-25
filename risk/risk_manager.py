"""
futures_trader_v1/risk/risk_manager.py — v0.1
v0.1 — 2026-07-25 — Initial build. Contract sizing, the R:R gate, the net
        daily-loss halt, and the reassess-after-loss discipline.

THE SIZING CONTRACT
-------------------
    contracts = floor( risk_budget / (stop_ticks * tick_value) )
    clamped to MAX_CONTRACTS, and to the margin manager's capacity,
    and refused entirely if the answer is < 1.

A stop that cannot be afforded at ONE contract is never widened to make the
trade fit. That is the single most expensive habit in retail futures and this
function is where it is made structurally impossible.

WHY MIN_RRR IS HERE AND WHY IT GATES
------------------------------------
options_trader_v3 carried MIN_RRR as an unreferenced constant for the project's
entire life (defect F). Meanwhile the sweep book ran a 75% win rate to
−$3,444 net across 99 trades: entries were good, the payoff was upside-down
(MFE ~+12% on losers before a −43% stop; winners booked ~+25% off a +60% peak).
No entry filter fixes that — only a rule that refuses a trade whose target does
not pay for its stop, plus scale-outs that bank the move that actually happens.
Both live here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Tuple

from data.contract_registry import ContractSpec

logger = logging.getLogger(__name__)

REJECT_NO_STOP = "no_structural_stop"
REJECT_STOP_TOO_TIGHT = "stop_inside_noise"
REJECT_STOP_TOO_WIDE = "stop_exceeds_atr_ceiling"
REJECT_RRR = "reward_does_not_pay_for_risk"
REJECT_BUDGET = "cannot_afford_one_contract"
REJECT_MARGIN = "margin_capacity"
REJECT_HALTED = "daily_loss_halt"


@dataclass
class SizingResult:
    approved: bool
    contracts: int
    stop_ticks: float
    risk_dollars: float
    reward_dollars: float
    rrr: float
    grade: str
    reason: str
    detail: str = ""

    @property
    def r_unit(self) -> float:
        """One R in dollars for the sized position — the denominator of every
        performance statistic this system will ever compute."""
        return self.risk_dollars


class RiskManager:
    def __init__(self,
                 spec: ContractSpec,
                 mode: str,
                 risk_per_trade: float,
                 max_contracts: int,
                 daily_loss_limit: float,
                 min_rrr: float,
                 min_stop_tick_mult: float = 1.0,
                 max_stop_atr_mult: float = 2.5,
                 grade_multipliers: Optional[dict] = None,
                 commission_rt: float = 2.50):
        self.spec = spec
        self.mode = mode.upper()
        self.risk_per_trade = risk_per_trade
        self.max_contracts = max_contracts
        self.daily_loss_limit = daily_loss_limit
        self.min_rrr = min_rrr
        self.min_stop_tick_mult = min_stop_tick_mult
        self.max_stop_atr_mult = max_stop_atr_mult
        self.grade_multipliers = grade_multipliers or {"A": 1.5, "B": 1.0}
        self.commission_rt = commission_rt
        self._halted_session: Optional[str] = None
        self._reassess_pending = False
        self.session_losses = 0

    # ── the halt ──────────────────────────────────────────────────────────────
    def is_halted(self, realized_pnl_today: float,
                  session: Optional[date] = None) -> Tuple[bool, str]:
        """NET semantics, identical to otv3 (verified there since risk_manager
        v1.4): wins offset losses. A green day keeps trading no matter how many
        individual losses stack up; only a genuinely red day halts. The halt
        latches for the session once breached, and it stops NEW ENTRIES only —
        open positions are always managed to their exits."""
        key = (session or date.today()).isoformat()
        if self._halted_session == key:
            return True, f"halted earlier this session (net ${realized_pnl_today:,.2f})"
        if realized_pnl_today <= -abs(self.daily_loss_limit):
            self._halted_session = key
            logger.warning("DAILY LOSS HALT: net %.2f <= -%.2f",
                           realized_pnl_today, self.daily_loss_limit)
            return True, (f"net realized ${realized_pnl_today:,.2f} breached "
                          f"${-abs(self.daily_loss_limit):,.2f}")
        return False, ""

    def register_loss(self) -> None:
        """A loss is fresh information about whether the read still holds. The
        engine must re-derive its regime before the next entry — ported from
        otv3, where it was one of the few gates that demonstrably prevented
        revenge-sequencing inside a hostile session."""
        self.session_losses += 1
        self._reassess_pending = True

    def clear_reassess(self) -> None:
        self._reassess_pending = False

    @property
    def reassess_pending(self) -> bool:
        return self._reassess_pending

    # ── sizing ────────────────────────────────────────────────────────────────
    def size(self,
             entry: float,
             stop: float,
             target: Optional[float],
             grade: str = "B",
             atr: Optional[float] = None,
             margin_capacity: Optional[int] = None,
             realized_pnl_today: float = 0.0,
             session: Optional[date] = None) -> SizingResult:
        spec = self.spec

        halted, why = self.is_halted(realized_pnl_today, session)
        if halted:
            return SizingResult(False, 0, 0, 0, 0, 0, grade, REJECT_HALTED, why)

        if stop is None or entry is None or stop == entry:
            return SizingResult(False, 0, 0, 0, 0, 0, grade, REJECT_NO_STOP,
                                "a futures entry without a structural stop is not a trade")

        stop_ticks = spec.ticks(entry - stop)
        floor_ticks = spec.min_stop_ticks * self.min_stop_tick_mult
        if stop_ticks < floor_ticks:
            return SizingResult(False, 0, stop_ticks, 0, 0, 0, grade,
                                REJECT_STOP_TOO_TIGHT,
                                f"stop {stop_ticks:.1f} ticks < floor {floor_ticks:.1f} "
                                f"— inside the spread/noise, the R is fiction")

        if atr and atr > 0:
            atr_ticks = spec.ticks(atr)
            if stop_ticks > atr_ticks * self.max_stop_atr_mult:
                return SizingResult(False, 0, stop_ticks, 0, 0, 0, grade,
                                    REJECT_STOP_TOO_WIDE,
                                    f"stop {stop_ticks:.1f} ticks > "
                                    f"{self.max_stop_atr_mult:.1f}x ATR "
                                    f"({atr_ticks:.1f} ticks) — the setup is not where "
                                    f"we think it is")

        risk_per_contract = stop_ticks * spec.tick_value + self.commission_rt

        rrr = 0.0
        reward_per_contract = 0.0
        if target is not None:
            reward_ticks = spec.ticks(target - entry)
            reward_per_contract = reward_ticks * spec.tick_value - self.commission_rt
            rrr = reward_per_contract / risk_per_contract if risk_per_contract > 0 else 0.0
            if self.min_rrr > 0 and rrr < self.min_rrr:
                return SizingResult(False, 0, stop_ticks, risk_per_contract,
                                    reward_per_contract, rrr, grade, REJECT_RRR,
                                    f"R:R {rrr:.2f} below the {self.min_rrr:.2f} floor "
                                    f"for {self.mode} — a good entry with a bad payoff "
                                    f"is how a 75%-win book loses money")

        budget = self.risk_per_trade * self.grade_multipliers.get(grade, 1.0)
        n = int(math.floor(budget / risk_per_contract)) if risk_per_contract > 0 else 0
        n = min(n, self.max_contracts)
        if margin_capacity is not None:
            n = min(n, margin_capacity)
            if n < 1:
                return SizingResult(False, 0, stop_ticks, risk_per_contract,
                                    reward_per_contract, rrr, grade, REJECT_MARGIN,
                                    "margin capacity is below one contract")
        if n < 1:
            return SizingResult(False, 0, stop_ticks, risk_per_contract,
                                reward_per_contract, rrr, grade, REJECT_BUDGET,
                                f"one contract risks ${risk_per_contract:,.2f} vs "
                                f"budget ${budget:,.2f} — the stop is never widened "
                                f"to make a trade fit")

        return SizingResult(True, n, stop_ticks, risk_per_contract * n,
                            reward_per_contract * n, rrr, grade, "approved",
                            f"{n} contract(s), {stop_ticks:.1f}-tick stop, "
                            f"${risk_per_contract * n:,.2f} at risk, R:R {rrr:.2f}")

    # ── scale-out plan ────────────────────────────────────────────────────────
    def scale_plan(self, contracts: int, entry: float, stop: float,
                   scale_at_r: float = 1.0,
                   fraction: float = 0.5) -> List[Tuple[int, float, str]]:
        """Return [(contracts_to_exit, price, label)].

        With one contract there is nothing to scale — the plan degrades to a
        single structural exit, and the runner logic carries the trade. That is
        the honest behaviour for a micro box and it is stated rather than
        silently producing a zero-quantity order.
        """
        if contracts < 2 or fraction <= 0:
            return []
        r = entry - stop
        first = max(1, int(round(contracts * fraction)))
        first = min(first, contracts - 1)
        return [(first, entry + r * scale_at_r, f"scale_{scale_at_r:g}R")]
