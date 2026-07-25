"""
futures_trader_v1/analysis/profile.py — v0.1
v0.1 — 2026-07-25 — Initial build. Volume profile: POC, value area, naked POCs,
        and value migration.

Market profile is the futures-native context the options engine had no analogue
for. Where that system asked "is price inside the Bollinger band", this asks
"where is price relative to where business was actually done" — which is the
question a futures auction answers.

Three outputs earn their place:
  POC        the price with the most volume — a magnet, and a target
  VALUE AREA the central 70% of volume — inside it is rotation, outside it is
             either acceptance (a new auction) or rejection (a fade)
  MIGRATION  today's value vs yesterday's — overlapping value is balance,
             displaced value is trend. This is the single best regime input
             futures offers and it feeds L1 at weight 0 pending calibration.

NAKED POCs (a prior session's POC never revisited) are carried into the
liquidity map as their own tier: unfinished business the auction tends to
return to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from data.series import Candles

OVERLAPPING, HIGHER, LOWER = "OVERLAPPING", "HIGHER", "LOWER"
INSIDE, ABOVE_VALUE, BELOW_VALUE = "INSIDE", "ABOVE_VALUE", "BELOW_VALUE"


@dataclass
class Profile:
    poc: Optional[float] = None
    vah: Optional[float] = None
    val: Optional[float] = None
    total_volume: float = 0.0
    bins: Dict[float, float] = field(default_factory=dict)
    warm: bool = False

    @property
    def value_width(self) -> Optional[float]:
        if self.vah is None or self.val is None:
            return None
        return self.vah - self.val

    def position(self, px: float) -> str:
        if self.vah is None:
            return INSIDE
        if px > self.vah:
            return ABOVE_VALUE
        if px < self.val:
            return BELOW_VALUE
        return INSIDE


@dataclass
class ProfileState:
    today: Profile = field(default_factory=Profile)
    prior: Optional[Profile] = None
    migration: str = OVERLAPPING
    overlap_pct: Optional[float] = None
    price_position: str = INSIDE
    naked_pocs: List[float] = field(default_factory=list)
    warm: bool = False

    @property
    def balanced(self) -> bool:
        """Balance = value overlapping AND price inside it. The precondition for
        a fade; its absence is the abort condition."""
        return self.migration == OVERLAPPING and self.price_position == INSIDE


def build_profile(c: Candles, tick_size: float,
                  bin_ticks: int = 4,
                  value_pct: float = 0.70) -> Profile:
    """Volume distributed across each bar's range, binned to `bin_ticks`.

    Distributing a bar's volume across its whole range (rather than dumping it
    at the close) is the honest approximation available from OHLCV. True TPO or
    footprint needs tick data — which the order-flow module collects, and which
    can replace this later without changing the interface.
    """
    p = Profile()
    if not c or not len(c):
        return p
    size = tick_size * bin_ticks
    if size <= 0:
        return p
    p.warm = True
    for i in range(len(c)):
        lo, hi, vol = c.low[i], c.high[i], c.volume[i]
        if vol <= 0:
            continue
        n = max(1, int(round((hi - lo) / size)) + 1)
        share = vol / n
        for k in range(n):
            b = round((lo + k * size) / size) * size
            p.bins[b] = p.bins.get(b, 0.0) + share
    if not p.bins:
        return p
    p.total_volume = sum(p.bins.values())
    p.poc = max(p.bins, key=lambda b: p.bins[b])

    # value area: expand from the POC, always taking the richer adjacent bin
    ordered = sorted(p.bins)
    i = ordered.index(p.poc)
    lo_i = hi_i = i
    acc = p.bins[p.poc]
    target = p.total_volume * value_pct
    while acc < target and (lo_i > 0 or hi_i < len(ordered) - 1):
        below = p.bins[ordered[lo_i - 1]] if lo_i > 0 else -1.0
        above = p.bins[ordered[hi_i + 1]] if hi_i < len(ordered) - 1 else -1.0
        if above >= below:
            hi_i += 1
            acc += above
        else:
            lo_i -= 1
            acc += below
    p.val, p.vah = ordered[lo_i], ordered[hi_i]
    return p


def _overlap(a: Profile, b: Profile) -> Optional[float]:
    if None in (a.val, a.vah, b.val, b.vah):
        return None
    lo, hi = max(a.val, b.val), min(a.vah, b.vah)
    inter = max(0.0, hi - lo)
    union = max(a.vah, b.vah) - min(a.val, b.val)
    return inter / union if union > 0 else None


def analyze(today: Candles, tick_size: float,
            prior: Optional[Candles] = None,
            prior_profiles: Optional[List[Profile]] = None,
            bin_ticks: int = 4) -> ProfileState:
    st = ProfileState()
    st.today = build_profile(today, tick_size, bin_ticks)
    if not st.today.warm:
        return st
    st.warm = True
    px = today.last
    st.price_position = st.today.position(px)

    if prior is not None and len(prior):
        st.prior = build_profile(prior, tick_size, bin_ticks)
        st.overlap_pct = _overlap(st.today, st.prior)
        if st.overlap_pct is not None:
            if st.overlap_pct >= 0.40:
                st.migration = OVERLAPPING
            elif st.today.poc > st.prior.poc:
                st.migration = HIGHER
            else:
                st.migration = LOWER

    # naked POCs: a prior POC price never traded through since
    for pp in (prior_profiles or []):
        if pp.poc is None:
            continue
        touched = any(today.low[i] <= pp.poc <= today.high[i] for i in range(len(today)))
        if not touched:
            st.naked_pocs.append(pp.poc)
    return st
