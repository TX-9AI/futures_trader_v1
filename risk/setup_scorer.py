"""
futures_trader_v1/risk/setup_scorer.py — v0.1
v0.1 — 2026-07-25 — Initial build. Weighted conviction score -> Grade A / B /
        no trade. There is no Grade C: below the B bar returns None.

TWO THINGS PORTED DELIBERATELY FROM options setup_scorer v1.4

1. A MECHANICALLY-VALIDATED SETUP IS NOT RE-GATED BY THE SCORER.
   In the options engine the ORB was dispatched regardless of regime — and then
   the scorer weighted `regime_conviction` at 20% of its grade, so the label
   that had been deliberately excluded at dispatch came back in through the
   side door and could veto a confirmed break. A live SPX break scored 0.4462
   against a 0.55 bar and was rejected four ticks running. The fix was to
   short-circuit: a confirmed geometric setup ALWAYS trades, and the only
   grading input is liquidity in the path to target. Same structure here:
   `geometry_gated=True` routes to `_grade_geometry`.

2. LIQUIDITY DOWNGRADES, IT NEVER VETOES.
   An unswept pool between entry and target means the path is obstructed —
   that is a smaller position, not a skipped trade.

Weights live in config.SCORE_WEIGHTS. New dimensions ship at 0.0 and earn their
weight from realized edge in Epoch 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import config as C

GRADE_A, GRADE_B = "A", "B"


@dataclass
class ScoreResult:
    grade: Optional[str]
    total: float
    breakdown: Dict[str, float] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)
    reason: str = ""
    geometry_gated: bool = False

    @property
    def fires(self) -> bool:
        return self.grade is not None

    def journal(self) -> Dict[str, Any]:
        return {"grade": self.grade, "total": round(self.total, 4),
                "breakdown": {k: round(v, 3) for k, v in self.breakdown.items()},
                "geometry_gated": self.geometry_gated, "reason": self.reason}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def score(signal, ctx: dict, geometry_gated: bool = False) -> ScoreResult:
    if geometry_gated:
        return _grade_geometry(signal, ctx)

    liq = ctx.get("liquidity")
    flow = ctx.get("flow")
    structure = ctx.get("structure")
    profile = ctx.get("profile")

    d: Dict[str, float] = {}

    # 1. level tier — the heaviest dimension, straight from the tiered map
    d["level_tier"] = _clamp(signal.level_tier)

    # 2. order flow confirmation. An APPROXIMATED CVD gets half a vote: it is a
    #    proxy derived from bar shape, and scoring a proxy as if it were tick
    #    truth is how a weak signal acquires unearned confidence.
    of = 0.5
    if flow is not None and getattr(flow, "warm", False):
        bias = getattr(flow, "bias", "")
        agrees = ((signal.direction == "LONG" and bias == "BUY") or
                  (signal.direction == "SHORT" and bias == "SELL"))
        div = getattr(flow, "divergence", "NO_DIV")
        div_helps = ((signal.direction == "LONG" and div == "BULLISH_DIV") or
                     (signal.direction == "SHORT" and div == "BEARISH_DIV"))
        of = 1.0 if (agrees or div_helps) else 0.0
        if div_helps:
            of = 1.0
        if getattr(flow, "approximated", True):
            of = 0.5 + (of - 0.5) * 0.5
    d["orderflow_confirm"] = _clamp(of)

    # 3. structure alignment
    sa = 0.5
    if structure is not None and getattr(structure, "warm", False):
        want = "BULL" if signal.direction == "LONG" else "BEAR"
        bias = getattr(structure, "bias", "NEUTRAL")
        sa = 1.0 if bias == want else 0.0 if bias not in ("NEUTRAL", "") else 0.5
    d["structure_align"] = _clamp(sa)

    # 4. regime conviction
    d["regime_conviction"] = _clamp(signal.regime_conviction)

    # 5. premium/discount — buying discount, selling premium
    pd = signal.pd_position
    if pd is None:
        d["pd_position"] = 0.5
    else:
        d["pd_position"] = _clamp(1.0 - pd) if signal.direction == "LONG" else _clamp(pd)

    # 6. session context — inside an enabled killzone is worth something
    d["session_context"] = 1.0 if signal.killzone else 0.4

    # 7/8. shadow dimensions — present, logged, weighted 0 until Epoch 2
    d["smt_divergence"] = _clamp(ctx.get("smt_score", 0.5))
    d["profile_context"] = (1.0 if (profile is not None and
                                    getattr(profile, "balanced", False)) else 0.5)

    w = C.SCORE_WEIGHTS
    total_w = sum(w.get(k, 0.0) for k in d) or 1.0
    total = sum(d[k] * w.get(k, 0.0) for k in d) / total_w

    if total >= C.GRADE_A_MIN_SCORE:
        return ScoreResult(GRADE_A, total, d, dict(w), "grade A")
    if total >= C.GRADE_B_MIN_SCORE:
        return ScoreResult(GRADE_B, total, d, dict(w), "grade B")
    return ScoreResult(None, total, d, dict(w),
                       f"below B bar ({total:.3f} < {C.GRADE_B_MIN_SCORE})")


def _grade_geometry(signal, ctx: dict) -> ScoreResult:
    """A confirmed geometric setup always trades. The ONLY grading input is
    whether the path to target is clear of unswept liquidity."""
    liq = ctx.get("liquidity")
    obstructions = []
    if liq is not None:
        obstructions = [l for l in liq.in_path(signal.entry, signal.target)
                        if l.tier >= 0.5]
    if not obstructions:
        return ScoreResult(GRADE_A, 1.0, {"path_clear": 1.0}, {},
                           "clear path to target", geometry_gated=True)
    names = ", ".join(l.name for l in obstructions[:3])
    return ScoreResult(GRADE_B, 0.6, {"path_clear": 0.0}, {},
                       f"unswept liquidity in path ({names}) — downgrade, not veto",
                       geometry_gated=True)
