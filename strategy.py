#!/usr/bin/env python3
"""LIQUIDITY GUARD v1 — uitsluitend PAPERTRADING, Python 3.10+.

STARTEN
  python liquidity_bot.py --self-test    # offline controles; geen berichten/orders
  python liquidity_bot.py --once        # een publieke datascan; geen Telegram
  python liquidity_bot.py               # continu; stop met Ctrl+C

Geen pip-pakketten nodig. Op Render: Python-service, start command
`python liquidity_bot.py`. PORT activeert een HTTP-healthserver. Gebruik een
persistent disk en STATE_FILE=/var/data/liquidity.sqlite3 om simulaties bij
herstart/redeploy te behouden. Zonder persistent disk kan de historie verdwijnen.

Optionele environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   meldingen naar jouw chat
  INITIAL_CAPITAL=1000                  USDT, alleen bij een nieuwe database
  STATE_FILE=liquidity.sqlite3          database met saldo, posities en trades
  PORT                                 alleen voor hosting-healthcheck
Omgevingsvariabelen worden door je host/shell ingesteld; .env wordt niet gelezen.
Geen exchange-account/API-key nodig. PAPER_TRADING=false wordt GEWEIGERD.

ONTWERP EN AFWIJKINGEN VAN DE AFBEELDING
De afbeelding is een onderzoeksopdracht, geen bewezen tradingstrategie. Dit is
een concrete, experimentele vertaling voor BTCUSDT, ETHUSDT en SOLUSDT op Binance
spot. Alleen long; geen futures, leverage, funding, short-orders of echte orders.
De publieke REST-documentatie is gecontroleerd op 20 oktober 2026:
https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md

Elke circa 10 seconden: spread, zichtbare diepte binnen +/-10 basispunten van mid,
orderboek-onbalans en agressieve koopomzet. De transactiemeting gebruikt maximaal
de laatste 1000 aggTrades binnen 30 seconden; dit is GEEN gegarandeerd volledig
30-secondenvenster. Alleen data met een laatste transactie <=15 seconden oud.
Orderboek heeft geen exchange-tijdstempel: RTT <=3 s en een oplopende update-ID
zijn controles, geen garantie tegen vertraagde data. Maximaal 500 niveaus per
zijde; bij onvoldoende bereik over de gekozen band worden entries geblokkeerd.

Regimes: OPWARMEN, STABIEL, FRAGIEL, ONDUIDELIJK. Baseline = medianen van maximaal
1 uur recente, geldige eigen snapshots; NIET de 20 dagen uit de afbeelding.
Na elke start minimaal 5 minuten en 30 samples opwarmen. Een gat >30 s reset dit.
FRAGIEL bij spread >8 bps of >2x baseline, diepte per kant <50.000 USDT of
<55% baseline. Deze drempels zijn aannames, geen geoptimaliseerde parameters.

ENTRY: stabiel regime, >=20 miljoen USDT 24h-omzet, drie opeenvolgende snapshots
met boek-onbalans >=0,15 en agressieve koopomzet >=58%; gesloten 5m-candle boven
EMA20 boven EMA50, EMA20 stijgt, groene candle sluit boven vorige close, omzet
minstens mediaan vorige 20 candles. Niet meer dan 0,5 ATR boven die candle kopen.
Bevestigingen gebruiken overlappende trades; ze zijn niet statistisch onafhankelijk.

EXIT: waargenomen best bid raakt SL of TP, 3 fragiele snapshots of max 4 uur.
Stopafstand = max(1,5 ATR14, 0,5% van instap). TP = 1,8x bruto stopafstand.
Risicobudget 0,5% geschat vermogen/trade, inclusief geraamde fees en stop-slippage;
max 25% vermogen als inleg per positie, max 2 posities en 6 entries/UTC-dag.
Na >=3% dagelijkse equity-daling of 3 verliestrades achtereen geen nieuwe entries
tot volgende UTC-dag; bestaande exits blijven werken. Dit is GEEN gegarandeerde
verlieslimiet en forceert geen sluiting. Bij ontbrekende portefeuilleprijzen geen
entries/dagreset. Dagbasis is eerste volledige waardering van de UTC-dag.

SIMULATIE
Instap loopt door asks, uitstap door bids; 0,10% fee per kant en 0,02% extra
ongunstige slippage per kant. Stop/TP zijn polling-triggers, geen exchange-orders.
Gaps worden tegen het waargenomen boek verwerkt, NIET tegen de oude stopprijs.
Kortstondige stop/TP-aanrakingen tussen polls of tijdens downtime worden gemist;
er is geen retrospectieve uitvoering. Bij onvoldoende boekdiepte blijft een exit
open en volgt een foutmelding. Vermogen gebruikt laatste beschikbare exitwaarde
(kan bij storing verouderd zijn); nieuwe entries vereisen verse positieschattingen.
Geen queue-, marktimpact-, latency- of exchange-filtermodel (lot/tick/min-notional);
bedragen zijn fractionele paper-eenheden. Resultaten voorspellen geen live rendement.
Geen HFT, backtest, VPIN, spoofingdetectie, dealer-gamma, ETF-flows, options/OTC,
annuleringsratio's, 20-daagse marktvergelijking of winstgarantie.

Telegram: startup, paper-entry/exit, storingen en status elke 30 minuten; geen
remote besturing. Berichten worden asynchroon verzonden; verzending kan mislukken.
De SQLite events-tabel is het blijvende logboek. Een tweede proces op dezelfde
database wordt geblokkeerd. Bewaar de database; verwijdering reset de simulatie.
"""

import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import queue
import signal
import sqlite3
import statistics
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
API = "https://data-api.binance.vision"
POLL_SECONDS = 10
FEE = 0.001
SLIPPAGE = 0.0002
RISK = 0.005
MAX_ALLOCATION = 0.25
MAX_DAILY_LOSS = 0.03
MAX_POSITIONS = 2
MIN_DEPTH = 50_000.0
MIN_VOLUME = 20_000_000.0
LOG = logging.getLogger("liquidity")
STOP = threading.Event()
HEALTH = {"mode": "PAPER ONLY", "last_success": None}


class DataError(Exception):
    pass


def positive(value):
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise DataError("Ongeldige numerieke marktwaarde")
    return n


class Market:
    def __init__(self):
        self.pause_until = 0.0
        self.cache = {}
        self.offset = 0.0
        self.synced = 0.0

    def get(self, path, **params):
        if time.monotonic() < self.pause_until:
            raise DataError("API-pauze wegens limiet/blokkade")
        started = time.monotonic()
        try:
            req = Request(API + path + "?" + urlencode(params),
                          headers={"User-Agent": "LiquidityGuard-Paper/1.0"})
            with urlopen(req, timeout=5) as response:
                result = json.load(response)
        except HTTPError as exc:
            if exc.code in (418, 429):
                try:
                    delay = max(60, float(exc.headers.get("Retry-After", "120")))
                except ValueError:
                    delay = 120
                self.pause_until = max(self.pause_until, time.monotonic() + delay)
            elif exc.code in (403, 451):
                self.pause_until = max(self.pause_until, time.monotonic() + 900)
            raise DataError(f"Publieke API HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise DataError("Publieke API niet bereikbaar of ongeldige JSON") from None
        if time.monotonic() - started > 3:
            raise DataError("Data-aanvraag duurde meer dan 3 seconden")
        if isinstance(result, dict) and "code" in result:
            raise DataError("Publieke API retourneert een foutcode")
        return result

    def now(self):
        return time.time() + self.offset

    def sync(self):
        if time.monotonic() - self.synced < 300:
            return
        before = time.time()
        stamp = self.get("/api/v3/time")["serverTime"] / 1000
        self.offset = stamp - (before + time.time()) / 2
        self.synced = time.monotonic()

    def book(self, symbol):
        raw = self.get("/api/v3/depth", symbol=symbol, limit=500)
        bids = [(positive(p), positive(q)) for p, q in raw["bids"]]
        asks = [(positive(p), positive(q)) for p, q in raw["asks"]]
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            raise DataError("Leeg/gekruist orderboek")
        if bids != sorted(bids, reverse=True) or asks != sorted(asks):
            raise DataError("Orderboek niet gesorteerd")
        return {"bids": bids, "asks": asks, "id": int(raw["lastUpdateId"]),
                "received": time.monotonic()}

    def context(self, symbol):
        now = self.now()
        # Fetch candle/ticker again if a 5-minute boundary was crossed.
        key = int(now // 300)
        cached = self.cache.get(symbol)
        if cached and cached[0] == key and time.monotonic() - cached[1] < 60:
            return cached[2], cached[3]
        candles = self.get("/api/v3/klines", symbol=symbol, interval="5m", limit=120)
        ticker = self.get("/api/v3/ticker/24hr", symbol=symbol)
        self.cache[symbol] = (key, time.monotonic(), candles, ticker)
        return candles, ticker

    def trades(self, symbol):
        return self.get("/api/v3/aggTrades", symbol=symbol, limit=1000)


def ema(values, period):
    out = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def candle_signal(raw, now):
    closed = [c for c in raw if int(c[6]) < now * 1000]
    if len(closed) < 60 or not 0 <= now * 1000 - int(closed[-1][6]) <= 360_000:
        raise DataError("Onvoldoende of verouderde gesloten candles")
    if any(int(b[0]) - int(a[0]) != 300_000 for a, b in zip(closed, closed[1:])):
        raise DataError("Gat in candlehistorie")
    closes = [positive(c[4]) for c in closed]
    fast, slow = ema(closes, 20), ema(closes, 50)
    ranges = [max(positive(c[2]) - positive(c[3]),
                  abs(float(c[2]) - closes[i - 1]),
                  abs(float(c[3]) - closes[i - 1]))
              for i, c in enumerate(closed) if i > 0]
    atr = statistics.mean(ranges[:14])
    for tr in ranges[14:]:
        atr = (atr * 13 + tr) / 14
    positive(atr)
    c = closed[-1]
    volume_ok = positive(c[7]) >= statistics.median([positive(x[7]) for x in closed[-21:-1]])
    trend = closes[-1] > fast[-1] > slow[-1] and fast[-1] > fast[-2]
    momentum = closes[-1] > positive(c[1]) and closes[-1] > closes[-2]
    return {"ok": trend and momentum and volume_ok, "atr": atr,
            "close": closes[-1], "candle": int(c[0])}


def trade_pressure(trades, now):
    recent = [t for t in trades if 0 <= now * 1000 - int(t["T"]) <= 30_000]
    if len(recent) < 10 or now * 1000 - max(int(t["T"]) for t in recent) > 15_000:
        raise DataError("Te weinig recente transacties")
    total = sum(positive(t["p"]) * positive(t["q"]) for t in recent)
    # m=True means buyer is maker: the aggressor is a SELLER.
    buys = sum(float(t["p"]) * float(t["q"]) for t in recent if t["m"] is False)
    return buys / total


def book_metrics(book):
    bid, ask = book["bids"][0][0], book["asks"][0][0]
    mid = (bid + ask) / 2
    low, high = mid * 0.999, mid * 1.001
    covered = book["bids"][-1][0] <= low and book["asks"][-1][0] >= high
    buy_depth = sum(p * q for p, q in book["bids"] if p >= low)
    sell_depth = sum(p * q for p, q in book["asks"] if p <= high)
    total = buy_depth + sell_depth
    return {"spread": (ask - bid) / mid * 10_000, "bid_depth": buy_depth,
            "ask_depth": sell_depth, "covered": covered,
            "imbalance": (buy_depth - sell_depth) / total if total else 0}


def regime(history, m, now):
    if not m["covered"]:
        return "ONDUIDELIJK"
    if len(history) < 30 or now - history[0][0] < 300:
        return "OPWARMEN"
    spread = statistics.median(x[1] for x in history)
    bids = statistics.median(x[2] for x in history)
    asks = statistics.median(x[3] for x in history)
    fragile = (m["spread"] > 8 or m["spread"] > 2 * spread
               or m["bid_depth"] < max(MIN_DEPTH, 0.55 * bids)
               or m["ask_depth"] < max(MIN_DEPTH, 0.55 * asks))
    return "FRAGIEL" if fragile else "STABIEL"


def fill(levels, qty, buy):
    if qty <= 0:
        raise DataError("Ongeldige ordergrootte")
    remaining, value = qty, 0.0
    for price, size in levels:
        take = min(remaining, size)
        value += take * price
        remaining -= take
        if remaining <= qty * 1e-10:
            return value / qty * (1 + SLIPPAGE if buy else 1 - SLIPPAGE)
    raise DataError("Onvoldoende boekdiepte voor volledige paper-uitvoering")


class Store:
    def __init__(self, path, initial):
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(path).expanduser()), timeout=0)
        # SQLite keeps the exclusive database lock across commits in this mode.
        self.db.execute("PRAGMA locking_mode=EXCLUSIVE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, timestamp TEXT, payload TEXT)")
        row = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        self.s = json.loads(row[0]) if row else {
            "version": 1, "cash": initial, "positions": {}, "last_entry": {},
            "day": "", "day_equity": initial, "halt": False, "losses": 0,
            "daily_entries": 0, "trades": 0, "wins": 0, "realized": 0.0}
        if self.s.get("version") != 1:
            raise ValueError("Onbekende databaseversie; niet automatisch resetten")
        if not math.isfinite(self.s["cash"]) or self.s["cash"] < 0:
            raise ValueError("Ongeldig opgeslagen saldo")
        self.save()

    def save(self, event=None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)",
                            (json.dumps(self.s, allow_nan=False),))
            if event:
                self.db.execute("INSERT INTO events(timestamp,payload) VALUES (?,?)",
                                (datetime.now(timezone.utc).isoformat(), json.dumps(event, allow_nan=False)))

    def equity(self):
        return self.s["cash"] + sum(p["mark_value"] for p in self.s["positions"].values())

    def day_guard(self, now):
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        s, eq = self.s, self.equity()
        if s["day"] != day:
            s.update(day=day, day_equity=eq, halt=False, losses=0, daily_entries=0)
        if eq <= s["day_equity"] * (1 - MAX_DAILY_LOSS) or s["losses"] >= 3:
            s["halt"] = True

    def open(self, symbol, book, info, now):
        s = self.s
        if (s["halt"] or s["daily_entries"] >= 6 or symbol in s["positions"]
                or len(s["positions"]) >= MAX_POSITIONS
                or s["last_entry"].get(symbol) == info["candle"]):
            return None
        estimate = book["asks"][0][0] * (1 + SLIPPAGE)
        distance = max(1.5 * info["atr"], estimate * 0.005)
        budget = self.equity() * RISK
        unit_risk = distance + 2 * estimate * (FEE + SLIPPAGE)
        allocation = min(self.equity() * MAX_ALLOCATION, s["cash"] * 0.95)
        qty = min(budget / unit_risk, allocation / (estimate * (1 + FEE)))
        entry = fill(book["asks"], qty, True)
        distance = max(1.5 * info["atr"], entry * 0.005)
        # Re-size against actual simulated execution and estimated stop costs.
        qty = min(qty, budget / (distance + 2 * entry * (FEE + SLIPPAGE)),
                  allocation / (entry * (1 + FEE)))
        entry = fill(book["asks"], qty, True)
        distance = max(1.5 * info["atr"], entry * 0.005)
        cost = qty * entry * (1 + FEE)
        if cost < 10 or cost > s["cash"] or distance >= entry:
            return None
        stop = entry - distance
        target = entry + 1.8 * distance
        mark = qty * fill(book["bids"], qty, False) * (1 - FEE)
        p = {"qty": qty, "entry": entry, "cost": cost, "stop": stop,
             "target": target, "opened": now, "mark_value": mark}
        s["cash"] -= cost
        s["positions"][symbol] = p
        s["last_entry"][symbol] = info["candle"]
        s["daily_entries"] += 1
        event = {"type": "PAPER_ENTRY", "symbol": symbol, **p}
        self.save(event)
        return f"PAPER LONG {symbol}\nInleg {cost:.2f} USDT | entry {entry:.6g}\nSL {stop:.6g} | TP {target:.6g}"

    def close(self, symbol, book, reason, now):
        s, p = self.s, self.s["positions"][symbol]
        price = fill(book["bids"], p["qty"], False)
        proceeds = p["qty"] * price * (1 - FEE)
        pnl = proceeds - p["cost"]
        s["cash"] += proceeds
        s["realized"] += pnl
        s["trades"] += 1
        s["wins"] += int(pnl > 0)
        s["losses"] = s["losses"] + 1 if pnl < 0 else 0
        del s["positions"][symbol]
        event = {"type": "PAPER_EXIT", "symbol": symbol, "reason": reason,
                 "price": price, "pnl_net": pnl, "time": now, "position": p}
        self.save(event)
        return f"PAPER EXIT {symbol} | {reason}\nNetto {pnl:+.2f} USDT, inclusief fees/slippage\nTotaal gerealiseerd {s['realized']:+.2f} USDT"


class Alerts:
    def __init__(self, enabled=True):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().removeprefix("bot")
        self.chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.enabled = enabled and bool(self.token and self.chat)
        self.q = queue.Queue(maxsize=100)
        if self.enabled:
            threading.Thread(target=self.worker, daemon=True).start()

    def send(self, message):
        LOG.info(message.replace("\n", " | "))
        if self.enabled:
            try:
                self.q.put_nowait(message)
            except queue.Full:
                LOG.warning("Telegram-wachtrij vol; bericht alleen gelogd")

    def worker(self):
        while True:
            msg = self.q.get()
            try:
                body = urlencode({"chat_id": self.chat, "text": msg}).encode()
                req = Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body)
                with urlopen(req, timeout=8) as response:
                    if not json.load(response).get("ok"):
                        LOG.warning("Telegram weigerde bericht; controleer chat-ID/token")
            except Exception:
                # Never log exception/URL: it contains the Telegram token.
                LOG.warning("Telegram-verzending mislukt; controleer token/chat-ID/netwerk")
            finally:
                self.q.task_done()


class Bot:
    def __init__(self, store, market, alerts):
        self.store, self.market, self.alerts = store, market, alerts
        self.history = {s: deque(maxlen=360) for s in SYMBOLS}
        self.confirm = Counter()
        self.fragile = Counter()
        self.book_ids = {}
        self.reasons = {}
        self.last_error = {}
        self.last_report = 0.0
        self.pool = ThreadPoolExecutor(max_workers=3)

    def error(self, symbol, exc):
        self.confirm[symbol] = self.fragile[symbol] = 0
        self.reasons[symbol] = str(exc)
        if time.monotonic() - self.last_error.get(symbol, -1e10) > 300:
            self.alerts.send(f"DATASTORING {symbol}: {exc}. Geen nieuwe instap; exits alleen met geldig boek.")
            self.last_error[symbol] = time.monotonic()

    def cycle(self):
        books = {}
        futures = {s: self.pool.submit(self.market.book, s) for s in SYMBOLS}
        for symbol, future in futures.items():
            try:
                books[symbol] = future.result()
            except (DataError, KeyError, TypeError, ValueError) as exc:
                self.error(symbol, exc)
        now = self.market.now()
        # Manage positions BEFORE slower signal requests, independent of entry filters.
        complete_marks = True
        for symbol, p in list(self.store.s["positions"].items()):
            try:
                book = books.get(symbol)
                if not book or time.monotonic() - book["received"] > 5:
                    raise DataError("Geen vers boek voor open positie")
                p["mark_value"] = p["qty"] * fill(book["bids"], p["qty"], False) * (1 - FEE)
            except DataError as exc:
                complete_marks = False
                self.error(symbol, exc)
        # Reset the day before realizing the first exit on the new day.
        if complete_marks:
            self.store.day_guard(now)
        for symbol, p in list(self.store.s["positions"].items()):
            book = books.get(symbol)
            if not book or time.monotonic() - book["received"] > 5:
                continue
            bid = book["bids"][0][0]
            why = ("STOP" if bid <= p["stop"] else "TARGET" if bid >= p["target"]
                   else "MAX 4 UUR" if now - p["opened"] >= 14_400 else None)
            if why:
                try:
                    self.alerts.send(self.store.close(symbol, book, why, now))
                except DataError as exc:
                    self.error(symbol, exc)
        if complete_marks:
            self.store.day_guard(now)
        # Clock synchronisation is an entry-data prerequisite, not an exit prerequisite.
        # A failing /time request must not prevent an already observed stop from exiting.
        self.market.sync()
        ok_count = 0
        for symbol in SYMBOLS:
            if symbol not in books:
                continue
            try:
                # Fetch signals, then refresh the book so entry does not use an old snapshot.
                raw, ticker = self.market.context(symbol)
                flow = trade_pressure(self.market.trades(symbol), self.market.now())
                book = self.market.book(symbol)
                now = self.market.now()
                info = candle_signal(raw, now)
                if abs(now * 1000 - int(ticker["closeTime"])) > 120_000:
                    raise DataError("24h-omzet is verouderd")
                m = book_metrics(book)
                if book["id"] <= self.book_ids.get(symbol, -1):
                    raise DataError("Orderboek-update-ID loopt niet op")
                self.book_ids[symbol] = book["id"]
                history = self.history[symbol]
                if history and now - history[-1][0] > 30:
                    history.clear()
                    self.confirm[symbol] = self.fragile[symbol] = 0
                while history and now - history[0][0] > 3600:
                    history.popleft()
                label = regime(history, m, now)
                if m["covered"]:
                    history.append((now, m["spread"], m["bid_depth"], m["ask_depth"]))
                self.fragile[symbol] = self.fragile[symbol] + 1 if label == "FRAGIEL" else 0
                candidate = (label == "STABIEL" and m["imbalance"] >= 0.15 and flow >= 0.58
                             and positive(ticker["quoteVolume"]) >= MIN_VOLUME and info["ok"]
                             and abs(book["asks"][0][0] - info["close"]) <= 0.5 * info["atr"])
                self.confirm[symbol] = self.confirm[symbol] + 1 if candidate else 0
                self.reasons[symbol] = (f"{label}, spread {m['spread']:.2f}bps, "
                                        f"koopdruk {flow:.0%}, bevestiging {self.confirm[symbol]}/3")
                if self.fragile[symbol] >= 3 and symbol in self.store.s["positions"]:
                    self.alerts.send(self.store.close(symbol, book, "LIQUIDITEIT FRAGIEL", now))
                # Require fresh liquidation marks for EVERY currently open position.
                fresh = complete_marks
                for held, p in self.store.s["positions"].items():
                    fresh_book = self.market.book(held)
                    p["mark_value"] = p["qty"] * fill(fresh_book["bids"], p["qty"], False) * (1 - FEE)
                if fresh:
                    self.store.day_guard(now)
                if fresh and self.confirm[symbol] >= 3:
                    if time.monotonic() - book["received"] > 3:
                        raise DataError("Entry-boek te oud na portefeuillecontrole")
                    message = self.store.open(symbol, book, info, now)
                    if message:
                        self.alerts.send(message)
                ok_count += 1
            except (DataError, KeyError, TypeError, ValueError) as exc:
                self.error(symbol, exc)
        self.store.save()
        if ok_count == len(SYMBOLS):
            HEALTH["last_success"] = time.time()
        if time.monotonic() - self.last_report >= 1800:
            s = self.store.s
            report = (f"LIQUIDITY GUARD | PAPER\nGeschat vermogen {self.store.equity():.2f} USDT"
                      f" | cash {s['cash']:.2f}\nOpen {len(s['positions'])} | gesloten {s['trades']}"
                      f" | netto gerealiseerd {s['realized']:+.2f} USDT\n"
                      f"Dagpauze {'JA' if s['halt'] else 'nee'} | entries vandaag {s['daily_entries']}/6\n"
                      + "\n".join(f"{x}: {self.reasons.get(x, 'wacht op data')}" for x in SYMBOLS))
            self.alerts.send(report)
            self.last_report = time.monotonic()


def health_server(port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            last = HEALTH["last_success"]
            age = time.time() - last if last else None
            payload = {**HEALTH, "data_age_seconds": age,
                       "data_ready": age is not None and age < 60}
            body = json.dumps(payload).encode()
            code = 503 if self.path == "/ready" and not payload["data_ready"] else 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def self_test():
    import tempfile
    import unittest

    class Tests(unittest.TestCase):
        def setUp(self):
            self.tmp = tempfile.TemporaryDirectory()
            self.path = str(Path(self.tmp.name) / "test.sqlite3")
            self.store = Store(self.path, 1000)
            self.book = {"bids": [(99.99, 1000), (99.8, 1000)],
                         "asks": [(100.01, 1000), (100.2, 1000)]}
            self.info = {"atr": 1.0, "candle": 123}

        def tearDown(self):
            self.store.db.close()
            self.tmp.cleanup()

        def test_fill_walks_book(self):
            self.assertAlmostEqual(fill([(100, 1), (110, 2)], 2, True), 105 * (1 + SLIPPAGE))
            with self.assertRaises(DataError):
                fill([(100, 1)], 2, False)

        def test_fees_and_roundtrip(self):
            self.store.open("BTCUSDT", self.book, self.info, 1000)
            self.store.close("BTCUSDT", self.book, "TEST", 1001)
            s = self.store.s
            self.assertLess(s["cash"], 1000)
            self.assertAlmostEqual(s["cash"], 1000 + s["realized"])
            self.assertEqual(s["trades"], 1)

        def test_gap_is_not_filled_at_stop(self):
            self.store.open("BTCUSDT", self.book, self.info, 1000)
            p = dict(self.store.s["positions"]["BTCUSDT"])
            crash = {"bids": [(90, 1000)]}
            self.store.close("BTCUSDT", crash, "STOP", 1001)
            expected = p["qty"] * 90 * (1 - SLIPPAGE) * (1 - FEE) - p["cost"]
            self.assertAlmostEqual(self.store.s["realized"], expected)

        def test_risk_budget_and_allocation(self):
            self.store.open("BTCUSDT", self.book, self.info, 1000)
            p = self.store.s["positions"]["BTCUSDT"]
            modeled_loss = p["cost"] - p["qty"] * p["stop"] * (1 - SLIPPAGE) * (1 - FEE)
            self.assertLessEqual(modeled_loss, 1000 * RISK)
            self.assertLessEqual(p["cost"], 1000 * MAX_ALLOCATION + 1e-9)

        def test_restart_duplicate_and_process_lock(self):
            self.store.open("BTCUSDT", self.book, self.info, 1000)
            other = sqlite3.connect(self.path, timeout=0)
            with self.assertRaises(sqlite3.OperationalError):
                other.execute("SELECT * FROM state").fetchall()
            other.close()
            self.store.close("BTCUSDT", self.book, "TEST", 1001)
            before = self.store.s["cash"]
            self.store.db.close()
            self.store = Store(self.path, 9999)
            self.assertEqual(self.store.s["cash"], before)
            self.assertIsNone(self.store.open("BTCUSDT", self.book, self.info, 1002))

        def test_daily_pause_and_reset(self):
            self.store.day_guard(1000)
            self.store.s["cash"] = 960
            self.store.day_guard(1001)
            self.assertTrue(self.store.s["halt"])
            self.assertIsNone(self.store.open("BTCUSDT", self.book, self.info, 1002))
            self.store.day_guard(100_000)
            self.assertFalse(self.store.s["halt"])
            self.assertEqual(self.store.s["day_equity"], 960)

        def test_fragility_and_warmup(self):
            m = book_metrics(self.book)
            self.assertEqual(regime([], m, 1000), "OPWARMEN")
            hist = [(i * 10, 2, 100_000, 100_000) for i in range(31)]
            self.assertEqual(regime(hist, m, 310), "STABIEL")
            self.assertEqual(regime(hist, {**m, "bid_depth": 10_000}, 310), "FRAGIEL")
            self.assertEqual(regime(hist, {**m, "covered": False}, 310), "ONDUIDELIJK")

        def test_trade_direction_and_staleness(self):
            ts = [{"T": 100_000, "p": "100", "q": "1", "m": False} for _ in range(10)]
            self.assertEqual(trade_pressure(ts, 100), 1)
            for t in ts:
                t["m"] = True
            self.assertEqual(trade_pressure(ts, 100), 0)
            with self.assertRaises(DataError):
                trade_pressure(ts, 120)

        def test_unclosed_candle_is_excluded(self):
            rows = []
            for i in range(61):
                p = 100 + i
                rows.append([i * 300_000, p - .5, p + 1, p - 1, p, 1,
                             (i + 1) * 300_000 - 1, 100, 1, 1, 1, 0])
            a = candle_signal(rows, 18_100)
            rows[-1][4] = 99_999
            b = candle_signal(rows, 18_100)
            self.assertEqual(a, b)
            self.assertTrue(a["ok"])

        def test_full_cycle_entry_exit_and_failed_data(self):
            class FakeMarket:
                def __init__(self):
                    self.t = 36_001.0
                    self.uid = 0
                    self.crash = False
                    self.broken = False

                def sync(self):
                    pass

                def now(self):
                    return self.t

                def book(self, symbol):
                    if self.broken:
                        raise DataError("synthetische storing")
                    self.uid += 1
                    bid, ask = (90, 90.02) if self.crash else (99.99, 100.01)
                    return {"bids": [(bid, 2000), (bid - .2, 1000)],
                            "asks": [(ask, 1000), (ask + .2, 1000)],
                            "id": self.uid, "received": time.monotonic()}

                def context(self, symbol):
                    rows = []
                    for i in range(120):
                        p = 100 - .01 * (119 - i)
                        rows.append([i * 300_000, p - .005, p + .1, p - .1, p, 1,
                                     (i + 1) * 300_000 - 1, 100, 1, 1, 1, 0])
                    return rows, {"closeTime": self.t * 1000, "quoteVolume": 30_000_000}

                def trades(self, symbol):
                    if self.crash:
                        raise DataError("transactiestoring na koersgat")
                    return [{"T": self.t * 1000, "p": "100", "q": "1", "m": False}
                            for _ in range(20)]

            class Silent:
                def send(self, _):
                    pass

            market = FakeMarket()
            bot = Bot(self.store, market, Silent())
            try:
                for symbol in SYMBOLS:
                    bot.history[symbol].extend((market.t - i * 10, 2, 200_000, 100_000)
                                               for i in range(32, 0, -1))
                bot.cycle()
                self.assertEqual(len(self.store.s["positions"]), 0)
                market.t += 10
                bot.cycle()
                self.assertEqual(len(self.store.s["positions"]), 0)
                market.t += 10
                bot.cycle()
                self.assertEqual(len(self.store.s["positions"]), 2)
                market.broken = True
                market.t += 10
                bot.cycle()
                self.assertEqual(len(self.store.s["positions"]), 2)
                self.assertTrue(all(bot.confirm[s] == 0 for s in SYMBOLS))
                market.broken = False
                market.crash = True
                market.t += 10
                bot.cycle()
                self.assertEqual(len(self.store.s["positions"]), 0)
                self.assertEqual(self.store.s["trades"], 2)
                self.assertLess(self.store.s["realized"], -30)
                self.assertTrue(self.store.s["halt"])
            finally:
                bot.pool.shutdown(wait=True)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description="Liquidity Guard — PAPER ONLY")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true", help="Eén scan, geen Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.self_test:
        return self_test()
    if os.getenv("PAPER_TRADING", "true").strip().lower() not in ("true", "1", "yes"):
        parser.error("Deze versie ondersteunt uitsluitend papertrading. Verwijder PAPER_TRADING=false.")
    initial = positive(os.getenv("INITIAL_CAPITAL", "1000"))
    store = Store(os.getenv("STATE_FILE", "liquidity.sqlite3"), initial)
    alerts = Alerts(enabled=not args.once)
    bot = Bot(store, Market(), alerts)
    server = health_server(int(os.environ["PORT"])) if os.getenv("PORT") else None
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    alerts.send("LIQUIDITY GUARD gestart | PAPER ONLY\nBTC / ETH / SOL spot; geen echte orders.\nWacht op marktdata; minimaal 5 minuten opwarmen.")
    try:
        while not STOP.is_set():
            start = time.monotonic()
            try:
                bot.cycle()
            except (DataError, KeyError, TypeError, ValueError) as exc:
                for symbol in SYMBOLS:
                    bot.confirm[symbol] = bot.fragile[symbol] = 0
                bot.error("ALGEMEEN", exc)
            if args.once:
                return 0 if HEALTH["last_success"] else 2
            STOP.wait(max(1, POLL_SECONDS - (time.monotonic() - start)))
    finally:
        bot.pool.shutdown(wait=True)
        store.save()
        store.db.close()
        if server:
            server.shutdown()
        LOG.info("Gestopt. Paper-posities blijven bewaard; tijdens stilstand geen exits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
