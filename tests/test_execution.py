"""
futures_trader_v1/tests/test_execution.py — v0.1
v0.1 — 2026-07-25 — Behavioural proof for the Phase-3 execution + strategy layer.
    python3 tests/test_execution.py
"""
import os, sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FT_SYMBOL", "MNQ")

import config as C
from data.contract_registry import get_spec
from data.series import Candles, Tape
from utils.sessions import ET
from analysis import market_structure as MS, opening_range as OR, orderflow as OF
from analysis import volatility as V, liquidity as LQ, profile as PF
from analysis.regime_confluence import (BALANCED, EXPANSION, TRENDING_DOWN, TRENDING_UP)
from strategy.base import LONG, SHORT, Signal
from strategy import day_mode, scalp_mode, swing_mode, hedge_mode
from risk import setup_scorer
from risk.risk_manager import RiskManager
from execution.order_confirm import FillResult, confirm_fill, paper_fill, FILLED, WORKING
from execution.entry_engine import EntryEngine, limit_at_mark
from execution.exit_engine import (ADJUST_STOP, CLOSE_ALL, CLOSE_PARTIAL, HOLD,
                                   MARKET, ExitEngine, ManagedPosition, RUNNER, FIXED)
from execution.position_manager import PositionManager
from database.trade_logger import TradeLogger, TradeRecord

PASS = FAIL = 0
def check(n, c, d=""):
    global PASS, FAIL
    if c: PASS += 1; print(f"  PASS  {n}")
    else: FAIL += 1; print(f"  FAIL  {n}  {d}")

SPEC = get_spec("MNQ")
T0 = datetime(2026, 7, 27, 9, 30, tzinfo=ET)
def mk(tf, rows, start=T0, step=1):
    c = Candles(tf)
    for i, (o, h, l, cl, v) in enumerate(rows):
        c.ts.append(start + timedelta(minutes=i * step))
        c.open.append(o); c.high.append(h); c.low.append(l)
        c.close.append(cl); c.volume.append(v)
    return c

print("\n=== 1. SIGNAL CONTRACT — geometry is gated at birth ===")
good = Signal("T", LONG, 21400, 21375, 21475)
check("valid long passes", good.validate(SPEC, 20)[0])
check("R:R computed", abs(good.rr - 3.0) < 1e-9, str(good.rr))
check("r_price(1) is one R up", abs(good.r_price(1.0) - 21425) < 1e-9)
check("r_of() reads back in R", abs(good.r_of(21450) - 2.0) < 1e-9)
check("LONG stop above entry is REFUSED",
      not Signal("T", LONG, 21400, 21425, 21475).validate()[0])
check("LONG target below entry is REFUSED",
      not Signal("T", LONG, 21400, 21375, 21390).validate()[0])
check("zero-width stop is REFUSED",
      not Signal("T", LONG, 21400, 21400, 21475).validate()[0])
check("stop inside the noise floor is REFUSED",
      not Signal("T", LONG, 21400, 21399.5, 21475).validate(SPEC, 20)[0])
check("SHORT geometry validates independently",
      Signal("T", SHORT, 21400, 21425, 21350).validate(SPEC, 20)[0])

print("\n=== 2. FILL CONFIRMATION — nothing books on submission ===")
class Ord:
    def __init__(s, status=WORKING, qty=0, px=None): s.status, s.filled_qty, s.fill_price = status, qty, px
class Sub:
    order_id = "o1"
seq = [Ord(WORKING), Ord(WORKING), Ord(FILLED, 2, 21400.25)]
it = iter(seq)
r = confirm_fill(place=lambda: Sub(), poll=lambda oid: next(it),
                 deadline_s=10, poll_s=0, sleep=lambda s: None,
                 clock=lambda: 0.0)
check("polls to a real fill and books the BROKER price",
      r.usable and r.fill_price == 21400.25 and r.filled_qty == 2, str(r))
ticks = iter([0.0, 1.0, 2.0, 99.0])
r2 = confirm_fill(place=lambda: Sub(), poll=lambda oid: Ord(WORKING),
                  cancel=lambda oid: None, deadline_s=5, poll_s=0,
                  sleep=lambda s: None, clock=lambda: next(ticks))
check("unfilled at deadline is NOT confirmed", not r2.confirmed, str(r2))
ticks2 = iter([0.0, 1.0, 99.0])
r3 = confirm_fill(place=lambda: Sub(), poll=lambda oid: Ord(WORKING, 1, 21400.0),
                  cancel=lambda oid: None, deadline_s=5, poll_s=0,
                  sleep=lambda s: None, clock=lambda: next(ticks2))
check("partial at deadline books ONLY the filled quantity",
      r3.confirmed and r3.filled_qty == 1 and r3.partial, str(r3))
check("a broker that raises on submit is not a fill",
      not confirm_fill(place=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                       poll=lambda o: None).confirmed)
pf = paper_fill(21400.0, 2, LONG, SPEC.tick_size, 1.0)
check("paper LONG pays a tick of slippage (honest, not optimistic)",
      abs(pf.fill_price - 21400.25) < 1e-9, str(pf.fill_price))
check("paper SHORT pays it the other way",
      abs(paper_fill(21400.0, 2, SHORT, SPEC.tick_size, 1.0).fill_price - 21399.75) < 1e-9)

print("\n=== 3. ENTRY ENGINE ===")
check("limit posts AT the mark by default (never crosses)",
      limit_at_mark(21400.13, SPEC, LONG, 0) == 21400.25 or
      limit_at_mark(21400.13, SPEC, LONG, 0) == 21400.0)
ee = EntryEngine(SPEC, paper=True)
res = ee.enter(good, 3, "A", 250.0, 21400.0,
               scale_targets=[(2, 21425.0, "scale_1R")])
check("paper entry fills and returns a plan", res.filled and res.plan.contracts == 3)
check("zero contracts is refused", not ee.enter(good, 0, "B", 0, 21400.0).filled)
live = EntryEngine(SPEC, paper=False)
check("live entry with no broker wired does NOT fabricate a fill",
      not live.enter(good, 1, "B", 250.0, 21400.0).filled)
seq2 = iter([Ord(FILLED, 1, 21400.25)])
live2 = EntryEngine(SPEC, paper=False, place=lambda **k: Sub(),
                    poll=lambda oid: next(seq2))
r4 = live2.enter(good, 3, "A", 250.0, 21400.0, [(2, 21425.0, "scale_1R")])
check("a partial live fill sizes the PLAN to what actually filled",
      r4.filled and r4.plan.contracts == 1, str(r4.plan.contracts if r4.plan else None))

print("\n=== 4. EXIT LADDER — order of evaluation is the design ===")
ex = ExitEngine(SPEC, "DAY")
def pos(contracts=3, direction=LONG, profile=RUNNER, **kw):
    p = ManagedPosition("t1", "D1", direction, 21400.0, 21375.0, 21375.0,
                        21475.0, contracts, contracts, profile, **kw)
    return p
p = pos()
check("holds while nothing has happened", ex.evaluate(p, 21405.0).action == HOLD)
check("FORCED flatten outranks everything and crosses the spread",
      ex.evaluate(p, 21405.0, must_flatten=True).action == CLOSE_ALL and
      ex.evaluate(p, 21405.0, must_flatten=True).order_mode == MARKET)
check("stop hit closes all at MARKET",
      ex.evaluate(p, 21374.0).action == CLOSE_ALL)
pr = pos(regime_at_entry=TRENDING_UP, regime_defined=True)
d = ex.evaluate(pr, 21410.0, regime=BALANCED)
check("regime flip closes a regime-defined trade even while GREEN",
      d.action == CLOSE_ALL and "regime flip" in d.reason, d.reason)
check("a regime flip does NOT close a trade that is not regime-defined",
      ex.evaluate(pos(), 21410.0, regime=BALANCED).action == HOLD)
d = ex.evaluate(pos(), 21425.0)
check("scale-out fires at +1R and banks part of the position",
      d.action == CLOSE_PARTIAL and d.contracts == 2, f"{d.action} {d.contracts}")
p1 = pos(contracts=1)
check("a 1-lot cannot scale — it ratchets to breakeven instead",
      ex.evaluate(p1, 21425.0).action == ADJUST_STOP)
p2 = pos(contracts=3); p2.scaled = True
d = ex.evaluate(p2, 21425.0)
check("after scaling, +1R moves the stop to BREAKEVEN",
      d.action == ADJUST_STOP and abs(d.new_stop - 21400.0) < 1e-9, str(d.new_stop))
p3 = pos(); p3.scaled = True; p3.breakeven_set = True; p3.stop = 21400.0
vol = type("V", (), {"atr": 10.0, "bb_middle": 21400.0, "warm": True})()
d = ex.evaluate(p3, 21460.0, vol=vol)
check("trail arms past +1.5R and only ever tightens",
      d.action == ADJUST_STOP and d.new_stop > 21400.0, str(d))
p3.trail_stop = 21450.0
check("a looser trail level is REJECTED (ratchet, not a moving target)",
      ex.evaluate(p3, 21455.0, vol=vol).action != ADJUST_STOP or
      ex.evaluate(p3, 21455.0, vol=vol).new_stop > 21450.0)
check("trail stop is what gets hit once it is above the entry",
      ex.evaluate(p3, 21449.0).action == CLOSE_ALL)
pf_ = pos(profile=FIXED); pf_.scaled = True; pf_.breakeven_set = True
check("a FIXED profile closes at target",
      ex.evaluate(pf_, 21480.0).action == CLOSE_ALL)
pr_ = pos(); pr_.scaled = True; pr_.breakeven_set = True
check("a RUNNER has NO hard take-profit — the trail owns the upside",
      ex.evaluate(pr_, 21480.0).action != CLOSE_ALL)
pt = pos(contracts=1, time_stop_min=8.0)
pt.opened_at = T0
check("time stop closes a scalp that never went anywhere",
      ex.evaluate(pt, 21402.0, now=T0 + timedelta(minutes=10)).action == CLOSE_ALL)
check("time stop does NOT close a scalp that is working",
      ex.evaluate(pos(contracts=1, time_stop_min=8.0), 21430.0,
                  now=T0 + timedelta(minutes=10)).action != CLOSE_ALL)
ph = pos(profile="HEDGE")
check("a HEDGE is never force-flattened by the session",
      ex.evaluate(ph, 21405.0, must_flatten=True).action != CLOSE_ALL)
p_r = pos(); p_r.scaled = True; p_r.breakeven_set = True; p_r.max_favorable_r = 2.5
flow_div = type("F", (), {"warm": True, "divergence": "BEARISH_DIV",
                          "divergence_strength": 0.6, "approximated": False,
                          "bias": "SELL", "cvd": -5})()
d = ex.evaluate(p_r, 21462.5, vol=vol, flow=flow_div)
check("exhaustion: a new extreme on weaker flow EXITS the runner",
      d.action == CLOSE_ALL and "exhaustion" in d.reason, d.reason)
check("R is anchored to the INITIAL stop, not the ratcheted one",
      abs(pos().r_of(21425.0) - 1.0) < 1e-9)
pbe = pos(); pbe.stop = 21400.0; pbe.breakeven_set = True
check("R denominator survives the breakeven ratchet",
      abs(pbe.r_of(21425.0) - 1.0) < 1e-9, str(pbe.r_of(21425.0)))

print("\n=== 5. POSITION MANAGER — anti-orphan ===")
import tempfile
tl = TradeLogger(os.path.join(tempfile.mkdtemp(), "t.db"), paper=True,
                 tick_value=SPEC.tick_value, tick_size=SPEC.tick_size)
tl.open_trade(TradeRecord(trade_id="t1", root="MNQ", contract_code="MNQU6",
                          mode="DAY", strategy="D1", direction="LONG", contracts=3,
                          entry_price=21400.0, stop_price=21375.0,
                          target_price=21475.0, stop_ticks=100,
                          risk_dollars=150.0, session_date="2026-07-27"), True)
pm = PositionManager(SPEC, "DAY", paper=True, trade_logger=tl)
pm.adopt(pos())
r = pm.manage(21425.0)
check("scale-out executes and books a partial", r.executed and not r.closed)
check("remaining contracts reduced", pm.position.contracts_open == 1)
check("a scaled position immediately rides free (stop at entry)",
      pm.position.breakeven_set and abs(pm.position.stop - 21400.0) < 1e-9)
alerts = []
pm2 = PositionManager(SPEC, "DAY", paper=False, alert=alerts.append)
pm2.adopt(pos())
r2 = pm2.manage(21374.0)
check("an unconfirmed close leaves the position OPEN (anti-orphan)",
      not r2.executed and pm2.position is not None, str(r2.message))
check("and it pages the operator", any("UNCONFIRMED" in a for a in alerts))
pm3 = PositionManager(SPEC, "DAY", paper=True)
pm3.adopt(pos(contracts=1))
r3 = pm3.manage(21374.0)
check("a full close flattens the manager", r3.closed and pm3.flat)
check("manage() on a flat manager returns None, never raises", pm3.manage(21400.0) is None)
pm4 = PositionManager(SPEC, "DAY", paper=True)
pm4.adopt(pos())
check("manage() tolerates every context object being absent",
      pm4.manage(21405.0) is not None)


print("\n=== 6. OPENING RANGE — definitions intact ===")
def orb_tape():
    rows = []
    # 09:30-09:34 range 21000-21020
    for i in range(5):
        rows.append((21005, 21020, 21000, 21010, 500))
    rows.append((21010, 21032, 21008, 21030, 900))   # 09:35 OPENS INSIDE, closes out
    rows.append((21030, 21034, 21018, 21031, 700))   # 09:36 wick in, body out = retest
    return mk("1m", rows)
st = OR.build_range(orb_tape(), SPEC, 5)
check("range built from the contract's own RTH open",
      st.high == 21020 and st.low == 21000, f"{st.low}/{st.high}")
st = OR.update(st, orb_tape(), SPEC)
check("break+retest CONFIRMS long", st.state == OR.CONFIRMED_LONG, st.state)
check("stop anchors to the impulsive candle WICK",
      abs(st.impulsive_stop - 21008) < 1e-9, str(st.impulsive_stop))
check("retest depth recorded in TICKS", st.retest_depth_ticks is not None)
outside = orb_tape()
outside.open[5] = 21025                      # opens ABOVE the range
st2 = OR.update(OR.build_range(outside, SPEC, 5), outside, SPEC)
check("a candle that OPENS OUTSIDE is not a break (definitional)",
      st2.state != OR.CONFIRMED_LONG, st2.state)
inside = orb_tape()
inside.close[6] = 21015                      # body closes back INSIDE
st3 = OR.update(OR.build_range(inside, SPEC, 5), inside, SPEC)
check("a body closing back inside DISARMS (it is not a graded retest)",
      st3.state != OR.CONFIRMED_LONG and st3.invalidation == OR.CLOSE_INSIDE,
      f"{st3.state}/{st3.invalidation}")
runaway = mk("1m", [(21005, 21020, 21000, 21010, 500)] * 5 +
                   [(21010, 21032, 21008, 21030, 900),
                    (21030, 21060, 21029, 21055, 900)])
st4 = OR.update(OR.build_range(runaway, SPEC, 5), runaway, SPEC)
check("a run to the projected target with no retest is a RUNAWAY (terminal)",
      st4.invalidation == OR.RUNAWAY, str(st4.invalidation))
stale = mk("1m", [(21005, 21020, 21000, 21010, 500)] * 5 +
                 [(21010, 21032, 21008, 21030, 900)] +
                 [(21030, 21033, 21026, 21031, 400)] * 14)
st5 = OR.update(OR.build_range(stale, SPEC, 5), stale, SPEC)
check("the retest window counts REAL BARS and times out (then re-arms)",
      st5.invalidation == OR.TIMEOUT and st5.state == OR.WAITING_FOR_BREAK,
      f"{st5.invalidation}/{st5.state}")
check("session break latches are maintained regardless of entry state",
      st5.broke_high is True)

print("\n=== 7. STRATEGIES ===")
TIERS = C.LEVEL_TIERS
liq = LQ.build(mk("1m", [(21005, 21020, 21000, 21010, 500)] * 30), SPEC.tick_size,
               TIERS, prior_high=21200, prior_low=20800)
t = orb_tape()
orb = OR.update(OR.build_range(t, SPEC, 5), t, SPEC)
ctx = {"spec": SPEC, "price": 21031.0, "now": T0, "c1": t, "orb": orb,
       "liquidity": liq, "structure": MS.analyze(t, SPEC.tick_size),
       "regime": EXPANSION, "conviction": 0.7, "session_phase": "NY_RTH",
       "killzone": "NY_AM", "flow": None, "vol": None, "profile": None}
sig = day_mode.dispatch(ctx)
check("D1 fires on a confirmed opening-range break+retest",
      sig is not None and sig.strategy == "D1_ORB_RETEST", str(sig))
check("D1 signal is geometrically valid", sig.validate(SPEC, SPEC.min_stop_ticks)[0])
opp = type("F", (), {"warm": True, "bias": "SELL", "cvd": -50,
                     "approximated": False, "divergence": "NO_DIV",
                     "divergence_strength": 0.0})()
check("D1 REFUSES a break made on opposing order flow",
      day_mode.OpeningDriveBreakRetest().evaluate({**ctx, "flow": opp}) is None)
sc = setup_scorer.score(sig, ctx, geometry_gated=True)
check("a confirmed geometric setup ALWAYS trades (never vetoed by the scorer)",
      sc.fires and sc.geometry_gated, sc.reason)
check("liquidity in the path DOWNGRADES A->B, it does not veto",
      setup_scorer._grade_geometry(
          Signal("D1", LONG, 21031, 21008, 21250), ctx).grade == "B")

sweep_rows = [(21005, 21020, 21000, 21010, 500)] * 20 + [
    (21010, 21210, 21008, 21190, 900),   # sweeps PDH 21200
    (21190, 21195, 21150, 21160, 900),
    (21160, 21170, 21150, 21165, 900)]
sw = mk("1m", sweep_rows)
liq2 = LQ.build(sw, SPEC.tick_size, TIERS, prior_high=21200, prior_low=20800)
ctx2 = {**ctx, "c1": sw, "price": 21165.0, "orb": None, "liquidity": liq2,
        "regime": BALANCED, "structure": MS.analyze(sw, SPEC.tick_size)}
s2 = day_mode.LiquiditySweepReversal().evaluate(ctx2)
check("D2 fires on a swept-and-reclaimed graded level",
      s2 is not None and s2.direction == SHORT, str(s2))
check("D2 carries the LEVEL TIER as scoring input",
      s2.level_tier >= 0.9, f"{s2.level_name} {s2.level_tier}")
check("D2 refuses to fade a committed trend",
      day_mode.LiquiditySweepReversal().evaluate({**ctx2, "regime": TRENDING_DOWN}) is None)

check("S1 refuses to act on APPROXIMATED order flow",
      scalp_mode.AbsorptionReversal().evaluate(
          {**ctx, "flow": type("F", (), {"warm": True, "approximated": True,
                                         "bias": "BUY", "cvd": 1,
                                         "bar_delta": [1]})()}) is None)

C.HEDGE_PORTFOLIO_VALUE = 250000.0
C.HEDGE_BETA = 1.0
C.HEDGE_TARGET_RATIO = 0.5
tgt = hedge_mode.required_contracts(250000, 1.0, 0.5, 21000, SPEC.multiplier)
check("hedge sizes off exposure, not off a stop",
      tgt.contracts == 3, f"{tgt.contracts} {tgt.reason}")
check("hedge reports the rounding RESIDUAL rather than hiding it",
      abs(tgt.residual_usd) > 0, str(tgt.residual_usd))
armed, why = hedge_mode.hedge_armed(TRENDING_DOWN, True)
check("conditional hedge arms on a risk-off regime", armed, why)
check("conditional hedge disarms when the regime normalises",
      not hedge_mode.hedge_armed(TRENDING_UP, True)[0])
check("always-on hedge ignores the regime entirely",
      hedge_mode.hedge_armed(TRENDING_UP, False)[0])
h = hedge_mode.BetaWeightedHedge().evaluate({**ctx, "price": 21000.0,
                                             "hedge_contracts_open": 0})
check("hedge emits an adjustment signal flagged is_hedge",
      h is not None and h.is_hedge, str(h))
check("hedge inside the drift band does NOT churn",
      hedge_mode.BetaWeightedHedge().evaluate(
          {**ctx, "price": 21000.0, "hedge_contracts_open": 3}) is None)
eff = hedge_mode.effectiveness([0.02, -0.03, 0.01, -0.02],
                               [0.005, -0.008, 0.002, -0.004])
check("hedge is scored on VARIANCE REDUCTION, not on P&L",
      eff is not None and eff > 0.5, str(eff))

print("\n=== 8. THE PAYOFF RULE, END TO END ===")
rm = RiskManager(SPEC, "DAY", 250.0, 3, 500.0, min_rrr=2.0)
bad = Signal("D2", LONG, 21400, 21375, 21420)
sized = rm.size(bad.entry, bad.stop, bad.target, "B", atr=60.0)
check("a good entry with a bad payoff is REFUSED before it can be sized",
      not sized.approved and sized.reason == "reward_does_not_pay_for_risk",
      f"rr={sized.rrr:.2f}")
ok = rm.size(good.entry, good.stop, good.target, "B", atr=60.0)
check("the same entry with a real target sizes normally",
      ok.approved and ok.contracts >= 1, ok.detail)

print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
sys.exit(1 if FAIL else 0)
