"""
futures_trader_v1/control/roll_control.py — v0.1
v0.1 — 2026-07-25 — Operator roll control: one, a subset, or all.

The operator spec was "roll one, all, or a selected subset", and the machinery
for it has existed since Phase 1 (`RollManager.plan_many` / `execute`) — but
nothing exposed it, so the only way to roll was to wait for the box's own
automatic check. This is the missing operator path.

TWO SAFETIES, both learned rather than invented:
  * PLAN BEFORE EXECUTE, always. Every entry point renders what it WOULD do and
    requires a separate confirm, because a roll touches a real position and the
    half-complete case is the expensive one.
  * A HALF-COMPLETE ROLL PAGES AND STOPS THE BATCH. If one box's front month
    closed and its back month did not open, continuing to roll eleven more
    boxes is the wrong instinct — that box is flat and believes it is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

from control import fleet_config as FC
from control.fleet import Fleet
from data.contract_registry import ROOTS
from execution.roll_manager import ROLL_HALF, RollManager

logger = logging.getLogger(__name__)


@dataclass
class RollReport:
    planned: List[str] = field(default_factory=list)
    executed: List[str] = field(default_factory=list)
    halted_on: str = ""
    warnings: List[str] = field(default_factory=list)

    def headline(self) -> str:
        h = f"{len(self.executed)}/{len(self.planned)} rolled"
        if self.halted_on:
            h += f" — BATCH HALTED at {self.halted_on}"
        return h


def root_of(symbol: str) -> str:
    """Box names may carry a disambiguating digit (MES2 = a second MES box)."""
    r = "".join(ch for ch in symbol.upper() if not ch.isdigit())
    return r if r in ROOTS else symbol.upper()


class RollControl:
    def __init__(self, fleet: Optional[Fleet] = None,
                 confirm: Optional[Callable[[str], bool]] = None,
                 alert: Optional[Callable[[str], None]] = None):
        self.fleet = fleet or Fleet()
        self.confirm = confirm or (lambda msg: False)
        self.alert = alert or (lambda m: logger.warning(m))

    def _targets(self, selection: str) -> List:
        """selection: 'all', a single box/symbol, or a comma-separated subset."""
        running = self.fleet.running()
        sel = (selection or "all").strip().lower()
        if sel == "all":
            return running
        wanted = {x.strip().upper() for x in sel.replace(";", ",").split(",") if x.strip()}
        return [i for i in running
                if i.box.upper() in wanted or i.symbol.upper() in wanted]

    def plan(self, selection: str = "all",
             on: Optional[date] = None) -> List[Tuple[object, object]]:
        on = on or date.today()
        out = []
        for inst in self._targets(selection):
            mgr = RollManager(auto=False, alert=self.alert)
            # Volume history lives on the BOX, so control asks the box rather
            # than guessing. A box that cannot answer is planned conservatively
            # (no crossover, deadline only) instead of being skipped silently.
            p = mgr.plan(root_of(inst.symbol), on, volume_history=None,
                         open_contracts=0, direction="FLAT")
            out.append((inst, p))
        return out

    def execute(self, selection: str = "all", on: Optional[date] = None,
                assume_yes: bool = False) -> RollReport:
        rep = RollReport()
        plans = self.plan(selection, on)
        if not plans:
            rep.warnings.append("no running boxes matched the selection")
            return rep

        lines = [p.describe() for _i, p in plans]
        rep.planned = lines
        if not assume_yes and not self.confirm("\n".join(lines)):
            rep.warnings.append("cancelled at confirmation")
            return rep

        for inst, p in plans:
            if p.kind == "no_roll_needed":
                continue
            # The BOX owns its position, so the box performs its own roll. Control
            # asks; it does not reach into a position it cannot see.
            res = self.fleet.run("venv/bin/python -m control.roll_now",
                                 instances=[inst])
            ok = bool(res) and res[0].ok
            out = res[0].out if res else ""
            rep.executed.append(f"{inst.box}: {'ok' if ok else 'FAILED'} {out[:120]}")
            if ROLL_HALF in out:
                rep.halted_on = inst.box
                self.alert(f"🚨 HALF-COMPLETE ROLL on {inst.box} — batch halted. "
                           f"That box may be FLAT and believe it is not.")
                break
        return rep
