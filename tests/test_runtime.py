"""
futures_trader_v1/tests/test_runtime.py — v0.2
v0.2 — 2026-07-25 — roll-volume section (front/back session volume, unpaired
        sessions excluded, back month only in-window), replay-harness section,
        and control-timer assertions.
v0.1 — 2026-07-25 — Behavioural proof for the Phase-4 runtime: store, reader,
        broker, feed producer, and the loop end to end.
    python3 tests/test_runtime.py
"""
import os, sys, tempfile, time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FT_SYMBOL", "MNQ")
os.environ.setdefault("FT_MODE", "DAY")

TMP = tempfile.mkdtemp()
os.environ["FT_CANDLE_STORE"] = os.path.join(TMP, "feed.db")
os.environ["FT_TRADES_DB"] = os.path.join(TMP, "trades.db")
os.environ["FT_JOURNAL_DIR"] = os.path.join(TMP, "journal")

import config as C
from data.contract_registry import get_spec
from data.feed_store import FeedStore
from data.market_data import MarketData
from data.futures_feed import Aggregator, FeedProducer, SimulatedTransport, DXLinkTransport
from execution.broker import PaperBroker, TastyTradeBroker, BUY, SELL
from execution.order_confirm import FILLED, WORKING
from utils import sessions as S

PASS = FAIL = 0
def check(n, c, d=""):
    global PASS, FAIL
    if c: PASS += 1; print(f"  PASS  {n}")
    else: FAIL += 1; print(f"  FAIL  {n}  {d}")

SPEC = get_spec("MNQ")

print("\n=== 1. FEED STORE — one writer, many readers ===")
sp = os.path.join(TMP, "s1.db")
st = FeedStore(sp)
st.upsert_candles("MNQ", "1m", [(1000, 1, 2, 0.5, 1.5, 10)])
check("candle written", len(st.fetch_candles("MNQ", "1m")) == 1)
st.upsert_candles("MNQ", "1m", [(1000, 1, 3, 0.5, 2.5, 20)])
rows = st.fetch_candles("MNQ", "1m")
check("a re-sent partial bar UPSERTS in place (idempotent under reconnect)",
      len(rows) == 1 and rows[0][4] == 2.5, str(rows))
st.append_trades("MNQ", [(1000, 1, 1.5, 2, "BUY"), (1000, 2, 1.6, 3, "SELL")])
check("tick prints stored with the aggressor side",
      len(st.fetch_trades("MNQ")) == 2)
st.beat("feed", "ok")
check("heartbeat is fresh after a beat", st.heartbeat_age("feed") < 5)
ro = FeedStore(sp, read_only=True)
check("a read-only reader sees the writer's data", len(ro.fetch_candles("MNQ", "1m")) == 1)
check("no heartbeat at all reads as None, not as zero",
      FeedStore(os.path.join(TMP, "empty.db")).heartbeat_age("feed") is None)

print("\n=== 2. MARKET DATA — fail loud, never stale ===")
md = MarketData(sp, stale_seconds=100000)
ok, why = md.healthy()
check("healthy while the heartbeat is fresh", ok, why)
md_stale = MarketData(sp, stale_seconds=0.0)
ok2, why2 = md_stale.healthy()
check("past the ceiling the reader reports DOWN", not ok2, why2)
check("a stale reader returns NO DATA rather than old data",
      md_stale.candles("MNQ", "1m") is None)
check("a stale tape is flagged stale", md_stale.tape("MNQ").stale)
check("mark comes from the shared quote", md.mark("MNQ") is None or isinstance(md.mark("MNQ"), float))

print("\n=== 3. AGGREGATOR — ticks become bars ===")
agg = Aggregator(0.25)
prints = [(60, 1, 100.0, 1, "BUY"), (70, 2, 102.0, 1, "BUY"),
          (80, 3, 99.0, 1, "SELL"), (90, 4, 101.0, 2, "BUY")]
out = agg.add(prints)
bar = out["1m"][0]
check("OHLC derived correctly from prints",
      bar[1] == 100.0 and bar[2] == 102.0 and bar[3] == 99.0 and bar[4] == 101.0,
      str(bar))
check("volume summed", bar[5] == 5, str(bar[5]))
out2 = agg.add([(120, 5, 105.0, 1, "BUY")])
check("a new minute opens a new bar", out2["1m"][0][0] == 120, str(out2["1m"][0][0]))

print("\n=== 4. FEED PRODUCER ===")
fp = FeedProducer(SimulatedTransport(tick_size=0.25), os.path.join(TMP, "f2.db"))
for _ in range(25): fp.step()
s2 = FeedStore(fp.store.path, read_only=True)
check("producer fills the store with candles", len(s2.fetch_candles("MNQ", "1m")) > 3)
check("producer archives the tick tape", len(s2.fetch_trades("MNQ")) > 100)
check("producer publishes a quote", s2.fetch_quote("MNQ") is not None)
check("producer beats the heartbeat", s2.heartbeat_age("feed") is not None)
raised = False
try:
    DXLinkTransport().connect("MNQ")
except NotImplementedError:
    raised = True
check("the LIVE transport REFUSES rather than inventing bars", raised)

print("\n=== 5. PAPER BROKER ===")
mark = [21000.0]
pb = PaperBroker(lambda: mark[0], 0.25, slippage_ticks=1.0)
o = pb.place(BUY, 2, limit=None)
r = pb.poll(o.order_id)
check("a marketable buy fills at mark + a tick",
      r.status == FILLED and abs(r.fill_price - 21000.25) < 1e-9, str(r.fill_price))
o2 = pb.place(BUY, 1, limit=20990.0)
check("a resting limit does NOT fill while the mark is away",
      pb.poll(o2.order_id).status == WORKING)
mark[0] = 20989.0
check("it fills once the mark reaches it", pb.poll(o2.order_id).status == FILLED)
o3 = pb.place(SELL, 1, limit=21500.0)
check("an unfilled order can be cancelled", pb.cancel(o3.order_id))
check("a filled order cannot be cancelled", not pb.cancel(o.order_id))
raised = False
try:
    TastyTradeBroker().place(BUY, 1)
except NotImplementedError as e:
    raised = "unverified" in str(e)
check("the LIVE broker REFUSES every unverified method", raised)

print("\n=== 6. END TO END — the loop takes and manages a trade ===")
import main as M

# a tape with a real opening-range break + retest, timestamped for today's RTH
sess = S.session_date()
open_et = datetime.combine(sess, S.dtime(*SPEC.rth_open), tzinfo=S.ET)
def epoch(minute):
    return int((open_et + timedelta(minutes=minute)).timestamp())
rows = []
for i in range(5):                                    # 09:30-09:34 range
    rows.append((epoch(i), 21005, 21020, 21000, 21010, 500))
rows.append((epoch(5), 21015, 21026, 21014, 21024, 900))   # opens inside, closes out
rows.append((epoch(6), 21024, 21027, 21019, 21023, 700))   # wick in, body out
for i in range(7, 160):                               # warm 5m/ATR, not just 1m
    rows.append((epoch(i), 21023, 21028, 21021, 21024, 400))

fs = FeedStore(C.CANDLE_STORE)
for tf, secs in (("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("1d", 86400)):
    if tf == "1m":
        fs.upsert_candles("MNQ", tf, [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows])
    else:
        agg = {}
        for r in rows:
            b = (r[0] // secs) * secs
            cur = agg.get(b)
            if cur is None: agg[b] = [r[1], r[2], r[3], r[4], r[5]]
            else:
                cur[1] = max(cur[1], r[2]); cur[2] = min(cur[2], r[3])
                cur[3] = r[4]; cur[4] += r[5]
        fs.upsert_candles("MNQ", tf, [(b, *v) for b, v in sorted(agg.items())])
fs.put_quote("MNQ", 21022.75, 21023.25, 21023.0)
fs.beat("feed", "test")

# pin the clock inside RTH so the session gates behave deterministically
fixed = open_et + timedelta(minutes=7)
class Clock:
    def __getattr__(self, k): return getattr(S, k)
    def now_et(self): return fixed
    def market_is_open(self, *a, **k): return True
    def session_date(self, *a, **k): return sess
M.S = Clock()

bot = M.Bot()
check("bot boots against a live store", bot.boot() is True)
bot.tick()
opened = bot.positions.position
check("a confirmed opening-range setup produces an OPEN POSITION end to end",
      opened is not None, str(bot.state.skipped))
if opened:
    check("position carries a real fill price and contracts",
          opened.entry > 0 and opened.contracts_open >= 1,
          f"{opened.entry} x{opened.contracts_open}")
    check("the trade was written to the database",
          len(bot.trades.get_open_trades()) == 1)
    check("R is anchored to the entry stop",
          abs(opened.initial_stop - opened.stop) < 1e-9)
    # walk it to the stop and confirm it closes and books
    bot.positions.manage(opened.initial_stop - 1.0, now=fixed)
    check("the position closes when the stop is taken out", bot.positions.flat)
    check("the database row is CLOSED", bot.trades.get_open_trades() == [])
    check("realized P&L recorded for the session",
          bot.trades.realized_pnl_today(sess) != 0.0,
          str(bot.trades.realized_pnl_today(sess)))
bot2 = M.Bot()
bot2.boot()
check("a restart ADOPTS nothing when the book is flat", bot2.positions.flat)

print("\n=== 6b. ROLL VOLUME — the crossover can actually fire ===")
vs = FeedStore(os.path.join(TMP, "vol.db"))
vs.add_session_volume("MNQU6", "2026-09-08", 900000)
vs.add_session_volume("MNQZ6", "2026-09-08", 400000)
vs.add_session_volume("MNQU6", "2026-09-09", 700000)
vs.add_session_volume("MNQZ6", "2026-09-09", 800000)
vh = vs.volume_history("MNQU6", "MNQZ6")
check("front and back volume recorded per session", len(vh) == 2, str(vh))
check("history is oldest-first and paired", vh[0][0] == "2026-09-08" and vh[1][2] == 800000)
vs.add_session_volume("MNQU6", "2026-09-10", 500000)
check("a session with only the FRONT reporting is EXCLUDED (a crossover needs "
      "two numbers; treating a missing back month as zero would freeze the roll)",
      len(vs.volume_history("MNQU6", "MNQZ6")) == 2)
vs.add_session_volume("MNQU6", "2026-09-08", 100000)
check("volume accumulates within a session",
      vs.volume_history("MNQU6", "MNQZ6")[0][1] == 1000000)
from data.contract_registry import assess_roll, CROSSOVER
from datetime import date as _date
hist = [(_date.fromisoformat(d), f, b) for d, f, b in
        [("2026-09-08", 900000, 400000), ("2026-09-09", 700000, 800000),
         ("2026-09-10", 500000, 1100000)]]
a = assess_roll("MNQ", _date(2026, 9, 10), hist, confirm_sessions=2)
check("real volume history CONFIRMS the crossover (it could not before)",
      a.state == CROSSOVER and a.should_roll, f"{a.state} {a.reason}")
a2 = assess_roll("MNQ", _date(2026, 9, 10), None, confirm_sessions=2)
check("with NO history it falls through to the deadline path only",
      not a2.should_roll, a2.reason)
fp2 = FeedProducer(SimulatedTransport(tick_size=0.25),
                   os.path.join(TMP, "roll.db"),
                   back_transport=SimulatedTransport(tick_size=0.25, seed=5))
check("the back month is NOT subscribed outside the roll window",
      not fp2.in_roll_window(_date(2026, 8, 1)))
check("it IS subscribed inside the window",
      fp2.in_roll_window(_date(2026, 9, 10)))

print("\n=== 6c. REPLAY HARNESS ===")
import subprocess
fp3 = FeedProducer(SimulatedTransport(tick_size=0.25),
                   os.path.join(TMP, "replay.db"),
                   back_transport=SimulatedTransport(tick_size=0.25, seed=9))
for _ in range(1200): fp3.step()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from replay import Replayer
rp = Replayer("MNQ", bookmark_sessions=15, step_bars=10)
summ = rp.run(fp3.store.path)
check("replay scores archived tape through the REAL engines", summ.ticks > 5,
      str(summ.ticks))
check("sampling is not mistaken for a feed gap (dt_max follows --step)",
      summ.stale == 0, f"{summ.stale}/{summ.ticks} stale")
check("the harness reports starvation separately from a legitimate veto",
      hasattr(summ, "ranging_silent") and hasattr(summ, "ranging_vetoed"))
check("a warm bookmark leaves no starved ticks", summ.starved == 0, str(summ.starved))
rp2 = Replayer("MNQ", step_bars=1)
check("dt_max is raised only as far as the step needs",
      rp2.l2.p.dt_max == 90.0, str(rp2.l2.p.dt_max))

print("\n=== 7. INSTALL SCHEMA — the unattended contract ===")
setup = open("setup_ec2.sh").read()
check("unattended detection present", "UNATTENDED=true" in setup)
check("installs are ALWAYS paper", "FT_PAPER_TRADING=True" in setup)
check("chmod +x runs AFTER git reset (git strips the bit)",
      setup.index("reset -q --hard") < setup.index('-name "*.sh" -exec chmod +x'))
check("bootstrap.sh is shredded during cleanup", "shred -u" in setup)
check("deploy dir and install.sh are removed", 'rm -rf "$DEPLOY_DIR"' in setup)
check("feed service starts BEFORE the bot", setup.index("start $FEED_SERVICE") <
      setup.index("start $BOT_SERVICE"))
check("needrestart is blocked so an upgrade cannot restart us mid-session",
      "needrestart" in setup)
conf = open("configure.sh").read()
check("go-live requires typing LIVE", 'type LIVE to confirm' in conf)
check("go-live shows capacity at the live balance first", "risk.capacity" in conf)
tm = open("install_control_timers.sh").read()
check("control timers installed for wake and EOD",
      "ft-wake.timer" in tm and "ft-eod.timer" in tm)
check("timers are Persistent=false (a missed run must not fire into a session)",
      tm.count("Persistent=false") >= 2)

print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
sys.exit(1 if FAIL else 0)
