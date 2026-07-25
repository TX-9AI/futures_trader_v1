"""
futures_trader_v1/execution/margin_manager.py — v0.1
v0.1 — 2026-07-25 — Initial build. Margin truth, the day/overnight rate split,
        the pre-emptive overnight gate, and settlement/variation tracking.

WHAT THIS SOLVES THAT options_trader_v3 NEVER HAD TO
----------------------------------------------------
A long option's maximum loss is the debit — sizing and risk are the same number,
so otv3 could size a trade with no concept of buying power at all. A futures
position's risk and its CAPITAL COST are two different numbers, and the second
one changes at 16:00 when the intraday rate expires.

Three failure modes this file exists to make impossible:
  1. Sizing a swing position at the DAY rate and discovering at 17:00 that the
     account cannot hold it overnight. (Gate: overnight modes size at INITIAL.)
  2. A fleet of independent boxes each individually within limits while the
     ACCOUNT is over. Every box shares one account, so margin is a fleet-level
     quantity. Each box publishes its usage; the control layer aggregates and
     can veto. Per-box-only accounting is the futures analogue of otv3's
     paper/live DB contamination — locally correct, globally wrong.
  3. Trusting the seed table. Seeds size the FIRST trade after an install; the
     broker's number governs from the first successful session onward, and the
     delta is logged so a stale seed is visible instead of silent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Optional, Tuple

from data.contract_registry import ContractSpec, get_spec

logger = logging.getLogger(__name__)

DAY_RATE = "DAY"
INITIAL_RATE = "INITIAL"
MAINTENANCE_RATE = "MAINTENANCE"

# Which rate a mode must be sized against. This mapping is the whole point of
# the file: a mode that carries overnight is never allowed to size on the
# intraday discount.
RATE_FOR_MODE = {
    "SCALP": DAY_RATE,
    "DAY":   DAY_RATE,
    "SWING": INITIAL_RATE,
    "HEDGE": INITIAL_RATE,
}


@dataclass
class MarginRates:
    """Per-contract requirement set. `source` is load-bearing: never report a
    margin number without saying where it came from."""
    root: str
    initial: float
    maintenance: float
    day: float
    source: str = "seed"          # seed | broker
    as_of: Optional[datetime] = None

    def rate(self, kind: str) -> float:
        return {DAY_RATE: self.day,
                INITIAL_RATE: self.initial,
                MAINTENANCE_RATE: self.maintenance}[kind]


@dataclass
class MarginDecision:
    allowed: bool
    max_contracts: int
    required: float
    available: float
    utilization: float
    rate_kind: str
    reason: str


@dataclass
class AccountSnapshot:
    net_liq: float = 0.0
    cash: float = 0.0
    buying_power: float = 0.0            # futures BP reported by the broker
    maintenance_used: float = 0.0
    as_of: Optional[datetime] = None
    source: str = "unknown"

    @property
    def stale(self) -> bool:
        return self.as_of is None


class MarginManager:
    """Owns margin truth for one box, and publishes its usage for the fleet.

    Deliberately does NOT place orders and does NOT know about strategies. It
    answers exactly two questions: how many contracts may this box hold, and can
    what it currently holds survive the overnight rate step-up.
    """

    def __init__(self,
                 spec: ContractSpec,
                 mode: str,
                 utilization_max: float = 0.35,
                 buffer_mult: float = 1.25,
                 use_broker: bool = True):
        self.spec = spec
        self.mode = mode.upper()
        self.utilization_max = utilization_max
        self.buffer_mult = buffer_mult
        self.use_broker = use_broker
        self.rates = MarginRates(spec.root, spec.init_margin,
                                 spec.maint_margin, spec.day_margin, "seed")
        self.account = AccountSnapshot()
        self._seed_delta_logged = False

    # ── truth refresh ─────────────────────────────────────────────────────────
    def apply_broker_rates(self, initial: float, maintenance: float,
                           day: Optional[float] = None,
                           now: Optional[datetime] = None) -> Dict[str, float]:
        """Replace the seeds with the broker's requirement and report the delta.
        The delta is the point: a seed that has drifted 40% is a sizing error
        the operator would otherwise never see."""
        delta = {
            "initial_pct": _pct_delta(self.rates.initial, initial),
            "maintenance_pct": _pct_delta(self.rates.maintenance, maintenance),
        }
        self.rates = MarginRates(
            self.spec.root, initial, maintenance,
            day if day is not None else initial * (self.spec.day_margin / max(self.spec.init_margin, 1e-9)),
            source="broker", as_of=now or datetime.utcnow())
        if not self._seed_delta_logged:
            logger.info("margin seeds -> broker: %s initial %.0f->%.0f (%+.1f%%), "
                        "maint %.0f->%.0f (%+.1f%%)",
                        self.spec.root, self.spec.init_margin, initial, delta["initial_pct"],
                        self.spec.maint_margin, maintenance, delta["maintenance_pct"])
            self._seed_delta_logged = True
        return delta

    def apply_account(self, snap: AccountSnapshot) -> None:
        self.account = snap

    # ── the questions this file answers ───────────────────────────────────────
    def rate_kind(self) -> str:
        return RATE_FOR_MODE.get(self.mode, INITIAL_RATE)

    def per_contract(self, kind: Optional[str] = None) -> float:
        return self.rates.rate(kind or self.rate_kind())

    def capacity(self, equity: Optional[float] = None) -> MarginDecision:
        """How many contracts this box may hold right now."""
        kind = self.rate_kind()
        per = self.per_contract(kind) * self.buffer_mult
        eq = equity if equity is not None else (self.account.net_liq or 0.0)
        if eq <= 0:
            return MarginDecision(False, 0, per, 0.0, 0.0, kind,
                                  "no account equity known — broker snapshot missing")
        allowance = eq * self.utilization_max
        n = int(allowance // per) if per > 0 else 0
        if n < 1:
            return MarginDecision(False, 0, per, allowance, 0.0, kind,
                                  f"one contract needs ${per:,.0f} incl. buffer, "
                                  f"box allowance is ${allowance:,.0f}")
        return MarginDecision(True, n, per, allowance, per / allowance, kind,
                              f"{n} contract(s) at the {kind} rate")

    def check_position(self, contracts: int,
                       equity: Optional[float] = None) -> MarginDecision:
        cap = self.capacity(equity)
        req = self.per_contract() * contracts * self.buffer_mult
        if contracts <= cap.max_contracts:
            return MarginDecision(True, cap.max_contracts, req, cap.available,
                                  req / cap.available if cap.available else 0.0,
                                  cap.rate_kind, "within box margin allowance")
        return MarginDecision(False, cap.max_contracts, req, cap.available,
                              req / cap.available if cap.available else 0.0,
                              cap.rate_kind,
                              f"{contracts} contracts need ${req:,.0f}; "
                              f"box allowance ${cap.available:,.0f}")

    def overnight_gate(self, contracts: int,
                       equity: Optional[float] = None) -> MarginDecision:
        """Can the CURRENT position survive the intraday→initial step-up?

        Run pre-emptively (config.OVERNIGHT_MARGIN_CHECK_ET) rather than at the
        step-up itself. A margin problem discovered at 17:00 is liquidated by
        someone else's algorithm at someone else's price.
        """
        per = self.rates.initial * self.buffer_mult
        eq = equity if equity is not None else (self.account.net_liq or 0.0)
        allowance = eq * self.utilization_max
        req = per * contracts
        if eq <= 0:
            return MarginDecision(False, 0, req, 0.0, 0.0, INITIAL_RATE,
                                  "no equity snapshot — cannot clear for overnight")
        ok = req <= allowance
        keep = int(allowance // per) if per > 0 else 0
        return MarginDecision(ok, keep, req, allowance,
                              req / allowance if allowance else 0.0, INITIAL_RATE,
                              "clears overnight initial margin" if ok else
                              f"overnight initial needs ${req:,.0f} vs ${allowance:,.0f} "
                              f"— reduce to {keep} contract(s)")

    def usage_report(self, contracts: int) -> Dict[str, object]:
        """What this box publishes to the control layer for fleet aggregation."""
        return {
            "root": self.spec.root,
            "mode": self.mode,
            "contracts": contracts,
            "rate_kind": self.rate_kind(),
            "per_contract": self.per_contract(),
            "margin_used": self.per_contract() * contracts,
            "overnight_requirement": self.rates.initial * contracts,
            "rates_source": self.rates.source,
        }


@dataclass
class SettlementTracker:
    """Daily settlement / variation margin. Futures cash-settle every day: an
    open swing position converts unrealized P&L into a real cash movement each
    session. Tracking it separately from trade P&L keeps the daily-loss breaker
    honest — a variation debit is not a losing trade, and counting it as one
    would halt a box that is doing exactly what it was told."""
    entries: Dict[str, float] = field(default_factory=dict)

    def record(self, session: date, variation: float) -> None:
        self.entries[session.isoformat()] = variation

    def cumulative(self) -> float:
        return sum(self.entries.values())

    def worst_day(self) -> Tuple[str, float]:
        if not self.entries:
            return ("", 0.0)
        k = min(self.entries, key=lambda x: self.entries[x])
        return (k, self.entries[k])


def _pct_delta(old: float, new: float) -> float:
    return 0.0 if not old else (new - old) / old * 100.0
