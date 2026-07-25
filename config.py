"""
futures_trader_v1/config.py — v0.5
v0.5 — 2026-07-25 — BP_GATE_ENABLED (follows trading mode: off in paper, on in
        live) and BP_MIN_HEADROOM_PCT for the buying-power gate.
v0.4 — 2026-07-25 — PAPER equity is a FIXED $25,000 constant, never resolved
        from the broker (operator directive: the futures account is not funded
        yet, and sizing that drifts with a live balance cannot be used to
        calibrate a dial).
v0.3 — 2026-07-25 — MODE ELIGIBILITY POLICY thresholds:
        OVERNIGHT_MARGIN_CEILING_USD, OVERNIGHT_MAX_MIN_STOP_PCT,
        SCALP_MAX_MIN_STOP_PCT. These decide which root/mode pairs are marked
        X (excluded, permanent) rather than 0 (unaffordable today).
v0.2 — 2026-07-25 — Risk expressed as a PERCENTAGE of equity (risk_per_trade())
        so paper and live occupy the same R-space; ACCOUNT_EQUITY_DEFAULT
        ($25k funded baseline); OVERNIGHT_GAP_MULT — a futures stop is not
        guaranteed through the break or a weekend.
v0.1 — 2026-07-25 — Initial build. Every tunable, every credential accessor,
        one box = one symbol = one mode.

CONVENTIONS INHERITED FROM options_trader_v3, DELIBERATELY:
  * Credentials come from the environment ONLY, never from source.
  * FT_PAPER_TRADING defaults True and must stay True in this file forever.
  * Every threshold is env-overridable (FT_*) so a box can be A/B'd without a
    deploy, and so the epoch calibration can move a dial without a code change.
  * NOTHING is expressed as a percentage OF PRICE. Distances are TICKS or ATR
    multiples. This closes, at birth, the entire otv3 tolerance-bug family.
  * MIN_RRR IS WIRED AND GATING. In otv3 it sat unreferenced for the project's
    whole life (defect F) while the sweep book ran 75% wins and −$3,444 net on
    pure payoff asymmetry. A futures engine that cannot express "I refuse this
    trade because the reward does not pay for the risk" has no business sizing
    a contract.
"""

from __future__ import annotations

import os
from typing import List

from data.contract_registry import get_spec, ROOTS

# ─── ENVIRONMENT HELPERS ──────────────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: List[str]) -> List[str]:
    v = os.environ.get(name)
    if not v:
        return list(default)
    return [x.strip().upper() for x in v.replace(";", ",").split(",") if x.strip()]


# ─── IDENTITY: ONE BOX = ONE SYMBOL = ONE MODE ────────────────────────────────
SYMBOL = _env("FT_SYMBOL", "MNQ").upper().lstrip("/")
MODE = _env("FT_MODE", "DAY").upper()          # DAY | SCALP | SWING | HEDGE
VALID_MODES = ("DAY", "SCALP", "SWING", "HEDGE")
SPEC = get_spec(SYMBOL)
BOX_NAME = f"{SYMBOL}-{MODE}"

# ─── CREDENTIALS (environment only) ───────────────────────────────────────────
TT_CLIENT_SECRET = _env("FT_TT_CLIENT_SECRET")
TT_REFRESH_TOKEN = _env("FT_TT_REFRESH_TOKEN")
TT_ACCOUNT_NUMBER = _env("FT_TT_ACCOUNT")       # the LLC futures account
TELEGRAM_TOKEN = _env("FT_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _env("FT_TELEGRAM_CHAT_ID")

# ─── MODE ─────────────────────────────────────────────────────────────────────
PAPER_TRADING = _env("FT_PAPER_TRADING", "True") != "False"
POLL_INTERVAL_SECONDS = _i("FT_POLL_SECONDS", 15)

# ─── SESSIONS ─────────────────────────────────────────────────────────────────
# Which session phases this box may OPEN in. Set from configure.sh.
# Phases: ASIA LONDON NY_PRE NY_RTH NY_POST  (plus the literal "ETH" flag, which
# permits an intraday-mode box to trade outside its contract's RTH).
_DEFAULT_SESSIONS = {
    "SCALP": ["NY_RTH"],
    "DAY":   ["NY_RTH"],
    "SWING": ["LONDON", "NY_PRE", "NY_RTH", "NY_POST"],
    "HEDGE": ["ASIA", "LONDON", "NY_PRE", "NY_RTH", "NY_POST"],
}
ENABLED_SESSIONS = _list("FT_SESSIONS", _DEFAULT_SESSIONS.get(MODE, ["NY_RTH"]))
ENTRY_CUTOFF_MIN = _f("FT_ENTRY_CUTOFF_MIN", 30.0)   # intraday: no new entries inside this
FLATTEN_LEAD_MIN = _f("FT_FLATTEN_LEAD_MIN", 5.0)    # intraday: be flat this early
KILLZONE_REQUIRED = _b("FT_KILLZONE_REQUIRED", MODE == "SCALP")
KILLZONES_ENABLED = _list("FT_KILLZONES", ["LONDON", "NY_AM", "SILVER_BULLET", "NY_PM"])

# ─── SIZING (CONTRACTS — the unit of everything) ──────────────────────────────
# Operator spec: "Sizing should be number of contracts — differs by box."
BASE_CONTRACTS = _i("FT_BASE_CONTRACTS", 1)
MAX_CONTRACTS = _i("FT_MAX_CONTRACTS", 3)
GRADE_SIZE_MULTIPLIER = {"A": 1.5, "B": 1.0}         # no Grade C — below B never fires
# The dollar ceiling a single trade's structural stop may cost. Sizing solves
# contracts = floor(RISK_PER_TRADE / (stop_ticks * tick_value)) and then clamps
# to MAX_CONTRACTS. If the answer is < 1 the trade does not fire — a stop that
# cannot be afforded at one contract is a stop that must not be widened.
# Account equity. Resolution order at call time: live broker snapshot ->
# FT_ACCOUNT_EQUITY -> this default. The capacity calculator ALWAYS reports
# which source it used and when, because a sizing table computed off a stale
# balance is worse than no table.
ACCOUNT_EQUITY_DEFAULT = _f("FT_ACCOUNT_EQUITY", 25000.0)
# PAPER equity is a FIXED CONSTANT and is never resolved from the broker.
# The futures account is not funded yet, so there is no live balance to read —
# and even once there is, a paper book whose sizing drifts with a live balance
# cannot be used to calibrate a dial, because every table would be computed
# against a different account than the last one. Firm $25,000 until told
# otherwise. Override deliberately with FT_PAPER_EQUITY.
PAPER_EQUITY_DEFAULT = _f("FT_PAPER_EQUITY", 25000.0)
ACCOUNT_EQUITY_EXPLICIT = "FT_ACCOUNT_EQUITY" in os.environ

# Risk is a PERCENTAGE of equity, with a dollar override. This is what keeps
# paper and live in the SAME R-SPACE: at $25k live 1% is $250, at $100k paper
# 1% is $1,000, and both take the same trades at the same stop distances for
# the same R. Only the contract counts differ, so expectancy in R transfers
# directly from the paper book to the live account. A flat dollar budget across
# two equity levels would NOT transfer — it would change which trades exist.
RISK_PCT_OF_EQUITY = _f("FT_RISK_PCT", 0.01)
RISK_PER_TRADE_USD_OVERRIDE = _f("FT_RISK_USD", 0.0)   # 0 = derive from equity


def risk_per_trade(equity: float) -> float:
    """The per-trade risk budget at a given equity. Always call this rather than
    reading a constant — the whole point is that it moves with the account."""
    if RISK_PER_TRADE_USD_OVERRIDE > 0:
        return RISK_PER_TRADE_USD_OVERRIDE
    return max(equity * RISK_PCT_OF_EQUITY, 0.0)


# Retained for modules that need a nominal figure before an equity snapshot
# exists (boot-time validation only — never for sizing a live trade).
RISK_PER_TRADE_USD = risk_per_trade(PAPER_EQUITY_DEFAULT if PAPER_TRADING
                                    else ACCOUNT_EQUITY_DEFAULT)
DAILY_LOSS_LIMIT_USD = _f("FT_DAILY_LOSS_LIMIT", RISK_PER_TRADE_USD * 2)
# Net semantics, identical to otv3: wins offset losses; only a genuinely red day
# halts new entries. Open positions keep being managed.
WEEKLY_LOSS_LIMIT_USD = _f("FT_WEEKLY_LOSS_LIMIT", DAILY_LOSS_LIMIT_USD * 3)

# ─── RISK/REWARD — WIRED, AND IT GATES ────────────────────────────────────────
MIN_RRR = _f("FT_MIN_RRR", 2.0)
MIN_RRR_BY_MODE = {
    "SCALP": _f("FT_MIN_RRR_SCALP", 1.5),
    "DAY":   _f("FT_MIN_RRR_DAY", 2.0),
    "SWING": _f("FT_MIN_RRR_SWING", 3.0),
    "HEDGE": _f("FT_MIN_RRR_HEDGE", 0.0),   # a hedge is insurance, not an edge
}
# Minimum structural stop, in ticks, below which the stop is inside the spread
# and the "R" is fiction. Per-contract floor lives in the registry; this is a
# global multiplier on it.
# Overnight gap multiplier. A futures stop is not guaranteed through the daily
# break or a weekend — price gaps THROUGH it. Overnight modes therefore size as
# if the stop will be exceeded by this factor, which roughly halves swing size
# for the same budget. Conservative on purpose; it is a dial, not a law.
OVERNIGHT_GAP_MULT = _f("FT_OVERNIGHT_GAP_MULT", 2.0)
# ── MODE ELIGIBILITY POLICY (see risk/eligibility.py) ────────────────────────
# Roots whose overnight initial margin exceeds this are DAY/SCALP only,
# permanently — a full-size NQ is not an overnight carry on this account at any
# plausible balance. The micro sibling is the vehicle for those strategies.
OVERNIGHT_MARGIN_CEILING_USD = _f("FT_OVN_MARGIN_CEILING", 7000.0)
# The gap-adjusted MINIMUM stop may not consume more than this share of the
# risk budget, or the contract is too coarse to carry overnight.
OVERNIGHT_MAX_MIN_STOP_PCT = _f("FT_OVN_MAX_MIN_STOP_PCT", 0.50)
# A scalp needs room to be wrong: the tightest sane stop must stay under this
# share of the budget, or the contract is not scalpable at this account size.
SCALP_MAX_MIN_STOP_PCT = _f("FT_SCALP_MAX_MIN_STOP_PCT", 0.40)
MIN_STOP_TICK_MULT = _f("FT_MIN_STOP_TICK_MULT", 1.0)
# Maximum stop, as an ATR multiple. A stop wider than this means the setup is
# not where we think it is.
MAX_STOP_ATR_MULT = _f("FT_MAX_STOP_ATR_MULT", 2.5)

# ─── SCALE-OUT (a futures-native capability options sizing could not express) ─
# otv3 excursion data: winners trailed out ~+25% off a ~+60% MFE peak while
# losers ran to a wide stop. With multi-contract sizing the fix is structural:
# bank a piece at the first objective, let the rest run on structure.
SCALE_OUT_ENABLED = _b("FT_SCALE_OUT", True)
SCALE_OUT_AT_R = _f("FT_SCALE_OUT_R", 1.0)          # take partial at +1R
SCALE_OUT_FRACTION = _f("FT_SCALE_OUT_FRACTION", 0.5)
MOVE_STOP_TO_BE_AT_R = _f("FT_BE_AT_R", 1.0)        # breakeven ratchet
TRAIL_ARM_AT_R = _f("FT_TRAIL_ARM_R", 1.5)
TRAIL_ATR_MULT = _f("FT_TRAIL_ATR_MULT", 2.0)       # chandelier fallback
TRAIL_STRUCTURE_BUFFER_TICKS = _i("FT_TRAIL_BUFFER_TICKS", 2)

# ─── MARGIN ───────────────────────────────────────────────────────────────────
# Day/scalp size against the intraday (reduced) rate; swing/hedge MUST size
# against the full overnight initial rate, because the position will meet it.
MARGIN_UTILIZATION_MAX = _f("FT_MARGIN_UTIL_MAX", 0.35)   # of net liq, this box
MARGIN_BUFFER_MULT = _f("FT_MARGIN_BUFFER_MULT", 1.25)    # cushion over requirement
OVERNIGHT_MARGIN_CHECK_ET = (16, 30)                      # pre-emptive de-risk check
MARGIN_CALL_ALERT_ONLY = _b("FT_MARGIN_CALL_ALERT_ONLY", True)
USE_BROKER_MARGIN = _b("FT_USE_BROKER_MARGIN", True)      # seeds -> broker truth
# ── BUYING-POWER GATE (the fleet-exposure check) ─────────────────────────────
# One shared account means the broker's buying power already nets off every
# other box's margin, so a single read at order time IS the fleet check.
# FOLLOWS TRADING MODE by default: off in paper (no real balance to gate
# against), on in live. Nothing to remember on go-live — the same pattern that
# made broker reconciliation reliable in the options engine.
BP_GATE_ENABLED = _b("FT_BP_GATE", not PAPER_TRADING)
# Never spend the last dollar: an adverse move before the stop needs somewhere
# to go, or a normal loser becomes a margin call.
BP_MIN_HEADROOM_PCT = _f("FT_BP_HEADROOM", 0.20)

# ─── ROLL ─────────────────────────────────────────────────────────────────────
ROLL_CONFIRM_SESSIONS = _i("FT_ROLL_CONFIRM_SESSIONS", 2)
ROLL_HARD_DEADLINE_DAYS = _i("FT_ROLL_DEADLINE_DAYS", 2)
ROLL_AS_CALENDAR_SPREAD = _b("FT_ROLL_CALENDAR_SPREAD", True)   # one order, not two legs
ROLL_AUTO = _b("FT_ROLL_AUTO", True)          # False = alert and wait for the menu
ROLL_ONLY_WHEN_FLAT = _b("FT_ROLL_ONLY_WHEN_FLAT", False)

# ─── REGIME (L1 -> L2 -> L3, ported architecture, futures evidence) ───────────
REGIME_ENGINE = _env("FT_REGIME_ENGINE", "l2")     # l1 | l2  (rollback lever)
CONVICTION_GATES_LIVE = _b("FT_CONVICTION_GATES", False)   # L3 — OFF until Epoch 4
ADX_TREND_THRESHOLD = _f("FT_ADX_TREND", 25.0)
ATR_PERIOD = _i("FT_ATR_PERIOD", 14)
BB_PERIOD = _i("FT_BB_PERIOD", 20)
BB_STD = _f("FT_BB_STD", 2.0)
REGIME_REASSESS_MINUTES = _i("FT_REGIME_REASSESS_MIN", 5)
# L2 integrator
INTEGRATOR_THETA_HOLD = _f("FT_INT_THETA_HOLD", 0.35)
INTEGRATOR_THETA_COMMIT = _f("FT_INT_THETA_COMMIT", 0.55)
INTEGRATOR_DISPLACEMENT = _f("FT_INT_DISPLACEMENT", 0.12)
INTEGRATOR_HALFLIFE_S = _f("FT_INT_HALFLIFE_S", 180.0)

# ─── CONVICTION DIMENSIONS ────────────────────────────────────────────────────
# Every NEW dimension ships at weight 0 and is calibrated to realized edge.
# That is not caution theatre — it is the one otv3 practice that repeatedly
# prevented an unvalidated idea from silently moving live size.
SCORE_WEIGHTS = {
    "level_tier":        _f("FT_W_LEVEL", 0.25),
    "orderflow_confirm": _f("FT_W_FLOW", 0.20),
    "structure_align":   _f("FT_W_STRUCT", 0.20),
    "regime_conviction": _f("FT_W_REGIME", 0.15),
    "pd_position":       _f("FT_W_PD", 0.10),
    "session_context":   _f("FT_W_SESSION", 0.10),
    "smt_divergence":    _f("FT_W_SMT", 0.0),     # Epoch 2 candidate — shadow
    "profile_context":   _f("FT_W_PROFILE", 0.0), # Epoch 2 candidate — shadow
}
GRADE_A_MIN_SCORE = _f("FT_GRADE_A", 0.75)
GRADE_B_MIN_SCORE = _f("FT_GRADE_B", 0.55)

# ─── LEVEL HIERARCHY ──────────────────────────────────────────────────────────
# Built in from day one, straight out of the otv3 2026-07-24 observation that a
# flat `is_named` boolean was letting genuine overnight raids fall into the
# low-conviction bucket and corrupt the sweep postmortem.
LEVEL_TIERS = {
    "OVERNIGHT_HIGH": 1.00, "OVERNIGHT_LOW": 1.00,
    "PDH": 1.00, "PDL": 1.00,
    "WEEKLY_HIGH": 0.90, "WEEKLY_LOW": 0.90,
    "HISTORIC_SR": 0.70,
    "NAKED_POC": 0.65,
    "SESSION_HIGH": 0.50, "SESSION_LOW": 0.50,
    "VALUE_AREA_EDGE": 0.45,
    "EQUAL_HL": 0.30,
}

# ─── ORDER FLOW ───────────────────────────────────────────────────────────────
ORDERFLOW_ENABLED = _b("FT_ORDERFLOW", True)
CVD_LOOKBACK_BARS = _i("FT_CVD_LOOKBACK", 20)
ABSORPTION_DELTA_TICKS = _f("FT_ABSORPTION_DELTA_TICKS", 3.0)  # flow vs price stall
DELTA_DIVERGENCE_MIN = _f("FT_DELTA_DIV_MIN", 0.25)

# ─── EXECUTION ────────────────────────────────────────────────────────────────
# Never cross the spread on an open; escalate only on a forced exit. Straight
# port of the otv3 limit-ladder policy, which was a genuine cost win.
ENTRY_ORDER_TYPE = _env("FT_ENTRY_ORDER", "LIMIT_AT_MARK")
ENTRY_LIMIT_MAX_TICKS_THROUGH = _i("FT_ENTRY_MAX_THROUGH", 0)
EXIT_LIMIT_REPRICE_EVERY_TICK = _b("FT_EXIT_REPRICE", True)
FORCED_EXIT_MARKET = _b("FT_FORCED_EXIT_MARKET", True)   # flatten window crosses
LIVE_FILL_POLL_SECONDS = _f("FT_FILL_POLL_S", 1.0)
LIVE_FILL_DEADLINE_SECONDS = _f("FT_FILL_DEADLINE_S", 20.0)
PAPER_SLIPPAGE_TICKS = _f("FT_PAPER_SLIPPAGE_TICKS", 1.0)
# Paper honesty: options paper booked the mark and stayed optimistic on FILL
# RATE. Futures paper pays one tick by default because on a liquid contract that
# IS the realistic cost of a marketable entry, and pretending otherwise makes
# every scalp backtest lie.
COMMISSION_PER_CONTRACT_RT = _f("FT_COMMISSION_RT", 2.50)  # round turn, all-in

# ─── DATA ─────────────────────────────────────────────────────────────────────
FEED_STALE_SECONDS = _f("FT_FEED_STALE_S", 120.0)
TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
CANDLE_STORE = _env("FT_CANDLE_STORE", "data/feed_store.db")
TRADES_DB = _env("FT_TRADES_DB", "trades.db")
JOURNAL_DIR = _env("FT_JOURNAL_DIR", "data/signal_journal")

# ─── HEDGE MODE (standalone; inputs prompted by configure.sh) ─────────────────
HEDGE_PORTFOLIO_VALUE = _f("FT_HEDGE_PORTFOLIO_USD", 0.0)
HEDGE_BETA = _f("FT_HEDGE_BETA", 1.0)
HEDGE_TARGET_RATIO = _f("FT_HEDGE_RATIO", 0.5)        # fraction of exposure covered
HEDGE_REBALANCE_DRIFT = _f("FT_HEDGE_DRIFT", 0.10)    # rebalance past 10% drift
HEDGE_CONDITIONAL = _b("FT_HEDGE_CONDITIONAL", False) # True = only when regime says so
HEDGE_MAX_CONTRACTS = _i("FT_HEDGE_MAX_CONTRACTS", 20)

# ─── SANITY ───────────────────────────────────────────────────────────────────
def validate() -> List[str]:
    """Called at boot and by devtools. Returns a list of problems; an empty list
    means the box is configured coherently. Fail LOUD at startup, never silently
    fall back — the otv3 lesson from a guarded config import that ran an entire
    module on defaults for a week."""
    problems: List[str] = []
    if SYMBOL not in ROOTS:
        problems.append(f"FT_SYMBOL={SYMBOL} is not in the contract registry")
    if MODE not in VALID_MODES:
        problems.append(f"FT_MODE={MODE} must be one of {VALID_MODES}")
    if BASE_CONTRACTS < 1 or MAX_CONTRACTS < BASE_CONTRACTS:
        problems.append("BASE_CONTRACTS/MAX_CONTRACTS incoherent")
    if RISK_PER_TRADE_USD <= 0:
        problems.append("FT_RISK_USD must be > 0")
    if MODE == "HEDGE" and HEDGE_PORTFOLIO_VALUE <= 0:
        problems.append("HEDGE mode requires FT_HEDGE_PORTFOLIO_USD (run configure.sh)")
    if not PAPER_TRADING and not (TT_CLIENT_SECRET and TT_REFRESH_TOKEN and TT_ACCOUNT_NUMBER):
        problems.append("LIVE mode without complete TastyTrade credentials")
    if abs(sum(SCORE_WEIGHTS.values()) - 1.0) > 0.001 and CONVICTION_GATES_LIVE:
        problems.append(f"score weights sum to {sum(SCORE_WEIGHTS.values()):.3f}, not 1.0")
    return problems


def min_rrr() -> float:
    return MIN_RRR_BY_MODE.get(MODE, MIN_RRR)


def is_intraday() -> bool:
    return MODE in ("DAY", "SCALP")
