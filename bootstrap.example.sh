#!/bin/bash
# =============================================================================
# futures_trader_v1/bootstrap.example.sh — v0.1
# v0.1 — 2026-07-25 — Template for an UNATTENDED install.
#
# HOW THE UNATTENDED PATH WORKS (unchanged from the options schema):
#   1. copy this to bootstrap.sh  (gitignored — secrets live ONLY there)
#   2. fill in the values
#   3. source it, then run install.sh:
#        source bootstrap.sh && curl -fsSL <raw install.sh url> -o install.sh && bash install.sh
#   4. setup_ec2.sh sees the credentials already exported, skips EVERY prompt,
#      and SHREDS bootstrap.sh during cleanup.
#
# Never commit bootstrap.sh. Never paste secrets into a prompt that a shell
# history will keep.
# =============================================================================

# ── TastyTrade (the LLC futures account) ─────────────────────────────────────
export FT_TT_CLIENT_SECRET="..."
export FT_TT_REFRESH_TOKEN="..."
export FT_TT_ACCOUNT="5WT00000"

# ── Telegram ─────────────────────────────────────────────────────────────────
export FT_TELEGRAM_TOKEN="..."
export FT_TELEGRAM_CHAT_ID="..."

# ── GitHub (for the box's own pulls) ─────────────────────────────────────────
export GITHUB_REPO="TX-9AI/futures_trader_v1"
export GITHUB_TOKEN="..."

# ── Box identity. One box = one symbol = one mode. ───────────────────────────
export FT_SYMBOL="MNQ"
export FT_MODE="DAY"              # DAY | SCALP | SWING | HEDGE

# ── Everything below is optional; configure.sh can set it later. ─────────────
# export FT_SESSIONS="NY_RTH"
# export FT_MAX_CONTRACTS="3"
# export FT_RISK_PCT="0.01"
# export FT_HEDGE_PORTFOLIO_USD="250000"

# INSTALLS ARE ALWAYS PAPER. Going live is a deliberate, separate act through
# configure.sh — never something an install script can do by accident.
