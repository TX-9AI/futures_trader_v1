"""
tests/test_no_undefined_names.py — v1.0 — 2026-07-31 (ported from options_trader_v3)

PORTED, NOT COPIED WHOLESALE. options_trader_v3 runs this gate in TWO places —
this suite test and `push.sh` — but futures_trader_v1 has NO push.sh at all
(checked 2026-07-31; the backlog item AE assumed it did). So the suite is the
only chokepoint here, which makes it more load-bearing than its sibling, not
less: there is no second net.

This repo was CLEAN when the gate was added — 0 undefined names across 63
tracked files. That is the point. The gate is not here to fix a defect; it is
here so the class cannot appear silently while the project is dormant and
nobody is watching.

THE DEFECT THIS EXISTS FOR — twice in two days, both caught by a live box crashing:

  2026-07-29  continuation_strategy v1.3 deleted the BB-midline block that defined
              `mid` and left four references, including the structural stop.
              NameError on EVERY tick. All 15 boxes took ZERO trades from the open
              until ~10:05 ET. The entire ORB window was missed.

  2026-07-30  butterfly_strategy reverted its proximity multiplier to a fixed
              1x EM, deleted `_mult`, and left the log line referencing it.
              NameError on every butterfly evaluation — but only a box in
              COMPRESSION reaches that gate, so IWM restarted twice while the
              other 14 boxes ran the identical code without a scratch.

Both are the same shape: a refactor removes a computation and leaves a reference
behind, on a path that only executes under conditions the test suite and most of
the fleet never hit. Python compiles it happily — an undefined name is a RUNTIME
error, so `python -c "import ast"` passes, the deploy gate passes, and the fault
waits for the one box that meets the condition.

Static analysis finds this in milliseconds. Neither incident needed to happen.

WHY THIS IS A TEST AND NOT A CANARY: check_versions greps for tokens that should
or should not be present; it cannot reason about scope. Pyflakes builds the
binding graph and answers the actual question — is this name reachable where it
is used.

ZERO TOLERANCE, deliberately. A quoted forward reference that pyflakes cannot
resolve is fixed with a TYPE_CHECKING import (see main.py) rather than
allow-listed, because the moment this gate has an exception list it starts
growing one and the third NameError ships.
"""

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tracked_py():
    try:
        out = subprocess.run(["git", "-C", REPO, "ls-files", "*.py"],
                             capture_output=True, text=True, timeout=30)
        files = [os.path.join(REPO, f) for f in out.stdout.split()
                 if f.strip()]
    except Exception:                                    # noqa: BLE001
        files = []
    if files:
        return files
    # not a git checkout (a box, a tarball): walk instead
    found = []
    for dp, dns, fns in os.walk(REPO):
        dns[:] = [d for d in dns
                  if d not in (".git", "__pycache__", "venv", ".venv")]
        found += [os.path.join(dp, f) for f in fns if f.endswith(".py")]
    return found


def test_no_undefined_names():
    """Any undefined name anywhere in the repo fails the suite.

    This is the check that would have caught BOTH the 07-29 `mid` outage and the
    07-30 `_mult` crash-loop, before either reached a box.
    """
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        pytest.fail(
            "pyflakes is not installed, so the undefined-name gate cannot run. "
            "Install it (pip install pyflakes) — this gate exists because two "
            "orphaned-variable NameErrors reached production in two days and "
            "each was found by a live box crashing. A silently skipped guard is "
            "the failure mode it was written to prevent.")

    files = _tracked_py()
    assert files, "found no python files to check — is REPO resolving correctly?"

    proc = subprocess.run([sys.executable, "-m", "pyflakes"] + files,
                          capture_output=True, text=True, timeout=180)
    undefined = [ln for ln in proc.stdout.splitlines()
                 if "undefined name" in ln.lower()]
    assert not undefined, (
        "UNDEFINED NAME(S) — this is the 07-29 `mid` / 07-30 `_mult` defect "
        "class, and it will raise at runtime on whatever path reaches it:\n  "
        + "\n  ".join(undefined))


if __name__ == "__main__":                               # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
