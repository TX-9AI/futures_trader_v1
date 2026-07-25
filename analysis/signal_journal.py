"""
futures_trader_v1/analysis/signal_journal.py — v0.1
v0.1 — 2026-07-25 — Initial build. Log-only capture of the perishable half of
        every decision.

"A gate you cannot counterfactual is a gate you cannot calibrate."

The 1-minute tape can be replayed forever. What evaporates at the close is the
order-flow context at signal time, the level tier that was in play, the L1
evidence vector, and WHICH GATE disposed of each signal. Without it, every
session between now and the Epoch 4 calibration is tape that can never become
calibration data.

DESIGN GUARANTEE, PORTED VERBATIM: every emission is wrapped so any failure —
full disk, bad payload, permissions — degrades to a missing log line and never
raises. The trading loop must be byte-identical whether this module is present,
absent, or broken. It imports nothing from execution/, risk/, or strategy/, it
never opens the trades database, and it places no orders.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

SCORED = "scored"
DISPOSITION = "disposition"
REGIME = "regime"
SWEEP_CHECK = "sweep_check"
FLOW = "flow"


class SignalJournal:
    def __init__(self, root: str = "data/signal_journal",
                 symbol: str = "", mode: str = "", enabled: bool = True):
        self.root = root
        self.symbol = symbol
        self.mode = mode
        self.enabled = enabled

    def _path(self, when: datetime) -> str:
        d = when.strftime("%Y-%m-%d")
        return os.path.join(self.root, d, f"{self.symbol or 'UNKNOWN'}.jsonl")

    def emit(self, event: str, payload: Optional[Dict[str, Any]] = None,
             when: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        try:
            ts = when or datetime.utcnow()
            rec = {"ts": ts.isoformat(), "event": event,
                   "symbol": self.symbol, "mode": self.mode}
            rec.update(payload or {})
            p = self._path(ts)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
            return True
        except Exception:
            return False        # deliberate: a journal failure is never fatal

    # convenience wrappers — the vocabulary, in one place
    def scored(self, **kw) -> bool:
        return self.emit(SCORED, kw)

    def disposition(self, outcome: str, **kw) -> bool:
        return self.emit(DISPOSITION, {"outcome": outcome, **kw})

    def regime(self, l1: Dict[str, Any], l2: Dict[str, Any], **kw) -> bool:
        return self.emit(REGIME, {"l1": l1, "l2": l2, **kw})

    def flow(self, **kw) -> bool:
        return self.emit(FLOW, kw)
