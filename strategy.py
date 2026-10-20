"""BTC Lead v1 — PAPER ONLY. Public books + authenticated, read-only PolyBolt.

No private key, order-signing client, live order endpoint or early sale exists here.
Wordt gestart vanuit bot.py; dit bestand bevat de paperstrategie.
"""
import asyncio
import bisect
from collections import Counter
from contextlib import suppress
from datetime import datetime, timezone
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import random
import sqlite3
import threading
import time

import requests
from polymarket import AsyncPublicClient
from websockets.asyncio.client import connect

VERSION = "1.0-BTC-Lead-TWAP-PAPER"
# Change settings here, not the old bot's environment flags.
PAPER_STAKE = 5.0                 # All-in, including simulated entry fee
INITIAL_CASH = 1000.0
MIN_NET_EDGE = 0.03               # Dollars per share, against lower confidence bound
MIN_SAMPLES = 100                 # Distinct resolved markets in the matching cell
MIN_DAYS = 3
TRAIN_DAYS = 30
MAX_DAILY_LOSS = 0.03
MAX_DAILY_ENTRIES = 12
MAX_PRICE = 0.95
MAX_SPREAD = 0.04
FEED_MAX_AGE = 2.5
MAX_TICK_GAP = 3.0
BOOK_MAX_AGE = 3.0
LATENCY_SECONDS = 0.5
SLIPPAGE_TICKS = 1
CHECKPOINTS = (300, 285, 240, 180, 120, 90, 60, 45, 30, 15)  # seconds remaining
GAP_EDGES = (0, 2, 5, 10, 20, 50, float("inf"))  # basis points
SCHEMA = "btc_chainlink60_projected_v1"
DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("BOT_DATA_DIR", "data"))
DB_NAME = "btc_lead_v1.sqlite3"    # Separate experiment; never imports old balances
WS_URL = "wss://ws-live-v2.polymarket.com/ws"
SPOT, TWAP = "price.crypto", "price.crypto.twap"
LOG = logging.getLogger("btc-lead")


class Skip(Exception):
    pass


def number(v):
    x = float(v)
    if not math.isfinite(x):
        raise Skip("niet_eindig_getal")
    return x


def day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def unix_ms(v):
    # Raw PolyBolt timestamps are explicitly milliseconds, not guessed units.
    x = number(v) / 1000
    if x < 1_600_000_000:
        raise Skip("timestamp_eenheid")
    return x


def gap_bin(bp):
    return max(0, min(len(GAP_EDGES)-2, bisect.bisect_right(GAP_EDGES, abs(bp))-1))


def wilson_lower(wins, n, z=1.96):
    """Conservative heuristic, NOT a guarantee for dependent/nonstationary markets."""
    if n <= 0:
        return 0.0
    p = wins/n
    return (p+z*z/(2*n)-z*math.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)


def history_estimate(rows, now):
    # rows: (market_start, observed_time, resolved_time, leader_won).
    valid = [r for r in rows if now-TRAIN_DAYS*86400 <= r[0] < now
             and r[1] < r[2] < now and r[3] in (0, 1)]
    unique = {r[0]: r for r in valid}
    n = len(unique)
    wins = sum(r[3] for r in unique.values())
    days = len({day(r[0]) for r in unique.values()})
    return {"n": n, "wins": wins, "days": days, "p": wins/n if n else None,
            "lower": wilson_lower(wins, n), "ready": n >= MIN_SAMPLES and days >= MIN_DAYS}


class Reference:
    """Keep raw Chainlink spot + authoritative TWAP samples; never use Binance."""
    def __init__(self):
        self.points = {SPOT: {}, TWAP: {}}
        self.connected = False
        self.state = "wachten_op_feed"
        self.sequences = {}
        self.acks = set()

    def reset(self):
        # Reconnect requires a fresh snapshot; no stale buffers carried across gaps.
        self.points = {SPOT: {}, TWAP: {}}
        self.sequences.clear()
        self.acks.clear()
        self.connected = False

    def apply(self, msg, now):
        channel = msg.get("channel")
        if channel not in self.points:
            return []
        if msg.get("v") != 1:
            raise Skip("feed_protocol")
        seq = msg.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise Skip("feed_sequence_ontbreekt")
        previous = self.sequences.get(channel)
        if msg.get("dropped", 0) or (previous is not None and seq != previous+1):
            raise Skip("feed_sequence_gat")
        self.sequences[channel] = seq
        p = msg.get("payload", {})
        if p.get("symbol") != "btcusd" or p.get("source") != "chainlink":
            raise Skip("verkeerde_referentiebron")
        if channel == TWAP and p.get("window_seconds") != 60:
            raise Skip("verkeerd_twap_venster")
        snapshot = msg.get("snapshot") is True
        items = p.get("data", []) if snapshot else [p]
        if not isinstance(items, list):
            raise Skip("feed_snapshot_formaat")
        added = []
        for item in items:
            ts = unix_ms(item["timestamp"])
            value = number(item.get("full_accuracy_value", item.get("value")))
            if value <= 0 or ts > now+0.5 or now-ts > 600:
                raise Skip("feed_prijs_of_tijd")
            if item.get("is_carried_forward") or p.get("is_carried_forward"):
                raise Skip("feed_doorgerolde_prijs")
            existing = self.points[channel].get(ts)
            if existing and not math.isclose(existing[0], value, rel_tol=1e-12):
                raise Skip("feed_herziening")
            self.points[channel][ts] = (value, now)
            added.append((channel, ts, value, now))
        for kind in self.points:
            self.points[kind] = {t: v for t, v in self.points[kind].items() if t >= now-900}
        self.connected = self.acks == {SPOT, TWAP}
        self.state = "verbonden" if self.connected else "wachten_op_abonnement"
        return added

    def current(self, now):
        if not self.connected or not self.points[SPOT] or not self.points[TWAP]:
            raise Skip("chainlink_niet_gereed")
        for kind in (SPOT, TWAP):
            ts = max(self.points[kind])
            _, received = self.points[kind][ts]
            if not 0 <= now-ts <= FEED_MAX_AGE or not 0 <= now-received <= FEED_MAX_AGE:
                raise Skip("chainlink_verouderd")
        ts = max(self.points[SPOT])
        return ts, self.points[SPOT][ts][0]

    def strike(self, start):
        # Exact boundary only: a TWAP one second later is a different price-to-beat.
        point = self.points[TWAP].get(float(start))
        if point is None:
            raise Skip("start_twap_ontbreekt_wacht_volgende_ronde")
        return point[0]

    def projected(self, end, now, spot):
        begin = end-60
        samples = sorted((t, v[0]) for t, v in self.points[SPOT].items() if t <= now)
        before = [(t, v) for t, v in samples if t <= begin]
        if not before or begin-before[-1][0] > MAX_TICK_GAP:
            raise Skip("eind_twap_historie_ontbreekt")
        last_ts, last_price = before[-1]
        cursor, integral = begin, 0.0
        for ts, price in samples:
            if ts <= begin:
                continue
            if ts-last_ts > MAX_TICK_GAP:
                raise Skip("eind_twap_datagat")
            integral += last_price*(ts-cursor)
            cursor, last_ts, last_price = ts, ts, price
        if now-last_ts > FEED_MAX_AGE:
            raise Skip("eind_twap_verouderd")
        integral += last_price*(now-cursor)
        # Explicit no-change assumption for the unobserved tail, NOT an official TWAP.
        projected = (integral + spot*(end-now))/60
        return projected, integral, now-begin

    def feature(self, market, now):
        start, end = market["start"], market["end"]
        remaining = end-now
        if not 0 < remaining <= 300:
            raise Skip("buiten_ronde")
        spot_ts, spot = self.current(now)
        strike = self.strike(start)
        if remaining <= 60:
            reference, integral, elapsed = self.projected(end, now, spot)
            kind = "projected_twap"
        else:
            reference, integral, elapsed = spot, None, 0.0
            kind = "spot_lead"
        bp = (reference/strike-1)*10000
        if abs(bp) < 1e-8:
            raise Skip("geen_voorsprong")
        return {"kind": kind, "side": "UP" if bp > 0 else "DOWN", "gap_bp": abs(bp),
                "gap_bin": gap_bin(bp), "strike": strike, "spot": spot, "spot_ts": spot_ts,
                "reference": reference, "integral": integral, "elapsed_final": elapsed,
                "remaining": remaining, "at": now}


def quote(book, market, side, budget, now):
    if str(book.asset_id) != market["assets"][side] or str(book.condition_id) != market["condition"]:
        raise Skip("verkeerd_orderboek")
    if book.timestamp is None or not -0.5 <= now-book.timestamp.timestamp() <= BOOK_MAX_AGE:
        raise Skip("orderboek_verouderd")
    tick, minimum = number(book.tick_size), number(book.min_order_size)
    if not 0 < tick < 1 or minimum <= 0 or budget <= 0:
        raise Skip("orderboek_metadata")
    bids = [(number(x.price), number(x.size)) for x in book.bids]
    asks = [(number(x.price), number(x.size)) for x in book.asks]
    if any(not 0 < p < 1 or s < 0 for p, s in bids+asks):
        raise Skip("ongeldig_orderboek")
    bids, asks = [(p,s) for p,s in bids if s>0], [(p,s) for p,s in asks if s>0]
    if not asks or not bids:
        raise Skip("leeg_orderboek")
    spread = min(p for p,s in asks)-max(p for p,s in bids)
    if not 0 < spread <= MAX_SPREAD:
        raise Skip("spread")
    left, qty, fees, notional = budget, 0.0, 0.0, 0.0
    for raw, size in sorted(asks):
        p = raw+tick*SLIPPAGE_TICKS
        if p > MAX_PRICE:
            break
        fee = market["fee_rate"]*(p*(1-p))**market["fee_exponent"]
        take = min(size, left/(p+fee))
        qty += take
        fees += take*fee
        notional += take*p
        left -= take*(p+fee)
        if left < 1e-9:
            break
    if left > 1e-6:
        raise Skip("onvoldoende_dieptes_onder_prijsplafond")
    qty = math.floor(qty*1e6)/1e6
    if qty < minimum:
        raise Skip("minimum_aandelen")
    return {"qty": qty, "spent": notional+fees, "fee": fees,
            "vwap": notional/qty, "all_in": (notional+fees)/qty, "spread": spread,
            "price_bin": min(9, int((notional/qty)*10)), "book_ts": book.timestamp.timestamp()}


async def discover(client, start):
    slug = f"btc-updown-5m-{start}"
    m = await asyncio.wait_for(client.get_market(slug=slug), 8)
    s = m.state
    if m.slug != slug or not m.condition_id or s.end_date is None or abs(s.end_date.timestamp()-start-300)>0.1:
        raise Skip("verkeerde_markt_of_eindtijd")
    if s.closed or not s.active or not s.accepting_orders or not s.enable_order_book:
        raise Skip("markt_niet_open")
    description = (m.description or "").lower()
    if "chainlink" not in description or not any(x in description for x in ("twap", "time-weighted", "time weighted")):
        raise Skip("twap_marktregels_niet_bevestigd")
    source = str(m.resolution.source or "")
    if not source.startswith("https://data.chain.link/") or "btc-usd-twap-60s" not in source:
        raise Skip("60s_btc_resolutiebron_niet_bevestigd")
    f = m.trading.fee_schedule
    if m.trading.fees_enabled is not False and f is None:
        raise Skip("fee_onbekend")
    rate = number(f.rate) if f and m.trading.fees_enabled is not False else 0.0
    exponent = number(f.exponent) if f else 1.0
    if not 0 <= rate <= 1 or not 0 <= exponent <= 10:
        raise Skip("fee_schema_ongeldig")
    version = getattr(m.version, "value", m.version)
    if version not in ("v1", "v2"):
        raise Skip("marktversie_onbekend")
    assets, indices = {}, {}
    for i, o in enumerate((m.outcomes.yes, m.outcomes.no)):
        label = o.label.strip().upper()
        asset = o.position_id if version == "v2" else o.token_id
        if label not in ("UP", "DOWN") or not asset:
            raise Skip("uitkomst_mapping")
        assets[label], indices[label] = str(asset), i
    if len(assets) != 2 or len(set(assets.values())) != 2:
        raise Skip("uitkomst_mapping")
    return {"start": start, "end": start+300, "slug": slug, "condition": str(m.condition_id),
            "assets": assets, "indices": indices, "fee_rate": rate, "fee_exponent": exponent,
            "seconds_delay": max(0,number(m.trading.seconds_delay or 0)),
            "rules_hash": hashlib.sha256((description+source).encode()).hexdigest(),
            "description": description, "resolution_source": source}


def payout(resolutions, market):
    matches = [r for r in resolutions if str(r.condition_id)==market["condition"]
               and getattr(r.status, "value", r.status)=="resolved"]
    if len(matches)!=1 or matches[0].payouts is None:
        return None
    values = [number(x) for x in matches[0].payouts]
    if len(values)!=2 or any(x<0 or x>1 for x in values) or abs(sum(values)-1)>1e-8:
        raise Skip("ongeldige_officiele_uitbetaling")
    return {side: values[i] for side, i in market["indices"].items()}


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1), schema TEXT,
            initial REAL, cash REAL, day TEXT, day_start REAL, entries INTEGER);
        CREATE TABLE IF NOT EXISTS markets(start INTEGER PRIMARY KEY, data TEXT NOT NULL,
            resolved_at REAL, up REAL, down REAL, last_poll REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS observations(start INTEGER, checkpoint INTEGER, at REAL,
            kind TEXT, side TEXT, gap_bin INTEGER, price_bin INTEGER, details TEXT,
            result INTEGER, PRIMARY KEY(start,checkpoint));
        CREATE INDEX IF NOT EXISTS obs_cell ON observations(checkpoint,kind,side,gap_bin,price_bin);
        CREATE TABLE IF NOT EXISTS positions(start INTEGER PRIMARY KEY, side TEXT, qty REAL,
            spent REAL, fee REAL, entered REAL, details TEXT, resolved_at REAL, proceeds REAL, pnl REAL);
        CREATE TABLE IF NOT EXISTS ticks(channel TEXT, ts REAL, value REAL, received REAL,
            PRIMARY KEY(channel,ts));
        CREATE TABLE IF NOT EXISTS skips(reason TEXT PRIMARY KEY, count INTEGER);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO account VALUES(1,?,?,?, ?,?,0)",
                            (SCHEMA, INITIAL_CASH, INITIAL_CASH, day(time.time()), INITIAL_CASH))
        if self.account()["schema"] != SCHEMA:
            raise RuntimeError("Database hoort bij andere strategie; niet overschrijven")
        self.verify()

    def account(self):
        return dict(self.db.execute("SELECT * FROM account WHERE id=1").fetchone())

    def verify(self):
        a = self.account()
        used = self.db.execute("SELECT COALESCE(SUM(spent),0),COALESCE(SUM(proceeds),0) FROM positions").fetchone()
        if not math.isclose(a["cash"], a["initial"]-used[0]+used[1], abs_tol=1e-6):
            raise RuntimeError("Paperboekhouding sluit niet aan; gestopt")

    def roll_day(self, now):
        a = self.account()
        if a["day"] != day(now):
            # Open stakes valued at cost for the new day's starting budget; still fully reserved.
            reserved = self.db.execute("SELECT COALESCE(SUM(spent),0) FROM positions WHERE resolved_at IS NULL").fetchone()[0]
            with self.db:
                self.db.execute("UPDATE account SET day=?,day_start=?,entries=0 WHERE id=1", (day(now),a["cash"]+reserved))

    def skip(self, reason):
        with self.db:
            self.db.execute("INSERT INTO skips VALUES(?,1) ON CONFLICT(reason) DO UPDATE SET count=count+1",(reason,))

    def market(self, market):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO markets(start,data) VALUES(?,?)",(market["start"],json.dumps(market)))

    def ticks(self, rows):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO ticks VALUES(?,?,?,?)", rows)

    def estimate(self, checkpoint, feature, q, now):
        rows = self.db.execute("""SELECT o.start,o.at,m.resolved_at,o.result
          FROM observations o JOIN markets m USING(start)
          WHERE checkpoint=? AND kind=? AND side=? AND gap_bin=? AND price_bin=?
          AND o.start>=? AND m.resolved_at<? AND o.result IN (0,1)""",
          (checkpoint,feature["kind"],feature["side"],feature["gap_bin"],q["price_bin"],
           now-TRAIN_DAYS*86400,now)).fetchall()
        return history_estimate(rows,now)

    def observed(self, start, checkpoint):
        return self.db.execute("SELECT 1 FROM observations WHERE start=? AND checkpoint=?",(start,checkpoint)).fetchone() is not None

    def observe(self, market, checkpoint, f, q, estimate):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,NULL)",
                (market["start"],checkpoint,f["at"],f["kind"],f["side"],f["gap_bin"],q["price_bin"],
                 json.dumps({"feature":f,"quote":q,"estimate_before_result":estimate})))

    def can_enter(self, start, spent):
        if self.db.execute("SELECT 1 FROM positions WHERE start=?",(start,)).fetchone():
            raise Skip("al_gehandeld_deze_ronde")
        if self.db.execute("SELECT 1 FROM positions WHERE resolved_at IS NULL").fetchone():
            raise Skip("positie_of_afrekening_nog_open")
        a = self.account()
        if a["entries"] >= MAX_DAILY_ENTRIES:
            raise Skip("daglimiet_aantal")
        if spent > PAPER_STAKE+1e-6 or spent <= 0 or a["cash"] < spent:
            raise Skip("onvoldoende_paperbudget")
        if a["cash"]-spent < a["day_start"]*(1-MAX_DAILY_LOSS)-1e-6:
            raise Skip("daglimiet_maximaal_verlies")

    def enter(self, market, f, q, estimate, now):
        self.can_enter(market["start"],q["spent"])
        details = json.dumps({"feature":f,"quote":q,"estimate":estimate})
        with self.db:
            self.db.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)",
                            (market["start"],f["side"],q["qty"],q["spent"],q["fee"],now,details))
            self.db.execute("UPDATE account SET cash=cash-?,entries=entries+1 WHERE id=1",(q["spent"],))
        self.verify()

    def settle(self, market, pay, now):
        row = self.db.execute("SELECT resolved_at FROM markets WHERE start=?",(market["start"],)).fetchone()
        if row is None or row[0] is not None:
            return None
        trade = self.db.execute("SELECT * FROM positions WHERE start=? AND resolved_at IS NULL",(market["start"],)).fetchone()
        result = None
        with self.db:
            self.db.execute("UPDATE markets SET resolved_at=?,up=?,down=? WHERE start=?",(now,pay["UP"],pay["DOWN"],market["start"]))
            # Fractional/void outcomes settle money but are excluded from binary training labels.
            if sorted(pay.values()) == [0.0,1.0]:
                for side in ("UP","DOWN"):
                    self.db.execute("UPDATE observations SET result=? WHERE start=? AND side=?",(int(pay[side]),market["start"],side))
            if trade:
                proceeds = trade["qty"]*pay[trade["side"]]
                pnl = proceeds-trade["spent"]
                self.db.execute("UPDATE positions SET resolved_at=?,proceeds=?,pnl=? WHERE start=?",(now,proceeds,pnl,market["start"]))
                self.db.execute("UPDATE account SET cash=cash+? WHERE id=1",(proceeds,))
                result = {"side":trade["side"],"pnl":pnl,"proceeds":proceeds}
        self.verify()
        return result


class Bot:
    def __init__(self, store):
        self.store, self.ref = store, Reference()
        self.market = None
        self.last_report = self.last_discovery = self.last_resolve = 0
        self.last_cleanup = 0
        self.last_cell = None
        self.messages = asyncio.Queue(maxsize=50)
        self.health = {"version":VERSION,"mode":"PAPER","status":"starting"}
        self.started = time.time()
        self.attempted = set()

    def notify(self, message):
        LOG.info(message)
        with suppress(asyncio.QueueFull):
            self.messages.put_nowait(message)

    async def telegram(self):
        token = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
        if token.startswith("bot"):
            token = token[3:]
        chat = os.getenv("TELEGRAM_CHAT_ID","").strip()
        while True:
            message = await self.messages.get()
            if token and chat:
                def send():
                    # Never log request URLs or exceptions containing the Telegram token.
                    try:
                        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id":chat,"text":message[:4000],"disable_web_page_preview":True},timeout=8)
                        return r.status_code == 200 and r.json().get("ok") is True
                    except Exception:
                        return False
                if not await asyncio.to_thread(send):
                    LOG.warning("Telegram niet bezorgd; bot draait verder")
            self.messages.task_done()

    async def reference_feed(self):
        auth = {"apiKey":os.getenv("POLYMARKET_API_KEY","").strip(),
                "secret":os.getenv("POLYMARKET_API_SECRET","").strip(),
                "passphrase":os.getenv("POLYMARKET_API_PASSPHRASE","").strip()}
        if not all(auth.values()):
            self.ref.state = "API_credentials_ontbreken"
            self.notify("BTC Lead: referentiefeed wacht op POLYMARKET_API_KEY, POLYMARKET_API_SECRET en POLYMARKET_API_PASSPHRASE. Geen private key nodig; uitsluitend paper.")
            return
        delay = 1
        while True:
            self.ref.reset()
            try:
                async with connect(WS_URL, open_timeout=10, ping_interval=20, ping_timeout=20,
                                   max_queue=1024, max_size=262144) as ws:
                    await ws.send(json.dumps({"op":"auth","rid":"a1","auth":auth}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(),10))
                    if reply.get("op") != "authed":
                        raise Skip("feed_auth_mislukt")
                    await ws.send(json.dumps({"op":"subscribe","rid":"s1","subscriptions":[
                        {"channel":SPOT,"filter":{"symbol":"btcusd","provider":"chainlink"}},
                        {"channel":TWAP,"filter":{"symbol":"btcusd","window_seconds":60}}]}))
                    delay = 1
                    while True:
                        msg = json.loads(await asyncio.wait_for(ws.recv(),15))
                        op = msg.get("op")
                        if op == "error":
                            raise Skip("feed_abonnement_geweigerd")
                        if op == "subscribed" and msg.get("channel") in (SPOT,TWAP):
                            provider = msg.get("provider")
                            if provider is not None and provider != "chainlink":
                                raise Skip("feed_verkeerde_provider")
                            self.ref.acks.add(msg["channel"])
                            continue
                        if op is not None:
                            continue
                        rows = self.ref.apply(msg,time.time())
                        self.store.ticks(rows)
            except asyncio.CancelledError:
                raise
            except sqlite3.Error:
                raise
            except Exception as exc:
                self.ref.connected = False
                self.ref.state = str(exc) if isinstance(exc,Skip) else type(exc).__name__
                LOG.warning("Referentiefeed onderbroken: %s",self.ref.state)
                await asyncio.sleep(min(30,delay)+random.random())
                delay = min(30,delay*2)

    async def resolutions(self, client, now):
        # Poll a rotating batch; a broken market must not block other settlements.
        rows = self.store.db.execute("""SELECT start,data FROM markets WHERE resolved_at IS NULL
             AND start+300<=? ORDER BY last_poll LIMIT 8""",(now,)).fetchall()
        for row in rows:
            market = json.loads(row["data"])
            with self.store.db:
                self.store.db.execute("UPDATE markets SET last_poll=? WHERE start=?",(time.time(),row["start"]))
            try:
                rs = await asyncio.wait_for(client.get_resolutions(condition_ids=market["condition"]),5)
                pay = payout(rs,market)
                if pay is None:
                    continue
                result = self.store.settle(market,pay,time.time())
                if result:
                    self.notify(f"PAPER officieel afgerond {result['side']} | netto {result['pnl']:+.2f} USD\n{market['slug']}")
            except Skip as exc:
                self.store.skip(str(exc))
            except (asyncio.TimeoutError, OSError):
                self.store.skip("afrekening_tijdelijk_onbereikbaar")
            except sqlite3.Error:
                raise
            except Exception as exc:
                LOG.warning("Afrekening API: %s",type(exc).__name__)

    async def settlement_worker(self, client):
        while True:
            self.store.roll_day(time.time())
            await self.resolutions(client,time.time())
            await asyncio.sleep(10)

    async def measure(self, client, checkpoint):
        market = self.market
        start = market["start"]
        self.attempted.add((start,checkpoint))
        # Fixed sampling times, independent of direction/price/edge. Never retry until lucky.
        f = self.ref.feature(market,time.time())
        book = await asyncio.wait_for(client.get_order_book(asset_id=market["assets"][f["side"]]),3)
        now = time.time()
        f = self.ref.feature(market,now)
        if abs(f["remaining"]-checkpoint)>2:
            raise Skip("meetmoment_gemist")
        q = quote(book,market,f["side"],PAPER_STAKE,now)
        estimate = self.store.estimate(checkpoint,f,q,now)
        self.last_cell = {**estimate,"gap_bp":f["gap_bp"],"remaining":round(f["remaining"]),
                          "side":f["side"],"all_in":q["all_in"]}
        if self.store.observed(start,checkpoint):
            return
        self.store.observe(market,checkpoint,f,q,estimate)
        if not estimate["ready"]:
            raise Skip("data_verzamelen_onvoldoende_vergelijkbare_rondes")
        if estimate["lower"]-q["all_in"] < MIN_NET_EDGE:
            raise Skip("onvoldoende_netto_edge")
        self.store.can_enter(start,q["spent"])
        await asyncio.sleep(max(LATENCY_SECONDS,market.get("seconds_delay",0)))
        book2 = await asyncio.wait_for(client.get_order_book(asset_id=market["assets"][f["side"]]),3)
        now2 = time.time()
        f2 = self.ref.feature(market,now2)
        if abs(f2["remaining"]-checkpoint)>3:
            raise Skip("instap_te_laat")
        if any(f2[k]!=f[k] for k in ("side","kind","gap_bin")):
            raise Skip("voorsprong_veranderd")
        q2 = quote(book2,market,f2["side"],PAPER_STAKE,now2)
        if q2["price_bin"] != q["price_bin"]:
            raise Skip("prijsklasse_veranderd")
        if estimate["lower"]-q2["all_in"] < MIN_NET_EDGE:
            raise Skip("edge_verdwenen_na_latency")
        self.store.enter(market,f2,q2,estimate,now2)
        self.notify(f"PAPER BTC {f2['side']} | all-in {q2['spent']:.2f} USD\n"
                    f"Lead {f2['gap_bp']:.2f} bp | resterend {f2['remaining']:.0f}s | {f2['kind']}\n"
                    f"Historisch {estimate['p']:.1%}, ondergrens {estimate['lower']:.1%}; n={estimate['n']}\n"
                    f"Kost/aandeel {q2['all_in']:.4f}, geschatte fee {q2['fee']:.4f}\n"
                    f"Vasthouden tot officiële uitslag. https://polymarket.com/event/{market['slug']}")

    def report(self):
        a = self.store.account()
        db = self.store.db
        closed,wins,pnl = db.execute("SELECT COUNT(*),COALESCE(SUM(pnl>0),0),COALESCE(SUM(pnl),0) FROM positions WHERE resolved_at IS NOT NULL").fetchone()
        opened,reserved = db.execute("SELECT COUNT(*),COALESCE(SUM(spent),0) FROM positions WHERE resolved_at IS NULL").fetchone()
        observed = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        resolved = db.execute("SELECT COUNT(*) FROM markets WHERE resolved_at IS NOT NULL").fetchone()[0]
        skips = dict(db.execute("SELECT reason,count FROM skips ORDER BY count DESC LIMIT 6").fetchall())
        report = {"version":VERSION,"at":time.time(),"account":a,"closed":closed,"wins":wins,
                  "net_pnl":pnl,"open":opened,"reserved":reserved,"observations":observed,
                  "resolved_markets":resolved,"last_cell":self.last_cell,"skips":skips,
                  "trades":[dict(r) for r in db.execute("SELECT * FROM positions ORDER BY start DESC LIMIT 1000")]}
        for trade in report["trades"]:
            trade["details"] = json.loads(trade["details"])
        # Human-shareable report: no API credentials or Telegram IDs.
        path = DATA_DIR/"btc_lead_report.json"
        temp = path.with_suffix(".tmp")
        with temp.open("w") as out:
            json.dump(report,out,indent=2,allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp,path)
        self.notify(f"{VERSION}\nFeed: {self.ref.state}\nPaper vrij {a['cash']:.2f} | gereserveerd {reserved:.2f} | open {opened}\n"
                    f"Afgerond {closed}, winst {wins}, netto {pnl:+.2f} USD\n"
                    f"Dataset: {observed} meetpunten, {resolved} afgeronde rondes\n"
                    f"Laatste vergelijkbare cel: {json.dumps(self.last_cell,ensure_ascii=False)}\n"
                    f"Controles: {json.dumps(skips,ensure_ascii=False)}\n"
                    "Geen kansen ingebouwd uit onbevestigde percentages. Eerst data verzamelen; daarna alleen edge na kosten.")

    async def cycle(self, client):
        now = time.time()
        self.store.roll_day(now)
        now = time.time()
        start = int(now//300)*300
        if (self.market is None or self.market["start"]!=start) and now-self.last_discovery>=10:
            self.last_discovery = now
            self.market = None
            self.market = await discover(client,start)
            self.store.market(self.market)
            self.attempted = {x for x in self.attempted if x[0]==start}
        if self.market and self.market["start"]==start:
            remaining = self.market["end"]-time.time()
            for checkpoint in CHECKPOINTS:
                if 0 <= checkpoint-remaining <= 1.5 and (start,checkpoint) not in self.attempted:
                    await self.measure(client,checkpoint)
                    break

    async def run(self):
        self.notify(f"{VERSION} gestart. Nieuwe paperproef: BTC, {PAPER_STAKE:.2f} USD all-in, maximaal één positie.\n"
                    "Historische cellen worden opgebouwd uit officiële resultaten; zonder voldoende data geen orders.\n"
                    "Bewaar btc_lead_v1.sqlite3 op een Railway-volume. Oude bot stoppen om dubbele processen te voorkomen.")
        async with AsyncPublicClient() as client:
            feed = asyncio.create_task(self.reference_feed())
            telegram = asyncio.create_task(self.telegram())
            settlement = asyncio.create_task(self.settlement_worker(client))
            try:
                while True:
                    if feed.done() and feed.exception() is not None:
                        raise feed.exception()
                    if settlement.done():
                        raise settlement.exception() or RuntimeError("Afrekeningstaak gestopt")
                    try:
                        await self.cycle(client)
                    except Skip as exc:
                        self.store.skip(str(exc))
                    except (sqlite3.Error, RuntimeError):
                        raise
                    except Exception as exc:
                        self.store.skip("api_fout_"+type(exc).__name__)
                        LOG.warning("Cyclus overgeslagen: %s",type(exc).__name__)
                    now = time.time()
                    self.health.update(status="running",feed=self.ref.state,last_cycle=now)
                    if now-self.last_report >= 900:
                        self.last_report = now
                        self.report()
                    if now-self.last_cleanup >= 3600:
                        self.last_cleanup = now
                        with self.store.db:
                            self.store.db.execute("DELETE FROM ticks WHERE ts<?",(now-7*86400,))
                    await asyncio.sleep(0.25)
            finally:
                feed.cancel()
                telegram.cancel()
                settlement.cancel()
                await asyncio.gather(feed,telegram,settlement,return_exceptions=True)


def health_server(bot):
    if not os.getenv("PORT"):
        return
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/","/health"):
                self.send_error(404)
                return
            payload = json.dumps(bot.health).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self,*args):
            pass
    server = ThreadingHTTPServer(("0.0.0.0",int(os.environ["PORT"])),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()


def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    if not (0<PAPER_STAKE<=5 and 0<MIN_NET_EDGE<1 and MIN_SAMPLES>=100 and MIN_DAYS>=3):
        raise RuntimeError("Ongeldige paperconfiguratie")
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    # No competing workers on the same ledger. Railway replicas must also remain 1.
    with (DATA_DIR/"btc_lead_v1.lock").open("w") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        store = Store(DATA_DIR/DB_NAME)
        bot = Bot(store)
        health_server(bot)
        try:
            asyncio.run(bot.run())
        finally:
            store.db.close()


if __name__ == "__main__":
    main()
