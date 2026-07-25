"""
futures_trader_v1/tests/test_foundation.py — v0.1
v0.1 — 2026-07-25 — Behavioural proof for the Phase-1 foundation.

Standard inherited from the working agreement: "compiles + behavioural proof
against real rows/tape, shown, before the file is presented." These are pure
functions over real dates and real contract specs — no broker, no network, no
environment. Run with: python3 tests/test_foundation.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import contract_registry as CR
from utils import sessions as S
from execution.margin_manager import MarginManager, AccountSnapshot
from execution.roll_manager import RollManager
from risk.risk_manager import RiskManager
from database.trade_logger import TradeLogger, TradeRecord

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


print("\n=== 1. CONTRACT REGISTRY: specs are arithmetic, not vibes ===")
mnq = CR.get_spec("MNQ")
es = CR.get_spec("ES")
zn = CR.get_spec("ZN")
check("MNQ 100 points = $200", abs(mnq.dollars(100.0) - 200.0) < 1e-9,
      f"got {mnq.dollars(100.0)}")
check("ES 1 point = $50", abs(es.dollars(1.0) - 50.0) < 1e-9, f"got {es.dollars(1.0)}")
check("ZN 1/32 (2 ticks) = $31.25", abs(zn.dollars(1 / 32) - 31.25) < 1e-6,
      f"got {zn.dollars(1/32)}")
check("MNQ tick rounding", abs(mnq.round_to_tick(21345.31) - 21345.25) < 1e-9,
      f"got {mnq.round_to_tick(21345.31)}")
check("37 roots registered", len(CR.ROOTS) == 37, f"got {len(CR.ROOTS)}")
check("every micro names its sibling",
      all(CR.SPECS[r].sibling for r in CR.MICRO_ROOTS))


print("\n=== 2. FRONT MONTH + EXPIRY RULES ===")
f, b = CR.front_and_back("MNQ", date(2026, 7, 25))
check("MNQ front on 2026-07-25 is Sep (U6)", f.code == "MNQU6", f"got {f.code}")
check("MNQ back is Dec (Z6)", b.code == "MNQZ6", f"got {b.code}")
check("MNQU6 last trade = 3rd Fri Sep 2026 (09-18)",
      f.last_trade == date(2026, 9, 18), f"got {f.last_trade}")
gcf, _ = CR.front_and_back("GC", date(2026, 7, 25))
check("GC front on 07-25 is Aug (FND = last BD of Jul)",
      gcf.code == "GCQ6" and gcf.last_trade == date(2026, 7, 31),
      f"got {gcf.code} {gcf.last_trade}")
clf, _ = CR.front_and_back("CL", date(2026, 7, 25))
check("CL front resolves to a monthly cycle", clf.root == "CL" and clf.last_trade >= date(2026, 7, 25),
      f"got {clf.code} {clf.last_trade}")

print("\n=== 3. ROLL STATE MACHINE ===")
a = CR.assess_roll("MNQ", date(2026, 8, 20))
check("far from expiry -> OFF_WINDOW", a.state == CR.OFF_WINDOW, a.state)
check("no roll far out", a.should_roll is False)
win_open = f.roll_window_open
a = CR.assess_roll("MNQ", win_open)
check(f"window opens {win_open} -> WINDOW_OPEN", a.state == CR.WINDOW_OPEN, a.state)
vh = [(date(2026, 9, 8), 900000, 400000),
      (date(2026, 9, 9), 700000, 800000),
      (date(2026, 9, 10), 500000, 1100000)]
a = CR.assess_roll("MNQ", date(2026, 9, 10), vh, confirm_sessions=2)
check("2 consecutive back-led sessions -> CROSSOVER",
      a.state == CR.CROSSOVER and a.should_roll, f"{a.state} streak={a.sessions_back_led}")
a1 = CR.assess_roll("MNQ", date(2026, 9, 10), vh[:2], confirm_sessions=2)
check("only 1 back-led session -> no roll yet",
      a1.state == CR.WINDOW_OPEN and not a1.should_roll, a1.state)
early = [(date(2026, 8, 3), 100, 900)]
a2 = CR.assess_roll("MNQ", date(2026, 8, 4), early, confirm_sessions=1)
check("back-month volume OUTSIDE the window is ignored",
      a2.state == CR.OFF_WINDOW and not a2.should_roll, a2.state)
gcd = CR.assess_roll("GC", date(2026, 7, 30))
check("physically-delivered GC forces off front before FND",
      gcd.state == CR.FORCED and gcd.should_roll, f"{gcd.state} {gcd.reason}")

print("\n=== 4. SESSION CLOCK ===")
from datetime import datetime
et = S.ET
check("2026-07-04 holiday observed 07-03 is a closure", S.is_holiday(date(2026, 7, 3)))
check("Sat is never a trading day", not S.is_trading_day(date(2026, 7, 25)))
mkt = datetime(2026, 7, 27, 10, 30, tzinfo=et)
check("Mon 10:30 ET market open", S.market_is_open(mkt))
check("Mon 10:30 ET is NY_RTH", S.session_phase(mkt) == "NY_RTH", S.session_phase(mkt))
check("Mon 10:30 ET is inside SILVER_BULLET", S.in_killzone(mkt, "SILVER_BULLET"))
brk = datetime(2026, 7, 27, 17, 30, tzinfo=et)
check("17:30 ET is the daily break", S.in_daily_break(brk) and not S.market_is_open(brk))
check("Mon 18:30 ET belongs to Tuesday's session",
      S.session_date(datetime(2026, 7, 27, 18, 30, tzinfo=et)) == date(2026, 7, 28),
      S.session_date(datetime(2026, 7, 27, 18, 30, tzinfo=et)))
sun = datetime(2026, 7, 26, 19, 0, tzinfo=et)
check("Sunday 19:00 ET is open (Globex week start)", S.market_is_open(sun))
check("MNQ RTH at 10:30", S.in_rth(mnq, mkt))
check("GC RTH ends 13:30 — 14:00 is outside",
      not S.in_rth(CR.get_spec("GC"), datetime(2026, 7, 27, 14, 0, tzinfo=et)))

print("\n=== 5. MODE-AWARE FLATTEN AUTHORITY ===")
late = datetime(2026, 7, 27, 15, 57, tzinfo=et)
check("DAY mode must be flat at 15:57", S.must_be_flat("DAY", mnq, late))
check("SCALP mode must be flat at 15:57", S.must_be_flat("SCALP", mnq, late))
check("SWING mode carries overnight", not S.must_be_flat("SWING", mnq, late))
check("HEDGE mode carries overnight", not S.must_be_flat("HEDGE", mnq, late))
ok, why = S.entries_allowed("DAY", mnq, datetime(2026, 7, 27, 15, 45, tzinfo=et))
check("DAY entries blocked inside the 30m cutoff", not ok, why)
ok, why = S.entries_allowed("SWING", mnq, datetime(2026, 7, 27, 3, 0, tzinfo=et),
                            enabled_sessions=["LONDON", "NY_RTH"])
check("SWING may enter in the London session", ok, why)
ok, why = S.entries_allowed("SWING", mnq, datetime(2026, 7, 27, 20, 0, tzinfo=et),
                            enabled_sessions=["LONDON", "NY_RTH"])
check("SWING blocked in a session it did not enable", not ok, why)

print("\n=== 6. SIZING + THE R:R GATE ===")
rm = RiskManager(mnq, "DAY", risk_per_trade=250.0, max_contracts=3,
                 daily_loss_limit=500.0, min_rrr=2.0, commission_rt=2.50)
r = rm.size(entry=21400.0, stop=21375.0, target=21475.0, grade="B", atr=60.0)
check("100-tick stop on MNQ = $50/contract -> 3 contracts (clamped)",
      r.approved and r.contracts == 3, r.detail)
check("planned R:R ~2.9 clears the 2.0 floor", r.approved and r.rrr > 2.0,
      f"rrr={r.rrr:.2f}")
r2 = rm.size(entry=21400.0, stop=21375.0, target=21425.0, grade="B", atr=60.0)
check("R:R 0.9 is REFUSED (defect F closed at birth)",
      not r2.approved and r2.reason == "reward_does_not_pay_for_risk",
      f"{r2.reason} rrr={r2.rrr:.2f}")
r3 = rm.size(entry=21400.0, stop=21399.0, target=21500.0, grade="B", atr=60.0)
check("a 4-tick stop is refused as inside the noise",
      not r3.approved and r3.reason == "stop_inside_noise", r3.reason)
r4 = rm.size(entry=21400.0, stop=21200.0, target=21900.0, grade="B", atr=40.0)
check("a stop beyond 2.5x ATR is refused",
      not r4.approved and r4.reason == "stop_exceeds_atr_ceiling", r4.reason)
big = RiskManager(es, "SWING", risk_per_trade=250.0, max_contracts=5,
                  daily_loss_limit=500.0, min_rrr=3.0)
r5 = big.size(entry=6500.0, stop=6480.0, target=6580.0, grade="B", atr=60.0)
check("ES 20pt stop = $1000/contract > $250 budget -> refused, stop NOT widened",
      not r5.approved and r5.reason == "cannot_afford_one_contract", r5.detail)
r6 = rm.size(entry=21400.0, stop=21375.0, target=21475.0, grade="B", atr=60.0,
             realized_pnl_today=-600.0)
check("daily loss halt blocks new entries",
      not r6.approved and r6.reason == "daily_loss_halt", r6.reason)
rm2 = RiskManager(mnq, "DAY", risk_per_trade=250.0, max_contracts=3,
                  daily_loss_limit=500.0, min_rrr=2.0, commission_rt=2.50)
r7 = rm2.size(entry=21400.0, stop=21375.0, target=21475.0, grade="A", atr=60.0,
             margin_capacity=1)
check("margin capacity clamps size", r7.approved and r7.contracts == 1, r7.detail)
check("the halt LATCHES for the session (a later flat P&L does not un-halt it)",
      not rm.size(entry=21400.0, stop=21375.0, target=21475.0, atr=60.0).approved)
plan = rm.scale_plan(3, 21400.0, 21375.0)
check("3-lot scale plan banks 2 at +1R", plan and plan[0][0] == 2 and
      abs(plan[0][1] - 21425.0) < 1e-9, str(plan))
check("1-lot has no scale plan (stated, not silently zero)", rm.scale_plan(1, 1, 0) == [])

print("\n=== 7. MARGIN: DAY vs OVERNIGHT RATES ===")
mm_day = MarginManager(mnq, "DAY", utilization_max=0.35, buffer_mult=1.25)
mm_day.apply_account(AccountSnapshot(net_liq=25000))
cap = mm_day.capacity()
check("DAY box sizes on the intraday rate",
      cap.rate_kind == "DAY" and cap.max_contracts >= 10, cap.reason)
mm_sw = MarginManager(mnq, "SWING", utilization_max=0.35, buffer_mult=1.25)
mm_sw.apply_account(AccountSnapshot(net_liq=25000))
cap_s = mm_sw.capacity()
check("SWING box sizes on the INITIAL rate (never the day discount)",
      cap_s.rate_kind == "INITIAL" and cap_s.max_contracts < cap.max_contracts,
      f"{cap_s.rate_kind} {cap_s.max_contracts} vs day {cap.max_contracts}")
og = mm_day.overnight_gate(contracts=8)
check("8 MNQ cannot be carried overnight on $25k -> gate refuses and states the keep size",
      not og.allowed and og.max_contracts < 8, og.reason)
d = mm_day.apply_broker_rates(initial=3100, maintenance=2800, day=800)
check("broker rates supersede seeds and the delta is reported",
      mm_day.rates.source == "broker" and abs(d["initial_pct"]) > 10,
      f"{mm_day.rates.source} {d}")
rep = mm_day.usage_report(2)
check("box publishes fleet-aggregatable margin usage",
      rep["overnight_requirement"] == 3100 * 2, str(rep))

print("\n=== 8. ROLL MANAGER: SPREAD, GRANULARITY, HALF-COMPLETE ===")
class _Fill:
    def __init__(self, ok=True, px=1.25):
        self.confirmed, self.fill_price, self.order_id = ok, px, "ord-1"

spread_calls = []
def _spread(**kw):
    spread_calls.append(kw)
    return _Fill(True, -3.75)

rmgr = RollManager(confirm_sessions=2, place_spread=_spread)
p = rmgr.plan("MNQ", date(2026, 9, 10), vh, open_contracts=2, direction="LONG")
check("crossover + open position -> calendar spread plan",
      p.kind == "calendar_spread", p.describe())
res = rmgr.execute(p)
check("spread roll completes on a confirmed fill",
      res.status == "COMPLETE" and spread_calls, res.message)
check("ledger prevents a second roll of the same contract",
      rmgr.ledger.already_rolled("MNQ", "MNQZ6"))

half = []
def _single(code, side, contracts):
    half.append((code, side))
    return _Fill(True) if side == "SELL" else _Fill(False)

rl = RollManager(confirm_sessions=2, prefer_spread=False, place_single=_single,
                 alert=lambda m: half.append(("ALERT", m)))
p2 = rl.plan("MNQ", date(2026, 9, 10), vh, open_contracts=2, direction="LONG")
res2 = rl.execute(p2)
check("legged roll that half-fills reports HALF_COMPLETE and pages",
      res2.status == "HALF_COMPLETE" and any(x[0] == "ALERT" for x in half),
      res2.message)
plans = rmgr.plan_many(["MNQ", "MES", "GC"], date(2026, 9, 10),
                       volumes={"MNQ": vh}, positions={"MNQ": (2, "LONG")})
check("per-contract granularity: one/subset/all", len(plans) == 3, str(len(plans)))

print("\n=== 9. TRADE LOG: no ghosts, R-native, mode-scoped ===")
import tempfile
tmp = tempfile.mkdtemp()
tl = TradeLogger(os.path.join(tmp, "t.db"), paper=True,
                 tick_value=mnq.tick_value, tick_size=mnq.tick_size)
rec = TradeRecord(trade_id="T1", root="MNQ", contract_code="MNQU6", mode="DAY",
                  strategy="ORB_RT", direction="LONG", contracts=2,
                  entry_price=21400.0, stop_price=21375.0, target_price=21475.0,
                  stop_ticks=100, risk_dollars=100.0, planned_rrr=3.0,
                  session_date="2026-07-27")
check("unconfirmed entry is REFUSED, not flagged",
      tl.open_trade(rec, confirmed_fill=False) is None)
check("confirmed entry is logged", tl.open_trade(rec, confirmed_fill=True) is not None)
tl.update_excursion("T1", 21460.0)
tl.update_excursion("T1", 21390.0)
open_rows = tl.get_open_trades()
check("MFE/MAE recorded in ticks AND R",
      open_rows[0]["max_favorable_ticks"] == 240 and open_rows[0]["max_adverse_r"] < 0,
      str(dict(open_rows[0])["max_favorable_ticks"]))
pnl = tl.close_trade("T1", 21425.0, "scale_1R", contracts_closed=1)
check("partial close books P&L and leaves the runner open",
      abs(pnl - 50.0) < 1e-6 and tl.get_open_trades()[0]["contracts_open"] == 1,
      f"pnl={pnl}")
tl.close_trade("T1", 21475.0, "target")
check("full close moves the row to CLOSED", tl.get_open_trades() == [])
e = tl.expectancy()
check("expectancy report carries n/win%/avg win R/avg loss R together",
      set(e) == {"n", "win_rate", "avg_win_r", "avg_loss_r", "expectancy_r"}, str(e))

print("\n=== 10. ELIGIBILITY POLICY + CAPACITY ===")
os.environ["FT_SYMBOL"] = "MNQ"
import config as C
from risk.eligibility import mode_permitted, box_viable
from risk.capacity import compute, tick_chart, resolve_equity
EQ = 25000.0
check("SI is EXCLUDED (X) in every mode at $25k",
      all(mode_permitted(CR.get_spec("SI"), m, EQ).excluded
          for m in ("SCALP", "DAY", "SWING", "HEDGE")))
check("SI exclusion names the micro sibling",
      "SIL" in mode_permitted(CR.get_spec("SI"), "DAY", EQ).reason,
      mode_permitted(CR.get_spec("SI"), "DAY", EQ).reason)
check("full-size NQ is X for SWING (overnight carry ceiling)",
      mode_permitted(CR.get_spec("NQ"), "SWING", EQ).excluded)
check("full-size NQ is NOT excluded for DAY",
      not mode_permitted(CR.get_spec("NQ"), "DAY", EQ).excluded)
check("MNQ is permitted in every mode",
      not any(mode_permitted(mnq, m, EQ).excluded
              for m in ("SCALP", "DAY", "SWING", "HEDGE")))
ok, why = box_viable("SI", EQ)
check("box_viable says an SI box should not exist", not ok, why)
ok, why = box_viable("MNQ", EQ)
check("box_viable approves an MNQ box", ok, why)
check("X and 0 are different: excluded vs unaffordable",
      mode_permitted(CR.get_spec("PA"), "DAY", EQ).excluded and
      not mode_permitted(CR.get_spec("MES"), "DAY", EQ).excluded)
rep = compute(symbol="MNQ")
check("capacity report is scoped to one symbol", rep.spec.root == "MNQ")
check("capacity resolves the front month", rep.contract_code.startswith("MNQ"),
      rep.contract_code)
check("paper equity is a firm $25,000",
      abs(rep.equity.value - 25000.0) < 1e-6, rep.equity.label())
from execution.margin_manager import AccountSnapshot as _AS
_funded = _AS(net_liq=487_000.0, as_of=S.now_et(), source="broker")
check("a funded broker balance does NOT leak into paper sizing",
      abs(compute(broker=_funded, symbol="MNQ").equity.value - 25000.0) < 1e-6,
      compute(broker=_funded, symbol="MNQ").equity.label())
check("paper equity label marks itself fixed",
      "PAPER — fixed" in rep.equity.label(), rep.equity.label())
check("risk budget is 1% of equity", abs(rep.risk_budget - 250.0) < 1e-6,
      str(rep.risk_budget))
row1 = rep.ladder[0]
check("1-lot MNQ affords a 495-tick stop at $250",
      abs(row1.max_stop_ticks - 495.0) < 1.0, str(row1.max_stop_ticks))
check("stop ladder halves as lots double",
      abs(rep.ladder[1].max_stop_ticks * 2 - rep.ladder[0].max_stop_ticks) < 6,
      f"{rep.ladder[0].max_stop_ticks} vs {rep.ladder[1].max_stop_ticks}")
check("overnight ladder is gap-adjusted (tighter than day)",
      rep.overnight_ladder[0].max_stop_ticks < rep.ladder[0].max_stop_ticks)
chart = tick_chart(rep)
check("tick chart renders inside 64 cols for mobile",
      max(len(l) for l in chart.splitlines()) <= 64,
      f"widest {max(len(l) for l in chart.splitlines())}")
check("tick chart carries the equity source", "PAPER" in chart or "broker" in chart)
si_chart = tick_chart(compute(symbol="SI"))
check("an all-X symbol renders the 'box should not exist' verdict",
      "SHOULD NOT EXIST" in si_chart)
check("an all-X symbol suppresses the nonsense stop ladder",
      "suppressed" in si_chart)

print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
sys.exit(1 if FAIL else 0)
