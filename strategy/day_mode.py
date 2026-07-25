"""
futures_trader_v1/strategy/day_mode.py — v0.1
v0.1 — 2026-07-25 — Initial build. D1 Opening Drive Break+Retest, D2 Liquidity
        Sweep Reversal, D3 Trend Continuation. Flat by the cash close.

Dispatch is RANKED, not first-match-by-accident:
  D1 owns the opening-range window outright.
  A RUNAWAY break hands off to D3 when the regime is trending — a break that ran
  without retesting is one of the strongest continuation tells there is — and to
  D2 otherwise, when the runaway is heading INTO a graded level.
  D2 and D3 own mid-session, split by whether the tape is trending.
"""

from __future__ import annotations

from typing import List, Optional

import config as C
from analysis import opening_range as OR
from analysis.regime_confluence import (BALANCED, EXPANSION, LIQUIDITY_SWEEP,
                                        TRENDING_DOWN, TRENDING_UP)
from strategy.base import LONG, SHORT, Signal, Strategy

TRENDING = (TRENDING_UP, TRENDING_DOWN)


class OpeningDriveBreakRetest(Strategy):
    """D1. Geometry-gated: a confirmed break+retest ALWAYS trades, graded only
    on liquidity in the path. Two futures-native additions to the ported core:
    the break must carry order-flow agreement, and it is refused if a top-tier
    level sits within half a stop of entry in the path — that is a wall, not a
    target."""
    name = "D1_ORB_RETEST"
    modes = ("DAY",)
    geometry_gated = True

    def evaluate(self, ctx: dict) -> Optional[Signal]:
        orb: OR.ORBState = ctx.get("orb")
        if not orb or not orb.confirmed:
            return None
        spec, price = ctx["spec"], ctx["price"]
        direction = LONG if orb.state == OR.CONFIRMED_LONG else SHORT
        stop = orb.impulsive_stop
        if stop is None:
            return None

        width = orb.width or 0.0
        target = (orb.high + width) if direction == LONG else (orb.low - width)

        flow = ctx.get("flow")
        if flow is not None and getattr(flow, "warm", False) and \
                not getattr(flow, "approximated", True):
            bias = getattr(flow, "bias", "")
            if (direction == LONG and bias == "SELL") or \
               (direction == SHORT and bias == "BUY"):
                return None            # a break on opposing flow is a grab

        liq = ctx.get("liquidity")
        risk = abs(price - stop)
        if liq is not None and risk > 0:
            for lv in liq.in_path(price, target):
                if lv.tier >= 0.9 and abs(lv.price - price) < risk * 0.5:
                    return None        # a wall inside half a stop

        return Signal(
            self.name, direction, price, stop, target,
            reason=f"opening-range {direction.lower()} break + retest "
                   f"(depth {orb.retest_depth_ticks:.0f} ticks)",
            regime=ctx.get("regime", ""), regime_conviction=ctx.get("conviction", 0.0),
            session_phase=ctx.get("session_phase", ""), killzone=ctx.get("killzone", ""),
            cvd=getattr(flow, "cvd", 0.0) if flow else 0.0,
            pd_position=getattr(ctx.get("structure"), "pd_position", None),
            confluence={"orb_width": width, "attempts": orb.attempts,
                        "retest_depth_ticks": orb.retest_depth_ticks},
            at=ctx.get("now"))


class LiquiditySweepReversal(Strategy):
    """D2. Location (graded level) + penetration + rejection + absorption.

    The level TIER is the heaviest input, and the stop sits just past the sweep
    extreme — structurally tight, which is what makes the R:R work. The options
    version had good entries and an upside-down payoff; here the target is the
    OPPOSING POOL (a named level, not a percentage) and MIN_RRR gates the trade
    downstream."""
    name = "D2_SWEEP_REVERSAL"
    modes = ("DAY", "SCALP")

    def evaluate(self, ctx: dict) -> Optional[Signal]:
        spec, price = ctx["spec"], ctx["price"]
        c1, liq = ctx.get("c1"), ctx.get("liquidity")
        if not c1 or not c1.has(4) or liq is None:
            return None
        regime = ctx.get("regime", "")
        if regime in TRENDING:
            return None            # do not fade a committed trend

        lookback = 3
        recent_high = max(c1.high[-lookback:])
        recent_low = min(c1.low[-lookback:])
        close = c1.close[-1]

        for lv in liq.levels:
            if lv.tier < 0.45:
                continue
            # sweep HIGH -> short
            if recent_high > lv.price and close < lv.price:
                pen_ticks = (recent_high - lv.price) / spec.tick_size
                if pen_ticks < 2:
                    continue
                stop = recent_high + spec.tick_size * 2
                target, tnote = self._target_from_levels(
                    price, SHORT, liq, 3.0, abs(stop - price))
                return self._mk(SHORT, price, stop, target, lv, pen_ticks, tnote, ctx)
            # sweep LOW -> long
            if recent_low < lv.price and close > lv.price:
                pen_ticks = (lv.price - recent_low) / spec.tick_size
                if pen_ticks < 2:
                    continue
                stop = recent_low - spec.tick_size * 2
                target, tnote = self._target_from_levels(
                    price, LONG, liq, 3.0, abs(price - stop))
                return self._mk(LONG, price, stop, target, lv, pen_ticks, tnote, ctx)
        return None

    def _mk(self, direction, price, stop, target, lv, pen, tnote, ctx) -> Signal:
        flow = ctx.get("flow")
        div = getattr(flow, "divergence", "NO_DIV") if flow else "NO_DIV"
        return Signal(
            self.name, direction, price, stop, target,
            reason=f"swept {lv.name} by {pen:.0f} ticks and reclaimed; {tnote}",
            level_name=lv.name, level_tier=lv.tier,
            regime=ctx.get("regime", ""), regime_conviction=ctx.get("conviction", 0.0),
            session_phase=ctx.get("session_phase", ""), killzone=ctx.get("killzone", ""),
            cvd=getattr(flow, "cvd", 0.0) if flow else 0.0,
            delta_divergence=getattr(flow, "divergence_strength", 0.0) if flow else 0.0,
            pd_position=getattr(ctx.get("structure"), "pd_position", None),
            confluence={"penetration_ticks": pen, "divergence": div},
            at=ctx.get("now"))


class TrendContinuation(Strategy):
    """D3. Trending regime + a retracement into the 0.62-0.79 zone of the
    impulse leg, ideally coinciding with an unfilled FVG or an order block, then
    entry on the resumption. Regime-defined: a flip out of trend closes it,
    green or red."""
    name = "D3_CONTINUATION"
    modes = ("DAY", "SWING")
    regime_defined = True

    def evaluate(self, ctx: dict) -> Optional[Signal]:
        regime = ctx.get("regime", "")
        if regime not in TRENDING:
            return None
        st, spec, price = ctx.get("structure"), ctx["spec"], ctx["price"]
        if st is None or not getattr(st, "warm", False):
            return None
        if st.range_high is None or st.range_low is None:
            return None

        direction = LONG if regime == TRENDING_UP else SHORT
        pd = st.pd_position
        if pd is None:
            return None
        # buy discount in an uptrend, sell premium in a downtrend
        in_zone = (0.21 <= pd <= 0.48) if direction == LONG else (0.52 <= pd <= 0.79)
        if not in_zone:
            return None

        want = "BULL" if direction == LONG else "BEAR"
        fvg = st.nearest_fvg(price, want)
        obs = [o for o in getattr(st, "order_blocks", [])
               if o.direction == want and not o.mitigated]
        if fvg is None and not obs:
            return None

        anchor_lo = fvg.bottom if fvg else min(o.bottom for o in obs)
        anchor_hi = fvg.top if fvg else max(o.top for o in obs)
        buf = spec.tick_size * 4
        stop = (anchor_lo - buf) if direction == LONG else (anchor_hi + buf)
        if (direction == LONG and stop >= price) or (direction == SHORT and stop <= price):
            return None
        risk = abs(price - stop)
        target, tnote = self._target_from_levels(price, direction,
                                                 ctx.get("liquidity"), 3.0, risk)
        flow = ctx.get("flow")
        return Signal(
            self.name, direction, price, stop, target,
            reason=f"{regime} retracement into "
                   f"{'FVG' if fvg else 'order block'} at PD {pd:.2f}; {tnote}",
            regime=regime, regime_conviction=ctx.get("conviction", 0.0),
            session_phase=ctx.get("session_phase", ""), killzone=ctx.get("killzone", ""),
            cvd=getattr(flow, "cvd", 0.0) if flow else 0.0,
            pd_position=pd, structure_note=f"bias={st.bias} last={st.last_break}",
            confluence={"pd": pd, "fvg": bool(fvg), "order_blocks": len(obs)},
            at=ctx.get("now"))


def dispatch(ctx: dict) -> Optional[Signal]:
    """Ranked, and the ranking encodes what the tape is telling us."""
    orb: OR.ORBState = ctx.get("orb")
    if orb and orb.confirmed:
        return OpeningDriveBreakRetest().evaluate(ctx)

    if orb and orb.invalidation == OR.RUNAWAY:
        # A runaway is proof of directional force. In a trend that is a
        # continuation setup; otherwise it is a candidate to be faded at the
        # level it is running into.
        if ctx.get("regime") in TRENDING:
            s = TrendContinuation().evaluate(ctx)
            if s:
                return s
        s = LiquiditySweepReversal().evaluate(ctx)
        if s:
            return s

    if ctx.get("regime") in TRENDING:
        return TrendContinuation().evaluate(ctx)
    return LiquiditySweepReversal().evaluate(ctx)
