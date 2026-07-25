"""
futures_trader_v1/control/orchestrator.py — v0.2
v0.2 — 2026-07-25 — WAKE GATE compares real datetimes instead of bare times.
        The time-of-day comparison read Saturday 09:00 as due against an 08:45
        wake (session_date correctly points at Monday), which would have started
        the entire fleet two days early and left it running all weekend.
v0.1 — 2026-07-25 — Initial build. Mode-aware and session-aware wake/stop.

WHY THIS IS NOT THE OPTIONS ORCHESTRATOR WITH THE NAMES CHANGED.

The options fleet had one rhythm: wake everything at 09:17, stop everything at
15:55. That worked because every box traded one session and held nothing
overnight. Two futures facts break it:

  SWING AND HEDGE BOXES MUST NOT BE STOPPED. An EOD sweep that stops whatever is
  running — the options design, and a good one there — would kill the process
  managing an open overnight position. The position would survive at the broker
  with nothing watching it: no stop management, no roll, no margin publication.
  That is the single most dangerous thing this control plane could do, so the
  stop sweep is MODE-FILTERED and overnight boxes are excluded BY CONSTRUCTION,
  not by remembering to pass a flag.

  BOXES TRADE DIFFERENT SESSIONS. A London-killzone scalper and an RTH day box
  do not share a wake time. Wake is computed per box from the sessions it has
  enabled, so the fleet comes up in waves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, List, Optional, Tuple

from control import ec2ops
from control import fleet_config as FC
from control.fleet import Fleet
from utils import sessions as S

logger = logging.getLogger(__name__)

# When each session phase begins, ET. A box wakes LEAD_MINUTES before the
# earliest phase it is allowed to trade, so its feed has warmed by then.
PHASE_START = {"ASIA": dtime(18, 0), "LONDON": dtime(2, 0),
               "NY_PRE": dtime(7, 0), "NY_RTH": dtime(9, 30),
               "NY_POST": dtime(16, 0)}
LEAD_MINUTES = 45


@dataclass
class WakePlan:
    to_start: List[ec2ops.Instance] = field(default_factory=list)
    already_up: List[ec2ops.Instance] = field(default_factory=list)
    not_yet: List[Tuple[str, str]] = field(default_factory=list)   # (box, when)
    reason: str = ""


@dataclass
class StopPlan:
    to_stop: List[ec2ops.Instance] = field(default_factory=list)
    protected: List[str] = field(default_factory=list)
    reason: str = ""


def wake_time_for(mode: str, enabled_sessions: List[str]) -> dtime:
    """The earliest moment a box could need to be awake, minus warm-up."""
    phases = [p for p in (enabled_sessions or ["NY_RTH"]) if p in PHASE_START]
    if not phases:
        phases = ["NY_RTH"]
    earliest = min(PHASE_START[p] for p in phases)
    dt = datetime.combine(date(2000, 1, 1), earliest) - timedelta(minutes=LEAD_MINUTES)
    return dt.time()


class Orchestrator:
    def __init__(self, fleet: Optional[Fleet] = None,
                 sessions_by_box: Optional[Dict[str, List[str]]] = None):
        self.fleet = fleet or Fleet()
        # Control does not invent a box's sessions; it reads what the box was
        # configured with, defaulting to RTH when it has not reported.
        self.sessions_by_box = sessions_by_box or {}

    def _sessions(self, inst) -> List[str]:
        return self.sessions_by_box.get(inst.box, ["NY_RTH"])

    # ── wake ─────────────────────────────────────────────────────────────────
    def plan_wake(self, now: Optional[datetime] = None) -> WakePlan:
        now = now or S.now_et()
        plan = WakePlan()
        if not S.is_trading_day(S.session_date(now)):
            plan.reason = "not a trading day"
            return plan
        for inst in self.fleet.instances():
            wt = wake_time_for(inst.mode, self._sessions(inst))
            due = _due(now, wt)
            if inst.state == ec2ops.RUNNING:
                plan.already_up.append(inst)
            elif due:
                plan.to_start.append(inst)
            else:
                plan.not_yet.append((inst.box, wt.strftime("%H:%M")))
        plan.reason = (f"{len(plan.to_start)} to start, "
                       f"{len(plan.already_up)} already up, "
                       f"{len(plan.not_yet)} not yet due")
        return plan

    def wake(self, now: Optional[datetime] = None,
             dry_run: bool = False) -> WakePlan:
        plan = self.plan_wake(now)
        if plan.to_start and not dry_run:
            self.fleet.start(plan.to_start)
        return plan

    # ── stop ─────────────────────────────────────────────────────────────────
    def plan_stop(self, include_overnight: bool = False) -> StopPlan:
        """Stop the INTRADAY boxes only.

        `include_overnight` exists for a deliberate operator action (an
        emergency, a maintenance window) and defaults to False. The protection
        is structural: an overnight box is never in `to_stop` unless someone
        explicitly asked for it, so no code path can stop a box that is holding
        a position by forgetting an argument.
        """
        plan = StopPlan()
        for inst in self.fleet.running():
            if inst.mode.upper() in FC.OVERNIGHT_MODES and not include_overnight:
                plan.protected.append(inst.box)
            else:
                plan.to_stop.append(inst)
        plan.reason = (f"stopping {len(plan.to_stop)} intraday box(es); "
                       f"{len(plan.protected)} overnight box(es) PROTECTED"
                       if not include_overnight else
                       f"stopping ALL {len(plan.to_stop)} running box(es)")
        return plan

    def stop(self, include_overnight: bool = False,
             dry_run: bool = False) -> StopPlan:
        plan = self.plan_stop(include_overnight)
        if plan.to_stop and not dry_run:
            self.fleet.stop(plan.to_stop)
        return plan

    # ── emergency ────────────────────────────────────────────────────────────
    def emergency_flatten(self) -> List:
        """Tell every running box to flatten and halt. Does NOT stop instances:
        a stopped box cannot confirm it is flat, and 'probably flat' is not a
        state this system accepts."""
        return self.fleet.run(
            "touch HALT && venv/bin/python -c "
            "\"print('halt flag set')\"")


def wake_datetime(session: date, wake_t: dtime) -> datetime:
    """The actual moment this box should come up for `session`.

    A wake time at or after 17:00 ET belongs to the CALENDAR DAY BEFORE the
    session it serves — an Asia box wakes Sunday 17:15 for Monday's session.
    """
    day = session - timedelta(days=1) if wake_t >= dtime(17, 0) else session
    return datetime.combine(day, wake_t, tzinfo=S.ET)


def _due(now: datetime, wake_t: dtime) -> bool:
    """Compare real datetimes, never bare times.

    The time-of-day version had a real bug: on a Saturday morning
    `session_date()` correctly attributes forward to Monday, so a clock
    comparison of 09:00 against a 08:45 wake time read as DUE and would have
    started the whole fleet two days early — instances running all weekend,
    feeds subscribed, for a session that had not begun.
    """
    sess = S.session_date(now)
    wdt = wake_datetime(sess, wake_t)
    end = datetime.combine(sess, dtime(17, 0), tzinfo=S.ET)
    return wdt <= now < end
