"""
futures_trader_v1/strategy/base.py — v0.2
v0.2 — 2026-07-25 — net_target(): fallback targets are solved for the NET R:R
        after commission, not the geometric one. On a micro a round turn is a
        material fraction of a small stop, so every geometric fallback was
        landing below the fee-aware floor the risk manager applies.
v0.1 — 2026-07-25 — Initial build. The Signal contract every strategy emits and
        the R math every strategy shares.

ONE RULE ENFORCED HERE, AND IT IS THE POINT OF THE WHOLE FILE
A Signal is INVALID unless it declares an entry, a structural stop AND a target.
`validate()` rejects anything else before it can reach sizing.

The options book is the argument: 99 sweep trades, 75% win rate, minus $3,444.
Entries were good. The engine simply had no way to say "this reward does not pay
for this risk", because MIN_RRR was declared and never referenced. A strategy
that cannot name where it is wrong and where it is going is not expressing a
trade, it is expressing a hope — and a hope sizes exactly the same as a trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

LONG, SHORT = "LONG", "SHORT"


@dataclass
class Signal:
    strategy: str
    direction: str
    entry: float
    stop: float
    target: float
    reason: str = ""
    # context captured at signal time — the perishable half, journalled verbatim
    level_name: str = ""
    level_tier: float = 0.0
    regime: str = ""
    regime_conviction: float = 0.0
    session_phase: str = ""
    killzone: str = ""
    cvd: float = 0.0
    delta_divergence: float = 0.0
    pd_position: Optional[float] = None
    structure_note: str = ""
    confluence: Dict[str, Any] = field(default_factory=dict)
    is_hedge: bool = False
    at: Optional[datetime] = None

    # ── geometry ─────────────────────────────────────────────────────────────
    @property
    def sign(self) -> int:
        return 1 if self.direction == LONG else -1

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_distance(self) -> float:
        return abs(self.target - self.entry)

    @property
    def rr(self) -> float:
        r = self.risk_distance
        return (self.reward_distance / r) if r > 0 else 0.0

    def r_price(self, multiple: float) -> float:
        """The price `multiple` R away from entry, in our favour."""
        return self.entry + self.sign * self.risk_distance * multiple

    def r_of(self, price: float) -> float:
        """Where `price` sits in R terms. Negative = against us."""
        r = self.risk_distance
        return 0.0 if r <= 0 else (price - self.entry) * self.sign / r

    # ── the gate ─────────────────────────────────────────────────────────────
    def validate(self, spec=None, min_stop_ticks: Optional[float] = None):
        """Returns (ok, reason). Geometry only — sizing, R:R and margin are the
        risk manager's job. This catches the incoherent signal: a stop on the
        wrong side, a target behind the entry, a zero-width stop."""
        if self.direction not in (LONG, SHORT):
            return False, f"bad direction {self.direction!r}"
        if self.risk_distance <= 0:
            return False, "stop equals entry — no defined risk"
        if self.direction == LONG:
            if self.stop >= self.entry:
                return False, "LONG stop is at or above entry"
            if self.target <= self.entry:
                return False, "LONG target is at or below entry"
        else:
            if self.stop <= self.entry:
                return False, "SHORT stop is at or below entry"
            if self.target >= self.entry:
                return False, "SHORT target is at or above entry"
        if spec is not None and min_stop_ticks is not None:
            t = spec.ticks(self.entry - self.stop)
            if t < min_stop_ticks:
                return False, (f"stop {t:.1f} ticks is inside the noise floor "
                               f"({min_stop_ticks:.1f})")
        return True, "ok"

    def journal(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy, "direction": self.direction,
            "entry": self.entry, "stop": self.stop, "target": self.target,
            "rr": round(self.rr, 3), "level_name": self.level_name,
            "level_tier": self.level_tier, "regime": self.regime,
            "regime_conviction": self.regime_conviction,
            "session_phase": self.session_phase, "killzone": self.killzone,
            "cvd": self.cvd, "delta_divergence": self.delta_divergence,
            "pd_position": self.pd_position, "reason": self.reason,
            "confluence": self.confluence,
        }


class Strategy:
    """Base class. `evaluate()` returns a Signal or None.

    Strategies are STATELESS with respect to the tape: everything they need
    arrives in the context dict. The replay harness therefore scores archived
    tape with the same objects the live loop runs, and a backtest cannot drift
    from production through accumulated internal state — the parity property
    that made the options replay trustworthy.
    """
    name = "base"
    modes: tuple = ()

    def evaluate(self, ctx: dict) -> Optional[Signal]:
        raise NotImplementedError

    # shared helpers -------------------------------------------------------
    @staticmethod
    def net_target(entry: float, direction: str, risk: float, want_rr: float,
                   spec=None, commission: float = 0.0) -> float:
        """The price achieving `want_rr` AFTER fees.

        Geometry is not the same as net. A round turn on MNQ is 5 ticks against
        a 36-tick stop, so a geometric 2.0R target settles at about 1.63 net and
        the risk manager — which is fee-aware — correctly refuses it. Every
        strategy that could not name a structural target was producing exactly
        that trade. Solving for the net multiple puts the fee arithmetic in ONE
        place instead of leaving each strategy to under-aim by a different
        amount.

            reward_ticks = (rr * (stop_ticks * tv + fee) + fee) / tv
        """
        sign = 1 if direction == LONG else -1
        if spec is None or spec.tick_value <= 0 or commission <= 0:
            return entry + sign * risk * want_rr
        stop_ticks = risk / spec.tick_size
        risk_usd = stop_ticks * spec.tick_value + commission
        reward_ticks = (want_rr * risk_usd + commission) / spec.tick_value
        return entry + sign * reward_ticks * spec.tick_size

    @staticmethod
    def _target_from_levels(entry: float, direction: str, liquidity,
                            fallback_r: float, risk: float,
                            spec=None, commission: float = 0.0) -> (float, str):
        """Prefer a NAMED level as the target — the opposing pool is where the
        move is actually going. Fall back to an R multiple only when the map has
        nothing in the path, and say which was used."""
        side = "ABOVE" if direction == LONG else "BELOW"
        if liquidity is not None:
            pool = liquidity.above() if direction == LONG else liquidity.below()
            for lv in pool:
                if abs(lv.price - entry) >= risk * 1.2:
                    return lv.price, f"target = {lv.name}"
        px = Strategy.net_target(entry, direction, risk, fallback_r, spec, commission)
        return px, f"target = {fallback_r:g}R net of fees (no level in path)"
