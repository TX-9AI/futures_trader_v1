"""
futures_trader_v1/risk/capacity.py — v0.3
v0.3 — 2026-07-25 — PAPER short-circuits equity resolution to the fixed
        constant BEFORE the broker branch (operator directive: paper must never
        pull the live balance). Header states the equity is fixed.
v0.2 — 2026-07-25 — Eligibility policy wired in: X (excluded) vs 0
        (unaffordable) markers, ladders suppressed for an excluded box mode,
        the "this box should not exist" verdict, and the universe matrix.
v0.1 — 2026-07-25 — Initial build. The tick chart: what THIS box's symbol can
        carry, per mode, at the account balance resolved AT CALL TIME.

Scoped to `config.SYMBOL` by design — a box knows one contract, and a table
listing 37 roots is a reference document, not an operator tool. Devtools calls
this; it re-derives on every invocation.

EQUITY RESOLUTION, AND WHY THE SOURCE IS PRINTED
  broker snapshot -> explicit FT_ACCOUNT_EQUITY -> paper/live default
A sizing table computed off a stale balance is worse than no table, so the
source and its timestamp are in the header of every render. The otv3 precedent:
status.py spent weeks printing a "$200 DAILY LOSS LIMIT HIT" banner because the
number was resolved in the wrong environment — the value was wrong and nothing
on screen said where it came from.

THREE CONSTRAINTS, AND THE REPORT NAMES WHICH ONE BINDS
  margin  — equity x utilization cap / per-contract requirement (mode's rate)
  risk    — budget / (stop_ticks x tick_value + commission)
  config  — MAX_CONTRACTS, the operator's own ceiling
Knowing WHICH one binds is the actionable part: a margin bind means the account
is too small for the contract, a risk bind means the stop is too wide for the
budget, and a config bind means you chose the limit yourself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

import config as C
from data.contract_registry import ContractSpec, front_and_back, get_spec
from execution.margin_manager import (DAY_RATE, INITIAL_RATE, RATE_FOR_MODE,
                                      MarginManager, AccountSnapshot)
from risk.eligibility import EXCLUDED, mode_permitted
from utils.sessions import ET, now_et, session_date

BIND_MARGIN = "margin"
BIND_RISK = "risk"
BIND_CONFIG = "config"
BIND_NONE = "none"


@dataclass
class EquityResolution:
    value: float
    source: str
    at: datetime
    synthetic: bool = False

    def label(self) -> str:
        return f"${self.value:,.0f}  ({self.source}, {self.at.strftime('%H:%M ET')})"


def resolve_equity(broker: Optional[AccountSnapshot] = None) -> EquityResolution:
    """PAPER short-circuits to the fixed constant BEFORE the broker is consulted.

    This ordering is deliberate and load-bearing. The bot holds a broker session
    in paper mode too (it needs the market data), so a naive broker-first
    resolver would let a live net-liq leak into paper sizing the moment the
    account is funded — silently changing every capacity table and every dial
    calibrated against one. Paper equity moves only when the operator moves it.
    """
    now = now_et()
    if C.PAPER_TRADING:
        return EquityResolution(C.PAPER_EQUITY_DEFAULT,
                                "PAPER — fixed", now, False)
    if broker and not broker.stale and broker.net_liq > 0:
        return EquityResolution(broker.net_liq, "broker", broker.as_of or now, False)
    if C.ACCOUNT_EQUITY_EXPLICIT:
        return EquityResolution(C.ACCOUNT_EQUITY_DEFAULT, "FT_ACCOUNT_EQUITY", now, False)
    return EquityResolution(C.ACCOUNT_EQUITY_DEFAULT, "config default", now, False)


@dataclass
class ModeCapacity:
    mode: str
    rate_kind: str
    per_contract: float          # includes the margin buffer
    by_margin: int
    by_risk: int
    by_config: int
    allowed: int
    binding: str
    gap_mult: float = 1.0
    excluded: bool = False
    exclusion_reason: str = ""

    def cell(self) -> str:
        return "X" if self.excluded else str(self.allowed)


@dataclass
class LadderRow:
    lots: int
    margin: float
    pct_equity: float
    max_stop_ticks: float
    max_stop_points: float
    dollar_risk: float
    holdable_overnight: bool


@dataclass
class CapacityReport:
    spec: ContractSpec
    contract_code: str
    equity: EquityResolution
    risk_budget: float
    modes: List[ModeCapacity]
    ladder: List[LadderRow]
    overnight_ladder: List[LadderRow]
    box_mode: str
    live_gap: Optional[List[Tuple[str, int, int]]] = None   # (mode, paper, live)
    warnings: List[str] = field(default_factory=list)


# ─── computation ──────────────────────────────────────────────────────────────
def _max_stop_ticks(budget: float, lots: int, spec: ContractSpec,
                    commission: float, gap_mult: float = 1.0) -> float:
    """Widest stop affordable at `lots` contracts. Inverts the sizing contract:
        risk = lots x (stop_ticks x tick_value + commission) x gap_mult
    Overnight modes carry gap_mult > 1 because a futures stop is not guaranteed
    through the break or a weekend — price gaps THROUGH it."""
    if lots < 1 or spec.tick_value <= 0:
        return 0.0
    per_lot = budget / (lots * max(gap_mult, 1e-9))
    return max((per_lot - commission) / spec.tick_value, 0.0)


def _cap_by_risk(budget: float, spec: ContractSpec, stop_ticks: float,
                 commission: float, gap_mult: float = 1.0) -> int:
    per = (stop_ticks * spec.tick_value + commission) * gap_mult
    return int(budget // per) if per > 0 else 0


def compute(broker: Optional[AccountSnapshot] = None,
            symbol: Optional[str] = None,
            on=None) -> CapacityReport:
    spec = get_spec(symbol or C.SYMBOL)
    eq = resolve_equity(broker)
    budget = C.risk_per_trade(eq.value)
    on = on or session_date()
    front, _ = front_and_back(spec.root, on)

    floor_ticks = spec.min_stop_ticks * C.MIN_STOP_TICK_MULT
    warnings: List[str] = []

    modes: List[ModeCapacity] = []
    for mode in ("SCALP", "DAY", "SWING", "HEDGE"):
        mm = MarginManager(spec, mode, C.MARGIN_UTILIZATION_MAX,
                           C.MARGIN_BUFFER_MULT, C.USE_BROKER_MARGIN)
        if broker and not C.PAPER_TRADING:
            mm.apply_account(broker)
        cap = mm.capacity(eq.value)
        gap = C.OVERNIGHT_GAP_MULT if mode in ("SWING", "HEDGE") else 1.0
        by_risk = (_cap_by_risk(budget, spec, floor_ticks,
                                C.COMMISSION_PER_CONTRACT_RT, gap)
                   if mode != "HEDGE" else 10 ** 6)   # hedge sizes off exposure
        by_cfg = C.HEDGE_MAX_CONTRACTS if mode == "HEDGE" else C.MAX_CONTRACTS
        elig = mode_permitted(spec, mode, eq.value)
        allowed = 0 if elig.excluded else min(cap.max_contracts, by_risk, by_cfg)
        if allowed == 0:
            binding = BIND_MARGIN if cap.max_contracts == 0 else BIND_RISK
        elif allowed == cap.max_contracts:
            binding = BIND_MARGIN
        elif allowed == by_risk:
            binding = BIND_RISK
        else:
            binding = BIND_CONFIG
        modes.append(ModeCapacity(mode, cap.rate_kind, mm.per_contract() * C.MARGIN_BUFFER_MULT,
                                  cap.max_contracts,
                                  0 if by_risk > 10 ** 5 else by_risk,
                                  by_cfg, allowed, "excluded" if elig.excluded else binding,
                                  gap, elig.excluded, elig.reason))
        if elig.excluded:
            warnings.append(f"{mode} X — {elig.reason}")
        elif allowed == 0:
            warnings.append(f"{mode}: permitted but unaffordable today — {binding} bound")

    box = C.MODE
    mm_box = MarginManager(spec, box, C.MARGIN_UTILIZATION_MAX, C.MARGIN_BUFFER_MULT)
    per_day = mm_box.rates.rate(RATE_FOR_MODE.get(box, INITIAL_RATE)) * C.MARGIN_BUFFER_MULT
    per_ovn = mm_box.rates.initial * C.MARGIN_BUFFER_MULT
    allowance = eq.value * C.MARGIN_UTILIZATION_MAX

    def _ladder(per: float, gap: float) -> List[LadderRow]:
        rows = []
        top = max(1, min(C.MAX_CONTRACTS + 2, 8))
        for n in range(1, top + 1):
            st = _max_stop_ticks(budget, n, spec, C.COMMISSION_PER_CONTRACT_RT, gap)
            rows.append(LadderRow(n, per * n, (per * n) / eq.value if eq.value else 0.0,
                                  st, st * spec.tick_size, budget,
                                  per_ovn * n <= allowance))
        return rows

    ladder = _ladder(per_day, 1.0)
    ovn_ladder = _ladder(per_ovn, C.OVERNIGHT_GAP_MULT)

    live_gap = None
    if eq.synthetic:
        live_eq = C.ACCOUNT_EQUITY_DEFAULT
        live_budget = C.risk_per_trade(live_eq)
        live_gap = []
        for m in modes:
            mm = MarginManager(spec, m.mode, C.MARGIN_UTILIZATION_MAX, C.MARGIN_BUFFER_MULT)
            lcap = mm.capacity(live_eq)
            gap = C.OVERNIGHT_GAP_MULT if m.mode in ("SWING", "HEDGE") else 1.0
            lrisk = (_cap_by_risk(live_budget, spec, floor_ticks,
                                  C.COMMISSION_PER_CONTRACT_RT, gap)
                     if m.mode != "HEDGE" else 10 ** 6)
            lcfg = C.HEDGE_MAX_CONTRACTS if m.mode == "HEDGE" else C.MAX_CONTRACTS
            live_gap.append((m.mode, m.allowed, min(lcap.max_contracts, lrisk, lcfg)))
        dead = [m for m, p, l in live_gap if p > 0 and l == 0]
        if dead:
            warnings.append("PAPER-ONLY: " + ", ".join(dead) +
                            f" cannot be traded at the live ${live_eq:,.0f} balance")

    return CapacityReport(spec, front.code, eq, budget, modes, ladder,
                          ovn_ladder, box, live_gap, warnings)


# ─── render (<= 64 cols, Termius on a phone) ──────────────────────────────────
def tick_chart(rep: CapacityReport) -> str:
    s = rep.spec
    L: List[str] = []
    bar = "=" * 62
    L.append(bar)
    L.append(f" TICK CHART — {s.root}  ({s.name})")
    L.append(f" contract {rep.contract_code} · tick {s.tick_size:g} = "
             f"${s.tick_value:,.2f} · mult {s.multiplier:g}")
    L.append(f" equity  {rep.equity.label()}")
    L.append(f" risk/trade ${rep.risk_budget:,.0f}  "
             f"({C.RISK_PCT_OF_EQUITY*100:.1f}% of equity)")
    L.append(f" box mode {rep.box_mode} · util cap "
             f"{C.MARGIN_UTILIZATION_MAX*100:.0f}% · max lots {C.MAX_CONTRACTS}")
    if C.PAPER_TRADING:
        L.append(" paper equity is FIXED — it does not track the broker")
    if rep.equity.synthetic:
        L.append(" *** SYNTHETIC equity — paper/live parity is BROKEN ***")
    L.append(bar)

    L.append("")
    L.append(" MODE CAPACITY")
    L.append(" mode    rate      $/lot   marg  risk   cfg  ALLOW  binds")
    L.append(" " + "-" * 58)
    for m in rep.modes:
        risk_s = "   -" if (m.by_risk == 0 and m.mode == "HEDGE") else f"{m.by_risk:4d}"
        L.append(f" {m.mode:7}{m.rate_kind:9}{m.per_contract:7,.0f}"
                 f"{m.by_margin:6d}{risk_s}{m.by_config:6d}{m.cell():>6}"
                 f"  {m.binding}")
    L.append("   n = lots · 0 = permitted, unaffordable now · X = excluded")

    _box = next((m for m in rep.modes if m.mode == rep.box_mode), None)
    if _box and _box.excluded:
        L.append("")
        L.append(f" STOP LADDER — suppressed: {rep.box_mode} is X for {s.root}")
        L.append(f"   {_box.exclusion_reason}")
        if all(m.excluded for m in rep.modes):
            L.append("")
            L.append(" >>> THIS BOX SHOULD NOT EXIST at this balance. <<<")
            if s.sibling:
                L.append(f"     Run {s.sibling} instead — same exposure, "
                         f"fundable size.")
        L.append(bar)
        return "\n".join(L)

    L.append("")
    L.append(f" STOP LADDER — {rep.box_mode} mode, ${rep.risk_budget:,.0f} budget")
    L.append(" lots   margin   %eq   max stop        = points   $risk")
    L.append(" " + "-" * 57)
    for r in rep.ladder:
        L.append(f" {r.lots:4d}{r.margin:9,.0f}{r.pct_equity*100:6.1f}%"
                 f"{r.max_stop_ticks:9.0f} ticks{r.max_stop_points:10.2f}"
                 f"{r.dollar_risk:8,.0f}")
    floor_ticks = s.min_stop_ticks * C.MIN_STOP_TICK_MULT
    L.append(f" floor {floor_ticks:.0f} ticks ({floor_ticks*s.tick_size:.2f} pts) — "
             f"tighter is inside noise")
    L.append(f" ceiling {C.MAX_STOP_ATR_MULT:.1f}x ATR — needs live data")

    _ovn = next((m for m in rep.modes if m.mode == "SWING"), None)
    if _ovn and _ovn.excluded:
        L.append("")
        L.append(" OVERNIGHT LADDER — X, not an overnight carry")
        L.append(f"   {_ovn.exclusion_reason}")
    else:
        L.append("")
        L.append(f" OVERNIGHT LADDER — initial rate, {C.OVERNIGHT_GAP_MULT:.0f}x gap allowance")
        L.append(" lots   margin   %eq   max stop        = points   hold?")
        L.append(" " + "-" * 57)
        for r in rep.overnight_ladder:
            L.append(f" {r.lots:4d}{r.margin:9,.0f}{r.pct_equity*100:6.1f}%"
                     f"{r.max_stop_ticks:9.0f} ticks{r.max_stop_points:10.2f}"
                     f"    {'yes' if r.holdable_overnight else 'NO':>4}")

    if rep.live_gap:
        L.append("")
        L.append(f" PAPER -> LIVE GAP (live equity "
                 f"${C.ACCOUNT_EQUITY_DEFAULT:,.0f})")
        L.append(" mode    paper lots   live lots   transfers?")
        L.append(" " + "-" * 57)
        for mode, p, l in rep.live_gap:
            verdict = "yes" if l > 0 else "NO — paper only"
            L.append(f" {mode:8}{p:11d}{l:12d}   {verdict}")

    if rep.warnings:
        L.append("")
        L.append(" WARNINGS")
        for w in rep.warnings:
            L.append(f"  ! {w}")
    L.append(bar)
    return "\n".join(L)


def universe_matrix(eq: EquityResolution) -> str:
    """Every root x mode at this balance. The operator view that answers
    'which boxes should exist' in one screen."""
    from data.contract_registry import ROOTS, SPECS
    budget = C.risk_per_trade(eq.value)
    L = ["=" * 62,
         f" UNIVERSE @ {eq.label()}  ·  risk ${budget:,.0f}/trade",
         " n = lots · X = excluded by policy",
         "=" * 62,
         f" {'root':6}{'SCALP':>6}{'DAY':>5}{'SWING':>6}{'HEDGE':>6}   note",
         " " + "-" * 56]
    drops, subs = [], []
    for r in ROOTS:
        spec = SPECS[r]
        cells = []
        for m in ("SCALP", "DAY", "SWING", "HEDGE"):
            e = mode_permitted(spec, m, eq.value)
            if e.excluded:
                cells.append("X")
                continue
            mm = MarginManager(spec, m, C.MARGIN_UTILIZATION_MAX, C.MARGIN_BUFFER_MULT)
            gap = C.OVERNIGHT_GAP_MULT if m in ("SWING", "HEDGE") else 1.0
            by_r = (_cap_by_risk(budget, spec,
                                 spec.min_stop_ticks * C.MIN_STOP_TICK_MULT,
                                 C.COMMISSION_PER_CONTRACT_RT, gap)
                    if m != "HEDGE" else 10 ** 6)
            cfg = C.HEDGE_MAX_CONTRACTS if m == "HEDGE" else C.MAX_CONTRACTS
            cells.append(str(min(mm.capacity(eq.value).max_contracts, by_r, cfg)))
        note = ""
        if all(c in ("X", "0") for c in cells):
            if spec.sibling:
                note = f"-> {spec.sibling}"
                subs.append(r)
            else:
                note = "DROP — no micro"
                drops.append(r)
        L.append(f" {r:6}{cells[0]:>6}{cells[1]:>5}{cells[2]:>6}{cells[3]:>6}   {note}")
    L.append(" " + "-" * 56)
    if subs:
        L.append(f" substitute the micro: {', '.join(subs)}")
    if drops:
        L.append(f" DROP from the universe: {', '.join(drops)}")
    L.append("=" * 62)
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Tick chart / capacity for this box's symbol")
    ap.add_argument("--symbol", help="override FT_SYMBOL (reference only)")
    ap.add_argument("--equity", type=float, help="override the resolved balance")
    ap.add_argument("--matrix", action="store_true",
                    help="every root x mode at this balance (universe view)")
    args = ap.parse_args(argv)
    snap = AccountSnapshot(net_liq=args.equity, as_of=now_et(),
                           source="cli") if args.equity else None
    if args.matrix:
        print(universe_matrix(resolve_equity(snap)))
        return 0
    print(tick_chart(compute(broker=snap, symbol=args.symbol)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
