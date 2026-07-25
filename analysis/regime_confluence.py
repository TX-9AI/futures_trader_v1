"""
futures_trader_v1/analysis/regime_confluence.py — v0.1
v0.1 — 2026-07-25 — Initial build. LAYER 1: instantaneous graded evidence.

THE GRAMMAR, PORTED VERBATIM FROM options_trader_v3 regime_confluence v1.2
    score_R = (∏ hard_veto ∈ {0,1}) · (∏ soft_necessary ∈ [0,1])
                                    · (Σ w_k · corroborator_k),  Σ w_k = 1
A hard veto is a fact that makes a regime impossible. A soft-necessary is a
condition that must be present but admits degree. Corroborators are supporting
evidence that cannot, alone, create a regime. This structure is what stopped the
options classifier from being a pile of booleans, and it transfers unchanged.

THE DIAL SETTINGS ARE THE POST-CALIBRATION ONES, NOT THE NAIVE PRIORS
These bounds are inherited from the 2026-07-22 ramp de-saturation, which re-fit
them against 60,341 ticks over 6 sessions. The values that shipped FIRST in the
options project were far too narrow and RANGING saturated at p90 = 1.0, colliding
with TRENDING on 14-25% of ticks; after re-fitting, co-occurrence fell to 4.3%.

    RANGE_ROOM   0.05 - 0.20   ->   0.17 - 1.00
    OSC_CROSS    2 - 5         ->   4 - 10

Starting from the CORRECTED values means Epoch 2's calibration begins where the
options project spent four months arriving, not where it started. Expect the
futures re-fit to move them again — tick size and session structure differ — but
the saturation failure mode is pre-empted.

FUTURES-NATIVE EVIDENCE SHIPS AT WEIGHT 0
CVD alignment (trend) and value-area overlap (balance) are wired in and logged
but weighted 0.0, so the ported blocks keep the exact weights they were
calibrated with. They earn weight in Epoch 2 from realized edge, or they stay at
zero. This is the one options practice that most consistently kept an
unvalidated idea from quietly moving live size.
"""

from __future__ import annotations

import math
import os as _os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

TRENDING_UP = "TRENDING_UP"
TRENDING_DOWN = "TRENDING_DOWN"
EXPANSION = "EXPANSION"              # <- BREAKOUT_VOLATILE
COMPRESSION = "COMPRESSION"
BALANCED = "BALANCED"                # <- RANGING
LIQUIDITY_SWEEP = "LIQUIDITY_SWEEP"  # <- SWEEP_REVERSAL
REGIMES = [TRENDING_UP, TRENDING_DOWN, EXPANSION, COMPRESSION,
           BALANCED, LIQUIDITY_SWEEP]


def _envf(name: str, default: float) -> float:
    try:
        return float(_os.environ.get("FT_RC_" + name, default))
    except (TypeError, ValueError):
        return default


# ─── Ramp bounds — every one env-overridable (FT_RC_<NAME>) ───────────────────
# Calibration is a config change with instant rollback, never a code edit.
FLAT_ANGLE_CUT_DEG  = _envf("FLAT_ANGLE_CUT_DEG", 20.0)   # >= => centre not flat (hard veto)
FLAT_ANGLE_SOFT_DEG = _envf("FLAT_ANGLE_SOFT_DEG", 8.0)   # full flat credit at CUT - SOFT
RANGE_WINDOW_BARS   = int(_envf("RANGE_WINDOW_BARS", 25)) # angle + crossings window
ADX_STRONG_SOLO     = _envf("ADX_STRONG_SOLO", 35.0)      # ADX that carries a trend alone
OSC_CROSS_LO        = _envf("OSC_CROSS_LO", 4.0)          # de-saturated 2026-07-22
OSC_CROSS_HI        = _envf("OSC_CROSS_HI", 10.0)         # de-saturated 2026-07-22
RANGE_ROOM_LO       = _envf("RANGE_ROOM_LO", 0.17)        # de-saturated 2026-07-22
RANGE_ROOM_HI       = _envf("RANGE_ROOM_HI", 1.00)        # de-saturated 2026-07-22
EXPANSION_ADX_LO    = _envf("EXPANSION_ADX_LO", 38.0)     # momentum-carry forgiveness ramp
EXPANSION_ADX_HI    = _envf("EXPANSION_ADX_HI", 50.0)
EXPAND_RATIO_LO     = _envf("EXPAND_RATIO_LO", 1.0)       # atr / atr_avg
EXPAND_RATIO_HI     = _envf("EXPAND_RATIO_HI", 1.5)
SWEEP_REJ_LO_TICKS  = _envf("SWEEP_REJ_LO_TICKS", 4.0)    # TICKS, not percent (futures change)
SWEEP_REJ_HI_TICKS  = _envf("SWEEP_REJ_HI_TICKS", 16.0)
SWEEP_HALFLIFE_BARS = _envf("SWEEP_HALFLIFE_BARS", 3.0)
COMPRESS_WIDTH_SPAN = _envf("COMPRESS_WIDTH_SPAN", 0.15)
BB_WIDTH_COMPRESSION_PCT = _envf("BB_WIDTH_COMPRESSION_PCT", 0.20)

# ─── Corroborator weights (each block sums to 1.0) ────────────────────────────
# Ported at their calibrated values. The futures-native terms are appended at
# 0.0 so the proven blocks are numerically untouched.
W_TREND_ALIGN, W_TREND_MOM = 0.65, 0.35
W_TREND_CVD = _envf("W_TREND_CVD", 0.0)          # futures-native, Epoch 2 candidate
W_RANGE_BASE, W_RANGE_OSC = 0.40, 0.60
W_RANGE_VALUE = _envf("W_RANGE_VALUE", 0.0)      # futures-native, Epoch 2 candidate
W_COMP_BASE, W_COMP_SQZ, W_COMP_STORED = 0.30, 0.35, 0.35


def ramp(x: Optional[float], lo: float, hi: float) -> float:
    """Monotone [lo, hi] -> [0, 1] clamp. None is 0.0 — an unobservable input
    contributes nothing rather than a neutral 0.5 that would be indistinguishable
    from real half-evidence."""
    if x is None:
        return 0.0
    if hi <= lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def flat_angle_deg(closes: List[float], atr: Optional[float]) -> Optional[float]:
    """Slope of the least-squares fit through `closes`, expressed as an angle
    after normalising the y-axis by ATR. Normalising is what makes 20 degrees
    mean the same thing on gold and on the Dow — a raw price slope would not."""
    n = len(closes)
    if n < 3 or not atr or atr <= 0:
        return None
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(closes) / n
    num = sum((xs[i] - mx) * (closes[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    slope_per_bar = num / den
    return abs(math.degrees(math.atan(slope_per_bar / atr)))


def midline_crossings(closes: List[float], midline: Optional[float] = None) -> int:
    """How many times price crossed the window's centre. Few crossings = a coil
    or a pin; many = genuine two-sided rotation."""
    if len(closes) < 3:
        return 0
    mid = midline if midline is not None else sum(closes) / len(closes)
    n = 0
    for i in range(1, len(closes)):
        if (closes[i - 1] - mid) * (closes[i] - mid) < 0:
            n += 1
    return n


def _combine(hard_vetoes: List[float],
             soft_necessary: List[float],
             corroborators: List[Tuple[float, float]]) -> float:
    for v in hard_vetoes:
        if v <= 0.0:
            return 0.0
    score = 1.0
    for s in soft_necessary:
        score *= max(0.0, min(1.0, s))
    if corroborators:
        total_w = sum(w for w, _ in corroborators)
        if total_w > 0:
            csum = sum(w * max(0.0, min(1.0, val)) for w, val in corroborators)
            score *= csum / total_w
    return max(0.0, min(1.0, score))


@dataclass
class Evidence:
    scores: Dict[str, Optional[float]] = field(default_factory=dict)
    detail: Dict[str, dict] = field(default_factory=dict)
    observable: bool = True
    reason: str = ""

    def top(self) -> Tuple[Optional[str], float]:
        live = {k: v for k, v in self.scores.items() if v is not None}
        if not live:
            return None, 0.0
        k = max(live, key=lambda x: live[x])
        return k, live[k]

    def vector(self) -> Dict[str, Optional[float]]:
        return dict(self.scores)


class ConfluenceScorer:
    """Layer 1. Stateless and pure: same inputs -> same vector, every time.

    Statelessness is deliberate and load-bearing — the replay harness scores
    archived tape with this exact object, so a backtest cannot drift from the
    live engine through accumulated internal state.
    """

    def score(self,
              closes: List[float],
              vol_state,
              trend_state,
              structure,
              flow=None,
              profile=None,
              sweep_rejection_ticks: Optional[float] = None,
              sweep_age_bars: Optional[float] = None) -> Evidence:
        ev = Evidence()

        if vol_state is None or not getattr(vol_state, "warm", False):
            ev.observable = False
            ev.reason = "volatility state not warm"
            ev.scores = {r: None for r in REGIMES}
            return ev
        atr = getattr(vol_state, "atr", None)
        if not atr or atr <= 0:
            ev.observable = False
            ev.reason = "ATR unavailable"
            ev.scores = {r: None for r in REGIMES}
            return ev

        win = closes[-RANGE_WINDOW_BARS:] if closes else []
        angle = flat_angle_deg(win, atr)
        crossings = midline_crossings(win, getattr(vol_state, "bb_middle", None))
        width = getattr(vol_state, "bb_width_pct", None)
        adx = getattr(trend_state, "adx", None)

        up, d_up = self._trending(TRENDING_UP, trend_state, structure, flow, adx)
        dn, d_dn = self._trending(TRENDING_DOWN, trend_state, structure, flow, adx)
        exp, d_exp = self._expansion(vol_state, adx)
        comp, d_comp = self._compression(vol_state, angle, width, atr)
        bal, d_bal = self._balanced(angle, crossings, width, profile)
        swp, d_swp = self._sweep(sweep_rejection_ticks, sweep_age_bars)

        ev.scores = {TRENDING_UP: up, TRENDING_DOWN: dn, EXPANSION: exp,
                     COMPRESSION: comp, BALANCED: bal, LIQUIDITY_SWEEP: swp}
        ev.detail = {TRENDING_UP: d_up, TRENDING_DOWN: d_dn, EXPANSION: d_exp,
                     COMPRESSION: d_comp, BALANCED: d_bal, LIQUIDITY_SWEEP: d_swp}
        return ev

    # ── per-regime evidence ──────────────────────────────────────────────────
    def _trending(self, regime, trend, structure, flow, adx):
        want = "BULL" if regime == TRENDING_UP else "BEAR"
        d: dict = {"want": want}

        # HARD VETO 1: the multi-timeframe vote must point our way.
        veto_dir = 1.0 if getattr(trend, "direction", "NEUTRAL") == want else 0.0
        # HARD VETO 2: structure must not be broken against us.
        sb = getattr(structure, "bias", "NEUTRAL") if structure else "NEUTRAL"
        veto_struct = 0.0 if (sb and sb != "NEUTRAL" and sb != want) else 1.0
        d["veto_dir"], d["veto_struct"] = veto_dir, veto_struct

        # SOFT NECESSARY: ADX. Ported behaviour — strength alone can carry a
        # trend once it clears ADX_STRONG_SOLO.
        adx_s = ramp(adx, 18.0, ADX_STRONG_SOLO)
        d["adx"], d["adx_s"] = adx, adx_s

        align = getattr(trend, "aligned_frames", 0)
        voting = max(1, len(getattr(trend, "voting_frames", []) or [1]))
        align_val = min(1.0, align / voting)
        mom = getattr(trend, "momentum", "STEADY")
        mom_val = 1.0 if mom == "ACCELERATING" else 0.5 if mom == "STEADY" else 0.2

        # futures-native, weight 0 until Epoch 2 earns it
        cvd_val = 0.5
        if flow is not None and getattr(flow, "warm", False):
            bias = getattr(flow, "bias", "")
            cvd_val = 1.0 if ((want == "BULL" and bias == "BUY") or
                              (want == "BEAR" and bias == "SELL")) else 0.0
            if getattr(flow, "approximated", True):
                cvd_val = 0.5 + (cvd_val - 0.5) * 0.5   # a proxy gets half a vote
        d["align"], d["mom"], d["cvd"] = align_val, mom_val, cvd_val

        score = _combine([veto_dir, veto_struct], [adx_s],
                         [(W_TREND_ALIGN, align_val), (W_TREND_MOM, mom_val),
                          (W_TREND_CVD, cvd_val)])
        return score, d

    def _expansion(self, vol, adx):
        ratio = getattr(vol, "expansion_ratio", None)
        expand_s = ramp(ratio, EXPAND_RATIO_LO, EXPAND_RATIO_HI)
        inside = getattr(vol, "inside_bands", None)
        outside_s = 0.0 if inside else 1.0
        if inside:
            # momentum carry: a strong trend that has not yet cleared the band
            # is forgiven progressively rather than vetoed outright.
            outside_s = ramp(adx, EXPANSION_ADX_LO, EXPANSION_ADX_HI)
        d = {"ratio": ratio, "expand_s": expand_s,
             "inside_bands": inside, "outside_s": outside_s}
        return _combine([], [expand_s, outside_s], []), d

    def _compression(self, vol, angle, width, atr):
        d = {"angle": angle, "width": width}
        veto_flat = 0.0 if (angle is not None and angle >= FLAT_ANGLE_CUT_DEG) else 1.0
        ratio = getattr(vol, "expansion_ratio", None)
        veto_notexp = 0.0 if (ratio is not None and ratio >= EXPAND_RATIO_HI) else 1.0
        narrow_s = 0.0
        if width is not None:
            narrow_s = ramp(BB_WIDTH_COMPRESSION_PCT - width, 0.0, COMPRESS_WIDTH_SPAN)
        squeeze_val = narrow_s
        stored_val = 1.0 - ramp(ratio, EXPAND_RATIO_LO, EXPAND_RATIO_HI)
        d.update(veto_flat=veto_flat, veto_notexp=veto_notexp,
                 narrow_s=narrow_s, stored=stored_val)
        score = _combine([veto_flat, veto_notexp], [narrow_s],
                         [(W_COMP_BASE, 1.0), (W_COMP_SQZ, squeeze_val),
                          (W_COMP_STORED, stored_val)])
        return score, d

    def _balanced(self, angle, crossings, width, profile):
        d = {"angle": angle, "crossings": crossings, "width": width}
        # HARD VETO: the centre must be flat. A sloping centre is a trend
        # wearing a range's clothes.
        veto_flat = 0.0 if (angle is None or angle >= FLAT_ANGLE_CUT_DEG) else 1.0
        flat_s = 1.0 if angle is None else ramp(
            FLAT_ANGLE_CUT_DEG - angle, 0.0, FLAT_ANGLE_SOFT_DEG)
        # SOFT NECESSARY: room to oscillate. De-saturated bounds.
        room_s = ramp(width, RANGE_ROOM_LO, RANGE_ROOM_HI)
        osc_s = ramp(float(crossings), OSC_CROSS_LO, OSC_CROSS_HI)
        value_val = 0.5
        if profile is not None and getattr(profile, "warm", False):
            value_val = 1.0 if getattr(profile, "balanced", False) else 0.0
        d.update(veto_flat=veto_flat, flat_s=flat_s, room_s=room_s,
                 osc_s=osc_s, value=value_val)
        score = _combine([veto_flat], [flat_s, room_s],
                         [(W_RANGE_BASE, 1.0), (W_RANGE_OSC, osc_s),
                          (W_RANGE_VALUE, value_val)])
        return score, d

    def _sweep(self, rejection_ticks, age_bars):
        if rejection_ticks is None:
            return 0.0, {"rejection_ticks": None}
        strength = ramp(rejection_ticks, SWEEP_REJ_LO_TICKS, SWEEP_REJ_HI_TICKS)
        decay = 1.0
        if age_bars is not None and SWEEP_HALFLIFE_BARS > 0:
            decay = 0.5 ** (age_bars / SWEEP_HALFLIFE_BARS)
        return strength * decay, {"rejection_ticks": rejection_ticks,
                                  "strength": strength, "decay": decay}
