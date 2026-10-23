"""BTC Lead v1.2 — opslagcontrole, vaste metingen en herstartveiligheid.

PAPER ONLY. Start via bot.py dat import strategy; strategy.main() uitvoert.
De database en bestaande historische categorieën blijven compatibel met v1.1.
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
import shutil
import signal
import sqlite3
import threading
import time
import uuid

import requests
from polymarket import AsyncPublicClient
from polymarket.errors import RateLimitError
from websockets.asyncio.client import connect

VERSION = "1.2-BTC-Lead-TWAP-PAPER"
PAPER_STAKE = 5.0
INITIAL_CASH = 1000.0
MIN_NET_EDGE = 0.03
MIN_SAMPLES = 100
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
# Eerste vaste meting 3s NA de bel. Eigen categorie 297; nooit mengen met 300.
CHECKPOINTS = (297, 285, 240, 180, 120, 90, 60, 45, 30, 15)
GAP_EDGES = (0, 2, 5, 10, 20, 50, float("inf"))
SCHEMA = "btc_chainlink60_projected_v1"
DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("BOT_DATA_DIR", "data"))
DB_NAME = "btc_lead_v1.sqlite3"
WS_URL = "wss://ws-live-v2.polymarket.com/ws"
SPOT, TWAP = "price.crypto", "price.crypto.twap"
LOG = logging.getLogger("btc-lead")


class Skip(Exception):
    pass


class StorageError(RuntimeError):
    pass


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise Skip("niet_eindig_getal")
    return result


def day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def unix_ms(value):
    result = number(value) / 1000
    if result < 1_600_000_000:
        raise Skip("timestamp_eenheid")
    return result


def gap_bin(bp):
    return max(0, min(len(GAP_EDGES)-2, bisect.bisect_right(GAP_EDGES, abs(bp))-1))


def wilson_lower(wins, n, z=1.96):
    if n <= 0:
        return 0.0
    p = wins/n
    return (p+z*z/(2*n)-z*math.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)


def history_estimate(rows, now):
    valid = [r for r in rows if now-TRAIN_DAYS*86400 <= r[0] < now
             and r[1] < r[2] < now and r[3] in (0, 1)]
    unique = {r[0]: r for r in valid}
    n = len(unique)
    wins = sum(r[3] for r in unique.values())
    days = len({day(r[0]) for r in unique.values()})
    return {"n": n, "wins": wins, "days": days, "p": wins/n if n else None,
            "lower": wilson_lower(wins, n), "ready": n >= MIN_SAMPLES and days >= MIN_DAYS}


def atomic_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(data, output, indent=2, ensure_ascii=False, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def storage_status(directory):
    directory = directory.resolve()
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    railway = bool(os.getenv("RAILWAY_SERVICE_ID") or os.getenv("RAILWAY_PROJECT_ID")
                   or os.getenv("RAILWAY_ENVIRONMENT_ID"))
    mounted = False
    if mount:
        root = Path(mount).resolve()
        mounted = directory.is_relative_to(root) and os.path.ismount(root)
    return {"path": str(directory), "railway": railway, "volume_verified": mounted,
            "entries_allowed": not railway or mounted,
            "status": ("volume_gecontroleerd" if mounted else
                       "Railway_volume_niet_bevestigd" if railway else "lokale_opslag")}


class PublicReads:
    TIMEOUTS = {"markt": 8, "orderboek": 3, "afrekening": 5}

    def __init__(self, notify=None):
        self.notify, self.states = notify, {}

    def ready(self, operation):
        return time.monotonic() >= self.states.get(operation, {}).get("retry_mono", 0)

    async def call(self, operation, function, **kwargs):
        state = self.states.setdefault(operation, {"failures": 0, "retry_mono": 0,
            "last_ok": None, "last_error": None, "last_notice": None})
        if not self.ready(operation):
            raise Skip("api_wachttijd_" + operation)
        try:
            result = await asyncio.wait_for(function(**kwargs), self.TIMEOUTS[operation])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            now, mono = time.time(), time.monotonic()
            status = 429 if isinstance(exc, RateLimitError) else getattr(exc, "status", None)
            if type(status) is not int or not 100 <= status <= 599:
                status = None
            state["failures"] += 1
            delay = min(300, 10 * 2 ** min(state["failures"]-1, 5))
            if status in (400, 401, 403, 422):
                delay = max(60, delay)
            suggested = getattr(exc, "retry_after", None)
            if type(suggested) in (int, float) and math.isfinite(suggested) and suggested >= 0:
                delay = max(delay, suggested)
            state["retry_mono"] = mono+delay
            state["last_error"] = {"at": now, "operation": operation, "http_status": status,
                                   "type": type(exc).__name__, "wait_seconds": delay}
            if state["last_notice"] is None or mono-state["last_notice"] >= 300:
                state["last_notice"] = mono
                message = (f"API {operation}: HTTP {status or 'onbekend'} "
                           f"({type(exc).__name__}); wachttijd {delay:.0f}s.")
                if status in (401, 403):
                    message += " Toegang/configuratie controleren als dit aanhoudt."
                (self.notify or LOG.warning)(message)
            # No exception text, response bodies, headers or secret-bearing URLs.
            raise Skip(f"api_{operation}_http_{status or 'onbekend'}") from None
        state.update(failures=0, retry_mono=0, last_ok=time.time())
        return result

    def summary(self):
        now = time.monotonic()
        return {name: {"consecutive_failures": state["failures"],
                "retry_in_seconds": max(0, math.ceil(state["retry_mono"]-now)),
                "last_ok": state["last_ok"], "last_error": state["last_error"]}
                for name, state in self.states.items()}


class Reference:
    def __init__(self):
        self.points = {SPOT: {}, TWAP: {}}
        self.anchors, self.invalid_anchors = {}, set()
        self.connected, self.state = False, "wachten_op_feed"
        self.sequences, self.acks = {}, set()

    def reset(self):
        self.points = {SPOT: {}, TWAP: {}}
        self.sequences.clear()
        self.acks.clear()
        self.connected, self.state = False, "wachten_op_feed"

    def apply(self, msg, now):
        channel = msg.get("channel")
        if channel not in self.points:
            return []
        if msg.get("v") != 1:
            raise Skip("feed_protocol")
        seq = msg.get("seq")
        if type(seq) is not int:
            raise Skip("feed_sequence_ontbreekt")
        previous = self.sequences.get(channel)
        if msg.get("dropped", 0) or (previous is not None and seq != previous+1):
            raise Skip("feed_sequence_gat")
        payload = msg.get("payload", {})
        if payload.get("symbol") != "btcusd" or payload.get("source") != "chainlink":
            raise Skip("verkeerde_referentiebron")
        if channel == TWAP and payload.get("window_seconds") != 60:
            raise Skip("verkeerd_twap_venster")
        items = payload.get("data", []) if msg.get("snapshot") is True else [payload]
        if not isinstance(items, list):
            raise Skip("feed_snapshot_formaat")
        # Validate the WHOLE frame before publishing any of its prices.
        pending = {}
        for item in items:
            ts = unix_ms(item["timestamp"])
            value = number(item.get("full_accuracy_value", item.get("value")))
            if value <= 0 or ts > now+0.5 or now-ts > 600:
                raise Skip("feed_prijs_of_tijd")
            if item.get("is_carried_forward") or payload.get("is_carried_forward"):
                raise Skip("feed_doorgerolde_prijs")
            existing = pending.get(ts) or self.points[channel].get(ts)
            if channel == TWAP and ts in self.anchors:
                existing = self.anchors[ts]
            if existing and not math.isclose(existing[0], value, rel_tol=1e-12):
                if channel == TWAP and ts % 300 == 0:
                    self.invalid_anchors.add(ts)
                raise Skip("feed_herziening")
            pending[ts] = (value, now)
        self.sequences[channel] = seq
        self.points[channel].update(pending)
        if channel == TWAP:
            for ts, value in pending.items():
                if ts % 300 == 0:
                    self.anchors.setdefault(ts, value)
        for kind in self.points:
            self.points[kind] = {t: v for t, v in self.points[kind].items() if t >= now-900}
        self.anchors = {t: v for t, v in self.anchors.items() if t >= now-900}
        self.invalid_anchors = {t for t in self.invalid_anchors if t >= now-900}
        self.connected = self.acks == {SPOT, TWAP}
        self.state = "verbonden" if self.connected else "wachten_op_abonnement"
        return [(channel, ts, value[0], value[1]) for ts, value in pending.items()]

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

    def strike(self, start, now):
        if start in self.invalid_anchors:
            raise Skip("start_twap_herzien_geblokkeerd")
        point = self.anchors.get(float(start)) or self.points[TWAP].get(float(start))
        if point is None or point[1] > now:
            raise Skip("start_twap_ontbreekt")
        return point[0]

    def projected(self, end, now, spot):
        begin = end-60
        samples = sorted((t, v[0]) for t, v in self.points[SPOT].items() if t <= now and v[1] <= now)
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
        return (integral+spot*(end-now))/60, integral, now-begin

    def feature(self, market, now):
        remaining = market["end"]-now
        if not 0 < remaining <= 300:
            raise Skip("buiten_ronde")
        spot_ts, spot = self.current(now)
        strike = self.strike(market["start"], now)
        if remaining <= 60:
            reference, integral, elapsed = self.projected(market["end"], now, spot)
            kind = "projected_twap"
        else:
            reference, integral, elapsed, kind = spot, None, 0.0, "spot_lead"
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
    if any(not 0 < p < 1 or size < 0 for p, size in bids+asks):
        raise Skip("ongeldig_orderboek")
    bids, asks = [(p,s) for p,s in bids if s>0], [(p,s) for p,s in asks if s>0]
    if not asks or not bids:
        raise Skip("leeg_orderboek")
    spread = min(p for p,s in asks)-max(p for p,s in bids)
    if not 0 < spread <= MAX_SPREAD:
        raise Skip("spread")
    left, qty, fees, notional = budget, 0.0, 0.0, 0.0
    for raw, size in sorted(asks):
        price = raw+tick*SLIPPAGE_TICKS
        if price > MAX_PRICE:
            break
        fee = market["fee_rate"]*(price*(1-price))**market["fee_exponent"]
        take = min(size, left/(price+fee))
        qty += take
        fees += take*fee
        notional += take*price
        left -= take*(price+fee)
        if left < 1e-9:
            break
    if left > 1e-6:
        raise Skip("onvoldoende_dieptes_onder_prijsplafond")
    qty = math.floor(qty*1e6)/1e6
    if qty < minimum:
        raise Skip("minimum_aandelen")
    return {"qty": qty, "spent": notional+fees, "fee": fees, "vwap": notional/qty,
            "all_in": (notional+fees)/qty, "spread": spread,
            "price_bin": min(9, int((notional/qty)*10)), "book_ts": book.timestamp.timestamp()}


async def discover(client, start, api):
    slug = f"btc-updown-5m-{start}"
    m = await api.call("markt", client.get_market, slug=slug)
    state = m.state
    if m.slug != slug or not m.condition_id or state.end_date is None or abs(state.end_date.timestamp()-start-300)>0.1:
        raise Skip("verkeerde_markt_of_eindtijd")
    if state.closed or not state.active or not state.accepting_orders or not state.enable_order_book:
        raise Skip("markt_niet_open")
    description = (m.description or "").lower()
    if "chainlink" not in description or not any(x in description for x in ("twap", "time-weighted", "time weighted")):
        raise Skip("twap_marktregels_niet_bevestigd")
    source = str(m.resolution.source or "")
    if not source.startswith("https://data.chain.link/") or "btc-usd-twap-60s" not in source:
        raise Skip("60s_btc_resolutiebron_niet_bevestigd")
    schedule = m.trading.fee_schedule
    if m.trading.fees_enabled is not False and schedule is None:
        raise Skip("fee_onbekend")
    rate = number(schedule.rate) if schedule and m.trading.fees_enabled is not False else 0.0
    exponent = number(schedule.exponent) if schedule else 1.0
    if not 0 <= rate <= 1 or not 0 <= exponent <= 10:
        raise Skip("fee_schema_ongeldig")
    version = getattr(m.version, "value", m.version)
    if version not in ("v1", "v2"):
        raise Skip("marktversie_onbekend")
    assets, indices = {}, {}
    for index, outcome in enumerate((m.outcomes.yes, m.outcomes.no)):
        label = outcome.label.strip().upper()
        asset = outcome.position_id if version == "v2" else outcome.token_id
        if label not in ("UP", "DOWN") or not asset:
            raise Skip("uitkomst_mapping")
        assets[label], indices[label] = str(asset), index
    if len(assets) != 2 or len(set(assets.values())) != 2:
        raise Skip("uitkomst_mapping")
    return {"start": start, "end": start+300, "slug": slug, "condition": str(m.condition_id),
            "assets": assets, "indices": indices, "fee_rate": rate, "fee_exponent": exponent,
            "seconds_delay": max(0, number(m.trading.seconds_delay or 0)),
            "rules_hash": hashlib.sha256((description+source).encode()).hexdigest(),
            "description": description, "resolution_source": source}


def payout(resolutions, market):
    matches = [r for r in resolutions if str(r.condition_id) == market["condition"]
               and getattr(r.status, "value", r.status) == "resolved"]
    if len(matches) != 1 or matches[0].payouts is None:
        return None
    values = [number(x) for x in matches[0].payouts]
    if len(values) != 2 or any(x<0 or x>1 for x in values) or abs(sum(values)-1)>1e-8:
        raise Skip("ongeldige_officiele_uitbetaling")
    return {side: values[index] for side, index in market["indices"].items()}


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.path.parent / "btc_lead_storage.json"
        self.storage = storage_status(self.path.parent)
        previous = None
        if self.manifest_path.exists():
            try:
                previous = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if not isinstance(previous, dict) or not previous.get("database_id"):
                    raise ValueError()
            except (ValueError, OSError):
                raise StorageError("Opslagmarker onleesbaar; database niet automatisch vervangen.") from None
        existed = self.path.is_file() and self.path.stat().st_size > 0
        if previous and not existed:
            raise StorageError("Opslagmarker aanwezig maar database ontbreekt. Herstel de bestaande database; geen lege vervanger aangemaakt.")
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StorageError("SQLite-controle mislukt; herstel uit een gecontroleerde back-up.")
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
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS measurement_attempts(start INTEGER, checkpoint INTEGER,
                at REAL NOT NULL, status TEXT NOT NULL, PRIMARY KEY(start,checkpoint));
            CREATE TABLE IF NOT EXISTS reference_observations(start INTEGER, checkpoint INTEGER,
                at REAL NOT NULL, details TEXT NOT NULL, PRIMARY KEY(start,checkpoint));
            CREATE TABLE IF NOT EXISTS invalid_anchors(start REAL PRIMARY KEY);
            """)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO account VALUES(1,?,?,?,?,?,0)",
                    (SCHEMA, INITIAL_CASH, INITIAL_CASH, day(time.time()), INITIAL_CASH))
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES('database_id',?)", (uuid.uuid4().hex,))
            self.database_id = self.db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
            expected = os.getenv("BOT_EXPECTED_DB_ID", "").strip()
            if expected and expected != self.database_id:
                raise StorageError("Database-ID wijkt af van BOT_EXPECTED_DB_ID. Controleer volume en opslagpad.")
            if self.account()["schema"] != SCHEMA:
                raise StorageError("Database hoort bij een andere strategie; niet overschrijven.")
            self.verify()
            counts = self.counts()
            if previous:
                if previous["database_id"] != self.database_id:
                    raise StorageError("Database-ID verschilt van de opslagmarker; niet verder schrijven.")
                for key in ("observations", "markets", "positions"):
                    if counts[key] < previous.get("counts", {}).get(key, 0):
                        raise StorageError("Database bevat minder historie dan de opslagmarker; controleer herstel of opslagpad.")
            self.startup = "bestaande_database" if existed else "NIEUWE_LEGE_DATABASE"
            self.resumed_counts = counts
            self.check_disk()
            if existed:
                self.backup()
            self.save_manifest()
        except Exception:
            self.db.close()
            raise

    def account(self):
        return dict(self.db.execute("SELECT * FROM account WHERE id=1").fetchone())

    def counts(self):
        return {table: self.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in ("observations", "markets", "positions", "reference_observations")}

    def verify(self):
        account = self.account()
        spent, proceeds = self.db.execute(
            "SELECT COALESCE(SUM(spent),0),COALESCE(SUM(proceeds),0) FROM positions"
        ).fetchone()
        values = (account["initial"], account["cash"], spent, proceeds)
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in values):
            raise StorageError("Paperboekhouding bevat ongeldige bedragen.")
        if not math.isclose(account["cash"], account["initial"]-spent+proceeds, abs_tol=1e-6):
            raise StorageError("Paperboekhouding sluit niet aan; gestopt.")
        orphan = self.db.execute("SELECT 1 FROM positions p LEFT JOIN markets m USING(start) WHERE m.start IS NULL LIMIT 1").fetchone()
        if orphan:
            raise StorageError("Paperpositie zonder marktgegevens; herstel nodig voor afrekening.")

    def check_disk(self):
        if shutil.disk_usage(self.path.parent).free < 50*1024*1024:
            raise StorageError("Minder dan 50 MB vrije opslag; vergroot of ruim het volume op.")

    def save_manifest(self):
        atomic_json(self.manifest_path, {"database_id": self.database_id,
            "at": time.time(), "version": VERSION, "counts": self.counts(),
            "database_path": str(self.path)})

    def backup(self):
        folder = self.path.parent / "backups"
        folder.mkdir(exist_ok=True)
        destination = folder / ("btc_lead_" + day(time.time()) + ".sqlite3")
        if destination.exists():
            return
        self.check_disk()
        temporary = destination.with_suffix(".tmp")
        if shutil.disk_usage(self.path.parent).free < self.path.stat().st_size + 50*1024*1024:
            raise StorageError("Onvoldoende vrije ruimte voor een consistente databaseback-up.")
        source = sqlite3.connect(self.path.as_uri()+"?mode=ro", uri=True, timeout=5)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target, pages=512)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StorageError("Back-upcontrole mislukt.")
        finally:
            target.close()
            source.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        # Maximaal twee dagelijkse lokale kopieën; bron bevat de volledige historie.
        for old in sorted(folder.glob("btc_lead_*.sqlite3"))[:-2]:
            old.unlink()

    def roll_day(self, now):
        account = self.account()
        if account["day"] != day(now):
            reserved = self.db.execute("SELECT COALESCE(SUM(spent),0) FROM positions WHERE resolved_at IS NULL").fetchone()[0]
            with self.db:
                self.db.execute("UPDATE account SET day=?,day_start=?,entries=0 WHERE id=1",
                                (day(now), account["cash"]+reserved))

    def skip(self, reason):
        with self.db:
            self.db.execute("INSERT INTO skips VALUES(?,1) ON CONFLICT(reason) DO UPDATE SET count=count+1", (reason,))

    def market(self, market):
        row = self.db.execute("SELECT data FROM markets WHERE start=?", (market["start"],)).fetchone()
        if row:
            saved = json.loads(row[0])
            if any(saved.get(key) != market.get(key) for key in ("condition", "assets", "indices", "rules_hash")):
                raise StorageError("Marktidentiteit of regels veranderden binnen dezelfde ronde.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO markets(start,data) VALUES(?,?)", (market["start"], json.dumps(market)))

    def ticks(self, rows):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO ticks VALUES(?,?,?,?)", rows)

    def invalidate_anchors(self, starts):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO invalid_anchors VALUES(?)", [(x,) for x in starts])

    def start_prices(self, now):
        rows = self.db.execute("""SELECT ts,value,received FROM ticks
            WHERE channel=? AND ts>=? AND ts<=? AND received<=?
            AND ts NOT IN (SELECT start FROM invalid_anchors)""", (TWAP, now-900, now, now)).fetchall()
        return {ts: (value, received) for ts, value, received in rows
                if math.isfinite(ts) and ts % 300 == 0 and math.isfinite(value) and value > 0
                and math.isfinite(received) and received >= ts-0.5}

    def claim_attempt(self, start, checkpoint, now):
        # Older successfully stored observations already constitute completed attempts.
        if self.observed(start, checkpoint):
            return False
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO measurement_attempts VALUES(?,?,?,'gestart')",
                                     (start, checkpoint, now))
        return cursor.rowcount == 1

    def finish_attempt(self, start, checkpoint, status):
        with self.db:
            self.db.execute("UPDATE measurement_attempts SET status=? WHERE start=? AND checkpoint=?",
                            (status, start, checkpoint))

    def reference_observe(self, start, checkpoint, feature):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO reference_observations VALUES(?,?,?,?)",
                            (start, checkpoint, feature["at"], json.dumps(feature)))

    def estimate(self, checkpoint, feature, quote_data, now):
        rows = self.db.execute("""SELECT o.start,o.at,m.resolved_at,o.result
          FROM observations o JOIN markets m USING(start)
          WHERE checkpoint=? AND kind=? AND side=? AND gap_bin=? AND price_bin=?
          AND o.start>=? AND m.resolved_at<? AND o.result IN (0,1)""",
          (checkpoint, feature["kind"], feature["side"], feature["gap_bin"], quote_data["price_bin"],
           now-TRAIN_DAYS*86400, now)).fetchall()
        return history_estimate(rows, now)

    def observed(self, start, checkpoint):
        return self.db.execute("SELECT 1 FROM observations WHERE start=? AND checkpoint=?", (start,checkpoint)).fetchone() is not None

    def observe(self, market, checkpoint, feature, quote_data, estimate):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,NULL)",
                (market["start"], checkpoint, feature["at"], feature["kind"], feature["side"],
                 feature["gap_bin"], quote_data["price_bin"], json.dumps({"feature": feature,
                 "quote": quote_data, "estimate_before_result": estimate})))

    def can_enter(self, start, spent):
        if not self.storage["entries_allowed"]:
            raise Skip("paperinstap_geblokkeerd_volume_niet_bevestigd")
        if not math.isfinite(spent):
            raise Skip("ongeldig_paperbedrag")
        if self.db.execute("SELECT 1 FROM positions WHERE start=?", (start,)).fetchone():
            raise Skip("al_gehandeld_deze_ronde")
        if self.db.execute("SELECT 1 FROM positions WHERE resolved_at IS NULL").fetchone():
            raise Skip("positie_of_afrekening_nog_open")
        account = self.account()
        if account["entries"] >= MAX_DAILY_ENTRIES:
            raise Skip("daglimiet_aantal")
        if spent > PAPER_STAKE+1e-6 or spent <= 0 or account["cash"] < spent:
            raise Skip("onvoldoende_paperbudget")
        if account["cash"]-spent < account["day_start"]*(1-MAX_DAILY_LOSS)-1e-6:
            raise Skip("daglimiet_maximaal_verlies")

    def enter(self, market, feature, quote_data, estimate, now):
        self.can_enter(market["start"], quote_data["spent"])
        details = json.dumps({"feature": feature, "quote": quote_data, "estimate": estimate})
        with self.db:
            self.db.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)",
                (market["start"], feature["side"], quote_data["qty"], quote_data["spent"], quote_data["fee"], now, details))
            self.db.execute("UPDATE account SET cash=cash-?,entries=entries+1 WHERE id=1", (quote_data["spent"],))
        self.verify()

    def settle(self, market, pay, now):
        if set(pay) != {"UP", "DOWN"} or any(not math.isfinite(x) or not 0<=x<=1 for x in pay.values()) or abs(sum(pay.values())-1)>1e-8:
            raise Skip("ongeldige_officiele_uitbetaling")
        row = self.db.execute("SELECT resolved_at FROM markets WHERE start=?", (market["start"],)).fetchone()
        if row is None or row[0] is not None:
            return None
        trade = self.db.execute("SELECT * FROM positions WHERE start=? AND resolved_at IS NULL", (market["start"],)).fetchone()
        result = None
        with self.db:
            self.db.execute("UPDATE markets SET resolved_at=?,up=?,down=? WHERE start=?", (now,pay["UP"],pay["DOWN"],market["start"]))
            if sorted(pay.values()) == [0.0, 1.0]:
                for side in ("UP", "DOWN"):
                    self.db.execute("UPDATE observations SET result=? WHERE start=? AND side=?", (int(pay[side]),market["start"],side))
            if trade:
                proceeds = trade["qty"]*pay[trade["side"]]
                pnl = proceeds-trade["spent"]
                self.db.execute("UPDATE positions SET resolved_at=?,proceeds=?,pnl=? WHERE start=?", (now,proceeds,pnl,market["start"]))
                self.db.execute("UPDATE account SET cash=cash+? WHERE id=1", (proceeds,))
                result = {"side": trade["side"], "pnl": pnl, "proceeds": proceeds}
        self.verify()
        return result

    def close(self):
        try:
            self.save_manifest()
            self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            self.db.close()


class Bot:
    def __init__(self, store):
        self.store, self.ref = store, Reference()
        self.started = time.time()
        self.ref.anchors = store.start_prices(self.started)
        self.ref.invalid_anchors = {r[0] for r in store.db.execute("SELECT start FROM invalid_anchors WHERE start>=?", (self.started-900,))}
        self.market, self.discovery, self.discovery_start = None, None, None
        self.last_discovery = self.last_report = self.last_maintenance = 0
        self.last_cell = None
        self.messages = asyncio.Queue(maxsize=50)
        self.api = PublicReads(self.notify)
        self.previous_counts = Counter(dict(store.db.execute("SELECT reason,count FROM skips")))
        self.previous_observed = store.counts()["observations"]
        self.previous_report_at = self.started
        self.attempted, self.notices = set(), {}
        self.stop = asyncio.Event()
        self.health = {"version": VERSION, "mode": "PAPER", "status": "starting", "telegram_failures": 0}

    def notify(self, message):
        LOG.info(message)
        try:
            self.messages.put_nowait(message)
        except asyncio.QueueFull:
            self.health["telegram_queue_dropped"] = self.health.get("telegram_queue_dropped", 0)+1

    def notice(self, key, message, interval=900):
        now = time.monotonic()
        if now-self.notices.get(key, -1e20) >= interval:
            self.notices[key] = now
            self.notify(message)

    async def telegram(self):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if token.startswith("bot"):
            token = token[3:]
        chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat:
            LOG.warning("Telegramconfiguratie ontbreekt; berichten zijn wel zichtbaar in de logs.")
        while True:
            message = await self.messages.get()
            try:
                if token and chat:
                    def send():
                        try:
                            response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat, "text": message[:4000], "disable_web_page_preview": True}, timeout=8)
                            return response.status_code == 200 and response.json().get("ok") is True
                        except Exception:
                            return False
                    if not await asyncio.to_thread(send):
                        self.health["telegram_failures"] += 1
                        LOG.warning("Telegram niet bezorgd; controleer configuratie/verbinding.")
            finally:
                self.messages.task_done()

    async def reference_feed(self):
        auth = {"apiKey": os.getenv("POLYMARKET_API_KEY", "").strip(),
                "secret": os.getenv("POLYMARKET_API_SECRET", "").strip(),
                "passphrase": os.getenv("POLYMARKET_API_PASSPHRASE", "").strip()}
        if not all(auth.values()):
            self.ref.state = "API_credentials_ontbreken"
            self.notify("Referentiefeed: POLYMARKET_API_KEY, POLYMARKET_API_SECRET of POLYMARKET_API_PASSPHRASE ontbreekt.")
            return
        delay = 1
        while True:
            self.ref.reset()
            connected_at = time.monotonic()
            try:
                async with connect(WS_URL, open_timeout=10, ping_interval=20, ping_timeout=20,
                                   max_queue=1024, max_size=262144) as websocket:
                    await websocket.send(json.dumps({"op": "auth", "rid": "a1", "auth": auth}))
                    reply = json.loads(await asyncio.wait_for(websocket.recv(), 10))
                    if reply.get("op") != "authed":
                        raise Skip("feed_auth_mislukt")
                    await websocket.send(json.dumps({"op": "subscribe", "rid": "s1", "subscriptions": [
                        {"channel": SPOT, "filter": {"symbol": "btcusd", "provider": "chainlink"}},
                        {"channel": TWAP, "filter": {"symbol": "btcusd", "window_seconds": 60}}]}))
                    while True:
                        message = json.loads(await asyncio.wait_for(websocket.recv(), 15))
                        operation = message.get("op")
                        if operation == "error":
                            raise Skip("feed_abonnement_geweigerd")
                        if operation == "subscribed" and message.get("channel") in (SPOT, TWAP):
                            if message.get("provider") not in (None, "chainlink"):
                                raise Skip("feed_verkeerde_provider")
                            self.ref.acks.add(message["channel"])
                            continue
                        if operation is not None:
                            continue
                        self.store.ticks(self.ref.apply(message, time.time()))
                        if time.monotonic()-connected_at >= 60:
                            delay = 1
            except asyncio.CancelledError:
                raise
            except (sqlite3.Error, StorageError):
                raise
            except Exception as exc:
                self.ref.connected = False
                self.ref.state = str(exc) if isinstance(exc, Skip) else type(exc).__name__
                self.store.invalidate_anchors(self.ref.invalid_anchors)
                self.store.skip("referentie_" + self.ref.state)
                self.notice("feed", "Referentiefeed onderbroken: " + self.ref.state, 300)
                await asyncio.sleep(min(30, delay)+random.random())
                delay = min(30, delay*2)

    async def resolutions(self, client, now):
        # Open money gets priority. Older unresolved markets still rotate afterward.
        rows = self.store.db.execute("""SELECT m.start,m.data FROM markets m
            WHERE m.resolved_at IS NULL AND m.start+300<=? AND m.last_poll<=?
            ORDER BY EXISTS(SELECT 1 FROM positions p WHERE p.start=m.start AND p.resolved_at IS NULL) DESC,
                     m.last_poll,m.start LIMIT 8""", (now, now-30)).fetchall()
        for row in rows:
            if not self.api.ready("afrekening"):
                break
            market = json.loads(row["data"])
            with self.store.db:
                self.store.db.execute("UPDATE markets SET last_poll=? WHERE start=?", (time.time(), row["start"]))
            try:
                result = await self.api.call("afrekening", client.get_resolutions, condition_ids=market["condition"])
                pay = payout(result, market)
                if pay is not None:
                    trade = self.store.settle(market, pay, time.time())
                    if trade:
                        self.notify(f"PAPER officieel afgerond {trade['side']} | netto {trade['pnl']:+.2f} USD\n{market['slug']}")
            except Skip as exc:
                self.store.skip(str(exc))
            except (sqlite3.Error, StorageError):
                raise
            except Exception as exc:
                self.store.skip("afrekening_" + type(exc).__name__)
                self.notice("afrekening", "Afrekening: onverwacht antwoord (" + type(exc).__name__ + ").")

    async def settlement_worker(self, client):
        while True:
            self.store.roll_day(time.time())
            await self.resolutions(client, time.time())
            await asyncio.sleep(10)

    async def measure(self, client, checkpoint):
        market = self.market
        start = market["start"]
        self.attempted.add((start, checkpoint))
        if not self.store.claim_attempt(start, checkpoint, time.time()):
            return
        status = "meetpunt_opgeslagen"
        try:
            await self._measure(client, checkpoint, market)
        except asyncio.CancelledError:
            status = "onderbroken_bij_stop_of_herstart"
            raise
        except Skip as exc:
            status = str(exc)
            raise
        except Exception:
            status = "onderbroken_of_programmafout"
            raise
        finally:
            self.store.finish_attempt(start, checkpoint, status)

    async def _measure(self, client, checkpoint, market):
        feature = self.ref.feature(market, time.time())
        self.store.reference_observe(market["start"], checkpoint, feature)
        requested_side = feature["side"]
        book = await self.api.call("orderboek", client.get_order_book, asset_id=market["assets"][requested_side])
        now = time.time()
        feature = self.ref.feature(market, now)
        if abs(feature["remaining"]-checkpoint)>2:
            raise Skip("meetmoment_gemist")
        if feature["side"] != requested_side:
            raise Skip("richting_veranderd_tijdens_orderboek")
        quote_data = quote(book, market, requested_side, PAPER_STAKE, now)
        estimate = self.store.estimate(checkpoint, feature, quote_data, now)
        self.last_cell = {**estimate, "at": now, "gap_bp": feature["gap_bp"],
            "remaining": round(feature["remaining"]), "side": requested_side,
            "all_in": quote_data["all_in"], "net_edge": estimate["lower"]-quote_data["all_in"]}
        self.store.observe(market, checkpoint, feature, quote_data, estimate)
        if not estimate["ready"]:
            raise Skip("data_verzamelen_onvoldoende_vergelijkbare_rondes")
        if estimate["lower"]-quote_data["all_in"] < MIN_NET_EDGE:
            raise Skip("onvoldoende_netto_edge")
        self.store.can_enter(market["start"], quote_data["spent"])
        await asyncio.sleep(max(LATENCY_SECONDS, market.get("seconds_delay", 0)))
        book_after = await self.api.call("orderboek", client.get_order_book, asset_id=market["assets"][requested_side])
        now_after = time.time()
        feature_after = self.ref.feature(market, now_after)
        if abs(feature_after["remaining"]-checkpoint)>3:
            raise Skip("instap_te_laat")
        if any(feature_after[key] != feature[key] for key in ("side", "kind", "gap_bin")):
            raise Skip("voorsprong_veranderd")
        quote_after = quote(book_after, market, requested_side, PAPER_STAKE, now_after)
        if quote_after["price_bin"] != quote_data["price_bin"]:
            raise Skip("prijsklasse_veranderd")
        if estimate["lower"]-quote_after["all_in"] < MIN_NET_EDGE:
            raise Skip("edge_verdwenen_na_latency")
        self.store.enter(market, feature_after, quote_after, estimate, now_after)
        self.notify(f"PAPER BTC {requested_side} | all-in {quote_after['spent']:.2f} USD\n"
            f"Lead {feature_after['gap_bp']:.2f} bp | resterend {feature_after['remaining']:.0f}s\n"
            f"Historisch {estimate['p']:.1%}, ondergrens {estimate['lower']:.1%}; n={estimate['n']}\n"
            f"Kost/aandeel {quote_after['all_in']:.4f}; geschatte fee {quote_after['fee']:.4f}\n"
            f"Vasthouden tot officiële uitslag. https://polymarket.com/event/{market['slug']}")

    async def cycle(self, client):
        now = time.time()
        start = int(now//300)*300
        self.store.roll_day(now)
        self.attempted = {item for item in self.attempted if item[0] == start}
        # Discovery runs independently; a slow lookup cannot freeze reporting/health.
        if self.discovery is not None and self.discovery.done():
            task, requested_start = self.discovery, self.discovery_start
            self.discovery = None
            try:
                market = task.result()
                if requested_start == start:
                    self.store.market(market)
                    self.market = market
            except Skip as exc:
                self.store.skip(str(exc))
            except (sqlite3.Error, StorageError):
                raise
            except Exception as exc:
                self.store.skip("marktantwoord_" + type(exc).__name__)
        needs_market = self.market is None or self.market["start"] != start
        if needs_market and self.discovery is None and now-self.last_discovery >= 10 and self.api.ready("markt"):
            self.last_discovery = now
            self.discovery_start = start
            self.discovery = asyncio.create_task(discover(client, start, self.api))
        remaining = start+300-time.time()
        for checkpoint in CHECKPOINTS:
            key = (start, checkpoint)
            if key in self.attempted:
                continue
            due = start+300-checkpoint
            if due < self.started or remaining > checkpoint:
                continue
            if checkpoint-remaining > 1.5:
                self.attempted.add(key)
                if self.store.claim_attempt(start, checkpoint, time.time()):
                    reason = "meetmoment_gemist_markt_niet_gereed" if needs_market else "meetmoment_gemist_planning"
                    self.store.finish_attempt(start, checkpoint, reason)
                    self.store.skip(reason)
                continue
            if needs_market:
                continue
            await self.measure(client, checkpoint)
            break

    def report(self):
        now = time.time()
        account, db = self.store.account(), self.store.db
        closed, wins, pnl = db.execute("SELECT COUNT(*),COALESCE(SUM(pnl>0),0),COALESCE(SUM(pnl),0) FROM positions WHERE resolved_at IS NOT NULL").fetchone()
        opened, reserved, oldest = db.execute("SELECT COUNT(*),COALESCE(SUM(spent),0),MIN(start) FROM positions WHERE resolved_at IS NULL").fetchone()
        counts = self.store.counts()
        resolved = db.execute("SELECT COUNT(*) FROM markets WHERE resolved_at IS NOT NULL").fetchone()[0]
        labelled = db.execute("SELECT COUNT(DISTINCT start) FROM observations WHERE result IN (0,1)").fetchone()[0]
        counters = Counter(dict(db.execute("SELECT reason,count FROM skips")))
        recent = dict((counters-self.previous_counts).most_common(8))
        added = counts["observations"]-self.previous_observed
        interval = max(0, now-self.previous_report_at)
        last_at = db.execute("SELECT MAX(at) FROM observations").fetchone()[0]
        stalled = now-self.started >= 900 and (last_at is None or now-last_at >= 900)
        most_developed = db.execute("""SELECT COUNT(DISTINCT o.start) AS n FROM observations o
            JOIN markets m USING(start) WHERE o.result IN (0,1) AND o.start>=? AND m.resolved_at<?
            GROUP BY checkpoint,kind,side,gap_bin,price_bin ORDER BY n DESC LIMIT 1""", (now-TRAIN_DAYS*86400, now)).fetchone()
        largest_n = most_developed[0] if most_developed else 0
        current_start = int(now//300)*300
        anchor_ready = current_start in self.ref.anchors and current_start not in self.ref.invalid_anchors
        api_status = self.api.summary()
        api_lines = []
        for operation, state in api_status.items():
            if state["consecutive_failures"]:
                api_lines.append(f"{operation}: HTTP {state['last_error']['http_status'] or 'onbekend'}, wacht {state['retry_in_seconds']}s")
            elif state["last_ok"] is not None:
                api_lines.append(f"{operation}: gelukt ({max(0, now-state['last_ok']):.0f}s geleden)")
        cell = self.last_cell
        if cell:
            chance = f"{cell['p']:.1%}" if cell["p"] is not None else "onbekend"
            cell_text = (f"{cell['side']}, n={cell['n']}/{MIN_SAMPLES}, dagen={cell['days']}/{MIN_DAYS}; "
                         f"historisch {chance}, ondergrens {cell['lower']:.1%}, kosten {cell['all_in']:.4f}; "
                         f"edge {cell['net_edge']:+.4f} (nodig {MIN_NET_EDGE:.2f}); {now-cell['at']:.0f}s geleden")
        else:
            cell_text = "nog geen uitvoerbaar meetpunt sinds herstart"
        pending_age = max(0, now-oldest-300) if oldest is not None else 0
        if pending_age >= 1800:
            self.notice("pending", "Paperpositie wacht >30 min na markteinde op officiële afrekening. Budget blijft gereserveerd.", 1800)
        report = {"version": VERSION, "at": now, "database_id": self.store.database_id,
            "storage": self.store.storage, "startup": self.store.startup, "account": account,
            "closed": closed, "wins": wins, "net_pnl": pnl, "open": opened, "reserved": reserved,
            **counts, "resolved_markets": resolved, "labelled_markets": labelled,
            "observations_added": added, "interval_seconds": interval, "last_observation_at": last_at,
            "collection_stalled": stalled, "recent_skips": recent, "api": api_status,
            "last_cell": cell, "largest_cell_n": largest_n, "start_twap_ready": anchor_ready,
            "pending_settlement_seconds": pending_age,
            "trades": [dict(row) for row in db.execute("SELECT * FROM positions ORDER BY start DESC LIMIT 1000")]}
        for trade in report["trades"]:
            trade["details"] = json.loads(trade["details"])
        self.store.save_manifest()
        atomic_json(self.store.path.parent/"btc_lead_report.json", report)
        self.notify(f"{VERSION}\nFeed: {self.ref.state}\n"
            f"Opslag: {self.store.storage['status']} | DB-ID {self.store.database_id}\n"
            f"Paper vrij {account['cash']:.2f} | gereserveerd {reserved:.2f} | open {opened}\n"
            f"Afgeronde trades {closed}, winst {wins}, netto {pnl:+.2f} USD\n"
            f"Uitvoerbare meetpunten {counts['observations']} (+{added} in {interval/60:.1f} min); "
            f"{resolved} afgeronde markten, {labelled} met trainingslabels\n"
            f"Extra referentiemetingen sinds v1.2: {counts['reference_observations']}\n"
            f"Grootste historische categorie: {largest_n}/{MIN_SAMPLES} rondes\n"
            f"Dataverzameling: {'STILSTAND ≥15 min' if stalled else 'opbouw/controle'}\n"
            f"Start-TWAP: {'bewaard' if anchor_ready else 'ontbreekt/geblokkeerd'}\n"
            f"API: {'; '.join(api_lines) or 'nog geen aanvraag'}\n"
            f"Laatste categorie: {cell_text}\n"
            f"Controles sinds vorig rapport: {json.dumps(recent, ensure_ascii=False)}\n"
            "PAPER. Geen bewijs van winstgevendheid zolang er geen getoetste handelsresultaten zijn.")
        self.previous_counts, self.previous_observed = counters, counts["observations"]
        self.previous_report_at = now

    async def maintenance(self):
        while True:
            await asyncio.sleep(3600)
            self.store.check_disk()
            with self.store.db:
                self.store.db.execute("DELETE FROM ticks WHERE ts<?", (time.time()-7*86400,))
                self.store.db.execute("DELETE FROM invalid_anchors WHERE start<?", (time.time()-7*86400,))
            # Backup uses an independent SQLite connection in a thread.
            await asyncio.to_thread(self.store.backup)

    async def run(self):
        startup = self.store.startup
        counts = self.store.resumed_counts
        self.notify(f"{VERSION} gestart | PAPER {PAPER_STAKE:.2f} USD\n"
            f"Opslag: {self.store.storage['status']}\nPad: {self.store.path}\n"
            f"Database: {startup}\nDB-ID: {self.store.database_id}\n"
            f"Teruggevonden: {counts['observations']} meetpunten, {counts['positions']} paperposities.\n"
            f"Eerste vaste meting: 3s na de bel. Instap vereist {MIN_SAMPLES} vergelijkbare rondes en {MIN_DAYS} UTC-kalenderdagen.")
        if startup == "NIEUWE_LEGE_DATABASE":
            self.notify("LET OP: deze start vond geen bestaande database. Als je al meetpunten had, controleer volume/opslagpad. Alleen gegevens op dit pad kunnen worden hervat.")
        if not self.store.storage["entries_allowed"]:
            self.notify("Railway-volume niet bevestigd. Verzamelen gaat door; paperinstappen zijn geblokkeerd. Koppel een echt volume en bewaar daarop de bestaande database. Maak de volumevariabelen niet zelf aan.")
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, self.stop.set)
        async with AsyncPublicClient() as client:
            feed = asyncio.create_task(self.reference_feed())
            telegram = asyncio.create_task(self.telegram())
            settlement = asyncio.create_task(self.settlement_worker(client))
            maintenance = asyncio.create_task(self.maintenance())
            try:
                while not self.stop.is_set():
                    for task in (feed, settlement, maintenance):
                        if task.done() and (task.exception() is not None or task is not feed):
                            raise task.exception() or RuntimeError("Achtergrondtaak onverwacht gestopt")
                    try:
                        await self.cycle(client)
                    except Skip as exc:
                        self.store.skip(str(exc))
                    except (sqlite3.Error, RuntimeError, OSError):
                        raise
                    except Exception as exc:
                        self.store.skip("cyclus_"+type(exc).__name__)
                        self.notice("cyclus", "Cyclus overgeslagen: "+type(exc).__name__)
                    now = time.time()
                    try:
                        self.ref.current(now)
                        fresh = True
                    except Skip:
                        fresh = False
                    self.health.update(status="running" if fresh else "degraded", feed=self.ref.state,
                        last_cycle=now, reference_fresh=fresh, storage=self.store.storage["status"],
                        last_api=self.api.summary())
                    if now-self.last_report >= 900:
                        self.last_report = now
                        self.store.check_disk()
                        self.report()
                    try:
                        await asyncio.wait_for(self.stop.wait(), 0.25)
                    except asyncio.TimeoutError:
                        pass
            finally:
                self.health["status"] = "stopping"
                tasks = [feed, settlement, maintenance]
                if self.discovery is not None:
                    tasks.append(self.discovery)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.messages.join(), 2)
                telegram.cancel()
                await asyncio.gather(telegram, return_exceptions=True)
                for signum in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(signum)


def health_server(bot):
    if not os.getenv("PORT"):
        return None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/health", "/ready"):
                self.send_error(404)
                return
            state = dict(bot.health)
            recent = time.time()-state.get("last_cycle", 0) < 15
            ready = recent and state.get("reference_fresh", False) and state.get("status") != "stopping"
            state["ready"] = ready
            payload = json.dumps(state, allow_nan=False).encode()
            self.send_response(200 if self.path != "/ready" or ready else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not (0<PAPER_STAKE<=5 and 0<MIN_NET_EDGE<1 and MIN_SAMPLES>=100 and MIN_DAYS>=3):
        raise RuntimeError("Ongeldige paperconfiguratie")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    selected = (DATA_DIR/DB_NAME).resolve()
    # Do not silently start empty when an older known path still has the database.
    if not selected.exists():
        for alternative in ((Path.cwd()/"data"/DB_NAME).resolve(), Path("/app/data")/DB_NAME):
            if alternative != selected and alternative.is_file():
                raise StorageError(f"Database gevonden op {alternative}, maar gekozen pad is {selected}. Verplaats eerst een consistente SQLite-back-up naar het juiste volume.")
    with (DATA_DIR/"btc_lead_v1.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StorageError("Er draait al een proces op deze database. Gebruik één replica.") from None
        store = Store(selected)
        bot = Bot(store)
        server = health_server(bot)
        try:
            asyncio.run(bot.run())
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            store.close()


if __name__ == "__main__":
    main()
