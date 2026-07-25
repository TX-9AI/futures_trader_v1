#!/bin/bash
# =============================================================================
# futures_trader_v1/configure.sh — v0.1
# v0.1 — 2026-07-25 — Runtime settings menu. Writes .env, reloads the units.
#
# GOING LIVE IS A DELIBERATE ACT AND IT IS LOUD. Option 6 requires typing the
# word LIVE, states what changes, and re-reads the tick chart at the live
# balance so the operator sees the new capacity BEFORE the switch — not after
# the first order. The options project's go-live had a list of steps to
# remember; anything you must remember is a step you will eventually forget.
# =============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$REPO" || exit 1
ENVF="$REPO/.env"; PY="${FT_PY:-python3}"; [ -x "$REPO/venv/bin/python" ] && PY="$REPO/venv/bin/python"
touch "$ENVF"; chmod 600 "$ENVF"

get() { grep -E "^$1=" "$ENVF" 2>/dev/null | tail -1 | cut -d= -f2-; }
set_() { grep -v -E "^$1=" "$ENVF" > "$ENVF.t" 2>/dev/null || true; echo "$1=$2" >> "$ENVF.t"; mv "$ENVF.t" "$ENVF"; chmod 600 "$ENVF"; }
reload() { sudo systemctl daemon-reload 2>/dev/null; echo "  (restart to apply: sudo systemctl restart futuresbot)"; }

while true; do
cat <<MENU

──────────────────────────────────────────────────────────────
  configure — $(get FT_SYMBOL) / $(get FT_MODE) / $( [ "$(get FT_PAPER_TRADING)" = "False" ] && echo LIVE || echo PAPER )
──────────────────────────────────────────────────────────────
   1) symbol            $(get FT_SYMBOL)
   2) mode              $(get FT_MODE)
   3) enabled sessions  $(get FT_SESSIONS)
   4) risk % of equity  $(get FT_RISK_PCT)
   5) max contracts     $(get FT_MAX_CONTRACTS)
   6) PAPER / LIVE      $( [ "$(get FT_PAPER_TRADING)" = "False" ] && echo LIVE || echo PAPER )
   7) daily loss limit  $(get FT_DAILY_LOSS_LIMIT)
   8) roll: auto/manual $(get FT_ROLL_AUTO)
   9) hedge inputs      portfolio=$(get FT_HEDGE_PORTFOLIO_USD) beta=$(get FT_HEDGE_BETA)
  10) Telegram creds
  11) TastyTrade creds
  12) show tick chart at current settings
   0) done
MENU
read -rp "  > " c
case "$c" in
  1) read -rp "  symbol root: " v; set_ FT_SYMBOL "${v^^}"; reload ;;
  2) read -rp "  mode (DAY|SCALP|SWING|HEDGE): " v; set_ FT_MODE "${v^^}"; reload ;;
  3) echo "  phases: ASIA LONDON NY_PRE NY_RTH NY_POST  (comma separated; add ETH to allow an intraday box outside RTH)"
     read -rp "  sessions: " v; set_ FT_SESSIONS "${v^^}"; reload ;;
  4) read -rp "  risk as fraction of equity (0.01 = 1%): " v; set_ FT_RISK_PCT "$v"; reload ;;
  5) read -rp "  max contracts: " v; set_ FT_MAX_CONTRACTS "$v"; reload ;;
  6) if [ "$(get FT_PAPER_TRADING)" = "False" ]; then
       set_ FT_PAPER_TRADING True; echo "  -> PAPER"; reload
     else
       echo ""
       echo "  GOING LIVE CHANGES:"
       echo "   · orders reach the real LLC futures account"
       echo "   · equity resolves from the BROKER, not the fixed \$25,000 paper constant"
       echo "   · sizing and the tick chart change with it — capacity at the live balance:"
       echo ""
       FT_PAPER_TRADING=False "$PY" -m risk.capacity 2>/dev/null | sed -n '1,18p'
       echo ""
       echo "   · the TastyTrade adapter must be VERIFIED first — it refuses by design"
       echo "     until each method is confirmed on the tiny account (Epoch 0)."
       read -rp "  type LIVE to confirm: " v
       if [ "$v" = "LIVE" ]; then set_ FT_PAPER_TRADING False; echo "  -> LIVE"; reload
       else echo "  cancelled — still PAPER"; fi
     fi ;;
  7) read -rp "  daily loss limit USD (blank = 2x risk): " v; set_ FT_DAILY_LOSS_LIMIT "$v"; reload ;;
  8) read -rp "  auto-roll? (true/false): " v; set_ FT_ROLL_AUTO "$v"; reload ;;
  9) read -rp "  portfolio value USD: " a; set_ FT_HEDGE_PORTFOLIO_USD "$a"
     read -rp "  portfolio beta (1.0): " b; set_ FT_HEDGE_BETA "${b:-1.0}"
     read -rp "  hedge ratio (0.5 = cover half): " r; set_ FT_HEDGE_RATIO "${r:-0.5}"
     read -rp "  conditional on regime? (true/false): " k; set_ FT_HEDGE_CONDITIONAL "${k:-false}"
     read -rp "  rebalance drift band (0.10): " d; set_ FT_HEDGE_DRIFT "${d:-0.10}"; reload ;;
 10) read -rsp "  telegram token: " t; echo ""; set_ FT_TELEGRAM_TOKEN "$t"
     read -rp "  telegram chat id: " i; set_ FT_TELEGRAM_CHAT_ID "$i"; reload ;;
 11) read -rsp "  TT client secret: " s; echo ""; set_ FT_TT_CLIENT_SECRET "$s"
     read -rsp "  TT refresh token: " r; echo ""; set_ FT_TT_REFRESH_TOKEN "$r"
     read -rp "  TT account number: " a; set_ FT_TT_ACCOUNT "$a"; reload ;;
 12) set -a; source "$ENVF"; set +a; "$PY" -m risk.capacity ;;
  0) exit 0 ;;
  *) echo "  ?" ;;
esac
done
