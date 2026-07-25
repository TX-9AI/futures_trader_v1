"""
futures_trader_v1/execution/order_confirm.py — v0.1
v0.1 — 2026-07-25 — Initial build. The FillResult contract and bounded-poll
        confirmation. Nothing in this system books on submission.

THE DEFECT FAMILY THIS CLOSES BEFORE IT CAN OPEN
options_trader_v3 defects N, O and P were all one mistake wearing three hats:
treating order SUBMISSION as a fill. A hard close booked eight legs at
pnl=+$0.00. Live entries recorded the signal mark instead of the broker's price.
A roll closed a real position and booked a rolled one that never existed. Each
took an audit to find because every one of them produced plausible-looking rows.

The contract: `confirmed=True` with a real price, or the caller does nothing.
There is no third state and no flagged-ghost row.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

FILLED, PARTIAL, WORKING, CANCELLED, REJECTED = (
    "FILLED", "PARTIAL", "WORKING", "CANCELLED", "REJECTED")


@dataclass
class FillResult:
    confirmed: bool
    fill_price: Optional[float] = None
    filled_qty: int = 0
    order_id: Optional[str] = None
    partial: bool = False
    status: str = WORKING
    message: str = ""

    @property
    def usable(self) -> bool:
        return self.confirmed and self.fill_price is not None and self.filled_qty > 0


def confirm_fill(place: Callable,
                 poll: Callable,
                 cancel: Optional[Callable] = None,
                 deadline_s: float = 20.0,
                 poll_s: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> FillResult:
    """Submit, then poll to a bounded deadline. Book only on a confirmed fill.

    `place`/`poll`/`cancel` are injected so this is testable with no broker —
    which matters, because this is the one function whose failure modes are
    invisible in paper and expensive in live.

    A PARTIAL at the deadline is returned as confirmed for the filled portion
    ONLY. The caller sizes to `filled_qty`, never to what it asked for.
    """
    try:
        submitted = place()
    except Exception as e:                                   # noqa: BLE001
        return FillResult(False, status=REJECTED, message=f"submit failed: {e}")
    if not submitted:
        return FillResult(False, status=REJECTED, message="broker returned nothing")

    order_id = getattr(submitted, "order_id", None) or getattr(submitted, "id", None)
    start = clock()
    last = None
    while clock() - start < deadline_s:
        try:
            last = poll(order_id)
        except Exception as e:                               # noqa: BLE001
            return FillResult(False, order_id=order_id, status=WORKING,
                              message=f"poll failed: {e}")
        st = getattr(last, "status", WORKING)
        qty = int(getattr(last, "filled_qty", 0) or 0)
        px = getattr(last, "fill_price", None)
        if st == FILLED and qty > 0 and px is not None:
            return FillResult(True, px, qty, order_id, False, FILLED, "filled")
        if st in (CANCELLED, REJECTED):
            if qty > 0 and px is not None:
                return FillResult(True, px, qty, order_id, True, st,
                                  "partial before cancel/reject")
            return FillResult(False, order_id=order_id, status=st,
                              message=f"order {st.lower()} with no fill")
        sleep(poll_s)

    # deadline reached
    qty = int(getattr(last, "filled_qty", 0) or 0) if last else 0
    px = getattr(last, "fill_price", None) if last else None
    if cancel:
        try:
            cancel(order_id)
        except Exception:                                    # noqa: BLE001
            pass
    if qty > 0 and px is not None:
        return FillResult(True, px, qty, order_id, True, PARTIAL,
                          "partial at deadline; remainder cancelled")
    return FillResult(False, order_id=order_id, status=WORKING,
                      message="unfilled at deadline; cancelled")


def paper_fill(price: float, qty: int, direction: str, tick_size: float,
               slippage_ticks: float = 1.0) -> FillResult:
    """Paper fills pay the slippage a marketable order would actually pay.

    The options paper model filled at the exact mid on both sides and was
    therefore structurally optimistic — which mattered most on the trades whose
    edge was thinnest. One tick against is the honest default for a liquid
    futures contract, and it is the difference between a scalp book that looks
    profitable and one that is.
    """
    adverse = tick_size * slippage_ticks * (1 if direction == "LONG" else -1)
    return FillResult(True, price + adverse, qty, "paper", False, FILLED,
                      f"paper fill, {slippage_ticks:g} tick slippage")
