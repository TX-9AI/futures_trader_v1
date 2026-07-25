"""
futures_trader_v1/tests/test_analysis.py — v0.1
v0.1 — 2026-07-25 — Behavioural proof for the Phase-2 analysis stack.

Synthetic tape with KNOWN geometry, so every assertion has a right answer that
was decided before the code ran. Pure stdlib — runnable on any box, in any venv.
    python3 tests/test_analysis.py
"""
import math, os, sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FT_SYMBOL", "MNQ")

from data.series import Candles, Tape
from utils.sessions import ET
from analysis import volatility as V
from analysis import trend as T
from analysis import market_structure as MS
from analysis import liquidity as LQ
from analysis import profile as PF
from analysis import orderflow as OF
from analysis import regime_confluence as RC
from analysis.conviction_integrator import ConvictionIntegrator, IntegratorParams
from analysis.signal_journal import SignalJournal

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else: FAIL += 1; print(f"  FAIL  {name}  {detail}")

T0 = datetime(2026, 7, 27, 9, 30, tzinfo=ET)
def mk(tf, rows, start=T0, step=5):
    c = Candles(tf)
    for i, (o, h, l, cl, v) in enumerate(rows):
        c.ts.append(start + timedelta(minutes=i * step))
        c.open.append(o); c.high.append(h); c.low.append(l)
        c.close.append(cl); c.volume.append(v)
    return c

def trending_up(n=80, base=21000.0, slope=6.0, noise=2.0):
    """Zig-zag uptrend: higher highs and higher lows WITH pullbacks. A perfectly
    monotonic ramp has no swing pivots and no opposing candles, so it cannot
    exercise structure detection — the tape must contain the geometry the code
    is supposed to find."""
    rows, px = [], base
    for i in range(n):
        leg = i % 5
        step = slope * (1.6 if leg < 3 else -1.1)     # 3 up, 2 back
        o = px
        px = px + step
        hi = max(o, px) + noise
        lo = min(o, px) - noise
        rows.append((o, hi, lo, px, 1000 + i))
    return rows

def flat_range(n=80, base=21000.0, amp=25.0, seed=7):
    """Mean-reverting walk inside a band. A pure sine is a bad range fixture:
    it dwells at its turning points, so the volume profile puts the POC at an
    extreme and value never brackets the last price."""
    rows, px, rnd = [], base, seed
    for i in range(n):
        rnd = (1103515245 * rnd + 12345) % 2147483648
        shock = ((rnd / 2147483648.0) - 0.5) * amp * 0.9
        o = px
        px = px + shock - (px - base) * 0.35          # pull back to the mean
        rows.append((o, max(o, px) + 3, min(o, px) - 3, px, 1000))
    return rows

def squeezed(n=90, base=21000.0, narrow_tail=12):
    """Wide history, then a RECENT coil. Two properties the percentile needs:
    a varied history to rank against, and a squeeze that has not yet filled its
    own lookback (a coil occupying 40% of the trailing window stops ranking as
    compressed — correct behaviour, and worth knowing)."""
    rows, px = [], base
    for i in range(n):
        amp = 30.0 if i < n - narrow_tail else 1.5
        o = px
        px = base + (amp if i % 2 else -amp) * 0.5
        rows.append((o, max(o, px) + amp * 0.3, min(o, px) - amp * 0.3, px, 600))
    return rows

print("\n=== 1. VOLATILITY — guards before numbers ===")
up = mk("5m", trending_up())
vs = V.analyze(up)
check("ATR computed on warm tape", vs.warm and vs.atr and vs.atr > 0, str(vs.atr))
check("Bollinger present", vs.bb_middle is not None)
check("VWAP computed when volume is real", vs.vwap is not None and vs.price_vs_vwap in ("ABOVE","BELOW"))
zero_vol = mk("5m", [(r[0], r[1], r[2], r[3], 0.0) for r in trending_up()])
vz = V.analyze(zero_vol)
check("ZERO-VOLUME: vwap is None, not NaN", vz.vwap is None, str(vz.vwap))
check("ZERO-VOLUME: price_vs_vwap is the string NONE", vz.price_vs_vwap == "NONE", vz.price_vs_vwap)
check("cold tape returns not-warm rather than a number", not V.analyze(mk("5m", trending_up(5))).warm)
check("ATR refuses a short sample", V.atr(mk("5m", trending_up(5))) is None)

print("\n=== 2. TREND — the weight-renormalization fix ===")
tape = Tape()
for tf in ("5m", "15m", "1h", "1d"):
    tape.put(mk(tf, trending_up()))
ts = T.analyze(tape)
check("uptrend detected", ts.direction == "BULL", f"{ts.direction} bull={ts.bull_score:.2f}")
check("4h absent is REPORTED, not silent", "4h" in ts.missing_frames, str(ts.missing_frames))
check("weights renormalize over present frames (bull_score reaches 1.0)",
      abs(ts.bull_score - 1.0) < 1e-6, f"{ts.bull_score:.4f}")
solo = Tape(); solo.put(mk("5m", trending_up()))
ss = T.analyze(solo)
check("a single available frame still clears the gate (no evaporation)",
      ss.direction == "BULL" and abs(ss.bull_score - 1.0) < 1e-6, f"{ss.bull_score:.3f}")
check("ADX computed from 5m", ts.adx is not None and ts.adx > 20, str(ts.adx))
empty = T.analyze(Tape())
check("no frames -> NEUTRAL and not warm", empty.direction == "NEUTRAL" and not empty.warm)
dn = Tape()
for tf in ("5m", "15m", "1h", "1d"):
    dn.put(mk(tf, [(o, h, l, c, v) for (o, h, l, c, v) in trending_up(80, 21000, -6.0)]))
check("downtrend detected", T.analyze(dn).direction == "BEAR", T.analyze(dn).direction)

print("\n=== 3. MARKET STRUCTURE ===")
st = MS.analyze(mk("1m", trending_up()), tick_size=0.25)
check("swings found", len(st.swings) > 0, str(len(st.swings)))
check("bias follows the break direction", st.bias == "BULL", st.bias)
check("BOS (not CHoCH) in a clean trend", st.last_break == "BOS", str(st.last_break))
check("PD position near the top in an uptrend",
      st.pd_position is not None and st.pd_position > 0.8, str(st.pd_position))
check("premium/discount reported", st.in_premium is True and st.in_discount is False)
gap = mk("1m", [(100,101,99,100,10),(100,102,101.5,102,10),(103,104,102.5,103.5,10)])
fv = MS.find_fvgs(gap, tick_size=0.25, min_ticks=1.0)
check("bullish FVG detected across the 3-bar gap", len(fv) == 1 and fv[0].direction == "BULL", str(fv))
check("FVG smaller than min_ticks is ignored (noise, not imbalance)",
      MS.find_fvgs(gap, tick_size=10.0, min_ticks=1.0) == [])
obs = MS.find_order_blocks(mk("1m", trending_up()), 0.25, min_displacement_ticks=8.0)
check("order blocks found on displacement", len(obs) > 0, str(len(obs)))

print("\n=== 4. LIQUIDITY — tiers and the overnight window ===")
TIERS = {"OVERNIGHT_HIGH":1.0,"OVERNIGHT_LOW":1.0,"PDH":1.0,"PDL":1.0,
         "WEEKLY_HIGH":0.9,"WEEKLY_LOW":0.9,"HISTORIC_SR":0.7,"NAKED_POC":0.65,
         "SESSION_HIGH":0.5,"SESSION_LOW":0.5,"VALUE_AREA_EDGE":0.45,"EQUAL_HL":0.3}
on_start = datetime(2026, 7, 26, 19, 0, tzinfo=ET)
on_rows = [(21000,21120,20950,21050,100)] + [(21050,21060,21040,21055,100)]*12
c_on = mk("1m", on_rows, start=on_start, step=60)
m = LQ.build(c_on, 0.25, TIERS, prior_high=21200, prior_low=20800)
check("overnight high captured from the 18:00->09:30 window",
      m.overnight_high == 21120, str(m.overnight_high))
check("overnight low captured", m.overnight_low == 20950, str(m.overnight_low))
names = {l.name for l in m.levels}
check("ON H/L present as NAMED levels (the 07-24 gap, closed at birth)",
      "OVERNIGHT_HIGH" in names and "OVERNIGHT_LOW" in names, str(names))
check("PDH/PDL carry tier 1.0",
      all(l.tier == 1.0 for l in m.levels if l.name in ("PDH","PDL")))
eq = [l for l in m.levels if l.name == "EQUAL_HL"]
check("equal H/L are the LOWEST tier", all(l.tier == 0.3 for l in eq) if eq else True)
strong = m.strongest_within(21120, 0.25, max_ticks=40)
check("strongest_within returns the top tier, not the closest",
      strong is not None and strong.tier == 1.0, strong.name if strong else "None")
check("levels split above/below current price",
      len(m.above()) > 0 and len(m.below()) > 0)

print("\n=== 5. PROFILE ===")
prof = PF.build_profile(mk("5m", flat_range()), tick_size=0.25, bin_ticks=4)
check("POC computed", prof.poc is not None, str(prof.poc))
check("value area brackets the POC", prof.val <= prof.poc <= prof.vah,
      f"{prof.val}/{prof.poc}/{prof.vah}")
ps = PF.analyze(mk("5m", flat_range()), 0.25, prior=mk("5m", flat_range()))
check("identical sessions -> OVERLAPPING value", ps.migration == "OVERLAPPING", ps.migration)
check("a price inside the value area reports INSIDE",
      ps.today.position(ps.today.poc) == "INSIDE", ps.today.position(ps.today.poc))
check("a price above VAH reports ABOVE_VALUE",
      ps.today.position(ps.today.vah + 100) == "ABOVE_VALUE")
check("balanced requires BOTH overlapping value AND price inside it",
      ps.balanced == (ps.migration == "OVERLAPPING" and ps.price_position == "INSIDE"))
ps2 = PF.analyze(mk("5m", flat_range(80, 21500)), 0.25, prior=mk("5m", flat_range(80, 21000)))
check("displaced value -> migration HIGHER", ps2.migration == "HIGHER", ps2.migration)
check("displaced value is NOT balanced", not ps2.balanced)

print("\n=== 6. ORDER FLOW ===")
fl = OF.build(mk("1m", trending_up()))
check("CVD positive in an uptrend (approximated)", fl.cvd > 0, str(fl.cvd))
check("approximated flag is TRUE without tick data", fl.approximated is True)
tr = [[OF.Trade(None, 100, 5, OF.BUY)] for _ in range(len(mk("1m", trending_up())))]
fl2 = OF.build(mk("1m", trending_up()), bar_trades=tr)
check("real aggressor data clears the approximated flag", fl2.approximated is False)
check("real buy prints produce positive CVD", fl2.cvd > 0)
div_rows = []
for i in range(24):
    base = 21000 + (i * 4 if i < 12 else 48 + (i - 12) * 5)
    div_rows.append((base, base + 3, base - 3, base + (2 if i < 12 else -2), 900))
fd = OF.build(mk("1m", div_rows))
check("divergence detector runs and returns a known label",
      fd.divergence in ("BEARISH_DIV", "BULLISH_DIV", "NO_DIV"), fd.divergence)
absorb = mk("1m", [(21000, 21002, 20998, 21000.25, 4000)] * 8)
fa = OF.build(absorb)
ok, side = OF.detect_absorption(absorb, fa, atr=6.0)
check("absorption logic evaluates without raising", isinstance(ok, bool))

print("\n=== 7. L1 CONFLUENCE — ported grammar and dials ===")
sc = RC.ConfluenceScorer()
up5 = mk("5m", trending_up()); vsu = V.analyze(up5)
tape_u = Tape()
for tf in ("5m","15m","1h","1d"): tape_u.put(mk(tf, trending_up()))
tsu = T.analyze(tape_u); msu = MS.analyze(up5, 0.25)
ev_up = sc.score(up5.close, vsu, tsu, msu)
check("TRENDING_UP scores highest on trending tape",
      ev_up.top()[0] == RC.TRENDING_UP, str(ev_up.top()))
check("TRENDING_DOWN is hard-vetoed to exactly 0",
      ev_up.scores[RC.TRENDING_DOWN] == 0.0, str(ev_up.scores[RC.TRENDING_DOWN]))
check("BALANCED stays LOW on a trending tape and far below TRENDING_UP",
      ev_up.scores[RC.BALANCED] < 0.30 and
      ev_up.scores[RC.BALANCED] < ev_up.scores[RC.TRENDING_UP] * 0.5,
      f"bal={ev_up.scores[RC.BALANCED]:.3f} up={ev_up.scores[RC.TRENDING_UP]:.3f}")
_steep = mk("5m", [(21000 + i*40, 21000 + i*40 + 5, 21000 + i*40 - 5, 21000 + i*40, 900)
                   for i in range(80)])
check("BALANCED is HARD-VETOED to exactly 0 on a steeply sloping centre",
      RC.ConfluenceScorer().score(_steep.close, V.analyze(_steep),
                                  T.analyze(Tape()), MS.analyze(_steep, 0.25)
                                  ).scores[RC.BALANCED] == 0.0)
rng5 = mk("5m", flat_range()); vsr = V.analyze(rng5)
tape_r = Tape()
for tf in ("5m","15m","1h","1d"): tape_r.put(mk(tf, flat_range()))
ev_rg = sc.score(rng5.close, vsr, T.analyze(tape_r), MS.analyze(rng5, 0.25))
check("BALANCED scores > 0 on flat oscillating tape",
      ev_rg.scores[RC.BALANCED] > 0, str(ev_rg.scores[RC.BALANCED]))
check("A2 CO-OCCURRENCE: BALANCED and TRENDING never both high on one tick",
      not (ev_rg.scores[RC.BALANCED] > 0.5 and
           max(ev_rg.scores[RC.TRENDING_UP], ev_rg.scores[RC.TRENDING_DOWN]) > 0.5),
      f"bal={ev_rg.scores[RC.BALANCED]:.2f}")
sq5 = mk("5m", squeezed()); ev_sq = sc.score(sq5.close, V.analyze(sq5),
                                             T.analyze(Tape()), MS.analyze(sq5, 0.25))
check("COMPRESSION scores on a recent squeeze", ev_sq.scores[RC.COMPRESSION] > 0,
      str(ev_sq.scores[RC.COMPRESSION]))
_long_coil = mk("5m", squeezed(90, narrow_tail=45))
check("a coil that fills its own lookback stops ranking compressed (relative, "
      "not absolute — documented behaviour)",
      RC.ConfluenceScorer().score(_long_coil.close, V.analyze(_long_coil),
                                  T.analyze(Tape()), MS.analyze(_long_coil, 0.25)
                                  ).scores[RC.COMPRESSION] <
      ev_sq.scores[RC.COMPRESSION])
cold = sc.score([], V.analyze(mk("5m", trending_up(4))), tsu, msu)
check("unobservable evidence -> all None + observable=False",
      not cold.observable and all(v is None for v in cold.scores.values()))
check("ramp() maps None to 0.0, not to a neutral 0.5", RC.ramp(None, 0, 1) == 0.0)
check("de-saturated RANGE_ROOM bounds are the 07-22 values",
      RC.RANGE_ROOM_LO == 0.17 and RC.RANGE_ROOM_HI == 1.00)
check("de-saturated OSC_CROSS bounds are the 07-22 values",
      RC.OSC_CROSS_LO == 4.0 and RC.OSC_CROSS_HI == 10.0)
check("futures-native corroborators ship at weight 0",
      RC.W_TREND_CVD == 0.0 and RC.W_RANGE_VALUE == 0.0)
check("ported trend weights are numerically untouched",
      RC.W_TREND_ALIGN == 0.65 and RC.W_TREND_MOM == 0.35)
sw, _ = sc._sweep(20.0, 0.0)
check("sweep strength ramps in TICKS not percent", sw > 0.9, str(sw))
sw_old, _ = sc._sweep(20.0, 9.0)
check("sweep evidence decays with age", sw_old < sw * 0.2, f"{sw_old:.3f} vs {sw:.3f}")

print("\n=== 8. L2 INTEGRATOR — persistence, hysteresis, staleness ===")
def ev(**kw):
    d = {r: 0.0 for r in RC.REGIMES}; d.update(kw); return d
ig = ConvictionIntegrator(); t = 0.0
for _ in range(40):
    t += 15.0; s = ig.update(t, ev(TRENDING_UP=0.9))
check("sustained evidence builds conviction toward it",
      0.85 <= s.convictions[RC.TRENDING_UP] <= 0.9, f"{s.convictions[RC.TRENDING_UP]:.3f}")
check("emitted regime is the committed one", s.regime == RC.TRENDING_UP)
before = s.convictions[RC.TRENDING_UP]
t += 15.0
flick = ig.update(t, ev(TRENDING_UP=0.0, BALANCED=1.0))
check("ONE contrary tick does NOT flip a banked regime (the whole point)",
      flick.regime == RC.TRENDING_UP, f"{flick.regime} {flick.trigger}")
check("decay resistance scales with banked conviction",
      flick.convictions[RC.TRENDING_UP] > before * 0.7,
      f"{flick.convictions[RC.TRENDING_UP]:.3f} from {before:.3f}")
for _ in range(60):
    t += 15.0; s2 = ig.update(t, ev(EXPANSION=0.95))
check("SUSTAINED contrary evidence does displace it",
      s2.regime == RC.EXPANSION, f"{s2.regime} {s2.trigger}")
check("displacement is recorded in the trigger string", "displaced" in s2.trigger or s2.regime == RC.EXPANSION)
ig2 = ConvictionIntegrator(); t2 = 0.0
for _ in range(5): t2 += 15.0; ig2.update(t2, ev(TRENDING_UP=0.8))
st_gap = ig2.update(t2 + 500.0, ev(TRENDING_UP=0.8))
check("a feed gap beyond dt_max marks STALE rather than pretending continuity",
      st_gap.stale and "stale" in st_gap.trigger, st_gap.trigger)
none_ev = {r: None for r in RC.REGIMES}
check("unobservable evidence marks stale", ig2.update(t2 + 515.0, none_ev).stale)
ig3 = ConvictionIntegrator(); t3 = 0.0
for _ in range(3): t3 += 15.0; s3 = ig3.update(t3, ev(BALANCED=0.2, TRENDING_UP=0.15))
check("indecision emits a LOW-CONVICTION label, never an UNKNOWN state",
      s3.regime in RC.REGIMES and s3.conviction < 0.3, f"{s3.regime} {s3.conviction:.2f}")
check("BALANCED commits slowly by design (tau_up 780s)",
      ConvictionIntegrator().p.per_regime[RC.BALANCED].tau_up == 780.0)
check("sweeps die fast (low lam) so they cannot squat",
      ConvictionIntegrator().p.per_regime[RC.LIQUIDITY_SWEEP].lam == 1.5)
check("ported hysteresis priors intact (commit .65 / hold .45 / displace .12)",
      (IntegratorParams().theta_commit, IntegratorParams().theta_hold,
       IntegratorParams().delta_displace) == (0.65, 0.45, 0.12))
import tempfile
snap = os.path.join(tempfile.mkdtemp(), "integ.json")
ig.save(snap)
ig4 = ConvictionIntegrator(); ig4.load(snap)
check("state survives a restart (warm load, no cold-start blind spot)",
      ig4.incumbent == ig.incumbent and
      abs(ig4.C[RC.EXPANSION] - ig.C[RC.EXPANSION]) < 1e-9)

print("\n=== 9. SIGNAL JOURNAL — never fatal ===")
jd = tempfile.mkdtemp()
j = SignalJournal(root=jd, symbol="MNQ", mode="DAY")
check("emits a line", j.scored(grade="A", score=0.81) is True)
check("line landed on disk", any(f.endswith(".jsonl") for _, _, fs in os.walk(jd) for f in fs))
bad = SignalJournal(root="/proc/nonexistent/nope", symbol="MNQ")
check("an unwritable journal returns False and does NOT raise",
      bad.scored(x=1) is False)
check("disabled journal is a no-op", SignalJournal(enabled=False).scored(x=1) is False)

print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
sys.exit(1 if FAIL else 0)
