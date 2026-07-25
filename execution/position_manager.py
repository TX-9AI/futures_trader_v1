"""
futures_trader_v1/execution/position_manager.py — v0.1
v0.1 — 2026-07-25 — Initial build. Owns the open position, applies exit
        decisions, books only confirmed fills.

TWO INHERITED INVARIANTS, BOTH EXPENSIVE TO LEARN

  ANTI-ORPHAN. An unconfirmed close leaves the row OPEN and the retry loop keeps
  working it. Booking a close that did not happen produces a flat database and a
  live position — the worst state this system can be in, and the one that took
  two audits to eliminate in the options engine.

  OPTIONAL KWARGS WITH DEFAULTS. Every context object threaded into
  `manage()` is optional. On 2026-07-16 a ten-file options deploy missed
  position_manager.py; boxes with open positions crash-looped on a signature
  mismatch and went unmanaged until a fleet-wide reset. A manager that raises is
  worse than one that degrades: missing context costs precision, a missing
  argument costs the position.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

import config as C
from execution.exit_engine import (ADJUST_STOP, CLOSE_ALL, CLOSE_PARTIAL, HOLD,
                                   MARKET, ExitDecision, ExitEngine,
                                   ManagedPosition)
from execution.order_confirm import FillResult, confirm_fill, paper_fill
from strategy.base import LONG

logger = logging.getLogger(__name__)


@dataclass
class ManageResult:
    decision: ExitDecision
    executed: bool = False
    fill: Optional[FillResult] = None
    closed: bool = False
    realized: float = 0.0
    message: str = ""


class PositionManager:
    def __init__(self, spec, mode: str, paper: bool = True,
                 trade_logger=None, journal=None,
                 place: Optional[Callable] = None,
                 poll: Optional[Callable] = None,
                 cancel: Optional[Callable] = None,
                 slippage_ticks: float = 1.0,
                 alert: Optional[Callable[[str], None]] = None):
        self.spec = spec
        self.mode = mode.upper()
        self.paper = paper
        self.exits = ExitEngine(spec, mode)
        self.trade_logger = trade_logger
        self.journal = journal
        self.place = place
        self.poll = poll
        self.cancel = cancel
        self.slippage_ticks = slippage_ticks
        self.alert = alert or (lambda m: logger.warning(m))
        self.position: Optional[ManagedPosition] = None
        self.unconfirmed_closes = 0

    # ── lifecycle ────────────────────────────────────────────────────────────
    def adopt(self, pos: ManagedPosition) -> None:
        self.position = pos

    @property
    def flat(self) -> bool:
        return self.position is None or self.position.contracts_open <= 0

    def manage(self, price: float,
               now: Optional[datetime] = None,
               regime: Optional[str] = None,
               structure=None, vol=None, flow=None, profile=None,
               must_flatten: bool = False) -> Optional[ManageResult]:
        if self.flat:
            return None
        pos = self.position

        if self.trade_logger:
            try:
                self.trade_logger.update_excursion(pos.trade_id, price)
            except Exception:                                # noqa: BLE001
                pass                                         # telemetry is never fatal

        d = self.exits.evaluate(pos, price, now=now, regime=regime,
                                structure=structure, vol=vol, flow=flow,
                                profile=profile, must_flatten=must_flatten)

        if d.action == HOLD:
            return ManageResult(d, message=d.reason)

        if d.action == ADJUST_STOP:
            if d.new_stop is not None:
                if "breakeven" in d.reason:
                    pos.stop = d.new_stop
                    pos.breakeven_set = True
                else:
                    pos.trail_stop = d.new_stop
                    pos.trail_armed = True
                if self.trade_logger:
                    try:
                        self.trade_logger.update_fields(
                            pos.trade_id, trail_stop=pos.trail_stop)
                    except Exception:                        # noqa: BLE001
                        pass
            return ManageResult(d, executed=True, message=d.reason)

        return self._close(pos, d, price)

    # ── closing ──────────────────────────────────────────────────────────────
    def _close(self, pos: ManagedPosition, d: ExitDecision,
               price: float) -> ManageResult:
        qty = min(max(1, d.contracts), pos.contracts_open)
        side = "SELL" if pos.direction == LONG else "BUY"
        close_dir = "SHORT" if pos.direction == LONG else "LONG"

        if self.paper:
            fill = paper_fill(price, qty, close_dir, self.spec.tick_size,
                              self.slippage_ticks)
        elif self.place and self.poll:
            mode = d.order_mode
            limit = None if mode == MARKET else self.spec.round_to_tick(price)
            fill = confirm_fill(
                place=lambda: self.place(side=side, contracts=qty, limit=limit,
                                         tif="DAY"),
                poll=self.poll, cancel=self.cancel,
                deadline_s=C.LIVE_FILL_DEADLINE_SECONDS,
                poll_s=C.LIVE_FILL_POLL_SECONDS)
        else:
            fill = FillResult(False, message="live close with no broker wired")

        if not fill.usable:
            # ANTI-ORPHAN: the row stays open, the retry loop keeps working it.
            self.unconfirmed_closes += 1
            if self.unconfirmed_closes in (1, 5, 20):
                self.alert(f"UNCONFIRMED CLOSE x{self.unconfirmed_closes} "
                           f"{pos.trade_id} ({d.reason}) — position still OPEN")
            return ManageResult(d, executed=False, fill=fill,
                                message=f"close not confirmed: {fill.message}")

        self.unconfirmed_closes = 0
        filled = min(fill.filled_qty, qty)
        realized = 0.0
        if self.trade_logger:
            try:
                realized = self.trade_logger.close_trade(
                    pos.trade_id, fill.fill_price, d.reason,
                    contracts_closed=filled,
                    commission=C.COMMISSION_PER_CONTRACT_RT * filled,
                    confirmed_fill=True) or 0.0
            except Exception as e:                           # noqa: BLE001
                logger.error("trade log close failed: %s", e)

        pos.contracts_open -= filled
        if d.action == CLOSE_PARTIAL:
            pos.scaled = True
            # A scaled position rides free: the banked piece has paid for the
            # risk, so the remainder's stop goes to entry in the same pass
            # rather than waiting for the ratchet's own tick.
            if not pos.breakeven_set:
                pos.stop = pos.entry
                pos.breakeven_set = True

        closed = pos.contracts_open <= 0
        if closed:
            self.position = None
        if self.journal:
            try:
                self.journal.disposition("exit", trade_id=pos.trade_id,
                                         reason=d.reason, r=d.r_at_decision,
                                         contracts=filled, price=fill.fill_price,
                                         closed=closed)
            except Exception:                                # noqa: BLE001
                pass
        return ManageResult(d, executed=True, fill=fill, closed=closed,
                            realized=realized, message=d.reason)
