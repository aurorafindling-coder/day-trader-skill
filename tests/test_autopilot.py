import copy
import datetime
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ap = load("trader-broker", "autopilot")
entry = ap.entry

EXAMPLE = os.path.join(ROOT, "trader-broker", "autopilot.example.json")
TSLA = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")


def example(**over):
    with open(EXAMPLE) as fh:
        cfg = json.load(fh)
    cfg.update(over)
    return cfg


def reel(**over):
    """The reel's limits ($5,000, $50 a trade) on TSLA alone."""
    cfg = example(budget=5000, max_loss_per_trade_usd=50, max_daily_loss_usd=150, max_trades_per_day=3,
                  max_orders_per_day=10, max_order_usd=4000, max_position_pct=75, cash_buffer_pct=10)
    cfg["autopilot"] = dict(cfg["autopilot"], watchlist=["TSLA"])
    cfg.update(over)
    return ap.check_config(cfg)


def tsla_chart():
    return entry.parse_chart(entry.read_text(TSLA), "TSLA")


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return entry.eastern_to_utc(datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(h, m)))


class Config(unittest.TestCase):
    def test_the_example_is_paper_and_sized_for_about_50_euros(self):
        cfg = ap.check_config(example())
        self.assertIs(cfg["paper"], True)
        self.assertLessEqual(cfg["budget"], 60)
        self.assertLessEqual(cfg["max_loss_per_trade_usd"], 0.05 * cfg["budget"])
        self.assertTrue(cfg["autopilot"]["watchlist"])

    def test_refuses_anything_but_paper(self):
        for paper in (False, None, "true", 1):
            with self.assertRaises(ap.AutopilotError):
                ap.check_config(example(paper=paper))

    def test_refuses_without_limits_or_with_leverage(self):
        for over in ({"max_loss_per_trade_usd": 0}, {"max_daily_loss_usd": None}, {"max_trades_per_day": 0},
                     {"require_stop": False}, {"allow_short": True}, {"allow_margin": True},
                     {"allow_options": True}, {"budget": 0}):
            with self.assertRaises(ap.AutopilotError, msg=str(over)):
                ap.check_config(example(**over))

    def test_refuses_an_empty_watchlist_and_an_atr_stop(self):
        cfg = example()
        cfg["autopilot"] = dict(cfg["autopilot"], watchlist=[])
        with self.assertRaises(ap.AutopilotError):
            ap.check_config(cfg)
        cfg["autopilot"] = dict(cfg["autopilot"], watchlist=["f", "F"], stop="atr:1.5")
        with self.assertRaises(ap.AutopilotError):
            ap.check_config(cfg)

    def test_the_only_broker_address_is_the_paper_api(self):
        with open(os.path.join(ROOT, "trader-broker", "autopilot.py"), encoding="utf-8") as fh:
            src = fh.read()
        hosts = set(re.findall(r"[\w.-]*alpaca\.markets", src))
        self.assertEqual(hosts, {"paper-api.alpaca.markets"})
        self.assertEqual(ap.PAPER_BASE, "https://paper-api.alpaca.markets")


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.charts = {"TSLA": tsla_chart()}
        cls.days = ap.full_days(cls.charts)

    def test_full_sessions_only(self):
        self.assertEqual(len(self.days), 59)
        self.assertNotIn(datetime.date(2026, 10, 19), self.days)   # the fixture stops mid-morning

    def test_matches_the_time_machine_history(self):
        # Same rule as timemachine's history (or-mid stop, 1R, stop first): 33 fired, 13 target, 15 stop, 5 neither.
        j = ap.replay(reel(), self.charts, self.days, ap.new_journal(reel()))
        s = ap.stats(j)
        self.assertEqual(s["trades"], 33)
        self.assertEqual(s["by_reason"], {"target": 13, "stop": 15, "flat_by": 5})
        self.assertEqual(s["pnl"], -14.24)
        for t in j["trades"]:
            self.assertLessEqual(t["max_loss"], 50)
            self.assertEqual(t["status"], "closed")
            if t["exit_reason"] == "stop":
                self.assertEqual(t["R"], -1.0)

    def test_one_position_and_one_trade_per_symbol_a_day(self):
        j = ap.replay(reel(), self.charts, self.days, ap.new_journal(reel()))
        dates = [t["date"] for t in j["trades"]]
        self.assertEqual(len(dates), len(set(dates)))

    def test_fifty_euros_cannot_buy_tsla(self):
        cfg = ap.check_config(example())
        lines = []
        j = ap.replay(cfg, self.charts, self.days, ap.new_journal(cfg), say=lines.append)
        self.assertEqual(j["trades"], [])
        skips = [l for l in lines if "SKIP" in l]
        self.assertTrue(skips)
        self.assertTrue(all("No order" in l for l in skips))

    def test_losses_shrink_the_next_size(self):
        cfg = reel(budget=3000, max_position_pct=100, cash_buffer_pct=0, max_order_usd=3000)
        j = ap.replay(cfg, self.charts, self.days, ap.new_journal(cfg))
        cash = 3000.0
        for t in j["trades"]:
            self.assertLessEqual(t["qty"] * t["entry_plan"], cash + 0.01)
            cash += t["pnl"]

    def test_the_cli_replays_offline(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "trader"))
            with open(os.path.join(d, "trader", "config.json"), "w") as fh:
                json.dump(example(), fh)
            out = io.StringIO()
            cwd = os.getcwd()
            os.chdir(d)
            try:
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    code = ap.main(["replay", "--offline", TSLA, "--budget", "5000", "--quiet"])
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 0)
            self.assertIn("Replayed 59 sessions", out.getvalue())
            self.assertTrue(os.path.exists(os.path.join(d, "trader", "state", "autopilot-replay.json")))


class Resp(object):
    def __init__(self, doc):
        self.text = "" if doc is None else json.dumps(doc)

    def read(self):
        return self.text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeAlpaca(object):
    """Alpaca's paper API, just enough of it: orders with bracket legs, cancel, account."""

    def __init__(self):
        self.calls, self.orders, self.n = [], {}, 0

    def find(self, oid):
        for o in self.orders.values():
            if o["id"] == oid:
                return o
            for leg in o.get("legs") or []:
                if leg["id"] == oid:
                    return leg
        raise KeyError(oid)

    def __call__(self, req, timeout=None):
        assert req.full_url.startswith(ap.PAPER_BASE + "/"), req.full_url
        method, path = req.get_method(), req.full_url[len(ap.PAPER_BASE):]
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.calls.append((method, path.split("?")[0], body))
        if path == "/v2/account":
            return Resp({"account_number": "PA0TEST", "status": "ACTIVE", "cash": "100000"})
        if method == "POST" and path == "/v2/orders":
            self.n += 1
            oid = "o%d" % self.n
            o = dict(body, id=oid, status="new", filled_qty="0", filled_avg_price=None)
            if body.get("order_class") == "bracket":
                o["legs"] = [{"id": oid + "-tp", "type": "limit", "status": "held"},
                             {"id": oid + "-sl", "type": "stop", "status": "held"}]
            self.orders[oid] = o
            return Resp(o)
        oid = path.split("/")[3].split("?")[0]
        if method == "GET":
            return Resp(self.find(oid))
        if method == "DELETE":
            self.find(oid)["status"] = "canceled"
            return Resp(None)
        raise AssertionError((method, path))


class Live(unittest.TestCase):
    """The engine against a fake Alpaca, on Aug 31 2026 (TSLA triggers at 9:50, target at 13:20)."""
    DAY = "2026-08-31"

    def setUp(self):
        self.day_chart = ap.by_day(tsla_chart())[datetime.date(2026, 8, 31)]
        self.fake = FakeAlpaca()
        self.broker = ap.AlpacaPaper("PKTEST", "secret", opener=self.fake)
        self.lines = []
        self.tmp = tempfile.mkdtemp()
        self.kill = os.path.join(self.tmp, "STOP")
        self.bot = ap.Autopilot(reel(), self.broker, lambda sym, now: self.day_chart, ap.new_journal(reel()),
                                say=self.lines.append, kill_file=self.kill)

    def step(self, hhmm):
        self.bot.step(at(self.DAY, hhmm))

    def trade(self):
        return self.bot.journal["trades"][0]

    def parent(self):
        return self.fake.orders["o1"]

    def test_no_keys_no_broker(self):
        with self.assertRaises(ap.AutopilotError):
            ap.AlpacaPaper("", "")

    def test_nothing_before_the_trigger(self):
        for t in ("09:30", "09:40", "09:45"):
            self.step(t)
        self.assertEqual(self.fake.calls, [])

    def test_places_one_bracket_order_with_the_checked_numbers(self):
        self.step("09:50")
        posts = [c for c in self.fake.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], {
            "symbol": "TSLA", "qty": "6", "side": "buy", "type": "market", "time_in_force": "day",
            "order_class": "bracket", "client_order_id": "ap-20260831-TSLA",
            "take_profit": {"limit_price": "367.30"}, "stop_loss": {"stop_price": "351.54"}})
        self.assertEqual(self.trade()["status"], "pending")
        self.assertTrue(any("PLACED" in l for l in self.lines))
        self.step("09:55")
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "POST"]), 1)   # no second entry

    def test_fill_then_take_profit(self):
        self.step("09:50")
        self.parent().update(status="filled", filled_qty="6", filled_avg_price="359.60")
        self.step("09:55")
        self.assertEqual((self.trade()["status"], self.trade()["entry_price"]), ("open", 359.6))
        self.parent()["legs"][0].update(status="filled", filled_avg_price="367.30")
        self.step("13:25")
        t = self.trade()
        self.assertEqual((t["status"], t["exit_reason"], t["pnl"]), ("closed", "target", 46.2))
        self.assertTrue(any("CLOSED  TSLA target" in l for l in self.lines))

    def test_stop_loss_counts_as_a_loss(self):
        self.step("09:50")
        self.parent().update(status="filled", filled_qty="6", filled_avg_price="359.42")
        self.parent()["legs"][1].update(status="filled", filled_avg_price="351.40")
        self.step("10:30")
        t = self.trade()
        self.assertEqual((t["exit_reason"], t["pnl"]), ("stop", -48.12))
        self.assertLess(t["R"], -1)                       # a stop can fill worse than its price

    def test_flat_by_cancels_the_legs_and_sells(self):
        self.step("09:50")
        self.parent().update(status="filled", filled_qty="6", filled_avg_price="359.42")
        self.step("15:55")
        deletes = [c[1] for c in self.fake.calls if c[0] == "DELETE"]
        self.assertEqual(sorted(deletes), ["/v2/orders/o1-sl", "/v2/orders/o1-tp"])
        sell = [c[2] for c in self.fake.calls if c[0] == "POST"][1]
        self.assertEqual((sell["side"], sell["qty"], sell["type"]), ("sell", "6", "market"))
        self.fake.orders["o2"].update(status="filled", filled_avg_price="362.00")
        self.step("15:56")
        t = self.trade()
        self.assertEqual((t["status"], t["exit_reason"], t["pnl"]), ("closed", "flat_by", 15.48))
        self.step("15:57")
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "POST"]), 2)   # one sell, once

    def test_an_entry_that_never_fills_is_canceled(self):
        self.step("09:50")
        self.step("15:55")
        self.assertEqual(self.trade()["status"], "void")
        self.assertIn(("DELETE", "/v2/orders/o1", None), self.fake.calls)
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "POST"]), 1)

    def test_kill_switch_closes_now_and_blocks_new_entries(self):
        self.step("09:50")
        self.parent().update(status="filled", filled_qty="6", filled_avg_price="359.42")
        open(self.kill, "w").close()
        self.step("10:00")
        self.assertEqual([c[2]["side"] for c in self.fake.calls if c[0] == "POST"], ["buy", "sell"])
        self.assertTrue(any("kill switch" in l for l in self.lines))

    def test_kill_switch_before_the_trigger_means_no_order(self):
        open(self.kill, "w").close()
        for t in ("09:50", "09:55", "10:00"):
            self.step(t)
        self.assertEqual(self.fake.calls, [])

    def test_a_rejected_order_is_logged_not_retried_blindly(self):
        def reject(req, timeout=None):
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"message": "insufficient buying power"}'))
        self.bot.broker = ap.AlpacaPaper("PKTEST", "secret", opener=reject)
        self.step("09:50")
        self.assertEqual(self.bot.journal["trades"], [])
        self.assertTrue(any("FAILED" in l and "insufficient buying power" in l for l in self.lines))


if __name__ == "__main__":
    unittest.main()
