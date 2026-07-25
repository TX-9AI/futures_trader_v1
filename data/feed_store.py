"""
futures_trader_v1/data/feed_store.py — v0.1
v0.1 — 2026-07-25 — Initial build. The box's single SQLite (WAL) tape store:
        one writer, many readers, with a heartbeat.

ONE PRODUCER, MANY READERS — ported wholesale, because it is the architecture
that finally made the options fleet stop lying to itself. Exactly one process
holds the broker stream and writes here. The bot, the status tool, the analysis
stack and any future observer are READERS. No consumer may open its own stream:
a second subscription is a second version of the truth, and reconciling two
tapes after the fact is not possible.

READERS FAIL LOUD. Past the heartbeat ceiling the reader returns None and warns.
A dead feed must surface as "no data" and never as stale numbers driving a
decision — which is exactly what a cached frame does if nobody checks its age.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL, tf TEXT NOT NULL, ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, tf, ts)
);
CREATE INDEX IF NOT EXISTS ix_candles ON candles(symbol, tf, ts);

CREATE TABLE IF NOT EXISTS trades_tape (
    symbol TEXT NOT NULL, ts INTEGER NOT NULL, seq INTEGER NOT NULL,
    price REAL, size REAL, aggressor TEXT,
    PRIMARY KEY (symbol, ts, seq)
);
CREATE INDEX IF NOT EXISTS ix_tape ON trades_tape(symbol, ts);

CREATE TABLE IF NOT EXISTS quotes (
    symbol TEXT PRIMARY KEY, ts INTEGER, bid REAL, ask REAL, last REAL
);

CREATE TABLE IF NOT EXISTS heartbeat (
    producer TEXT PRIMARY KEY, ts INTEGER, note TEXT
);
"""


def _now() -> int:
    return int(time.time())


class FeedStore:
    def __init__(self, path: str, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if not read_only:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            with closing(self._conn()) as c:
                c.executescript(SCHEMA)
                c.commit()

    def _conn(self) -> sqlite3.Connection:
        if self.read_only and os.path.exists(self.path):
            c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        else:
            c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass                      # read-only connections cannot set WAL
        return c

    # ── writer side (producer only) ──────────────────────────────────────────
    def upsert_candles(self, symbol: str, tf: str,
                       rows: Sequence[Tuple]) -> int:
        """rows: (ts_epoch, o, h, l, c, v). Upsert so a re-sent partial candle
        updates in place instead of duplicating — the tape must be idempotent
        under reconnects."""
        if self.read_only or not rows:
            return 0
        with closing(self._conn()) as c:
            c.executemany(
                "INSERT INTO candles (symbol,tf,ts,open,high,low,close,volume) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(symbol,tf,ts) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume",
                [(symbol, tf, int(r[0]), r[1], r[2], r[3], r[4],
                  r[5] if len(r) > 5 else 0.0) for r in rows])
            c.commit()
            return len(rows)

    def append_trades(self, symbol: str, prints: Sequence[Tuple]) -> int:
        """prints: (ts_epoch, seq, price, size, aggressor).

        THIS IS THE IRREPLACEABLE DATASET. Tick data with an aggressor side
        cannot be reconstructed after the session — exactly as option chains
        could not. The options project discovered that exposure late, with 29
        boxes accumulating an archive that had no copy on control. Archive from
        day one and harvest nightly."""
        if self.read_only or not prints:
            return 0
        with closing(self._conn()) as c:
            c.executemany(
                "INSERT OR REPLACE INTO trades_tape "
                "(symbol,ts,seq,price,size,aggressor) VALUES (?,?,?,?,?,?)",
                [(symbol, int(p[0]), int(p[1]), p[2], p[3], p[4]) for p in prints])
            c.commit()
            return len(prints)

    def put_quote(self, symbol: str, bid: float, ask: float,
                  last: Optional[float] = None) -> None:
        if self.read_only:
            return
        with closing(self._conn()) as c:
            c.execute("INSERT OR REPLACE INTO quotes (symbol,ts,bid,ask,last) "
                      "VALUES (?,?,?,?,?)", (symbol, _now(), bid, ask, last))
            c.commit()

    def beat(self, producer: str = "feed", note: str = "") -> None:
        if self.read_only:
            return
        with closing(self._conn()) as c:
            c.execute("INSERT OR REPLACE INTO heartbeat (producer,ts,note) "
                      "VALUES (?,?,?)", (producer, _now(), note))
            c.commit()

    # ── reader side ──────────────────────────────────────────────────────────
    def heartbeat_age(self, producer: str = "feed") -> Optional[float]:
        try:
            with closing(self._conn()) as c:
                r = c.execute("SELECT ts FROM heartbeat WHERE producer=?",
                              (producer,)).fetchone()
        except sqlite3.Error:
            return None
        return None if not r else float(_now() - r["ts"])

    def fetch_candles(self, symbol: str, tf: str, limit: int = 400) -> List[Tuple]:
        try:
            with closing(self._conn()) as c:
                rows = c.execute(
                    "SELECT ts,open,high,low,close,volume FROM candles "
                    "WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?",
                    (symbol, tf, limit)).fetchall()
        except sqlite3.Error:
            return []
        return [(r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"])
                for r in reversed(rows)]

    def fetch_trades(self, symbol: str, since_ts: int = 0,
                     limit: int = 20000) -> List[Tuple]:
        try:
            with closing(self._conn()) as c:
                rows = c.execute(
                    "SELECT ts,seq,price,size,aggressor FROM trades_tape "
                    "WHERE symbol=? AND ts>=? ORDER BY ts,seq LIMIT ?",
                    (symbol, int(since_ts), limit)).fetchall()
        except sqlite3.Error:
            return []
        return [(r["ts"], r["seq"], r["price"], r["size"], r["aggressor"])
                for r in rows]

    def fetch_quote(self, symbol: str) -> Optional[dict]:
        try:
            with closing(self._conn()) as c:
                r = c.execute("SELECT ts,bid,ask,last FROM quotes WHERE symbol=?",
                              (symbol,)).fetchone()
        except sqlite3.Error:
            return None
        if not r:
            return None
        return {"ts": r["ts"], "bid": r["bid"], "ask": r["ask"], "last": r["last"],
                "mark": ((r["bid"] + r["ask"]) / 2.0
                         if r["bid"] and r["ask"] else r["last"])}
