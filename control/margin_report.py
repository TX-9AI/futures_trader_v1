"""
futures_trader_v1/control/margin_report.py — v0.1
v0.1 — 2026-07-25 — The tiny box-side script control calls to read exposure.

Lives in control/ because it exists only to serve the control plane, but it RUNS
ON THE BOX. It prints one JSON line and nothing else, so the fan-out can parse
the last line of stdout without caring what else the shell emitted.

Read-only by construction: it opens the trade log, sums what is open, and asks
the margin manager what that costs. It cannot place, close, or modify anything.
"""

from __future__ import annotations

import json
import sys


def report() -> dict:
    import config as C
    from data.contract_registry import get_spec
    from database.trade_logger import TradeLogger
    from execution.margin_manager import MarginManager

    spec = get_spec(C.SYMBOL)
    tl = TradeLogger(C.TRADES_DB, C.PAPER_TRADING, spec.tick_value, spec.tick_size)
    rows = tl.get_open_trades()
    contracts = sum(int(r["contracts_open"] or 0) for r in rows)
    direction = rows[0]["direction"] if rows else "FLAT"
    mm = MarginManager(spec, C.MODE, C.MARGIN_UTILIZATION_MAX, C.MARGIN_BUFFER_MULT)
    out = mm.usage_report(contracts)
    out.update(box=C.BOX_NAME, direction=direction,
               paper=bool(C.PAPER_TRADING), open_trades=len(rows))
    return out


def main() -> int:
    try:
        print(json.dumps(report()))
    except Exception as e:                                   # noqa: BLE001
        # Print valid JSON even on failure so the collector records a box that
        # answered-but-broke differently from one that never answered.
        print(json.dumps({"error": str(e)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
