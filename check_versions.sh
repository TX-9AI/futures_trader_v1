#!/bin/bash
# =============================================================================
# futures_trader_v1/check_versions.sh — v1.0
# v1.0 — 2026-07-25 — the bump check now COMPARES THE HEADER VERSIONS directly
#         (working tree vs HEAD:file) instead of grepping the diff text for a
#         version line. The grep approach red-flagged data/futures_feed.py even
#         though HEAD was v0.1 and the tree was v0.2 — the bump was genuinely
#         there and the pattern missed it. Comparing the two headers answers the
#         real question, cannot false-positive on diff formatting, and also
#         catches a changed file that has no header at all.
# v0.9 — 2026-07-25 — a RED now says WHICH failure it is: header never bumped,
#         versus header bumped in an earlier commit so the bump is absent from
#         this diff. Those need opposite responses and the old message could not
#         tell them apart.
# v0.8 — 2026-07-25 — Phase-6 canaries: roll volume, replay harness, timers.
# v0.7 — 2026-07-25 — buying-power gate canaries.
# v0.6 — 2026-07-25 — Phase-5 canaries (11 new) + the control suite in the gate.
# v0.5 — 2026-07-25 — Phase-4 canaries (13 new) + the runtime suite in the gate.
# v0.4 — 2026-07-25 — GIT-AWARE BUMP CHECK. Header/changelog PARITY cannot catch
#         a version bump that silently no-opped: if an edit misses the title
#         line, title and changelog stay equally stale and parity still reads
#         green. Happened twice (config.py/capacity.py, then exit_engine.py) —
#         both times an edit assumed a "#" prefix that Python docstring headers
#         do not have. This compares working-tree changes against HEAD and reds
#         any changed .py/.sh whose diff contains no version line.
# v0.3 — 2026-07-25 — Phase-3 canaries (22 new) + the execution suite in the gate.
# v0.2 — 2026-07-25 — Phase-2 canaries (17 new) + both test suites in the gate.
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
echo "── version bumped where content changed (git-aware) ───────────"
if git rev-parse --git-dir >/dev/null 2>&1; then
  changed=$(git diff --name-only HEAD 2>/dev/null | grep -E '\.(py|sh)$' || true)
  if [ -z "$changed" ]; then
    echo "  (no tracked .py/.sh changes against HEAD)"
  fi
  for f in $changed; do
    [ -f "$f" ] || continue
    cur=$(grep -m1 -oE 'v[0-9]+\.[0-9]+' "$f" 2>/dev/null | head -1)
    old=$(git show "HEAD:$f" 2>/dev/null | grep -m1 -oE 'v[0-9]+\.[0-9]+' | head -1)
    if [ -z "$cur" ]; then
      echo "  ✗ $f  CHANGED and has NO version header at all"; RED=1
    elif [ -z "$old" ]; then
      echo "  ✓ $f  new in this change ($cur)"
    elif [ "$cur" = "$old" ]; then
      echo "  ✗ $f  CHANGED but header still $cur — BUMP IT"; RED=1
    else
      echo "  ✓ $f  $old -> $cur"
    fi
  done
else
  echo "  (not a git checkout — skipped)"
fi

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
canary "BB width is a PERCENTILE not a ratio" analysis/volatility.py "def width_percentile"
canary "VWAP guards zero volume"          analysis/volatility.py "if cum_v <= 0:"
canary "trend weights RENORMALIZE"        analysis/trend.py       "def _renormalize"
canary "missing frames are reported"      analysis/trend.py       "missing_frames"
canary "L1 de-saturated RANGE_ROOM"       analysis/regime_confluence.py "RANGE_ROOM_LO\", 0.17"
canary "L1 de-saturated OSC_CROSS"        analysis/regime_confluence.py "OSC_CROSS_HI\", 10.0"
canary "futures corroborators at weight 0" analysis/regime_confluence.py "W_TREND_CVD\", 0.0"
canary "L1 veto/necessary/corroborator grammar" analysis/regime_confluence.py "def _combine"
canary "L2 has no UNKNOWN label"          analysis/conviction_integrator.py "always argmax"
canary "L2 decay resists with conviction" analysis/conviction_integrator.py "exp(prm.lam \* c)"
canary "L2 stale reason survives emission" analysis/conviction_integrator.py "stale_prefix"
canary "BALANCED commits slowly (780s)"   analysis/conviction_integrator.py "tau_up=780.0"
canary "ON high/low exists"               analysis/liquidity.py   "def overnight_extremes"
canary "level tier is a VALUE not a flag" analysis/liquidity.py   "strongest_within"
canary "orderflow declares approximation" analysis/orderflow.py   "approximated"
canary "journal never raises"             analysis/signal_journal.py "return False        # deliberate"
canary "Signal demands entry+stop+target"  strategy/base.py         "def validate"
canary "R anchors to the INITIAL stop"     execution/exit_engine.py "initial_stop"
canary "exits outrank adjustments"         execution/exit_engine.py "_exhaustion_exit"
canary "scale-out at +1R exists"           execution/exit_engine.py "CLOSE_PARTIAL"
canary "trail only ever tightens"          execution/exit_engine.py "def _tightens"
canary "forced flatten crosses the spread" execution/exit_engine.py "session flatten"
canary "hedge is never session-flattened"  execution/exit_engine.py "pos.profile != HEDGE"
canary "anti-orphan: row stays OPEN"       execution/position_manager.py "ANTI-ORPHAN"
canary "manager kwargs are optional"       execution/position_manager.py "structure=None, vol=None"
canary "no booking on submission"          execution/order_confirm.py "def confirm_fill"
canary "partial books only what filled"    execution/entry_engine.py "fill.filled_qty"
canary "paper pays slippage"               execution/order_confirm.py "def paper_fill"
canary "ORB opens-inside is definitional"  analysis/opening_range.py "inside_open"
canary "ORB counts real bars"              analysis/opening_range.py "bars_since_break"
canary "ORB timeout re-arms"               analysis/opening_range.py "TIMEOUT"
canary "geometry gate bypasses the scorer" risk/setup_scorer.py     "_grade_geometry"
canary "liquidity downgrades not vetoes"   risk/setup_scorer.py     "downgrade, not veto"
canary "D1 refuses opposing flow"          strategy/day_mode.py     "a break on opposing flow"
canary "D2 will not fade a trend"          strategy/day_mode.py     "do not fade a committed trend"
canary "S1 refuses approximated flow"      strategy/scalp_mode.py   "approximated"
canary "W2 abort condition exists"         strategy/swing_mode.py   "def value_fade_aborted"
canary "hedge scored on variance"          strategy/hedge_mode.py   "def effectiveness"

echo ""
echo "── test suite ─────────────────────────────────────────────────"
for suite in tests/test_foundation.py tests/test_analysis.py tests/test_execution.py tests/test_runtime.py tests/test_control.py; do
  "$PY" "$suite" >/tmp/ft_tests.txt 2>&1
  printf "  %-28s %s\n" "$(basename "$suite")" "$(tail -2 /tmp/ft_tests.txt | head -1 | xargs)"
  grep -q "0 failed" /tmp/ft_tests.txt || { echo "  ✗ $suite FAILING"; RED=1; }
done

echo ""
if [ "$RED" -eq 0 ]; then echo "  ALL GREEN — safe to push"; else echo "  ✗ REDS PRESENT — do not push"; fi
exit $RED
