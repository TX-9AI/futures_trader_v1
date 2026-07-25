"""
futures_trader_v1/tests/test_control.py — v0.2
v0.2 — 2026-07-25 — roll-control section: one/subset/all targeting, confirm
        required, and a half-complete roll halting the batch.
v0.1 — 2026-07-25 — Behavioural proof for the Phase-5 control plane.
    python3 tests/test_control.py
"""
import json, os, sys, tempfile
from datetime import date, datetime, time as dtime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp()
os.environ.update(FT_SYMBOL="MNQ", FT_MODE="DAY", FTC_MOCK="1",
                  FTC_BASE_DIR=TMP, FTC_ACCOUNT_EQUITY="25000",
                  FTC_FLEET="MNQ:DAY,MES:DAY,MYM:DAY,M6E:SWING,MGC:SWING,MES2:HEDGE")

from control import ec2ops, fleet_config as FC
from control.fleet import Fleet, RunResult
from control.orchestrator import Orchestrator, wake_time_for
from control.margin_governor import BoxUsage, MarginGovernor, from_reports
from control.harvest import Harvester
from control.eod_conductor import Conductor
from utils import sessions as S

PASS = FAIL = 0
def check(n, c, d=""):
    global PASS, FAIL
    if c: PASS += 1; print(f"  PASS  {n}")
    else: FAIL += 1; print(f"  FAIL  {n}  {d}")

def mk_fleet(runner=None):
    return Fleet(backend=ec2ops.MockBackend(FC.FLEET), runner=runner)

print("\n=== 1. DISCOVERY — state-driven, not memory-driven ===")
f = mk_fleet()
check("all configured boxes discovered", len(f.instances()) == 6, str(len(f.instances())))
check("boxes carry their MODE (control must know it)",
      {i.mode for i in f.instances()} == {"DAY", "SWING", "HEDGE"},
      str({i.mode for i in f.instances()}))
check("nothing running before a wake", f.running() == [])
f.start(f.instances()[:2])
check("start moves boxes to running", len(f.running()) == 2)
check("listing is sorted and shows state",
      f.listing()[0][0] < f.listing()[-1][0])

print("\n=== 2. WAKE — per-box, session-aware ===")
check("an RTH box wakes 45m before 09:30",
      wake_time_for("DAY", ["NY_RTH"]) == dtime(8, 45),
      str(wake_time_for("DAY", ["NY_RTH"])))
check("a London box wakes 45m before 02:00",
      wake_time_for("SCALP", ["LONDON"]) == dtime(1, 15),
      str(wake_time_for("SCALP", ["LONDON"])))
check("a box wakes for its EARLIEST enabled session",
      wake_time_for("SWING", ["NY_RTH", "LONDON"]) == dtime(1, 15))
check("an Asia box's wake wraps into the prior evening",
      wake_time_for("SWING", ["ASIA"]) == dtime(17, 15),
      str(wake_time_for("SWING", ["ASIA"])))

f2 = mk_fleet()
orch = Orchestrator(f2, sessions_by_box={"MNQ-DAY": ["NY_RTH"],
                                         "MES-DAY": ["NY_RTH"],
                                         "MYM-DAY": ["LONDON"]})
mon = datetime(2026, 7, 27, 9, 0, tzinfo=S.ET)
plan = orch.plan_wake(mon)
names = {i.box for i in plan.to_start}
check("RTH boxes are due at 09:00", "MNQ-DAY" in names and "MES-DAY" in names, str(names))
early = orch.plan_wake(datetime(2026, 7, 27, 3, 0, tzinfo=S.ET))
check("at 03:00 the London box is due but the RTH boxes are not",
      "MYM-DAY" in {i.box for i in early.to_start} and
      "MNQ-DAY" in {b for b, _ in early.not_yet},
      f"start={[i.box for i in early.to_start]} not_yet={[b for b,_ in early.not_yet]}")
sat = orch.plan_wake(datetime(2026, 7, 25, 9, 0, tzinfo=S.ET))
check("no wake on a non-trading day", sat.to_start == [], sat.reason)
orch.wake(mon)
check("wake actually starts the instances", len(f2.running()) > 0)

print("\n=== 3. STOP — overnight boxes are PROTECTED by construction ===")
f3 = mk_fleet()
f3.start(f3.instances())
sp = f3  # all running
o3 = Orchestrator(f3)
plan = o3.plan_stop()
stopped = {i.box for i in plan.to_stop}
check("intraday boxes are stopped",
      {"MNQ-DAY", "MES-DAY", "MYM-DAY"} <= stopped, str(stopped))
check("SWING boxes are NOT stopped", "M6E-SWING" not in stopped)
check("HEDGE boxes are NOT stopped", "MES2-HEDGE" not in stopped)
check("protection is reported, not silent",
      "M6E-SWING" in plan.protected and "PROTECTED" in plan.reason, plan.reason)
o3.stop()
still = {i.box for i in f3.running()}
check("after the sweep, only overnight boxes remain up",
      still == {"M6E-SWING", "MGC-SWING", "MES2-HEDGE"}, str(still))
f3.start(f3.instances())
forced = o3.plan_stop(include_overnight=True)
check("an explicit operator override CAN stop everything",
      len(forced.to_stop) == 6 and forced.protected == [], str(len(forced.to_stop)))

print("\n=== 4. MARGIN GOVERNOR — one account, one bet ===")
g = MarginGovernor(equity=25000, fleet_max=0.60, overnight_max=0.40,
                   group_max=0.30, state_dir=os.path.join(TMP, "state"))
light = [BoxUsage("MNQ-DAY", "MNQ", "DAY", 1, "LONG", "DAY", 844, 844, 3375)]
v = g.assess(light)
check("a light fleet is OK", v.status == "OK", v.headline())
heavy = [BoxUsage(f"B{i}", "MNQ", "DAY", 3, "LONG", "DAY", 844, 2532, 10125)
         for i in range(8)]
v2 = g.assess(heavy)
check("individually-compliant boxes can BREACH the account",
      v2.status == "BREACH", v2.headline())
check("the breach names the fleet cap", any("fleet margin" in x for x in v2.findings),
      str(v2.findings))
corr = [BoxUsage("MNQ-DAY", "MNQ", "DAY", 2, "LONG", "DAY", 844, 1688, 6750),
        BoxUsage("MES-DAY", "MES", "DAY", 2, "LONG", "DAY", 531, 1062, 4250),
        BoxUsage("MYM-DAY", "MYM", "DAY", 3, "LONG", "DAY", 344, 5000, 4125)]
v3 = g.assess(corr)
grp = v3.groups["equity_index"]
check("correlated roots aggregate into ONE group",
      grp.gross_contracts == 7 and set(grp.boxes) == {"MNQ-DAY", "MES-DAY", "MYM-DAY"},
      str(grp))
check("a one-way group is identified as one bet, not three positions",
      grp.one_way is True)
check("group cap breach explains the shape",
      any("ONE bet" in x for x in v3.findings), str(v3.findings))
mixed = [BoxUsage("A", "MNQ", "DAY", 2, "LONG", "DAY", 844, 4000, 0),
         BoxUsage("B", "MES", "DAY", 2, "SHORT", "DAY", 531, 4000, 0)]
check("a hedged group is NOT flagged one-way",
      g.assess(mixed).groups["equity_index"].one_way is False)
ovn = [BoxUsage("S1", "M6E", "SWING", 3, "LONG", "INITIAL", 290, 870, 6000),
       BoxUsage("S2", "MGC", "SWING", 3, "LONG", "INITIAL", 1350, 4050, 5000)]
v4 = g.assess(ovn)
check("the OVERNIGHT cap is checked separately from intraday",
      any("OVERNIGHT" in x for x in v4.findings), str(v4.findings))
v5 = g.assess(heavy + [BoxUsage("FLAT-1", "MGC", "DAY", 0, "FLAT")])
check("a breach tells FLAT boxes to stand down (stop new risk, unwind nothing)",
      "FLAT-1" in v5.stand_down, str(v5.stand_down))
p = g.publish(v5)
check("the constraint is published for boxes to read",
      p and json.load(open(p))["status"] == "BREACH")
check("governor never places or closes orders",
      not any(hasattr(g, m) for m in ("close", "liquidate", "flatten", "place")))
check("no equity known -> WARN, not a false OK",
      MarginGovernor(equity=0).assess(light).status == "WARN")

print("\n=== 5. HARVEST — order flow first, warn-never-stop ===")
f4 = mk_fleet(); f4.start(f4.instances())
copied = []
def ok_copy(inst, remote, local):
    copied.append((inst.box, remote))
    open(local, "w").write("x")
    return True
h = Harvester(f4, base_dir=TMP, copier=ok_copy)
r = h.run(date(2026, 7, 27))
check("order flow is pulled FIRST for each box",
      copied[0][1] == "data/feed_store.db", str(copied[:2]))
check("every running box harvested", len(r.pulled) == 6, str(len(r.pulled)))
def flow_fails(inst, remote, local):
    if remote == "data/feed_store.db":
        return False
    open(local, "w").write("x"); return True
r2 = Harvester(f4, base_dir=TMP, copier=flow_fails).run(date(2026, 7, 27))
check("a failed order-flow pull is escalated as UNRECOVERABLE",
      any("unrecoverable" in w for w in r2.warnings), str(r2.warnings[:1]))
check("but the run continues and still pulls the rest",
      len(r2.pulled) == 6, str(len(r2.pulled)))

print("\n=== 6. EOD CHAIN — order is load-bearing ===")
f5 = mk_fleet(); f5.start(f5.instances())
sent = []
class FakeFleet(Fleet):
    def margin_usage(self):
        return [{"root": "MNQ", "mode": "DAY", "contracts": 1, "direction": "LONG",
                 "rate_kind": "DAY", "per_contract": 844, "margin_used": 844,
                 "overnight_requirement": 3375, "box": "MNQ-DAY"}]
ff = FakeFleet(backend=f5.backend, runner=lambda i, c: RunResult(i.box, True, "{}"))
ff.start(ff.instances())
cond = Conductor(ff, MarginGovernor(equity=25000, state_dir=os.path.join(TMP, "st")),
                 Harvester(ff, base_dir=TMP, copier=ok_copy),
                 Orchestrator(ff), notifier=lambda t: sent.append(t) or True)
res = cond.run(date(2026, 7, 27))
names = [p.name for p in res.phases]
check("phase order: margin -> harvest -> stop -> consolidate -> expectancy -> roll",
      names == ["margin", "harvest", "stop", "consolidate", "expectancy", "roll"],
      str(names))
check("HARVEST runs BEFORE STOP (data would be unreachable otherwise)",
      names.index("harvest") < names.index("stop"))
check("the chain notifies once with a summary", len(sent) == 1)
check("overnight boxes survived the chain",
      {i.box for i in ff.running()} == {"M6E-SWING", "MGC-SWING", "MES2-HEDGE"},
      str({i.box for i in ff.running()}))
class Boom(Harvester):
    def run(self, day=None, instances=None):
        raise RuntimeError("scp exploded")
cond2 = Conductor(ff, MarginGovernor(equity=25000, state_dir=os.path.join(TMP, "st2")),
                  Boom(ff, base_dir=TMP), Orchestrator(ff),
                  notifier=lambda t: True)
res2 = cond2.run(date(2026, 7, 27))
check("a phase that raises does NOT stop the chain",
      len(res2.phases) == 6 and "harvest" in res2.failed, str(res2.failed))
check("later phases still ran after the failure",
      "roll" in [p.name for p in res2.phases])

print("\n=== 7. CONSOLIDATION + EXPECTANCY ===")
from database.trade_logger import TradeLogger, TradeRecord
day = date(2026, 7, 27)
tdir = os.path.join(TMP, "trades", day.isoformat())
os.makedirs(tdir, exist_ok=True)
tl = TradeLogger(os.path.join(tdir, "MNQ-DAY_trades.db"), True, 0.50, 0.25)
for i, (r_mult, strat) in enumerate([(2.0, "D1"), (-1.0, "D1"), (3.0, "D1"),
                                     (-1.0, "D2"), (-1.0, "D2"), (0.4, "D2"),
                                     (0.4, "D2")]):
    tid = f"T{i}"
    tl.open_trade(TradeRecord(trade_id=tid, root="MNQ", contract_code="MNQU6",
                              mode="DAY", strategy=strat, direction="LONG",
                              contracts=1, entry_price=21000, stop_price=20990,
                              target_price=21030, stop_ticks=40,
                              risk_dollars=20.0, session_date=day.isoformat()), True)
    tl.close_trade(tid, 21000 + r_mult * 10, "test")
FC.TRADES_DIR = os.path.join(TMP, "trades")
FC.REPORTS_DIR = os.path.join(TMP, "reports")
c = Conductor(ff, notifier=lambda t: True)
pc = c._consolidate(day)
check("consolidation dedupes by trade_id and buckets by SESSION DATE",
      pc.ok and "7 trade(s)" in pc.headline, pc.headline)
pe = c._expectancy(day)
rep = pe.detail["report"]
check("expectancy computed per strategy", set(rep) == {"D1", "D2"}, str(set(rep)))
check("D2 is a WINNING-RATE-POSITIVE, EXPECTANCY-NEGATIVE book — exactly the "
      "shape that hid in the options data",
      rep["D2"]["win_rate"] == 0.5 and rep["D2"]["expectancy_r"] < 0,
      str(rep["D2"]))
check("the headline states expectancy, never win rate alone",
      "E=" in pe.headline, pe.headline)

print("\n=== 8. ROLL CONTROL — one / subset / all ===")
from control.roll_control import RollControl, root_of
check("box names with a disambiguating digit resolve to the real root",
      root_of("MES2") == "MES" and root_of("MNQ") == "MNQ")
f6 = mk_fleet(runner=lambda i, c: RunResult(i.box, True, "COMPLETE"))
f6.start(f6.instances())
rc = RollControl(f6, confirm=lambda m: True)
check("'all' targets every running box", len(rc.plan("all")) == 6)
check("a single symbol targets one box", len(rc.plan("MNQ")) == 1)
check("a comma-separated subset targets exactly those",
      len(rc.plan("MNQ,MES,MGC")) == 3)
check("an unknown selection targets nothing rather than everything",
      rc.plan("NOPE") == [])
declined = RollControl(f6, confirm=lambda m: False).execute("all")
check("execute REQUIRES confirmation and cancels without it",
      declined.executed == [] and "cancelled" in declined.warnings[0])
halt_calls = []
f7 = mk_fleet(runner=lambda i, c: RunResult(i.box, True, "HALF_COMPLETE front closed"))
f7.start(f7.instances())
rc2 = RollControl(f7, confirm=lambda m: True, alert=halt_calls.append)
# 2026-09-17 is one business day before MNQU6's last trade, so the roll is
# FORCED and a plan actually exists to execute. On 09-10 the window is merely
# open and every plan is "no roll needed" — nothing to halt on.
rep = rc2.execute("MNQ", on=date(2026, 9, 17))
check("a HALF-COMPLETE roll HALTS the batch instead of rolling on",
      rep.halted_on != "" and len(rep.executed) == 1, rep.headline())
ok_run = mk_fleet(runner=lambda i, c: RunResult(i.box, True, "COMPLETE"))
ok_run.start(ok_run.instances())
clean = RollControl(ok_run, confirm=lambda m: True).execute("MNQ", on=date(2026, 9, 17))
check("a clean roll does not halt anything", clean.halted_on == "", clean.headline())
check("and it pages", any("HALF-COMPLETE" in a for a in halt_calls))

print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
sys.exit(1 if FAIL else 0)
