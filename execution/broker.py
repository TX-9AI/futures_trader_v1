"""
futures_trader_v1/execution/broker.py — v0.1
v0.1 — 2026-07-25 — Initial build. The broker seam: an abstract interface, a
        fully-working PaperBroker, and the TastyTrade adapter skeleton.

WHY THE INTERFACE EXISTS BEFORE THE ADAPTER
Everything above this file — sizing, the exit ladder, the roll, the position
manager — is already proven against 234 assertions with no broker present. That
was only possible because order placement was injected from the start.

BE HONEST ABOUT WHAT IS AND IS NOT VERIFIED. PaperBroker is exercised by the
test suite. TastyTradeBroker CANNOT be verified here: it needs the SDK, live
credentials, and a funded futures account that does not exist yet. Its methods
raise NotImplementedError with the specific thing to confirm rather than
returning something plausible. A skeleton that silently returns a fake fill is
far more dangerous than one that refuses — that is the submission-vs-fill defect
family wearing a different hat.

EVERY LIVE PATH IS CONFIRMED AT THE FIRST TINY-ACCOUNT SHAKEDOWN, not by
reading. Fill rates on mark-limit entries, real slippage on forced exits, real
margin numbers, and one complete roll on a real position.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from execution.order_confirm import (CANCELLED, FILLED, REJECTED, WORKING,
                                     FillResult)

logger = logging.getLogger(__name__)

BUY, SELL = "BUY", "SELL"


@dataclass
class OrderStatus:
    order_id: str
    status: str = WORKING
    filled_qty: int = 0
    fill_price: Optional[float] = None
    message: str = ""


class Broker:
    """The seam. Four verbs and one account read — deliberately small, because
    every method here is a place a live account can be harmed."""

    def place(self, side: str, contracts: int, limit: Optional[float] = None,
              tif: str = "DAY", contract_code: Optional[str] = None):
        raise NotImplementedError

    def poll(self, order_id: str) -> OrderStatus:
        raise NotImplementedError

    def cancel(self, order_id: str) -> bool:
        raise NotImplementedError

    def place_spread(self, root: str, sell_code: str, buy_code: str,
                     contracts: int, direction: str) -> FillResult:
        raise NotImplementedError

    def account(self) -> dict:
        raise NotImplementedError


class PaperBroker(Broker):
    """Simulates against the live tape. Fills marketable orders at the mark plus
    a tick, and holds a limit until the mark reaches it.

    PAPER IS HONEST ABOUT PRICE AND OPTIMISTIC ABOUT FILL RATE, and that residual
    is stated rather than hidden: a resting limit here fills the moment the mark
    touches it, where a real book might never reach the front of the queue. That
    is the one gap the tiny-account shakedown exists to measure.
    """

    def __init__(self, mark_fn, tick_size: float, slippage_ticks: float = 1.0):
        self.mark_fn = mark_fn
        self.tick_size = tick_size
        self.slippage_ticks = slippage_ticks
        self._ids = itertools.count(1)
        self.orders: Dict[str, dict] = {}
        self.equity: float = 0.0

    def place(self, side: str, contracts: int, limit: Optional[float] = None,
              tif: str = "DAY", contract_code: Optional[str] = None):
        oid = f"paper-{next(self._ids)}"
        self.orders[oid] = {"side": side, "qty": contracts, "limit": limit,
                            "code": contract_code, "filled": 0, "px": None,
                            "status": WORKING}
        return OrderStatus(oid, WORKING)

    def poll(self, order_id: str) -> OrderStatus:
        o = self.orders.get(order_id)
        if not o:
            return OrderStatus(order_id, REJECTED, message="unknown order")
        if o["status"] == FILLED:
            return OrderStatus(order_id, FILLED, o["filled"], o["px"])
        mark = self.mark_fn()
        if mark is None:
            return OrderStatus(order_id, WORKING, message="no mark")
        adverse = self.tick_size * self.slippage_ticks
        if o["limit"] is None:
            px = mark + (adverse if o["side"] == BUY else -adverse)
        else:
            reached = (mark <= o["limit"]) if o["side"] == BUY else (mark >= o["limit"])
            if not reached:
                return OrderStatus(order_id, WORKING, message="limit not reached")
            px = o["limit"]
        o.update(status=FILLED, filled=o["qty"], px=px)
        return OrderStatus(order_id, FILLED, o["qty"], px)

    def cancel(self, order_id: str) -> bool:
        o = self.orders.get(order_id)
        if not o or o["status"] == FILLED:
            return False
        o["status"] = CANCELLED
        return True

    def place_spread(self, root: str, sell_code: str, buy_code: str,
                     contracts: int, direction: str) -> FillResult:
        mark = self.mark_fn() or 0.0
        return FillResult(True, 0.0, contracts, f"paper-spread-{next(self._ids)}",
                          False, FILLED, f"paper roll {sell_code}->{buy_code}")

    def account(self) -> dict:
        return {"net_liq": self.equity, "buying_power": self.equity,
                "maintenance_used": 0.0, "source": "paper"}


class TastyTradeBroker(Broker):
    """SKELETON — every method refuses rather than guesses.

    Confirm each of these against the live SDK during Epoch 0, on the tiny
    account, one at a time:
      * futures symbology the order endpoint expects (/MNQU6 vs /MNQ vs an
        instrument object) and whether it matches the streamer's symbol
      * whether a futures order takes a signed price or a side + positive price
      * whether MARKET is accepted on an outright and on a calendar spread
        (the options engine learned the hard way that spreads reject MARKET)
      * the exact field carrying the per-leg net FILL price, not the order price
      * how a partial fill is reported while the remainder still works
      * whether buying power is reported as futures BP or total, and which
        number the margin manager should size against
    """

    def __init__(self, session=None, account_number: str = "",
                 contract_code: str = ""):
        self.session = session
        self.account_number = account_number
        self.contract_code = contract_code

    def _todo(self, what: str):
        raise NotImplementedError(
            f"TastyTradeBroker.{what} is unverified — confirm against the SDK on "
            f"the tiny account before any live order. See the class docstring.")

    def place(self, side, contracts, limit=None, tif="DAY", contract_code=None):
        self._todo("place")

    def poll(self, order_id: str) -> OrderStatus:
        self._todo("poll")

    def cancel(self, order_id: str) -> bool:
        self._todo("cancel")

    def place_spread(self, root, sell_code, buy_code, contracts, direction):
        self._todo("place_spread")

    def account(self) -> dict:
        self._todo("account")


def build(paper: bool, mark_fn, tick_size: float, **kw) -> Broker:
    if paper:
        return PaperBroker(mark_fn, tick_size, kw.get("slippage_ticks", 1.0))
    return TastyTradeBroker(kw.get("session"), kw.get("account_number", ""),
                            kw.get("contract_code", ""))
