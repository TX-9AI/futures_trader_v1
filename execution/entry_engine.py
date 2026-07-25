"""
futures_trader_v1/execution/entry_engine.py — v0.1
v0.1 — 2026-07-25 — Initial build. Mark-limit entries, fill-confirmed, with
        the scale-out plan attached at entry.

EXECUTION POLICY, PORTED: NEVER CROSS THE SPREAD TO GET IN.
An entry posts a limit AT THE MARK and re-prices to the fresh mark each tick.
An entry that never fills costs nothing — the strategy re-signals next tick.
The options project measured this: a fixed buffer past the mark on a thin
instrument was a larger cost than the edge being captured.

The one exception is a FORCED exit, which lives in exit_engine and crosses
without hesitation, because an unmanaged position at the bell is not a price
problem, it is a risk problem.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Tuple

import config as C
from execution.order_confirm import FillResult, confirm_fill, paper_fill
from strategy.base import LONG, SHORT, Signal

logger = logging.getLogger(__name__)


@dataclass
class EntryPlan:
    signal: Signal
    contracts: int
    limit_price: float
    scale_targets: List[Tuple[int, float, str]]
    grade: str
    risk_dollars: float
    trade_id: str


@dataclass
class EntryResult:
    filled: bool
    plan: Optional[EntryPlan] = None
    fill: Optional[FillResult] = None
    reason: str = ""


def limit_at_mark(mark: float, spec, direction: str,
                  max_ticks_through: int = 0) -> float:
    """Post at the mark, snapped to a tick. `max_ticks_through` > 0 permits a
    deliberate lean into the spread; the default of 0 never crosses."""
    px = mark + (spec.tick_size * max_ticks_through *
                 (1 if direction == LONG else -1))
    return spec.round_to_tick(px)


class EntryEngine:
    def __init__(self, spec, paper: bool = True,
                 place: Optional[Callable] = None,
                 poll: Optional[Callable] = None,
                 cancel: Optional[Callable] = None,
                 slippage_ticks: float = 1.0):
        self.spec = spec
        self.paper = paper
        self.place = place
        self.poll = poll
        self.cancel = cancel
        self.slippage_ticks = slippage_ticks

    def enter(self, signal: Signal, contracts: int, grade: str,
              risk_dollars: float, mark: float,
              scale_targets: Optional[List[Tuple[int, float, str]]] = None
              ) -> EntryResult:
        if contracts < 1:
            return EntryResult(False, reason="zero contracts")
        limit = limit_at_mark(mark, self.spec, signal.direction,
                              C.ENTRY_LIMIT_MAX_TICKS_THROUGH)
        plan = EntryPlan(signal, contracts, limit, scale_targets or [], grade,
                         risk_dollars, uuid.uuid4().hex[:12])

        if self.paper:
            fill = paper_fill(limit, contracts, signal.direction,
                              self.spec.tick_size, self.slippage_ticks)
            return EntryResult(True, plan, fill, "paper fill at mark + slippage")

        if not (self.place and self.poll):
            return EntryResult(False, plan, reason="live entry with no broker wired")

        side = "BUY" if signal.direction == LONG else "SELL"
        fill = confirm_fill(
            place=lambda: self.place(side=side, contracts=contracts,
                                     limit=limit, tif="DAY"),
            poll=self.poll, cancel=self.cancel,
            deadline_s=C.LIVE_FILL_DEADLINE_SECONDS,
            poll_s=C.LIVE_FILL_POLL_SECONDS)

        if not fill.usable:
            # Nothing is recorded. An unfilled entry is not a position, and the
            # strategy will re-signal next tick if the setup is still there.
            return EntryResult(False, plan, fill,
                               fill.message or "entry not confirmed")
        if fill.filled_qty < contracts:
            # Size to what actually filled, never to what was requested.
            plan.contracts = fill.filled_qty
            plan.scale_targets = _rescale(plan.scale_targets, fill.filled_qty,
                                          contracts)
        return EntryResult(True, plan, fill, "fill confirmed")


def _rescale(targets: List[Tuple[int, float, str]], filled: int,
             requested: int) -> List[Tuple[int, float, str]]:
    if not targets or requested <= 0 or filled >= requested:
        return targets
    out = []
    for qty, px, label in targets:
        q = max(1, int(round(qty * filled / requested)))
        if q < filled:
            out.append((q, px, label))
    return out
