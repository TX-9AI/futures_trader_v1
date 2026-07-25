"""
futures_trader_v1/control/notify.py — v0.1
v0.1 — 2026-07-25 — Initial build. Control-side Telegram.

Separate from the box-side alerts: this speaks for the FLEET. Per-box entries
and exits stay on the box's own channel, so the control channel carries only
what a human must act on across the whole account.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from typing import Optional

from control import fleet_config as FC

logger = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str, token: Optional[str] = None,
         chat_id: Optional[str] = None, timeout: float = 10.0) -> bool:
    tok = token or FC.TELEGRAM_TOKEN
    cid = chat_id or FC.TELEGRAM_CHAT_ID
    if not tok or not cid:
        logger.debug("control telegram not configured; dropped: %s", text[:80])
        return False
    try:
        data = urllib.parse.urlencode(
            {"chat_id": cid, "text": f"[FLEET] {text}"}).encode()
        with urllib.request.urlopen(API.format(token=tok), data=data,
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception as e:                                   # noqa: BLE001
        logger.warning("control telegram failed: %s", e)
        return False
