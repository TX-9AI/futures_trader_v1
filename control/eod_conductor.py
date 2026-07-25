"""
futures_trader_v1/control/eod_conductor.py — v0.1
v0.1 — 2026-07-25 — Initial build. The end-of-session chain.

ALWAYS-RUN, WARN-NEVER-STOP. Every phase is wrapped; a phase that fails records
a warning and the chain continues. The options EOD chain took months to reach
"it just runs", and the property that got it there was that no analysis phase
could ever break a recovery phase.

PHASE ORDER IS NOT ARBITRARY:

  1 MARGIN     read the fleet's exposure while the boxes are still up. This is
               the only phase that cannot be redone later.
  2 HARVEST    order flow FIRST — the irreplaceable dataset — then trades and
               journals. Also must happen before anything stops.
  3 STOP       intraday boxes only. Swing and hedge boxes are PROTECTED by the
               orchestrator's construction, not by an argument passed here.
  4 CONSOLIDATE  roll the per-box trade DBs into one dated view.
  5 EXPECTANCY report n / win% / avg win R / avg loss R / expectancy R per
               strategy. Win rate alone is the statistic that hid a 75%-winning,
               $3,444-losing book for two months.
  6 ROLL       check every root's roll window and report what is due.

THE ORDER OF 2 AND 3 IS LOAD-BEARING. Stopping first would make the data
unreachable until the next wake, and a box rebuilt in between would lose it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional

from control import fleet_config as FC
from control import notify
from control.fleet import Fleet
from control.harvest import Harvester
from control.margin_governor import MarginGovernor, from_reports
from control.orchestrator import Orchestrator
from data.contract_registry import ROOTS, assess_roll

logger = logging.getLogger(__name__)


@dataclass
class PhaseResult:
    name: str
    ok: bool
    headline: str = ""
    detail: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class ChainResult:
    day: date
    phases: List[PhaseResult] = field(default_factory=list)

    @property
    def failed(self) -> List[str]:
        return [p.name for p in self.phases if not p.ok]

    def summary(self) -> str:
        lines = [f"EOD {self.day.isoformat()}"]
        for p in self.phases:
            lines.append(f"{'OK ' if p.ok else 'WARN'} {p.name}: "
                         f"{p.headline or p.error}")
        return "\n".join(lines)


class Conductor:
    def __init__(self, fleet: Optional[Fleet] = None,
                 governor: Optional[MarginGovernor] = None,
                 harvester: Optional[Harvester] = None,
                 orchestrator: Optional[Orchestrator] = None,
                 notifier: Callable[[str], bool] = notify.send):
        self.fleet = fleet or Fleet()
        self.gov = governor or MarginGovernor()
        self.harvest = harvester or Harvester(self.fleet)
        self.orch = orchestrator or Orchestrator(self.fleet)
        self.notify = notifier

    def _phase(self, name: str, fn) -> PhaseResult:
        try:
            return fn()
        except Exception as e:                               # noqa: BLE001
            logger.exception("%s failed", name)
            return PhaseResult(name, False, error=str(e))

    def run(self, day: Optional[date] = None, dry_run: bool = False,
            stop_boxes: bool = True) -> ChainResult:
        day = day or date.today()
        res = ChainResult(day)

        res.phases.append(self._phase("margin", lambda: self._margin()))
        res.phases.append(self._phase("harvest", lambda: self._harvest(day)))
        if stop_boxes:
            res.phases.append(self._phase("stop", lambda: self._stop(dry_run)))
        res.phases.append(self._phase("consolidate", lambda: self._consolidate(day)))
        res.phases.append(self._phase("expectancy", lambda: self._expectancy(day)))
        res.phases.append(self._phase("roll", lambda: self._roll(day)))

        try:
            self.notify(res.summary())
        except Exception:                                    # noqa: BLE001
            pass
        return res

    # ── phases ───────────────────────────────────────────────────────────────
    def _margin(self) -> PhaseResult:
        rows = self.fleet.margin_usage()
        v = self.gov.assess(from_reports(rows))
        self.gov.publish(v)
        return PhaseResult("margin", v.status != "BREACH", v.headline(),
                           {"status": v.status, "findings": v.findings,
                            "stand_down": v.stand_down})

    def _harvest(self, day: date) -> PhaseResult:
        r = self.harvest.run(day)
        flow_failures = [f for f in r.failed if f.endswith(":orderflow")]
        return PhaseResult("harvest", not flow_failures, r.headline(),
                           {"failed": r.failed, "warnings": r.warnings})

    def _stop(self, dry_run: bool) -> PhaseResult:
        plan = self.orch.stop(include_overnight=False, dry_run=dry_run)
        return PhaseResult("stop", True, plan.reason,
                           {"stopped": [i.box for i in plan.to_stop],
                            "protected": plan.protected})

    def _consolidate(self, day: date) -> PhaseResult:
        src = os.path.join(FC.TRADES_DIR, day.isoformat())
        if not os.path.isdir(src):
            return PhaseResult("consolidate", True, "no trade DBs to consolidate")
        rows: List[dict] = []
        for fn in sorted(os.listdir(src)):
            if not fn.endswith(".db"):
                continue
            try:
                with closing(sqlite3.connect(
                        f"file:{os.path.join(src, fn)}?mode=ro", uri=True)) as c:
                    c.row_factory = sqlite3.Row
                    for r in c.execute("SELECT * FROM trades WHERE status='CLOSED'"):
                        rows.append(dict(r))
            except sqlite3.Error as e:
                logger.warning("could not read %s: %s", fn, e)
        # DEDUPE BY trade_id AND BUCKET BY session_date, never by filename —
        # the options rollups were filename-bucketed and 61% of rows sat in a
        # file whose date did not match their entry time.
        seen, clean = set(), []
        for r in rows:
            tid = r.get("trade_id")
            if tid in seen:
                continue
            seen.add(tid)
            if r.get("session_date") == day.isoformat():
                clean.append(r)
        os.makedirs(FC.REPORTS_DIR, exist_ok=True)
        out = os.path.join(FC.REPORTS_DIR, f"fleet_trades_{day.isoformat()}.json")
        with open(out, "w") as fh:
            json.dump(clean, fh, indent=2, default=str)
        return PhaseResult("consolidate", True,
                           f"{len(clean)} trade(s) dated {day} "
                           f"({len(rows) - len(clean)} deduped/other-date)",
                           {"file": out})

    def _expectancy(self, day: date) -> PhaseResult:
        f = os.path.join(FC.REPORTS_DIR, f"fleet_trades_{day.isoformat()}.json")
        if not os.path.exists(f):
            return PhaseResult("expectancy", True, "no consolidated trades")
        with open(f) as fh:
            rows = json.load(fh)
        by: Dict[str, List[float]] = {}
        for r in rows:
            by.setdefault(r.get("strategy", "?"), []).append(
                float(r.get("realized_r") or 0.0))
        report = {}
        for strat, rs in sorted(by.items()):
            wins = [x for x in rs if x > 0]
            losses = [x for x in rs if x <= 0]
            report[strat] = {
                "n": len(rs),
                "win_rate": round(len(wins) / len(rs), 3) if rs else 0.0,
                "avg_win_r": round(sum(wins) / len(wins), 3) if wins else 0.0,
                "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else 0.0,
                "expectancy_r": round(sum(rs) / len(rs), 3) if rs else 0.0}
        out = os.path.join(FC.REPORTS_DIR, f"expectancy_{day.isoformat()}.json")
        with open(out, "w") as fh:
            json.dump(report, fh, indent=2)
        # The headline names EXPECTANCY, never win rate alone.
        bits = [f"{k} n={v['n']} E={v['expectancy_r']:+.2f}R "
                f"(win {v['win_rate']*100:.0f}%)" for k, v in report.items()]
        return PhaseResult("expectancy", True, "; ".join(bits) or "no trades",
                           {"file": out, "report": report})

    def _roll(self, day: date) -> PhaseResult:
        due, watch = [], []
        for sym, _mode in FC.FLEET:
            root = "".join(ch for ch in sym if not ch.isdigit())
            if root not in ROOTS:
                continue
            try:
                a = assess_roll(root, day)
            except Exception:                                # noqa: BLE001
                continue
            if a.should_roll:
                due.append(f"{root} {a.front.code}->{a.back.code} ({a.reason})")
            elif a.state == "WINDOW_OPEN":
                watch.append(f"{root} {a.days_to_last_trade}d")
        head = (f"{len(due)} due" + (f": {'; '.join(due)}" if due else "")
                + (f" | watching {', '.join(watch)}" if watch else ""))
        return PhaseResult("roll", True, head, {"due": due, "watching": watch})


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s eod: %(message)s")
    ap = argparse.ArgumentParser(description="futures control EOD chain")
    ap.add_argument("--date")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-stop", action="store_true",
                    help="run the chain without stopping any box")
    a = ap.parse_args(argv)
    day = date.fromisoformat(a.date) if a.date else date.today()
    r = Conductor().run(day, dry_run=a.dry_run, stop_boxes=not a.no_stop)
    print(r.summary())
    return 0            # warn-never-stop: the chain never fails the timer


if __name__ == "__main__":
    raise SystemExit(main())
