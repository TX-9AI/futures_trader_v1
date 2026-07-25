#!/bin/bash
# =============================================================================
# futures_trader_v1/install_control_timers.sh — v0.1
# v0.1 — 2026-07-25 — systemd timers for the control plane.
#
# TWO TIMERS, NOT TWELVE. The wake is per-box and session-aware, so instead of
# one timer per wave the orchestrator runs every 15 minutes and decides for
# itself who is due. Adding a box, or changing which sessions it trades, needs
# no timer change — which is the difference between a schedule you maintain and
# one you fight.
#
#   ft-wake.timer   every 15 min      -> orchestrator wakes whatever is due
#   ft-eod.timer    16:10 ET weekdays -> the EOD chain
#
# The EOD chain is warn-never-stop and always exits 0, so a bad session can
# never leave the timer in a failed state and silently stop running.
#
# NOTE ON TIME: OnCalendar uses the box's local time. Set the control server to
# America/New_York (timedatectl set-timezone America/New_York) or the EOD timer
# will drift against the session by whatever the offset is.
# =============================================================================
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
ENVF="$REPO/.env"; touch "$ENVF"

echo "  installing control timers from $REPO"
echo "  timezone is currently: $(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"

sudo tee /etc/systemd/system/ft-wake.service >/dev/null <<EOF
[Unit]
Description=futures control - session-aware fleet wake
[Service]
Type=oneshot
User=$USER
WorkingDirectory=$REPO
EnvironmentFile=$ENVF
ExecStart=$PY -c "from control.orchestrator import Orchestrator; p=Orchestrator().wake(); print(p.reason)"
EOF

sudo tee /etc/systemd/system/ft-wake.timer >/dev/null <<'EOF'
[Unit]
Description=run the fleet wake check every 15 minutes
[Timer]
OnCalendar=*:0/15
Persistent=false
[Install]
WantedBy=timers.target
EOF

sudo tee /etc/systemd/system/ft-eod.service >/dev/null <<EOF
[Unit]
Description=futures control - EOD chain
[Service]
Type=oneshot
User=$USER
WorkingDirectory=$REPO
EnvironmentFile=$ENVF
ExecStart=$PY -m control.eod_conductor
EOF

sudo tee /etc/systemd/system/ft-eod.timer >/dev/null <<'EOF'
[Unit]
Description=run the EOD chain after the cash close
[Timer]
OnCalendar=Mon-Fri 16:10
Persistent=false
[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ft-wake.timer ft-eod.timer
echo ""
systemctl list-timers 'ft-*' --no-pager || true
echo ""
echo "  Persistent=false on BOTH by design: a missed overnight run must never"
echo "  fire on boot into a live session."
