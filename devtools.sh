#!/bin/bash
# =============================================================================
# futures_trader_v1/devtools.sh — v0.3
# v0.3 — 2026-07-25 — Phase-3: execution + strategy suite menu item.
# v0.2 — 2026-07-25 — Phase-2: analysis suite + push gate menu items.
# v0.1 — 2026-07-25 — Initial build. Only items that actually work are listed;
#         fleet sections land in Phase 4 and are deliberately ABSENT rather than
#         present-and-dead. A menu with dead options is worse than a small menu.
# =============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # self-locating
cd "$REPO" || exit 1
PY="${FT_PY:-python3}"
[ -x "$REPO/venv/bin/python" ] && PY="$REPO/venv/bin/python"

sym() { "$PY" -c "import config;print(config.SYMBOL)" 2>/dev/null || echo "?"; }
mode() { "$PY" -c "import config;print(config.MODE)" 2>/dev/null || echo "?"; }

banner() {
  echo ""
  echo "──────────────────────────────────────────────────────────────"
  echo "  futures_trader_v1 devtools v0.1"
  echo "  box: $(sym) · mode: $(mode) · $( [ "${FT_PAPER_TRADING:-True}" = "False" ] && echo LIVE || echo PAPER )"
  echo "──────────────────────────────────────────────────────────────"
}

tick_chart()   { "$PY" -m risk.capacity; }
tick_other()   { read -rp "  symbol (reference only): " s; "$PY" -m risk.capacity --symbol "${s^^}"; }
tick_equity()  { read -rp "  equity to model: " e; "$PY" -m risk.capacity --equity "$e"; }
universe()     { "$PY" -m risk.capacity --matrix; }
cfg_check()    { "$PY" -c "import config,sys;p=config.validate();print('\n'.join(p) if p else 'config OK — no problems');sys.exit(0)"; }
roll_status()  { "$PY" -m execution.roll_status 2>/dev/null || echo "  (Phase 2)"; }
run_tests()    { "$PY" tests/test_foundation.py; }
run_analysis() { "$PY" tests/test_analysis.py; }
run_exec()     { "$PY" tests/test_execution.py; }
run_gate()     { bash check_versions.sh; }

while true; do
  banner
  cat <<'MENU'
  ── capacity / sizing ─────────────────────────────────────────
    1) TICK CHART — this box's symbol, current balance
    2) tick chart — another symbol (reference)
    3) tick chart — model a different account balance
    4) universe matrix — every root x mode at this balance
  ── config / health ───────────────────────────────────────────
   10) validate config
   11) roll status (front month, window, crossover)
  ── tests ─────────────────────────────────────────────────────
   20) foundation test suite
   21) analysis test suite
   23) execution + strategy suite
   22) full push gate (check_versions.sh)
    0) quit
MENU
  read -rp "  > " c
  case "$c" in
    1) tick_chart ;;  2) tick_other ;; 3) tick_equity ;; 4) universe ;;
    10) cfg_check ;; 11) roll_status ;;
    20) run_tests ;; 21) run_analysis ;; 23) run_exec ;; 22) run_gate ;;
    0) exit 0 ;;
    *) echo "  ?" ;;
  esac
  read -rp "  [enter] " _
done
