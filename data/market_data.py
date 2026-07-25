"""
futures_trader_v1/data/market_data.py — v0.2
v0.2 — 2026-07-25 — staleness compares >= so a ceiling of 0 fails closed.
v0.1 — 2026-07-25 — Initial build. The pure READER over the shared store.

Nothing downstream of this file knows what a broker is. The analysis stack takes
`Candles` and `Tape`; this is the only place the store's row format is touched.
That boundary is why the whole analysis layer could be written and tested before
a broker existed, and why swapping the feed later changes exactly one file.

STALENESS IS AN ANSWER, NOT AN EXCEPTION. Past FEED_STALE_SECONDS every read
returns None with `stale=True` on the Tape. A caller that ignores it gets no
data rather than old data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import config as C
from data.feed_store import FeedStore
from data.series import Candles, Tape
from utils.sessions import ET

logger = logging.getLogger(__name__)


class MarketData:
    def __init__(self, store_path: Optional[str] = None,
                 stale_seconds: Optional[float] = None):
        self.store = FeedStore(store_path or C.CANDLE_STORE, read_only=True)
        self.stale_seconds = (stale_seconds if stale_seconds is not None
                              else C.FEED_STALE_SECONDS)
        self._warned = False

    # ── health ───────────────────────────────────────────────────────────────
    def healthy(self) -> (bool, str):
        age = self.store.heartbeat_age("feed")
        if age is None:
            return False, "no feed heartbeat — producer never ran"
        # >= not >, so a ceiling of 0 means "nothing is fresh enough" rather
        # than silently disabling the guard. A staleness check that can be
        # switched off by a zero is a staleness check that will be.
        if age >= self.stale_seconds:
            return False, f"feed heartbeat {age:.0f}s old (ceiling {self.stale_seconds:.0f}s)"
        return True, f"feed alive ({age:.0f}s)"

    # ── reads ────────────────────────────────────────────────────────────────
    def candles(self, symbol: str, tf: str, limit: int = 400) -> Optional[Candles]:
        ok, why = self.healthy()
        if not ok:
            if not self._warned:
                logger.warning("market data unavailable: %s", why)
                self._warned = True
            return None
        self._warned = False
        rows = self.store.fetch_candles(symbol, tf, limit)
        if not rows:
            return None
        return Candles.from_rows(tf, [
            (datetime.fromtimestamp(r[0], tz=timezone.utc).astimezone(ET),
             r[1], r[2], r[3], r[4], r[5]) for r in rows])

    def tape(self, symbol: str, timeframes: Optional[List[str]] = None,
             limit: int = 400) -> Tape:
        t = Tape()
        ok, why = self.healthy()
        t.stale = not ok
        if not ok:
            return t
        for tf in (timeframes or C.TIMEFRAMES):
            c = self.candles(symbol, tf, limit)
            if c:
                t.put(c)
        t.as_of = datetime.now(ET)
        return t

    def quote(self, symbol: str) -> Optional[dict]:
        ok, _ = self.healthy()
        return self.store.fetch_quote(symbol) if ok else None

    def mark(self, symbol: str) -> Optional[float]:
        q = self.quote(symbol)
        return q.get("mark") if q else None

    def bar_trades(self, symbol: str, candles: Candles) -> Optional[list]:
        """Group tick prints into per-candle buckets for the order-flow engine.
        Returns None when no tick data exists, which is the signal that CVD must
        fall back to its bar-shape approximation — and SAY that it did."""
        if not candles or not len(candles):
            return None
        start = int(candles.ts[0].timestamp())
        prints = self.store.fetch_trades(symbol, start)
        if not prints:
            return None
        from analysis.orderflow import Trade
        edges = [int(t.timestamp()) for t in candles.ts]
        buckets = [[] for _ in edges]
        j = 0
        for ts, seq, price, size, agg in prints:
            while j + 1 < len(edges) and ts >= edges[j + 1]:
                j += 1
            buckets[j].append(Trade(ts, price, size, agg))
        return buckets
