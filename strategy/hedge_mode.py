"""
futures_trader_v1/strategy/hedge_mode.py — v0.1
v0.1 — 2026-07-25 — Initial build. H1 Beta-Weighted Portfolio Hedge.

A HEDGE HAS NO EDGE, AND THAT IS THE POINT.
It is not supposed to make money. It is supposed to lose a SPECIFIC amount in a
SPECIFIC scenario, in exchange for offsetting exposure it was bought to cover.
So it is measured against that exposure — never against a P&L target — and its
results are reported in a SEPARATE LEDGER so a hedge's expected losses can never
contaminate the trading expectancy that the epoch calibration reads.

Everything about the hedge's shape is operator input, prompted by configure.sh:
portfolio value, beta, hedge ratio, instrument, max contracts, always-on vs
conditional, rebalance band.

TWO THINGS DELIBERATELY NOT DONE
  * No continuous rebalancing. A hedge rebalanced every tick is a commission
    generator. It moves only when realized drift exceeds the band.
  * No stop loss. A stop on a hedge removes the protection at precisely the
    moment it is working, which defeats the entire instrument.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

import config as C
from analysis.regime_confluence import (EXPANSION, TRENDING_DOWN, TRENDING_UP)
from strategy.base import LONG, SHORT, Signal, Strategy

ARMED, DISARMED = "ARMED", "DISARMED"


@dataclass
class HedgeTarget:
    contracts: int
    direction: str
    notional_covered: float
    residual_usd: float          # the rounding error, reported not hidden
    reason: str


def required_contracts(portfolio_value: float, beta: float, ratio: float,
                       index_price: float, multiplier: float) -> HedgeTarget:
    """contracts = (portfolio x beta x ratio) / (index_price x multiplier)

    The residual is REPORTED. A whole-contract hedge on a five-figure portfolio
    can be materially over- or under-covered by rounding, and an operator who
    cannot see that number cannot judge whether the hedge is doing its job."""
    if index_price <= 0 or multiplier <= 0:
        return HedgeTarget(0, SHORT, 0.0, 0.0, "no price")
    exposure = portfolio_value * beta * ratio
    per = index_price * multiplier
    raw = exposure / per
    n = int(round(raw))
    n = max(0, min(n, C.HEDGE_MAX_CONTRACTS))
    covered = n * per
    return HedgeTarget(n, SHORT, covered, exposure - covered,
                       f"{n} contracts covers ${covered:,.0f} of ${exposure:,.0f}")


def hedge_armed(regime: str, conditional: bool) -> Tuple[bool, str]:
    """Always-on, or conditional on a risk-off read. Hysteresis lives in the L2
    integrator: because the regime label is already hysteretic and slow to
    displace, the hedge inherits that stability and cannot flicker on a single
    contrary tick."""
    if not conditional:
        return True, "always-on"
    if regime in (TRENDING_DOWN, EXPANSION):
        return True, f"conditional: armed on {regime}"
    return False, f"conditional: disarmed on {regime or 'unknown'}"


class BetaWeightedHedge(Strategy):
    name = "H1_BETA_HEDGE"
    modes = ("HEDGE",)

    def evaluate(self, ctx: dict) -> Optional[Signal]:
        spec, price = ctx["spec"], ctx["price"]
        held = int(ctx.get("hedge_contracts_open", 0) or 0)
        pv = C.HEDGE_PORTFOLIO_VALUE
        if pv <= 0:
            return None

        armed, why = hedge_armed(ctx.get("regime", ""), C.HEDGE_CONDITIONAL)
        want = required_contracts(pv, C.HEDGE_BETA, C.HEDGE_TARGET_RATIO,
                                  price, spec.multiplier) if armed else \
            HedgeTarget(0, SHORT, 0.0, 0.0, why)

        if want.contracts == held:
            return None
        drift = abs(want.contracts - held) / max(want.contracts or held or 1, 1)
        if held > 0 and drift < C.HEDGE_REBALANCE_DRIFT:
            return None                  # inside the band — do not churn

        adding = want.contracts > held
        direction = SHORT if adding else LONG      # the adjusting order's side
        # A hedge carries no stop and no target. Both fields are required by the
        # Signal contract, so they are set to sentinels FAR outside any plausible
        # range and the hedge exit profile never consults them — rather than
        # inventing levels that would look like a trade thesis in the journal.
        far = price * (10.0 if direction == LONG else 0.1)
        near = price * (0.1 if direction == LONG else 10.0)
        return Signal(
            self.name, direction, price, near, far,
            reason=(f"hedge {'increase' if adding else 'reduce'} "
                    f"{held} -> {want.contracts} ({why}); "
                    f"residual ${want.residual_usd:,.0f}"),
            regime=ctx.get("regime", ""), regime_conviction=ctx.get("conviction", 0.0),
            session_phase=ctx.get("session_phase", ""), is_hedge=True,
            confluence={"target_contracts": want.contracts, "held": held,
                        "armed": armed, "residual_usd": want.residual_usd,
                        "notional_covered": want.notional_covered},
            at=ctx.get("now"))


def effectiveness(portfolio_returns, hedged_returns) -> Optional[float]:
    """Hedge effectiveness: 1 - var(portfolio + hedge) / var(portfolio).

    This is the ONLY score that matters for H1. A hedge that does not reduce
    variance is failing at its only job, whatever its standalone P&L says — and
    a hedge that made money in a rising market was probably too large."""
    n = min(len(portfolio_returns or []), len(hedged_returns or []))
    if n < 3:
        return None
    def var(xs):
        m = sum(xs) / len(xs)
        return sum((x - m) ** 2 for x in xs) / len(xs)
    base = var(portfolio_returns[:n])
    return None if base <= 0 else 1.0 - var(hedged_returns[:n]) / base


def dispatch(ctx: dict) -> Optional[Signal]:
    return BetaWeightedHedge().evaluate(ctx)
