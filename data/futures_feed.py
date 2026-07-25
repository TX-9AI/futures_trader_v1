"""
futures_trader_v1/data/futures_feed.py — v0.2
v0.2 — 2026-07-25 — BACK-MONTH VOLUME during the roll window. The roll trigger
        is a front-vs-back volume crossover and a one-contract subscription had
        nothing to compare, so the roll could only ever fire on its hard
        deadline. The back month is now subscribed for VOLUME ONLY, only inside
        the window, and released when it closes.
v0.1 — 2026-07-25 — Initial build. THE box's single market-data producer.

    python -m data.futures_feed          (runs as futures-feed.service)

ONE SUBSCRIPTION PER BOX. This process holds it; everything else reads the
store. That is not a performance choice — it is the only way two components can
be guaranteed to have seen the same tape, and reconciling divergent tapes after
the fact is impossible.

WHAT IT WRITES
  candles      per timeframe, upserted so a re-sent partial bar updates in place
  trades_tape  tick prints WITH THE AGGRESSOR SIDE — the irreplaceable dataset.
               Order flow cannot be reconstructed after the session. The options
               project learned this about option chains with 29 boxes
               accumulating an archive that had no copy on control. Archive from
               day one, harvest nightly.
  quotes       bid/ask/last, so every consumer marks at the same price
  heartbeat    every loop, so readers can fail loud instead of aging quietly

THE TRANSPORT IS INJECTED. `SimulatedTransport` is exercised by the test suite
and drives the whole pipeline end to end. `DXLinkTransport` is a SKELETON that
refuses rather than guesses — the symbology, the event names and the aggressor
field all need confirming against the live SDK during Epoch 0, and a transport
that invents plausible bars is worse than one that will not start.
"""

from __future__ import annotations

import logging
import math
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import config as C
from data.contract_registry import (assess_roll, front_and_back, get_spec,
                                    OFF_WINDOW)
from data.feed_store import FeedStore
from utils import sessions as S

log = logging.getLogger("futures-feed")

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600,
              "4h": 14400, "1d": 86400}


class Transport:
    """Returns whatever has arrived since the last call."""

    def connect(self, symbol: str) -> None:
        raise NotImplementedError

    def drain(self) -> Tuple[List[Tuple], Optional[Tuple[float, float, float]]]:
        """-> (prints, quote). prints: (ts, seq, price, size, aggressor)."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class SimulatedTransport(Transport):
    """Deterministic synthetic tape. Used by the tests and by `--sim`, which is
    how the whole pipeline gets exercised on a box with no market open."""

    def __init__(self, start_price: float = 21000.0, tick_size: float = 0.25,
                 seed: int = 11, step_s: int = 1):
        self.px = start_price
        self.tick = tick_size
        self.rnd = seed
        self.step_s = step_s
        self.seq = 0
        self.t = int(time.time())

    def connect(self, symbol: str) -> None:
        log.info("simulated transport connected for %s", symbol)

    def drain(self):
        prints = []
        for _ in range(20):
            self.rnd = (1103515245 * self.rnd + 12345) % 2147483648
            u = self.rnd / 2147483648.0
            move = (1 if u > 0.5 else -1) * self.tick * (1 + int(u * 3))
            self.px = round((self.px + move) / self.tick) * self.tick
            self.seq += 1
            prints.append((self.t, self.seq, self.px, 1 + int(u * 5),
                           "BUY" if move > 0 else "SELL"))
            self.t += self.step_s
        return prints, (self.px - self.tick, self.px + self.tick, self.px)


class DXLinkTransport(Transport):
    """SKELETON — refuses rather than guesses.

    Confirm during Epoch 0, on the tiny account, one at a time:
      * the streamer symbol for a futures front month (/MNQU6 vs /MNQ) and
        whether it matches what the ORDER endpoint expects
      * which event carries the aggressor side, and how it is encoded
      * whether candle events arrive as snapshots or increments on reconnect
      * the reconnect/backfill behaviour after a session break
    """

    def connect(self, symbol: str) -> None:
        raise NotImplementedError(
            "DXLinkTransport is unverified — confirm the futures symbology and "
            "the aggressor field against the SDK before any live feed. "
            "Use SimulatedTransport (--sim) until then.")

    def drain(self):
        raise NotImplementedError("DXLinkTransport.drain unverified")


@dataclass
class Aggregator:
    """Ticks -> candles. Keeps a partial bar per timeframe and upserts it, so a
    consumer always sees the developing bar and never a gap."""
    tick_size: float
    bars: Dict[str, Dict[int, List[float]]] = field(default_factory=dict)

    def add(self, prints: Sequence[Tuple]) -> Dict[str, List[Tuple]]:
        out: Dict[str, List[Tuple]] = {}
        for tf, secs in TF_SECONDS.items():
            if tf not in C.TIMEFRAMES:
                continue
            book = self.bars.setdefault(tf, {})
            touched = set()
            for ts, _seq, px, size, _agg in prints:
                b = (int(ts) // secs) * secs
                cur = book.get(b)
                if cur is None:
                    book[b] = [px, px, px, px, float(size)]
                else:
                    cur[1] = max(cur[1], px)
                    cur[2] = min(cur[2], px)
                    cur[3] = px
                    cur[4] += float(size)
                touched.add(b)
            if touched:
                out[tf] = [(b, *book[b]) for b in sorted(touched)]
            # keep memory bounded — the store is the archive, not this dict
            if len(book) > 1200:
                for k in sorted(book)[:-800]:
                    del book[k]
        return out


class FeedProducer:
    def __init__(self, transport: Optional[Transport] = None,
                 store_path: Optional[str] = None,
                 back_transport: Optional[Transport] = None):
        self.spec = get_spec(C.SYMBOL)
        self.store = FeedStore(store_path or C.CANDLE_STORE)
        self.transport = transport or DXLinkTransport()
        self.agg = Aggregator(self.spec.tick_size)
        self.running = True
        front, back = front_and_back(C.SYMBOL, S.session_date())
        self.contract = front.code
        self.back_contract = back.code
        # THE BACK MONTH IS SUBSCRIBED ONLY INSIDE THE ROLL WINDOW, and only for
        # VOLUME. A crossover needs two numbers and a box that watches one
        # contract has nothing to compare — but carrying a second subscription
        # all year would double the feed cost for a signal that matters on about
        # eight days of it. No back-month candles, no back-month quotes.
        self._back_transport = back_transport
        self._back_connected = False

    def in_roll_window(self, on=None) -> bool:
        try:
            a = assess_roll(C.SYMBOL, on or S.session_date())
        except Exception:                                    # noqa: BLE001
            return False
        return a.state != OFF_WINDOW

    def step(self) -> int:
        sess = S.session_date().isoformat()
        prints, quote = self.transport.drain()
        n = 0
        if prints:
            self.store.append_trades(C.SYMBOL, prints)
            for tf, rows in self.agg.add(prints).items():
                n += self.store.upsert_candles(C.SYMBOL, tf, rows)
            self.store.add_session_volume(
                self.contract, sess, sum(p[3] for p in prints))
        if quote:
            self.store.put_quote(C.SYMBOL, quote[0], quote[1], quote[2])

        # back month — volume only, and only inside the window
        if self.in_roll_window():
            if self._back_transport is not None and not self._back_connected:
                try:
                    self._back_transport.connect(self.back_contract)
                    self._back_connected = True
                    log.info("roll window open — subscribed %s for volume",
                             self.back_contract)
                except Exception as e:                       # noqa: BLE001
                    log.warning("back-month subscribe failed: %s", e)
            if self._back_connected:
                try:
                    bprints, _ = self._back_transport.drain()
                    if bprints:
                        self.store.add_session_volume(
                            self.back_contract, sess,
                            sum(p[3] for p in bprints))
                except Exception as e:                       # noqa: BLE001
                    log.warning("back-month drain failed: %s", e)
        elif self._back_connected:
            try:
                self._back_transport.close()
            except Exception:                                # noqa: BLE001
                pass
            self._back_connected = False
            log.info("roll window closed — released %s", self.back_contract)

        self.store.beat("feed", f"{self.contract} {len(prints)} prints"
                                f"{' +back' if self._back_connected else ''}")
        return n

    def run(self, poll_s: float = 1.0) -> int:
        self.transport.connect(C.SYMBOL)
        log.info("feed producing %s (%s) -> %s", C.SYMBOL, self.contract,
                 self.store.path)
        while self.running:
            try:
                self.step()
            except Exception as e:                           # noqa: BLE001
                log.exception("feed step failed: %s", e)
            time.sleep(poll_s)
        self.transport.close()
        return 0

    def stop(self, *_):
        self.running = False


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s feed: %(message)s")
    argv = argv if argv is not None else sys.argv[1:]
    sim = "--sim" in argv
    tick = get_spec(C.SYMBOL).tick_size
    t = SimulatedTransport(tick_size=tick) if sim else None
    b = SimulatedTransport(tick_size=tick, seed=29) if sim else DXLinkTransport()
    p = FeedProducer(t, back_transport=b)
    signal.signal(signal.SIGTERM, p.stop)
    signal.signal(signal.SIGINT, p.stop)
    return p.run()


if __name__ == "__main__":
    raise SystemExit(main())
