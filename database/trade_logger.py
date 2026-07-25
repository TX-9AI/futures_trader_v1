"""
futures_trader_v1/database/trade_logger.py — v0.1
v0.1 — 2026-07-25 — Initial build. SQLite trade log with the futures schema,
        mode isolation from birth, and R-native performance columns.

DESIGNED AGAINST THREE otv3 DEFECTS THAT SHOULD NEVER RECUR
  Q  Mode isolation: every decision query is scoped to paper/live at the schema
     level, not bolted on later. In otv3 a single unfiltered trades.db meant
     weeks of paper P&L could gate the LIVE daily-loss breaker.
  N/O  Nothing is booked on SUBMISSION. `open_trade` requires a confirmed fill
     price; there is no code path that writes an entry from a signal mark.
  Excursion blindness: MFE/MAE are recorded IN TICKS AND IN R every tick from
     the first version, because the single most useful finding in the options
     book ("winners trailed out at +25% off a +60% peak") was only visible once
     excursion columns existed.

R IS THE NATIVE UNIT. Dollars vary with contract size; R does not. Every report
this system produces bins on R so a 1-lot MNQ scalp and a 3-lot ES swing are
comparable rows in the same table.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT UNIQUE,
    paper_trade         INTEGER NOT NULL DEFAULT 1,
    -- identity
    root                TEXT NOT NULL,
    contract_code       TEXT NOT NULL,
    mode                TEXT NOT NULL,
    strategy            TEXT NOT NULL,
    direction           TEXT NOT NULL,
    contracts           INTEGER NOT NULL,
    -- entry
    entry_time          TEXT,
    session_date        TEXT,
    session_phase       TEXT,
    killzone            TEXT,
    entry_price         REAL,
    stop_price          REAL,
    target_price        REAL,
    stop_ticks          REAL,
    risk_dollars        REAL,
    planned_rrr         REAL,
    grade               TEXT,
    setup_score         REAL,
    -- context at entry (the perishable half)
    regime              TEXT,
    regime_conviction   REAL,
    adx_at_entry        REAL,
    atr_at_entry        REAL,
    level_tier          REAL,
    level_name          TEXT,
    cvd_at_entry        REAL,
    delta_divergence    REAL,
    pd_position         REAL,
    notes               TEXT,
    -- lifecycle
    status              TEXT NOT NULL DEFAULT 'OPEN',
    contracts_open      INTEGER,
    scaled_out          INTEGER DEFAULT 0,
    trail_stop          REAL,
    max_favorable_ticks REAL DEFAULT 0,
    max_adverse_ticks   REAL DEFAULT 0,
    max_favorable_r     REAL DEFAULT 0,
    max_adverse_r       REAL DEFAULT 0,
    held_overnight      INTEGER DEFAULT 0,
    roll_id             TEXT,
    -- exit
    exit_time           TEXT,
    exit_price          REAL,
    exit_reason         TEXT,
    realized_pnl        REAL,
    realized_r          REAL,
    commission          REAL DEFAULT 0,
    order_id            TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_session ON trades(session_date, paper_trade);
CREATE INDEX IF NOT EXISTS ix_trades_status  ON trades(status, paper_trade);

CREATE TABLE IF NOT EXISTS settlements (
    session_date  TEXT PRIMARY KEY,
    paper_trade   INTEGER NOT NULL DEFAULT 1,
    variation     REAL,
    net_liq       REAL,
    margin_used   REAL,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS rolls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    roll_id       TEXT UNIQUE,
    root          TEXT,
    from_code     TEXT,
    to_code       TEXT,
    contracts     INTEGER,
    kind          TEXT,
    status        TEXT,
    fill_price    REAL,
    at            TEXT,
    message       TEXT
);
"""


@dataclass
class TradeRecord:
    trade_id: str
    root: str
    contract_code: str
    mode: str
    strategy: str
    direction: str            # LONG | SHORT
    contracts: int
    entry_price: float
    stop_price: float
    target_price: Optional[float] = None
    stop_ticks: float = 0.0
    risk_dollars: float = 0.0
    planned_rrr: float = 0.0
    grade: str = "B"
    setup_score: float = 0.0
    entry_time: str = ""
    session_date: str = ""
    session_phase: str = ""
    killzone: str = ""
    regime: str = ""
    regime_conviction: float = 0.0
    adx_at_entry: float = 0.0
    atr_at_entry: float = 0.0
    level_tier: float = 0.0
    level_name: str = ""
    cvd_at_entry: float = 0.0
    delta_divergence: float = 0.0
    pd_position: float = 0.0
    notes: str = ""
    paper_trade: int = 1
    order_id: str = ""


class TradeLogger:
    def __init__(self, db_path: str = "trades.db", paper: bool = True,
                 tick_value: float = 1.0, tick_size: float = 0.25):
        self.db_path = db_path
        self.paper = 1 if paper else 0
        self.tick_value = tick_value
        self.tick_size = tick_size
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        with closing(self._conn()) as c:
            c.executescript(SCHEMA)
            c.commit()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ── writes ────────────────────────────────────────────────────────────────
    def open_trade(self, rec: TradeRecord, confirmed_fill: bool) -> Optional[int]:
        """A position exists in this table only when the broker says it does.
        `confirmed_fill=False` is refused outright rather than written with a
        flag, because a flagged ghost is still a row the position manager will
        try to manage (otv3 defects N and O, learned across two audits)."""
        if not confirmed_fill:
            logger.error("REFUSED to log %s: entry not fill-confirmed", rec.trade_id)
            return None
        d = asdict(rec)
        d["paper_trade"] = self.paper
        d["contracts_open"] = rec.contracts
        d["status"] = "OPEN"
        d.setdefault("entry_time", datetime.utcnow().isoformat())
        cols = ",".join(d)
        marks = ",".join("?" * len(d))
        with closing(self._conn()) as c:
            cur = c.execute(f"INSERT INTO trades ({cols}) VALUES ({marks})",
                            list(d.values()))
            c.commit()
            return cur.lastrowid

    def update_excursion(self, trade_id: str, price: float) -> None:
        """Every tick. Ticks AND R, so the excursion report needs no join."""
        with closing(self._conn()) as c:
            row = c.execute("SELECT direction, entry_price, stop_ticks, "
                            "max_favorable_ticks, max_adverse_ticks FROM trades "
                            "WHERE trade_id=? AND status='OPEN'", (trade_id,)).fetchone()
            if not row:
                return
            sign = 1.0 if row["direction"] == "LONG" else -1.0
            move_ticks = sign * (price - row["entry_price"]) / self.tick_size
            mf = max(row["max_favorable_ticks"] or 0.0, move_ticks)
            ma = min(row["max_adverse_ticks"] or 0.0, move_ticks)
            st = row["stop_ticks"] or 0.0
            c.execute("UPDATE trades SET max_favorable_ticks=?, max_adverse_ticks=?, "
                      "max_favorable_r=?, max_adverse_r=? WHERE trade_id=?",
                      (mf, ma, (mf / st) if st else 0.0, (ma / st) if st else 0.0,
                       trade_id))
            c.commit()

    def update_fields(self, trade_id: str, **fields) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        with closing(self._conn()) as c:
            c.execute(f"UPDATE trades SET {sets} WHERE trade_id=?",
                      list(fields.values()) + [trade_id])
            c.commit()

    def close_trade(self, trade_id: str, exit_price: float, reason: str,
                    contracts_closed: Optional[int] = None,
                    commission: float = 0.0,
                    confirmed_fill: bool = True) -> Optional[float]:
        """Books P&L ONLY on a confirmed fill. An unconfirmed close leaves the
        row OPEN so the retry loop keeps working it — the anti-orphan invariant
        that otv3 needed two audits to arrive at."""
        if not confirmed_fill:
            logger.error("close for %s not confirmed — row stays OPEN", trade_id)
            return None
        with closing(self._conn()) as c:
            row = c.execute("SELECT * FROM trades WHERE trade_id=? AND status='OPEN'",
                            (trade_id,)).fetchone()
            if not row:
                return None
            n = contracts_closed or row["contracts_open"] or row["contracts"]
            sign = 1.0 if row["direction"] == "LONG" else -1.0
            ticks = sign * (exit_price - row["entry_price"]) / self.tick_size
            pnl = ticks * self.tick_value * n - commission
            remaining = (row["contracts_open"] or row["contracts"]) - n
            risk = row["risk_dollars"] or 0.0
            per_contract_risk = risk / max(row["contracts"], 1)
            realized_r = pnl / (per_contract_risk * n) if per_contract_risk else 0.0
            if remaining > 0:
                c.execute("UPDATE trades SET contracts_open=?, scaled_out=1, "
                          "realized_pnl=COALESCE(realized_pnl,0)+? WHERE trade_id=?",
                          (remaining, pnl, trade_id))
            else:
                c.execute("UPDATE trades SET status='CLOSED', contracts_open=0, "
                          "exit_time=?, exit_price=?, exit_reason=?, "
                          "realized_pnl=COALESCE(realized_pnl,0)+?, realized_r=?, "
                          "commission=COALESCE(commission,0)+? WHERE trade_id=?",
                          (datetime.utcnow().isoformat(), exit_price, reason,
                           pnl, realized_r, commission, trade_id))
            c.commit()
            return pnl

    def record_roll(self, roll_id: str, root: str, from_code: str, to_code: str,
                    contracts: int, kind: str, status: str,
                    fill_price: Optional[float], message: str = "") -> None:
        with closing(self._conn()) as c:
            c.execute("INSERT OR REPLACE INTO rolls "
                      "(roll_id,root,from_code,to_code,contracts,kind,status,"
                      "fill_price,at,message) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (roll_id, root, from_code, to_code, contracts, kind, status,
                       fill_price, datetime.utcnow().isoformat(), message))
            c.commit()

    def record_settlement(self, session: date, variation: float,
                          net_liq: float = 0.0, margin_used: float = 0.0,
                          note: str = "") -> None:
        with closing(self._conn()) as c:
            c.execute("INSERT OR REPLACE INTO settlements "
                      "(session_date,paper_trade,variation,net_liq,margin_used,note) "
                      "VALUES (?,?,?,?,?,?)",
                      (session.isoformat(), self.paper, variation, net_liq,
                       margin_used, note))
            c.commit()

    # ── reads (ALL mode-scoped) ───────────────────────────────────────────────
    def get_open_trades(self) -> List[Dict[str, Any]]:
        with closing(self._conn()) as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM trades WHERE status='OPEN' AND "
                "COALESCE(paper_trade,1)=? ORDER BY id", (self.paper,))]

    def realized_pnl_today(self, session: Optional[date] = None) -> float:
        s = (session or date.today()).isoformat()
        with closing(self._conn()) as c:
            r = c.execute("SELECT COALESCE(SUM(realized_pnl),0) v FROM trades "
                          "WHERE session_date=? AND status='CLOSED' AND "
                          "COALESCE(paper_trade,1)=?", (s, self.paper)).fetchone()
            return float(r["v"] or 0.0)

    def expectancy(self, strategy: Optional[str] = None) -> Dict[str, float]:
        """Win rate is not an edge. This returns win%, avg win R, avg loss R and
        EXPECTANCY IN R together, because otv3's sweep book was 75% winners and
        deeply negative — and the report that showed only win% hid it."""
        q = ("SELECT realized_r FROM trades WHERE status='CLOSED' AND "
             "COALESCE(paper_trade,1)=?")
        args: List[Any] = [self.paper]
        if strategy:
            q += " AND strategy=?"
            args.append(strategy)
        with closing(self._conn()) as c:
            rs = [float(r["realized_r"] or 0.0) for r in c.execute(q, args)]
        if not rs:
            return {"n": 0, "win_rate": 0.0, "avg_win_r": 0.0,
                    "avg_loss_r": 0.0, "expectancy_r": 0.0}
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        aw = sum(wins) / len(wins) if wins else 0.0
        al = sum(losses) / len(losses) if losses else 0.0
        return {"n": len(rs), "win_rate": len(wins) / len(rs), "avg_win_r": aw,
                "avg_loss_r": al, "expectancy_r": sum(rs) / len(rs)}
