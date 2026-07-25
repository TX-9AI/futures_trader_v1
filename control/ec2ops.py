"""
futures_trader_v1/control/ec2ops.py — v0.1
v0.1 — 2026-07-25 — Initial build. Every EC2 call, and a mock that needs no AWS.

STATE-DRIVEN, NOT MEMORY-DRIVEN — ported from the options control plane, where
it was the design rule that made the EOD sweep reliable. Actions operate on
what is RUNNING UNDER THE TAG RIGHT NOW, never on a stored manifest. A box
hand-started mid-day is still swept; a box that died is still reported. No
ownership tracking, nothing to go stale.

boto3 is imported lazily so the whole control plane is testable, and so a
missing SDK produces a clear message instead of an import error at load.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from control import fleet_config as FC

logger = logging.getLogger(__name__)

RUNNING, STOPPED, PENDING, STOPPING = "running", "stopped", "pending", "stopping"


@dataclass
class Instance:
    instance_id: str
    symbol: str = ""
    mode: str = ""
    state: str = STOPPED
    private_ip: str = ""

    @property
    def box(self) -> str:
        return FC.box_name(self.symbol, self.mode)


class MockBackend:
    """A fleet in memory. Used by the tests and by --mock, so the whole
    orchestration flow can be exercised with no AWS and no money."""

    def __init__(self, fleet=None):
        self.instances: Dict[str, Instance] = {}
        for i, (sym, mode) in enumerate(fleet or FC.FLEET):
            iid = f"i-mock{i:04d}"
            self.instances[iid] = Instance(iid, sym, mode, STOPPED,
                                           f"10.0.0.{10 + i}")

    def describe(self) -> List[Instance]:
        return list(self.instances.values())

    def start(self, ids: List[str]) -> List[str]:
        for i in ids:
            if i in self.instances:
                self.instances[i].state = RUNNING
        return ids

    def stop(self, ids: List[str]) -> List[str]:
        for i in ids:
            if i in self.instances:
                self.instances[i].state = STOPPED
        return ids


class AwsBackend:
    def __init__(self, region: str = ""):
        try:
            import boto3
        except ImportError as e:                             # pragma: no cover
            raise RuntimeError(
                "boto3 is not installed on this control server — "
                "pip install boto3, or run with FTC_MOCK=1") from e
        self.ec2 = boto3.client("ec2", region_name=region or FC.REGION)

    def describe(self) -> List[Instance]:
        out: List[Instance] = []
        pages = self.ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "tag:Project", "Values": [FC.PROJECT_TAG]}])
        for page in pages:
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    out.append(Instance(
                        inst["InstanceId"], tags.get("Symbol", ""),
                        tags.get("Mode", "DAY"),
                        inst.get("State", {}).get("Name", STOPPED),
                        inst.get("PrivateIpAddress", "")))
        return out

    def start(self, ids: List[str]) -> List[str]:
        if not ids:
            return []
        self.ec2.start_instances(InstanceIds=ids)
        return ids

    def stop(self, ids: List[str]) -> List[str]:
        if not ids:
            return []
        # STOPPED, NEVER TERMINATED. Config, EBS and paper/live settings must
        # survive to the next wake.
        self.ec2.stop_instances(InstanceIds=ids)
        return ids


def backend(mock: Optional[bool] = None):
    use_mock = FC.MOCK if mock is None else mock
    return MockBackend() if use_mock else AwsBackend()
