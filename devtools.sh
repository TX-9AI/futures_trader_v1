#!/bin/bash
# =============================================================================
# futures_trader_v1/devtools.sh — v0.4
# v0.4 — 2026-07-25 — Phase-4: box/service items, feed health, simulation mode,
#         runtime suite.
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
run_runtime()  { "$PY" tests/test_runtime.py; }
bot_status()   { "$PY" status.py; }
svc()          { systemctl is-active "$1" 2>/dev/null || echo "-"; }
svc_status()   { echo "  futuresbot   : $(svc futuresbot)"; echo "  futures-feed : $(svc futures-feed)"; }
log_tail()     { sudo journalctl -u futuresbot -n 60 --no-pager; }
feed_health()  { "$PY" -c "from data.market_data import MarketData;ok,why=MarketData().healthy();print(('OK  ' if ok else 'DOWN')+' '+why)"; }
feed_sim()     { echo "  ctrl-C to stop"; "$PY" -m data.futures_feed --sim; }
restart_bot()  { sudo systemctl restart futuresbot && svc_status; }
run_gate()     { bash check_versions.sh; }

while true; do
  banner
  cat <<'MENU'
  ── capacity / sizing ─────────────────────────────────────────
    1) TICK CHART — this box's symbol, current balance
    2) tick chart — another symbol (reference)
    3) tick chart — model a different account balance
    4) universe matrix — every root x mode at this balance
  ── box / services ────────────────────────────────────────────
    5) status.py — live snapshot
    6) service status (bot + feed)
    7) feed health
    8) journal tail (bot)
    9) restart bot
  ── config / health ───────────────────────────────────────────
   10) validate config
   11) roll status (front month, window, crossover)
  ── tests ─────────────────────────────────────────────────────
   20) foundation test suite
   21) analysis test suite
   23) execution + strategy suite
   24) runtime suite (store, broker, loop end-to-end)
   25) run the feed in SIMULATION (no market needed)
   22) full push gate (check_versions.sh)
    0) quit
MENU
  read -rp "  > " c
  case "$c" in
    1) tick_chart ;;  2) tick_other ;; 3) tick_equity ;; 4) universe ;;
    5) bot_status ;; 6) svc_status ;; 7) feed_health ;; 8) log_tail ;; 9) restart_bot ;;
    10) cfg_check ;; 11) roll_status ;;
    20) run_tests ;; 21) run_analysis ;; 23) run_exec ;; 24) run_runtime ;; 25) feed_sim ;; 22) run_gate ;;
    0) exit 0 ;;
    *) echo "  ?" ;;
  esac
  read -rp "  [enter] " _
done
