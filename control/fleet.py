"""
futures_trader_v1/control/fleet.py — v0.2
v0.2 — 2026-07-25 — margin_usage() calls control.margin_report as a module
        instead of an inline python -c, which kept the remote command to ONE
        quoting level (the fan-out adds its own layer over SSH and nested
        quotes collide with it).
v0.1 — 2026-07-25 — Initial build. SSH fan-out: list, ping, run, collect.

Control only ever does three things to a box: START it, READ from it, and PUSH
a small constraint file. It never injects what to trade. Every box runs
standalone and can be operated by hand at any time — the fleet is a
convenience, not a dependency. That property is what let the options fleet
survive control-plane outages without losing a session.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from control import ec2ops
from control import fleet_config as FC

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    box: str
    ok: bool
    out: str = ""
    err: str = ""
    rc: int = 0


def ssh_command(ip: str, remote: str, timeout: int = 0) -> List[str]:
    return ["ssh", "-i", FC.SSH_KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
            "-o", f"ConnectTimeout={timeout or FC.SSH_TIMEOUT}",
            f"{FC.SSH_USER}@{ip}", remote]


class Fleet:
    def __init__(self, backend=None, runner: Optional[Callable] = None):
        self.backend = backend or ec2ops.backend()
        self._runner = runner        # injected for tests; None = real ssh

    # ── discovery ────────────────────────────────────────────────────────────
    def instances(self) -> List[ec2ops.Instance]:
        return self.backend.describe()

    def running(self) -> List[ec2ops.Instance]:
        return [i for i in self.instances() if i.state == ec2ops.RUNNING]

    def by_mode(self, mode: str) -> List[ec2ops.Instance]:
        return [i for i in self.instances() if i.mode.upper() == mode.upper()]

    def listing(self) -> List[Tuple[str, str, str]]:
        return [(i.box, i.private_ip or "-", i.state)
                for i in sorted(self.instances(), key=lambda x: x.box)]

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self, instances: List[ec2ops.Instance]) -> List[str]:
        ids = [i.instance_id for i in instances if i.state != ec2ops.RUNNING]
        return self.backend.start(ids)

    def stop(self, instances: List[ec2ops.Instance]) -> List[str]:
        ids = [i.instance_id for i in instances if i.state == ec2ops.RUNNING]
        return self.backend.stop(ids)

    # ── fan-out ──────────────────────────────────────────────────────────────
    def run(self, command: str, instances: Optional[List] = None,
            workers: int = 8, timeout: int = 60) -> List[RunResult]:
        targets = instances if instances is not None else self.running()
        if not targets:
            return []

        def one(inst) -> RunResult:
            remote = f"cd {FC.BOX_DIR} && {command}"
            if self._runner is not None:
                return self._runner(inst, remote)
            try:
                p = subprocess.run(ssh_command(inst.private_ip, remote),
                                   capture_output=True, text=True, timeout=timeout)
                return RunResult(inst.box, p.returncode == 0,
                                 p.stdout.strip(), p.stderr.strip(), p.returncode)
            except subprocess.TimeoutExpired:
                return RunResult(inst.box, False, "", "ssh timeout", 124)
            except Exception as e:                           # noqa: BLE001
                return RunResult(inst.box, False, "", str(e), 1)

        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            return sorted(ex.map(one, targets), key=lambda r: r.box)

    def ping(self) -> List[RunResult]:
        return self.run("echo alive", timeout=20)

    # ── the reads the control plane actually needs ───────────────────────────
    def collect_json(self, command: str,
                     instances: Optional[List] = None) -> List[dict]:
        """Run something that prints JSON on each box and parse it. A box that
        prints garbage is SKIPPED WITH A WARNING rather than failing the sweep —
        one sick box must never cost the whole fleet's reading."""
        rows = []
        for r in self.run(command, instances):
            if not r.ok or not r.out:
                logger.warning("%s: no usable output (%s)", r.box, r.err[:120])
                continue
            try:
                payload = json.loads(r.out.strip().splitlines()[-1])
            except (ValueError, IndexError):
                logger.warning("%s: unparseable output", r.box)
                continue
            payload.setdefault("box", r.box)
            rows.append(payload)
        return rows

    def margin_usage(self) -> List[dict]:
        """What every running box currently ties up. Feeds the governor."""
        return self.collect_json("venv/bin/python -m control.margin_report")

    def push_file(self, inst, local: str, remote_name: str) -> bool:
        if self._runner is not None:
            return True
        try:
            subprocess.run(
                ["scp", "-i", FC.SSH_KEY, "-o", "StrictHostKeyChecking=no",
                 "-o", "LogLevel=ERROR", local,
                 f"{FC.SSH_USER}@{inst.private_ip}:{FC.BOX_DIR}/{remote_name}"],
                capture_output=True, timeout=30, check=True)
            return True
        except Exception as e:                               # noqa: BLE001
            logger.warning("push to %s failed: %s", inst.box, e)
            return False
