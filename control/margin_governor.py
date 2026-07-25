"""
futures_trader_v1/control/margin_governor.py — v0.1
v0.1 — 2026-07-25 — Initial build. Fleet-level margin and correlated exposure.

THE PIECE WITH NO OPTIONS ANALOGUE, AND THE REASON IT MUST EXIST.

In the options fleet, every box's worst case was the debit it had already paid.
Boxes could be completely independent because no box could reach into another
box's risk. Futures break that assumption in two ways at once:

  ONE ACCOUNT.   Twelve boxes draw margin from the same balance. Each can sit
                 inside its own 35% utilisation cap while the ACCOUNT sits at
                 400%. Per-box accounting is locally correct and globally wrong
                 — the same shape as the paper/live database contamination that
                 took two audits to find.

  ONE BET.       Long MNQ, long MES and long MYM is not three positions. It is
                 one trade wearing three costumes, and it will lose on the same
                 candle. Correlation groups are counted as a single exposure.

WHAT THIS DOES AND DOES NOT DO. It AGGREGATES what boxes publish and it VETOES
new capacity by telling boxes to stand down. It does NOT place orders and it
does NOT close positions: a control plane that can liquidate is a control plane
whose bug can liquidate. Reducing risk stays with the box that owns the trade.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from control import fleet_config as FC

logger = logging.getLogger(__name__)

OK, WARN, BREACH = "OK", "WARN", "BREACH"


@dataclass
class BoxUsage:
    """What a box publishes each tick (execution.margin_manager.usage_report)."""
    box: str
    root: str
    mode: str
    contracts: int = 0
    direction: str = "FLAT"
    rate_kind: str = "DAY"
    per_contract: float = 0.0
    margin_used: float = 0.0
    overnight_requirement: float = 0.0
    as_of: Optional[str] = None

    @property
    def group(self) -> str:
        return FC.group_of(self.root)

    @property
    def signed(self) -> int:
        s = 1 if self.direction == "LONG" else -1 if self.direction == "SHORT" else 0
        return s * self.contracts


@dataclass
class GroupExposure:
    group: str
    margin: float = 0.0
    overnight: float = 0.0
    net_contracts: int = 0
    gross_contracts: int = 0
    boxes: List[str] = field(default_factory=list)

    @property
    def one_way(self) -> bool:
        """All boxes in the group leaning the same way — the case where three
        positions are really one."""
        return abs(self.net_contracts) == self.gross_contracts and self.gross_contracts > 0


@dataclass
class FleetVerdict:
    status: str
    equity: float
    margin_used: float
    overnight_required: float
    margin_pct: float
    overnight_pct: float
    groups: Dict[str, GroupExposure] = field(default_factory=dict)
    findings: List[str] = field(default_factory=list)
    stand_down: List[str] = field(default_factory=list)

    def headline(self) -> str:
        return (f"fleet margin {self.margin_pct*100:.0f}% "
                f"(overnight {self.overnight_pct*100:.0f}%) — {self.status}")


class MarginGovernor:
    def __init__(self, equity: Optional[float] = None,
                 fleet_max: Optional[float] = None,
                 overnight_max: Optional[float] = None,
                 group_max: Optional[float] = None,
                 state_dir: Optional[str] = None):
        self.equity = equity if equity is not None else FC.ACCOUNT_EQUITY
        self.fleet_max = fleet_max if fleet_max is not None else FC.FLEET_MARGIN_MAX_PCT
        self.overnight_max = (overnight_max if overnight_max is not None
                              else FC.FLEET_OVERNIGHT_MAX_PCT)
        self.group_max = group_max if group_max is not None else FC.GROUP_MARGIN_MAX_PCT
        self.state_dir = state_dir or FC.STATE_DIR

    # ── assessment ───────────────────────────────────────────────────────────
    def assess(self, usages: List[BoxUsage]) -> FleetVerdict:
        eq = self.equity or 0.0
        used = sum(u.margin_used for u in usages)
        overnight = sum(u.overnight_requirement for u in usages
                        if u.mode in FC.OVERNIGHT_MODES)
        v = FleetVerdict(OK, eq, used, overnight,
                         used / eq if eq else 0.0,
                         overnight / eq if eq else 0.0)

        for u in usages:
            if u.contracts <= 0:
                continue
            g = v.groups.setdefault(u.group, GroupExposure(u.group))
            g.margin += u.margin_used
            g.overnight += u.overnight_requirement if u.mode in FC.OVERNIGHT_MODES else 0.0
            g.net_contracts += u.signed
            g.gross_contracts += u.contracts
            g.boxes.append(u.box)

        if eq <= 0:
            v.status = WARN
            v.findings.append("no account equity known — cannot govern")
            return v

        if v.margin_pct > self.fleet_max:
            v.status = BREACH
            v.findings.append(
                f"fleet margin {v.margin_pct*100:.0f}% over the "
                f"{self.fleet_max*100:.0f}% cap (${used:,.0f} of ${eq:,.0f})")
        elif v.margin_pct > self.fleet_max * 0.85:
            v.status = WARN
            v.findings.append(f"fleet margin {v.margin_pct*100:.0f}% approaching the cap")

        if v.overnight_pct > self.overnight_max:
            v.status = BREACH
            v.findings.append(
                f"OVERNIGHT requirement {v.overnight_pct*100:.0f}% over the "
                f"{self.overnight_max*100:.0f}% cap — swing/hedge boxes cannot "
                f"all carry; reduce BEFORE the rate step-up, not after")

        for g in v.groups.values():
            pct = g.margin / eq
            if pct > self.group_max:
                v.status = BREACH if v.status != BREACH else v.status
                shape = ("one-way — this is ONE bet, not "
                         f"{len(g.boxes)} positions" if g.one_way else "mixed direction")
                v.findings.append(
                    f"{g.group} exposure {pct*100:.0f}% over the "
                    f"{self.group_max*100:.0f}% group cap ({shape}): "
                    f"{', '.join(g.boxes)}")

        if v.status == BREACH:
            v.stand_down = self._stand_down(usages, v)
        return v

    def _stand_down(self, usages: List[BoxUsage], v: FleetVerdict) -> List[str]:
        """Which boxes should take NO NEW positions.

        Deliberately conservative and deliberately non-destructive: the flat
        boxes in the offending groups are told to stand down first, because
        stopping new risk costs nothing, while unwinding existing risk is a
        decision that belongs to the box holding it.
        """
        offenders = {g.group for g in v.groups.values()
                     if g.margin / (v.equity or 1) > self.group_max}
        out = []
        for u in usages:
            if u.contracts == 0 and (not offenders or u.group in offenders):
                out.append(u.box)
        if not out:      # fleet-wide breach with everyone holding
            out = [u.box for u in usages if u.contracts == 0]
        return sorted(set(out))

    # ── publication ──────────────────────────────────────────────────────────
    def publish(self, v: FleetVerdict) -> Optional[str]:
        """Write the stand-down list where boxes can read it.

        Same delivery pattern the options fleet used for its pre-market flags:
        control writes a small JSON file, each box reads its own line. Control
        never injects a decision — it publishes a constraint and the box
        applies it.
        """
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            p = os.path.join(self.state_dir, "fleet_margin.json")
            with open(p, "w") as fh:
                json.dump({"at": datetime.utcnow().isoformat(),
                           "status": v.status,
                           "margin_pct": round(v.margin_pct, 4),
                           "overnight_pct": round(v.overnight_pct, 4),
                           "findings": v.findings,
                           "stand_down": v.stand_down}, fh, indent=2)
            return p
        except Exception as e:                               # noqa: BLE001
            logger.warning("could not publish fleet margin state: %s", e)
            return None


def from_reports(rows: List[dict]) -> List[BoxUsage]:
    """Adapt whatever the boxes published into BoxUsage."""
    out = []
    for r in rows or []:
        out.append(BoxUsage(
            box=r.get("box") or FC.box_name(r.get("root", ""), r.get("mode", "")),
            root=r.get("root", ""), mode=r.get("mode", ""),
            contracts=int(r.get("contracts", 0) or 0),
            direction=r.get("direction", "FLAT"),
            rate_kind=r.get("rate_kind", "DAY"),
            per_contract=float(r.get("per_contract", 0) or 0),
            margin_used=float(r.get("margin_used", 0) or 0),
            overnight_requirement=float(r.get("overnight_requirement", 0) or 0),
            as_of=r.get("as_of")))
    return out
