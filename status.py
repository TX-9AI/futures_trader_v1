"""
futures_trader_v1/status.py — v0.2
v0.2 — 2026-07-25 — shows the buying-power gate state, and says plainly that it
        is INERT rather than passing in paper.
v0.1 — 2026-07-25 — Live snapshot for one box. Read-only, no orders, no writes.

EVERY NUMBER STATES ITS SOURCE. The options status tool spent weeks printing a
"$200 DAILY LOSS LIMIT HIT" banner that was never true — the limit resolved in
the SSH process's environment, where the real value was absent, so it fell back
to a default and displayed a halt that had not happened. The bot itself was
fine. A dashboard that cannot say where a number came from is a dashboard that
can lie confidently.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import config as C
from data.contract_registry import front_and_back, get_spec
from data.market_data import MarketData
from database.trade_logger import TradeLogger
from utils import sessions as S


def main() -> int:
    spec = get_spec(C.SYMBOL)
    md = MarketData()
    tl = TradeLogger(C.TRADES_DB, C.PAPER_TRADING, spec.tick_value, spec.tick_size)
    now = S.now_et()
    sess = S.session_date(now)
    front, back = front_and_back(C.SYMBOL, sess)
    ok, why = md.healthy()
    mark = md.mark(C.SYMBOL)
    pnl = tl.realized_pnl_today(sess)
    openrows = tl.get_open_trades()

    bar = "=" * 62
    print(bar)
    print(f" {C.BOX_NAME}  ·  {'PAPER' if C.PAPER_TRADING else 'LIVE'}"
          f"  ·  {now.strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f" contract {front.code}  (back {back.code})")
    print(bar)
    print(f" feed      {'OK ' if ok else 'DOWN'}  {why}")
    print(f" mark      {mark if mark is not None else '—'}   (source: feed store)")
    print(f" session   {S.session_phase(now)}  ·  killzones "
          f"{','.join(S.active_killzones(now)) or '—'}")
    print(f" market    {'OPEN' if S.market_is_open(now) else 'CLOSED'}"
          f"{'  (daily break)' if S.in_daily_break(now) else ''}")
    allowed, areason = S.entries_allowed(C.MODE, spec, now, C.ENTRY_CUTOFF_MIN,
                                         C.ENABLED_SESSIONS)
    print(f" entries   {'allowed' if allowed else 'blocked'} — {areason}")
    print(f" flatten   {'REQUIRED NOW' if S.must_be_flat(C.MODE, spec, now) else 'not yet'}"
          f"   ({'carries overnight' if C.MODE in S.OVERNIGHT_MODES else 'intraday'})")
    print("")
    equity = C.PAPER_EQUITY_DEFAULT if C.PAPER_TRADING else C.ACCOUNT_EQUITY_DEFAULT
    limit = C.DAILY_LOSS_LIMIT_USD
    print(f" risk      ${C.risk_per_trade(equity):,.0f}/trade "
          f"({C.RISK_PCT_OF_EQUITY*100:.1f}% of ${equity:,.0f}"
          f"{' PAPER-FIXED' if C.PAPER_TRADING else ''})")
    print(f" day P&L   ${pnl:,.2f}   limit ${-abs(limit):,.2f}"
          f"   {'HALTED' if pnl <= -abs(limit) else 'ok'}")
    print(f"           (limit source: config/env chain, not a display fallback)")
    if C.PAPER_TRADING:
        print(f" buy power INERT in paper — the gate is skipped, not passed")
    else:
        print(f" buy power gate {'ON' if C.BP_GATE_ENABLED else 'OFF'} · "
              f"{C.BP_MIN_HEADROOM_PCT*100:.0f}% headroom reserved")
        print(f"           (read from the broker at ORDER time; one shared "
              f"account,\n            so it already nets every other box)")
    print("")
    if openrows:
        for r in openrows:
            print(f" OPEN      {r['strategy']} {r['direction']} x{r['contracts_open']}"
                  f" @ {r['entry_price']}")
            print(f"           stop {r['trail_stop'] or r['stop_price']} · "
                  f"target {r['target_price']} · MFE {r['max_favorable_r']:+.2f}R"
                  f" · MAE {r['max_adverse_r']:+.2f}R")
    else:
        print(" OPEN      flat")
    print(bar)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
