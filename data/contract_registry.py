"""
futures_trader_v1/data/contract_registry.py — v0.1
v0.1 — 2026-07-25 — Initial build. The contract truth table + front-month
        resolution + the volume-crossover roll state machine.

WHY THIS FILE EXISTS AND WHY IT IMPORTS NOTHING
-----------------------------------------------
options_trader_v3 had exactly one instrument-shape table (STRIKE_INCREMENTS) and
it was a hint — a wrong value cost a slightly-off strike. Futures have no such
mercy: tick_value is the unit of P&L, multiplier is the unit of exposure, and
the contract you are trading STOPS EXISTING on a date. Getting any of the three
wrong is not a degraded fill, it is a wrong number in every risk calculation.

So this module is PURE DATA + PURE FUNCTIONS. It imports nothing from config, so
config can import it, and so tests can exercise it with no environment at all.
The otv3 lesson driving that: regime_confluence v1.1 spent a week running every
constant on fallbacks because one guarded config import silently failed.

MARGIN VALUES ARE SEED PRIORS, NOT TRUTH.
Exchange margins change with volatility, and the broker's number is the only one
that can liquidate you. These seeds exist so the engine can size and gate BEFORE
a broker session exists (install time, paper, backtest). margin_manager refreshes
them from the account at every session start and logs the delta. Never quote a
number from this table to the operator without saying where it came from.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

# ─── MONTH CODES ──────────────────────────────────────────────────────────────
MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
               7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}
CODE_TO_MONTH = {v: k for k, v in MONTH_CODES.items()}

QUARTERLY = "HMUZ"
ALL_MONTHS = "FGHJKMNQUVXZ"

# Expiry rule identifiers. The rule decides where the roll WINDOW sits; the
# volume crossover decides where inside it the roll actually happens.
RULE_INDEX_3RD_FRI = "index_3rd_friday"      # ES/NQ/YM/RTY + micros
RULE_FX_2BD_B4_3RD_WED = "fx_2bd_b4_3rd_wed"  # 6E/6J/6B/6A/6C
RULE_RATES_LAST_BD_PRIOR = "rates_last_bd_prior"
RULE_CL = "cl_3bd_b4_25th_prior"
RULE_NG = "ng_3bd_b4_month"
RULE_PHYS_FND = "phys_first_notice"          # metals / ags: last BD of prior month
RULE_CRYPTO_LAST_FRI = "crypto_last_friday"


@dataclass(frozen=True)
class ContractSpec:
    """Everything about a contract that is a FACT, not a preference."""
    root: str
    name: str
    exchange: str
    asset_class: str          # index | energy | metals | rates | fx | ag | crypto
    tick_size: float
    tick_value: float         # USD per tick per contract — the unit of P&L
    multiplier: float         # USD per 1.0 of price
    months: str               # month codes that list
    expiry_rule: str
    physically_delivered: bool
    rth_open: Tuple[int, int]   # ET
    rth_close: Tuple[int, int]  # ET
    min_stop_ticks: int       # below this a stop is inside the noise/spread
    typical_spread_ticks: float
    roll_lead_days: int       # business days before last-trade/FND the window opens
    init_margin: float        # SEED PRIOR — refreshed from broker
    maint_margin: float       # SEED PRIOR
    day_margin: float         # SEED PRIOR — intraday reduced rate
    is_micro: bool = False
    sibling: str = ""         # the mini/micro partner root
    notes: str = ""

    @property
    def ticks_per_point(self) -> float:
        return 1.0 / self.tick_size

    def ticks(self, price_delta: float) -> float:
        """Price distance -> ticks. Everything in this engine is measured in
        ticks or ATR, never in percent — the otv3 tolerance-bug family (a 0.05%
        break buffer that meant $0.49 on MU and $3.00 on SPX) is impossible if
        no percentage of price ever enters a comparison."""
        return abs(price_delta) / self.tick_size

    def dollars(self, price_delta: float, contracts: int = 1) -> float:
        return self.ticks(price_delta) * self.tick_value * contracts

    def round_to_tick(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 10)


# ─── THE TABLE ────────────────────────────────────────────────────────────────
# 34 roots: the liquid CME/NYMEX/COMEX/CBOT complex, minis and micros paired.
def _s(*a, **kw) -> ContractSpec:
    return ContractSpec(*a, **kw)


SPECS: Dict[str, ContractSpec] = {s.root: s for s in [
    # ── Equity index ──────────────────────────────────────────────────────────
    _s("ES", "E-mini S&P 500", "CME", "index", 0.25, 12.50, 50, QUARTERLY,
       RULE_INDEX_3RD_FRI, False, (9, 30), (16, 0), 8, 1.0, 8,
       17000, 15500, 4250, False, "MES"),
    _s("MES", "Micro E-mini S&P 500", "CME", "index", 0.25, 1.25, 5, QUARTERLY,
       RULE_INDEX_3RD_FRI, False, (9, 30), (16, 0), 8, 1.0, 8,
       1700, 1550, 425, True, "ES"),
    _s("NQ", "E-mini Nasdaq 100", "CME", "index", 0.25, 5.00, 20, QUARTERLY,
       RULE_INDEX_3RD_FRI, False, (9, 30), (16, 0), 20, 1.0, 8,
       27000, 24500, 6750, False, "MNQ"),
    _s("MNQ", "Micro E-mini Nasdaq 100", "CME", "index", 0.25, 0.50, 2, QUARTERLY,
       RULE_INDEX_3RD_FRI, False, (9, 30), (16, 0), 20, 1.0, 8,
       2700, 2450, 675, True, "NQ"),
    _s("YM", "E-mini Dow", "CBOT", "index", 1.0, 5.00, 5, QUARTERLY,
       RULE_INDEX_3RD_FRI, False, (9, 30), (16, 0), 12, 1.0, 8,
       11000, 10000, 2750, False, "MYM"),
    _s("MYM", "Micro E-mini Dow", "CBOT", "index", 1.0, 0.50, 0.5, QUARTERLY,
       RULE_INDEX_3RD_FRI, False, (9, 30), (16, 0), 12, 1.0, 8,
       1100, 1000, 275, True, "YM"),
    _s("RTY", "E-mini Russell 2000", "CME", "index", 0.10, 5.00, 50, QUARTERLY,
       RULE_INDEX_3RD_FRI, False, (9, 30), (16, 0), 15, 1.0, 8,
       8500, 7700, 2125, False, "M2K"),
    _s("M2K", "Micro E-mini Russell 2000", "CME", "index", 0.10, 0.50, 5, QUARTERLY,
       RULE_INDEX_3RD_FRI, False, (9, 30), (16, 0), 15, 1.0, 8,
       850, 770, 215, True, "RTY"),

    # ── Energy ────────────────────────────────────────────────────────────────
    _s("CL", "WTI Crude Oil", "NYMEX", "energy", 0.01, 10.00, 1000, ALL_MONTHS,
       RULE_CL, True, (9, 0), (14, 30), 15, 1.0, 6,
       7500, 6800, 3750, False, "MCL"),
    _s("MCL", "Micro WTI Crude Oil", "NYMEX", "energy", 0.01, 1.00, 100, ALL_MONTHS,
       RULE_CL, False, (9, 0), (14, 30), 15, 1.0, 6,
       750, 680, 375, True, "CL"),
    _s("NG", "Henry Hub Natural Gas", "NYMEX", "energy", 0.001, 10.00, 10000, ALL_MONTHS,
       RULE_NG, True, (9, 0), (14, 30), 20, 2.0, 6,
       5200, 4700, 2600, False, "MNG"),
    _s("MNG", "Micro Henry Hub Natural Gas", "NYMEX", "energy", 0.001, 2.50, 2500, ALL_MONTHS,
       RULE_NG, False, (9, 0), (14, 30), 20, 2.0, 6,
       1300, 1180, 650, True, "NG"),
    _s("RB", "RBOB Gasoline", "NYMEX", "energy", 0.0001, 4.20, 42000, ALL_MONTHS,
       RULE_CL, True, (9, 0), (14, 30), 20, 2.0, 6,
       9000, 8200, 4500),
    _s("HO", "NY Harbor ULSD", "NYMEX", "energy", 0.0001, 4.20, 42000, ALL_MONTHS,
       RULE_CL, True, (9, 0), (14, 30), 20, 2.0, 6,
       9500, 8600, 4750),

    # ── Metals ────────────────────────────────────────────────────────────────
    _s("GC", "Gold", "COMEX", "metals", 0.10, 10.00, 100, "GJMQVZ",
       RULE_PHYS_FND, True, (8, 20), (13, 30), 25, 1.0, 10,
       13500, 12300, 6750, False, "MGC"),
    _s("MGC", "Micro Gold", "COMEX", "metals", 0.10, 1.00, 10, "GJMQVZ",
       RULE_PHYS_FND, False, (8, 20), (13, 30), 25, 1.0, 10,
       1350, 1230, 675, True, "GC"),
    _s("SI", "Silver", "COMEX", "metals", 0.005, 25.00, 5000, "HKNUZ",
       RULE_PHYS_FND, True, (8, 25), (13, 25), 20, 1.0, 10,
       17500, 16000, 8750, False, "SIL"),
    _s("SIL", "Micro Silver (1,000 oz)", "COMEX", "metals", 0.005, 5.00, 1000, "HKNUZ",
       RULE_PHYS_FND, False, (8, 25), (13, 25), 20, 1.0, 10,
       3500, 3200, 1750, True, "SI"),
    _s("HG", "Copper", "COMEX", "metals", 0.0005, 12.50, 25000, "HKNUZ",
       RULE_PHYS_FND, True, (8, 10), (13, 0), 20, 1.0, 10,
       8500, 7700, 4250, False, "MHG"),
    _s("MHG", "Micro Copper", "COMEX", "metals", 0.0005, 1.25, 2500, "HKNUZ",
       RULE_PHYS_FND, False, (8, 10), (13, 0), 20, 1.0, 10,
       850, 770, 425, True, "HG"),
    _s("PL", "Platinum", "NYMEX", "metals", 0.10, 5.00, 50, "FJNV",
       RULE_PHYS_FND, True, (8, 20), (13, 5), 25, 2.0, 10,
       5500, 5000, 2750),
    _s("PA", "Palladium", "NYMEX", "metals", 0.10, 10.00, 100, "HMUZ",
       RULE_PHYS_FND, True, (8, 20), (13, 0), 30, 4.0, 10,
       14000, 12700, 7000),

    # ── Rates ─────────────────────────────────────────────────────────────────
    _s("ZB", "30-Year U.S. Treasury Bond", "CBOT", "rates", 1 / 32, 31.25, 1000, QUARTERLY,
       RULE_RATES_LAST_BD_PRIOR, True, (8, 20), (15, 0), 8, 1.0, 12,
       5200, 4700, 2600),
    _s("ZN", "10-Year U.S. Treasury Note", "CBOT", "rates", 1 / 64, 15.625, 1000, QUARTERLY,
       RULE_RATES_LAST_BD_PRIOR, True, (8, 20), (15, 0), 8, 1.0, 12,
       2400, 2180, 1200),
    _s("ZF", "5-Year U.S. Treasury Note", "CBOT", "rates", 1 / 128, 7.8125, 1000, QUARTERLY,
       RULE_RATES_LAST_BD_PRIOR, True, (8, 20), (15, 0), 8, 1.0, 12,
       1500, 1360, 750),
    _s("ZT", "2-Year U.S. Treasury Note", "CBOT", "rates", 1 / 256, 7.8125, 2000, QUARTERLY,
       RULE_RATES_LAST_BD_PRIOR, True, (8, 20), (15, 0), 8, 1.0, 12,
       1100, 1000, 550),

    # ── FX ────────────────────────────────────────────────────────────────────
    _s("6E", "Euro FX", "CME", "fx", 0.00005, 6.25, 125000, QUARTERLY,
       RULE_FX_2BD_B4_3RD_WED, False, (8, 20), (15, 0), 10, 1.0, 8,
       2900, 2650, 1450, False, "M6E"),
    _s("M6E", "Micro Euro FX", "CME", "fx", 0.0001, 1.25, 12500, QUARTERLY,
       RULE_FX_2BD_B4_3RD_WED, False, (8, 20), (15, 0), 10, 1.0, 8,
       290, 265, 145, True, "6E"),
    _s("6J", "Japanese Yen", "CME", "fx", 0.0000005, 6.25, 12500000, QUARTERLY,
       RULE_FX_2BD_B4_3RD_WED, False, (8, 20), (15, 0), 10, 1.0, 8,
       3400, 3100, 1700),
    _s("6B", "British Pound", "CME", "fx", 0.0001, 6.25, 62500, QUARTERLY,
       RULE_FX_2BD_B4_3RD_WED, False, (8, 20), (15, 0), 10, 1.0, 8,
       2200, 2000, 1100),
    _s("6A", "Australian Dollar", "CME", "fx", 0.0001, 10.00, 100000, QUARTERLY,
       RULE_FX_2BD_B4_3RD_WED, False, (8, 20), (15, 0), 10, 1.0, 8,
       1900, 1730, 950),
    _s("6C", "Canadian Dollar", "CME", "fx", 0.00005, 5.00, 100000, QUARTERLY,
       RULE_FX_2BD_B4_3RD_WED, False, (8, 20), (15, 0), 10, 1.0, 8,
       1500, 1360, 750),

    # ── Agriculture ───────────────────────────────────────────────────────────
    _s("ZC", "Corn", "CBOT", "ag", 0.25, 12.50, 5000, "HKNUZ",
       RULE_PHYS_FND, True, (9, 30), (14, 20), 8, 1.0, 10,
       1800, 1640, 900),
    _s("ZS", "Soybeans", "CBOT", "ag", 0.25, 12.50, 5000, "FHKNQUX",
       RULE_PHYS_FND, True, (9, 30), (14, 20), 8, 1.0, 10,
       2900, 2640, 1450),
    _s("ZW", "Chicago SRW Wheat", "CBOT", "ag", 0.25, 12.50, 5000, "HKNUZ",
       RULE_PHYS_FND, True, (9, 30), (14, 20), 8, 1.0, 10,
       2400, 2180, 1200),

    # ── Crypto ────────────────────────────────────────────────────────────────
    _s("MBT", "Micro Bitcoin", "CME", "crypto", 5.0, 0.50, 0.1, ALL_MONTHS,
       RULE_CRYPTO_LAST_FRI, False, (9, 30), (16, 0), 20, 2.0, 6,
       1600, 1450, 800, True, "BTC"),
    _s("MET", "Micro Ether", "CME", "crypto", 0.50, 0.05, 0.1, ALL_MONTHS,
       RULE_CRYPTO_LAST_FRI, False, (9, 30), (16, 0), 20, 2.0, 6,
       900, 820, 450, True, "ETH"),
]}

ROOTS: List[str] = sorted(SPECS)
MICRO_ROOTS: List[str] = sorted(r for r, s in SPECS.items() if s.is_micro)


def get_spec(root: str) -> ContractSpec:
    r = (root or "").upper().lstrip("/")
    if r not in SPECS:
        raise KeyError(f"unknown contract root {root!r}; known: {', '.join(ROOTS)}")
    return SPECS[r]


def is_known(root: str) -> bool:
    return (root or "").upper().lstrip("/") in SPECS


# ─── CALENDAR PRIMITIVES ──────────────────────────────────────────────────────
def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    """n-th <weekday> of a month. weekday: Mon=0 .. Sun=6."""
    d = date(y, m, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_business_day(y: int, m: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _last_weekday_of_month(y: int, m: int, weekday: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def add_business_days(d: date, n: int) -> date:
    """Weekend-aware only. Holidays are applied by utils.sessions, which owns
    the exchange calendar; the registry deliberately stays holiday-agnostic so
    it has no dependency and no drift."""
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if d.weekday() < 5:
            remaining -= 1
    return d


def _prev_month(y: int, m: int) -> Tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def last_trade_date(root: str, year: int, month: int) -> date:
    """Approximate last-trade / first-notice date for a delivery month.

    APPROXIMATE IS DELIBERATE AND SAFE HERE: this date sets where the roll
    WINDOW opens, and the window is days wide. The actual roll trigger is the
    volume crossover observed inside it. When the broker reports a real expiry
    for the resolved contract, RollManager overrides this value — see
    `ContractCycle.with_broker_expiry`.
    """
    spec = get_spec(root)
    rule = spec.expiry_rule
    if rule == RULE_INDEX_3RD_FRI:
        return _nth_weekday(year, month, 4, 3)
    if rule == RULE_FX_2BD_B4_3RD_WED:
        return add_business_days(_nth_weekday(year, month, 2, 3), -2)
    if rule == RULE_CRYPTO_LAST_FRI:
        return _last_weekday_of_month(year, month, 4)
    if rule == RULE_RATES_LAST_BD_PRIOR:
        py, pm = _prev_month(year, month)
        return _last_business_day(py, pm)
    if rule == RULE_PHYS_FND:
        py, pm = _prev_month(year, month)
        return _last_business_day(py, pm)
    if rule == RULE_CL:
        py, pm = _prev_month(year, month)
        return add_business_days(date(py, pm, 25), -3)
    if rule == RULE_NG:
        return add_business_days(date(year, month, 1), -3)
    raise ValueError(f"no expiry rule for {rule}")


@dataclass(frozen=True)
class ContractCycle:
    """A single listed contract: root + delivery month + its dates."""
    root: str
    year: int
    month: int
    last_trade: date
    roll_window_open: date

    @property
    def code(self) -> str:
        return f"{self.root}{MONTH_CODES[self.month]}{self.year % 10}"

    @property
    def full_code(self) -> str:
        return f"/{self.root}{MONTH_CODES[self.month]}{self.year % 100:02d}"

    def with_broker_expiry(self, real_last_trade: date, lead_days: int) -> "ContractCycle":
        return ContractCycle(self.root, self.year, self.month, real_last_trade,
                             add_business_days(real_last_trade, -lead_days))


def listed_cycles(root: str, on: date, count: int = 4) -> List[ContractCycle]:
    """The next `count` listed cycles whose last-trade date is >= `on`."""
    spec = get_spec(root)
    out: List[ContractCycle] = []
    y, m = on.year, on.month
    for _ in range(36):
        if MONTH_CODES[m] in spec.months:
            lt = last_trade_date(root, y, m)
            if lt >= on:
                out.append(ContractCycle(root, y, m, lt,
                                         add_business_days(lt, -spec.roll_lead_days)))
                if len(out) == count:
                    break
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def front_and_back(root: str, on: date) -> Tuple[ContractCycle, ContractCycle]:
    cycles = listed_cycles(root, on, 2)
    if len(cycles) < 2:
        raise RuntimeError(f"could not resolve two cycles for {root} on {on}")
    return cycles[0], cycles[1]


# ─── ROLL STATE MACHINE ───────────────────────────────────────────────────────
# States are the operator's vocabulary, not the implementer's. otv3 v3.4 had to
# rename ORBState because RANGING meant two unrelated things in two files.
OFF_WINDOW = "OFF_WINDOW"          # roll window has not opened
WINDOW_OPEN = "WINDOW_OPEN"        # in the window, front still dominant
CROSSOVER = "CROSSOVER_CONFIRMED"  # back month has taken volume leadership
FORCED = "FORCED"                  # deadline reached; roll regardless of volume
ROLLED = "ROLLED"                  # position/quotes now on the back month


@dataclass
class RollAssessment:
    root: str
    state: str
    front: ContractCycle
    back: ContractCycle
    sessions_back_led: int
    reason: str
    days_to_last_trade: int
    should_roll: bool
    hard_deadline: date


def assess_roll(root: str,
                on: date,
                volume_history: Optional[List[Tuple[date, float, float]]] = None,
                confirm_sessions: int = 2,
                hard_deadline_days: int = 2,
                already_rolled: bool = False) -> RollAssessment:
    """Decide whether the front month should be abandoned.

    volume_history: completed sessions, oldest→newest, as
        (session_date, front_volume, back_volume).
    Only sessions inside the roll window count — a back month leading volume
    three weeks out means nothing, and counting it is how a naive roll fires
    early into an illiquid contract.

    RULE (operator spec 2026-07-25): roll on VOLUME in the normal window.
      1. Window opens `roll_lead_days` business days before last-trade/FND.
      2. Inside the window, `confirm_sessions` CONSECUTIVE completed sessions
         with back_volume > front_volume confirm the crossover.
      3. A hard deadline overrides volume entirely: physically-delivered
         contracts must be off the front month `hard_deadline_days` business
         days before First Notice, because a delivery notice is not a P&L event
         you negotiate. Cash-settled: one business day before last trade.
    """
    spec = get_spec(root)
    front, back = front_and_back(root, on)
    days_left = (front.last_trade - on).days

    deadline = add_business_days(
        front.last_trade,
        -(hard_deadline_days if spec.physically_delivered else 1))

    if already_rolled:
        return RollAssessment(root, ROLLED, front, back, 0,
                              "already rolled to back month", days_left, False, deadline)

    if on >= deadline:
        return RollAssessment(root, FORCED, front, back, 0,
                              ("first-notice deadline" if spec.physically_delivered
                               else "last-trade deadline"),
                              days_left, True, deadline)

    if on < front.roll_window_open:
        return RollAssessment(root, OFF_WINDOW, front, back, 0,
                              f"window opens {front.roll_window_open}",
                              days_left, False, deadline)

    streak = 0
    for sess_date, fv, bv in reversed(volume_history or []):
        if sess_date < front.roll_window_open or sess_date > on:
            continue
        if bv > fv:
            streak += 1
        else:
            break

    if streak >= confirm_sessions:
        return RollAssessment(root, CROSSOVER, front, back, streak,
                              f"back month led volume {streak} consecutive session(s)",
                              days_left, True, deadline)

    return RollAssessment(root, WINDOW_OPEN, front, back, streak,
                          f"window open, front still leading (streak {streak}/{confirm_sessions})",
                          days_left, False, deadline)
