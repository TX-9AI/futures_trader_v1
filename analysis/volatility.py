"""
futures_trader_v1/analysis/volatility.py — v0.1
v0.1 — 2026-07-25 — Initial build. ATR, Bollinger, session VWAP, expansion
        state. Every divisor guarded.

BOLLINGER WIDTH IS A PERCENTILE, NOT A RATIO — see width_percentile().

THE ZERO-VOLUME LESSON, BUILT IN FROM THE FIRST LINE
The options engine computed VWAP as (typical*volume).cumsum()/volume.cumsum().
On a cash index the feed reported volume=0, so 0/0 produced NaN — a numpy
RuntimeWarning, not an exception, so the try/except never fired. `price >= NaN`
is always False, which silently pinned the VWAP signal to "BELOW" for an entire
session and handed one side of the book an unearned confirmation.

Here VWAP returns None when cumulative volume is zero, and `price_vs_vwap` is
the string "NONE". Absence is a stated value, never a number that happens to
compare False. Futures always print real volume, so this should never trigger —
which is exactly why it must be tested rather than assumed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from data.series import Candles

ABOVE, BELOW, NONE = "ABOVE", "BELOW", "NONE"
EXPANDING, CONTRACTING, STEADY = "EXPANDING", "CONTRACTING", "STEADY"
MIN_WIDTH_HISTORY = 10        # samples needed before a width percentile means anything


@dataclass
class VolState:
    atr: Optional[float] = None
    atr_avg: Optional[float] = None
    expansion_ratio: Optional[float] = None
    expansion: str = STEADY
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_width_abs: Optional[float] = None      # upper - lower, in price
    bb_width_pct: Optional[float] = None      # PERCENTILE RANK (0..1) of the current
                                              # width within its own recent history
    vwap: Optional[float] = None
    vwap_upper: Optional[float] = None
    vwap_lower: Optional[float] = None
    price_vs_vwap: str = NONE
    inside_bands: Optional[bool] = None
    warm: bool = False                        # enough bars for the numbers to mean anything

    @property
    def usable(self) -> bool:
        return self.warm and self.atr is not None and self.atr > 0


def true_range(c: Candles, i: int) -> float:
    if i == 0:
        return c.high[0] - c.low[0]
    pc = c.close[i - 1]
    return max(c.high[i] - c.low[i], abs(c.high[i] - pc), abs(c.low[i] - pc))


def atr(c: Candles, period: int = 14) -> Optional[float]:
    """Wilder's ATR. Returns None rather than a short-sample average — a
    half-warm ATR is the denominator of every stop and every regime ramp."""
    if not c.has(period + 1):
        return None
    trs = [true_range(c, i) for i in range(1, len(c))]
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def atr_series(c: Candles, period: int = 14, back: int = 20) -> List[float]:
    out: List[float] = []
    for k in range(back):
        end = len(c) - k
        if end < period + 1:
            break
        v = atr(c.slice(0, end), period)
        if v is not None:
            out.append(v)
    return list(reversed(out))


def bollinger(c: Candles, period: int = 20, std_mult: float = 2.0):
    if not c.has(period):
        return None, None, None, None
    win = c.close[-period:]
    mid = sum(win) / period
    var = sum((x - mid) ** 2 for x in win) / period
    sd = math.sqrt(var)
    up, lo = mid + std_mult * sd, mid - std_mult * sd
    return up, mid, lo, (up - lo)


def bb_width_series(c: Candles, period: int = 20, std_mult: float = 2.0,
                    back: int = 50) -> List[float]:
    out: List[float] = []
    for k in range(back):
        end = len(c) - k
        if end < period:
            break
        _, _, _, w = bollinger(c.slice(0, end), period, std_mult)
        if w is not None:
            out.append(w)
    return list(reversed(out))


def width_percentile(history: List[float], current: float) -> Optional[float]:
    """Rank the current Bollinger width WITHIN ITS OWN RECENT HISTORY.

    This is the unit the ported L1 ramp bounds expect, and the reason a single
    dial works across a $0.005-tick silver contract and a $1-tick Dow: a
    percentile is unitless and self-normalising, so "narrow" means narrow FOR
    THIS INSTRUMENT RIGHT NOW rather than narrow in absolute points.

    CONSEQUENCE WORTH KNOWING: the measure is RELATIVE, so a squeeze that
    persists long enough to fill its own lookback stops ranking as compressed.
    That is correct — a permanently quiet market is not compressing, it is just
    quiet — but it means COMPRESSION evidence naturally fades on a long coil
    rather than accumulating. Do not "fix" that without a reason.

    A width-to-price ratio — the obvious implementation — is NOT the same
    quantity and would silently invalidate every inherited bound: an index BB
    is a fraction of a percent of spot, so a RANGE_ROOM floor of 0.17 could
    never be cleared and BALANCED would score zero forever. Caught by
    tests/test_analysis.py before it could reach a box."""
    if len(history) < MIN_WIDTH_HISTORY:
        # Too little history to rank against. Return None rather than a number:
        # with only a handful of samples the percentile is dominated by the
        # sample itself (a constant-width tape ranks its own width at 1.00),
        # and a confident-looking 1.00 would feed the L1 room ramp as "maximum
        # room to oscillate" on what may be a dead-flat coil.
        return None
    below = sum(1 for w in history if w <= current)
    return below / len(history)


def session_vwap(c: Candles, bars: Optional[int] = None):
    """Volume-weighted average price plus a 1-sigma band.

    Returns (vwap, upper, lower) or (None, None, None) when cumulative volume
    is zero. The guard is the point — see the module docstring."""
    src = c.tail(bars) if bars else c
    if not len(src):
        return None, None, None
    cum_v = sum(src.volume)
    if cum_v <= 0:
        return None, None, None
    cum_pv = sum(src.typical(i) * src.volume[i] for i in range(len(src)))
    vw = cum_pv / cum_v
    if not math.isfinite(vw):
        return None, None, None
    var = sum(src.volume[i] * (src.typical(i) - vw) ** 2 for i in range(len(src))) / cum_v
    sd = math.sqrt(var) if var > 0 else 0.0
    return vw, vw + sd, vw - sd


def analyze(c5: Candles,
            atr_period: int = 14,
            bb_period: int = 20,
            bb_std: float = 2.0,
            session_bars: Optional[int] = None) -> VolState:
    s = VolState()
    if not c5 or not c5.has(max(atr_period + 1, bb_period)):
        return s
    s.warm = True
    s.atr = atr(c5, atr_period)
    hist = atr_series(c5, atr_period, back=20)
    if hist:
        s.atr_avg = sum(hist) / len(hist)
    if s.atr and s.atr_avg and s.atr_avg > 0:
        s.expansion_ratio = s.atr / s.atr_avg
        s.expansion = (EXPANDING if s.expansion_ratio >= 1.25 else
                       CONTRACTING if s.expansion_ratio <= 0.80 else STEADY)
    s.bb_upper, s.bb_middle, s.bb_lower, s.bb_width_abs = bollinger(c5, bb_period, bb_std)
    if s.bb_width_abs is not None:
        s.bb_width_pct = width_percentile(
            bb_width_series(c5, bb_period, bb_std, back=50), s.bb_width_abs)
    s.vwap, s.vwap_upper, s.vwap_lower = session_vwap(c5, session_bars)
    px = c5.last
    if s.vwap is not None and px is not None:
        s.price_vs_vwap = ABOVE if px >= s.vwap else BELOW
    if s.bb_upper is not None and px is not None:
        s.inside_bands = s.bb_lower <= px <= s.bb_upper
    return s
