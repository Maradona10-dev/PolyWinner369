"""BTC 5m repricing + public event recording + offline replay — PAPER ONLY.
Python 3.12. Start as usual: python bot.py

v7.4: observation candidates and paper execution limits are separate.
Eligible candidates are sampled at most once per 10 seconds, including while
accounts are blocked. Rejected/cancelled candidates do not consume the cap of
two actual paper entries per account per market. Existing fill counts migrate
from closed trades and open positions. Old balances/history are preserved.
Both filled and rejected quotes have hypothetical 1/2/5/15-second follow-ups;
the five-minute report shows separate cohorts, missing observations and costs.
These overlapping observations are not independent trades or account PnL.
The cost guard, sizing, one-tick stress and exit rules have not been relaxed.

The bounded local receive-order journal retains ~1GiB in replay/.
Market changes, BTC updates, atomic L2 snapshots/deltas, reconnects, eligible
signals and 100ms decision clocks are recorded without blocking on disk.
Existing ledgers are preserved. A recording/clock failure blocks NEW entries.

Replay your recorded events (no network/Telegram/ledger writes):
    python bot.py --replay 'replay/tape-*.jsonl' --latency-ms 250 --stake 5
    python bot.py --replay 'replay/tape-*.jsonl' --latency-ms 500 --stake 5
    python bot.py --replay 'replay/tape-*.jsonl' --latency-ms 1000 --stake 5
Adjust replay/ to your BOT_DATA_DIR or Railway volume directory if configured.
Replay varies execution delay/stake with the recorded signal generator fixed.
It never looks at later quotes, assumes a maker fill, or invents settlement.
Unfinished positions/pending orders are explicitly reported at the tape end.
This is an independent taker simulator, not Marketlens SDK integration.
No external historical-data account or API key is required. No performance
claim follows from the synthetic tests; collect real events before comparison.

requirements.txt (existing):
    polymarket-client==0.13.0
    requests>=2.32,<3
    flask>=3,<4
The pinned SDK supplies websockets>=13,<16. No wallet or trading API is used.

Optional environment: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PAPER_TRADE_AMOUNT
(default 7.50), PAPER_BALANCE (default 1000), BOT_DATA_DIR, PORT.
Use a Railway volume for persistence. The separate ledger is
polymarket_latency_comparison.json, with two separate fictitious accounts.
Old polymarket_latency_paper.json is retained; an old position is managed to exit.
Changing PAPER_BALANCE does not reset a ledger. Both comparison arms use the
same net +0.01/share target, -0.04/share loss limit, and 15s maximum hold attempt.
A delayed entry is rejected if immediate round-trip loss is >=0.035/share.
Both sides use their own ask/bid depth, fees and one adverse tick each way.
Markouts at 1/2/5/15s continue after exits and never change account cash.
Missing/late observations remain missing, including across a restart.
Reverse is a research counterfactual, not a claim of positive model edge.

This is an uncalibrated quote-repricing model, NOT an established win probability.
Paper executions use fresh REST depth AFTER a simulated delay, both fees and
one adverse tick per side. They still cannot prove executable live returns.
"""
import asyncio
import contextlib
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import signal
import argparse
import queue
import uuid
from statistics import NormalDist
import threading
import time

import requests
from flask import Flask, jsonify
from polymarket import AsyncPublicClient
from polymarket.streams import MarketSpec
from websockets.asyncio.client import connect

VERSION = "7.4-BTC-Candidate-PAPER"
COMPATIBLE_LEDGERS = {"7.0-BTC-Repricing-PAPER", "7.1-BTC-Repricing-PAPER", "7.2-BTC-Paired-PAPER", "7.3-BTC-Replay-PAPER", VERSION}
STAKE = float(os.getenv("PAPER_TRADE_AMOUNT", "7.50"))
START_BALANCE = float(os.getenv("PAPER_BALANCE", "1000"))
DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("BOT_DATA_DIR", "."))
STATE_PATH = DATA_DIR / "polymarket_latency_paper.json"  # legacy: close existing positions only
LAB_PATH = DATA_DIR / "polymarket_latency_comparison.json"
ARM_NAMES = ("original", "reverse")
MARKOUT_HORIZONS = (1, 2, 5, 15)
MARKOUT_TOLERANCE = .4
ENTRY_STOP_BUFFER = .005
LAB_TAKE_PROFIT = .01
MAX_PAPER_ENTRIES_PER_ROUND = 2
CANDIDATE_INTERVAL = 10  # Sample eligible candidates, also while paper accounts are blocked.
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
# Experimental starting settings, not optimized performance claims.
MIN_GROSS_EDGE = .05
MIN_NET_EDGE = .01
MAX_SPREAD = .025
MODEL_MARGIN = .005
MIN_PRICE, MAX_PRICE = .10, .90
ENTRY_TIME_MIN, ENTRY_TIME_MAX = 45, 270
ANCHOR_AGE, ANCHOR_MAX_AGE = 1.0, 2.0
MIN_BTC_MOVE = .0001                    # 1 basis point
MIN_MODEL_MOVE = .025
EXECUTION_DELAY = .250                  # plus actual REST round-trip time
MAX_ENTRY_WAIT = 1.5
MAX_BTC_AGE, MAX_BOOK_AGE = 1.0, 2.0
MAX_FEED_SKEW = 1.5
VOL_WINDOW_SECONDS = 180
VOL_MIN_SECONDS = 60
VOL_MIN_RETURNS = 30
VOL_MAX_INTERVAL = 10
HISTORY_EVENT_MAX_AGE = 10
BOOK_REPAIR_INTERVAL = 3
BTC_IDLE_RECONNECT = 15
MAX_HOLD_SECONDS = 15
EXIT_BEFORE_EXPIRY = 20
STOP_LOSS_PER_SHARE = .04
MAX_ENTRIES_PER_ROUND = 2
COOLDOWN_SECONDS = 10
DAILY_LOSS_FRACTION = .03
REPORT_SECONDS = 300
SLIPPAGE_TICKS = 1
NORMAL = NormalDist()
LOG = logging.getLogger("repricing")
HEALTH = {"heartbeat": 0., "mode": "PAPER", "version": VERSION}
RECORDER = None


def record(kind, **data):
    if RECORDER is not None:
        RECORDER.emit(kind, **data)


def record_book(direction, book):
    record('book', direction=direction, asset=book.asset, condition=book.condition,
           bids=list(book.bids.items()), asks=list(book.asks.items()),
           source=book.source, received=book.received, tick=book.tick, minimum=book.minimum)


class LedgerError(RuntimeError):
    """Local persistence failed: stop rather than lose paper accounting."""


class Skip(Exception):
    """A named rejection; never silently substitute old data or fake fills."""


def finite(value):
    x = float(value)
    if not math.isfinite(x):
        raise ValueError("Non-finite number")
    return x


def stamp(value):
    return value.timestamp() if isinstance(value, datetime) else finite(value)


def fresh(ts, received, now, limit):
    return ts > 0 and received > 0 and -.5 <= now-ts <= limit and 0 <= now-received <= limit


def fee_unit(price, market):
    if not 0 < price < 1:
        raise Skip("invalid_fee_price")
    return market["fee_rate"] * (price * (1-price)) ** market["fee_exponent"]


@dataclass
class Book:
    asset: str
    condition: str
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    source: float = 0.
    received: float = 0.
    tick: float = 0.
    minimum: float = 0.
    ready: bool = False
    revision: int = 0

    def snapshot(self, payload, now):
        if str(payload.asset_id) != self.asset or str(payload.condition_id) != self.condition:
            raise Skip("wrong_book")
        ts = stamp(payload.timestamp) if payload.timestamp is not None else 0.
        if self.ready and ts < self.source:
            return False
        def levels(items):
            out = {}
            for item in items:
                price, size = finite(item.price), finite(item.size)
                if not 0 < price < 1 or size < 0:
                    raise Skip("invalid_book_level")
                if size > 0:
                    out[price] = size
            return out
        bids, asks = levels(payload.bids), levels(payload.asks)
        tick = getattr(payload, "tick_size", None)
        minimum = getattr(payload, "min_order_size", None)
        if tick is not None:
            self.tick = finite(tick)
        if minimum is not None:
            self.minimum = finite(minimum)
        self.bids, self.asks = bids, asks
        self.source, self.received, self.ready = ts, now, True
        self.revision += 1
        return True

    def delta(self, change, ts, now):
        if str(change.asset_id) != self.asset or not self.ready or ts < self.source:
            return
        side = getattr(change.side, "value", change.side)
        if side not in ("BUY", "SELL"):
            raise Skip("invalid_book_side")
        price, size = finite(change.price), finite(change.size)
        if not 0 < price < 1 or size < 0:
            raise Skip("invalid_book_delta")
        levels = self.bids if side == "BUY" else self.asks
        if size == 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.source, self.received = ts, now
        self.revision += 1

    def check(self, now):
        if not self.ready or not fresh(self.source, self.received, now, MAX_BOOK_AGE):
            raise Skip("book_stale")
        if not self.bids or not self.asks:
            raise Skip("book_empty")
        if max(self.bids) >= min(self.asks):
            raise Skip("book_crossed")
        if not 0 < self.tick < 1 or self.minimum <= 0:
            raise Skip("book_metadata_missing")

    def mid(self):
        return (max(self.bids) + min(self.asks))/2

    def spread(self):
        return min(self.asks)-max(self.bids)


def buy_quote(book, market, budget, now):
    book.check(now)
    remaining, qty, notional, fees = budget, 0., 0., 0.
    raw_notional = slippage_cost = 0.
    for price, size in sorted(book.asks.items()):
        price += SLIPPAGE_TICKS*book.tick
        if price > MAX_PRICE:
            break
        if price < MIN_PRICE:
            raise Skip("price_outside_range")
        unit_fee = fee_unit(price, market)
        take = min(size, remaining/(price+unit_fee))
        qty += take
        notional += take*price
        raw_notional += take*(price-SLIPPAGE_TICKS*book.tick)
        slippage_cost += take*SLIPPAGE_TICKS*book.tick
        fees += take*unit_fee
        remaining -= take*(price+unit_fee)
        if remaining < 1e-8:
            break
    if remaining > 1e-6:
        raise Skip("insufficient_ask_depth")
    # Conservative: retain cost but floor credited shares to 6 decimals.
    qty = math.floor(qty*1_000_000)/1_000_000
    if qty < book.minimum:
        raise Skip("minimum_order_size")
    return dict(qty=qty, spent=notional+fees, fee=fees, price=notional/qty,
                raw_notional=raw_notional, slippage_cost=slippage_cost)


def sell_quote(book, market, qty, now):
    book.check(now)
    remaining, notional, fees = qty, 0., 0.
    raw_notional = slippage_cost = 0.
    for price, size in sorted(book.bids.items(), reverse=True):
        price -= SLIPPAGE_TICKS*book.tick
        if price <= 0:
            continue
        take = min(size, remaining)
        notional += take*price
        raw_notional += take*(price+SLIPPAGE_TICKS*book.tick)
        slippage_cost += take*SLIPPAGE_TICKS*book.tick
        fees += take*fee_unit(price, market)
        remaining -= take
        if remaining < 1e-8:
            break
    if remaining > 1e-6:
        raise Skip("insufficient_bid_depth")
    if qty < book.minimum:
        raise Skip("minimum_exit_size")
    return dict(received=notional-fees, fee=fees, price=notional/qty,
                raw_notional=raw_notional, slippage_cost=slippage_cost)


class Spot:
    def __init__(self):
        self.price = self.source = self.received = 0.
        self.samples = deque(maxlen=600)
        self.segment = 0
        self.event_stats = Counter()
        self.event_ages = deque(maxlen=3000)
        self.last_wire = 0.

    def reset(self, preserve_history=False):
        # A disconnect invalidates the tradable price, not all recent volatility.
        # Never calculate a return spanning a disconnected segment.
        self.price = self.source = self.received = 0.
        self.segment += 1
        if not preserve_history:
            self.samples.clear()

    def update(self, price, ts, now):
        price, ts = finite(price), finite(ts)
        age = now-ts
        self.last_wire = now
        self.event_stats["received"] += 1
        self.event_ages.append(age)
        latest = self.samples[-1][0] if self.samples else 0
        if price <= 0 or ts < max(self.source, latest):
            self.event_stats["out_of_order_or_invalid"] += 1
            return
        if not -.5 <= age <= HISTORY_EVENT_MAX_AGE:
            self.event_stats["too_old_or_future"] += 1
            return
        # Historical observations may be useful even when too old for an entry.
        # check() still applies the original one-second entry freshness limit.
        self.price, self.source, self.received = price, ts, now
        self.event_stats["fresh" if age <= MAX_BTC_AGE else "history_only"] += 1
        second = int(ts)
        point = (second, price, self.segment)
        if self.samples and self.samples[-1][0] == second:
            self.samples[-1] = point
        else:
            self.samples.append(point)
        while self.samples and self.samples[0][0] < ts-VOL_WINDOW_SECONDS:
            self.samples.popleft()

    def check(self, now):
        if not fresh(self.source, self.received, now, MAX_BTC_AGE):
            raise Skip("btc_stale")

    def history_metrics(self):
        # Irregular observed intervals: no invented candles or zero-return fills.
        # Ignore the still-changing latest second and long/disconnected gaps.
        points = list(self.samples)[:-1]
        variance, seconds, returns, excluded = 0., 0., 0, 0
        for (t0, p0, seg0), (t1, p1, seg1) in zip(points, points[1:]):
            dt = t1-t0
            if seg0 != seg1 or dt <= 0 or dt > VOL_MAX_INTERVAL:
                excluded += 1
                continue
            variance += math.log(p1/p0)**2
            seconds += dt
            returns += 1
        return variance, seconds, returns, excluded

    def sigma(self):
        variance, seconds, returns, _ = self.history_metrics()
        if seconds < VOL_MIN_SECONDS or returns < VOL_MIN_RETURNS:
            raise Skip("volatility_warmup")
        return max(1e-6, math.sqrt(variance/seconds))


def model_probability(anchor, spot, now, expiry):
    # Implied digital-price response, anchored BEFORE the observed spot move.
    # No claim that this is the real settlement probability or official strike.
    t0, t1 = expiry-anchor["time"], expiry-now
    sigma = anchor["sigma"]
    if t1 <= 0 or t0 <= 0 or now < anchor["time"] or sigma <= 0:
        raise Skip("model_time")
    p0 = min(.95, max(.05, anchor["p"]))
    numerator = NORMAL.inv_cdf(p0)*sigma*math.sqrt(t0) + math.log(spot/anchor["spot"])
    return min(.995, max(.005, NORMAL.cdf(numerator/(sigma*math.sqrt(t1)))))


def entry_assessment(book, market, direction, anchor, spot_price, budget, now):
    remaining = market["expiry"]-now
    if not ENTRY_TIME_MIN <= remaining <= ENTRY_TIME_MAX:
        raise Skip("outside_entry_window")
    if not 0 <= now-anchor["time"] <= ANCHOR_MAX_AGE+MAX_ENTRY_WAIT:
        raise Skip("anchor_expired")
    up = model_probability(anchor, spot_price, now, market["expiry"])
    ref = up if direction == "UP" else 1-up
    base = anchor["p"] if direction == "UP" else 1-anchor["p"]
    move = math.log(spot_price/anchor["spot"])*(1 if direction == "UP" else -1)
    if move < MIN_BTC_MOVE or ref-base < MIN_MODEL_MOVE:
        raise Skip("no_btc_impulse")
    book.check(now)
    if book.spread() > MAX_SPREAD:
        raise Skip("spread")
    quote = buy_quote(book, market, budget, now)
    if ref-quote["price"] < MIN_GROSS_EDGE:
        raise Skip("gross_edge")
    # Quote scenario: fair mid less half-spread, uncertainty and adverse sell tick.
    target_bid = min(.99, ref-book.spread()/2-MODEL_MARGIN)
    target_fill = target_bid-SLIPPAGE_TICKS*book.tick
    if target_fill <= 0:
        raise Skip("target_price")
    edge = target_fill-fee_unit(target_fill, market)-quote["spent"]/quote["qty"]
    if edge < MIN_NET_EDGE:
        raise Skip("edge_after_costs")
    sell_quote(book, market, quote["qty"], now)  # current full-size exit depth
    return quote, ref, target_bid, edge


class Ledger:
    def __init__(self, path=STATE_PATH):
        self.path = Path(path)
        self.data = dict(schema=1, strategy=VERSION, cash=START_BALANCE,
                         initial=START_BALANCE, position=None, trades=[], pending=None,
                         round_counts={}, last_exit=0., day="", day_start=START_BALANCE,
                         daily_stopped=False)
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if self.data.get("schema") != 1 or self.data.get("strategy") not in COMPATIBLE_LEDGERS:
                raise LedgerError("Onbekende ledger; bestaande data niet overschreven")
            self.validate()
        self.data["strategy"] = VERSION
        # A paper request interrupted before its atomic fill never filled.
        self.data["pending"] = None
        self.save()

    def validate(self):
        d = self.data
        if finite(d["cash"]) < -1e-6 or finite(d["initial"]) <= 0:
            raise LedgerError("Ongeldig paper saldo")
        expected = d["initial"] + sum(finite(t["pnl"]) for t in d["trades"])
        if d["position"]:
            expected -= finite(d["position"]["spent"])
        if abs(expected-d["cash"]) > 1e-5:
            raise LedgerError("Ledger sluit niet aan; herstel data voor herstart")

    def save(self):
        try:
            self._save()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LedgerError("Paperadministratie kon niet veilig worden opgeslagen") from exc

    def _save(self):
        self.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(self.data, f, allow_nan=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def roll_day(self, now):
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if day != self.data["day"]:
            # Realized-balance baseline (not a hard mark-to-market loss cap).
            p = self.data["position"]
            self.data.update(day=day, day_start=self.data["cash"]+(p["spent"] if p else 0),
                             daily_stopped=False)
            self.save()

    def allowed(self, market, now):
        d = self.data
        if d["position"] or d["daily_stopped"] or d["cash"] < STAKE:
            raise Skip("portfolio_limit")
        if now-d["last_exit"] < COOLDOWN_SECONDS:
            raise Skip("cooldown")
        if d["round_counts"].get(market["slug"], 0) >= MAX_ENTRIES_PER_ROUND:
            raise Skip("round_limit")

    def enter(self, position):
        if self.data["position"] or position["spent"] > self.data["cash"]:
            raise LedgerError("Invalid paper entry")
        self.data["cash"] -= position["spent"]
        self.data["position"] = position
        self.data["pending"] = None
        counts = self.data["round_counts"]
        counts[position["market"]["slug"]] = counts.get(position["market"]["slug"], 0)+1
        if len(counts) > 100:
            self.data["round_counts"] = dict(list(counts.items())[-50:])
        self.save()

    def close(self, proceeds, fee, reason, now):
        p = self.data["position"]
        if not p or proceeds < 0:
            raise LedgerError("Invalid paper exit")
        trade = dict(direction=p["direction"], market=p["market"]["slug"],
                     entry_time=p["entered"], exit_time=now, seconds=now-p["entered"],
                     spent=p["spent"], received=proceeds, qty=p["qty"],
                     fees=p["fee"]+fee, pnl=proceeds-p["spent"], reason=reason)
        self.data["cash"] += proceeds
        self.data["trades"].append(trade)
        self.data.update(position=None, pending=None, last_exit=now)
        if self.data["cash"] <= self.data["day_start"]*(1-DAILY_LOSS_FRACTION):
            self.data["daily_stopped"] = True
        self.save()
        return trade


async def discover(client, cycle):
    slug = f"btc-updown-5m-{cycle}"
    m = await asyncio.wait_for(client.get_market(slug=slug), 8)
    s = m.state
    if m.slug != slug or not m.condition_id or s.end_date is None:
        raise Skip("wrong_market")
    if abs(s.end_date.timestamp()-(cycle+300)) > 1:
        raise Skip("wrong_expiry")
    if s.closed or not s.active or not s.accepting_orders or not s.enable_order_book:
        raise Skip("market_not_tradable")
    f = m.trading.fee_schedule
    if m.trading.fees_enabled is not False and f is None:
        raise Skip("fee_unknown")
    rate = finite(f.rate) if f and m.trading.fees_enabled is not False else 0.
    exponent = finite(f.exponent) if f else 1.
    if not 0 <= rate <= 1 or not 0 <= exponent <= 10:
        raise Skip("fee_invalid")
    assets, indices = {}, {}
    version = getattr(m.version, "value", m.version)
    if version not in ("v1", "v2"):
        raise Skip("unsupported_market_version")
    for i, outcome in enumerate((m.outcomes.yes, m.outcomes.no)):
        label = outcome.label.strip().upper()
        asset = outcome.position_id if version == "v2" else outcome.token_id
        if label not in ("UP", "DOWN") or not asset:
            raise Skip("outcome_mapping")
        assets[label], indices[label] = str(asset), i
    if len(assets) != 2 or len(set(assets.values())) != 2:
        raise Skip("outcome_mapping")
    return dict(slug=slug, condition=str(m.condition_id), expiry=cycle+300,
                assets=assets, indices=indices, fee_rate=rate, fee_exponent=exponent)


def resolved_payout(resolutions, market, direction):
    matches = [r for r in resolutions if str(r.condition_id) == market["condition"]
               and getattr(r.status, "value", r.status) == "resolved"]
    if len(matches) != 1 or matches[0].payouts is None:
        return None
    payouts = list(map(finite, matches[0].payouts))
    if len(payouts) != 2 or any(p < 0 or p > 1 for p in payouts) or abs(sum(payouts)-1) > 1e-8:
        raise Skip("invalid_resolution")
    return payouts[market["indices"][direction]]


class Engine:
    def __init__(self, client, ledger):
        self.client, self.ledger = client, ledger
        self.spot = Spot()
        self.market, self.books, self.stream_task = None, {}, None
        self.anchors = deque(maxlen=80)
        self.forecasts = deque(maxlen=100)
        self.scores = dict(n=0, model_error=0., baseline_error=0.)
        self.stats = Counter()
        self.messages = asyncio.Queue(maxsize=100)
        self.last_anchor = self.last_forecast = self.last_report = 0.
        self.last_exit_attempt = self.last_resolve = self.next_discovery = 0.
        self.stream_ok = False
        self.stream_connected = False
        self.last_book_event = 0.
        self.book_diagnostics = {}
        self.last_repair = {}

    def notify(self, text):
        LOG.info(text)
        with contextlib.suppress(asyncio.QueueFull):
            self.messages.put_nowait(text)

    async def telegram_worker(self):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().removeprefix("bot")
        chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        def send(text):
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat, "text": text,
                                        "disable_web_page_preview": True}, timeout=8)
                if not r.ok or not r.json().get("ok"):
                    LOG.warning("Telegram bericht niet afgeleverd (HTTP %s)", r.status_code)
            except Exception:
                LOG.warning("Telegram tijdelijk niet bereikbaar")  # no secret-bearing URL
        while True:
            message = await self.messages.get()
            if token and chat:
                await asyncio.to_thread(send, message)
            self.messages.task_done()

    async def spot_stream(self):
        delay = 1
        while True:
            try:
                async with connect(BINANCE_WS, ping_interval=20, ping_timeout=20,
                                   open_timeout=10, max_queue=32) as ws:
                    self.spot.reset(preserve_history=True)
                    record('spot_reset')
                    self.anchors.clear()
                    delay = 1
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), BTC_IDLE_RECONNECT)
                        event = json.loads(raw)
                        if event.get("e") == "aggTrade" and event.get("s") == "BTCUSDT":
                            self.spot.update(event["p"], finite(event["T"])/1000, time.time())
                            record('spot', price=self.spot.price, source=self.spot.source,
                                   received=self.spot.received)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["btc_reconnect"] += 1
                LOG.warning("BTC stream: %s", type(exc).__name__)
            self.spot.reset(preserve_history=True)
            record('spot_reset')
            self.anchors.clear()
            await asyncio.sleep(delay)
            delay = min(30, delay*2)

    async def rest_book(self, direction, *, market=None, validate=True):
        market = self.market if market is None else market
        asset = market["assets"][direction]
        payload = await asyncio.wait_for(self.client.get_order_book(asset_id=asset), 3)
        book = Book(asset, market["condition"])
        book.snapshot(payload, time.time())
        if validate:
            book.check(time.time())
        return book

    async def market_stream(self, market):
        delay = 1
        while self.market == market:
            try:
                self.stream_ok = False
                self.stream_connected = False
                self.anchors.clear()
                # REST metadata then wait for the stream's full snapshots.
                for direction in ("UP", "DOWN"):
                    b = await self.rest_book(direction, market=market, validate=False)
                    b.ready = False
                    self.books[direction] = b
                spec = MarketSpec(asset_ids=list(market["assets"].values()), custom_feature_enabled=True)
                async with await self.client.subscribe(spec) as stream:
                    delay = 1
                    self.stream_connected = True
                    self.last_book_event = time.time()
                    while True:
                        event = await asyncio.wait_for(anext(stream), 15)
                        if getattr(stream, "dropped", 0):
                            raise Skip("stream_messages_dropped")
                        payload, now = event.payload, time.time()
                        if str(getattr(payload, "condition_id", "")) != market["condition"]:
                            continue
                        self.last_book_event = now
                        if event.type == "book":
                            for b in self.books.values():
                                if str(payload.asset_id) == b.asset:
                                    if b.snapshot(payload, now):
                                        record_book(next(d for d,v in self.books.items() if v is b), b)
                        elif event.type == "price_change":
                            ts = stamp(payload.timestamp) if payload.timestamp is not None else 0.
                            updates = []
                            for change in payload.price_changes:
                                for b in self.books.values():
                                    revision = b.revision
                                    b.delta(change, ts, now)
                                    if b.revision != revision:
                                        updates.append(dict(direction=next(d for d,v in self.books.items() if v is b),
                                                            price=finite(change.price), size=finite(change.size),
                                                            side=getattr(change.side, 'value', change.side),
                                                            source=b.source, received=b.received))
                            if updates:
                                record('deltas', updates=updates)
                        elif event.type == "tick_size_change":
                            for b in self.books.values():
                                if str(payload.asset_id) == b.asset:
                                    b.tick = finite(payload.new_tick_size)
                                    b.revision += 1
                                    record_book(next(d for d,v in self.books.items() if v is b), b)
                        self.stream_ok = all(b.ready for b in self.books.values())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["market_reconnect"] += 1
                LOG.warning("Polymarket stream: %s", type(exc).__name__)
            self.stream_ok = False
            self.stream_connected = False
            record('book_reset')
            self.anchors.clear()
            for b in self.books.values():
                b.ready = False
            await asyncio.sleep(delay)
            delay = min(20, delay*2)

    async def switch_market(self, market):
        if self.stream_task:
            self.stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.stream_task
        self.market, self.books = market, {}
        record('market', market=market)
        self.anchors.clear()
        self.forecasts.clear()
        self.stream_ok = False
        self.stream_connected = False
        self.last_repair.clear()
        self.book_diagnostics.clear()
        self.stream_task = asyncio.create_task(self.market_stream(market))

    async def repair_book(self, direction, market):
        before = self.books.get(direction)
        revision = before.revision if before else -1
        try:
            candidate = await self.rest_book(direction, market=market, validate=False)
            if self.market != market or not self.stream_connected:
                return
            live = self.books.get(direction)
            # Do not overwrite a more recent stream update with an in-flight REST response.
            changed = live is not before or (live and live.revision != revision)
            if live and (candidate.source < live.source or
                         (changed and candidate.source <= live.source)):
                self.stats["repair_older_than_stream"] += 1
                return
            candidate.revision = (live.revision+1) if live else 1
            self.books[direction] = candidate
            record_book(direction, candidate)
            self.stream_ok = len(self.books) == 2 and all(b.ready for b in self.books.values())
            self.anchors.clear()  # new model anchor after resynchronization
            self.forecasts.clear()
            try:
                candidate.check(time.time())
                status = "hersteld"
                self.stats["book_repaired"] += 1
            except Skip as exc:
                status = str(exc)
                self.stats["rest_confirmed:"+status] += 1
            self.book_diagnostics[direction] = status
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = "REST:"+type(exc).__name__
            self.book_diagnostics[direction] = status
            self.stats["repair_failed:"+type(exc).__name__] += 1

    async def repair_books(self):
        while True:
            market = self.market
            if market and time.time() < market["expiry"] and self.stream_connected:
                tasks = []
                for direction in ("UP", "DOWN"):
                    try:
                        if direction not in self.books:
                            raise Skip("book_missing")
                        self.books[direction].check(time.time())
                    except Skip:
                        key = (market["slug"], direction)
                        if time.monotonic()-self.last_repair.get(key, -100) >= BOOK_REPAIR_INTERVAL:
                            self.last_repair[key] = time.monotonic()
                            tasks.append(self.repair_book(direction, market))
                if tasks:
                    await asyncio.gather(*tasks)
            await asyncio.sleep(.5)

    def paired_mid(self, now):
        self.spot.check(now)
        if not self.stream_ok or len(self.books) != 2:
            raise Skip("market_stream_warmup")
        for b in self.books.values():
            b.check(now)
            if abs(b.source-self.spot.source) > MAX_FEED_SKEW:
                raise Skip("feed_time_skew")
            if b.spread() > MAX_SPREAD:
                raise Skip("spread")
        up, down = self.books["UP"].mid(), self.books["DOWN"].mid()
        if abs(up+down-1) > .04:
            raise Skip("inconsistent_outcomes")
        return up/(up+down)

    def observe(self, now):
        try:
            p = self.paired_mid(now)
            sigma = self.spot.sigma()
        except Skip:
            self.anchors.clear()
            self.forecasts.clear()
            raise
        if now-self.last_anchor >= .1:
            self.anchors.append(dict(time=now, p=p, spot=self.spot.price, sigma=sigma))
            self.last_anchor = now
        while self.forecasts and self.forecasts[0][0] <= now:
            due, prediction, baseline = self.forecasts.popleft()
            if now-due <= .5:
                self.scores["n"] += 1
                self.scores["model_error"] += abs(prediction-p)
                self.scores["baseline_error"] += abs(baseline-p)
        eligible = [a for a in self.anchors if ANCHOR_AGE <= now-a["time"] <= ANCHOR_MAX_AGE]
        if not eligible:
            raise Skip("anchor_warmup")
        anchor = eligible[-1]
        if now-self.last_forecast >= 1:
            prediction = model_probability(anchor, self.spot.price, now, self.market["expiry"])
            self.forecasts.append((now+2, prediction, p))
            self.last_forecast = now
        return anchor

    async def enter(self, direction, anchor):
        now = time.time()
        self.ledger.allowed(self.market, now)
        self.spot.check(now)
        entry_assessment(self.books[direction], self.market, direction, anchor, self.spot.price, STAKE, now)
        self.stats["candidate"] += 1
        started = time.monotonic()
        self.ledger.data["pending"] = dict(direction=direction, time=now)
        self.ledger.save()
        try:
            await asyncio.sleep(EXECUTION_DELAY)
            book = await self.rest_book(direction)
            now = time.time()
            if time.monotonic()-started > MAX_ENTRY_WAIT:
                raise Skip("entry_too_slow")
            self.spot.check(now)
            if anchor not in self.anchors:
                raise Skip("anchor_invalidated")
            if abs(book.source-self.spot.source) > MAX_FEED_SKEW:
                raise Skip("feed_time_skew")
            self.ledger.allowed(self.market, now)
            quote, ref, target, edge = entry_assessment(book, self.market, direction, anchor,
                                                       self.spot.price, STAKE, now)
            position = dict(**quote, direction=direction, market=self.market.copy(),
                            anchor=anchor.copy(), target=target, reference_entry=ref,
                            entered=now, delay_ms=(time.monotonic()-started)*1000)
            self.ledger.enter(position)
            self.stats["entry"] += 1
            self.notify(f"PAPER {direction} BTC 5m — HERPRIJZING\n"
                        f"All-in ${quote['spent']:.2f} | {quote['qty']:.6f} aandelen\n"
                        f"Koop VWAP {quote['price']:.4f} | model {ref:.4f}\n"
                        f"Doel-bid {target:.4f} | geschatte netto ruimte {edge:.4f}/aandeel\n"
                        f"Vertraging incl. REST: {position['delay_ms']:.0f} ms\n"
                        f"https://polymarket.com/event/{self.market['slug']}")
        except Skip as exc:
            self.stats["cancel_after_delay:"+str(exc)] += 1
        finally:
            self.ledger.data["pending"] = None
            self.ledger.save()

    def exit_reason(self, p, now):
        if now-p["entered"] >= MAX_HOLD_SECONDS:
            return "time_limit"
        if p["market"]["expiry"]-now <= EXIT_BEFORE_EXPIRY:
            return "before_expiry"
        try:
            self.spot.check(now)
            up = model_probability(p["anchor"], self.spot.price, now, p["market"]["expiry"])
            ref = up if p["direction"] == "UP" else 1-up
            if ref < p["reference_entry"]-.025:
                return "model_reversal"
        except Skip:
            return "data_stale"
        try:
            b = self.books[p["direction"]]
            quote = sell_quote(b, p["market"], p["qty"], now)
            if max(b.bids) >= p["target"]:
                return "repriced"
            if (quote["received"]-p["spent"])/p["qty"] <= -STOP_LOSS_PER_SHARE:
                return "loss_limit"
        except (Skip, KeyError):
            return "data_stale"
        return None

    async def manage_position(self):
        p, now = self.ledger.data["position"], time.time()
        if now >= p["market"]["expiry"]:
            if now-self.last_resolve >= 20:
                self.last_resolve = now
                rs = await asyncio.wait_for(self.client.get_resolutions(condition_ids=p["market"]["condition"]), 5)
                payout = resolved_payout(rs, p["market"], p["direction"])
                if payout is not None:
                    t = self.ledger.close(p["qty"]*payout, 0., "resolution_fallback", time.time())
                    self.notify(f"PAPER noodafrekening na expiry: ${t['pnl']:+.2f}. "
                                "Geen tijdige verkoop mogelijk; officiële uitkomst gebruikt.")
            return
        reason = self.exit_reason(p, now)
        if not reason or now-self.last_exit_attempt < 1:
            return
        self.last_exit_attempt = now
        await asyncio.sleep(EXECUTION_DELAY)
        if time.time() >= p["market"]["expiry"]:
            return
        book = await self.rest_book(p["direction"])
        now = time.time()
        if now >= p["market"]["expiry"]:
            return
        quote = sell_quote(book, p["market"], p["qty"], now)
        # Once triggered we execute at refreshed bids, even if price deteriorated.
        t = self.ledger.close(quote["received"], quote["fee"], reason, now)
        self.stats["exit:"+reason] += 1
        self.notify(f"PAPER {p['direction']} GESLOTEN: {reason}\n"
                    f"Verkoop VWAP {quote['price']:.4f} | netto ${t['pnl']:+.2f}\n"
                    f"Beide fees ${t['fees']:.4f} | duur {t['seconds']:.1f}s\n"
                    f"Vrij paper saldo ${self.ledger.data['cash']:.2f}")

    def report(self):
        now = time.time()
        d, s = self.ledger.data, self.scores
        trades = d["trades"]
        msg = (f"{VERSION}\nOpen {int(d['position'] is not None)} | gesloten {len(trades)} | "
               f"winst {sum(t['pnl']>0 for t in trades)}\n"
               f"Vrij ${d['cash']:.2f} | netto gerealiseerd ${sum(t['pnl'] for t in trades):+.2f}\n"
               f"Controles sinds vorig rapport (herhalingen mogelijk): {dict(self.stats)}\n")
        _, seconds, returns, excluded = self.spot.history_metrics()
        msg += (f"BTC historie: {seconds:.0f}/{VOL_MIN_SECONDS}s bruikbaar, "
                f"{returns}/{VOL_MIN_RETURNS} rendementen, {excluded} intervallen overgeslagen.\n")
        if self.spot.event_ages:
            ages = sorted(self.spot.event_ages)
            p95 = ages[min(len(ages)-1, int(len(ages)*.95))]
            msg += (f"BTC berichtleeftijd p50/p95: {ages[len(ages)//2]*1000:.0f}/"
                    f"{p95*1000:.0f} ms | berichten {dict(self.spot.event_stats)}\n")
        msg += f"Huidige BTC-prijsleeftijd: {now-self.spot.source:.2f}s\n" if self.spot.source else "BTC: wacht op verse prijs\n"
        for direction, b in self.books.items():
            bid = f"{max(b.bids):.3f}" if b.bids else "leeg"
            ask = f"{min(b.asks):.3f}" if b.asks else "leeg"
            msg += (f"{direction}: bid {bid}, ask {ask}, niveaus {len(b.bids)}/{len(b.asks)}, "
                    f"leeftijd {now-b.source:.1f}s | REST {self.book_diagnostics.get(direction, 'niet nodig')}\n")
        if s["n"]:
            msg += (f"2s quotevoorspelling, n={s['n']}: fout model {s['model_error']/s['n']:.4f}, "
                    f"onveranderde quote {s['baseline_error']/s['n']:.4f}. Lager is beter.\n")
        msg += "PAPER: geschatte fills/kosten; modelkans is ongekalibreerd. Geen live orders."
        self.notify(msg[:3900])
        self.stats.clear()
        self.spot.event_stats.clear()
        self.spot.event_ages.clear()

    async def run(self):
        self.notify(f"{VERSION} gestart\nBTC-only | inzet ${STAKE:.2f} | realtime feeds\n"
                    f"Bruto edge ≥{MIN_GROSS_EDGE:.2f}, netto ruimte ≥{MIN_NET_EDGE:.2f}/aandeel\n"
                    f"Max. houdtijd {MAX_HOLD_SECONDS}s (verkooppoging, geen vulgarantie).\n"
                    "Feedherstel actief; bestaande paperhistorie en posities blijven behouden.")
        jobs = [asyncio.create_task(self.spot_stream()), asyncio.create_task(self.telegram_worker()),
                asyncio.create_task(self.repair_books())]
        try:
            while True:
                now = time.time()
                HEALTH["heartbeat"] = now
                try:
                    self.ledger.roll_day(now)
                    p = self.ledger.data["position"]
                    if p and self.market != p["market"]:
                        await self.switch_market(p["market"])
                    if not p and (not self.market or now >= self.market["expiry"]):
                        if now < self.next_discovery:
                            raise Skip("discovery_backoff")
                        self.next_discovery = now+5
                        await self.switch_market(await discover(self.client, int(now//300)*300))
                    if p:
                        await self.manage_position()
                    else:
                        self.ledger.allowed(self.market, now)
                        if not ENTRY_TIME_MIN <= self.market["expiry"]-now <= ENTRY_TIME_MAX:
                            raise Skip("outside_entry_window")
                        anchor = self.observe(now)
                        direction = "UP" if self.spot.price > anchor["spot"] else "DOWN"
                        await self.enter(direction, anchor)
                except Skip as exc:
                    self.stats[str(exc)] += 1
                except LedgerError:
                    # Persistence/invariant failures are fatal; do not continue on bad state.
                    LOG.exception("Fatale lokale fout; paperproces stopt")
                    raise
                except Exception as exc:
                    self.stats["api_error:"+type(exc).__name__] += 1
                    LOG.warning("API tijdelijk niet beschikbaar: %s", type(exc).__name__)
                    await asyncio.sleep(1)
                if time.time()-self.last_report >= REPORT_SECONDS:
                    self.report()
                    self.last_report = time.time()
                await asyncio.sleep(.1)
        finally:
            if self.stream_task:
                jobs.append(self.stream_task)
            for task in jobs:
                task.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            self.ledger.save()



def cost_guard(book, market, quote, now):
    """Reject an entry already at/near its all-in stop, never hide its fees."""
    immediate = sell_quote(book, market, quote['qty'], now)
    loss = (quote['spent']-immediate['received'])/quote['qty']
    if loss >= STOP_LOSS_PER_SHARE-ENTRY_STOP_BUFFER:
        raise Skip('roundtrip_cost_near_stop')
    return immediate, loss


def cost_breakdown(book, quote, immediate):
    """All components in dollars per credited share; sum equals immediate net loss.

    Depth includes conservative share-floor rounding. Slippage is the configured
    extra tick stress, not measured slippage. Fees use the market's fee schedule.
    """
    qty = quote['qty']
    spread = book.spread()
    fees = (quote['fee']+immediate['fee'])/qty
    slippage = (quote['slippage_cost']+immediate['slippage_cost'])/qty
    total = (quote['spent']-immediate['received'])/qty
    return dict(spread=spread, fees=fees, slippage=slippage,
                depth_and_rounding=total-spread-fees-slippage, total=total)


def candidate_summary(data):
    """Descriptive per-arm cohorts; overlapping observations are not independent trades."""
    results = {}
    for name in ARM_NAMES:
        results[name] = {}
        for cohort in ('filled', 'rejected'):
            trials = [t for t in data['trials'] if t.get('arms',{}).get(name,{}).get('status') == cohort]
            costs = [t['arms'][name]['costs'] for t in trials if t['arms'][name].get('costs')]
            summary = dict(candidates=len(trials), cost_n=len(costs), costs={}, horizons={})
            if costs:
                summary['costs'] = {key:sum(c[key] for c in costs)/len(costs) for key in costs[0]}
            for horizon in MARKOUT_HORIZONS:
                key = str(horizon)
                samples = [t.get('markouts',{}).get(key,{}).get('arms',{}).get(name) for t in trials]
                observed = [s for s in samples if s and s['status']=='observed']
                priced = [s for s in observed if 'price_pnl' in s and 'total_fees' in s]
                missing = sum(bool(s and s['status']!='observed') for s in samples)
                summary['horizons'][key] = dict(n=len(observed), missing=missing,
                    pending=sum(s is None for s in samples),
                    net_mean=sum(s['hypothetical_net'] for s in observed)/len(observed) if observed else None,
                    price_mean=sum(s['price_pnl'] for s in priced)/len(priced) if priced else None,
                    fees_mean=sum(s['total_fees'] for s in priced)/len(priced) if priced else None,
                    priced_n=len(priced),
                    positive=sum(s['hypothetical_net']>0 for s in observed))
            results[name][cohort] = summary
    return results


def lab_summary(data):
    """Full-account and matched-cohort results are deliberately separate."""
    totals = {}
    for name, account in data['arms'].items():
        rows = account['trades']
        totals[name] = dict(n=len(rows), wins=sum(t['pnl'] > 0 for t in rows),
                            pnl=sum(t['pnl'] for t in rows), fees=sum(t['fees'] for t in rows))
    maps = {name:{t['trial_id']:t for t in a['trades'] if t.get('trial_id') is not None}
            for name,a in data['arms'].items()}
    paired_ids = set(maps['original']) & set(maps['reverse'])
    paired = dict(n=len(paired_ids), **{name:sum(maps[name][i]['pnl'] for i in paired_ids)
                                      for name in ARM_NAMES})
    return totals, paired


class ComparisonLedger:
    """One atomic file: two accounts, paired decisions, follow-up quote observations."""
    def __init__(self, path=LAB_PATH):
        self.path = Path(path)
        def account():
            return dict(initial=START_BALANCE, cash=START_BALANCE, position=None, trades=[],
                        day='', day_start=START_BALANCE, daily_stopped=False)
        self.data = dict(schema=2, experiment='paired-v1', created=time.time(),
                         arms={name:account() for name in ARM_NAMES}, trials=[], next_id=1,
                         round_counts={}, last_trial=0., last_exit=0.)
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if self.data.get('schema') != 2 or self.data.get('experiment') != 'paired-v1':
                raise LedgerError('Onbekende vergelijking; bestand niet overschreven')
        # Migrate from the old attempt cap. Derive actual fills from accounting;
        # rejected/cancelled candidates never spend the paper-entry allowance.
        if 'fill_counts' not in self.data:
            self.data['fill_counts'] = {name:{} for name in ARM_NAMES}
            for name,a in self.data['arms'].items():
                counts = self.data['fill_counts'][name]
                for t in a['trades']:
                    counts[t['market']] = counts.get(t['market'],0)+1
                p = a['position']
                if p:
                    slug = p['market']['slug']
                    counts[slug] = counts.get(slug,0)+1
        self.validate()
        # Atomic fills are either present in both account state and trial, or absent.
        for trial in self.data['trials']:
            if trial['status'] == 'pending':
                trial.update(status='interrupted', reason='restart_before_fill')
        self.save()

    def validate(self):
        d = self.data
        if set(d['arms']) != set(ARM_NAMES):
            raise LedgerError('Vergelijkingsarmen ontbreken')
        ids = [t['id'] for t in d['trials']]
        if len(ids) != len(set(ids)):
            raise LedgerError('Dubbele trial-ID')
        for a in d['arms'].values():
            if finite(a['cash']) < -1e-6 or finite(a['initial']) <= 0:
                raise LedgerError('Ongeldig vergelijkingssaldo')
            expected = a['initial']+sum(finite(t['pnl']) for t in a['trades'])
            if a['position']:
                expected -= finite(a['position']['spent'])
            if abs(expected-a['cash']) > 1e-5:
                raise LedgerError('Vergelijkingssaldo sluit niet aan')

    def save(self):
        try:
            self.validate()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            with tmp.open('w') as f:
                json.dump(self.data, f, allow_nan=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise LedgerError('Vergelijking niet veilig opgeslagen') from exc

    def roll_day(self, now):
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        changed = False
        for a in self.data['arms'].values():
            if a['day'] != day:
                p = a['position']
                a.update(day=day, day_start=a['cash']+(p['spent'] if p else 0), daily_stopped=False)
                changed = True
        if changed:
            self.save()

    def allowed(self, market, now):
        d = self.data
        if any(a['position'] for a in d['arms'].values()):
            raise Skip('comparison_position_open')
        if any(t['status'] == 'pending' for t in d['trials'][-2:]):
            raise Skip('comparison_pending')
        if all(a['daily_stopped'] or a['cash'] < STAKE for a in d['arms'].values()):
            raise Skip('both_accounts_blocked')
        if now-max(d['last_trial'],d['last_exit']) < COOLDOWN_SECONDS:
            raise Skip('comparison_cooldown')
        if all(d['fill_counts'][name].get(market['slug'],0) >= MAX_PAPER_ENTRIES_PER_ROUND for name in ARM_NAMES):
            raise Skip('comparison_round_limit')

    def observation_allowed(self, market, now):
        if any(t['status']=='pending' for t in self.data['trials'][-2:]):
            raise Skip('comparison_pending')
        if now-self.data['last_trial'] < CANDIDATE_INTERVAL:
            raise Skip('candidate_sampling_interval')

    def begin(self, market, direction, now):
        self.observation_allowed(market, now)
        trial = dict(id=self.data['next_id'], market=market.copy(), signal=direction,
                     created=now, status='pending', arms={}, markouts={},
                     policy=dict(stake=STAKE, target=LAB_TAKE_PROFIT, stop=STOP_LOSS_PER_SHARE,
                                 stop_buffer=ENTRY_STOP_BUFFER, hold_seconds=MAX_HOLD_SECONDS,
                                 execution_delay=EXECUTION_DELAY, slippage_ticks=SLIPPAGE_TICKS))
        trial['policy']['version'] = VERSION
        self.data['next_id'] += 1
        self.data['trials'].append(trial)
        self.data['last_trial'] = now
        self.save()
        return trial

    def cancel(self, trial, reason):
        trial.update(status='cancelled', reason=reason)
        self.save()

    def commit(self, trial, decisions, now):
        if trial['status'] != 'pending' or set(decisions) != set(ARM_NAMES):
            raise LedgerError('Ongeldige vergelijkingstransactie')
        # Validate every arm before any balance changes.
        for name,r in decisions.items():
            a = self.data['arms'][name]
            if r['status'] == 'filled' and (a['position'] or r['quote']['spent'] > a['cash']):
                raise LedgerError('Dubbele of ongedekte paperpositie')
            if r['status']=='filled' and self.data['fill_counts'][name].get(trial['market']['slug'],0) >= MAX_PAPER_ENTRIES_PER_ROUND:
                raise LedgerError('Paperinstaplimiet overschreden')
        trial.update(status='observing', entered=now, arms=decisions)
        for name,r in decisions.items():
            if r['status'] != 'filled':
                continue
            q = r['quote']
            a = self.data['arms'][name]
            a['cash'] -= q['spent']
            a['position'] = dict(**q, trial_id=trial['id'], direction=r['direction'],
                                 market=trial['market'].copy(), entered=now,
                                 immediate_exit=r['immediate_exit'], baseline_loss=r['baseline_loss'])
            counts = self.data['fill_counts'][name]
            slug = trial['market']['slug']
            counts[slug] = counts.get(slug,0)+1
            if len(counts) > 100:
                self.data['fill_counts'][name] = dict(list(counts.items())[-50:])
        self.save()

    def close(self, name, proceeds, fee, reason, now):
        a = self.data['arms'][name]
        p = a['position']
        proceeds, fee = finite(proceeds), finite(fee)
        if not p or proceeds < 0 or fee < 0:
            raise LedgerError('Ongeldige vergelijkingsuitstap')
        t = dict(trial_id=p['trial_id'], direction=p['direction'], market=p['market']['slug'],
                 entry_time=p['entered'], exit_time=now, seconds=now-p['entered'], qty=p['qty'],
                 spent=p['spent'], received=proceeds, fees=p['fee']+fee,
                 pnl=proceeds-p['spent'], reason=reason)
        t['price_pnl'] = t['pnl']+t['fees']
        a['cash'] += proceeds
        a['trades'].append(t)
        a['position'] = None
        if a['cash'] <= a['day_start']*(1-DAILY_LOSS_FRACTION):
            a['daily_stopped'] = True
        self.data['last_exit'] = now
        self.save()
        return t


class ComparisonEngine(Engine):
    """Feeds and legacy exits reused; new entries only through the paired engine."""
    def __init__(self, client, legacy, lab):
        super().__init__(client, legacy)
        self.lab = lab
        self.entry_task = None
        self.exit_tasks = {}
        self.exit_attempts = {}
        self.observe_task = None
        self.research_task = None
        self.last_candidate_notice = 0.

    async def paired_entry(self, trial, anchor):
        market, direction = trial['market'], trial['signal']
        other = 'DOWN' if direction == 'UP' else 'UP'
        started = time.monotonic()
        try:
            await asyncio.sleep(EXECUTION_DELAY)
            # Same observation window; do not fetch reverse only after original fills.
            results = await asyncio.gather(
                self.rest_book(direction, market=market), self.rest_book(other, market=market),
                return_exceptions=True)
            now = time.time()
            if self.market != market or time.monotonic()-started > MAX_ENTRY_WAIT:
                raise Skip('pair_changed_or_too_slow')
            if RECORDER is not None and RECORDER.failed:
                raise Skip('recorder_failed_entries_paused')
            self.spot.check(now)
            if anchor not in self.anchors:
                raise Skip('anchor_invalidated')
            original_book, reverse_book = results
            if isinstance(original_book, BaseException):
                raise Skip('original_book_unavailable')
            if abs(original_book.source-self.spot.source) > MAX_FEED_SKEW:
                raise Skip('feed_time_skew')
            # Confirm the ORIGINAL trigger again after latency; reverse is its counterfactual.
            _, ref, _, _ = entry_assessment(original_book, market, direction, anchor,
                                            self.spot.price, STAKE, now)
            trial['model_reference'] = ref
            trial['btc_at_fill'] = self.spot.price
            trial['execution_ms'] = (time.monotonic()-started)*1000
            decisions = {}
            for name, side, b in zip(ARM_NAMES,(direction,other),results):
                row = dict(direction=side, status='rejected', quote=None)
                decisions[name] = row
                try:
                    if isinstance(b, BaseException):
                        raise Skip('book_unavailable')
                    b.check(now)
                    if abs(b.source-self.spot.source) > MAX_FEED_SKEW:
                        raise Skip('feed_time_skew')
                    if b.spread() > MAX_SPREAD:
                        raise Skip('spread')
                    q = buy_quote(b, market, STAKE, now)
                    row.update(quote=q, bid=max(b.bids), ask=min(b.asks), book_source=b.source,
                               entry_mid=b.mid())
                    immediate = sell_quote(b, market, q['qty'], now)
                    row.update(immediate_exit=immediate['received'],
                               baseline_loss=(q['spent']-immediate['received'])/q['qty'],
                               costs=cost_breakdown(b,q,immediate))
                    cost_guard(b, market, q, now)
                    a = self.lab.data['arms'][name]
                    if a['position']:
                        raise Skip('paper_position_open')
                    if self.lab.data['fill_counts'][name].get(market['slug'],0) >= MAX_PAPER_ENTRIES_PER_ROUND:
                        raise Skip('paper_round_fill_limit')
                    if now-self.lab.data['last_exit'] < COOLDOWN_SECONDS:
                        raise Skip('paper_exit_cooldown')
                    if a['daily_stopped'] or a['cash'] < q['spent']:
                        raise Skip('account_limit')
                    row['status'] = 'filled'
                except Skip as exc:
                    row['reason'] = str(exc)
                    self.stats[name+':'+str(exc)] += 1
            # One atomic save includes both fill decisions and all debits.
            self.lab.commit(trial, decisions, now)
            lines = [f"PAPER VERGELIJKING #{trial['id']} | BTC signaal {direction}",
                     f"Uitvoering na {trial['execution_ms']:.0f} ms incl. vertraging"]
            for name,r in decisions.items():
                label = 'ORIGINEEL' if name == 'original' else 'REVERSE'
                if r['status'] == 'filled':
                    lines.append(f"{label} {r['direction']}: ${r['quote']['spent']:.2f}, "
                                 f"koop {r['quote']['price']:.4f}, directe verkoopfrictie "
                                 f"{r['baseline_loss']:.4f}/aandeel")
                else:
                    lines.append(f"{label} {r['direction']}: overgeslagen — {r['reason']}")
                if r.get('costs'):
                    c = r['costs']
                    lines.append(f"Kosten cent/aandeel: spread {100*c['spread']:.2f}, "
                                 f"fees {100*c['fees']:.2f}, extra slippage {100*c['slippage']:.2f}, "
                                 f"diepte/afronding {100*c['depth_and_rounding']:.2f}; "
                                 f"totaal {100*c['total']:.2f} (grens {100*(STOP_LOSS_PER_SHARE-ENTRY_STOP_BUFFER):.2f}).")
            lines.append('Ook afgewezen quotes volgen we op 1/2/5/15s. Metingen wijzigen geen saldo.')
            if any(r['status']=='filled' for r in decisions.values()) or now-self.last_candidate_notice >= REPORT_SECONDS:
                self.notify('\n'.join(lines))
                self.last_candidate_notice = now
            else:
                LOG.info('Kandidaat #%s gemeten; beide paperinstappen afgewezen', trial['id'])
        except asyncio.CancelledError:
            self.lab.cancel(trial,'shutdown_before_fill')
            raise
        except LedgerError:
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc,Skip) else type(exc).__name__
            self.lab.cancel(trial,reason)
            self.stats['pair_cancel:'+reason] += 1

    def record_markouts(self, now):
        """Contemporaneous observations only. Never substitute a later successful price."""
        changed = False
        for trial in self.lab.data['trials']:
            if trial['status'] != 'observing':
                continue
            elapsed = now-trial['entered']
            for horizon in MARKOUT_HORIZONS:
                key = str(horizon)
                if key in trial['markouts'] or elapsed < horizon:
                    continue
                sample = dict(observed_at=now, delay=elapsed-horizon, arms={})
                trial['markouts'][key] = sample
                changed = True
                for name,row in trial['arms'].items():
                    out = dict(status='missing', executed=row['status']=='filled')
                    sample['arms'][name] = out
                    if elapsed-horizon > MARKOUT_TOLERANCE:
                        out['reason'] = 'missed_window_or_restart'
                        continue
                    if not row.get('quote'):
                        out['reason'] = 'no_entry_quote'
                        continue
                    if self.market != trial['market']:
                        out['reason'] = 'different_market'
                        continue
                    try:
                        b = self.books[row['direction']]
                        q = sell_quote(b,trial['market'],row['quote']['qty'],now)
                        out.update(status='observed', bid=max(b.bids), ask=min(b.asks),
                                   source=b.source, mid=b.mid(), price=q['price'],
                                   received=q['received'], exit_fee=q['fee'],
                                   hypothetical_net=q['received']-row['quote']['spent'],
                                   price_pnl=q['received']+q['fee']-row['quote']['spent']+row['quote']['fee'],
                                   total_fees=q['fee']+row['quote']['fee'],
                                   mid_move=b.mid()-row['entry_mid'] if 'entry_mid' in row else None)
                    except (Skip,KeyError) as exc:
                        out['reason'] = str(exc)
            if len(trial['markouts']) == len(MARKOUT_HORIZONS):
                trial['status'] = 'observed'
        if changed:
            self.lab.save()

    async def markout_worker(self):
        while True:
            self.record_markouts(time.time())
            await asyncio.sleep(.05)

    async def research_worker(self):
        """Record eligible signals even when a paper account already has a position."""
        last_checkpoint = 0.
        while True:
            now = time.time()
            record('clock')
            if self.market and now-last_checkpoint >= 5:
                books = {d:dict(asset=b.asset, condition=b.condition,
                                bids=list(b.bids.items()), asks=list(b.asks.items()),
                                source=b.source,received=b.received,tick=b.tick,minimum=b.minimum)
                         for d,b in self.books.items() if b.ready}
                record('checkpoint',version=VERSION,signal_stake=STAKE,
                       market=self.market.copy(),books=books,
                       spot=dict(price=self.spot.price,source=self.spot.source,received=self.spot.received))
                last_checkpoint = now
            if self.market and self.stream_ok:
                try:
                    anchor = self.observe(now)
                    direction = 'UP' if self.spot.price > anchor['spot'] else 'DOWN'
                    entry_assessment(self.books[direction], self.market, direction, anchor,
                                     self.spot.price, STAKE, now)
                    record('signal', direction=direction, anchor=anchor.copy(),
                           slug=self.market['slug'])
                except (Skip, KeyError):
                    pass
            await asyncio.sleep(.1)

    def comparison_exit_reason(self, p, now):
        if now-p['entered'] >= MAX_HOLD_SECONDS:
            return 'time_limit'
        if p['market']['expiry']-now <= EXIT_BEFORE_EXPIRY:
            return 'before_expiry'
        try:
            self.spot.check(now)
            b = self.books[p['direction']]
            q = sell_quote(b,p['market'],p['qty'],now)
        except (Skip,KeyError):
            return 'data_stale'
        net_per_share = (q['received']-p['spent'])/p['qty']
        if net_per_share >= LAB_TAKE_PROFIT:
            return 'net_target'
        if net_per_share <= -STOP_LOSS_PER_SHARE:
            return 'loss_limit'
        return None

    async def close_comparison(self, name, reason):
        p = self.lab.data['arms'][name]['position']
        if not p:
            return
        try:
            now = time.time()
            if now >= p['market']['expiry']:
                rs = await asyncio.wait_for(self.client.get_resolutions(condition_ids=p['market']['condition']),5)
                payout = resolved_payout(rs,p['market'],p['direction'])
                if payout is None:
                    return
                t = self.lab.close(name,p['qty']*payout,0.,'resolution_fallback',time.time())
            else:
                await asyncio.sleep(EXECUTION_DELAY)
                if time.time() >= p['market']['expiry']:
                    return
                b = await self.rest_book(p['direction'],market=p['market'])
                now = time.time()
                if now >= p['market']['expiry']:
                    return
                q = sell_quote(b,p['market'],p['qty'],now)
                t = self.lab.close(name,q['received'],q['fee'],reason,now)
            self.stats[name+':exit:'+t['reason']] += 1
            self.notify(f"PAPER {name.upper()} #{p['trial_id']} {p['direction']} gesloten\n"
                        f"{t['reason']} | netto ${t['pnl']:+.2f} | duur {t['seconds']:.1f}s\n"
                        f"Prijsresultaat ${t['price_pnl']:+.2f}, beide fees ${t['fees']:.4f}\n"
                        "Vervolgmetingen blijven doorlopen tot 15s na instap.")
        except LedgerError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats[name+':exit_unavailable:'+type(exc).__name__] += 1
            # No available bid = no invented sale. Position stays in the ledger.

    def reap_tasks(self):
        if self.entry_task and self.entry_task.done():
            self.entry_task.result()
            self.entry_task = None
        for name,task in list(self.exit_tasks.items()):
            if task.done():
                task.result()
                del self.exit_tasks[name]
        if self.observe_task and self.observe_task.done():
            self.observe_task.result()
            raise LedgerError('Vervolgmetingen onverwacht gestopt')
        if self.research_task and self.research_task.done():
            self.research_task.result()
            raise LedgerError('Replaymetingen onverwacht gestopt')

    def schedule_exits(self, now):
        for name,a in self.lab.data['arms'].items():
            p = a['position']
            if not p or name in self.exit_tasks:
                continue
            expired = now >= p['market']['expiry']
            interval = 20 if expired else 1
            if now-self.exit_attempts.get(name,0) < interval:
                continue
            reason = 'resolution_fallback' if expired else self.comparison_exit_reason(p,now)
            if reason:
                self.exit_attempts[name] = now
                self.exit_tasks[name] = asyncio.create_task(self.close_comparison(name,reason))

    def report(self):
        totals,paired = lab_summary(self.lab.data)
        lines = [VERSION, 'Twee paperaccounts; kostenfilter actief.']
        for name,a in self.lab.data['arms'].items():
            t = totals[name]
            lines.append(f"{name.upper()}: gesloten {t['n']}, winst {t['wins']}, "
                         f"netto ${t['pnl']:+.2f}, fees ${t['fees']:.2f}, "
                         f"open {int(a['position'] is not None)}, vrij ${a['cash']:.2f}")
            rejected = [tr.get('arms',{}).get(name,{}) for tr in self.lab.data['trials']]
            costs = sum(r.get('reason') == 'roundtrip_cost_near_stop' for r in rejected)
            lines.append(f"Afgewezen {name}: {sum(r.get('status') == 'rejected' for r in rejected)}, "
                         f"waarvan kosten bij verliesgrens: {costs}")
        lines.append(f"Beide uitgevoerd én gesloten: {paired['n']} paren | "
                     f"origineel ${paired['original']:+.2f}, reverse ${paired['reverse']:+.2f}")
        measured = candidate_summary({'trials':self.lab.data['trials'][-200:]})
        lines.append('VERVOLGMETINGEN — laatste 200 kandidaten, inclusief afwijzingen.')
        for name in ARM_NAMES:
            for cohort,label in (('filled','paper uitgevoerd'),('rejected','afgewezen')):
                summary = measured[name][cohort]
                lines.append(f"{name.upper()} {label}: {summary['candidates']} kandidaten")
                c = summary['costs']
                if c:
                    lines.append(f"Gem. kosten cent/aandeel (n={summary['cost_n']}): "
                                 f"spread {100*c['spread']:.2f} + fees {100*c['fees']:.2f} + "
                                 f"slip {100*c['slippage']:.2f} + diepte/afr. {100*c['depth_and_rounding']:.2f} "
                                 f"= {100*c['total']:.2f}")
                if not summary['candidates']:
                    continue
                for horizon,s in summary['horizons'].items():
                    value = f"${s['net_mean']:+.3f}" if s['net_mean'] is not None else 'onbekend'
                    lines.append(f"{horizon}s netto hypothetisch gem. {value}, "
                                 f"positief {s['positive']}/{s['n']}, mist {s['missing']}, wacht {s['pending']}")
                    if s['priced_n']:
                        lines.append(f"  Prijs-PnL ${s['price_mean']:+.3f} − fees ${s['fees_mean']:.3f} "
                                     f"(gem., n={s['priced_n']}; slippage zit in prijs).")
        lines.append('Overlap mogelijk: dit zijn geen onafhankelijke trades of extra saldowinsten.')
        _,seconds,returns,_ = self.spot.history_metrics()
        lines.append(f"BTC historie {seconds:.0f}s / {returns} rendementen | "
                     f"boekherstel {self.book_diagnostics}")
        lines.append('Controles (herhalingen): '+str(dict(self.stats.most_common(12))))
        legacy = self.ledger.data
        lines.append(f"Oude test: {len(legacy['trades'])} gesloten, "
                     f"netto ${sum(t['pnl'] for t in legacy['trades']):+.2f}; niet meegeteld.")
        lines.append('PAPER: geschatte fills. Vervolgmetingen zijn geen extra winst/verliesboekingen.')
        if RECORDER is not None:
            lines.insert(1, f"Replay-opname: {'FOUT — entries geblokkeerd' if RECORDER.failed else 'actief'}; "
                         f"{RECORDER.written} events geschreven, wachtrij {RECORDER.pending}.")
        delays = sorted(t['execution_ms'] for t in self.lab.data['trials'] if t.get('execution_ms'))
        if delays:
            lines.insert(2, f"Gemeten instapvertraging p50/p95: {delays[len(delays)//2]:.0f}/"
                         f"{delays[min(len(delays)-1,int(len(delays)*.95))]:.0f} ms (n={len(delays)}).")
        message = ''
        for line in lines:
            if len(message)+len(line)+1 > 3800:
                self.notify(message)
                message = VERSION+' — vervolg\n'
            message += line+'\n'
        if message:
            self.notify(message.rstrip())
        self.stats.clear()
        self.spot.event_stats.clear()
        self.spot.event_ages.clear()

    async def run(self):
        self.notify(f"{VERSION} gestart | ALLEEN PAPER\n"
                    f"ORIGINEEL versus REVERSE, elk eigen saldo; inzet ${STAKE:.2f} per variant.\n"
                    f"Instap geweigerd bij directe verkoopfrictie ≥"
                    f"{STOP_LOSS_PER_SHARE-ENTRY_STOP_BUFFER:.3f}/aandeel.\n"
                    f"Gelijke exits: netto +{LAB_TAKE_PROFIT:.2f}/aandeel, "
                    f"-{STOP_LOSS_PER_SHARE:.2f}/aandeel of na {MAX_HOLD_SECONDS}s.\n"
                    "Max. 2 paperinstappen per variant per ronde. Kandidaten blijven gemeten, maximaal elke 10s.\n"
                    "BTC/L2-opname + offline replay actief. Geen Marketlens-account nodig.")
        self.observe_task = asyncio.create_task(self.markout_worker())
        self.research_task = asyncio.create_task(self.research_worker())
        jobs = [asyncio.create_task(self.spot_stream()),asyncio.create_task(self.telegram_worker()),
                asyncio.create_task(self.repair_books()),self.observe_task,
                self.research_task]
        try:
            while True:
                now = time.time()
                HEALTH['heartbeat'] = now
                self.reap_tasks()
                try:
                    self.ledger.roll_day(now)
                    self.lab.roll_day(now)
                    legacy = self.ledger.data['position']
                    positions = [a['position'] for a in self.lab.data['arms'].values() if a['position']]
                    required = legacy['market'] if legacy else (positions[0]['market'] if positions else None)
                    if required and self.market != required:
                        await self.switch_market(required)
                    if not required and not self.entry_task and (not self.market or now >= self.market['expiry']):
                        if now < self.next_discovery:
                            raise Skip('discovery_backoff')
                        self.next_discovery = now+5
                        await self.switch_market(await discover(self.client,int(now//300)*300))
                    if legacy:
                        await self.manage_position()  # never creates new legacy trades
                    else:
                        self.schedule_exits(now)
                        if self.entry_task:
                            raise Skip('comparison_pending')
                        if RECORDER is not None and RECORDER.failed:
                            raise Skip('recorder_failed_entries_paused')
                        self.lab.observation_allowed(self.market,now)
                        if not ENTRY_TIME_MIN <= self.market['expiry']-now <= ENTRY_TIME_MAX:
                            raise Skip('outside_entry_window')
                        anchor = self.observe(now)
                        direction = 'UP' if self.spot.price > anchor['spot'] else 'DOWN'
                        # Keep the signal generator fixed for both directions.
                        entry_assessment(self.books[direction],self.market,direction,anchor,
                                         self.spot.price,STAKE,now)
                        trial = self.lab.begin(self.market,direction,now)
                        self.entry_task = asyncio.create_task(self.paired_entry(trial,anchor))
                except Skip as exc:
                    self.stats[str(exc)] += 1
                except LedgerError:
                    raise
                except Exception as exc:
                    self.stats['api_error:'+type(exc).__name__] += 1
                    LOG.warning('Vergelijking tijdelijk gepauzeerd: %s',type(exc).__name__)
                    await asyncio.sleep(1)
                if time.time()-self.last_report >= REPORT_SECONDS:
                    self.report()
                    self.last_report = time.time()
                await asyncio.sleep(.1)
        finally:
            for task in [self.stream_task,self.entry_task,*self.exit_tasks.values()]:
                if task:
                    jobs.append(task)
            for task in jobs:
                task.cancel()
            await asyncio.gather(*jobs,return_exceptions=True)
            self.ledger.save()
            self.lab.save()


class TapeRecorder:
    """Bounded nonblocking public-data journal. Disk work never runs on the feed loop.

    Retains at most 64 x ~16MiB chunks (~1GiB), deleting oldest closed chunks.
    A queue overflow/disk error/clock jump permanently blocks new entries this run.
    Existing positions are still managed. Truncated tails are not silently replayed.
    """
    def __init__(self, directory, max_bytes=16*1024*1024, max_files=64):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.max_files = max_bytes, max_files
        self.session = uuid.uuid4().hex
        self.queue = queue.Queue(maxsize=20000)
        self.failed = False
        self.closed = False
        self.written = self.seq = 0
        self.wall, self.mono = time.time(), time.monotonic()
        self.thread = threading.Thread(target=self._writer, daemon=True)
        self.thread.start()
        self.emit('session', version=VERSION, signal_stake=STAKE,
                  policy=dict(min_gross_edge=MIN_GROSS_EDGE, min_net_edge=MIN_NET_EDGE,
                              min_btc_move=MIN_BTC_MOVE, min_model_move=MIN_MODEL_MOVE,
                              take_profit=LAB_TAKE_PROFIT, stop=STOP_LOSS_PER_SHARE,
                              hold=MAX_HOLD_SECONDS, slippage_ticks=SLIPPAGE_TICKS))

    @property
    def pending(self):
        return self.queue.qsize()

    def emit(self, kind, **data):
        if self.failed or self.closed:
            return
        now = time.time()
        if abs((now-self.wall)-(time.monotonic()-self.mono)) > .5:
            self.failed = True
            LOG.error('Clock jump: nieuwe paperentries gepauzeerd')
            return
        self.seq += 1
        row = dict(schema=1, session=self.session, seq=self.seq, time=now, kind=kind, **data)
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self.failed = True
            LOG.error('Replay-wachtrij vol: nieuwe paperentries gepauzeerd')

    def _writer(self):
        handle = None
        size = 0
        last_flush = time.monotonic()
        try:
            while True:
                try:
                    row = self.queue.get(timeout=.5)
                except queue.Empty:
                    if handle:
                        handle.flush()
                    continue
                if row is None:
                    self.queue.task_done()
                    break
                if handle is None or size >= self.max_bytes:
                    if handle:
                        handle.flush()
                        os.fsync(handle.fileno())
                        handle.close()
                    path = self.directory / f"tape-{time.time_ns():020d}-{self.session[:8]}.jsonl"
                    handle = path.open('x', encoding='utf-8')
                    size = 0
                    files = sorted(self.directory.glob('tape-*.jsonl'))
                    for old in files[:-self.max_files]:
                        old.unlink()
                line = json.dumps(row, allow_nan=False, separators=(',', ':'))+'\n'
                handle.write(line)
                size += len(line.encode('utf-8'))
                self.written += 1
                self.queue.task_done()
                if time.monotonic()-last_flush >= 1:
                    handle.flush()
                    last_flush = time.monotonic()
        except Exception:
            self.failed = True
            LOG.exception('Replay-opslag defect; bestaande posities blijven beheerd')
        finally:
            if handle and not handle.closed:
                with contextlib.suppress(OSError):
                    handle.flush()
                    os.fsync(handle.fileno())
                    handle.close()

    def close(self):
        self.closed = True
        if self.thread.is_alive():
            try:
                self.queue.put(None, timeout=1)
            except queue.Full:
                self.failed = True
                return
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                self.failed = True


def tape_rows(paths):
    """Strict receive-order replay; missing chunks or malformed records are errors."""
    previous_time, session, seq = -1., None, 0
    seen = set()
    for path in paths:
        with Path(path).open(encoding='utf-8') as f:
            for line_number, line in enumerate(f, 1):
                try:
                    row = json.loads(line)
                    if row['schema'] != 1 or finite(row['time']) < previous_time:
                        raise ValueError('wrong schema or receive times out of order')
                    if row['session'] == session:
                        if row['seq'] != seq+1:
                            raise ValueError('missing or duplicated event/chunk')
                    elif row['session'] in seen:
                        raise ValueError('interleaved recording sessions')
                    else:
                        seen.add(row['session'])
                    previous_time, session, seq = row['time'], row['session'], row['seq']
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(f'Invalid tape {Path(path).name}:{line_number}: {exc}') from exc
                yield row


def restored_book(row):
    b = Book(row['asset'], row['condition'])
    b.bids = {finite(p):finite(q) for p,q in row['bids']}
    b.asks = {finite(p):finite(q) for p,q in row['asks']}
    if any(not 0 < p < 1 or q <= 0 for side in (b.bids,b.asks) for p,q in side.items()):
        raise ValueError('Invalid recorded depth')
    b.source, b.received = finite(row['source']), finite(row['received'])
    b.tick, b.minimum = finite(row['tick']), finite(row['minimum'])
    b.ready = True
    return b


class TapeReplay:
    """Taker-only historical execution at delayed times, never future quotes.

    Reuses buy_quote/sell_quote/fees/cost_guard and original signal confirmation.
    No maker orders, queue-priority claims, official settlement or network calls.
    Signal parameters stay fixed: this is execution sensitivity, not optimization.
    """
    def __init__(self, latency=.5, stake=7.5, balance=1000.):
        if not 0 <= finite(latency) <= 10 or not 0 < finite(stake) <= finite(balance):
            raise ValueError('Invalid latency/stake/balance')
        self.latency, self.stake = latency, stake
        self.accounts = {n:dict(cash=balance, initial=balance, position=None, trades=[],
                                day='', day_start=balance, stopped=False,
                                peak=balance, max_drawdown=0.) for n in ARM_NAMES}
        self.market, self.books, self.spot = None, {}, dict(price=0., source=0., received=0.)
        self.jobs = []
        self.stats = Counter()
        self.session = None
        self.round_counts = Counter()
        self.recorded_versions = set()
        self.last_trial = self.last_exit = -float('inf')
        self.trial_id = 0
        self.last_time = 0.
        self.recorded_stakes = set()

    def fresh_spot(self, now):
        if not fresh(self.spot['source'],self.spot['received'],now,MAX_BTC_AGE):
            raise Skip('btc_stale')

    def get_book(self, direction, market, now):
        if not self.market or self.market['slug'] != market['slug']:
            raise Skip('wrong_replay_market')
        b = self.books.get(direction)
        if not b or b.asset != market['assets'][direction]:
            raise Skip('missing_replay_book')
        b.check(now)
        return b

    def roll_day(self, now):
        day = datetime.fromtimestamp(now,timezone.utc).date().isoformat()
        for a in self.accounts.values():
            if a['day'] != day:
                p = a['position']
                a.update(day=day,day_start=a['cash']+(p['spent'] if p else 0.),stopped=False)

    def entry(self, payload, now):
        market, direction, anchor, trial_id, session = payload
        if session != self.session:
            raise Skip('session_interrupted_entry')
        self.fresh_spot(now)
        if not self.market or self.market['slug'] != market['slug']:
            raise Skip('market_changed')
        b = self.get_book(direction,market,now)
        if abs(b.source-self.spot['source']) > MAX_FEED_SKEW:
            raise Skip('feed_time_skew')
        entry_assessment(b,market,direction,anchor,self.spot['price'],self.stake,now)
        for name, side in zip(ARM_NAMES,(direction,'DOWN' if direction=='UP' else 'UP')):
            try:
                a = self.accounts[name]
                if a['position'] or a['stopped'] or a['cash'] < self.stake:
                    raise Skip('account_limit')
                if self.round_counts[(name,market['slug'])] >= MAX_PAPER_ENTRIES_PER_ROUND:
                    raise Skip('paper_round_fill_limit')
                if now-self.last_exit < COOLDOWN_SECONDS:
                    raise Skip('paper_exit_cooldown')
                b = self.get_book(side,market,now)
                if abs(b.source-self.spot['source']) > MAX_FEED_SKEW:
                    raise Skip('feed_time_skew')
                if b.spread() > MAX_SPREAD:
                    raise Skip('spread')
                q = buy_quote(b,market,self.stake,now)
                cost_guard(b,market,q,now)
                a['cash'] -= q['spent']
                self.round_counts[(name,market['slug'])] += 1
                a['position'] = dict(**q,direction=side,market=market,entered=now,
                                     trial_id=trial_id,exit_pending=False,next_exit=now)
            except Skip as exc:
                self.stats[name+':entry:'+str(exc)] += 1

    def execute_exit(self, payload, now):
        name, reason = payload
        a = self.accounts[name]
        p = a['position']
        if not p:
            return
        try:
            if now >= p['market']['expiry']:
                raise Skip('expired_unresolved')
            b = self.get_book(p['direction'],p['market'],now)
            q = sell_quote(b,p['market'],p['qty'],now)
            pnl = q['received']-p['spent']
            a['cash'] += q['received']
            a['trades'].append(dict(trial_id=p['trial_id'],pnl=pnl,
                                    fees=p['fee']+q['fee'],reason=reason,
                                    entered=p['entered'],exited=now,qty=p['qty']))
            a['position'] = None
            a['peak'] = max(a['peak'],a['cash'])
            a['max_drawdown'] = max(a['max_drawdown'],1-a['cash']/a['peak'])
            a['stopped'] = a['cash'] <= a['day_start']*(1-DAILY_LOSS_FRACTION)
            self.last_exit = now
        except Skip as exc:
            p['exit_pending'] = False
            p['next_exit'] = now+1
            self.stats[name+':exit:'+str(exc)] += 1

    def due(self, before, inclusive=False):
        while self.jobs:
            self.jobs.sort(key=lambda j:j[0])
            due, kind, payload = self.jobs[0]
            if due > before or (due == before and not inclusive):
                break
            self.jobs.pop(0)
            self.roll_day(due)
            try:
                if kind == 'entry':
                    self.entry(payload,due)
                else:
                    self.execute_exit(payload,due)
            except Skip as exc:
                self.stats['delayed_entry:'+str(exc)] += 1

    def schedule_exits(self, now):
        for name,a in self.accounts.items():
            p = a['position']
            if not p or p['exit_pending'] or now < p['next_exit']:
                continue
            if now >= p['market']['expiry']:
                continue  # Never manufacture a resolution from Binance or the last quote.
            reason = None
            if now-p['entered'] >= MAX_HOLD_SECONDS:
                reason = 'time_limit'
            elif p['market']['expiry']-now <= EXIT_BEFORE_EXPIRY:
                reason = 'before_expiry'
            else:
                try:
                    self.fresh_spot(now)
                    b = self.get_book(p['direction'],p['market'],now)
                    q = sell_quote(b,p['market'],p['qty'],now)
                    net = (q['received']-p['spent'])/p['qty']
                    if net >= LAB_TAKE_PROFIT:
                        reason = 'net_target'
                    elif net <= -STOP_LOSS_PER_SHARE:
                        reason = 'loss_limit'
                except Skip:
                    reason = 'data_stale'
            if reason:
                p['exit_pending'] = True
                self.jobs.append((now+self.latency,'exit',(name,reason)))

    def step(self, row):
        now, kind = row['time'], row['kind']
        self.due(now)  # Execute BEFORE applying any later book information.
        if row['session'] != self.session:
            if self.session is not None:
                self.stats['recording_restart'] += 1
            self.session = row['session']
            self.market, self.books = None, {}
            self.spot = dict(price=0.,source=0.,received=0.)
        self.roll_day(now)
        if kind == 'session':
            self.recorded_stakes.add(row['signal_stake'])
            self.recorded_versions.add(row.get('version'))
            if row.get('version') not in ('7.3-BTC-Replay-PAPER',VERSION):
                raise ValueError('Replay requires matching recording version')
        elif kind in ('market','checkpoint'):
            m = row['market']
            if not self.market or m['slug'] != self.market['slug']:
                self.market, self.books = m, {}
            if kind == 'checkpoint':
                if row.get('version',VERSION) not in ('7.3-BTC-Replay-PAPER',VERSION):
                    raise ValueError('Replay requires matching checkpoint version')
                self.recorded_versions.add(row.get('version',VERSION))
                if 'signal_stake' in row:
                    self.recorded_stakes.add(row['signal_stake'])
                self.spot = row['spot']
                self.books = {d:restored_book(b) for d,b in row['books'].items()}
        elif kind == 'book':
            if self.market and row['condition'] == self.market['condition']:
                self.books[row['direction']] = restored_book(row)
        elif kind in ('delta', 'deltas'):
            for update in row['updates'] if kind == 'deltas' else [row]:
                b = self.books.get(update['direction'])
                if b and update['source'] >= b.source:
                    if update['side'] not in ('BUY','SELL'):
                        raise ValueError('Invalid recorded book side')
                    price,size = finite(update['price']),finite(update['size'])
                    if not 0 < price < 1 or size < 0:
                        raise ValueError('Invalid recorded book update')
                    side = b.bids if update['side']=='BUY' else b.asks
                    if size == 0:
                        side.pop(price,None)
                    else:
                        side[price] = size
                    b.source, b.received = update['source'], update['received']
        elif kind == 'book_reset':
            self.books.clear()
        elif kind == 'spot_reset':
            self.spot = dict(price=0.,source=0.,received=0.)
        elif kind == 'spot':
            self.spot = {k:row[k] for k in ('price','source','received')}
        self.due(now,inclusive=True)
        if kind == 'clock':
            self.schedule_exits(now)  # Same ~100ms decision cadence as the online loop.
        if kind == 'signal':
            pending = any(j[1]=='entry' for j in self.jobs)
            if (not pending and self.market and row['slug']==self.market['slug']
                    and now-self.last_trial >= CANDIDATE_INTERVAL):
                self.trial_id += 1
                self.last_trial = now
                self.jobs.append((now+self.latency,'entry',
                                  (self.market.copy(),row['direction'],row['anchor'],self.trial_id,self.session)))
        self.last_time = now
        self.stats['events'] += 1

    def result(self):
        arms = {}
        for name,a in self.accounts.items():
            rows = a['trades']
            p = a['position']
            arms[name] = dict(closed=len(rows),wins=sum(t['pnl']>0 for t in rows),
                              realized_pnl=sum(t['pnl'] for t in rows),
                              closed_trade_fees=sum(t['fees'] for t in rows),
                              cash=a['cash'],open_position=bool(p),
                              open_cost=p['spent'] if p else 0.,
                              max_realized_drawdown=a['max_drawdown'])
        maps = {n:{t['trial_id']:t for t in a['trades']} for n,a in self.accounts.items()}
        paired = set(maps['original']) & set(maps['reverse'])
        return dict(version=VERSION,mode='OFFLINE_REPLAY_PAPER',latency_ms=self.latency*1000,
                    stake=self.stake,recorded_signal_stakes=sorted(self.recorded_stakes),
                    recorded_versions=sorted(self.recorded_versions),entry_cap_policy='actual_fills_per_arm',
                    arms=arms,matched_closed_pairs=dict(n=len(paired),**{
                        n:sum(maps[n][i]['pnl'] for i in paired) for n in ARM_NAMES}),
                    pending_orders=len(self.jobs),counters=dict(self.stats),
                    limitations=['Fixed recorded signal generator; execution sensitivity only.',
                                 'Taker fills only; no queue priority simulation.',
                                 'No official settlement data: expired/open positions stay unresolved.',
                                 'Quotes are historical observed depth; not guaranteed live fills.'])


def start_health_server():
    if not os.getenv("PORT"):
        return
    app = Flask(__name__)
    @app.get("/")
    @app.get("/health")
    def health():
        alive = time.time()-HEALTH["heartbeat"] < 60
        return jsonify(**HEALTH, alive=alive), 200 if alive else 503
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ["PORT"]),
                                           use_reloader=False), daemon=True).start()


async def main():
    global RECORDER
    if importlib.metadata.version("polymarket-client") != "0.13.0":
        raise RuntimeError("requirements.txt vereist polymarket-client==0.13.0")
    if not 0 < finite(STAKE) <= finite(START_BALANCE):
        raise RuntimeError("Ongeldige PAPER_TRADE_AMOUNT/PAPER_BALANCE")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Railway/Linux: prevent two local processes spending the same paper ledger.
    import fcntl
    with (DATA_DIR / "polymarket_latency_paper.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start_health_server()
        RECORDER = TapeRecorder(DATA_DIR / 'replay')
        try:
            async with AsyncPublicClient() as client:
                engine = ComparisonEngine(client, Ledger(), ComparisonLedger())
                task = asyncio.create_task(engine.run())
                for sig in (signal.SIGINT, signal.SIGTERM):
                    asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            await asyncio.to_thread(RECORDER.close)


def cli():
    parser = argparse.ArgumentParser(description='BTC 5m paper experiment + offline event replay')
    parser.add_argument('--replay', nargs='+', metavar='JSONL',
                        help='recording files or quoted glob, e.g. "replay/tape-*.jsonl"')
    parser.add_argument('--latency-ms', type=float, default=500.,
                        help='total simulated decision-to-fill delay, replay only (default 500ms)')
    parser.add_argument('--stake',type=float,default=STAKE,help='all-in replay stake')
    args = parser.parse_args()
    if args.replay:
        import glob
        paths = sorted(set(p for pattern in args.replay for p in glob.glob(pattern)))
        if not paths:
            parser.error('Geen replay-opnamen gevonden; laat de bot eerst data verzamelen.')
        replay = TapeReplay(args.latency_ms/1000,args.stake,START_BALANCE)
        for row in tape_rows(paths):
            replay.step(row)
        # Never run pending orders beyond the last recorded receive time.
        print(json.dumps(replay.result(),indent=2,allow_nan=False))
    else:
        asyncio.run(main())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cli()
