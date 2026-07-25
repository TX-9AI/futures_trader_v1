"""
futures_trader_v1/control/fleet_config.py — v0.1
v0.1 — 2026-07-25 — Initial build. What the control plane knows about the fleet.

Named fleet_config so it can never be confused with the BOX config.py at the
repo root — they are read by different processes on different machines and
mixing them up is the kind of mistake that only shows up in production.
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple


def _env(n: str, d: str = "") -> str:
    return os.environ.get(n, d)


def _f(n: str, d: float) -> float:
    try:
        return float(os.environ.get(n, d))
    except (TypeError, ValueError):
        return float(d)


def _i(n: str, d: int) -> int:
    return int(_f(n, d))


def _b(n: str, d: bool) -> bool:
    v = os.environ.get(n)
    return d if v is None else v.strip().lower() in ("1", "true", "yes", "on")


# ── AWS / discovery ──────────────────────────────────────────────────────────
REGION = _env("FTC_REGION", "us-east-1")
PROJECT_TAG = _env("FTC_PROJECT_TAG", "futures_trader")
MOCK = _b("FTC_MOCK", False)

# ── SSH ──────────────────────────────────────────────────────────────────────
SSH_KEY = os.path.expanduser(_env("FTC_SSH_KEY", "~/.ssh/tx-9.pem"))
SSH_USER = _env("FTC_SSH_USER", "ubuntu")
SSH_TIMEOUT = _i("FTC_SSH_TIMEOUT", 12)
BOX_DIR = _env("FTC_BOX_DIR", "~/futures-trader")

# ── control-side paths ───────────────────────────────────────────────────────
BASE_DIR = os.path.expanduser(_env("FTC_BASE_DIR", "~/futures_control"))
OHLC_DIR = os.path.join(BASE_DIR, "ohlc")
TRADES_DIR = os.path.join(BASE_DIR, "trades")
FLOW_DIR = os.path.join(BASE_DIR, "orderflow")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
STATE_DIR = os.path.join(BASE_DIR, "state")

# ── the account is SHARED, so exposure is a FLEET quantity ───────────────────
# This is the single biggest structural difference from the options control
# plane. There, each box's worst case was the debit it paid, so boxes could be
# fully independent. Here every box draws on ONE account's margin, and a fleet
# that is individually compliant can be collectively over-committed.
ACCOUNT_EQUITY = _f("FTC_ACCOUNT_EQUITY", 25000.0)
FLEET_MARGIN_MAX_PCT = _f("FTC_FLEET_MARGIN_MAX", 0.60)   # of net liq, all boxes
FLEET_OVERNIGHT_MAX_PCT = _f("FTC_FLEET_OVERNIGHT_MAX", 0.40)
# Correlated roots count as ONE bet for exposure purposes. Long MNQ + long MES
# + long MYM is not three positions, it is one trade in three costumes.
CORRELATION_GROUPS: Dict[str, List[str]] = {
    "equity_index": ["ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K"],
    "energy": ["CL", "MCL", "NG", "MNG", "RB", "HO"],
    "metals": ["GC", "MGC", "SI", "SIL", "HG", "MHG", "PL", "PA"],
    "rates": ["ZB", "ZN", "ZF", "ZT"],
    "fx": ["6E", "M6E", "6J", "6B", "6A", "6C"],
    "ag": ["ZC", "ZS", "ZW"],
    "crypto": ["MBT", "MET"],
}
GROUP_MARGIN_MAX_PCT = _f("FTC_GROUP_MARGIN_MAX", 0.30)


def group_of(root: str) -> str:
    r = (root or "").upper()
    for g, roots in CORRELATION_GROUPS.items():
        if r in roots:
            return g
    return "other"


# ── the fleet ────────────────────────────────────────────────────────────────
# (symbol, mode). One box each. Modes decide the wake/stop rhythm, so the
# control plane must know them — it cannot treat every box the same way the
# options fleet could.
def _parse_fleet(raw: str) -> List[Tuple[str, str]]:
    out = []
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            s, m = item.split(":", 1)
        else:
            s, m = item, "DAY"
        out.append((s.strip().upper(), m.strip().upper()))
    return out


FLEET: List[Tuple[str, str]] = _parse_fleet(_env(
    "FTC_FLEET",
    "MNQ:DAY,MES:DAY,MYM:DAY,M2K:DAY,MGC:DAY,MCL:DAY,"
    "MNQ2:SCALP,MES2:SCALP,"
    "M6E:SWING,MGC2:SWING,SIL:SWING,"
    "MES3:HEDGE"))

INTRADAY_MODES = ("DAY", "SCALP")
OVERNIGHT_MODES = ("SWING", "HEDGE")


def boxes(mode: str = "") -> List[Tuple[str, str]]:
    return [b for b in FLEET if not mode or b[1] == mode.upper()]


def intraday_boxes() -> List[Tuple[str, str]]:
    return [b for b in FLEET if b[1] in INTRADAY_MODES]


def overnight_boxes() -> List[Tuple[str, str]]:
    return [b for b in FLEET if b[1] in OVERNIGHT_MODES]


def box_name(symbol: str, mode: str) -> str:
    return f"{symbol}-{mode}"


# ── telegram (control-side) ──────────────────────────────────────────────────
TELEGRAM_TOKEN = _env("FTC_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _env("FTC_TELEGRAM_CHAT_ID")
