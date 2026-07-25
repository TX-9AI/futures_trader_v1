"""
futures_trader_v1/risk/eligibility.py — v0.1
v0.1 — 2026-07-25 — Initial build. Which root/mode pairs are permitted AT ALL,
        as distinct from which are affordable today.

THE DISTINCTION THIS MODULE EXISTS TO MAKE
  X  EXCLUDED — a policy judgement that does not change with a good week.
     "We will never carry a full-size NQ overnight on this account" is true at
     $25k and still true at $40k; it is a statement about the contract's
     nominal size relative to the business, not about today's balance.
  0  BLOCKED  — arithmetic. Permitted, but the current equity or risk budget
     cannot fund one contract right now. This one moves as the account grows.
  n  ALLOWED  — lots.

Conflating the two is how a capacity table becomes misleading: a "0" invites
the operator to wait for the account to grow into a trade that should never be
taken, and it invites the engine to take that trade the moment it can be
afforded. An "X" says the answer is no regardless.

THE OBSERVATION ROUTE FOR EXCLUDED ROOTS
Every expensive root that is worth watching has a micro sibling in the registry
(ES->MES, NQ->MNQ, GC->MGC, SI->SIL, HG->MHG, CL->MCL, NG->MNG, YM->MYM,
RTY->M2K, 6E->M6E). The micro is how the strategy gets observed; the full-size
contract is simply not the vehicle. That is why paper equity does NOT need to
be inflated to make big names fire — inflating it would only produce a paper
book full of trades the live account can never take.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import config as C
from data.contract_registry import ContractSpec, get_spec

ALLOWED = "allowed"
BLOCKED = "blocked"       # arithmetic — could change with equity
EXCLUDED = "excluded"     # policy — will not change


@dataclass
class Eligibility:
    status: str
    reason: str

    @property
    def excluded(self) -> bool:
        return self.status == EXCLUDED

    def marker(self, lots: int) -> str:
        if self.status == EXCLUDED:
            return "X"
        return str(lots)


def mode_permitted(spec: ContractSpec, mode: str, equity: float) -> Eligibility:
    """Policy gate, evaluated before any arithmetic."""
    mode = mode.upper()
    budget = C.risk_per_trade(equity)

    # ── overnight modes: nominal size ceiling ────────────────────────────────
    if mode in ("SWING", "HEDGE"):
        if spec.init_margin > C.OVERNIGHT_MARGIN_CEILING_USD:
            sib = f" — use {spec.sibling}" if spec.sibling else ""
            return Eligibility(
                EXCLUDED,
                f"overnight initial ${spec.init_margin:,.0f} exceeds the "
                f"${C.OVERNIGHT_MARGIN_CEILING_USD:,.0f} carry ceiling{sib}")
        # A stop that gaps is the whole overnight risk. If the MINIMUM stop
        # already costs most of the budget once gap-adjusted, the contract is
        # too coarse to carry, whatever the margin says.
        gapped = (spec.min_stop_ticks * spec.tick_value
                  + C.COMMISSION_PER_CONTRACT_RT) * C.OVERNIGHT_GAP_MULT
        if gapped > budget * C.OVERNIGHT_MAX_MIN_STOP_PCT:
            sib = f" — use {spec.sibling}" if spec.sibling else ""
            return Eligibility(
                EXCLUDED,
                f"gap-adjusted minimum stop ${gapped:,.0f} exceeds "
                f"{C.OVERNIGHT_MAX_MIN_STOP_PCT*100:.0f}% of the "
                f"${budget:,.0f} budget{sib}")

    # ── scalp: the tightest stop must leave room to be wrong ────────────────
    min_cost = spec.min_stop_ticks * spec.tick_value + C.COMMISSION_PER_CONTRACT_RT
    if mode == "SCALP" and min_cost > budget * C.SCALP_MAX_MIN_STOP_PCT:
        sib = f" — use {spec.sibling}" if spec.sibling else ""
        return Eligibility(
            EXCLUDED,
            f"tightest sane stop costs ${min_cost:,.0f}, over "
            f"{C.SCALP_MAX_MIN_STOP_PCT*100:.0f}% of the ${budget:,.0f} "
            f"budget — no room to be wrong{sib}")

    # ── any mode: one contract at the minimum stop must fit the budget ──────
    if mode != "HEDGE" and min_cost > budget:
        sib = f" — use {spec.sibling}" if spec.sibling else ""
        return Eligibility(
            EXCLUDED,
            f"one contract at the minimum stop risks ${min_cost:,.0f} vs a "
            f"${budget:,.0f} budget — the stop is never widened to fit{sib}")

    return Eligibility(ALLOWED, "permitted")


def box_viable(symbol: Optional[str] = None,
               equity: Optional[float] = None) -> Tuple[bool, str]:
    """Should this box exist at all? If every mode is EXCLUDED, the answer is no
    and the operator should be told plainly rather than watching a box run for
    two weeks and place no trades."""
    spec = get_spec(symbol or C.SYMBOL)
    eq = equity if equity is not None else C.ACCOUNT_EQUITY_DEFAULT
    live = [m for m in ("SCALP", "DAY", "SWING", "HEDGE")
            if not mode_permitted(spec, m, eq).excluded]
    if not live:
        sib = (f" Run {spec.sibling} instead." if spec.sibling
               else " There is no micro sibling for this root.")
        return False, (f"{spec.root} is excluded in every mode at "
                       f"${eq:,.0f}.{sib}")
    return True, f"{spec.root} permitted in: {', '.join(live)}"
