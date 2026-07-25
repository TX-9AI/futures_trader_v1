"""
futures_trader_v1/notifications/alerts.py — v0.1
v0.1 — 2026-07-25 — Initial build. Telegram transport + the small, fixed set of
        events worth interrupting a human for.

DELIBERATELY FEW EVENTS. A channel that pages on everything is a channel nobody
reads, and the one message that mattered arrives in a stream of noise. Startup,
shutdown, entry, exit, roll, halt, and the things that mean the machine cannot
help itself: an unconfirmed close, a half-complete roll, a margin refusal.

Never fatal. Every send is wrapped; a dead network degrades to a missing message
and never interrupts the trading loop.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from typing import Optional

import config as C

logger = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str, token: Optional[str] = None,
         chat_id: Optional[str] = None, timeout: float = 8.0) -> bool:
    tok = token or C.TELEGRAM_TOKEN
    cid = chat_id or C.TELEGRAM_CHAT_ID
    if not tok or not cid:
        logger.debug("telegram not configured; message dropped: %s", text[:80])
        return False
    try:
        data = urllib.parse.urlencode(
            {"chat_id": cid, "text": text, "parse_mode": "HTML"}).encode()
        with urllib.request.urlopen(API.format(token=tok), data=data,
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception as e:                                   # noqa: BLE001
        logger.warning("telegram send failed: %s", e)
        return False


class Alerts:
    def __init__(self, symbol: str, mode: str, paper: bool = True, sender=send):
        self.symbol, self.mode, self.paper = symbol, mode, paper
        self._send = sender

    def _tag(self) -> str:
        return f"[{self.symbol}·{self.mode}·{'PAPER' if self.paper else 'LIVE'}]"

    def raw(self, text: str) -> bool:
        return self._send(f"{self._tag()} {text}")

    def startup(self, detail: str = "") -> bool:
        return self.raw(f"🟢 started {detail}")

    def shutdown(self, detail: str = "") -> bool:
        return self.raw(f"⚪ stopped {detail}")

    def entry(self, sig, contracts: int, grade: str, price: float) -> bool:
        return self.raw(
            f"📥 {sig.strategy} {sig.direction} {contracts} @ {price:.4f}\n"
            f"stop {sig.stop:.4f} · target {sig.target:.4f} · "
            f"R:R {sig.rr:.2f} · grade {grade}\n{sig.reason}")

    def exit(self, reason: str, contracts: int, price: float,
             r: float, pnl: float) -> bool:
        icon = "✅" if pnl >= 0 else "🔻"
        return self.raw(f"{icon} exit {contracts} @ {price:.4f} · "
                        f"{r:+.2f}R · ${pnl:,.2f}\n{reason}")

    def roll(self, text: str) -> bool:
        return self.raw(f"🔄 {text}")

    def halt(self, why: str) -> bool:
        return self.raw(f"🛑 HALTED — {why}")

    def attention(self, why: str) -> bool:
        """The machine cannot resolve this itself."""
        return self.raw(f"🚨 {why}")
