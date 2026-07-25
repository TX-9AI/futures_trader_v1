"""
futures_trader_v1/control/roll_now.py — v0.1
v0.1 — 2026-07-25 — Box-side: perform this box's roll right now.

Runs ON THE BOX (control invokes it over the fan-out) because the box is the
only process that knows its own open position, its own volume history and its
own broker session. Control asks for a roll; it never reaches into a position
it cannot see.

Prints one status line so the fan-out can report it, and returns non-zero only
on a genuine failure — a "nothing to roll" is a success.
"""

from __future__ import annotations

import sys
from datetime import date


def main() -> int:
    import config as C
    from data.contract_registry import front_and_back
    from data.market_data import MarketData
    from database.trade_logger import TradeLogger
    from data.contract_registry import get_spec
    from execution import broker as BR
    from execution.roll_manager import RollLedger, RollManager
    from utils import sessions as S

    spec = get_spec(C.SYMBOL)
    sess = S.session_date()
    md = MarketData()
    tl = TradeLogger(C.TRADES_DB, C.PAPER_TRADING, spec.tick_value, spec.tick_size)
    rows = tl.get_open_trades()
    held = sum(int(r["contracts_open"] or 0) for r in rows)
    direction = rows[0]["direction"] if rows else "FLAT"

    front, back = front_and_back(C.SYMBOL, sess)
    vh = [(date.fromisoformat(d), fv, bv)
          for d, fv, bv in md.store.volume_history(front.code, back.code)]

    brk = BR.build(C.PAPER_TRADING, lambda: md.mark(C.SYMBOL), spec.tick_size)
    mgr = RollManager(C.ROLL_CONFIRM_SESSIONS, C.ROLL_HARD_DEADLINE_DAYS,
                      C.ROLL_AS_CALENDAR_SPREAD, auto=True,
                      only_when_flat=C.ROLL_ONLY_WHEN_FLAT,
                      ledger=RollLedger(), place_spread=brk.place_spread)
    plan = mgr.plan(C.SYMBOL, sess, vh, held, direction)
    if plan.kind == "no_roll_needed":
        print(f"{C.BOX_NAME}: no roll needed — {plan.assessment.reason}")
        return 0
    res = mgr.execute(plan)
    tl.record_roll(f"{C.SYMBOL}-{plan.to_code}-{sess}", C.SYMBOL, plan.from_code,
                   plan.to_code, held, plan.kind, res.status, res.fill_price,
                   res.message)
    print(f"{C.BOX_NAME}: {res.status} {plan.from_code}->{plan.to_code} "
          f"({res.message})")
    return 0 if res.status in ("COMPLETE", "PENDING") else 1


if __name__ == "__main__":
    raise SystemExit(main())
