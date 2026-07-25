"""
futures_trader_v1/execution/roll_manager.py — v0.1
v0.1 — 2026-07-25 — Initial build. Roll planning and execution: volume-crossover
        trigger, calendar-spread execution, per-contract operator granularity.

OPERATOR SPEC (2026-07-25)
  * Trigger: volume, inside the normal window.
  * Execution: calendar spread is fine (and is the default — one order, one
    price, no naked moment).
  * Granularity: roll one / a subset / all, from the menu.
  * Calendar spreads are also a legitimate STRATEGY, not only plumbing —
    see strategy/ (Epoch 2 build) which consumes the same spread primitives.

THE FAILURE THIS FILE IS BUILT AROUND
A legged roll has a window — between the front-month close and the back-month
open — where the position does not exist. If the second leg fails there, an
overnight swing position has silently become flat, or a hedge has silently
stopped hedging, and nothing raises. That is the futures twin of otv3 defect P
(the broken-wing roll that closed a real vertical and booked a fictional one).
So: calendar spread by default; a legged fallback that PRESERVES POSITION TRUTH
and pages loudly on a half-complete roll; and no state advances on submission —
only on a confirmed fill.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

from data.contract_registry import (CROSSOVER, FORCED, OFF_WINDOW, ROLLED,
                                    WINDOW_OPEN, ContractCycle, RollAssessment,
                                    assess_roll, get_spec)

logger = logging.getLogger(__name__)

PLAN_NONE = "no_roll_needed"
PLAN_SPREAD = "calendar_spread"
PLAN_LEGGED = "legged"
PLAN_FLAT_ONLY = "quotes_only"      # nothing open; just repoint the feed

ROLL_PENDING = "PENDING"
ROLL_HALF = "HALF_COMPLETE"
ROLL_DONE = "COMPLETE"
ROLL_FAILED = "FAILED"


@dataclass
class RollPlan:
    root: str
    kind: str
    assessment: RollAssessment
    contracts: int
    from_code: str
    to_code: str
    direction: str          # LONG | SHORT | FLAT
    rationale: str
    requires_operator: bool = False

    def describe(self) -> str:
        if self.kind == PLAN_NONE:
            return f"{self.root}: {self.assessment.reason}"
        pos = "flat" if self.direction == "FLAT" else f"{self.contracts} {self.direction}"
        return (f"{self.root}: roll {pos} {self.from_code} -> {self.to_code} "
                f"via {self.kind} ({self.rationale}; "
                f"{self.assessment.days_to_last_trade}d to last trade)")


@dataclass
class RollResult:
    status: str
    plan: RollPlan
    fill_price: Optional[float] = None
    order_id: Optional[str] = None
    message: str = ""
    at: Optional[datetime] = None


@dataclass
class RollLedger:
    """Per-root roll state, persisted so a restart cannot re-roll a position
    that has already been rolled (the idempotence lesson from otv3's working
    order-id resume)."""
    rolled: Dict[str, str] = field(default_factory=dict)     # root -> to_code
    history: List[Dict] = field(default_factory=list)

    def already_rolled(self, root: str, to_code: str) -> bool:
        return self.rolled.get(root) == to_code

    def record(self, root: str, to_code: str, result: RollResult) -> None:
        if result.status == ROLL_DONE:
            self.rolled[root] = to_code
        self.history.append({
            "root": root, "to": to_code, "status": result.status,
            "price": result.fill_price, "order_id": result.order_id,
            "at": (result.at or datetime.utcnow()).isoformat(),
            "message": result.message,
        })


class RollManager:
    """Plans and executes rolls for one box (or, from control, for a subset).

    `place_spread` / `place_single` are injected so this module is testable with
    no broker at all — the roll logic is the part that must be provably correct
    before a live account ever sees it.
    """

    def __init__(self,
                 confirm_sessions: int = 2,
                 hard_deadline_days: int = 2,
                 prefer_spread: bool = True,
                 auto: bool = True,
                 only_when_flat: bool = False,
                 ledger: Optional[RollLedger] = None,
                 place_spread: Optional[Callable] = None,
                 place_single: Optional[Callable] = None,
                 alert: Optional[Callable[[str], None]] = None):
        self.confirm_sessions = confirm_sessions
        self.hard_deadline_days = hard_deadline_days
        self.prefer_spread = prefer_spread
        self.auto = auto
        self.only_when_flat = only_when_flat
        self.ledger = ledger or RollLedger()
        self.place_spread = place_spread
        self.place_single = place_single
        self.alert = alert or (lambda m: logger.info("ALERT: %s", m))

    # ── planning ──────────────────────────────────────────────────────────────
    def plan(self,
             root: str,
             on: date,
             volume_history: Optional[List[Tuple[date, float, float]]] = None,
             open_contracts: int = 0,
             direction: str = "FLAT") -> RollPlan:
        a = assess_roll(root, on, volume_history,
                        confirm_sessions=self.confirm_sessions,
                        hard_deadline_days=self.hard_deadline_days,
                        already_rolled=self.ledger.already_rolled(
                            root, _code(root, on)))
        if not a.should_roll:
            return RollPlan(root, PLAN_NONE, a, open_contracts,
                            a.front.code, a.back.code, direction, a.reason)

        if open_contracts == 0:
            return RollPlan(root, PLAN_FLAT_ONLY, a, 0, a.front.code, a.back.code,
                            "FLAT", "flat — repoint quotes to the back month only")

        if self.only_when_flat:
            return RollPlan(root, PLAN_NONE, a, open_contracts, a.front.code,
                            a.back.code, direction,
                            "FT_ROLL_ONLY_WHEN_FLAT is set and a position is open "
                            "— operator must close or override", True)

        kind = PLAN_SPREAD if (self.prefer_spread and self.place_spread) else PLAN_LEGGED
        rationale = a.reason if a.state != FORCED else f"FORCED: {a.reason}"
        return RollPlan(root, kind, a, open_contracts, a.front.code, a.back.code,
                        direction, rationale, requires_operator=not self.auto)

    def plan_many(self, roots: List[str], on: date,
                  volumes: Optional[Dict[str, List[Tuple[date, float, float]]]] = None,
                  positions: Optional[Dict[str, Tuple[int, str]]] = None) -> List[RollPlan]:
        """Per-contract granularity: pass one root, a chosen subset, or all."""
        volumes = volumes or {}
        positions = positions or {}
        out = []
        for r in roots:
            n, d = positions.get(r, (0, "FLAT"))
            out.append(self.plan(r, on, volumes.get(r), n, d))
        return out

    # ── execution ─────────────────────────────────────────────────────────────
    def execute(self, plan: RollPlan) -> RollResult:
        now = datetime.utcnow()
        if plan.kind in (PLAN_NONE,):
            return RollResult(ROLL_DONE, plan, message="nothing to do", at=now)
        if plan.requires_operator:
            self.alert(f"ROLL WAITING FOR OPERATOR — {plan.describe()}")
            return RollResult(ROLL_PENDING, plan,
                              message="auto-roll disabled; awaiting menu action", at=now)
        if plan.kind == PLAN_FLAT_ONLY:
            self.ledger.record(plan.root, plan.to_code,
                               RollResult(ROLL_DONE, plan, message="quotes repointed", at=now))
            return RollResult(ROLL_DONE, plan, message="flat — quotes repointed", at=now)

        if plan.kind == PLAN_SPREAD and self.place_spread:
            res = self._execute_spread(plan, now)
        else:
            res = self._execute_legged(plan, now)
        self.ledger.record(plan.root, plan.to_code, res)
        return res

    def _execute_spread(self, plan: RollPlan, now: datetime) -> RollResult:
        """One order: close front / open back at a single net price. No moment
        exists in which the position is unhedged or unheld."""
        try:
            fill = self.place_spread(root=plan.root,
                                     sell_code=plan.from_code if plan.direction == "LONG" else plan.to_code,
                                     buy_code=plan.to_code if plan.direction == "LONG" else plan.from_code,
                                     contracts=plan.contracts,
                                     direction=plan.direction)
        except Exception as e:                                  # noqa: BLE001
            self.alert(f"ROLL SPREAD ERROR {plan.root}: {e}")
            return RollResult(ROLL_FAILED, plan, message=str(e), at=now)
        if not fill or not getattr(fill, "confirmed", False):
            self.alert(f"ROLL SPREAD UNFILLED {plan.root} — position unchanged, "
                       f"will retry next tick")
            return RollResult(ROLL_PENDING, plan, message="spread not filled", at=now)
        return RollResult(ROLL_DONE, plan, fill_price=getattr(fill, "fill_price", None),
                          order_id=getattr(fill, "order_id", None),
                          message="rolled as calendar spread", at=now)

    def _execute_legged(self, plan: RollPlan, now: datetime) -> RollResult:
        """Fallback. Close the front FIRST and only open the back on a confirmed
        close — the reverse order would double exposure on a partial. A failure
        between the two is reported as HALF_COMPLETE with the true state, never
        swallowed."""
        if not self.place_single:
            return RollResult(ROLL_FAILED, plan,
                              message="no order function wired", at=now)
        close_side = "SELL" if plan.direction == "LONG" else "BUY"
        open_side = "BUY" if plan.direction == "LONG" else "SELL"
        closed = self.place_single(code=plan.from_code, side=close_side,
                                   contracts=plan.contracts)
        if not closed or not getattr(closed, "confirmed", False):
            return RollResult(ROLL_PENDING, plan,
                              message="front-month close unfilled; nothing changed", at=now)
        opened = self.place_single(code=plan.to_code, side=open_side,
                                   contracts=plan.contracts)
        if not opened or not getattr(opened, "confirmed", False):
            self.alert(f"🚨 HALF-COMPLETE ROLL {plan.root}: front closed "
                       f"({plan.contracts} {close_side} {plan.from_code}) but back month "
                       f"DID NOT OPEN. Box is FLAT and believes it is not. Operator action.")
            return RollResult(ROLL_HALF, plan,
                              fill_price=getattr(closed, "fill_price", None),
                              message="front closed, back month unfilled", at=now)
        return RollResult(ROLL_DONE, plan, fill_price=getattr(opened, "fill_price", None),
                          order_id=getattr(opened, "order_id", None),
                          message="rolled legged (front closed, back opened)", at=now)


def _code(root: str, on: date) -> str:
    from data.contract_registry import front_and_back
    _, back = front_and_back(root, on)
    return back.code
