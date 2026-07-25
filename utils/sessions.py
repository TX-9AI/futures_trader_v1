"""
futures_trader_v1/utils/sessions.py — v0.1
v0.1 — 2026-07-25 — Initial build. The exchange clock: Globex session model,
        daily break, holiday/early-close calendar, ICT killzones, and the
        per-mode flatten authority.

THE SINGLE BIGGEST STRUCTURAL DIFFERENCE FROM options_trader_v3.
The options engine had one session (09:30–16:00 ET), one hard close (15:45),
and boxes that were switched OFF overnight. Futures trade ~23 hours, settle
daily, and break for an hour. Every time rule in this system resolves HERE, so
that a rule can never be written twice in two files with two values — the otv3
defect-H trap (config.NO_ENTRY_AFTER_ET vs a hardcoded time_utils.NO_ENTRY,
where the "obvious fix" would have silently moved the global cutoff by 3 hours).

Times are US/Eastern throughout. The CME quotes CT; the operator thinks in ET;
one conversion at the boundary beats two conventions in the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("US/Eastern")
except Exception:                                     # pragma: no cover
    import pytz
    ET = pytz.timezone("US/Eastern")

# ─── GLOBEX DAY ───────────────────────────────────────────────────────────────
# The trading DAY opens Sunday 18:00 ET and each weekday at 18:00 ET, breaking
# 17:00–18:00 ET. A "session date" is the date the day SETTLES on, so Sunday
# 18:00 ET belongs to Monday. Every daily aggregate in this engine — volume for
# the roll crossover, prior-day high/low, value area — keys on session_date.
GLOBEX_OPEN_ET = dtime(18, 0)
GLOBEX_CLOSE_ET = dtime(17, 0)

# ─── KILLZONES (ET) ───────────────────────────────────────────────────────────
# Named windows, not magic numbers scattered through strategies. A strategy asks
# `in_killzone(now, "NY_AM")`; it never carries an hour literal.
KILLZONES: Dict[str, Tuple[dtime, dtime]] = {
    "ASIA":          (dtime(19, 0), dtime(23, 0)),
    "LONDON":        (dtime(2, 0),  dtime(5, 0)),
    "NY_AM":         (dtime(7, 0),  dtime(10, 0)),
    "SILVER_BULLET": (dtime(10, 0), dtime(11, 0)),
    "NY_LUNCH":      (dtime(12, 0), dtime(13, 30)),
    "NY_PM":         (dtime(13, 30), dtime(16, 0)),
}

# ─── HOLIDAY CALENDAR ─────────────────────────────────────────────────────────
# CME equity-index holidays and early closes. Full closes: no session at all.
# Early closes: session ends at the stated ET time (13:00 typical).
# NOTE: this table is maintained by hand and MUST be extended each year. A
# missing year does not fail closed silently — `calendar_coverage_ok()` reports
# it and devtools surfaces it, because an unknown holiday looks exactly like a
# dead feed and that ambiguity cost otv3 a week of "spool-up failure" debugging.
FULL_CLOSURES: Dict[int, List[Tuple[int, int]]] = {
    2026: [(1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3),
           (9, 7), (11, 26), (12, 25)],
    2027: [(1, 1), (1, 18), (2, 15), (3, 26), (5, 31), (6, 18), (7, 5),
           (9, 6), (11, 25), (12, 24)],
}
EARLY_CLOSES: Dict[int, List[Tuple[int, int, int, int]]] = {   # (m, d, hh, mm)
    2026: [(7, 2, 13, 0), (11, 27, 13, 0), (12, 24, 13, 0)],
    2027: [(7, 2, 13, 0), (11, 26, 13, 0), (12, 23, 13, 0)],
}


def calendar_coverage_ok(on: date) -> bool:
    return on.year in FULL_CLOSURES


def is_holiday(d: date) -> bool:
    return (d.month, d.day) in FULL_CLOSURES.get(d.year, [])


def early_close_time(d: date) -> Optional[dtime]:
    for m, dd, hh, mi in EARLY_CLOSES.get(d.year, []):
        if (m, dd) == (d.month, d.day):
            return dtime(hh, mi)
    return None


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_holiday(d)


def next_trading_day(d: date) -> date:
    n = d + timedelta(days=1)
    while not is_trading_day(n):
        n += timedelta(days=1)
    return n


def prior_trading_day(d: date) -> date:
    p = d - timedelta(days=1)
    while not is_trading_day(p):
        p -= timedelta(days=1)
    return p


# ─── NOW / SESSION RESOLUTION ─────────────────────────────────────────────────
def now_et() -> datetime:
    return datetime.now(ET)


def to_et(dt: datetime) -> datetime:
    return dt.astimezone(ET) if dt.tzinfo else dt.replace(tzinfo=ET)


def session_date(dt: Optional[datetime] = None) -> date:
    """The date this moment SETTLES on. After 18:00 ET the session belongs to
    the next trading day; 17:00–18:00 is the break and belongs to neither, so it
    is attributed forward (the next session it will open into)."""
    dt = to_et(dt or now_et())
    if dt.time() >= dtime(17, 0):
        return next_trading_day(dt.date())
    return dt.date() if is_trading_day(dt.date()) else next_trading_day(dt.date())


def in_daily_break(dt: Optional[datetime] = None) -> bool:
    t = to_et(dt or now_et()).time()
    return dtime(17, 0) <= t < dtime(18, 0)


def market_is_open(dt: Optional[datetime] = None) -> bool:
    """Globex open: not the break, not a full closure, and inside the weekly
    Sunday-18:00 → Friday-17:00 span."""
    dt = to_et(dt or now_et())
    if in_daily_break(dt):
        return False
    wd, t = dt.weekday(), dt.time()
    if wd == 5:                                   # Saturday
        return False
    if wd == 6 and t < GLOBEX_OPEN_ET:            # Sunday before the open
        return False
    if wd == 4 and t >= GLOBEX_CLOSE_ET:          # Friday after the close
        return False
    sd = session_date(dt)
    if is_holiday(sd):
        return False
    ec = early_close_time(sd)
    if ec and dt.date() == sd and t >= ec:
        return False
    return True


@dataclass(frozen=True)
class SessionWindow:
    name: str
    start: dtime
    end: dtime

    def contains(self, t: dtime) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end     # wraps midnight


def in_killzone(dt: Optional[datetime], name: str) -> bool:
    kz = KILLZONES.get(name.upper())
    if not kz:
        return False
    return SessionWindow(name, *kz).contains(to_et(dt or now_et()).time())


def active_killzones(dt: Optional[datetime] = None) -> List[str]:
    t = to_et(dt or now_et()).time()
    return [n for n, (s, e) in KILLZONES.items() if SessionWindow(n, s, e).contains(t)]


def session_phase(dt: Optional[datetime] = None) -> str:
    """Coarse label used by the regime scorer and journalled on every trade.
    ASIA / LONDON / NY_PRE / NY_RTH / NY_POST / BREAK / CLOSED."""
    dt = to_et(dt or now_et())
    if not market_is_open(dt):
        return "BREAK" if in_daily_break(dt) else "CLOSED"
    t = dt.time()
    if dtime(18, 0) <= t or t < dtime(2, 0):
        return "ASIA"
    if dtime(2, 0) <= t < dtime(7, 0):
        return "LONDON"
    if dtime(7, 0) <= t < dtime(9, 30):
        return "NY_PRE"
    if dtime(9, 30) <= t < dtime(16, 0):
        return "NY_RTH"
    return "NY_POST"


# ─── RTH / CASH SESSION (per contract) ────────────────────────────────────────
def in_rth(spec, dt: Optional[datetime] = None) -> bool:
    """RTH is per-contract: index 09:30–16:00, gold 08:20–13:30, crude
    09:00–14:30. `spec` is a ContractSpec from data.contract_registry."""
    dt = to_et(dt or now_et())
    if not market_is_open(dt):
        return False
    t = dt.time()
    o, c = dtime(*spec.rth_open), dtime(*spec.rth_close)
    ec = early_close_time(dt.date())
    if ec and ec < c:
        c = ec
    return o <= t < c


def cash_close_dt(spec, on: Optional[date] = None) -> datetime:
    """The contract's cash-session close for a given session date — the moment a
    DAY or SCALP box must be flat."""
    on = on or session_date()
    c = dtime(*spec.rth_close)
    ec = early_close_time(on)
    if ec and ec < c:
        c = ec
    return datetime.combine(on, c, tzinfo=ET)


def minutes_to_cash_close(spec, dt: Optional[datetime] = None) -> float:
    dt = to_et(dt or now_et())
    return (cash_close_dt(spec, session_date(dt)) - dt).total_seconds() / 60.0


# ─── MODE-AWARE FLATTEN AUTHORITY ─────────────────────────────────────────────
# Operator spec 2026-07-25: "In day/scalp trade mode, do not carry past the cash
# session; in hedge or swing, always carry it." That sentence lives in exactly
# one function.
INTRADAY_MODES = ("DAY", "SCALP")
OVERNIGHT_MODES = ("SWING", "HEDGE")


def must_be_flat(mode: str, spec, dt: Optional[datetime] = None,
                 flatten_lead_min: float = 5.0) -> bool:
    """True once an intraday mode is inside its flatten window. Overnight modes
    never return True here — they are flattened only by a stop, a target, a
    roll, or an operator command."""
    if mode.upper() not in INTRADAY_MODES:
        return False
    return minutes_to_cash_close(spec, dt) <= flatten_lead_min


def entries_allowed(mode: str, spec, dt: Optional[datetime] = None,
                    entry_cutoff_min: float = 30.0,
                    enabled_sessions: Optional[List[str]] = None) -> Tuple[bool, str]:
    """One place where 'can this box open a NEW position right now' is decided.
    Returns (allowed, reason) — the reason string is journalled, because an
    unexplained no-trade day is indistinguishable from a broken engine."""
    dt = to_et(dt or now_et())
    if not market_is_open(dt):
        return False, ("daily break" if in_daily_break(dt) else "market closed")
    if not calendar_coverage_ok(session_date(dt)):
        return False, "exchange calendar has no coverage for this year — extend sessions.py"
    phase = session_phase(dt)
    if enabled_sessions and phase not in enabled_sessions:
        return False, f"session {phase} not enabled for this box"
    m = mode.upper()
    if m in INTRADAY_MODES:
        if not in_rth(spec, dt) and "ETH" not in (enabled_sessions or []):
            return False, "intraday mode outside RTH"
        left = minutes_to_cash_close(spec, dt)
        if left <= entry_cutoff_min:
            return False, f"inside entry cutoff ({left:.0f}m to cash close)"
    return True, "ok"
