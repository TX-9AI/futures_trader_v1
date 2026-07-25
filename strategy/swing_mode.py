"""
futures_trader_v1/strategy/swing_mode.py — v0.1
v0.1 — 2026-07-25 — Initial build. W1 HTF PD-Array Swing, W2 Value Migration
        Fade. Held overnight; sized on the OVERNIGHT INITIAL margin rate.

Two things separate these from the intraday strategies and both are structural:

  THEY CARRY. The position survives the daily break and the weekend, so the
  stop is not guaranteed — price gaps THROUGH it. Sizing applies
  OVERNIGHT_GAP_MULT, which roughly halves swing size for the same budget.

  THEY ARE JUDGED OVER EPOCHS, NOT SESSIONS. n accumulates slowly. The interim
  measure is the MAE distribution: if stops are being hit on noise rather than
  on structure, the array selection is wrong and no amount of sample fixes it.
"""

from __future__ import annotations

from typing import Optional

import config as C
from analysis.profile import ABOVE_VALUE, BELOW_VALUE, INSIDE, OVERLAPPING
from analysis.regime_confluence import BALANCED, TRENDING_DOWN, TRENDING_UP
from strategy.base import LONG, SHORT, Signal, Strategy


class PDArraySwing(Strategy):
    """W1. Higher-timeframe bias, retracement into an HTF array located in the
    correct half of the dealing range, confirmed by a lower-timeframe change of
    character. The HTF pays for patience: the invalidation is one structure
    level away and the target is successive HTF pools."""
    name = "W1_PD_ARRAY"
    modes = ("SWING",)
    regime_defined = False       # an HTF thesis survives an intraday regime flip

    def evaluate(self, ctx: dict) -> Optional[Signal]:
        htf = ctx.get("structure_htf") or ctx.get("structure")
        ltf = ctx.get("structure")
        spec, price = ctx["spec"], ctx["price"]
        if htf is None or not getattr(htf, "warm", False):
            return None
        if htf.bias == "NEUTRAL" or htf.pd_position is None:
            return None

        direction = LONG if htf.bias == "BULL" else SHORT
        pd = htf.pd_position
        # discount for longs, premium for shorts — the whole premise
        if direction == LONG and pd > 0.45:
            return None
        if direction == SHORT and pd < 0.55:
            return None

        want = "BULL" if direction == LONG else "BEAR"
        fvg = htf.nearest_fvg(price, want)
        obs = [o for o in getattr(htf, "order_blocks", [])
               if o.direction == want and not o.mitigated]
        if fvg is None and not obs:
            return None

        # LTF change of character in our direction is the trigger
        if ltf is not None and getattr(ltf, "warm", False):
            if ltf.last_break_direction not in (want, "NEUTRAL"):
                return None

        lo = fvg.bottom if fvg else min(o.bottom for o in obs)
        hi = fvg.top if fvg else max(o.top for o in obs)
        buf = spec.tick_size * 8          # HTF arrays deserve a wider buffer
        stop = (lo - buf) if direction == LONG else (hi + buf)
        if (direction == LONG and stop >= price) or (direction == SHORT and stop <= price):
            return None
        risk = abs(price - stop)
        target, tnote = self._target_from_levels(price, direction,
                                                 ctx.get("liquidity"), 4.0, risk)
        return Signal(
            self.name, direction, price, stop, target,
            reason=f"HTF {htf.bias} bias, retracement into "
                   f"{'FVG' if fvg else 'order block'} at PD {pd:.2f}; {tnote}",
            regime=ctx.get("regime", ""), regime_conviction=ctx.get("conviction", 0.0),
            session_phase=ctx.get("session_phase", ""),
            cvd=getattr(ctx.get("flow"), "cvd", 0.0) if ctx.get("flow") else 0.0,
            pd_position=pd, structure_note=f"htf_bias={htf.bias}",
            confluence={"pd": pd, "htf_fvg": bool(fvg), "obs": len(obs)},
            at=ctx.get("now"))


class ValueMigrationFade(Strategy):
    """W2. Balanced auction: fade the value-area edge back toward the POC.

    THE ABORT CONDITION IS THE TRADE. Two consecutive closes accepted outside
    value means the auction is no longer balanced and the premise is void — exit
    immediately, do not wait for the stop. That is the market-profile analogue
    of a regime-flip exit, and it is what stops a range trade from quietly
    becoming a trend loss."""
    name = "W2_VALUE_FADE"
    modes = ("SWING", "DAY")
    regime_defined = True

    def evaluate(self, ctx: dict) -> Optional[Signal]:
        prof, spec, price = ctx.get("profile"), ctx["spec"], ctx["price"]
        if prof is None or not getattr(prof, "warm", False):
            return None
        if prof.migration != OVERLAPPING:
            return None                  # only fade a balanced auction
        today = prof.today
        if today.vah is None or today.val is None or today.poc is None:
            return None

        pos = today.position(price)
        buf = spec.tick_size * 4
        prior_hi = getattr(prof.prior, "vah", None) if prof.prior else None
        prior_lo = getattr(prof.prior, "val", None) if prof.prior else None

        if pos == ABOVE_VALUE:
            stop = max(price, prior_hi or price) + buf * 3
            target = today.poc
            direction = SHORT
        elif pos == BELOW_VALUE:
            stop = min(price, prior_lo or price) - buf * 3
            target = today.poc
            direction = LONG
        else:
            return None
        if (direction == LONG and (stop >= price or target <= price)) or \
           (direction == SHORT and (stop <= price or target >= price)):
            return None

        return Signal(
            self.name, direction, price, stop, target,
            reason=f"price {pos} in a balanced auction; fade to POC {target:.2f}",
            level_name="VALUE_AREA_EDGE", level_tier=0.45,
            regime=ctx.get("regime", ""), regime_conviction=ctx.get("conviction", 0.0),
            session_phase=ctx.get("session_phase", ""),
            pd_position=getattr(ctx.get("structure"), "pd_position", None),
            confluence={"migration": prof.migration, "overlap": prof.overlap_pct,
                        "position": pos},
            at=ctx.get("now"))


def value_fade_aborted(prof, closes, spec) -> bool:
    """Acceptance outside value: two consecutive closes beyond the edge. Called
    by the loop each tick for an open W2 position, ahead of the normal ladder."""
    if prof is None or not getattr(prof, "warm", False) or len(closes) < 2:
        return False
    t = prof.today
    if t.vah is None:
        return False
    return ((closes[-1] > t.vah and closes[-2] > t.vah) or
            (closes[-1] < t.val and closes[-2] < t.val))


def dispatch(ctx: dict) -> Optional[Signal]:
    return PDArraySwing().evaluate(ctx) or ValueMigrationFade().evaluate(ctx)
