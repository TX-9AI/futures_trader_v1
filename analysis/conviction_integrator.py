"""
futures_trader_v1/analysis/conviction_integrator.py — v0.1
v0.1 — 2026-07-25 — Initial build. LAYER 2: persistence.

THIS IS THE "PERSISTENCE AND PRESCIENCE" REQUIREMENT, MADE MECHANICAL.
Layer 1 is memoryless: it reports what this instant looks like. That is not
enough, and the options project proved it expensively — a memoryless classifier
dropped to a no-trade label mid-trend at an average ADX of 29, vetoing entries
during the strongest conditions the engine would ever see.

Persistence is not smoothing. It is the refusal to un-learn something on one
tick of contrary evidence, while still yielding to sustained contrary evidence.
That asymmetry is the whole design:

  RISE   toward evidence with time constant tau_up
  DECAY  away from it with tau_dn(C) = tau_dn0 * exp(lam * C)
         — decay RESISTANCE scales with banked conviction, so a regime that has
         earned belief over minutes does not evaporate in one tick, while a
         regime that just arrived can leave just as fast.

  EMIT   always argmax. There is no seventh "unknown" label. Indecision is a LOW
         CONVICTION NUMBER on a best-fit label, never a separate state that can
         become a hard gate. (Deleting that label was the single highest-value
         fix in the options L2 port.)
  HOLD   theta_hold hysteresis + a displacement margin a challenger must clear.
  STALE  a data fault is the ONLY hard no-trade marker. Missing data blocks;
         indecision does not.

ALL CONSTANTS BELOW ARE PRIORS INHERITED FROM options conviction_integrator v2.0
AND ARE RECALIBRATED IN EPOCH 3. They transfer directly because both engines run
a ~15-second poll, so the per-regime time constants mean the same thing in bars.
The RANGING/BALANCED constant carries a specific empirical finding worth keeping:
on real tape, trends held a false-flat angle for 12-15 bars while genuine ranges
held 24-29, so tau_up is set to commit at ~17-19 bars — past the impostor window,
inside the genuine one. Balance is the premium-selling / fade gate; slow is
correct.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from analysis.regime_confluence import (BALANCED, COMPRESSION, EXPANSION,
                                        LIQUIDITY_SWEEP, REGIMES,
                                        TRENDING_DOWN, TRENDING_UP)


@dataclass
class RegimeParams:
    """Units: seconds."""
    tau_up: float
    tau_dn0: float
    lam: float


@dataclass
class IntegratorParams:
    theta_commit: float = 0.65     # conviction needed to EMIT a regime
    theta_hold: float = 0.45       # conviction needed to KEEP one (hysteresis)
    delta_displace: float = 0.12   # margin a challenger needs over the incumbent
    dt_max: float = 90.0           # gap beyond this => do not integrate, mark stale
    tau_stale: float = 600.0       # decay constant while evidence is unobservable

    per_regime: Dict[str, RegimeParams] = field(default_factory=lambda: {
        # Directional regimes commit in roughly one confirmed candle of
        # sustained evidence — recognition is fast, commitment needs a read.
        TRENDING_UP:     RegimeParams(tau_up=40.0,  tau_dn0=25.0, lam=2.2),
        TRENDING_DOWN:   RegimeParams(tau_up=40.0,  tau_dn0=25.0, lam=2.2),
        EXPANSION:       RegimeParams(tau_up=40.0,  tau_dn0=25.0, lam=2.2),
        # Sweeps are EVENTS: recognised fast, and they must die fast when stale
        # so banked sweep conviction cannot squat over a developing expansion.
        LIQUIDITY_SWEEP: RegimeParams(tau_up=25.0,  tau_dn0=15.0, lam=1.5),
        # Compression builds over minutes.
        COMPRESSION:     RegimeParams(tau_up=180.0, tau_dn0=40.0, lam=2.0),
        # See the module docstring: commits at ~17-19 bars, deliberately slow.
        BALANCED:        RegimeParams(tau_up=780.0, tau_dn0=60.0, lam=2.0),
    })


@dataclass
class IntegratorState:
    regime: Optional[str] = None
    conviction: float = 0.0
    convictions: Dict[str, float] = field(default_factory=dict)
    stale: bool = False
    trigger: str = ""
    dt: float = 0.0


class ConvictionIntegrator:
    def __init__(self, params: Optional[IntegratorParams] = None):
        self.p = params or IntegratorParams()
        self.C: Dict[str, float] = {r: 0.0 for r in REGIMES}
        self.incumbent: Optional[str] = None
        self.last_t: Optional[float] = None
        self.stale = False

    # ── integration ──────────────────────────────────────────────────────────
    def update(self, t: float, evidence: Dict[str, Optional[float]]) -> IntegratorState:
        dt = 0.0 if self.last_t is None else max(0.0, t - self.last_t)
        self.last_t = t

        if dt > self.p.dt_max:
            # A gap this large means the feed was blind. Do NOT pretend
            # continuity — decay everything and declare it, so a data fault can
            # never masquerade as a held conviction.
            self._decay_all(dt, self.p.tau_stale)
            self.stale = True
            return self._emit(dt, "stale: feed gap")

        observable = any(v is not None for v in evidence.values())
        if not observable:
            self._decay_all(dt, self.p.tau_stale)
            self.stale = True
            return self._emit(dt, "stale: no observable evidence")

        self.stale = False
        for r in REGIMES:
            e = evidence.get(r)
            if e is None:
                continue
            prm = self.p.per_regime[r]
            c = self.C[r]
            if e >= c:
                # rise toward evidence; tracks it, never overshoots it
                self.C[r] = c + (e - c) * (1.0 - math.exp(-dt / prm.tau_up))
            else:
                tau_dn = prm.tau_dn0 * math.exp(prm.lam * c)
                self.C[r] = e + (c - e) * math.exp(-dt / tau_dn)
            self.C[r] = max(0.0, min(1.0, self.C[r]))
        return self._emit(dt, "integrated")

    def _decay_all(self, dt: float, tau: float) -> None:
        f = math.exp(-dt / tau) if tau > 0 else 0.0
        for r in REGIMES:
            self.C[r] *= f

    # ── emission: always argmax, hysteresis + displacement ───────────────────
    def _emit(self, dt: float, why: str) -> IntegratorState:
        p = self.p
        top = max(self.C, key=lambda r: self.C[r])
        top_c = self.C[top]
        trigger = why
        # A STALE reason must survive the emission branches below. Losing it was
        # a real bug caught in test: the state carried stale=True while the
        # trigger string read "released TRENDING_UP", so the operator-facing
        # explanation blamed conviction decay for what was actually a dead feed.
        stale_prefix = f"{why} | " if self.stale else ""

        if self.incumbent is not None and self.C[self.incumbent] >= p.theta_hold:
            if (top != self.incumbent and top_c >= p.theta_commit
                    and top_c - self.C[self.incumbent] >= p.delta_displace):
                trigger = (f"displaced {self.incumbent} -> {top} "
                           f"({top_c:.2f} vs {self.C[self.incumbent]:.2f})")
                self.incumbent = top
            else:
                trigger = f"held {self.incumbent} ({self.C[self.incumbent]:.2f})"
        else:
            if self.incumbent is not None:
                trigger = (f"released {self.incumbent} "
                           f"({self.C[self.incumbent]:.2f} < {p.theta_hold})")
            self.incumbent = top

        return IntegratorState(regime=self.incumbent,
                               conviction=self.C.get(self.incumbent, 0.0),
                               convictions=dict(self.C),
                               stale=self.stale,
                               trigger=stale_prefix + trigger, dt=dt)

    # ── persistence across restarts ──────────────────────────────────────────
    def snapshot(self) -> dict:
        return {"C": dict(self.C), "incumbent": self.incumbent,
                "last_t": self.last_t, "stale": self.stale}

    def restore(self, snap: dict) -> None:
        """Warm-load at boot. A restart that resets conviction to zero throws
        away exactly the persistence this layer exists to provide — the bot
        would spend the first minutes after every restart uncommitted."""
        if not snap:
            return
        self.C.update({k: float(v) for k, v in (snap.get("C") or {}).items()
                       if k in self.C})
        self.incumbent = snap.get("incumbent")
        self.last_t = snap.get("last_t")
        self.stale = bool(snap.get("stale", False))

    def save(self, path: str) -> bool:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            with open(path, "w") as fh:
                json.dump(self.snapshot(), fh)
            return True
        except Exception:
            return False

    def load(self, path: str) -> bool:
        try:
            with open(path) as fh:
                self.restore(json.load(fh))
            return True
        except Exception:
            return False
