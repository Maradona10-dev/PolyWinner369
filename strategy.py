"""BTC Lead v1.3 — prospectieve toetsing en export via Telegram.

PAPER ONLY. Start via bot.py dat import strategy; strategy.main() uitvoert.
De database en bestaande historische categorieën blijven compatibel met v1.1.

=== TIJDELIJKE TEST-INSTELLINGEN (27 okt 2026) ===
- MIN_NET_EDGE verlaagd naar 0.00 zodat hij paper trades kan openen
- Volume-blokkade uitgezet (entries_allowed = True)
- MIN_SAMPLES / MIN_DAYS iets soepeler
Verwijder of zet terug naar productie-waarden zodra je genoeg paper trades hebt gezien.
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
import tempfile
import time
import uuid

import requests
from polymarket import AsyncPublicClient
from polymarket.errors import RateLimitError
from websockets.asyncio.client import connect

VERSION = "1.3-BTC-Lead-TWAP-PAPER"
PAPER_STAKE = 5.0
INITIAL_CASH = 1000.0

# ========== TIJDELIJKE TESTWAARDEN ==========
MIN_NET_EDGE = 0.00          # was 0.03  → tijdelijk 0 zodat hij trades kan openen
MIN_SAMPLES = 40             # was 100
MIN_DAYS = 2                 # was 3
# ===========================================

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


def mounted_path(path):
    if os.path.ismount(path):
        return True
    # Linux may report a same-filesystem bind mount as False in ismount().
    try:
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields=line.split()
            if len(fields)<5:
                continue
            mountpoint=fields[4]
            for encoded, decoded in ((r'\040',' '),(r'\011','\t'),(r'\012','\n'),(r'\134','\\')):
                mountpoint=mountpoint.replace(encoded,decoded)
            if Path(mountpoint).resolve()==path:
                return True
    except OSError:
        pass
    return False


def storage_status(directory):
    directory = directory.resolve()
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    railway = bool(os.getenv("RAILWAY_SERVICE_ID") or os.getenv("RAILWAY_PROJECT_ID")
                   or os.getenv("RAILWAY_ENVIRONMENT_ID"))
    mounted = False
    if mount:
        root = Path(mount).resolve()
        mounted = directory.is_relative_to(root) and mounted_path(root)
    return {"path": str(directory), "railway": railway, "volume_verified": mounted,
            "mount_path_configured": bool(mount),
            # ========== TIJDELIJK GEFROCEERD VOOR TEST ==========
            "entries_allowed": True,
            # ===================================================
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


def validation_protocol():
    """Fixed rules for forward evaluation; changing rules starts a new cohort."""
    return {"decision_model": "BTC_LEAD_RULES_V1_2", "schema": SCHEMA, "stake": PAPER_STAKE,
        "min_edge": MIN_NET_EDGE, "min_samples": MIN_SAMPLES, "min_days": MIN_DAYS,
        "train_days": TRAIN_DAYS, "checkpoints": list(CHECKPOINTS),
        "gap_edges": list(GAP_EDGES[:-1])+["inf"], "max_price": MAX_PRICE,
        "max_spread": MAX_SPREAD, "slippage_ticks": SLIPPAGE_TICKS,
        "feed_max_age": FEED_MAX_AGE, "book_max_age": BOOK_MAX_AGE, "max_tick_gap": MAX_TICK_GAP,
        "latency_seconds": LATENCY_SECONDS, "fee": "market_metadata_per_level",
        "max_daily_entries": MAX_DAILY_ENTRIES, "max_daily_loss": MAX_DAILY_LOSS,
        "test_min_closed": 200, "test_min_utc_days": 14,
        "interval": "UTC_day_block_bootstrap_2000_seed_731",
        "fill_model": "PAPER_taker_quote_after_latency_plus_slippage",
        "decision": "past_resolved_matching_cells_wilson_lower"}


def audit_trades(db, protocol_id=None):
    """Audit recorded PAPER fills and rebuild pre-entry history.

    Never turns a reference observation into a trade. Arithmetic checks are
    separate from evidence of profitability; raw fills remain simulations.
    """
    audit_at=time.time()
    errors, audited, prospective = [], [], []
    prospective_open = 0
    positions = [dict(row) for row in db.execute("SELECT * FROM positions ORDER BY entered,start")]
    account = dict(db.execute("SELECT * FROM account WHERE id=1").fetchone())
    protocol = None
    if protocol_id is None:
        candidates = []
        for key, value in db.execute("SELECT key,value FROM metadata WHERE key LIKE 'validation_%'"):
            try:
                record = json.loads(value)
                candidates.append((record["started"], key[len("validation_"):], record))
            except (TypeError, ValueError, KeyError):
                errors.append({"check": "protocol_metadata_ongeldig"})
        if candidates:
            _, protocol_id, protocol = max(candidates, key=lambda item: item[0])
    else:
        row = db.execute("SELECT value FROM metadata WHERE key=?", ("validation_"+protocol_id,)).fetchone()
        if row:
            protocol = json.loads(row[0])
    if protocol is not None and protocol.get("protocol") != validation_protocol():
        # Old cohorts require their matching code/configuration to replay decisions.
        errors.append({"check": "protocol_komt_niet_overeen_met_deze_code"})
    spent_total, proceeds_total = 0.0, 0.0
    for trade in positions:
        start = trade["start"]
        try:
            for key in ("spent", "fee", "qty", "entered"):
                if not math.isfinite(trade[key]):
                    raise ValueError()
            if not 0 < trade["spent"] or not 0 < trade["qty"] or not 0 <= trade["fee"] <= trade["spent"]:
                raise ValueError()
            spent_total += trade["spent"]
            details = json.loads(trade["details"])
            is_current = protocol_id is not None and details.get("validation_protocol") == protocol_id
            market_row = db.execute("SELECT * FROM markets WHERE start=?", (start,)).fetchone()
            if market_row is None or trade["side"] not in ("UP", "DOWN"):
                raise ValueError()
            m = json.loads(market_row["data"])
            if not start <= trade["entered"] < m["end"] or trade["entered"]>audit_at:
                errors.append({"start": start, "check": "instap_buiten_marktronde"})
            if trade["resolved_at"] is None:
                if is_current:
                    prospective_open += 1
                if trade["proceeds"] is not None or trade["pnl"] is not None:
                    errors.append({"start": start, "check": "open_positie_met_eindresultaat"})
            else:
                for key in ("proceeds", "pnl", "resolved_at"):
                    if not math.isfinite(trade[key]):
                        raise ValueError()
                proceeds_total += trade["proceeds"]
                pay = market_row["up"] if trade["side"] == "UP" else market_row["down"]
                other = market_row["down"] if trade["side"] == "UP" else market_row["up"]
                if (pay is None or other is None or not 0<=pay<=1 or not 0<=other<=1
                        or not math.isclose(pay+other,1,abs_tol=1e-8)
                        or market_row["resolved_at"] is None
                        or trade["resolved_at"] < max(trade["entered"],m["end"]) or trade["resolved_at"]>audit_at):
                    raise ValueError()
                if not math.isclose(trade["proceeds"],trade["qty"]*pay,abs_tol=1e-6):
                    errors.append({"start": start, "check": "uitbetaling_verschilt_van_aandelen_maal_opgeslagen_payout"})
                if not math.isclose(trade["pnl"],trade["proceeds"]-trade["spent"],abs_tol=1e-6):
                    errors.append({"start": start, "check": "netto_pnl_klopt_niet"})
                audited.append(trade)
                if is_current:
                    prospective.append(trade)
            if is_current:
                f, q, estimate = details["feature"], details["quote"], details["estimate"]
                if (not all(math.isfinite(f[key]) for key in ("remaining","at","reference","strike","gap_bp","spot_ts"))
                        or not all(math.isfinite(q[key]) for key in ("spread","vwap","all_in","spent","fee","qty","book_ts"))
                        or f["strike"]<=0 or f["reference"]<=0):
                    raise ValueError()
                if protocol is None or trade["entered"] < protocol["started"]:
                    errors.append({"start": start, "check": "instap_voor_vastleggen_testprotocol"})
                cp = min(CHECKPOINTS, key=lambda x: abs(x-f["remaining"]))
                observation = db.execute("SELECT * FROM observations WHERE start=? AND checkpoint=?",(start,cp)).fetchone()
                if observation is None:
                    raise ValueError()
                when = observation["at"]
                rows = db.execute("""SELECT o.start,o.at,m.resolved_at,o.result
                    FROM observations o JOIN markets m USING(start)
                    WHERE o.checkpoint=? AND o.kind=? AND o.side=? AND o.gap_bin=? AND o.price_bin=?
                    AND o.start>=? AND m.resolved_at<? AND o.result IN (0,1)""",
                    (cp,f["kind"],f["side"],f["gap_bin"],q["price_bin"],when-TRAIN_DAYS*86400,when)).fetchall()
                rebuilt = history_estimate(rows,when)
                if (not rebuilt["ready"] or estimate.get("n") != rebuilt["n"]
                        or estimate.get("wins") != rebuilt["wins"]
                        or estimate.get("days") != rebuilt["days"]
                        or not math.isclose(estimate["p"],rebuilt["p"],abs_tol=1e-10)
                        or not math.isclose(estimate["lower"],rebuilt["lower"],abs_tol=1e-10)):
                    errors.append({"start": start, "check": "kansschatting_niet_reproduceerbaar_uit_toen_bekende_uitslagen"})
                signed_bp=(f["reference"]/f["strike"]-1)*10000
                if (trade["side"] != f["side"] or f["side"] != ("UP" if signed_bp>0 else "DOWN")
                        or gap_bin(signed_bp) != f["gap_bin"]
                        or (observation["kind"],observation["side"],observation["gap_bin"],observation["price_bin"])
                        != (f["kind"],f["side"],f["gap_bin"],q["price_bin"])):
                    errors.append({"start": start, "check": "signaal_of_historische_categorie_wijkt_af"})
                if (abs(f["remaining"]-cp)>3 or not math.isclose(f["at"],trade["entered"],abs_tol=1e-6)
                        or trade["entered"]-when+1e-6 < max(LATENCY_SECONDS,m.get("seconds_delay",0))
                        or not -0.5 <= trade["entered"]-q["book_ts"] <= BOOK_MAX_AGE):
                    errors.append({"start": start, "check": "tijd_of_latencycontrole_mislukt"})
                if (rebuilt["lower"]-q["all_in"] < MIN_NET_EDGE-1e-9
                        or trade["spent"] > PAPER_STAKE+1e-6
                        or not 0<q["spread"]<=MAX_SPREAD
                        or q["vwap"]>MAX_PRICE+2e-6):
                    errors.append({"start": start, "check": "instapvoorwaarden_niet_gehaald"})
                if (not math.isclose(q["spent"],trade["spent"],abs_tol=1e-6)
                        or not math.isclose(q["qty"],trade["qty"],abs_tol=1e-6)
                        or not math.isclose(q["fee"],trade["fee"],abs_tol=1e-6)
                        or not math.isclose(q["all_in"]*q["qty"],q["spent"],abs_tol=1e-6)
                        or not math.isclose(q["vwap"]*q["qty"]+q["fee"],q["spent"],abs_tol=1e-6)):
                    errors.append({"start": start, "check": "aankoopkosten_of_aandelen_kloppen_niet"})
                boundary=datetime.fromtimestamp(trade["entered"],timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
                prior_spent=db.execute("SELECT COALESCE(SUM(spent),0) FROM positions WHERE entered<?",(trade["entered"],)).fetchone()[0]
                prior_proceeds=db.execute("SELECT COALESCE(SUM(proceeds),0) FROM positions WHERE resolved_at<=?",(trade["entered"],)).fetchone()[0]
                prior_pnl=db.execute("SELECT COALESCE(SUM(pnl),0) FROM positions WHERE resolved_at<?",(boundary,)).fetchone()[0]
                earlier_today=db.execute("SELECT COUNT(*) FROM positions WHERE entered>=? AND entered<?",(boundary,trade["entered"])).fetchone()[0]
                overlap=db.execute("SELECT 1 FROM positions WHERE entered<? AND (resolved_at IS NULL OR resolved_at>?) LIMIT 1",(trade["entered"],trade["entered"])).fetchone()
                cash_before=account["initial"]-prior_spent+prior_proceeds
                if (overlap or earlier_today>=MAX_DAILY_ENTRIES or cash_before<trade["spent"]
                        or cash_before-trade["spent"]<(account["initial"]+prior_pnl)*(1-MAX_DAILY_LOSS)-1e-6):
                    errors.append({"start": start, "check": "risicolimiet_of_overlappende_positie"})
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
            errors.append({"start": start, "check": "onvolledige_of_ongeldige_handel"})
    if not math.isclose(account["cash"],account["initial"]-spent_total+proceeds_total,abs_tol=1e-6):
        errors.append({"check": "kasboek_sluit_niet_aan"})
    prospective.sort(key=lambda x:(x["resolved_at"],x["start"]))
    n = len(prospective)
    wins = sum(t["pnl"]>0 for t in prospective)
    pnl = sum(t["pnl"] for t in prospective)
    fees = sum(t["fee"] for t in prospective)
    notional = sum(t["spent"] for t in prospective)
    equity = peak = drawdown = 0.0
    days = {}
    for trade in prospective:
        equity += trade["pnl"]
        peak = max(peak,equity)
        drawdown = max(drawdown,peak-equity)
        bucket = days.setdefault(day(trade["entered"]),[0.0,0.0])
        bucket[0] += trade["pnl"]; bucket[1] += trade["spent"]
    interval = None
    verdict = "GEEN_AFGERONDE_TRADES" if n == 0 else "VOORLOPIG_ONVOLDOENDE_HANDELSRESULTATEN"
    # Days are resampled as blocks; never treat repeated checkpoints as independent trades.
    if n>=200 and len(days)>=14:
        rng = random.Random(731)
        blocks = list(days.values())
        samples = []
        for _ in range(2000):
            draw = [rng.choice(blocks) for _ in blocks]
            samples.append(sum(x[0] for x in draw)/sum(x[1] for x in draw))
        samples.sort()
        interval = [samples[49],samples[1949]]
        verdict = ("POSITIEF_BINNEN_DEZE_PAPERPROEF" if interval[0]>0 else
                   "NEGATIEF_BINNEN_DEZE_PAPERPROEF" if interval[1]<0 else "GEEN_OVERTUIGENDE_POSITIEVE_EDGE")
    if errors:
        verdict = "CONTROLEFOUTEN_EERST_HERSTELLEN"
    return {"at":time.time(),"version":VERSION,"protocol_id":protocol_id,
        "protocol":protocol,"errors":errors,"verdict":verdict,
        "all_recorded_positions":len(positions),"all_closed_positions":len(audited),
        "prospective_closed":n,"prospective_open":prospective_open,
        "wins":wins,"win_rate":wins/n if n else None,"utc_days":len(days),
        "net_pnl":pnl,"estimated_entry_fees":fees,"turnover":notional,
        "net_return_on_turnover":pnl/notional if notional else None,
        "
