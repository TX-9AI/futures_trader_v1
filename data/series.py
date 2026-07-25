"""
futures_trader_v1/data/series.py — v0.1
v0.1 — 2026-07-25 — Initial build. The tape container every analysis module
        consumes, and the one contract they all share.

WHY THIS IS STDLIB-ONLY AND NOT A DataFrame
The options fleet was burned repeatedly by "wrong venv / no pandas / no pytest
on the box" — enough that the deploy gate had to become an `ast.parse` syntax
check instead of a test run. An analysis stack whose primitives need pandas can
only be verified where pandas is installed. These are plain lists, so every
engine below can be exercised on any box, in any venv, with `python3` alone.

`from_rows()` adapts whatever the feed produces. If a pandas frame shows up
later, it is converted at the boundary — once — instead of the whole analysis
layer inheriting the dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional, Sequence


@dataclass
class Candles:
    """OHLCV for one timeframe, oldest -> newest. `ts` is timezone-aware ET."""
    tf: str
    ts: List[datetime] = field(default_factory=list)
    open: List[float] = field(default_factory=list)
    high: List[float] = field(default_factory=list)
    low: List[float] = field(default_factory=list)
    close: List[float] = field(default_factory=list)
    volume: List[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.close)

    def __bool__(self) -> bool:
        return len(self.close) > 0

    @property
    def last(self) -> Optional[float]:
        return self.close[-1] if self.close else None

    def tail(self, n: int) -> "Candles":
        n = max(0, n)
        return Candles(self.tf, self.ts[-n:], self.open[-n:], self.high[-n:],
                       self.low[-n:], self.close[-n:], self.volume[-n:])

    def slice(self, start: int, end: Optional[int] = None) -> "Candles":
        e = end if end is not None else len(self)
        return Candles(self.tf, self.ts[start:e], self.open[start:e], self.high[start:e],
                       self.low[start:e], self.close[start:e], self.volume[start:e])

    def has(self, n: int) -> bool:
        """Enough bars to compute something honest. Every engine calls this
        rather than indexing hopefully — a starved timeframe must produce a
        stated absence, never a number derived from three bars."""
        return len(self) >= n

    def typical(self, i: int) -> float:
        return (self.high[i] + self.low[i] + self.close[i]) / 3.0

    def is_up(self, i: int) -> bool:
        return self.close[i] >= self.open[i]

    def body_high(self, i: int) -> float:
        return max(self.open[i], self.close[i])

    def body_low(self, i: int) -> float:
        return min(self.open[i], self.close[i])

    def range_(self, i: int) -> float:
        return self.high[i] - self.low[i]

    @classmethod
    def from_rows(cls, tf: str, rows: Iterable[Sequence]) -> "Candles":
        """rows: (ts, open, high, low, close, volume), oldest first."""
        c = cls(tf)
        for r in rows:
            c.ts.append(r[0]); c.open.append(float(r[1])); c.high.append(float(r[2]))
            c.low.append(float(r[3])); c.close.append(float(r[4]))
            c.volume.append(float(r[5]) if len(r) > 5 and r[5] is not None else 0.0)
        return c

    @classmethod
    def from_dataframe(cls, tf: str, df) -> "Candles":
        """Boundary adapter. The ONLY place a frame is touched."""
        cols = {c.lower(): c for c in df.columns}
        return cls.from_rows(tf, zip(
            list(df.index),
            df[cols.get("open", "open")], df[cols.get("high", "high")],
            df[cols.get("low", "low")], df[cols.get("close", "close")],
            df[cols.get("volume", "volume")] if "volume" in cols else [0.0] * len(df)))


@dataclass
class Tape:
    """Every timeframe for one symbol at one instant, plus which ones are real.

    `available()` exists because of the options trend-engine defect: the vote
    assigned 25% of its weight to a 4-hour timeframe the live feed never
    produced, so a quarter of the weight silently evaporated and TRENDING became
    unreachable — 0 occurrences in 34,925 replay ticks. Weight is now only ever
    distributed over timeframes that actually returned bars.
    """
    frames: dict = field(default_factory=dict)     # tf -> Candles
    as_of: Optional[datetime] = None
    stale: bool = False

    def get(self, tf: str) -> Optional[Candles]:
        c = self.frames.get(tf)
        return c if (c and len(c)) else None

    def available(self, need: int = 1) -> List[str]:
        return [tf for tf, c in self.frames.items() if c and c.has(need)]

    def put(self, c: Candles) -> None:
        self.frames[c.tf] = c
