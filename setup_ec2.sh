#!/bin/bash
# =============================================================================
# futures_trader_v1/setup_ec2.sh — v0.1
# v0.1 — 2026-07-25 — Full box build. Unattended when credentials are already
#         in the environment; interactive otherwise.
#
# SCHEMA CARRIED OVER FROM options_trader_v3, INCLUDING ITS SCARS:
#   * UNATTENDED DETECTION — if bootstrap.sh already exported the credentials,
#     every prompt is skipped and the install is hands-free.
#   * ALWAYS PAPER. No trading-mode prompt exists. Risk, mode and instrument are
#     set afterward via configure.sh, so an install can never go live by itself.
#   * chmod +x AFTER git reset --hard, recursively. Git strips execute bits on a
#     reset; doing this before the reset silently produces a box whose menu will
#     not run.
#   * CLEANUP: the deploy checkout and install.sh are removed and bootstrap.sh
#     is SHREDDED, so no box keeps a copy of the secrets on disk.
#   * needrestart is blocked and the apt timers are moved out of the session, so
#     an unattended upgrade can never restart the bot mid-trade.
# =============================================================================
set -e

INSTALL_DIR="$HOME/futures-trader"
DEPLOY_DIR="$HOME/futures-trader-deploy"
BOT_SERVICE="futuresbot"
FEED_SERVICE="futures-feed"
VENV="$INSTALL_DIR/venv"
G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; R='\033[0;31m'; B='\033[1m'; N='\033[0m'

echo ""
echo -e "${B}${C}╔══════════════════════════════════════════════════════╗${N}"
echo -e "${B}${C}║   futures_trader_v1  |  Vertigo Capital              ║${N}"
echo -e "${B}${C}║   one box = one symbol = one mode  |  PAPER          ║${N}"
echo -e "${B}${C}╚══════════════════════════════════════════════════════╝${N}"
echo ""

# ── unattended detection ─────────────────────────────────────────────────────
UNATTENDED=false
if [ -n "${FT_TT_CLIENT_SECRET:-}" ] && [ -n "${FT_TT_REFRESH_TOKEN:-}" ] \
   && [ -n "${FT_TT_ACCOUNT:-}" ]; then
  UNATTENDED=true
  echo -e "  ${G}credentials found in environment — unattended install${N}"
else
  echo -e "  ${Y}no credentials in environment — interactive install${N}"
  echo "  have ready: TT client secret, refresh token, account number,"
  echo "              Telegram bot token + chat id, GitHub PAT"
fi
echo ""

ask() { # ask VAR "prompt" [silent]
  local __v="$1" __p="$2" __s="${3:-}" __x=""
  if [ -n "${!__v:-}" ]; then return 0; fi
  if [ "$UNATTENDED" = true ]; then return 0; fi
  if [ -n "$__s" ]; then read -rsp "  $__p: " __x; echo ""; else read -rp "  $__p: " __x; fi
  export "$__v"="$__x"
}
ask FT_TT_CLIENT_SECRET "TastyTrade client secret" silent
ask FT_TT_REFRESH_TOKEN "TastyTrade refresh token" silent
ask FT_TT_ACCOUNT       "TastyTrade account number"
ask FT_TELEGRAM_TOKEN   "Telegram bot token" silent
ask FT_TELEGRAM_CHAT_ID "Telegram chat id"
ask FT_SYMBOL           "symbol root (e.g. MNQ)"
ask FT_MODE             "mode (DAY|SCALP|SWING|HEDGE)"

FT_SYMBOL="${FT_SYMBOL:-MNQ}"
FT_MODE="${FT_MODE:-DAY}"

# ── code ─────────────────────────────────────────────────────────────────────
echo -e "  ${C}installing to $INSTALL_DIR${N}"
if [ -d "$INSTALL_DIR/.git" ]; then
  cd "$INSTALL_DIR" && git fetch -q origin && git reset -q --hard origin/main
else
  rm -rf "$INSTALL_DIR"
  cp -r "$DEPLOY_DIR" "$INSTALL_DIR"
  cd "$INSTALL_DIR" && git fetch -q origin && git reset -q --hard origin/main
fi
# AFTER the reset, never before — git strips the execute bit on reset.
find "$INSTALL_DIR" -name "*.sh" -exec chmod +x {} \;
echo "  code at $(git rev-parse --short HEAD)"

python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
echo "  venv ready"

# ── host hardening: an unattended upgrade must never restart us mid-session ──
sudo mkdir -p /etc/needrestart/conf.d
echo '$nrconf{restart} = "l";' | sudo tee /etc/needrestart/conf.d/90-futures.conf >/dev/null
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
echo "  host hardened (no auto-restart during a session)"

# ── systemd ──────────────────────────────────────────────────────────────────
ENVF="$INSTALL_DIR/.env"
cat > "$ENVF" <<EOF
FT_SYMBOL=$FT_SYMBOL
FT_MODE=$FT_MODE
FT_PAPER_TRADING=True
FT_TT_CLIENT_SECRET=${FT_TT_CLIENT_SECRET:-}
FT_TT_REFRESH_TOKEN=${FT_TT_REFRESH_TOKEN:-}
FT_TT_ACCOUNT=${FT_TT_ACCOUNT:-}
FT_TELEGRAM_TOKEN=${FT_TELEGRAM_TOKEN:-}
FT_TELEGRAM_CHAT_ID=${FT_TELEGRAM_CHAT_ID:-}
EOF
chmod 600 "$ENVF"

sudo tee /etc/systemd/system/$FEED_SERVICE.service >/dev/null <<EOF
[Unit]
Description=futures_trader feed producer ($FT_SYMBOL)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENVF
ExecStart=$VENV/bin/python -m data.futures_feed
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/$BOT_SERVICE.service >/dev/null <<EOF
[Unit]
Description=futures_trader bot ($FT_SYMBOL $FT_MODE)
After=network-online.target $FEED_SERVICE.service
Wants=network-online.target $FEED_SERVICE.service
[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENVF
ExecStart=$VENV/bin/python main.py
Restart=always
RestartSec=15
[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable $FEED_SERVICE $BOT_SERVICE >/dev/null 2>&1
# Feed FIRST so the store is warm before the bot reads it.
sudo systemctl start $FEED_SERVICE
sleep 3
sudo systemctl start $BOT_SERVICE
echo "  services installed and started (feed first)"

# ── cleanup: leave no secrets and no stale copy of the code ─────────────────
cd "$HOME"
rm -rf "$DEPLOY_DIR"
rm -f "$HOME/install.sh"
if [ -f "$HOME/bootstrap.sh" ]; then
  shred -u "$HOME/bootstrap.sh" 2>/dev/null || rm -f "$HOME/bootstrap.sh"
  echo "  bootstrap.sh shredded"
fi

echo ""
echo -e "  ${G}${B}done — $FT_SYMBOL / $FT_MODE / PAPER${N}"
echo "  next:  cd $INSTALL_DIR && ./configure.sh     (mode, risk, sessions, go-live)"
echo "         ./devtools.sh                          (1 = tick chart)"
echo "         bash check_versions.sh                 (the push gate)"
echo ""
cd "$INSTALL_DIR" && exec bash --rcfile <(echo "source ~/.bashrc; source $VENV/bin/activate")
