"""
futures_trader_v1/analysis/trend.py — v0.1
v0.1 — 2026-07-25 — Initial build. EMA stacks, ADX from the 5m frame,
        multi-timeframe direction vote with WEIGHT RENORMALIZATION.

THE DEFECT THIS FILE IS BUILT AROUND (options trend_engine, found 2026-07-16)
The direction vote carried tf_weights {1d:0.30, 4h:0.25, 1h:0.25, 15m:0.15,
5m:0.05}. The live feed never produced a 4h frame, so 25% of the weight simply
EVAPORATED — it was not redistributed, it was lost. Thin 1d/1h returned NEUTRAL
and diluted the remainder below the 0.30 gate, and `_is_trending()` hard-rejects
NEUTRAL. Result: TRENDING was unreachable. Zero occurrences across 34,925 replay
ticks, including 455 ticks at ADX >= 50 with confirming structure.

The options fix was to drop 4h and reweight toward intraday. That fixed the
symptom. THE FIX HERE IS STRUCTURAL: weights are declared as intent, then
RENORMALIZED over the timeframes that actually returned bars. A missing frame
can no longer silently drain the vote — it redistributes, and the state reports
which frames voted so a starved feed is visible instead of inferred.

ADX is sourced from the 5-MINUTE frame, carried over from options where it was
validated: 1m ADX is noise, 15m ADX is late.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from data.series import Candles, Tape
from analysis.volatility import true_range

BULL, BEAR, NEUTRAL = "BULL", "BEAR", "NEUTRAL"
ACCELERATING, DECELERATING, STEADY = "ACCELERATING", "DECELERATING", "STEADY"

# Declared intent. Renormalized over whatever is actually present.
# Intraday-primary, matching the options v3.1 correction: the frames that move a
# same-session decision carry the vote; the daily supplies context, not control.
TF_WEIGHTS: Dict[str, float] = {
    "1d": 0.15, "4h": 0.10, "1h": 0.20, "15m": 0.25, "5m": 0.30,
}
EMA_FAST, EMA_MID, EMA_SLOW = 8, 21, 50
ADX_PERIOD = 14
DIRECTION_GATE = 0.30          # weighted vote needed to call a direction


@dataclass
class TrendState:
    direction: str = NEUTRAL
    bull_score: float = 0.0
    bear_score: float = 0.0
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    momentum: str = STEADY
    roc_5: Optional[float] = None
    aligned_frames: int = 0
    voting_frames: List[str] = field(default_factory=list)
    missing_frames: List[str] = field(default_factory=list)
    per_frame: Dict[str, str] = field(default_factory=dict)
    warm: bool = False

    @property
    def strong(self) -> bool:
        return self.adx is not None and self.adx >= 25.0


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def adx(c: Candles, period: int = ADX_PERIOD):
    """Wilder's ADX/+DI/-DI. Returns (adx, +di, -di) or (None, None, None)."""
    if not c.has(period * 2 + 1):
        return None, None, None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(c)):
        up = c.high[i] - c.high[i - 1]
        dn = c.low[i - 1] - c.low[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(true_range(c, i))

    def wilder(seq: List[float]) -> List[float]:
        out = [sum(seq[:period])]
        for v in seq[period:]:
            out.append(out[-1] - out[-1] / period + v)
        return out

    str_, pdm, mdm = wilder(trs), wilder(plus_dm), wilder(minus_dm)
    dxs: List[float] = []
    for i in range(len(str_)):
        if str_[i] <= 0:
            continue
        p = 100.0 * pdm[i] / str_[i]
        m = 100.0 * mdm[i] / str_[i]
        s = p + m
        if s > 0:
            dxs.append(100.0 * abs(p - m) / s)
    if len(dxs) < period:
        return None, None, None
    a = sum(dxs[:period]) / period
    for d in dxs[period:]:
        a = (a * (period - 1) + d) / period
    denom = str_[-1] if str_[-1] > 0 else None
    pdi = 100.0 * pdm[-1] / denom if denom else None
    mdi = 100.0 * mdm[-1] / denom if denom else None
    return a, pdi, mdi


def frame_direction(c: Candles) -> str:
    """EMA stack on one timeframe. NEUTRAL when the stack is not ordered or the
    frame is too short — an honest abstention, which is why the renormalizer
    below must not treat NEUTRAL the same as ABSENT."""
    if not c or not c.has(EMA_SLOW):
        return NEUTRAL
    f, m, s = (ema(c.close, EMA_FAST), ema(c.close, EMA_MID), ema(c.close, EMA_SLOW))
    if None in (f, m, s):
        return NEUTRAL
    px = c.close[-1]
    if f > m > s and px > s:
        return BULL
    if f < m < s and px < s:
        return BEAR
    return NEUTRAL


def _renormalize(available: List[str]) -> Dict[str, float]:
    """Distribute the declared weights over the frames that exist. This is the
    whole fix: a missing 4h no longer removes 10% from the vote, it hands that
    10% to the frames that did report."""
    present = {tf: w for tf, w in TF_WEIGHTS.items() if tf in available}
    total = sum(present.values())
    if total <= 0:
        return {}
    return {tf: w / total for tf, w in present.items()}


def momentum_state(c5: Candles, lookback: int = 5) -> (str, Optional[float]):
    if not c5 or not c5.has(lookback * 2 + 1):
        return STEADY, None
    def roc(end: int) -> float:
        a, b = c5.close[end - lookback], c5.close[end]
        return (b - a) / a if a else 0.0
    now, prior = roc(len(c5) - 1), roc(len(c5) - 1 - lookback)
    if abs(now) > abs(prior) * 1.15:
        return ACCELERATING, now
    if abs(now) < abs(prior) * 0.85:
        return DECELERATING, now
    return STEADY, now


def analyze(tape: Tape) -> TrendState:
    st = TrendState()
    avail = tape.available(need=EMA_SLOW)
    st.voting_frames = [tf for tf in TF_WEIGHTS if tf in avail]
    st.missing_frames = [tf for tf in TF_WEIGHTS if tf not in avail]
    weights = _renormalize(st.voting_frames)
    if not weights:
        return st
    st.warm = True

    for tf, w in weights.items():
        d = frame_direction(tape.get(tf))
        st.per_frame[tf] = d
        if d == BULL:
            st.bull_score += w
        elif d == BEAR:
            st.bear_score += w
    st.aligned_frames = sum(1 for d in st.per_frame.values() if d != NEUTRAL)

    if st.bull_score >= DIRECTION_GATE and st.bull_score > st.bear_score:
        st.direction = BULL
    elif st.bear_score >= DIRECTION_GATE and st.bear_score > st.bull_score:
        st.direction = BEAR

    c5 = tape.get("5m")
    if c5:
        st.adx, st.plus_di, st.minus_di = adx(c5)
        st.momentum, st.roc_5 = momentum_state(c5)
    return st
