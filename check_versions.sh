#!/bin/bash
# =============================================================================
# futures_trader_v1/check_versions.sh — v0.1
# v0.1 — 2026-07-25 — Initial build. Header/changelog parity + canaries.
#
# WHY THIS EXISTS ON DAY ONE
# It caught two silent no-op version bumps in its first run: config.py and
# risk/capacity.py had changed content sitting under stale v0.1 titles because
# an edit's match string was wrong and the replace quietly did nothing. That is
# the same failure class that shipped a risk_manager NameError to 29 boxes in
# the options project — a canary was legitimately red and the deploy went out
# anyway. The rule that follows: RUN IT AND READ THE REDS BEFORE PUSHING.
#
# Usage:  bash check_versions.sh          (exit 1 on any red)
# =============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO" || exit 1
PY="${FT_PY:-python3}"
RED=0

echo "── header / changelog parity ──────────────────────────────────"
"$PY" - <<'PYEOF' || RED=1
import re, os, sys
bad = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'venv')]
    for f in sorted(files):
        if not f.endswith(('.py', '.sh')) or f == '__init__.py':
            continue
        p = os.path.join(root, f)
        txt = open(p).read()
        m = re.search(r'—\s*v(\d+\.\d+)', txt[:600])
        entries = re.findall(r'^\s*#?\s*v(\d+\.\d+)\s*—', txt[:3000], re.M)
        title = m.group(1) if m else None
        newest = entries[0] if entries else None
        ok = title and newest and title == newest
        print(f"  {'✓' if ok else '✗'} {p:38} v{title}")
        if not ok:
            bad.append(p)
sys.exit(1 if bad else 0)
PYEOF

echo ""
echo "── canaries: values a stale sync would silently revert ────────"
canary() {  # name  file  pattern
  if grep -q "$3" "$2" 2>/dev/null; then echo "  ✓ $1"; else echo "  ✗ $1 — MISSING in $2"; RED=1; fi
}
canary "MIN_RRR is wired and gating"      risk/risk_manager.py   "REJECT_RRR"
canary "sizing refuses to widen a stop"   risk/risk_manager.py   "cannot_afford_one_contract"
canary "paper equity does not consult broker" risk/capacity.py   "if C.PAPER_TRADING:"
canary "X vs 0 distinction present"       risk/eligibility.py    "EXCLUDED = \"excluded\""
canary "overnight modes size on INITIAL"  execution/margin_manager.py "\"SWING\": INITIAL_RATE"
canary "roll pages on half-complete"      execution/roll_manager.py   "ROLL_HALF"
canary "calendar spread is the default"   execution/roll_manager.py   "PLAN_SPREAD"
canary "entries refused without a fill"   database/trade_logger.py    "REFUSED to log"
canary "reads are mode-scoped"            database/trade_logger.py    "COALESCE(paper_trade,1)"
canary "expectancy reports avg win/loss R" database/trade_logger.py   "avg_loss_r"
canary "flatten authority is single-source" utils/sessions.py    "def must_be_flat"
canary "roll ignores out-of-window volume" data/contract_registry.py "roll_window_open"

echo ""
echo "── test suite ─────────────────────────────────────────────────"
"$PY" tests/test_foundation.py >/tmp/ft_tests.txt 2>&1
tail -2 /tmp/ft_tests.txt | head -1
grep -q "0 failed" /tmp/ft_tests.txt || { echo "  ✗ tests failing — see /tmp/ft_tests.txt"; RED=1; }

echo ""
if [ "$RED" -eq 0 ]; then echo "  ALL GREEN — safe to push"; else echo "  ✗ REDS PRESENT — do not push"; fi
exit $RED
