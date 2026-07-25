"""
futures_trader_v1/analysis/orderflow.py — v0.1
v0.1 — 2026-07-25 — Initial build. Cumulative delta, delta divergence,
        absorption.

THE SIGNAL THE OPTIONS ENGINE COULD NOT SEE
An option chain tells you what dealers are positioned for. Tick data with an
aggressor side tells you who is actually paying up, right now, at this price.
That is a different and more immediate class of information, and it is the main
reason futures is worth building rather than porting.

Three constructs, in ascending order of usefulness:

  CVD          cumulative signed volume. Direction of aggression.
  DIVERGENCE   a new price extreme on WEAKER cumulative delta. The push is
               being made by fewer/smaller aggressors than the last one — the
               classic exhaustion tell, and the confirmation the sweep trade
               was missing when it ran 75% wins to a negative book.
  ABSORPTION   delta pushes hard, price does NOT move. Someone large is filling
               passively against the aggression. This is the scalp trade's
               entire premise and it is invisible without tick data.

DEGRADED MODE IS EXPLICIT. If the feed supplies no aggressor side, delta is
approximated from bar shape (close position within the range × volume) and
`approximated=True` is set on the state. Every consumer must check it: an
approximated CVD is a weak proxy and must never be scored as if it were real.
Silent degradation is how a false signal becomes a live gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from data.series import Candles

BUY, SELL = "BUY", "SELL"
BULLISH_DIV, BEARISH_DIV, NO_DIV = "BULLISH_DIV", "BEARISH_DIV", "NO_DIV"


@dataclass
class Trade:
    """One print. `aggressor` is BUY when it lifted the offer."""
    ts: object
    price: float
    size: float
    aggressor: str


@dataclass
class FlowState:
    cvd: float = 0.0
    cvd_series: List[float] = field(default_factory=list)
    bar_delta: List[float] = field(default_factory=list)
    divergence: str = NO_DIV
    divergence_strength: float = 0.0
    absorption: bool = False
    absorption_side: str = ""
    approximated: bool = True
    warm: bool = False

    @property
    def bias(self) -> str:
        return BUY if self.cvd > 0 else SELL if self.cvd < 0 else ""


def delta_from_trades(trades: Sequence[Trade]) -> float:
    return sum(t.size if t.aggressor == BUY else -t.size for t in trades)


def approximate_bar_delta(c: Candles, i: int) -> float:
    """Fallback when no aggressor side exists: where the bar closed within its
    own range, scaled by volume. Range +1 (all buying) to -1 (all selling).
    A PROXY, and labelled as one."""
    rng = c.high[i] - c.low[i]
    if rng <= 0:
        return 0.0
    pos = (c.close[i] - c.low[i]) / rng
    return (pos * 2.0 - 1.0) * c.volume[i]


def build(c: Candles,
          bar_trades: Optional[Sequence[Sequence[Trade]]] = None,
          lookback: int = 20) -> FlowState:
    """bar_trades[i] holds the prints inside candle i when tick data exists."""
    st = FlowState()
    if not c or not len(c):
        return st
    st.warm = True
    st.approximated = bar_trades is None

    for i in range(len(c)):
        d = (delta_from_trades(bar_trades[i]) if bar_trades and i < len(bar_trades)
             else approximate_bar_delta(c, i))
        st.bar_delta.append(d)
        st.cvd += d
        st.cvd_series.append(st.cvd)

    st.divergence, st.divergence_strength = detect_divergence(c, st, lookback)
    st.absorption, st.absorption_side = detect_absorption(c, st)
    return st


def detect_divergence(c: Candles, st: FlowState,
                      lookback: int = 20) -> Tuple[str, float]:
    """A new price extreme reached on weaker cumulative delta.

    Compares the most recent extreme against the prior extreme inside the
    window — not against an arbitrary bar — so the comparison is between two
    comparable pushes.
    """
    n = min(lookback, len(c))
    if n < 6 or len(st.cvd_series) < n:
        return NO_DIV, 0.0
    hi = c.high[-n:]
    lo = c.low[-n:]
    cvd = st.cvd_series[-n:]
    half = n // 2

    i_recent_hi = half + max(range(n - half), key=lambda k: hi[half + k])
    i_prior_hi = max(range(half), key=lambda k: hi[k])
    if hi[i_recent_hi] > hi[i_prior_hi] and cvd[i_recent_hi] < cvd[i_prior_hi]:
        span = abs(cvd[i_prior_hi]) or 1.0
        return BEARISH_DIV, min(1.0, abs(cvd[i_prior_hi] - cvd[i_recent_hi]) / span)

    i_recent_lo = half + min(range(n - half), key=lambda k: lo[half + k])
    i_prior_lo = min(range(half), key=lambda k: lo[k])
    if lo[i_recent_lo] < lo[i_prior_lo] and cvd[i_recent_lo] > cvd[i_prior_lo]:
        span = abs(cvd[i_prior_lo]) or 1.0
        return BULLISH_DIV, min(1.0, abs(cvd[i_recent_lo] - cvd[i_prior_lo]) / span)

    return NO_DIV, 0.0


def detect_absorption(c: Candles, st: FlowState,
                      bars: int = 3,
                      delta_ratio: float = 1.5,
                      max_progress_atr: float = 0.35,
                      atr: Optional[float] = None) -> Tuple[bool, str]:
    """Strong one-sided delta with no price progress.

    Progress is measured against ATR when available and against the bars' own
    average range otherwise — never against a percentage of price.
    """
    if len(c) < bars + 1 or len(st.bar_delta) < bars:
        return False, ""
    recent = st.bar_delta[-bars:]
    net = sum(recent)
    gross = sum(abs(d) for d in recent)
    if gross <= 0 or abs(net) / gross < 0.6:
        return False, ""
    hist = st.bar_delta[-(bars * 4):-bars] or [0.0]
    avg_abs = sum(abs(d) for d in hist) / len(hist) or 1.0
    if abs(net) / (avg_abs * bars) < delta_ratio:
        return False, ""
    ref = atr if (atr and atr > 0) else (
        sum(c.range_(i) for i in range(len(c) - bars, len(c))) / bars)
    if ref <= 0:
        return False, ""
    progress = abs(c.close[-1] - c.close[-1 - bars])
    if progress > ref * max_progress_atr:
        return False, ""
    return True, (BUY if net > 0 else SELL)
