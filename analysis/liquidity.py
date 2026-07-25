"""
futures_trader_v1/analysis/liquidity.py — v0.1
v0.1 — 2026-07-25 — Initial build. The TIERED level map, with overnight
        high/low present from the first version.

BUILT DIRECTLY FROM THE options_trader_v3 OBSERVATION OF 2026-07-24
Two findings there, both fixed here at birth rather than retrofitted:

  1. NO OVERNIGHT HIGH/LOW EXISTED. The mapper knew PDH/PDL and the individual
     Asia/London/NY session extremes but had no explicit overnight high/low —
     one of the most-raided levels at the cash open. Real ON raids were either
     untagged or matched only some individual session extreme, which meant
     high-conviction sweeps were landing in the low-conviction bucket and
     corrupting the very sweep postmortem built to measure them.

  2. NAMED LEVELS WERE A FLAT BOOLEAN. `is_named` gave PDH and "Asia Low"
     identical weight. They are not identical. The tier is now a VALUE, not a
     flag — consistent with every other graded dimension in the system.

TIER ORDER (operator, 2026-07-24), highest to lowest conviction:
    Overnight H/L  ==  PDH/PDL     (top)
    Weekly H/L
    Historic S/R (multi-touch, multi-day)
    Naked POC
    Individual session H/L          (components, not headlines)
    Value-area edge
    Equal highs/lows                (lowest — self-defined)

Sweeps fire against these. The tier is the heaviest single input to the setup
score, so getting the ORDER right matters more than getting the values precise —
Epoch 2 calibrates the values against realized edge and will demote any tier
that does not out-perform the one beneath it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Dict, List, Optional

from data.series import Candles
from utils.sessions import ET, session_date, to_et

# Names must match config.LEVEL_TIERS keys.
OVERNIGHT_HIGH, OVERNIGHT_LOW = "OVERNIGHT_HIGH", "OVERNIGHT_LOW"
PDH, PDL = "PDH", "PDL"
WEEKLY_HIGH, WEEKLY_LOW = "WEEKLY_HIGH", "WEEKLY_LOW"
HISTORIC_SR = "HISTORIC_SR"
NAKED_POC = "NAKED_POC"
SESSION_HIGH, SESSION_LOW = "SESSION_HIGH", "SESSION_LOW"
VALUE_AREA_EDGE = "VALUE_AREA_EDGE"
EQUAL_HL = "EQUAL_HL"

ABOVE, BELOW = "ABOVE", "BELOW"


@dataclass
class Level:
    name: str
    price: float
    tier: float
    side: str = ""                 # ABOVE | BELOW, relative to current price
    touches: int = 1
    swept: bool = False
    detail: str = ""

    def distance_ticks(self, px: float, tick_size: float) -> float:
        return abs(self.price - px) / tick_size


@dataclass
class LiquidityMap:
    levels: List[Level] = field(default_factory=list)
    overnight_high: Optional[float] = None
    overnight_low: Optional[float] = None
    prior_high: Optional[float] = None
    prior_low: Optional[float] = None
    warm: bool = False

    def above(self) -> List[Level]:
        return sorted([l for l in self.levels if l.side == ABOVE], key=lambda l: l.price)

    def below(self) -> List[Level]:
        return sorted([l for l in self.levels if l.side == BELOW],
                      key=lambda l: l.price, reverse=True)

    def nearest(self, px: float, side: Optional[str] = None) -> Optional[Level]:
        pool = self.levels if side is None else [l for l in self.levels if l.side == side]
        return min(pool, key=lambda l: abs(l.price - px)) if pool else None

    def strongest_within(self, px: float, tick_size: float,
                         max_ticks: float) -> Optional[Level]:
        """The highest-tier level inside a tick window — what a sweep or an
        absorption trade asks for. Ties break to the closer level."""
        near = [l for l in self.levels
                if l.distance_ticks(px, tick_size) <= max_ticks]
        if not near:
            return None
        return max(near, key=lambda l: (l.tier, -l.distance_ticks(px, tick_size)))

    def in_path(self, entry: float, target: float) -> List[Level]:
        lo, hi = (entry, target) if entry <= target else (target, entry)
        return sorted([l for l in self.levels if lo < l.price < hi],
                      key=lambda l: abs(l.price - entry))


def _tier(name: str, tiers: Dict[str, float]) -> float:
    return tiers.get(name, 0.30)


def _session_slice(c: Candles, day: date, start: dtime, end: dtime) -> Candles:
    """Bars whose ET timestamp falls in [start, end) on the given calendar span.
    Handles a window that wraps midnight (the overnight span does)."""
    idx = []
    for i, t in enumerate(c.ts):
        et = to_et(t)
        tt = et.time()
        inside = (start <= tt < end) if start <= end else (tt >= start or tt < end)
        if inside:
            idx.append(i)
    if not idx:
        return Candles(c.tf)
    return Candles(c.tf, [c.ts[i] for i in idx], [c.open[i] for i in idx],
                   [c.high[i] for i in idx], [c.low[i] for i in idx],
                   [c.close[i] for i in idx], [c.volume[i] for i in idx])


def overnight_extremes(c: Candles, for_session: Optional[date] = None):
    """The overnight span = Globex open (18:00 ET) through the NY cash open
    (09:30 ET) — i.e. Asia plus London plus the pre-market. Derived from bars
    the feed already holds; nothing extra is subscribed.

    Its absence in the options mapper is the specific gap this function closes.
    """
    if not len(c):
        return None, None
    sess = for_session or session_date()
    idx = []
    for i, t in enumerate(c.ts):
        et = to_et(t)
        tt, d = et.time(), et.date()
        if tt >= dtime(18, 0) and d < sess:
            idx.append(i)
        elif tt < dtime(9, 30) and d == sess:
            idx.append(i)
    if not idx:
        return None, None
    return max(c.high[i] for i in idx), min(c.low[i] for i in idx)


def equal_levels(c: Candles, tick_size: float, tolerance_ticks: float = 2.0,
                 lookback: int = 120, min_touches: int = 2) -> List[Level]:
    """Self-defined equal highs/lows — the LOWEST tier, and deliberately so.
    They are where retail stops sit, which makes them real, but they are not
    structural in the way a prior-day extreme is."""
    out: List[Level] = []
    win = c.tail(lookback)
    if not len(win):
        return out
    for series, name in ((win.high, EQUAL_HL), (win.low, EQUAL_HL)):
        used = [False] * len(series)
        for i in range(len(series)):
            if used[i]:
                continue
            cluster = [series[i]]
            for j in range(i + 1, len(series)):
                if used[j]:
                    continue
                if abs(series[j] - series[i]) / tick_size <= tolerance_ticks:
                    cluster.append(series[j])
                    used[j] = True
            if len(cluster) >= min_touches:
                out.append(Level(name, sum(cluster) / len(cluster), 0.0,
                                 touches=len(cluster), detail=f"{len(cluster)} touches"))
    return out


def build(c1: Candles,
          tick_size: float,
          tiers: Dict[str, float],
          prior_high: Optional[float] = None,
          prior_low: Optional[float] = None,
          weekly_high: Optional[float] = None,
          weekly_low: Optional[float] = None,
          naked_pocs: Optional[List[float]] = None,
          value_area: Optional[tuple] = None,
          for_session: Optional[date] = None) -> LiquidityMap:
    m = LiquidityMap()
    if not c1 or not len(c1):
        return m
    m.warm = True
    px = c1.last

    m.overnight_high, m.overnight_low = overnight_extremes(c1, for_session)
    m.prior_high, m.prior_low = prior_high, prior_low

    def add(name: str, price: Optional[float], detail: str = "", touches: int = 1):
        if price is None:
            return
        m.levels.append(Level(name, price, _tier(name, tiers),
                              ABOVE if price >= px else BELOW, touches, False, detail))

    add(OVERNIGHT_HIGH, m.overnight_high, "Globex 18:00 -> 09:30 ET")
    add(OVERNIGHT_LOW, m.overnight_low, "Globex 18:00 -> 09:30 ET")
    add(PDH, prior_high, "prior session")
    add(PDL, prior_low, "prior session")
    add(WEEKLY_HIGH, weekly_high, "prior week")
    add(WEEKLY_LOW, weekly_low, "prior week")
    for p in (naked_pocs or []):
        add(NAKED_POC, p, "untouched POC")
    if value_area:
        add(VALUE_AREA_EDGE, value_area[0], "VAH")
        add(VALUE_AREA_EDGE, value_area[1], "VAL")

    # today's own session extremes — components, below the headlines
    sess = _session_slice(c1, for_session or session_date(), dtime(9, 30), dtime(16, 0))
    if len(sess):
        add(SESSION_HIGH, max(sess.high), "RTH so far")
        add(SESSION_LOW, min(sess.low), "RTH so far")

    for lv in equal_levels(c1, tick_size):
        lv.tier = _tier(EQUAL_HL, tiers)
        lv.side = ABOVE if lv.price >= px else BELOW
        m.levels.append(lv)

    # de-duplicate: when two names land within a tick of each other the HIGHER
    # tier wins. An overnight high that equals the session high is an overnight
    # high, not a session high, and mislabeling it is exactly the corruption
    # the 07-24 observation described.
    m.levels.sort(key=lambda l: (-l.tier, l.price))
    kept: List[Level] = []
    for lv in m.levels:
        if not any(abs(k.price - lv.price) / tick_size <= 1.0 for k in kept):
            kept.append(lv)
    m.levels = kept
    return m
