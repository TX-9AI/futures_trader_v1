"""
futures_trader_v1/analysis/opening_range.py — v0.1
v0.1 — 2026-07-25 — Initial build. The opening-range state machine, ported with
        its definitions intact.

THE MOST VALIDATED MECHANIC IN THE LINEAGE, AND EVERY WORD OF IT IS DELIBERATE.

    RANGE  = the contract's first N minutes of RTH. The registry supplies each
             contract's own RTH open, so gold's range is 08:20-08:25.
    BREAK  = a 1m candle that OPENS INSIDE the range and CLOSES OUTSIDE it.
    RETEST = a SUBSEQUENT 1m candle within ORB_MAX_RETEST_BARS whose WICK
             enters the range and whose BODY stays entirely OUTSIDE.
    STOP   = beyond the impulsive (break) candle's WICK.

WHY "OPENS INSIDE" IS DEFINITIONAL. It is an OPENING-RANGE break. A candle that
began life outside the range never broke out of it — it was already out. That is
late continuation, and admitting it was worth measurable inverted-risk entries.

WHY THERE IS NO BUFFER AND NO GRACE BAND. The retest IS the noise filter: a
marginal break that means nothing simply fails its retest. The options engine
carried a 0.05%-of-price break buffer that meant $0.49 on one symbol and $3.00
on another, and a grace band that admitted a candle whose body closed back
INSIDE the range — the disarm condition — as a confirmed retest.

BARS ARE BARS, NOT POLL TICKS. The retest window counts DEDUPED 1-minute
candles. The options engine counted 15-second loop iterations as bars, inflating
4x, and killed a live armed window in about three minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import List, Optional

from data.series import Candles
from utils.sessions import ET, to_et

NO_RANGE = "NO_RANGE"
WAITING_FOR_BREAK = "WAITING_FOR_BREAK"
ARMED_LONG = "ARMED_LONG"
ARMED_SHORT = "ARMED_SHORT"
CONFIRMED_LONG = "CONFIRMED_LONG"
CONFIRMED_SHORT = "CONFIRMED_SHORT"
EXPIRED = "EXPIRED"

CLOSE_INSIDE = "close_inside"   # re-arms
RUNAWAY = "runaway"             # terminal — hands off to continuation/sweep
TIMEOUT = "timeout"             # re-arms with a fresh window


@dataclass
class ORBState:
    state: str = NO_RANGE
    high: Optional[float] = None
    low: Optional[float] = None
    established: bool = False
    break_index: Optional[int] = None
    break_high: Optional[float] = None
    break_low: Optional[float] = None
    impulsive_stop: Optional[float] = None
    retest_index: Optional[int] = None
    retest_depth_ticks: Optional[float] = None
    invalidation: Optional[str] = None
    broke_high: bool = False        # session-level latches, maintained always
    broke_low: bool = False
    bars_since_break: int = 0
    attempts: int = 0

    @property
    def width(self) -> Optional[float]:
        if self.high is None or self.low is None:
            return None
        return self.high - self.low

    @property
    def confirmed(self) -> bool:
        return self.state in (CONFIRMED_LONG, CONFIRMED_SHORT)

    @property
    def direction(self) -> str:
        return ("LONG" if self.state == CONFIRMED_LONG else
                "SHORT" if self.state == CONFIRMED_SHORT else "")


def build_range(c1: Candles, spec, window_minutes: int = 5,
                session: Optional[date] = None) -> ORBState:
    """The opening range from the contract's own RTH open. Returns
    `established=False` rather than a guess when the window is incomplete — a
    carried prior-day range must never be tradeable."""
    st = ORBState()
    if not c1 or not len(c1):
        return st
    o = dtime(*spec.rth_open)
    end_dt = datetime.combine(date(2000, 1, 1), o) + timedelta(minutes=window_minutes)
    end = end_dt.time()
    sess = session
    idx = []
    for i, t in enumerate(c1.ts):
        et = to_et(t)
        if sess is not None and et.date() != sess:
            continue
        if o <= et.time() < end:
            idx.append(i)
    if not idx:
        return st
    st.high = max(c1.high[i] for i in idx)
    st.low = min(c1.low[i] for i in idx)
    # complete only when the window has actually elapsed
    last_et = to_et(c1.ts[-1])
    st.established = last_et.time() >= end or len(idx) >= window_minutes
    st.state = WAITING_FOR_BREAK if st.established else NO_RANGE
    return st


def update(st: ORBState, c1: Candles, spec,
           max_retest_bars: int = 12,
           runaway_r: float = 1.0,
           cutoff: Optional[dtime] = None,
           session: Optional[date] = None) -> ORBState:
    """Walk the session's 1m candles once and resolve the machine.

    Deliberately RE-DERIVED from the tape each call rather than incremented per
    poll tick. A pure function of the candles cannot drift, cannot double-count
    a candle seen twice by a 15-second loop, and replays identically offline.
    """
    if not st.established or st.high is None or not c1 or not len(c1):
        return st

    o = dtime(*spec.rth_open)
    end_dt = datetime.combine(date(2000, 1, 1), o) + timedelta(minutes=5)
    after = end_dt.time()

    bars = []
    for i in range(len(c1)):
        et = to_et(c1.ts[i])
        if session is not None and et.date() != session:
            continue
        if et.time() < after:
            continue
        if cutoff and et.time() >= cutoff:
            break
        bars.append(i)

    st.state = WAITING_FOR_BREAK
    st.break_index = st.retest_index = None
    st.impulsive_stop = None
    st.invalidation = None
    st.bars_since_break = 0
    armed = None            # ("LONG"/"SHORT", break_bar_position)

    for pos, i in enumerate(bars):
        op, hi, lo, cl = c1.open[i], c1.high[i], c1.low[i], c1.close[i]

        # session latches: a FACT about the session, maintained in every state,
        # close-based, with no origin gate. Other strategies read these.
        if cl > st.high:
            st.broke_high = True
        if cl < st.low:
            st.broke_low = True

        if armed is None:
            inside_open = st.low <= op <= st.high
            if inside_open and cl > st.high:
                armed = ("LONG", pos)
                st.break_index, st.break_high, st.break_low = i, hi, lo
                st.impulsive_stop = lo
                st.state = ARMED_LONG
                st.attempts += 1
            elif inside_open and cl < st.low:
                armed = ("SHORT", pos)
                st.break_index, st.break_high, st.break_low = i, hi, lo
                st.impulsive_stop = hi
                st.state = ARMED_SHORT
                st.attempts += 1
            continue

        direction, bpos = armed
        st.bars_since_break = pos - bpos

        # disarm: a close back inside the range falsifies the hypothesis
        if st.low <= cl <= st.high:
            st.invalidation = CLOSE_INSIDE
            armed = None
            st.state = WAITING_FOR_BREAK
            continue

        # runaway: reached the projected target with no retest. Terminal —
        # the setup is gone, but the DIRECTIONAL FORCE is real information and
        # is handed to trend continuation.
        width = st.width or 0.0
        if direction == "LONG" and hi >= st.high + width * runaway_r:
            st.invalidation = RUNAWAY
            st.state = EXPIRED
            return st
        if direction == "SHORT" and lo <= st.low - width * runaway_r:
            st.invalidation = RUNAWAY
            st.state = EXPIRED
            return st

        # retest: WICK in, BODY out. Measured in ticks, logged even on a miss.
        body_lo, body_hi = min(op, cl), max(op, cl)
        if direction == "LONG":
            depth = st.high - lo
            if lo <= st.high and body_lo > st.high:
                st.retest_index = i
                st.retest_depth_ticks = depth / spec.tick_size
                st.state = CONFIRMED_LONG
                return st
        else:
            depth = hi - st.low
            if hi >= st.low and body_hi < st.low:
                st.retest_index = i
                st.retest_depth_ticks = depth / spec.tick_size
                st.state = CONFIRMED_SHORT
                return st

        # timeout: the window is measured in REAL BARS. Expiry RE-ARMS — the
        # second attempt is often the cleaner one, and a terminal timeout blinds
        # the engine to a fresh break for the rest of the session.
        if st.bars_since_break >= max_retest_bars:
            st.invalidation = TIMEOUT
            armed = None
            st.state = WAITING_FOR_BREAK

    return st
