#!/bin/bash
# =============================================================================
# futures_trader_v1/install.sh — v0.1
# v0.1 — 2026-07-25 — Web installer. The one-liner entry point.
#
#   curl -fsSL https://raw.githubusercontent.com/TX-9AI/futures_trader_v1/main/install.sh -o install.sh && bash install.sh
#
# REPO POINTER LIVES HERE AND NOWHERE ELSE. The options project shipped an
# installer that still cloned the PREVIOUS repo, so every "fresh v3 install"
# silently deployed v2 for weeks — caught only because a banner printed the old
# version number. One constant, one place, and setup_ec2.sh echoes what it
# actually cloned.
# =============================================================================
set -e
REPO="https://github.com/TX-9AI/futures_trader_v1.git"
DEPLOY_DIR="$HOME/futures-trader-deploy"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   futures_trader_v1  |  Vertigo Capital              ║"
echo "║   installer v0.1                                     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

sudo apt-get update -qq
sudo apt-get install -y -qq git python3-venv python3-pip

if [ -d "$DEPLOY_DIR/.git" ]; then
  echo "  updating existing deploy checkout..."
  cd "$DEPLOY_DIR" && git fetch -q origin && git reset -q --hard origin/main
else
  echo "  cloning $REPO ..."
  git clone -q "$REPO" "$DEPLOY_DIR"
fi
echo "  repository ready: $(cd "$DEPLOY_DIR" && git rev-parse --short HEAD)"
echo ""
chmod +x "$DEPLOY_DIR/setup_ec2.sh"
bash "$DEPLOY_DIR/setup_ec2.sh"
