"""
futures_trader_v1/strategy/scalp_mode.py — v0.2
v0.2 — 2026-07-25 — fallback targets solved NET of fees (base.net_target).
v0.1 — 2026-07-25 — Initial build. S1 Absorption Reversal, S2 Killzone Micro-
        Continuation. Flat by the cash close, time-stopped, killzone-gated.

A SCALP MUST CLEAR ITS OWN COST STRUCTURE. Commission plus a tick of slippage
each way eats a marginal edge alive, which is why both strategies here carry a
TIME STOP: a premise that has not resolved quickly was not the premise. Holding
a stalled scalp is how a scalp book turns into an accidental day-trading book
with a scalp's stop.
"""

from __future__ import annotations

from typing import Optional

import config as C
from analysis import orderflow as OF
from analysis.regime_confluence import TRENDING_DOWN, TRENDING_UP
from strategy.base import LONG, SHORT, Signal, Strategy


class AbsorptionReversal(Strategy):
    """S1. The purest order-flow trade, and it cannot be built without tick
    data — which is precisely why it is worth building.

    Price arrives at a graded level, delta pushes hard in one direction, and
    PRICE DOES NOT GO. Someone large is filling passively. Entry is the flip of
    delta sign; invalidation is inches away beyond the absorption zone.

    REFUSES TO FIRE ON APPROXIMATED FLOW. A bar-shape proxy cannot distinguish
    absorption from a quiet drift, and scoring it as though it could is how a
    fabricated signal reaches live size."""
    name = "S1_ABSORPTION"
    modes = ("SCALP",)
    time_stop_min = 8.0

    def evaluate(self, ctx: dict) -> Optional[Signal]:
        flow, liq, spec, price = (ctx.get("flow"), ctx.get("liquidity"),
                                  ctx["spec"], ctx["price"])
        c1, vol = ctx.get("c1"), ctx.get("vol")
        if flow is None or not getattr(flow, "warm", False) or liq is None or not c1:
            return None
        if getattr(flow, "approximated", True):
            return None
        atr = getattr(vol, "atr", None) if vol else None
        absorbed, side = OF.detect_absorption(c1, flow, atr=atr)
        if not absorbed:
            return None

        lv = liq.strongest_within(price, spec.tick_size,
                                  max_ticks=max(spec.min_stop_ticks * 1.5, 8))
        if lv is None or lv.tier < 0.45:
            return None

        # Absorption of SELL aggression is buyers filling -> we go LONG.
        direction = LONG if side == OF.SELL else SHORT
        buf = spec.tick_size * 3
        recent_lo = min(c1.low[-4:])
        recent_hi = max(c1.high[-4:])
        stop = (recent_lo - buf) if direction == LONG else (recent_hi + buf)
        risk = abs(price - stop)
        if risk <= 0:
            return None
        target, tnote = self._target_from_levels(price, direction, liq, 2.0, risk, spec, C.COMMISSION_PER_CONTRACT_RT)

        return Signal(
            self.name, direction, price, stop, target,
            reason=f"{side} aggression absorbed at {lv.name}; {tnote}",
            level_name=lv.name, level_tier=lv.tier,
            regime=ctx.get("regime", ""), regime_conviction=ctx.get("conviction", 0.0),
            session_phase=ctx.get("session_phase", ""), killzone=ctx.get("killzone", ""),
            cvd=flow.cvd, delta_divergence=getattr(flow, "divergence_strength", 0.0),
            pd_position=getattr(ctx.get("structure"), "pd_position", None),
            confluence={"absorbed_side": side, "level_ticks":
                        lv.distance_ticks(price, spec.tick_size)},
            at=ctx.get("now"))


class KillzoneMicroContinuation(Strategy):
    """S2. Inside an enabled killzone, after a 1m break of structure with flow
    agreement, enter on the first retrace into the resulting gap. Hard
    time-boxed to the killzone that produced it."""
    name = "S2_KILLZONE_CONT"
    modes = ("SCALP",)
    time_stop_min = 12.0

    def evaluate(self, ctx: dict) -> Optional[Signal]:
        kz = ctx.get("killzone")
        if C.KILLZONE_REQUIRED and not kz:
            return None
        st, spec, price = ctx.get("structure"), ctx["spec"], ctx["price"]
        if st is None or not getattr(st, "warm", False):
            return None
        if st.last_break is None or st.last_break_direction == "NEUTRAL":
            return None

        direction = LONG if st.last_break_direction == "BULL" else SHORT
        want = "BULL" if direction == LONG else "BEAR"

        flow = ctx.get("flow")
        if flow is not None and getattr(flow, "warm", False):
            bias = getattr(flow, "bias", "")
            if (direction == LONG and bias == "SELL") or \
               (direction == SHORT and bias == "BUY"):
                return None

        fvg = st.nearest_fvg(price, want)
        if fvg is None or not fvg.contains(price):
            return None                 # must be retracing INTO the gap now

        buf = spec.tick_size * 2
        stop = (fvg.bottom - buf) if direction == LONG else (fvg.top + buf)
        risk = abs(price - stop)
        if risk <= 0:
            return None
        target, tnote = self._target_from_levels(price, direction,
                                                 ctx.get("liquidity"), 2.0, risk)
        return Signal(
            self.name, direction, price, stop, target,
            reason=f"{st.last_break} {st.last_break_direction} in {kz or 'session'}, "
                   f"retrace into FVG; {tnote}",
            regime=ctx.get("regime", ""), regime_conviction=ctx.get("conviction", 0.0),
            session_phase=ctx.get("session_phase", ""), killzone=kz or "",
            cvd=getattr(flow, "cvd", 0.0) if flow else 0.0,
            pd_position=st.pd_position,
            structure_note=f"{st.last_break}/{st.last_break_direction}",
            confluence={"killzone": kz, "fvg_mid": fvg.mid},
            at=ctx.get("now"))


def dispatch(ctx: dict) -> Optional[Signal]:
    return AbsorptionReversal().evaluate(ctx) or KillzoneMicroContinuation().evaluate(ctx)
