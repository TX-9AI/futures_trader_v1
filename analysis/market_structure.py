"""
futures_trader_v1/analysis/market_structure.py — v0.1
v0.1 — 2026-07-25 — Initial build. Swings, BOS/CHoCH, fair value gaps, order
        blocks, and the dealing range / premium-discount position.

The SMC vocabulary, made mechanical. Every construct here is a geometric fact
about closed candles — no discretion, no redrawing, and no percentage of price
anywhere: distances are ticks or ATR multiples so the same code behaves
identically on a $0.005-tick silver contract and a $1-tick Dow.

BOS vs CHoCH is the distinction that carries the information:
  BOS   — structure broken IN the prevailing direction. Continuation.
  CHoCH — the first break AGAINST it. The earliest honest reversal signal.
A system that calls both "a break" throws away the half that matters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from data.series import Candles

HIGH, LOW = "HIGH", "LOW"
BULL, BEAR, NEUTRAL = "BULL", "BEAR", "NEUTRAL"
BOS, CHOCH = "BOS", "CHoCH"


@dataclass
class Swing:
    kind: str            # HIGH | LOW
    index: int
    price: float
    ts: object = None


@dataclass
class FVG:
    """Three-candle imbalance. Bullish: low[i] > high[i-2] — price skipped a
    zone nobody traded. `filled` is tracked so a trail can park against the
    NEAREST UNFILLED gap rather than a historical one."""
    direction: str
    top: float
    bottom: float
    index: int
    filled: bool = False

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    def contains(self, px: float) -> bool:
        return self.bottom <= px <= self.top


@dataclass
class OrderBlock:
    """The last opposing candle before a displacement leg. Bullish OB = last
    down-close candle before an up-move that broke structure."""
    direction: str
    top: float
    bottom: float
    index: int
    displaced_ticks: float = 0.0
    mitigated: bool = False


@dataclass
class StructureState:
    swings: List[Swing] = field(default_factory=list)
    bias: str = NEUTRAL
    last_break: Optional[str] = None          # BOS | CHoCH
    last_break_direction: str = NEUTRAL
    last_break_index: Optional[int] = None
    fvgs: List[FVG] = field(default_factory=list)
    order_blocks: List[OrderBlock] = field(default_factory=list)
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    pd_position: Optional[float] = None       # 0.0 = range low, 1.0 = range high
    warm: bool = False

    def unfilled_fvgs(self, direction: str) -> List[FVG]:
        return [f for f in self.fvgs if f.direction == direction and not f.filled]

    def nearest_fvg(self, px: float, direction: str) -> Optional[FVG]:
        cands = self.unfilled_fvgs(direction)
        return min(cands, key=lambda f: abs(f.mid - px)) if cands else None

    @property
    def in_discount(self) -> Optional[bool]:
        return None if self.pd_position is None else self.pd_position < 0.5

    @property
    def in_premium(self) -> Optional[bool]:
        return None if self.pd_position is None else self.pd_position > 0.5


def find_swings(c: Candles, strength: int = 2) -> List[Swing]:
    """Fractal pivots: a high with `strength` lower highs on each side.
    Deliberately simple and deterministic — an anchor rule that redraws is worse
    than a crude one that does not."""
    out: List[Swing] = []
    if not c.has(strength * 2 + 1):
        return out
    for i in range(strength, len(c) - strength):
        left = range(i - strength, i)
        right = range(i + 1, i + strength + 1)
        if all(c.high[i] > c.high[j] for j in left) and \
           all(c.high[i] >= c.high[j] for j in right):
            out.append(Swing(HIGH, i, c.high[i], c.ts[i] if c.ts else None))
        if all(c.low[i] < c.low[j] for j in left) and \
           all(c.low[i] <= c.low[j] for j in right):
            out.append(Swing(LOW, i, c.low[i], c.ts[i] if c.ts else None))
    return sorted(out, key=lambda s: s.index)


def find_fvgs(c: Candles, tick_size: float, min_ticks: float = 1.0) -> List[FVG]:
    """Gaps smaller than `min_ticks` are quote noise, not imbalance. Measured in
    TICKS, never in percent."""
    out: List[FVG] = []
    if not c.has(3):
        return out
    for i in range(2, len(c)):
        if c.low[i] > c.high[i - 2]:
            gap = c.low[i] - c.high[i - 2]
            if gap / tick_size >= min_ticks:
                out.append(FVG(BULL, c.low[i], c.high[i - 2], i))
        elif c.high[i] < c.low[i - 2]:
            gap = c.low[i - 2] - c.high[i]
            if gap / tick_size >= min_ticks:
                out.append(FVG(BEAR, c.low[i - 2], c.high[i], i))
    # mark fills using subsequent trade
    for f in out:
        for j in range(f.index + 1, len(c)):
            if f.direction == BULL and c.low[j] <= f.bottom:
                f.filled = True
                break
            if f.direction == BEAR and c.high[j] >= f.top:
                f.filled = True
                break
    return out


def find_order_blocks(c: Candles, tick_size: float,
                      min_displacement_ticks: float = 8.0) -> List[OrderBlock]:
    out: List[OrderBlock] = []
    if not c.has(4):
        return out
    for i in range(1, len(c) - 2):
        leg = c.close[i + 2] - c.close[i]
        ticks = abs(leg) / tick_size
        if ticks < min_displacement_ticks:
            continue
        if leg > 0 and not c.is_up(i):
            ob = OrderBlock(BULL, c.high[i], c.low[i], i, ticks)
        elif leg < 0 and c.is_up(i):
            ob = OrderBlock(BEAR, c.high[i], c.low[i], i, ticks)
        else:
            continue
        for j in range(i + 3, len(c)):
            if ob.direction == BULL and c.low[j] <= ob.top:
                ob.mitigated = True
                break
            if ob.direction == BEAR and c.high[j] >= ob.bottom:
                ob.mitigated = True
                break
        out.append(ob)
    return out


def _classify_break(swings: List[Swing], c: Candles) -> Tuple[Optional[str], str, Optional[int]]:
    """Walk the swing sequence and label the most recent structural break.

    Uses CLOSES, never wicks — a wick through a swing is a raid, and calling it
    a break is how a sweep gets traded as a breakout.
    """
    highs = [s for s in swings if s.kind == HIGH]
    lows = [s for s in swings if s.kind == LOW]
    if not highs or not lows:
        return None, NEUTRAL, None

    bias = NEUTRAL
    last_kind, last_dir, last_idx = None, NEUTRAL, None
    ref_high, ref_low = highs[0], lows[0]

    for i in range(min(ref_high.index, ref_low.index) + 1, len(c)):
        px = c.close[i]
        if px > ref_high.price:
            last_kind = BOS if bias in (BULL, NEUTRAL) else CHOCH
            last_dir, last_idx, bias = BULL, i, BULL
            nxt = [s for s in highs if s.index > i]
            if nxt:
                ref_high = nxt[0]
            nl = [s for s in lows if s.index < i]
            if nl:
                ref_low = nl[-1]
        elif px < ref_low.price:
            last_kind = BOS if bias in (BEAR, NEUTRAL) else CHOCH
            last_dir, last_idx, bias = BEAR, i, BEAR
            nxt = [s for s in lows if s.index > i]
            if nxt:
                ref_low = nxt[0]
            nh = [s for s in highs if s.index < i]
            if nh:
                ref_high = nh[-1]
    return last_kind, last_dir, last_idx


def analyze(c: Candles, tick_size: float,
            swing_strength: int = 2,
            range_lookback: int = 60,
            fvg_min_ticks: float = 1.0,
            ob_min_ticks: float = 8.0) -> StructureState:
    st = StructureState()
    if not c or not c.has(swing_strength * 2 + 3):
        return st
    st.warm = True
    st.swings = find_swings(c, swing_strength)
    st.fvgs = find_fvgs(c, tick_size, fvg_min_ticks)
    st.order_blocks = find_order_blocks(c, tick_size, ob_min_ticks)

    kind, direction, idx = _classify_break(st.swings, c)
    st.last_break, st.last_break_direction, st.last_break_index = kind, direction, idx
    st.bias = direction

    win = c.tail(range_lookback)
    if len(win):
        st.range_high, st.range_low = max(win.high), min(win.low)
        span = st.range_high - st.range_low
        if span > 0:
            st.pd_position = (c.last - st.range_low) / span
    return st
