#!/usr/bin/env python3
"""Autopilot for trader-broker: day trades on its own, on an Alpaca PAPER account only.
Standard library only.

    python3 autopilot.py check                         # config + paper account, no orders
    python3 autopilot.py run                           # trade the paper account until you stop it
    python3 autopilot.py run --until-close             # today only, then exit (for a daily scheduler)
    python3 autopilot.py replay --offline TSLA.json.gz # past sessions, simulated broker, no network
    python3 autopilot.py replay --symbols F,SOFI       # 60 days of Yahoo bars, simulated broker
    python3 autopilot.py status                        # the journal: every trade, P&L, R

This is the one place in the pack that places orders without asking, and so
it is fenced in on purpose:

  * PAPER ONLY. The only broker address in this file is Alpaca's paper API,
    https://paper-api.alpaca.markets. There is no switch to a live account.
    "paper": false in the config makes it refuse to start.
  * The same rules as by hand. Every entry goes through trader-entry (the
    trigger), trader-exit (stop and target), trader-budget (the size) and
    trader-risk (PASS or BLOCK). Only a PASS is sent.
  * One position at a time, long only, whole shares, a bracket order every
    time (stop-loss and take-profit wait at the broker), flat by flat_by.
  * Kill switch: create the file trader/STOP. No new entries, and an open
    position is closed at once. Delete the file to go on.

The rule: an opening-range breakout on the symbols in "autopilot.watchlist".
After each 5-minute bar closes it checks every symbol it has not traded today;
the first one whose trigger bar closed within "fresh_minutes" (5) is planned
(stop "or-mid", target "1R" by default), sized to the budget (the budget plus
the realized P&L so far, so losses shrink the next size), checked, and sent.

trader/config.json: the usual keys (budget, max_loss_per_trade_usd,
max_daily_loss_usd, max_trades_per_day, entry window, flat_by ...) plus
    "autopilot": {"watchlist": ["F", "SOFI"], "stop": "or-mid", "target": "1R",
                  "or_minutes": 15, "fresh_minutes": 5, "poll_seconds": 30}
Start from autopilot.example.json in this folder.

Keys: ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment (paper keys,
from the Alpaca dashboard). Never in the config, never in a chat.

The journal is trader/state/autopilot.json (replay: trader/state/autopilot-replay.json).
Replay is a simulation: entries fill at the trigger bar's close, the stop is
checked before the target in every bar (a bar that touches both is a stop),
a gap through the stop fills at the bar's open, and flat_by sells at the last
closed bar's close. Real fills, spreads and fees differ. Not financial advice.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER_BASE = "https://paper-api.alpaca.markets"     # the only broker address in this file
BAR = 300
AP_DEFAULTS = {"watchlist": [], "stop": "or-mid", "target": "1R", "or_minutes": 15,
               "fresh_minutes": 5, "poll_seconds": 30}
KILL_FILE = "trader/STOP"
OPEN_LEG = ("new", "accepted", "held", "pending_new", "partially_filled", "accepted_for_bidding", "calculated")
DEAD = ("canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended")


class AutopilotError(Exception):
    pass


def _sibling(folder, name):
    path = os.path.join(os.path.dirname(HERE), folder, name + ".py")
    if not os.path.exists(path):
        raise SystemExit("autopilot needs %s next to trader-broker (install the whole pack)" % path)
    spec = importlib.util.spec_from_file_location("autopilot_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


entry = _sibling("trader-entry", "entry")
planner = _sibling("trader-exit", "exit")
sizer = _sibling("trader-budget", "size")
risk = _sibling("trader-risk", "check")
PLAN_ERRORS = (entry.EntryError, planner.PlanError, sizer.SizeError, risk.RiskError)


# ---------------------------------------------------------------- config and journal

def load_config(path):
    if not os.path.exists(path):
        raise AutopilotError("no %s: copy trader-broker/autopilot.example.json there and edit it" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except ValueError as e:
        raise AutopilotError("%s is not valid JSON: %s" % (path, e))
    return check_config(raw)


def check_config(raw):
    cfg = dict(sizer.DEFAULTS)
    cfg.update(raw or {})
    ap = dict(AP_DEFAULTS)
    ap.update(cfg.get("autopilot") or {})
    cfg["autopilot"] = ap
    if cfg.get("paper") is not True:
        raise AutopilotError("\"paper\" is not true in the config: the autopilot trades paper accounts only")
    if not sizer._f(cfg.get("budget")) or sizer._f(cfg["budget"]) <= 0:
        raise AutopilotError("set \"budget\" in the config (the money this is for, in dollars)")
    for k in ("max_loss_per_trade_usd", "max_daily_loss_usd", "max_trades_per_day"):
        if not sizer._f(cfg.get(k)) or sizer._f(cfg[k]) <= 0:
            raise AutopilotError("set \"%s\" above 0: the autopilot never runs without that limit" % k)
    if not cfg.get("require_stop", True):
        raise AutopilotError("\"require_stop\" must stay true for the autopilot")
    for k in ("allow_short", "allow_margin", "allow_options"):
        if cfg.get(k):
            raise AutopilotError("\"%s\" must be false for the autopilot (long stock only)" % k)
    wl = [str(s).strip().upper() for s in ap.get("watchlist") or [] if str(s).strip()]
    if not wl:
        raise AutopilotError("autopilot.watchlist is empty: list the symbols it may trade")
    ap["watchlist"] = list(dict.fromkeys(wl))
    rule, _ = planner.parse_stop(ap["stop"])
    if rule == "atr":
        raise AutopilotError("autopilot.stop: or-mid, or-low or pct:X (atr is not supported here)")
    planner.parse_target(ap["target"])
    if int(ap["or_minutes"]) % 5 or not 5 <= int(ap["or_minutes"]) <= 120:
        raise AutopilotError("autopilot.or_minutes wants a multiple of 5 from 5 to 120")
    return cfg


def new_journal(cfg):
    return {"created_by": "trader-autopilot", "paper": True, "budget": float(cfg["budget"]),
            "trades": [], "events": []}


def load_journal(path, cfg):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return new_journal(cfg)


def save_journal(path, journal):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    journal["events"] = journal["events"][-1000:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(journal, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def realized(journal):
    return round(sum(t.get("pnl") or 0.0 for t in journal["trades"] if t["status"] == "closed"), 2)


def as_broker_state(journal):
    """What trader-risk reads from trader-broker's log: today's placed entries with their max loss."""
    return {"orders": [{"status": "placed", "date": t["date"], "symbol": t["symbol"], "side": "buy",
                        "kind": "entry", "max_loss": t["max_loss"]}
                       for t in journal["trades"] if t["status"] != "void"]}


def _r2(x):
    return round(float(x) + 0.0, 2)


def close_trade(trade, price, reason, now):
    trade.update(status="closed", exit_price=_r2(price), exit_reason=reason, closed_at=iso(now))
    e = trade["entry_price"]
    trade["pnl"] = _r2((price - e) * trade["qty"])
    rps = e - trade["stop"]
    trade["R"] = round((price - e) / rps, 2) if rps > 0 else None


def iso(now_utc):
    return to_et(now_utc).strftime("%Y-%m-%d %H:%M:%S ET")


def to_et(now_utc):
    return entry.to_eastern(now_utc)


# ---------------------------------------------------------------- the engine

class Autopilot(object):
    """One step per call: manage the open position, else look for one entry."""

    def __init__(self, cfg, broker, data, journal, save=None, say=None, kill_file=None):
        self.cfg, self.ap = cfg, cfg["autopilot"]
        self.broker, self.data, self.journal = broker, data, journal
        self._save = save or (lambda j: None)
        self._say = say or (lambda line: None)
        self.kill_file = kill_file
        self.scanned = None
        self.summarised = set()
        self.passed = set()             # (day, symbol): triggered but skipped; an orb triggers once a day

    def say(self, now, msg):
        line = "%s  %s" % (to_et(now).strftime("%Y-%m-%d %H:%M"), msg)
        self.journal["events"].append(line)
        self._say(line)

    def save(self):
        self._save(self.journal)

    def open_trade(self):
        return next((t for t in self.journal["trades"] if t["status"] in ("pending", "open")), None)

    def today(self, day):
        return [t for t in self.journal["trades"] if t["date"] == day and t["status"] != "void"]

    def killed(self):
        return bool(self.kill_file and os.path.exists(self.kill_file))

    def step(self, now):
        et = to_et(now)
        day = et.date()
        ok, _ = entry.trading_day(day)
        trade = self.open_trade()
        if trade:
            self.manage(trade, now)
            return
        if not ok:
            return
        o, c = entry.session(day)
        if et.time() >= c:
            self.summary(day, now)
            return
        if self.killed():
            return
        slot = entry.ts_of(now) // BAR
        if slot == self.scanned:
            return
        self.scanned = slot
        cfg = self.cfg
        before = entry.parse_hhmm(cfg["no_entries_before"], "no_entries_before")
        cutoff = entry.early_cutoff(day, entry.parse_hhmm(cfg["no_entries_after"], "no_entries_after"),
                                    datetime.datetime.combine(day, c))
        if et.time() < before or et > cutoff:
            return
        done = self.today(day.isoformat())
        if len(done) >= int(cfg["max_trades_per_day"]):
            return
        if sum(t["max_loss"] for t in done) >= float(cfg["max_daily_loss_usd"]):
            return
        traded = {t["symbol"] for t in done}
        for sym in self.ap["watchlist"]:
            if sym in traded or (day, sym) in self.passed:
                continue
            if self.try_symbol(sym, now):
                return

    def manage(self, trade, now):
        before = trade["status"]
        try:
            self.broker.sync(trade, now)
        except AutopilotError as e:
            self.say(now, "%s: could not check the order (%s); trying again" % (trade["symbol"], e))
        if trade["status"] == "open" and before == "pending":
            self.say(now, "FILLED  buy %d %s at %.2f" % (trade["qty"], trade["symbol"], trade["entry_price"]))
        if trade["status"] in ("open", "pending"):
            et = to_et(now)
            flat = datetime.datetime.combine(et.date(), entry.parse_hhmm(trade["flat_by"], "flat_by"))
            late = et.date().isoformat() > trade["date"] or et >= flat
            if late or self.killed():
                why = "kill switch (trader/STOP)" if self.killed() and not late else "flat by %s" % trade["flat_by"]
                try:
                    self.broker.flatten(trade, now)
                except AutopilotError as e:
                    self.say(now, "%s: flatten failed (%s); trying again" % (trade["symbol"], e))
                if trade["status"] not in ("closed", "void"):
                    self.say(now, "%s: closing, %s" % (trade["symbol"], why))
        if trade["status"] == "closed" and before != "closed":
            self.say(now, "CLOSED  %s %s at %.2f: %+.2f USD (%+.2fR), %d shares bought at %.2f"
                     % (trade["symbol"], trade["exit_reason"], trade["exit_price"], trade["pnl"],
                        trade["R"] or 0, trade["qty"], trade["entry_price"]))
        if trade["status"] == "void" and before != "void":
            self.say(now, "VOID  %s: %s" % (trade["symbol"], trade.get("note") or "the entry did not fill"))
        self.save()

    def try_symbol(self, sym, now):
        try:
            chart = self.data(sym, now)
            res = entry.evaluate(chart, now, "orb", int(self.ap["or_minutes"]), "5m",
                                 cfg={"no_entries_before": self.cfg["no_entries_before"],
                                      "no_entries_after": self.cfg["no_entries_after"]})
        except PLAN_ERRORS as e:
            self.say(now, "%s: no data (%s)" % (sym, e))
            return False
        if res["status"] != "triggered":
            return False
        self.passed.add((to_et(now).date(), sym))
        if res.get("minutes_since", 99) > int(self.ap["fresh_minutes"]):
            return False
        try:
            order, sizing, verdict = self.build(res, now)
        except PLAN_ERRORS as e:
            self.say(now, "SKIP  %s triggered but: %s" % (sym, e))
            return False
        if order is None:
            self.say(now, "SKIP  %s triggered at %.2f but: %s" % (sym, res["trigger"]["price"], sizing["refusal"]))
            return False
        if verdict["verdict"] != "PASS":
            self.say(now, "BLOCK  %s: %s" % (sym, "; ".join(verdict["reasons"])))
            return False
        day = to_et(now).date().isoformat()
        p = order["plan"]
        trade = {"id": "ap-%s-%s" % (day.replace("-", ""), sym), "date": day, "symbol": sym,
                 "qty": int(order["qty"]), "entry_plan": p["entry"], "stop": p["stop"], "target": p["target"],
                 "risk_per_share": p["risk_per_share"], "max_loss": p["max_loss"], "flat_by": p["flat_by"],
                 "trigger_ts": res["_trigger_end"], "placed_at": iso(now), "status": "pending",
                 "why": order["why"], "fingerprint": verdict["fingerprint"]}
        try:
            self.broker.place(order, trade, now)
        except AutopilotError as e:
            self.say(now, "FAILED  %s order not placed: %s" % (sym, e))
            return False
        self.journal["trades"].append(trade)
        self.say(now, "PLACED  buy %d %s (bracket: stop %.2f, target %.2f, max loss %.2f USD) %s"
                 % (trade["qty"], sym, trade["stop"], trade["target"], trade["max_loss"], order["why"]))
        if trade["status"] == "open":
            self.say(now, "FILLED  buy %d %s at %.2f" % (trade["qty"], sym, trade["entry_price"]))
        self.save()
        return True

    def build(self, res, now):
        """trader-exit, trader-budget and trader-risk on one trigger. -> (order or None, sizing, verdict)."""
        cfg, ap = self.cfg, self.ap
        et = to_et(now)
        day = et.date()
        trig, orr = res["trigger"], res["opening_range"]
        stop_rule, stop_p = planner.parse_stop(ap["stop"])
        t_rule, t_p = planner.parse_target(ap["target"])
        c = planner.compute(trig["price"], stop_rule, stop_p, t_rule, t_p, orr["high"], orr["low"])
        flat_by, _ = planner.resolve_flat_by(day, None, cfg, entry.parse_hhmm(trig["close_time"], "entry"))
        plan = {"symbol": res["symbol"], "side": "buy", "date": day.isoformat(), "setup": "orb",
                "entry": float(c["entry"]), "stop": float(c["stop"]), "target": float(c["target"]),
                "entry_time": trig["time"], "flat_by": flat_by, "stop_label": c["stop_label"],
                "target_label": c["target_label"]}
        cash = float(cfg["budget"]) + realized(self.journal)
        last = (res.get("last") or {}).get("close") or trig["price"]
        s = sizer.size_trade(plan, cfg, cash=cash, price=last, today=day.isoformat())
        order = sizer.bracket_order(s)
        res["_trigger_end"] = entry.ts_of(entry.eastern_to_utc(
            datetime.datetime.combine(day, entry.parse_hhmm(trig["close_time"], "trigger"))))
        if order is None:
            return None, s, None
        out = risk.check([order], cfg, portfolio={"cash": cash, "positions": []},
                         prices={res["symbol"]: last}, state=as_broker_state(self.journal),
                         today=day.isoformat(), now=now, price_times={res["symbol"]: now})
        return order, s, out["results"][0]

    def summary(self, day, now):
        key = day.isoformat()
        if key in self.summarised:
            return
        self.summarised.add(key)
        done = [t for t in self.journal["trades"] if t["date"] == key and t["status"] == "closed"]
        pnl = sum(t["pnl"] for t in done)
        self.say(now, "DAY  %s: %d trade%s, %+.2f USD today, %+.2f USD in total, cash %.2f USD"
                 % (key, len(done), "" if len(done) == 1 else "s", pnl, realized(self.journal),
                    float(self.cfg["budget"]) + realized(self.journal)))
        self.save()


# ---------------------------------------------------------------- brokers

class SimBroker(object):
    """Replay fills from the session's own 5-minute bars."""

    def __init__(self, charts):
        self.charts = charts

    def _bars(self, trade, now):
        now_ts = entry.ts_of(now)
        for b in self.charts[trade["symbol"]]["bars"]:
            if b["t"] % BAR or b["t"] < trade["trigger_ts"] or b["t"] + BAR > now_ts:
                continue
            if to_et(entry.utc_of(b["t"])).date().isoformat() != trade["date"]:
                continue
            yield b

    def place(self, order, trade, now):
        trade.update(status="open", entry_price=_r2(order["plan"]["entry"]), broker_id="sim-" + trade["id"],
                     seen_ts=trade["trigger_ts"] - BAR)

    def sync(self, trade, now):
        if trade["status"] != "open":
            return
        for b in self._bars(trade, now):
            if b["t"] <= trade["seen_ts"]:
                continue
            trade["seen_ts"] = b["t"]
            if b["l"] <= trade["stop"]:
                close_trade(trade, min(b["o"], trade["stop"]), "stop", now)
                return
            if b["h"] >= trade["target"]:
                close_trade(trade, max(b["o"], trade["target"]), "target", now)
                return

    def flatten(self, trade, now):
        self.sync(trade, now)
        if trade["status"] != "open":
            return
        last = None
        for b in self._bars(trade, now):
            last = b
        close_trade(trade, last["c"] if last else trade["entry_price"], "flat_by", now)


class AlpacaPaper(object):
    """Alpaca's trading API v2 on the paper address only."""

    def __init__(self, key, secret, opener=None):
        if not key or not secret:
            raise AutopilotError("set ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment (paper keys)")
        self.key, self.secret = key, secret
        self.opener = opener or urllib.request.urlopen

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(PAPER_BASE + path, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener(req, timeout=20) as r:
                text = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("message")
            except Exception:
                msg = None
            raise AutopilotError("Alpaca %s %s: HTTP %s %s" % (method, path.split("?")[0], e.code, msg or e.reason))
        except (urllib.error.URLError, OSError) as e:
            raise AutopilotError("Alpaca unreachable: %s" % e)
        return json.loads(text) if text.strip() else {}

    def account(self):
        return self._req("GET", "/v2/account")

    def place(self, order, trade, now):
        body = {"symbol": order["symbol"], "qty": order["qty"], "side": "buy", "type": order["type"],
                "time_in_force": "day", "order_class": "bracket", "client_order_id": trade["id"],
                "take_profit": {"limit_price": order["take_profit_limit_price"]},
                "stop_loss": {"stop_price": order["stop_loss_stop_price"]}}
        if order.get("limit_price"):
            body["limit_price"] = order["limit_price"]
        r = self._req("POST", "/v2/orders", body)
        trade.update(broker_id=r["id"], status="pending")

    def _order(self, oid):
        return self._req("GET", "/v2/orders/%s?nested=true" % oid)

    def sync(self, trade, now):
        o = self._order(trade["broker_id"])
        filled = float(o.get("filled_qty") or 0)
        if trade["status"] == "pending":
            if o.get("status") == "filled" or (filled and o.get("status") in DEAD):
                trade.update(status="open", qty=int(filled), entry_price=_r2(o["filled_avg_price"]))
            elif o.get("status") in DEAD:
                trade.update(status="void", note="entry %s at Alpaca" % o.get("status"))
                return
        if trade["status"] != "open":
            return
        if trade.get("flatten_id"):
            f = self._order(trade["flatten_id"])
            if f.get("status") == "filled":
                close_trade(trade, float(f["filled_avg_price"]), "flat_by", now)
                return
        for leg in o.get("legs") or []:
            if leg.get("status") == "filled":
                close_trade(trade, float(leg["filled_avg_price"]),
                            "target" if leg.get("type") == "limit" else "stop", now)
                return

    def flatten(self, trade, now):
        o = self._order(trade["broker_id"])
        filled = float(o.get("filled_qty") or 0)
        if not filled:
            if o.get("status") not in DEAD:
                self._req("DELETE", "/v2/orders/%s" % trade["broker_id"])
            trade.update(status="void", note="entry not filled by flat_by; canceled")
            return
        if trade["status"] == "pending":
            trade.update(status="open", qty=int(filled), entry_price=_r2(o["filled_avg_price"]))
        if o.get("status") in OPEN_LEG:
            self._req("DELETE", "/v2/orders/%s" % trade["broker_id"])
        for leg in o.get("legs") or []:
            if leg.get("status") == "filled":
                self.sync(trade, now)
                return
            if leg.get("status") in OPEN_LEG:
                try:
                    self._req("DELETE", "/v2/orders/%s" % leg["id"])
                except AutopilotError:
                    pass                       # already gone; the sell below decides
        if not trade.get("flatten_id"):
            r = self._req("POST", "/v2/orders", {"symbol": trade["symbol"], "qty": str(trade["qty"]),
                                                 "side": "sell", "type": "market", "time_in_force": "day",
                                                 "client_order_id": trade["id"] + "-flat"})
            trade["flatten_id"] = r["id"]
        self.sync(trade, now)


# ---------------------------------------------------------------- data

def yahoo_chart(sym, now):
    time.sleep(entry.PAUSE)
    return entry.get_chart(sym, "5m")


def load_charts(a, cfg):
    """Replay data: --offline FILE/FOLDER, or 60 days from Yahoo for the symbols."""
    syms = [s.strip().upper() for s in (a.symbols or "").split(",") if s.strip()] or cfg["autopilot"]["watchlist"]
    charts = {}
    if a.offline and os.path.isfile(a.offline):
        ch = entry.parse_chart(entry.read_text(a.offline), "")
        charts[ch["symbol"]] = ch
        return charts
    for sym in syms:
        if a.offline:
            charts[sym] = entry.parse_chart(entry.read_text(entry.offline_file(a.offline, sym)), sym)
        else:
            print("fetching 60 days of 5-minute bars for %s ..." % sym, file=sys.stderr)
            charts[sym] = entry.parse_chart(entry.yahoo_get(entry.chart_path(sym, "60d", "5m")), sym)
            time.sleep(entry.PAUSE)
    return charts


def full_days(charts):
    """Sessions every chart has in full (each 5-minute bar from the open to the close)."""
    days = None
    for ch in charts.values():
        count = {}
        for b in ch["bars"]:
            if b["t"] % BAR:
                continue
            et = to_et(entry.utc_of(b["t"]))
            s = entry.session(et.date())
            if s and s[0] <= et.time() < s[1]:
                count[et.date()] = count.get(et.date(), 0) + 1
        mine = set()
        for d, n in count.items():
            o, c = entry.session(d)
            want = (c.hour * 60 + c.minute - o.hour * 60 - o.minute) // 5
            if n == want:
                mine.add(d)
        days = mine if days is None else days & mine
    return sorted(days or [])


def by_day(chart):
    """{date: the chart with only that day's bars}: each replay step reads one session, not sixty."""
    out = {}
    for b in chart["bars"]:
        out.setdefault(to_et(entry.utc_of(b["t"])).date(), []).append(b)
    return {d: dict(chart, bars=bars) for d, bars in out.items()}


def replay(cfg, charts, days, journal, say=None):
    """Walk each day bar by bar (a step at every 5-minute mark) with the simulated broker."""
    wl = [s for s in cfg["autopilot"]["watchlist"] if s in charts] or sorted(charts)
    cfg = dict(cfg, autopilot=dict(cfg["autopilot"], watchlist=wl))
    split = {sym: by_day(ch) for sym, ch in charts.items()}
    sim = SimBroker({})
    bot = Autopilot(cfg, sim, lambda sym, now: sim.charts[sym], journal, say=say)
    for d in days:
        sim.charts = {sym: split[sym][d] for sym in split}
        o, c = entry.session(d)
        t = datetime.datetime.combine(d, o)
        end = datetime.datetime.combine(d, c)
        while t <= end:
            bot.step(entry.eastern_to_utc(t))
            t += datetime.timedelta(minutes=5)
    return journal


def stats(journal):
    closed = [t for t in journal["trades"] if t["status"] == "closed"]
    rs = [t["R"] for t in closed if t["R"] is not None]
    return {"trades": len(closed), "wins": sum(1 for t in closed if t["pnl"] > 0),
            "losses": sum(1 for t in closed if t["pnl"] < 0),
            "by_reason": {k: sum(1 for t in closed if t["exit_reason"] == k) for k in ("target", "stop", "flat_by")},
            "pnl": realized(journal), "avg_R": round(sum(rs) / len(rs), 3) if rs else None,
            "cash": round(journal["budget"] + realized(journal), 2), "budget": journal["budget"]}


def print_stats(journal):
    s = stats(journal)
    print("Trades %d: %d won, %d lost (target %d, stop %d, flat by the close %d)."
          % (s["trades"], s["wins"], s["losses"], s["by_reason"]["target"], s["by_reason"]["stop"],
             s["by_reason"]["flat_by"]))
    if s["trades"]:
        print("P&L %+.2f USD on a %.2f USD budget (%+.1f%%), average %+.3fR. Cash now %.2f USD."
              % (s["pnl"], s["budget"], 100.0 * s["pnl"] / s["budget"], s["avg_R"] or 0, s["cash"]))
    print("Paper / simulated results. Fees, spreads and slippage are not in them. Not financial advice.")


# ---------------------------------------------------------------- commands

def cmd_check(a):
    cfg = load_config(a.config)
    ap = cfg["autopilot"]
    print("Config OK: budget %.2f USD, max loss %.2f a trade, %.2f a day, %d trades a day, entries %s to %s, "
          "flat by %s." % (float(cfg["budget"]), float(cfg["max_loss_per_trade_usd"]),
                           float(cfg["max_daily_loss_usd"]), int(cfg["max_trades_per_day"]),
                           cfg["no_entries_before"], cfg["no_entries_after"], cfg["flat_by"]))
    print("Rule: %d-minute opening-range breakout, stop %s, target %s, on %s."
          % (int(ap["or_minutes"]), ap["stop"], ap["target"], ", ".join(ap["watchlist"])))
    if a.offline_only:
        return 0
    acct = AlpacaPaper(os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")).account()
    print("Alpaca PAPER account %s: status %s, cash %s USD (the autopilot sizes to your budget, not to this)."
          % (acct.get("account_number"), acct.get("status"), acct.get("cash")))
    return 0


def cmd_run(a):
    cfg = load_config(a.config)
    broker = AlpacaPaper(os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY"))
    acct = broker.account()
    journal = load_journal(a.journal, cfg)

    def say(line):
        print(line, flush=True)

    bot = Autopilot(cfg, broker, yahoo_chart, journal, save=lambda j: save_journal(a.journal, j), say=say,
                    kill_file=KILL_FILE)
    now = entry.utc_now()
    bot.say(now, "START  paper account %s, budget %.2f USD, realized so far %+.2f USD, watching %s. "
                 "Kill switch: create %s." % (acct.get("account_number"), float(cfg["budget"]), realized(journal),
                                              ", ".join(cfg["autopilot"]["watchlist"]), KILL_FILE))
    bot.save()
    start_day = to_et(now).date()
    try:
        while True:
            now = entry.utc_now()
            try:
                bot.step(now)
            except (AutopilotError,) + PLAN_ERRORS as e:
                bot.say(now, "error: %s" % e)
            et = to_et(now)
            if a.until_close and not bot.open_trade():
                s = entry.session(start_day)
                if et.date() > start_day or s is None or et.time() >= s[1]:
                    bot.summary(start_day, now)
                    return 0
            time.sleep(float(cfg["autopilot"]["poll_seconds"]))
    except KeyboardInterrupt:
        bot.say(entry.utc_now(), "STOP  by hand. %s" % ("An open position stays at Alpaca with its bracket."
                                                        if bot.open_trade() else "No open position."))
        bot.save()
        return 0


def cmd_replay(a):
    cfg = load_config(a.config)
    if a.budget:
        cfg["budget"] = a.budget
    charts = load_charts(a, cfg)
    days = full_days(charts)
    if a.days:
        days = days[-a.days:]
    if not days:
        raise AutopilotError("no full sessions in the data")
    journal = new_journal(cfg)
    quiet = a.quiet
    replay(cfg, charts, days, journal, say=None if quiet else (lambda line: print(line)))
    print("Replayed %d sessions (%s to %s) on %s." % (len(days), days[0], days[-1], ", ".join(sorted(charts))))
    print_stats(journal)
    if a.out:
        save_journal(a.out, journal)
        print("Journal: %s" % a.out)
    return 0


def cmd_status(a):
    if not os.path.exists(a.journal):
        print("No journal yet at %s." % a.journal)
        return 1
    with open(a.journal, encoding="utf-8") as fh:
        journal = json.load(fh)
    for t in journal["trades"]:
        if t["status"] == "closed":
            print("%s  %-5s %3d @ %8.2f -> %8.2f  %-7s %+8.2f USD  %+.2fR"
                  % (t["date"], t["symbol"], t["qty"], t["entry_price"], t["exit_price"], t["exit_reason"],
                     t["pnl"], t["R"] or 0))
        else:
            print("%s  %-5s %3d  %s" % (t["date"], t["symbol"], t["qty"], t["status"].upper()))
    print_stats(journal)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Autopilot: opening-range breakouts on an Alpaca PAPER account.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "run", "replay", "status"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="trader/config.json")
        if name == "check":
            p.add_argument("--offline-only", action="store_true", help="check the config only, no Alpaca call")
        if name in ("run", "status"):
            p.add_argument("--journal", default="trader/state/autopilot.json")
        if name == "run":
            p.add_argument("--until-close", action="store_true", help="exit after today's close")
        if name == "replay":
            p.add_argument("--offline", metavar="PATH", help="a saved Yahoo chart (.json/.json.gz) or a folder")
            p.add_argument("--symbols", help="comma-separated; default the config's watchlist")
            p.add_argument("--days", type=int, help="only the last N full sessions")
            p.add_argument("--budget", type=float, help="override the budget for this replay")
            p.add_argument("--out", default="trader/state/autopilot-replay.json")
            p.add_argument("--quiet", action="store_true", help="only the summary")
    a = ap.parse_args(argv)
    try:
        return {"check": cmd_check, "run": cmd_run, "replay": cmd_replay, "status": cmd_status}[a.cmd](a)
    except (AutopilotError,) + PLAN_ERRORS as e:
        print("autopilot: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
