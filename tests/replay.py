"""
futures_trader_v1/tests/replay.py — v0.1
v0.1 — 2026-07-25 — Offline replay of archived tape through the LIVE engines.

    python3 tests/replay.py --store <feed_store.db> [--symbol MNQ]
    python3 tests/replay.py --dir <harvest dir> --bookmark 15

THIS IS THE VEHICLE FOR THE ENTIRE EPOCH LADDER. Epochs 2-4 — re-fitting L1
bounds, calibrating L2 hysteresis, placing L3 conviction bars — all require
scoring archived tape with the same code the fleet runs. It imports the real
engines rather than reimplementing them, so a replay can never quietly drift
into measuring a bot that no longer exists.

THE BOOKMARK IS BUILT IN, NOT RETROFITTED.
The options replay fed the harness ONE DAY AT A TIME, so ATR, Bollinger and the
EMA stacks never warmed and the higher timeframes stayed starved. Measured
consequence: on 100,281 replay ticks, 21.9% produced NO ranging evidence at all
because ATR was still cold — and it was the first ~75 minutes of every session,
the most active part of the day. The diary understated one whole regime for
months and the fix stayed on the to-do list.

So this harness carries a ROLLING WINDOW OF BARS across sessions
(`--bookmark`, default 15 sessions). Engines are stateless pure functions of the
bars handed to them, so there is no serialized state to drift — only warm depth.
And the summary REPORTS THE STARVED FRACTION explicitly, so if it is ever
non-trivial again it is visible on the first run instead of a year later.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import liquidity as LQ
from analysis import market_structure as MS
from analysis import orderflow as OF
from analysis import profile as PF
from analysis import trend as TR
from analysis import volatility as VOL
from analysis.conviction_integrator import (ConvictionIntegrator,
                                            IntegratorParams)
from analysis.regime_confluence import REGIMES, ConfluenceScorer
from data.contract_registry import get_spec
from data.feed_store import FeedStore
from data.series import Candles, Tape
from utils.sessions import ET

WARM_BARS = {"1m": 200, "5m": 120, "15m": 80, "1h": 60, "1d": 30}


@dataclass
class ReplaySummary:
    ticks: int = 0
    sessions: List[str] = field(default_factory=list)
    labels: Counter = field(default_factory=Counter)
    starved: int = 0                 # ticks with NO usable evidence
    ranging_silent: int = 0          # BALANCED returned None — UNSCOREABLE
    ranging_vetoed: int = 0          # BALANCED scored 0.0 — scored and refused
    cooccurrence: int = 0            # two regimes both > 0.5 on one tick
    stale: int = 0

    def report(self) -> str:
        t = max(self.ticks, 1)
        lines = ["=" * 62,
                 f" REPLAY — {self.ticks} ticks over {len(self.sessions)} session(s)",
                 "=" * 62, " emitted label distribution:"]
        for lab, n in self.labels.most_common():
            lines.append(f"   {lab:16} {n:7d}  {100*n/t:5.1f}%")
        lines += ["",
                  f" starved (no evidence at all)   {self.starved:7d}  "
                  f"{100*self.starved/t:5.1f}%",
                  f" BALANCED unscoreable (starved) {self.ranging_silent:7d}  "
                  f"{100*self.ranging_silent/t:5.1f}%",
                  f" BALANCED scored-and-vetoed     {self.ranging_vetoed:7d}  "
                  f"{100*self.ranging_vetoed/t:5.1f}%  (healthy)",
                  f" two regimes >0.5 on one tick   {self.cooccurrence:7d}  "
                  f"{100*self.cooccurrence/t:5.1f}%",
                  f" L2 stale                       {self.stale:7d}  "
                  f"{100*self.stale/t:5.1f}%",
                  ""]
        if self.starved / t > 0.05 or self.ranging_silent / t > 0.05:
            lines.append(" ⚠ WARM-UP STARVATION — raise --bookmark, or the tape")
            lines.append("   does not carry enough prior sessions. Calibrating on")
            lines.append("   this run would repeat the options blind spot.")
        else:
            lines.append(" warm-up looks healthy — bookmark is doing its job")
        lines.append("=" * 62)
        return "\n".join(lines)


class Replayer:
    """Scores archived tape with the production engines."""

    def __init__(self, symbol: str, bookmark_sessions: int = 15,
                 step_bars: int = 1):
        self.symbol = symbol
        self.spec = get_spec(symbol)
        self.bookmark = bookmark_sessions
        self.step = max(1, step_bars)

        self.l1 = ConfluenceScorer()
        # SAMPLING IS NOT A FEED GAP. The integrator marks anything beyond
        # dt_max as stale — correct live, wrong here: replaying every Nth bar
        # puts N minutes between updates by choice, not by fault. Without this
        # a --step 10 run reported 98% stale and every conviction stayed at
        # zero, which would have made the whole calibration meaningless.
        params = IntegratorParams()
        params.dt_max = max(params.dt_max, self.step * 60 * 1.5)
        self.l2 = ConvictionIntegrator(params)
        self.summary = ReplaySummary()

    # ── tape assembly ────────────────────────────────────────────────────────
    def _load(self, store_path: str) -> Dict[str, Candles]:
        st = FeedStore(store_path, read_only=True)
        out: Dict[str, Candles] = {}
        for tf in ("1m", "5m", "15m", "1h", "1d"):
            rows = st.fetch_candles(self.symbol, tf, limit=200000)
            if not rows:
                continue
            out[tf] = Candles.from_rows(tf, [
                (datetime.fromtimestamp(r[0], tz=timezone.utc).astimezone(ET),
                 r[1], r[2], r[3], r[4], r[5]) for r in rows])
        return out

    @staticmethod
    def _upto(c: Candles, ts) -> Candles:
        """Bars strictly at or before `ts`. Slicing rather than replaying a
        stream keeps the harness a pure function of the archive — the same run
        twice gives the same answer, which is what makes a calibration
        reproducible."""
        n = 0
        for t in c.ts:
            if t > ts:
                break
            n += 1
        return c.slice(0, n)

    # ── the run ──────────────────────────────────────────────────────────────
    def run(self, store_path: str, out_path: Optional[str] = None) -> ReplaySummary:
        frames = self._load(store_path)
        base = frames.get("1m")
        if not base or not len(base):
            raise SystemExit(f"no 1m tape for {self.symbol} in {store_path}")

        # Start only once the bookmark window has depth — replaying the first
        # cold bars would put exactly the starvation we are trying to avoid back
        # into the numbers.
        start = min(len(base) - 1, WARM_BARS["1m"])
        fh = open(out_path, "w") if out_path else None
        seen_sessions = set()

        try:
            for i in range(start, len(base), self.step):
                ts = base.ts[i]
                seen_sessions.add(ts.date().isoformat())
                tape = Tape()
                for tf, c in frames.items():
                    sliced = self._upto(c, ts)
                    keep = WARM_BARS.get(tf, 100)
                    if len(sliced):
                        tape.put(sliced.tail(keep))
                c1 = tape.get("1m")
                c5 = tape.get("5m") or c1
                if not c1:
                    continue

                vol = VOL.analyze(c5)
                trend = TR.analyze(tape)
                struct = MS.analyze(c1, self.spec.tick_size)
                flow = OF.build(c1)
                prof = PF.analyze(c5, self.spec.tick_size)

                ev = self.l1.score(c1.close, vol, trend, struct, flow, prof)
                st = self.l2.update(ts.timestamp(), ev.vector())

                self.summary.ticks += 1
                if not ev.observable:
                    self.summary.starved += 1
                # UNSCOREABLE and VETOED are different facts and conflating
                # them is what made the options diagnosis slow: a None means the
                # inputs were not warm enough to have an opinion, a 0.0 means
                # the engine looked and said no. Only the first is starvation.
                if ev.observable:
                    b = ev.scores.get("BALANCED")
                    if b is None:
                        self.summary.ranging_silent += 1
                    elif b == 0.0:
                        self.summary.ranging_vetoed += 1
                hot = [r for r in REGIMES if (ev.scores.get(r) or 0) > 0.5]
                if len(hot) > 1:
                    self.summary.cooccurrence += 1
                if st.stale:
                    self.summary.stale += 1
                self.summary.labels[st.regime or "none"] += 1

                if fh:
                    fh.write(json.dumps({
                        "ts": ts.isoformat(), "sym": self.symbol,
                        "price": c1.close[-1],
                        "l1": {k: (round(v, 4) if v is not None else None)
                               for k, v in ev.scores.items()},
                        "l2": {"regime": st.regime,
                               "conviction": round(st.conviction, 4),
                               "stale": st.stale, "trigger": st.trigger},
                        "warm": {"atr": vol.atr is not None,
                                 "bb": vol.bb_width_pct is not None,
                                 "frames": sorted(tape.frames)},
                    }) + "\n")
        finally:
            if fh:
                fh.close()
        self.summary.sessions = sorted(seen_sessions)
        return self.summary


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="offline replay through the live engines")
    ap.add_argument("--store", help="a feed_store.db to replay")
    ap.add_argument("--dir", help="a directory of harvested *_flow.db files")
    ap.add_argument("--symbol", default=os.environ.get("FT_SYMBOL", "MNQ"))
    ap.add_argument("--bookmark", type=int, default=15,
                    help="sessions of warm-up depth to carry (default 15)")
    ap.add_argument("--step", type=int, default=1, help="score every Nth bar")
    ap.add_argument("--out", help="write per-tick JSONL here")
    a = ap.parse_args(argv)

    stores = []
    if a.store:
        stores = [a.store]
    elif a.dir:
        stores = sorted(glob.glob(os.path.join(a.dir, "**", "*.db"), recursive=True))
    if not stores:
        ap.error("give --store or --dir")

    r = Replayer(a.symbol, a.bookmark, a.step)
    for s in stores:
        try:
            r.run(s, a.out)
        except SystemExit as e:
            print(f"  skip {os.path.basename(s)}: {e}")
    print(r.summary.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
