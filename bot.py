"""BTC Lead v1 — PAPER ONLY.

Public order books and authenticated, read-only PolyBolt reference prices.
No private key, live order endpoint or early sale is used.
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


# ============================================================
# CONFIGURATIE
# ============================================================

VERSION = "1.0-BTC-Lead-TWAP-PAPER"

# Nieuwe instellingen staan hier.
# Oude LIVE_TRADING- of momentum-instellingen worden niet gebruikt.
PAPER_STAKE = 5.0
INITIAL_CASH = 1000.0

# Minimaal voordeel per aandeel, na geschatte aankoopkosten.
# Vergelijking met de conservatieve ondergrens van de kansschatting.
MIN_NET_EDGE = 0.03

# Benodigde historische data per vergelijkbare categorie.
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

# Vaste meetmomenten: seconden resterend in de vijfminutenmarkt.
CHECKPOINTS = (
    300,
    285,
    240,
    180,
    120,
    90,
    60,
    45,
    30,
    15,
)

# Voorsprong in basispunten.
# 1 basispunt = 0,01%.
GAP_EDGES = (
    0,
    2,
    5,
    10,
    20,
    50,
    float("inf"),
)

SCHEMA = "btc_chainlink60_projected_v1"

DATA_DIR = Path(
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    or os.getenv("BOT_DATA_DIR", "data")
)

# Nieuwe proef, gescheiden van de oude bot.
DB_NAME = "btc_lead_v1.sqlite3"

WS_URL = "wss://ws-live-v2.polymarket.com/ws"

SPOT = "price.crypto"
TWAP = "price.crypto.twap"

LOG = logging.getLogger("btc-lead")


# ============================================================
# ALGEMENE HULPFUNCTIES
# ============================================================

class Skip(Exception):
    pass


def number(value):
    result = float(value)

    if not math.isfinite(result):
        raise Skip("niet_eindig_getal")

    return result


def day(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        timezone.utc,
    ).date().isoformat()


def unix_ms(value):
    # PolyBolt levert timestamps in milliseconden.
    result = number(value) / 1000

    if result < 1_600_000_000:
        raise Skip("timestamp_eenheid")

    return result


def gap_bin(bp):
    return max(
        0,
        min(
            len(GAP_EDGES) - 2,
            bisect.bisect_right(GAP_EDGES, abs(bp)) - 1,
        ),
    )


def wilson_lower(wins, n, z=1.96):
    """
    Conservatieve statistische ondergrens.

    Geen garantie: marktwaarnemingen kunnen gecorreleerd zijn
    en marktomstandigheden kunnen veranderen.
    """
    if n <= 0:
        return 0.0

    p = wins / n

    return (
        p
        + z * z / (2 * n)
        - z * math.sqrt(
            p * (1 - p) / n
            + z * z / (4 * n * n)
        )
    ) / (1 + z * z / n)


def history_estimate(rows, now):
    """
    Iedere rij:
    market_start, observed_time, resolved_time, leader_won.

    Alleen uitslagen die al bekend waren vóór de beslissing.
    Iedere markt telt maximaal één keer binnen deze categorie.
    """
    valid = [
        row
        for row in rows
        if now - TRAIN_DAYS * 86400 <= row[0] < now
        and row[1] < row[2] < now
        and row[3] in (0, 1)
    ]

    unique = {
        row[0]: row
        for row in valid
    }

    n = len(unique)
    wins = sum(row[3] for row in unique.values())
    days = len({day(row[0]) for row in unique.values()})

    return {
        "n": n,
        "wins": wins,
        "days": days,
        "p": wins / n if n else None,
        "lower": wilson_lower(wins, n),
        "ready": (
            n >= MIN_SAMPLES
            and days >= MIN_DAYS
        ),
    }


# ============================================================
# CHAINLINK REFERENTIEPRIJZEN
# ============================================================

class Reference:
    """
    Bewaart Chainlink spot en officiële TWAP-waarnemingen.
    Geen Binance-fallback.
    """

    def __init__(self):
        self.points = {
            SPOT: {},
            TWAP: {},
        }

        self.connected = False
        self.state = "wachten_op_feed"
        self.sequences = {}
        self.acks = set()

    def reset(self):
        # Na herverbinden eerst nieuwe snapshots gebruiken.
        self.points = {
            SPOT: {},
            TWAP: {},
        }

        self.sequences.clear()
        self.acks.clear()
        self.connected = False

    def apply(self, message, now):
        channel = message.get("channel")

        if channel not in self.points:
            return []

        if message.get("v") != 1:
            raise Skip("feed_protocol")

        sequence = message.get("seq")

        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
        ):
            raise Skip("feed_sequence_ontbreekt")

        previous = self.sequences.get(channel)

        if (
            message.get("dropped", 0)
            or (
                previous is not None
                and sequence != previous + 1
            )
        ):
            raise Skip("feed_sequence_gat")

        self.sequences[channel] = sequence

        payload = message.get("payload", {})

        if (
            payload.get("symbol") != "btcusd"
            or payload.get("source") != "chainlink"
        ):
            raise Skip("verkeerde_referentiebron")

        if (
            channel == TWAP
            and payload.get("window_seconds") != 60
        ):
            raise Skip("verkeerd_twap_venster")

        is_snapshot = message.get("snapshot") is True

        items = (
            payload.get("data", [])
            if is_snapshot
            else [payload]
        )

        if not isinstance(items, list):
            raise Skip("feed_snapshot_formaat")

        added = []

        for item in items:
            timestamp = unix_ms(item["timestamp"])

            value = number(
                item.get(
                    "full_accuracy_value",
                    item.get("value"),
                )
            )

            if (
                value <= 0
                or timestamp > now + 0.5
                or now - timestamp > 600
            ):
                raise Skip("feed_prijs_of_tijd")

            if (
                item.get("is_carried_forward")
                or payload.get("is_carried_forward")
            ):
                raise Skip("feed_doorgerolde_prijs")

            existing = self.points[channel].get(timestamp)

            if existing and not math.isclose(
                existing[0],
                value,
                rel_tol=1e-12,
            ):
                raise Skip("feed_herziening")

            self.points[channel][timestamp] = (
                value,
                now,
            )

            added.append(
                (
                    channel,
                    timestamp,
                    value,
                    now,
                )
            )

        for kind in self.points:
            self.points[kind] = {
                timestamp: value
                for timestamp, value in self.points[kind].items()
                if timestamp >= now - 900
            }

        self.connected = self.acks == {SPOT, TWAP}

        self.state = (
            "verbonden"
            if self.connected
            else "wachten_op_abonnement"
        )

        return added

    def current(self, now):
        if (
            not self.connected
            or not self.points[SPOT]
            or not self.points[TWAP]
        ):
            raise Skip("chainlink_niet_gereed")

        for kind in (SPOT, TWAP):
            timestamp = max(self.points[kind])
            _, received = self.points[kind][timestamp]

            if (
                not 0 <= now - timestamp <= FEED_MAX_AGE
                or not 0 <= now - received <= FEED_MAX_AGE
            ):
                raise Skip("chainlink_verouderd")

        timestamp = max(self.points[SPOT])

        return (
            timestamp,
            self.points[SPOT][timestamp][0],
        )

    def strike(self, start):
        # Alleen de TWAP exact op de marktstart.
        # Eén seconde later is een andere referentie.
        point = self.points[TWAP].get(float(start))

        if point is None:
            raise Skip(
                "start_twap_ontbreekt_wacht_volgende_ronde"
            )

        return point[0]

    def projected(self, end, now, spot):
        begin = end - 60

        samples = sorted(
            (timestamp, value[0])
            for timestamp, value in self.points[SPOT].items()
            if timestamp <= now
        )

        before = [
            (timestamp, value)
            for timestamp, value in samples
            if timestamp <= begin
        ]

        if (
            not before
            or begin - before[-1][0] > MAX_TICK_GAP
        ):
            raise Skip("eind_twap_historie_ontbreekt")

        last_timestamp, last_price = before[-1]
        cursor = begin
        integral = 0.0

        for timestamp, price in samples:
            if timestamp <= begin:
                continue

            if timestamp - last_timestamp > MAX_TICK_GAP:
                raise Skip("eind_twap_datagat")

            integral += last_price * (timestamp - cursor)

            cursor = timestamp
            last_timestamp = timestamp
            last_price = price

        if now - last_timestamp > FEED_MAX_AGE:
            raise Skip("eind_twap_verouderd")

        integral += last_price * (now - cursor)

        # Projectie: de nog onbekende seconden krijgen de huidige koers.
        # Dit is nadrukkelijk niet de officiële eind-TWAP.
        projected = (
            integral
            + spot * (end - now)
        ) / 60

        return (
            projected,
            integral,
            now - begin,
        )

    def feature(self, market, now):
        start = market["start"]
        end = market["end"]

        remaining = end - now

        if not 0 < remaining <= 300:
            raise Skip("buiten_ronde")

        spot_timestamp, spot = self.current(now)
        strike = self.strike(start)

        if remaining <= 60:
            reference, integral, elapsed = self.projected(
                end,
                now,
                spot,
            )

            kind = "projected_twap"

        else:
            reference = spot
            integral = None
            elapsed = 0.0
            kind = "spot_lead"

        bp = (reference / strike - 1) * 10000

        if abs(bp) < 1e-8:
            raise Skip("geen_voorsprong")

        return {
            "kind": kind,
            "side": "UP" if bp > 0 else "DOWN",
            "gap_bp": abs(bp),
            "gap_bin": gap_bin(bp),
            "strike": strike,
            "spot": spot,
            "spot_ts": spot_timestamp,
            "reference": reference,
            "integral": integral,
            "elapsed_final": elapsed,
            "remaining": remaining,
            "at": now,
        }


# ============================================================
# PAPER AANKOOPPRIJS EN KOSTEN
# ============================================================

def quote(book, market, side, budget, now):
    if (
        str(book.asset_id) != market["assets"][side]
        or str(book.condition_id) != market["condition"]
    ):
        raise Skip("verkeerd_orderboek")

    if (
        book.timestamp is None
        or not -0.5 <= (
            now - book.timestamp.timestamp()
        ) <= BOOK_MAX_AGE
    ):
        raise Skip("orderboek_verouderd")

    tick = number(book.tick_size)
    minimum = number(book.min_order_size)

    if (
        not 0 < tick < 1
        or minimum <= 0
        or budget <= 0
    ):
        raise Skip("orderboek_metadata")

    bids = [
        (number(level.price), number(level.size))
        for level in book.bids
    ]

    asks = [
        (number(level.price), number(level.size))
        for level in book.asks
    ]

    if any(
        not 0 < price < 1 or size < 0
        for price, size in bids + asks
    ):
        raise Skip("ongeldig_orderboek")

    bids = [
        (price, size)
        for price, size in bids
        if size > 0
    ]

    asks = [
        (price, size)
        for price, size in asks
        if size > 0
    ]

    if not asks or not bids:
        raise Skip("leeg_orderboek")

    spread = (
        min(price for price, size in asks)
        - max(price for price, size in bids)
    )

    if not 0 < spread <= MAX_SPREAD:
        raise Skip("spread")

    left = budget
    qty = 0.0
    fees = 0.0
    notional = 0.0

    for raw_price, size in sorted(asks):
        price = raw_price + tick * SLIPPAGE_TICKS

        if price > MAX_PRICE:
            break

        fee = market["fee_rate"] * (
            price * (1 - price)
        ) ** market["fee_exponent"]

        take = min(
            size,
            left / (price + fee),
        )

        qty += take
        fees += take * fee
        notional += take * price

        left -= take * (price + fee)

        if left < 1e-9:
            break

    if left > 1e-6:
        raise Skip(
            "onvoldoende_dieptes_onder_prijsplafond"
        )

    # Conservatief: aandelen naar beneden afronden.
    qty = math.floor(qty * 1e6) / 1e6

    if qty < minimum:
        raise Skip("minimum_aandelen")

    return {
        "qty": qty,
        "spent": notional + fees,
        "fee": fees,
        "vwap": notional / qty,
        "all_in": (notional + fees) / qty,
        "spread": spread,
        "price_bin": min(
            9,
            int((notional / qty) * 10),
        ),
        "book_ts": book.timestamp.timestamp(),
    }


# ============================================================
# MARKT VINDEN EN OFFICIËLE UITSLAG
# ============================================================

async def discover(client, start):
    slug = f"btc-updown-5m-{start}"

    market = await asyncio.wait_for(
        client.get_market(slug=slug),
        8,
    )

    state = market.state

    if (
        market.slug != slug
        or not market.condition_id
        or state.end_date is None
        or abs(
            state.end_date.timestamp() - start - 300
        ) > 0.1
    ):
        raise Skip("verkeerde_markt_of_eindtijd")

    if (
        state.closed
        or not state.active
        or not state.accepting_orders
        or not state.enable_order_book
    ):
        raise Skip("markt_niet_open")

    description = (market.description or "").lower()

    if (
        "chainlink" not in description
        or not any(
            word in description
            for word in (
                "twap",
                "time-weighted",
                "time weighted",
            )
        )
    ):
        raise Skip("twap_marktregels_niet_bevestigd")

    source = str(market.resolution.source or "")

    if (
        not source.startswith("https://data.chain.link/")
        or "btc-usd-twap-60s" not in source
    ):
        raise Skip("60s_btc_resolutiebron_niet_bevestigd")

    schedule = market.trading.fee_schedule

    if (
        market.trading.fees_enabled is not False
        and schedule is None
    ):
        raise Skip("fee_onbekend")

    rate = (
        number(schedule.rate)
        if schedule
        and market.trading.fees_enabled is not False
        else 0.0
    )

    exponent = (
        number(schedule.exponent)
        if schedule
        else 1.0
    )

    if (
        not 0 <= rate <= 1
        or not 0 <= exponent <= 10
    ):
        raise Skip("fee_schema_ongeldig")

    version = getattr(
        market.version,
        "value",
        market.version,
    )

    if version not in ("v1", "v2"):
        raise Skip("marktversie_onbekend")

    assets = {}
    indices = {}

    for index, outcome in enumerate(
        (
            market.outcomes.yes,
            market.outcomes.no,
        )
    ):
        label = outcome.label.strip().upper()

        asset = (
            outcome.position_id
            if version == "v2"
            else outcome.token_id
        )

        if label not in ("UP", "DOWN") or not asset:
            raise Skip("uitkomst_mapping")

        assets[label] = str(asset)
        indices[label] = index

    if (
        len(assets) != 2
        or len(set(assets.values())) != 2
    ):
        raise Skip("uitkomst_mapping")

    return {
        "start": start,
        "end": start + 300,
        "slug": slug,
        "condition": str(market.condition_id),
        "assets": assets,
        "indices": indices,
        "fee_rate": rate,
        "fee_exponent": exponent,
        "seconds_delay": max(
            0,
            number(market.trading.seconds_delay or 0),
        ),
        "rules_hash": hashlib.sha256(
            (description + source).encode()
        ).hexdigest(),
        "description": description,
        "resolution_source": source,
    }


def payout(resolutions, market):
    matches = [
        resolution
        for resolution in resolutions
        if str(resolution.condition_id)
        == market["condition"]
        and getattr(
            resolution.status,
            "value",
            resolution.status,
        ) == "resolved"
    ]

    if (
        len(matches) != 1
        or matches[0].payouts is None
    ):
        return None

    values = [
        number(value)
        for value in matches[0].payouts
    ]

    if (
        len(values) != 2
        or any(
            value < 0 or value > 1
            for value in values
        )
        or abs(sum(values) - 1) > 1e-8
    ):
        raise Skip("ongeldige_officiele_uitbetaling")

    return {
        side: values[index]
        for side, index in market["indices"].items()
    }


# ============================================================
# DATABASE EN PAPERBOEKHOUDING
# ============================================================

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row

        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")

        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS account(
                id INTEGER PRIMARY KEY CHECK(id=1),
                schema TEXT,
                initial REAL,
                cash REAL,
                day TEXT,
                day_start REAL,
                entries INTEGER
            );

            CREATE TABLE IF NOT EXISTS markets(
                start INTEGER PRIMARY KEY,
                data TEXT NOT NULL,
                resolved_at REAL,
                up REAL,
                down REAL,
                last_poll REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS observations(
                start INTEGER,
                checkpoint INTEGER,
                at REAL,
                kind TEXT,
                side TEXT,
                gap_bin INTEGER,
                price_bin INTEGER,
                details TEXT,
                result INTEGER,
                PRIMARY KEY(start, checkpoint)
            );

            CREATE INDEX IF NOT EXISTS obs_cell
            ON observations(
                checkpoint,
                kind,
                side,
                gap_bin,
                price_bin
            );

            CREATE TABLE IF NOT EXISTS positions(
                start INTEGER PRIMARY KEY,
                side TEXT,
                qty REAL,
                spent REAL,
                fee REAL,
                entered REAL,
                details TEXT,
                resolved_at REAL,
                proceeds REAL,
                pnl REAL
            );

            CREATE TABLE IF NOT EXISTS ticks(
                channel TEXT,
                ts REAL,
                value REAL,
                received REAL,
                PRIMARY KEY(channel, ts)
            );

            CREATE TABLE IF NOT EXISTS skips(
                reason TEXT PRIMARY KEY,
                count INTEGER
            );
            """
        )

        with self.db:
            self.db.execute(
                """
                INSERT OR IGNORE INTO account
                VALUES(1, ?, ?, ?, ?, ?, 0)
                """,
                (
                    SCHEMA,
                    INITIAL_CASH,
                    INITIAL_CASH,
                    day(time.time()),
                    INITIAL_CASH,
                ),
            )

        if self.account()["schema"] != SCHEMA:
            raise RuntimeError(
                "Database hoort bij andere strategie; "
                "niet overschrijven"
            )

        self.verify()

    def account(self):
        row = self.db.execute(
            "SELECT * FROM account WHERE id=1"
        ).fetchone()

        return dict(row)

    def verify(self):
        account = self.account()

        used = self.db.execute(
            """
            SELECT
                COALESCE(SUM(spent), 0),
                COALESCE(SUM(proceeds), 0)
            FROM positions
            """
        ).fetchone()

        expected_cash = (
            account["initial"]
            - used[0]
            + used[1]
        )

        if not math.isclose(
            account["cash"],
            expected_cash,
            abs_tol=1e-6,
        ):
            raise RuntimeError(
                "Paperboekhouding sluit niet aan; gestopt"
            )

    def roll_day(self, now):
        account = self.account()

        if account["day"] != day(now):
            reserved = self.db.execute(
                """
                SELECT COALESCE(SUM(spent), 0)
                FROM positions
                WHERE resolved_at IS NULL
                """
            ).fetchone()[0]

            with self.db:
                self.db.execute(
                    """
                    UPDATE account
                    SET day=?, day_start=?, entries=0
                    WHERE id=1
                    """,
                    (
                        day(now),
                        account["cash"] + reserved,
                    ),
                )

    def skip(self, reason):
        with self.db:
            self.db.execute(
                """
                INSERT INTO skips VALUES(?, 1)
                ON CONFLICT(reason)
                DO UPDATE SET count=count+1
                """,
                (reason,),
            )

    def market(self, market):
        with self.db:
            self.db.execute(
                """
                INSERT OR IGNORE INTO markets(start, data)
                VALUES(?, ?)
                """,
                (
                    market["start"],
                    json.dumps(market),
                ),
            )

    def ticks(self, rows):
        with self.db:
            self.db.executemany(
                """
                INSERT OR IGNORE INTO ticks
                VALUES(?, ?, ?, ?)
                """,
                rows,
            )

    def estimate(self, checkpoint, feature, quote_data, now):
        rows = self.db.execute(
            """
            SELECT
                o.start,
                o.at,
                m.resolved_at,
                o.result
            FROM observations o
            JOIN markets m USING(start)
            WHERE checkpoint=?
              AND kind=?
              AND side=?
              AND gap_bin=?
              AND price_bin=?
              AND o.start>=?
              AND m.resolved_at<?
              AND o.result IN (0, 1)
            """,
            (
                checkpoint,
                feature["kind"],
                feature["side"],
                feature["gap_bin"],
                quote_data["price_bin"],
                now - TRAIN_DAYS * 86400,
                now,
            ),
        ).fetchall()

        return history_estimate(rows, now)

    def observed(self, start, checkpoint):
        row = self.db.execute(
            """
            SELECT 1
            FROM observations
            WHERE start=? AND checkpoint=?
            """,
            (
                start,
                checkpoint,
            ),
        ).fetchone()

        return row is not None

    def observe(
        self,
        market,
        checkpoint,
        feature,
        quote_data,
        estimate,
    ):
        details = {
            "feature": feature,
            "quote": quote_data,
            "estimate_before_result": estimate,
        }

        with self.db:
            self.db.execute(
                """
                INSERT OR IGNORE INTO observations
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    market["start"],
                    checkpoint,
                    feature["at"],
                    feature["kind"],
                    feature["side"],
                    feature["gap_bin"],
                    quote_data["price_bin"],
                    json.dumps(details),
                ),
            )

    def can_enter(self, start, spent):
        already_traded = self.db.execute(
            """
            SELECT 1
            FROM positions
            WHERE start=?
            """,
            (start,),
        ).fetchone()

        if already_traded:
            raise Skip("al_gehandeld_deze_ronde")

        open_position = self.db.execute(
            """
            SELECT 1
            FROM positions
            WHERE resolved_at IS NULL
            """
        ).fetchone()

        if open_position:
            raise Skip("positie_of_afrekening_nog_open")

        account = self.account()

        if account["entries"] >= MAX_DAILY_ENTRIES:
            raise Skip("daglimiet_aantal")

        if (
            spent > PAPER_STAKE + 1e-6
            or spent <= 0
            or account["cash"] < spent
        ):
            raise Skip("onvoldoende_paperbudget")

        minimum_cash = (
            account["day_start"]
            * (1 - MAX_DAILY_LOSS)
        )

        if account["cash"] - spent < minimum_cash - 1e-6:
            raise Skip("daglimiet_maximaal_verlies")

    def enter(
        self,
        market,
        feature,
        quote_data,
        estimate,
        now,
    ):
        self.can_enter(
            market["start"],
            quote_data["spent"],
        )

        details = json.dumps(
            {
                "feature": feature,
                "quote": quote_data,
                "estimate": estimate,
            }
        )

        with self.db:
            self.db.execute(
                """
                INSERT INTO positions
                VALUES(
                    ?, ?, ?, ?, ?, ?, ?,
                    NULL, NULL, NULL
                )
                """,
                (
                    market["start"],
                    feature["side"],
                    quote_data["qty"],
                    quote_data["spent"],
                    quote_data["fee"],
                    now,
                    details,
                ),
            )

            self.db.execute(
                """
                UPDATE account
                SET cash=cash-?, entries=entries+1
                WHERE id=1
                """,
                (quote_data["spent"],),
            )

        self.verify()

    def settle(self, market, pay, now):
        row = self.db.execute(
            """
            SELECT resolved_at
            FROM markets
            WHERE start=?
            """,
            (market["start"],),
        ).fetchone()

        if row is None or row[0] is not None:
            return None

        trade = self.db.execute(
            """
            SELECT *
            FROM positions
            WHERE start=?
              AND resolved_at IS NULL
            """,
            (market["start"],),
        ).fetchone()

        result = None

        with self.db:
            self.db.execute(
                """
                UPDATE markets
                SET resolved_at=?, up=?, down=?
                WHERE start=?
                """,
                (
                    now,
                    pay["UP"],
                    pay["DOWN"],
                    market["start"],
                ),
            )

            # Alleen binaire uitslagen gebruiken als trainingslabels.
            if sorted(pay.values()) == [0.0, 1.0]:
                for side in ("UP", "DOWN"):
                    self.db.execute(
                        """
                        UPDATE observations
                        SET result=?
                        WHERE start=? AND side=?
                        """,
                        (
                            int(pay[side]),
                            market["start"],
                            side,
                        ),
                    )

            if trade:
                proceeds = (
                    trade["qty"]
                    * pay[trade["side"]]
                )

                pnl = proceeds - trade["spent"]

                self.db.execute(
                    """
                    UPDATE positions
                    SET resolved_at=?, proceeds=?, pnl=?
                    WHERE start=?
                    """,
                    (
                        now,
                        proceeds,
                        pnl,
                        market["start"],
                    ),
                )

                self.db.execute(
                    """
                    UPDATE account
                    SET cash=cash+?
                    WHERE id=1
                    """,
                    (proceeds,),
                )

                result = {
                    "side": trade["side"],
                    "pnl": pnl,
                    "proceeds": proceeds,
                }

        self.verify()

        return result


# ============================================================
# BOT
# ============================================================

class Bot:
    def __init__(self, store):
        self.store = store
        self.ref = Reference()

        self.market = None

        self.last_report = 0
        self.last_discovery = 0
        self.last_resolve = 0
        self.last_cleanup = 0

        self.last_cell = None

        self.messages = asyncio.Queue(maxsize=50)

        self.health = {
            "version": VERSION,
            "mode": "PAPER",
            "status": "starting",
        }

        self.started = time.time()
        self.attempted = set()

    def notify(self, message):
        LOG.info(message)

        with suppress(asyncio.QueueFull):
            self.messages.put_nowait(message)

    async def telegram(self):
        token = os.getenv(
            "TELEGRAM_BOT_TOKEN",
            "",
        ).strip()

        if token.startswith("bot"):
            token = token[3:]

        chat = os.getenv(
            "TELEGRAM_CHAT_ID",
            "",
        ).strip()

        while True:
            message = await self.messages.get()

            if token and chat:
                def send():
                    # Geen URLs of exceptions met tokens loggen.
                    try:
                        response = requests.post(
                            (
                                "https://api.telegram.org/"
                                f"bot{token}/sendMessage"
                            ),
                            json={
                                "chat_id": chat,
                                "text": message[:4000],
                                "disable_web_page_preview": True,
                            },
                            timeout=8,
                        )

                        return (
                            response.status_code == 200
                            and response.json().get("ok") is True
                        )

                    except Exception:
                        return False

                delivered = await asyncio.to_thread(send)

                if not delivered:
                    LOG.warning(
                        "Telegram niet bezorgd; bot draait verder"
                    )

            self.messages.task_done()

    async def reference_feed(self):
        auth = {
            "apiKey": os.getenv(
                "POLYMARKET_API_KEY",
                "",
            ).strip(),

            "secret": os.getenv(
                "POLYMARKET_API_SECRET",
                "",
            ).strip(),

            "passphrase": os.getenv(
                "POLYMARKET_API_PASSPHRASE",
                "",
            ).strip(),
        }

        if not all(auth.values()):
            self.ref.state = "API_credentials_ontbreken"

            self.notify(
                "BTC Lead: referentiefeed wacht op "
                "POLYMARKET_API_KEY, POLYMARKET_API_SECRET "
                "en POLYMARKET_API_PASSPHRASE. "
                "Geen private key nodig; uitsluitend paper."
            )

            return

        delay = 1

        while True:
            self.ref.reset()

            try:
                async with connect(
                    WS_URL,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=1024,
                    max_size=262144,
                ) as websocket:

                    await websocket.send(
                        json.dumps(
                            {
                                "op": "auth",
                                "rid": "a1",
                                "auth": auth,
                            }
                        )
                    )

                    reply = json.loads(
                        await asyncio.wait_for(
                            websocket.recv(),
                            10,
                        )
                    )

                    if reply.get("op") != "authed":
                        raise Skip("feed_auth_mislukt")

                    await websocket.send(
                        json.dumps(
                            {
                                "op": "subscribe",
                                "rid": "s1",
                                "subscriptions": [
                                    {
                                        "channel": SPOT,
                                        "filter": {
                                            "symbol": "btcusd",
                                            "provider": "chainlink",
                                        },
                                    },
                                    {
                                        "channel": TWAP,
                                        "filter": {
                                            "symbol": "btcusd",
                                            "window_seconds": 60,
                                        },
                                    },
                                ],
                            }
                        )
                    )

                    delay = 1

                    while True:
                        message = json.loads(
                            await asyncio.wait_for(
                                websocket.recv(),
                                15,
                            )
                        )

                        operation = message.get("op")

                        if operation == "error":
                            raise Skip(
                                "feed_abonnement_geweigerd"
                            )

                        if (
                            operation == "subscribed"
                            and message.get("channel")
                            in (SPOT, TWAP)
                        ):
                            provider = message.get("provider")

                            if (
                                provider is not None
                                and provider != "chainlink"
                            ):
                                raise Skip(
                                    "feed_verkeerde_provider"
                                )

                            self.ref.acks.add(
                                message["channel"]
                            )

                            continue

                        if operation is not None:
                            continue

                        rows = self.ref.apply(
                            message,
                            time.time(),
                        )

                        self.store.ticks(rows)

            except asyncio.CancelledError:
                raise

            except sqlite3.Error:
                raise

            except Exception as exc:
                self.ref.connected = False

                self.ref.state = (
                    str(exc)
                    if isinstance(exc, Skip)
                    else type(exc).__name__
                )

                LOG.warning(
                    "Referentiefeed onderbroken: %s",
                    self.ref.state,
                )

                await asyncio.sleep(
                    min(30, delay) + random.random()
                )

                delay = min(30, delay * 2)

    async def resolutions(self, client, now):
        # Roterende batch: één probleemmarkt blokkeert niet alles.
        rows = self.store.db.execute(
            """
            SELECT start, data
            FROM markets
            WHERE resolved_at IS NULL
              AND start+300<=?
            ORDER BY last_poll
            LIMIT 8
            """,
            (now,),
        ).fetchall()

        for row in rows:
            market = json.loads(row["data"])

            with self.store.db:
                self.store.db.execute(
                    """
                    UPDATE markets
                    SET last_poll=?
                    WHERE start=?
                    """,
                    (
                        time.time(),
                        row["start"],
                    ),
                )

            try:
                resolutions = await asyncio.wait_for(
                    client.get_resolutions(
                        condition_ids=market["condition"],
                    ),
                    5,
                )

                pay = payout(
                    resolutions,
                    market,
                )

                if pay is None:
                    continue

                result = self.store.settle(
                    market,
                    pay,
                    time.time(),
                )

                if result:
                    self.notify(
                        "PAPER officieel afgerond "
                        f"{result['side']} | "
                        f"netto {result['pnl']:+.2f} USD\n"
                        f"{market['slug']}"
                    )

            except Skip as exc:
                self.store.skip(str(exc))

            except (asyncio.TimeoutError, OSError):
                self.store.skip(
                    "afrekening_tijdelijk_onbereikbaar"
                )

            except sqlite3.Error:
                raise

            except Exception as exc:
                LOG.warning(
                    "Afrekening API: %s",
                    type(exc).__name__,
                )

    async def settlement_worker(self, client):
        while True:
            self.store.roll_day(time.time())

            await self.resolutions(
                client,
                time.time(),
            )

            await asyncio.sleep(10)

    async def measure(self, client, checkpoint):
        market = self.market
        start = market["start"]

        self.attempted.add(
            (start, checkpoint)
        )

        # Vaste meetmomenten, geen herhaalde pogingen
        # totdat toevallig een gunstige quote verschijnt.
        feature = self.ref.feature(
            market,
            time.time(),
        )

        book = await asyncio.wait_for(
            client.get_order_book(
                asset_id=market["assets"][feature["side"]],
            ),
            3,
        )

        now = time.time()

        feature = self.ref.feature(
            market,
            now,
        )

        if abs(feature["remaining"] - checkpoint) > 2:
            raise Skip("meetmoment_gemist")

        quote_data = quote(
            book,
            market,
            feature["side"],
            PAPER_STAKE,
            now,
        )

        estimate = self.store.estimate(
            checkpoint,
            feature,
            quote_data,
            now,
        )

        self.last_cell = {
            **estimate,
            "gap_bp": feature["gap_bp"],
            "remaining": round(feature["remaining"]),
            "side": feature["side"],
            "all_in": quote_data["all_in"],
        }

        if self.store.observed(start, checkpoint):
            return

        self.store.observe(
            market,
            checkpoint,
            feature,
            quote_data,
            estimate,
        )

        if not estimate["ready"]:
            raise Skip(
                "data_verzamelen_onvoldoende_vergelijkbare_rondes"
            )

        if (
            estimate["lower"] - quote_data["all_in"]
            < MIN_NET_EDGE
        ):
            raise Skip("onvoldoende_netto_edge")

        self.store.can_enter(
            start,
            quote_data["spent"],
        )

        await asyncio.sleep(
            max(
                LATENCY_SECONDS,
                market.get("seconds_delay", 0),
            )
        )

        book_after = await asyncio.wait_for(
            client.get_order_book(
                asset_id=market["assets"][feature["side"]],
            ),
            3,
        )

        now_after = time.time()

        feature_after = self.ref.feature(
            market,
            now_after,
        )

        if (
            abs(feature_after["remaining"] - checkpoint)
            > 3
        ):
            raise Skip("instap_te_laat")

        if any(
            feature_after[key] != feature[key]
            for key in (
                "side",
                "kind",
                "gap_bin",
            )
        ):
            raise Skip("voorsprong_veranderd")

        quote_after = quote(
            book_after,
            market,
            feature_after["side"],
            PAPER_STAKE,
            now_after,
        )

        if (
            quote_after["price_bin"]
            != quote_data["price_bin"]
        ):
            raise Skip("prijsklasse_veranderd")

        if (
            estimate["lower"] - quote_after["all_in"]
            < MIN_NET_EDGE
        ):
            raise Skip("edge_verdwenen_na_latency")

        self.store.enter(
            market,
            feature_after,
            quote_after,
            estimate,
            now_after,
        )

        self.notify(
            f"PAPER BTC {feature_after['side']} | "
            f"all-in {quote_after['spent']:.2f} USD\n"
            f"Lead {feature_after['gap_bp']:.2f} bp | "
            f"resterend {feature_after['remaining']:.0f}s | "
            f"{feature_after['kind']}\n"
            f"Historisch {estimate['p']:.1%}, "
            f"ondergrens {estimate['lower']:.1%}; "
            f"n={estimate['n']}\n"
            f"Kost/aandeel {quote_after['all_in']:.4f}, "
            f"geschatte fee {quote_after['fee']:.4f}\n"
            "Vasthouden tot officiële uitslag. "
            f"https://polymarket.com/event/{market['slug']}"
        )

    def report(self):
        account = self.store.account()
        db = self.store.db

        closed, wins, pnl = db.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(pnl>0), 0),
                COALESCE(SUM(pnl), 0)
            FROM positions
            WHERE resolved_at IS NOT NULL
            """
        ).fetchone()

        opened, reserved = db.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(spent), 0)
            FROM positions
            WHERE resolved_at IS NULL
            """
        ).fetchone()

        observed = db.execute(
            "SELECT COUNT(*) FROM observations"
        ).fetchone()[0]

        resolved = db.execute(
            """
            SELECT COUNT(*)
            FROM markets
            WHERE resolved_at IS NOT NULL
            """
        ).fetchone()[0]

        skips = dict(
            db.execute(
                """
                SELECT reason, count
                FROM skips
                ORDER BY count DESC
                LIMIT 6
                """
            ).fetchall()
        )

        report_data = {
            "version": VERSION,
            "at": time.time(),
            "account": account,
            "closed": closed,
            "wins": wins,
            "net_pnl": pnl,
            "open": opened,
            "reserved": reserved,
            "observations": observed,
            "resolved_markets": resolved,
            "last_cell": self.last_cell,
            "skips": skips,
            "trades": [
                dict(row)
                for row in db.execute(
                    """
                    SELECT *
                    FROM positions
                    ORDER BY start DESC
                    LIMIT 1000
                    """
                )
            ],
        }

        for trade in report_data["trades"]:
            trade["details"] = json.loads(
                trade["details"]
            )

        # Deelbaar rapport zonder API-credentials of Telegram-ID.
        path = DATA_DIR / "btc_lead_report.json"
        temporary = path.with_suffix(".tmp")

        with temporary.open("w") as output:
            json.dump(
                report_data,
                output,
                indent=2,
                allow_nan=False,
            )

            output.flush()
            os.fsync(output.fileno())

        os.replace(
            temporary,
            path,
        )

        self.notify(
            f"{VERSION}\n"
            f"Feed: {self.ref.state}\n"
            f"Paper vrij {account['cash']:.2f} | "
            f"gereserveerd {reserved:.2f} | "
            f"open {opened}\n"
            f"Afgerond {closed}, winst {wins}, "
            f"netto {pnl:+.2f} USD\n"
            f"Dataset: {observed} meetpunten, "
            f"{resolved} afgeronde rondes\n"
            "Laatste vergelijkbare cel: "
            f"{json.dumps(self.last_cell, ensure_ascii=False)}\n"
            "Controles: "
            f"{json.dumps(skips, ensure_ascii=False)}\n"
            "Geen kansen ingebouwd uit onbevestigde percentages. "
            "Eerst data verzamelen; daarna alleen edge na kosten."
        )

    async def cycle(self, client):
        now = time.time()
        self.store.roll_day(now)

        now = time.time()
        start = int(now // 300) * 300

        needs_market = (
            self.market is None
            or self.market["start"] != start
        )

        if (
            needs_market
            and now - self.last_discovery >= 10
        ):
            self.last_discovery = now
            self.market = None

            self.market = await discover(
                client,
                start,
            )

            self.store.market(self.market)

            self.attempted = {
                item
                for item in self.attempted
                if item[0] == start
            }

        if (
            self.market
            and self.market["start"] == start
        ):
            remaining = (
                self.market["end"]
                - time.time()
            )

            for checkpoint in CHECKPOINTS:
                if (
                    0 <= checkpoint - remaining <= 1.5
                    and (start, checkpoint)
                    not in self.attempted
                ):
                    await self.measure(
                        client,
                        checkpoint,
                    )

                    break

    async def run(self):
        self.notify(
            f"{VERSION} gestart. Nieuwe paperproef: "
            f"BTC, {PAPER_STAKE:.2f} USD all-in, "
            "maximaal één positie.\n"
            "Historische cellen worden opgebouwd uit officiële "
            "resultaten; zonder voldoende data geen orders.\n"
            "Bewaar btc_lead_v1.sqlite3 op een Railway-volume. "
            "Oude bot stoppen om dubbele processen te voorkomen."
        )

        async with AsyncPublicClient() as client:
            feed = asyncio.create_task(
                self.reference_feed()
            )

            telegram = asyncio.create_task(
                self.telegram()
            )

            settlement = asyncio.create_task(
                self.settlement_worker(client)
            )

            try:
                while True:
                    if (
                        feed.done()
                        and feed.exception() is not None
                    ):
                        raise feed.exception()

                    if settlement.done():
                        raise (
                            settlement.exception()
                            or RuntimeError(
                                "Afrekeningstaak gestopt"
                            )
                        )

                    try:
                        await self.cycle(client)

                    except Skip as exc:
                        self.store.skip(str(exc))

                    except (sqlite3.Error, RuntimeError):
                        raise

                    except Exception as exc:
                        self.store.skip(
                            "api_fout_"
                            + type(exc).__name__
                        )

                        LOG.warning(
                            "Cyclus overgeslagen: %s",
                            type(exc).__name__,
                        )

                    now = time.time()

                    self.health.update(
                        status="running",
                        feed=self.ref.state,
                        last_cycle=now,
                    )

                    if now - self.last_report >= 900:
                        self.last_report = now
                        self.report()

                    if now - self.last_cleanup >= 3600:
                        self.last_cleanup = now

                        with self.store.db:
                            self.store.db.execute(
                                """
                                DELETE FROM ticks
                                WHERE ts<?
                                """,
                                (now - 7 * 86400,),
                            )

                    await asyncio.sleep(0.25)

            finally:
                feed.cancel()
                telegram.cancel()
                settlement.cancel()

                await asyncio.gather(
                    feed,
                    telegram,
                    settlement,
                    return_exceptions=True,
                )


# ============================================================
# RAILWAY HEALTH-ENDPOINT
# ============================================================

def health_server(bot):
    if not os.getenv("PORT"):
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in (
                "/",
                "/health",
            ):
                self.send_error(404)
                return

            payload = json.dumps(
                bot.health
            ).encode()

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json",
            )

            self.send_header(
                "Content-Length",
                str(len(payload)),
            )

            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            int(os.environ["PORT"]),
        ),
        Handler,
    )

    threading.Thread(
        target=server.serve_forever,
        daemon=True,
    ).start()


# ============================================================
# START
# ============================================================

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not (
        0 < PAPER_STAKE <= 5
        and 0 < MIN_NET_EDGE < 1
        and MIN_SAMPLES >= 100
        and MIN_DAYS >= 3
    ):
        raise RuntimeError(
            "Ongeldige paperconfiguratie"
        )

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Blokkeer dubbele processen op dezelfde database.
    # Railway moet daarnaast op één replica blijven staan.
    lock_path = DATA_DIR / "btc_lead_v1.lock"

    with lock_path.open("w") as lock:
        fcntl.flock(
            lock,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )

        store = Store(
            DATA_DIR / DB_NAME
        )

        bot = Bot(store)
        health_server(bot)

        try:
            asyncio.run(
                bot.run()
            )

        finally:
            store.db.close()


if __name__ == "__main__":
    main()
