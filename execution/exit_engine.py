"""
futures_trader_v1/execution/exit_engine.py — v0.2
v0.2 — 2026-07-25 — LADDER REORDER: the exhaustion DIVERGENCE exit moved above
        the trail. An ADJUSTMENT must never preempt a CLOSE — the position was
        riding an extra tick on a signal that had already said to leave. The
        extension stage now competes with the trail and the TIGHTER level wins
        (a stretched move gets a shorter leash, not a close). Exhaustion exits
        refuse to act on APPROXIMATED order flow.
v0.1 — 2026-07-25 — Initial build. Every way a futures position closes, in R.

"MAKE ENTRY EASY, MAKE EXIT SMART." This file is the second half.

THE ASYMMETRY THIS IS BUILT TO FIX
The options sweep book: 75% win rate, minus $3,444 across 99 trades. Per-trade
excursion told the story — losers showed roughly +12% MFE before dying at a wide
stop, winners were trailed out around +25% off a +60% peak. Winning three of
four and still losing money is not an entry problem and no entry filter fixes it.
Three mechanisms here, all structural rather than discretionary:

  SCALE-OUT AT +1R    bank half the position at the first objective. Options
                      sizing could not express this (one contract, one exit);
                      futures sizing can, and it converts "winners give back a
                      third of MFE" from a trailing-stop tuning problem into a
                      solved one.
  BREAKEVEN RATCHET   at +1R the stop moves to entry. A trade that has paid for
                      itself never becomes a loser.
  STRUCTURE TRAIL     the trail parks behind STRUCTURE (unfilled FVG, then last
                      swing), with an ATR chandelier only as a fallback. A
                      percentage trail is a tripwire; structure is where the
                      trade is actually invalidated.

EVERY THRESHOLD IS IN R OR IN TICKS. Never a percentage of price. R is also
what makes a 1-lot MNQ scalp and a 3-lot ES swing comparable rows in the same
expectancy table.

ORDER OF EVALUATION IS THE DESIGN. First match wins, and the ladder runs from
"we have no choice" down to "let it work".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import config as C
from strategy.base import LONG, SHORT

HOLD = "HOLD"
CLOSE_ALL = "CLOSE_ALL"
CLOSE_PARTIAL = "CLOSE_PARTIAL"
ADJUST_STOP = "ADJUST_STOP"

LIMIT, MARKET = "LIMIT", "MARKET"

# exit profiles
RUNNER = "RUNNER"     # no hard take-profit; the trail owns the upside
FIXED = "FIXED"       # closes at target
HEDGE = "HEDGE"       # closes only on operator action, rebalance or roll


@dataclass
class ManagedPosition:
    trade_id: str
    strategy: str
    direction: str
    entry: float
    stop: float                 # the LIVE stop; ratchets move it
    initial_stop: float         # immutable — the R denominator, forever
    target: float
    contracts_open: int
    contracts_initial: int
    profile: str = RUNNER
    regime_at_entry: str = ""
    regime_defined: bool = False       # a flip kills the thesis
    opened_at: Optional[datetime] = None
    scaled: bool = False
    breakeven_set: bool = False
    trail_armed: bool = False
    trail_stop: Optional[float] = None
    max_favorable_r: float = 0.0
    max_adverse_r: float = 0.0
    time_stop_min: Optional[float] = None
    notes: str = ""

    @property
    def sign(self) -> int:
        return 1 if self.direction == LONG else -1

    @property
    def risk(self) -> float:
        """One R in price. Anchored to the INITIAL stop and never re-derived —
        once the stop ratchets to breakeven, a live-stop denominator would make
        R infinite and every R-keyed rule would misfire."""
        return abs(self.entry - self.initial_stop)

    def r_of(self, price: float) -> float:
        r = self.risk
        return 0.0 if r <= 0 else (price - self.entry) * self.sign / r

    def r_price(self, mult: float) -> float:
        return self.entry + self.sign * self.risk * mult

    def stop_hit(self, price: float) -> bool:
        live = self.effective_stop
        return price <= live if self.direction == LONG else price >= live

    @property
    def effective_stop(self) -> float:
        if self.trail_stop is None:
            return self.stop
        return (max(self.stop, self.trail_stop) if self.direction == LONG
                else min(self.stop, self.trail_stop))


@dataclass
class ExitDecision:
    action: str = HOLD
    contracts: int = 0
    reason: str = ""
    order_mode: str = LIMIT
    new_stop: Optional[float] = None
    r_at_decision: float = 0.0

    @property
    def closes(self) -> bool:
        return self.action in (CLOSE_ALL, CLOSE_PARTIAL)


class ExitEngine:
    def __init__(self, spec, mode: str):
        self.spec = spec
        self.mode = mode.upper()

    # ── the ladder ───────────────────────────────────────────────────────────
    def evaluate(self, pos: ManagedPosition, price: float,
                 now: Optional[datetime] = None,
                 regime: Optional[str] = None,
                 structure=None, vol=None, flow=None, profile=None,
                 must_flatten: bool = False) -> ExitDecision:
        r = pos.r_of(price)
        pos.max_favorable_r = max(pos.max_favorable_r, r)
        pos.max_adverse_r = min(pos.max_adverse_r, r)

        # 1. FORCED — session flatten. Crosses the spread without hesitation:
        #    an unmanaged position past the bell is a risk problem, not a price
        #    problem. Overnight modes never reach here (utils.sessions decides).
        if must_flatten and pos.profile != HEDGE:
            return ExitDecision(CLOSE_ALL, pos.contracts_open,
                                "session flatten", MARKET, r_at_decision=r)

        # 2. STOP — structural, and it includes any ratchet or trail.
        if pos.stop_hit(price):
            which = ("trail" if (pos.trail_stop is not None and
                                 pos.effective_stop == pos.trail_stop)
                     else "breakeven" if pos.breakeven_set and abs(pos.stop - pos.entry) < 1e-9
                     else "initial stop")
            return ExitDecision(CLOSE_ALL, pos.contracts_open,
                                f"stop hit ({which}) at {r:+.2f}R", MARKET,
                                r_at_decision=r)

        # 3. THESIS DEATH — the regime that defined the trade is gone. Fires
        #    green or red; the trade was never about the P&L, it was about the
        #    read, and the read is dead.
        if pos.regime_defined and regime and regime != pos.regime_at_entry:
            return ExitDecision(CLOSE_ALL, pos.contracts_open,
                                f"regime flip {pos.regime_at_entry} -> {regime} "
                                f"at {r:+.2f}R", LIMIT, r_at_decision=r)

        # 4. TIME STOP — a scalp premise that has not resolved was not the
        #    premise. Only ever applied where a strategy asked for it.
        if pos.time_stop_min and pos.opened_at and now:
            held = (now - pos.opened_at).total_seconds() / 60.0
            if held >= pos.time_stop_min and r < C.SCALE_OUT_AT_R:
                return ExitDecision(CLOSE_ALL, pos.contracts_open,
                                    f"time stop: {held:.0f}m with no progress "
                                    f"({r:+.2f}R)", LIMIT, r_at_decision=r)

        # 5. EXHAUSTION EXIT — a spent move, even while still technically going
        #    our way. This sits with the CLOSING decisions, above the trail:
        #    an adjustment must never preempt an exit, or the position rides one
        #    more tick on a signal that already said to leave.
        if pos.profile == RUNNER and r >= C.TRAIL_ARM_AT_R:
            d = self._exhaustion_exit(pos, r, flow)
            if d is not None:
                return d

        # 6. SCALE-OUT — bank the move that actually happens.
        if (C.SCALE_OUT_ENABLED and not pos.scaled and
                pos.contracts_open >= 2 and r >= C.SCALE_OUT_AT_R):
            qty = max(1, int(round(pos.contracts_open * C.SCALE_OUT_FRACTION)))
            qty = min(qty, pos.contracts_open - 1)
            if qty >= 1:
                return ExitDecision(CLOSE_PARTIAL, qty,
                                    f"scale {qty} at +{r:.2f}R", LIMIT,
                                    r_at_decision=r)

        # 7. BREAKEVEN RATCHET — a trade that paid for itself cannot lose.
        if not pos.breakeven_set and r >= C.MOVE_STOP_TO_BE_AT_R:
            return ExitDecision(ADJUST_STOP, 0, f"stop to breakeven at +{r:.2f}R",
                                new_stop=pos.entry, r_at_decision=r)

        # 8. TRAIL — structure first, ATR only as a fallback. Exhaustion's
        #    EXTENSION stage competes here and the TIGHTER level wins: a
        #    stretched move gets a shorter leash, it does not get closed.
        if r >= C.TRAIL_ARM_AT_R:
            new = self._trail_level(pos, price, structure, vol)
            ext = self._exhaustion_tighten(pos, price, r, vol)
            cand = [x for x in (new, ext) if x is not None]
            if cand:
                best = max(cand) if pos.direction == LONG else min(cand)
                if self._tightens(pos, best):
                    tag = "trail" if best == new else "exhaustion trail"
                    return ExitDecision(ADJUST_STOP, 0,
                                        f"{tag} -> {best:.4f} at +{r:.2f}R",
                                        new_stop=best, r_at_decision=r)

        # 9. TARGET — only for FIXED profiles. A RUNNER has no hard take-profit
        #    by design; the trail decides when the move is over.
        if pos.profile == FIXED and self._target_hit(pos, price):
            return ExitDecision(CLOSE_ALL, pos.contracts_open,
                                f"target hit at {r:+.2f}R", LIMIT,
                                r_at_decision=r)

        return ExitDecision(HOLD, 0, f"hold at {r:+.2f}R", r_at_decision=r)

    # ── trail construction ───────────────────────────────────────────────────
    def _trail_level(self, pos: ManagedPosition, price: float,
                     structure, vol) -> Optional[float]:
        buf = self.spec.tick_size * C.TRAIL_STRUCTURE_BUFFER_TICKS
        want = "BULL" if pos.direction == LONG else "BEAR"

        # (a) nearest unfilled in-favour FVG — room to pull back INTO the gap
        if structure is not None and getattr(structure, "warm", False):
            fvg = structure.nearest_fvg(price, want)
            if fvg is not None:
                lvl = (fvg.bottom - buf) if pos.direction == LONG else (fvg.top + buf)
                if self._behind(pos, lvl, price):
                    return self.spec.round_to_tick(lvl)

            # (b) last swing that held
            swings = [s for s in getattr(structure, "swings", [])
                      if (s.kind == "LOW" if pos.direction == LONG else s.kind == "HIGH")]
            if swings:
                lvl = (swings[-1].price - buf if pos.direction == LONG
                       else swings[-1].price + buf)
                if self._behind(pos, lvl, price):
                    return self.spec.round_to_tick(lvl)

        # (c) ATR chandelier — a fallback, never the first choice
        atr = getattr(vol, "atr", None) if vol is not None else None
        if atr and atr > 0:
            lvl = (price - atr * C.TRAIL_ATR_MULT if pos.direction == LONG
                   else price + atr * C.TRAIL_ATR_MULT)
            if self._behind(pos, lvl, price):
                return self.spec.round_to_tick(lvl)
        return None

    @staticmethod
    def _behind(pos: ManagedPosition, level: float, price: float) -> bool:
        return level < price if pos.direction == LONG else level > price

    @staticmethod
    def _tightens(pos: ManagedPosition, new: float) -> bool:
        """A trail only ever moves in our favour. Never loosens — that is what
        makes it a ratchet rather than a moving target."""
        cur = pos.effective_stop
        return new > cur if pos.direction == LONG else new < cur

    @staticmethod
    def _target_hit(pos: ManagedPosition, price: float) -> bool:
        return (price >= pos.target if pos.direction == LONG
                else price <= pos.target)

    # ── exhaustion, in two explicit stages ──────────────────────────────────
    # EXTENSION TIGHTENS, DIVERGENCE EXITS. Deliberately two-stage: a strong
    # trend can stay stretched for a long time, so being far from the anchor is
    # a reason to shorten the leash, not a reason to leave. Only a new extreme
    # made on WEAKER participation says the move is actually spent.
    def _exhaustion_exit(self, pos: ManagedPosition, r: float,
                         flow) -> Optional[ExitDecision]:
        if flow is None or not getattr(flow, "warm", False):
            return None
        if getattr(flow, "approximated", True):
            return None      # a bar-shape proxy cannot carry an exit decision
        div = getattr(flow, "divergence", "NO_DIV")
        against = ((pos.direction == LONG and div == "BEARISH_DIV") or
                   (pos.direction == SHORT and div == "BULLISH_DIV"))
        if against and r >= pos.max_favorable_r - 0.05:
            return ExitDecision(CLOSE_ALL, pos.contracts_open,
                                f"exhaustion: new extreme on weaker flow "
                                f"({div}) at {r:+.2f}R", LIMIT, r_at_decision=r)
        return None

    def _exhaustion_tighten(self, pos: ManagedPosition, price: float,
                            r: float, vol) -> Optional[float]:
        mid = getattr(vol, "bb_middle", None) if vol is not None else None
        atr = getattr(vol, "atr", None) if vol is not None else None
        if not (mid and atr and atr > 0):
            return None
        if abs(price - mid) / atr < 2.0:
            return None
        return self.spec.round_to_tick(pos.entry + pos.sign * pos.risk * (r * 0.85))
