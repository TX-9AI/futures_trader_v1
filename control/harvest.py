"""
futures_trader_v1/control/harvest.py — v0.1
v0.1 — 2026-07-25 — Initial build. Pull the session's data back to control.

THE ORDER-FLOW ARCHIVE IS THE POINT OF THIS FILE.

Three datasets come back each night, and they are not equally replaceable:

  OHLC        reconstructible from the broker later if lost. Nice to have.
  trades.db   reconstructible from broker history. Nice to have.
  ORDERFLOW   tick prints with the aggressor side. NOT RECONSTRUCTIBLE. Once
              the session is gone it is gone, exactly as option chains were.

The options project discovered that exposure late: 29 boxes were accumulating
an irreplaceable archive with no copy on control, so any box rebuilt from
scratch lost that symbol's history permanently. Order flow is the futures
equivalent and it is harvested FIRST, before anything else can fail the run.

Everything is warn-never-stop. A harvest that aborts on one unreachable box is
a harvest that loses the other eleven boxes' data too.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from control import fleet_config as FC
from control.fleet import Fleet

logger = logging.getLogger(__name__)


@dataclass
class HarvestResult:
    pulled: Dict[str, List[str]] = field(default_factory=dict)
    failed: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def headline(self) -> str:
        n = sum(len(v) for v in self.pulled.values())
        return (f"harvest: {n} file(s) from {len(self.pulled)} box(es)"
                + (f", {len(self.failed)} failed" if self.failed else ""))


class Harvester:
    def __init__(self, fleet: Optional[Fleet] = None,
                 base_dir: Optional[str] = None,
                 copier=None):
        self.fleet = fleet or Fleet()
        self.base = base_dir or FC.BASE_DIR
        self._copier = copier          # injected for tests

    def _dest(self, kind: str, day: date) -> str:
        d = {"orderflow": FC.FLOW_DIR, "ohlc": FC.OHLC_DIR,
             "trades": FC.TRADES_DIR}.get(kind, FC.REPORTS_DIR)
        if self.base != FC.BASE_DIR:
            d = os.path.join(self.base, kind)
        p = os.path.join(d, day.isoformat())
        os.makedirs(p, exist_ok=True)
        return p

    def _scp(self, inst, remote: str, local: str) -> bool:
        if self._copier is not None:
            return self._copier(inst, remote, local)
        try:
            subprocess.run(
                ["scp", "-i", FC.SSH_KEY, "-o", "StrictHostKeyChecking=no",
                 "-o", "LogLevel=ERROR",
                 f"{FC.SSH_USER}@{inst.private_ip}:{FC.BOX_DIR}/{remote}", local],
                capture_output=True, timeout=180, check=True)
            return True
        except Exception as e:                               # noqa: BLE001
            logger.warning("scp %s from %s failed: %s", remote, inst.box, e)
            return False

    def run(self, day: Optional[date] = None,
            instances: Optional[List] = None) -> HarvestResult:
        day = day or date.today()
        res = HarvestResult()
        targets = instances if instances is not None else self.fleet.running()
        if not targets:
            res.warnings.append("no running boxes to harvest")
            return res

        # ORDER FLOW FIRST — the only dataset that cannot be recreated.
        for inst in targets:
            got = []
            flow_local = os.path.join(self._dest("orderflow", day),
                                      f"{inst.box}_{day}_flow.db")
            if self._scp(inst, "data/feed_store.db", flow_local):
                got.append(flow_local)
            else:
                res.failed.append(f"{inst.box}:orderflow")
                res.warnings.append(
                    f"{inst.box}: ORDER FLOW NOT PULLED — this session's tick "
                    f"tape is unrecoverable if the box is rebuilt")

            db_local = os.path.join(self._dest("trades", day),
                                    f"{inst.box}_{day}_trades.db")
            if self._scp(inst, "trades.db", db_local):
                got.append(db_local)
            else:
                res.failed.append(f"{inst.box}:trades")

            j_local = os.path.join(self._dest("reports", day),
                                   f"{inst.box}_{day}_journal.jsonl")
            if self._scp(inst, f"data/signal_journal/{day}/{inst.symbol}.jsonl",
                         j_local):
                got.append(j_local)

            if got:
                res.pulled[inst.box] = got
        return res
