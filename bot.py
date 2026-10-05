#!/usr/bin/env python3
"""
JJ SIMON - FAIR PRICING THEORY BOT v1.2
BloFin Futures - AGGRESSIVE
========================================

Aggressieve instellingen behouden:
- Risk 1.8% per trade
- 1.25R targets
- 12x isolated
- Max 4 posities
- Scan elke 15 sec
- 2 min re-entry cooldown
- 12 min continuation / 100 min sessie
- 1m signalen

Gerichte verbeteringen:
1) Open posities worden ALTIJD beheerd, ook buiten de entry-sessie.
2) Fair Value komt van de echte 1m session-open candle, nooit van "last price" bij restart.
3) Displacement wordt gemeten tegen de mediane body van recente candles + BOS.
"""

import os
import time
import json
import hmac
import base64
import hashlib
import threading
from uuid import uuid4
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from statistics import median

import requests

try:
    from flask import Flask
except Exception:
    Flask = None

# ============================================================
# CONFIG - AGGRESSIEVE VERSIE
# ============================================================

VERSION = "1.2-Aggressive"
BOT_NAME = "JJ Fair Pricing Aggressive"

API_KEY = os.getenv("EXCHANGE_API_KEY", "").strip()
API_SECRET = os.getenv("EXCHANGE_SECRET", "").strip()
API_PASSPHRASE = os.getenv("EXCHANGE_PASSWORD", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if TELEGRAM_BOT_TOKEN.startswith("bot"):
    TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN[3:]
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PAPER_TRADING = os.getenv("PAPER_TRADING", "True").lower() == "true"
BLOFIN_DEMO = os.getenv("BLOFIN_DEMO", "False").lower() == "true"

BASE_URL = (
    "https://demo-trading-openapi.blofin.com"
    if BLOFIN_DEMO
    else "https://openapi.blofin.com"
)

# ----- Agressieve strategie parameters -----
SIGNAL_BAR = os.getenv("JJ_SIGNAL_BAR", "1m")
CONTINUATION_MINUTES = int(os.getenv("JJ_CONTINUATION_MINUTES", "12"))
SESSION_LENGTH_MINUTES = int(os.getenv("JJ_SESSION_LENGTH_MINUTES", "100"))

# Verbeterde displacement:
# huidige candle-body moet >= 1.25x de mediane body van de vorige 12 candles zijn.
# 1.25 houdt hem bewust agressief; hoger = selectiever.
DISPLACEMENT_BODY_MULT = float(os.getenv("JJ_DISPLACEMENT_BODY_MULT", "1.25"))
DISPLACEMENT_LOOKBACK = int(os.getenv("JJ_DISPLACEMENT_LOOKBACK", "12"))
BOS_LOOKBACK = int(os.getenv("JJ_BOS_LOOKBACK", "4"))

RISK_PER_TRADE = float(os.getenv("JJ_RISK_PER_TRADE", "0.018"))
FIXED_RR = float(os.getenv("JJ_FIXED_RR", "1.25"))
USE_ATR_STOPS = os.getenv("JJ_USE_ATR_STOPS", "True").lower() == "true"
ATR_PERIOD = int(os.getenv("JJ_ATR_PERIOD", "14"))
ATR_SL_MULT = float(os.getenv("JJ_ATR_SL_MULT", "1.1"))

LEVERAGE = int(os.getenv("JJ_LEVERAGE", "12"))
MARGIN_MODE = os.getenv("JJ_MARGIN_MODE", "isolated").strip().lower()
MAX_ACTIVE_POSITIONS = int(os.getenv("JJ_MAX_ACTIVE_POSITIONS", "4"))
SCAN_SECONDS = int(os.getenv("JJ_SCAN_SECONDS", "15"))
REENTRY_COOLDOWN_MIN = int(os.getenv("JJ_REENTRY_COOLDOWN_MIN", "2"))

# Sessie-opens (UTC)
SESSION_OPENS_UTC = [
    {"hour": 13, "minute": 30},   # New York open
]

STATE_FILE = "jj_fair_pricing_aggressive_v12_state.json"

DEFAULT_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT",
    "SUI-USDT", "NEAR-USDT", "APT-USDT", "ARB-USDT", "OP-USDT",
    "INJ-USDT", "TIA-USDT", "FET-USDT", "RENDER-USDT", "WIF-USDT",
    "PEPE-USDT", "WLD-USDT", "JUP-USDT", "ORDI-USDT"
]

_env_symbols = os.getenv("JJ_SYMBOLS", "").strip()
SYMBOLS = (
    [x.strip().upper() for x in _env_symbols.split(",") if x.strip()]
    if _env_symbols else DEFAULT_SYMBOLS
)

# ============================================================
# HELPERS
# ============================================================

session = requests.Session()
instrument_cache = {}
state_lock = threading.Lock()

state = {
    "managed": {},
    "fair_values": {},
    "last_exit_ts": {},
}

def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def now_ms():
    return int(time.time() * 1000)

def now_utc():
    return datetime.now(timezone.utc)

def fmt_price(v):
    if v >= 1000:
        return f"{v:.2f}"
    if v >= 1:
        return f"{v:.5f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")

def tg(message):
    print(message, flush=True)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def load_state():
    global state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            state.update(saved)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"State load error: {e}", flush=True)

def save_state():
    try:
        with state_lock:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"State save error: {e}", flush=True)

def floor_step(value, step):
    if step <= 0:
        return value
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    units = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(units * d_step)

def decimal_str(value):
    d = Decimal(str(value))
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s

# ============================================================
# BLOFIN API
# ============================================================

def _check_api_response(data, where):
    if not isinstance(data, dict):
        raise RuntimeError(f"{where}: invalid response")
    if str(data.get("code", "")) != "0":
        raise RuntimeError(f"{where}: {data.get('code')} {data.get('msg')}")
    return data

def public_get(path, params=None):
    params = params or {}
    r = session.get(BASE_URL + path, params=params, timeout=15)
    r.raise_for_status()
    return _check_api_response(r.json(), path)

def _signed_headers(method, request_path, body_str=""):
    if not API_KEY or not API_SECRET or not API_PASSPHRASE:
        raise RuntimeError("BloFin API credentials ontbreken")
    timestamp = str(now_ms())
    nonce = str(uuid4())
    prehash = f"{request_path}{method.upper()}{timestamp}{nonce}{body_str}"
    hex_sig = hmac.new(
        API_SECRET.encode(),
        prehash.encode(),
        hashlib.sha256,
    ).hexdigest().encode()
    signature = base64.b64encode(hex_sig).decode()
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json",
    }

def private_get(path, params=None):
    params = params or {}
    query = urlencode(params)
    request_path = path + (f"?{query}" if query else "")
    headers = _signed_headers("GET", request_path, "")
    r = session.get(BASE_URL + request_path, headers=headers, timeout=15)
    r.raise_for_status()
    return _check_api_response(r.json(), path)

def private_post(path, body):
    body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    headers = _signed_headers("POST", path, body_str)
    r = session.post(
        BASE_URL + path,
        headers=headers,
        data=body_str.encode(),
        timeout=15,
    )
    r.raise_for_status()
    return _check_api_response(r.json(), path)

# ============================================================
# MARKET DATA
# ============================================================

def load_instruments():
    global instrument_cache
    data = public_get("/api/v1/market/instruments").get("data", [])
    instrument_cache = {
        x.get("instId"): x
        for x in data
        if isinstance(x, dict) and x.get("instId")
    }
    return instrument_cache

def valid_symbols():
    if not instrument_cache:
        load_instruments()
    return [s for s in SYMBOLS if s in instrument_cache]

def instrument(symbol):
    if symbol not in instrument_cache:
        load_instruments()
    x = instrument_cache.get(symbol)
    if not x:
        raise RuntimeError(f"Instrument niet beschikbaar: {symbol}")
    return x

def candles(symbol, bar, limit=50, confirmed_only=True):
    """
    BloFin candles. Voor signalen gebruiken we alleen gesloten candles.
    Voor de exacte session-open mogen we ook de lopende 1m candle lezen,
    omdat de OPEN-prijs vanaf het begin van die candle vaststaat.
    """
    data = public_get(
        "/api/v1/market/candles",
        {"instId": symbol, "bar": bar, "limit": str(limit)},
    ).get("data", [])

    rows = []
    for c in data:
        if not isinstance(c, list) or len(c) < 6:
            continue

        confirm = str(c[-1])
        if confirmed_only and confirm != "1":
            continue

        rows.append({
            "ts": int(c[0]),
            "open": fnum(c[1]),
            "high": fnum(c[2]),
            "low": fnum(c[3]),
            "close": fnum(c[4]),
            "volume": fnum(c[5]),
            "confirm": confirm,
        })

    rows.sort(key=lambda x: x["ts"])
    return rows

def session_open_price(symbol, session_open):
    """
    Haalt de ECHTE open-prijs van de 1m candle waarvan ts exact gelijk is
    aan de ingestelde sessie-open.

    Belangrijk: bij een Railway restart om bv. 14:05 UTC blijft de fair value
    dus de 13:30 candle-open en wordt NIET de actuele tickerprijs gebruikt.
    """
    target_ts = int(session_open.timestamp() * 1000)

    # Sessie duurt 100 min; 180 recente 1m candles geeft ruime marge.
    rows = candles(symbol, "1m", limit=180, confirmed_only=False)

    for row in rows:
        if row["ts"] == target_ts:
            return row["open"]

    return None

def last_price(symbol):
    data = public_get(
        "/api/v1/market/tickers",
        {"instId": symbol},
    ).get("data", [])
    if not data:
        raise RuntimeError(f"Geen ticker voor {symbol}")
    return fnum(data[0].get("last"))

def account():
    data = private_get("/api/v1/account/balance").get("data", {})
    equity = fnum(data.get("totalEquity"))
    available = 0.0
    for d in data.get("details", []) or []:
        if d.get("currency") == "USDT":
            available = fnum(d.get("available"))
            break
    return equity, available

def positions():
    data = private_get("/api/v1/account/positions").get("data", [])
    out = []
    for p in data or []:
        qty = fnum(p.get("positions"))
        if abs(qty) <= 0:
            continue
        p = dict(p)
        p["_qty"] = qty
        p["_is_long"] = (
            str(p.get("positionSide", "")).lower() in ("long", "net")
            and qty > 0
        )
        out.append(p)
    return out

def get_position(symbol):
    for p in positions():
        if p.get("instId") == symbol:
            return p
    return None

def set_leverage(symbol):
    body = {
        "instId": symbol,
        "leverage": str(LEVERAGE),
        "marginMode": MARGIN_MODE,
    }
    return private_post("/api/v1/account/set-leverage", body)

# ============================================================
# STRATEGY LOGIC
# ============================================================

def get_current_session_open():
    now = now_utc()
    candidates = []
    for s in SESSION_OPENS_UTC:
        open_time = now.replace(
            hour=s["hour"],
            minute=s["minute"],
            second=0,
            microsecond=0,
        )
        if open_time > now:
            open_time -= timedelta(days=1)
        candidates.append(open_time)
    return max(candidates)

def is_in_session_window(session_open):
    minutes_since = (now_utc() - session_open).total_seconds() / 60
    return 0 <= minutes_since <= SESSION_LENGTH_MINUTES

def get_phase(session_open):
    minutes_since = (now_utc() - session_open).total_seconds() / 60
    if minutes_since <= CONTINUATION_MINUTES:
        return "continuation"
    return "reversion"

def detect_displacement_and_bos(rows):
    """
    Aggressieve maar robuustere displacement:
    - huidige body >= DISPLACEMENT_BODY_MULT * median body van vorige N candles
    - candle moet BOS maken over de vorige BOS_LOOKBACK highs/lows

    Dit voorkomt dat een relatief kleine candle al displacement heet alleen
    omdat de direct voorgaande candle toevallig nóg kleiner was.
    """
    need = max(DISPLACEMENT_LOOKBACK + 1, BOS_LOOKBACK + 1, 6)
    if len(rows) < need:
        return None

    curr = rows[-1]
    history = rows[-(DISPLACEMENT_LOOKBACK + 1):-1]

    historical_bodies = [
        abs(r["close"] - r["open"])
        for r in history
        if abs(r["close"] - r["open"]) > 0
    ]
    if not historical_bodies:
        return None

    baseline_body = median(historical_bodies)
    body_curr = abs(curr["close"] - curr["open"])

    is_displacement = (
        baseline_body > 0
        and body_curr >= baseline_body * DISPLACEMENT_BODY_MULT
    )
    if not is_displacement:
        return None

    bos_rows = rows[-(BOS_LOOKBACK + 1):-1]
    recent_high = max(r["high"] for r in bos_rows)
    recent_low = min(r["low"] for r in bos_rows)

    bullish_bos = curr["close"] > recent_high
    bearish_bos = curr["close"] < recent_low

    if bullish_bos:
        direction = "long"
    elif bearish_bos:
        direction = "short"
    else:
        return None

    return {
        "displacement": True,
        "bos": True,
        "direction": direction,
        "close": curr["close"],
        "ts": curr["ts"],
        "body": body_curr,
        "median_body": baseline_body,
        "body_multiple": body_curr / baseline_body if baseline_body > 0 else 0,
    }

def strategy_snapshot(symbol, session_open, fair_value):
    rows = candles(symbol, SIGNAL_BAR, limit=50, confirmed_only=True)
    if len(rows) < max(DISPLACEMENT_LOOKBACK + 1, BOS_LOOKBACK + 1, 8):
        return None

    signal = detect_displacement_and_bos(rows)
    if not signal:
        return None

    phase = get_phase(session_open)
    minutes_since = (now_utc() - session_open).total_seconds() / 60

    valid = False

    # Continuation:
    # boven fair value + bullish displacement/BOS -> long
    # onder fair value + bearish displacement/BOS -> short
    if phase == "continuation":
        if signal["direction"] == "long" and signal["close"] > fair_value:
            valid = True
        elif signal["direction"] == "short" and signal["close"] < fair_value:
            valid = True

    # Reversion:
    # onder fair value + bullish displacement/BOS -> long terug naar fair
    # boven fair value + bearish displacement/BOS -> short terug naar fair
    else:
        if signal["direction"] == "long" and signal["close"] < fair_value:
            valid = True
        elif signal["direction"] == "short" and signal["close"] > fair_value:
            valid = True

    if not valid:
        return None

    return {
        "symbol": symbol,
        "direction": signal["direction"],
        "phase": phase,
        "fair_value": fair_value,
        "close": signal["close"],
        "minutes_since_open": minutes_since,
        "ts": signal["ts"],
        "body_multiple": signal["body_multiple"],
    }

# ============================================================
# SIZING & ORDERS
# ============================================================

def calculate_size(symbol, entry_price, equity, available, stop_distance):
    meta = instrument(symbol)
    contract_value = fnum(meta.get("contractValue"))
    min_size = fnum(meta.get("minSize"))
    lot_size = fnum(meta.get("lotSize"))

    if min(
        contract_value,
        min_size,
        lot_size,
        entry_price,
        stop_distance,
    ) <= 0:
        return None

    risk_amount = equity * RISK_PER_TRADE
    notional = risk_amount / (stop_distance / entry_price)

    # Agressieve bestaande cap behouden.
    max_notional = available * 0.88 * LEVERAGE
    notional = min(notional, max_notional)

    contracts = floor_step(
        notional / (contract_value * entry_price),
        lot_size,
    )
    if contracts < min_size:
        return None

    return {
        "contracts": contracts,
        "notional": contracts * contract_value * entry_price,
        "risk_usdt": risk_amount,
    }

def open_position(snapshot):
    symbol = snapshot["symbol"]
    direction = snapshot["direction"]

    equity, available = account()
    live = last_price(symbol)

    last_exit = int(
        state.get("last_exit_ts", {}).get(symbol, 0) or 0
    )
    if (
        last_exit
        and now_ms() - last_exit < REENTRY_COOLDOWN_MIN * 60_000
    ):
        return False

    # Stop distance - agressieve bestaande instellingen behouden.
    if USE_ATR_STOPS:
        rows = candles(
            symbol,
            SIGNAL_BAR,
            40,
            confirmed_only=True,
        )
        trs = []
        for i in range(1, len(rows)):
            tr = max(
                rows[i]["high"] - rows[i]["low"],
                abs(rows[i]["high"] - rows[i - 1]["close"]),
                abs(rows[i]["low"] - rows[i - 1]["close"]),
            )
            trs.append(tr)

        atr_slice = trs[-ATR_PERIOD:]
        atr = (
            sum(atr_slice) / len(atr_slice)
            if atr_slice
            else live * 0.01
        )
        stop_dist = atr * ATR_SL_MULT
    else:
        stop_dist = live * 0.007

    sizing = calculate_size(
        symbol,
        live,
        equity,
        available,
        stop_dist,
    )
    if not sizing:
        return False

    if direction == "long":
        stop = live - stop_dist
        tp = live + stop_dist * FIXED_RR
        side = "buy"
    else:
        stop = live + stop_dist
        tp = live - stop_dist * FIXED_RR
        side = "sell"

    tg(
        f"🟠 JJ AGGRESSIVE READY\n"
        f"{symbol} {direction.upper()} · {snapshot['phase'].upper()}\n"
        f"Fair Value: {fmt_price(snapshot['fair_value'])}\n"
        f"Entry ~{fmt_price(live)} | SL {fmt_price(stop)} | "
        f"TP {fmt_price(tp)} ({FIXED_RR}R)\n"
        f"Displacement: {snapshot.get('body_multiple', 0):.2f}x median body\n"
        f"Minuten open: {snapshot['minutes_since_open']:.1f}"
    )

    record = {
        "entry_ts": now_ms(),
        "entry_price": live,
        "direction": direction,
        "stop": stop,
        "tp": tp,
        "phase": snapshot["phase"],
        "fair_value": snapshot["fair_value"],
        "contracts": sizing["contracts"],
        "risk_usdt": sizing["risk_usdt"],
        "notional": sizing["notional"],
        "signal_ts": snapshot["ts"],
        "body_multiple": snapshot.get("body_multiple", 0),
        "paper": PAPER_TRADING,
    }

    if PAPER_TRADING:
        with state_lock:
            state["managed"][symbol] = record
        save_state()
        tg(
            f"🟢 PAPER OPEN {symbol} {direction.upper()} "
            f"@ {fmt_price(live)}"
        )
        return True

    set_leverage(symbol)

    body = {
        "instId": symbol,
        "marginMode": MARGIN_MODE,
        "positionSide": "net",
        "side": side,
        "orderType": "market",
        "size": decimal_str(sizing["contracts"]),
        "reduceOnly": "false",
        "clientOrderId": (
            f"jja{int(time.time())}{symbol.split('-')[0]}"
        )[:32],
        "slTriggerPrice": decimal_str(stop),
        "slOrderPrice": "-1",
        "slTriggerPriceType": "mark",
    }

    private_post("/api/v1/trade/order", body)

    with state_lock:
        state["managed"][symbol] = record
    save_state()

    tg(
        f"🟢 LIVE OPEN {symbol} {direction.upper()} "
        f"@ ~{fmt_price(live)}"
    )
    return True

def manage_position(symbol):
    """
    BELANGRIJK:
    Deze functie wordt voortaan altijd aangeroepen, óók buiten de 100-min
    entry-sessie. Daardoor blijven TP / fair-value exits actief.
    De exchange-side hard SL blijft daarnaast de noodbeveiliging.
    """
    managed = state.get("managed", {}).get(symbol)
    if not managed:
        return

    live = last_price(symbol)
    direction = managed["direction"]
    entry = managed["entry_price"]
    stop = managed["stop"]
    tp = managed["tp"]
    fair = managed.get("fair_value", 0)

    hit_stop = (
        (direction == "long" and live <= stop)
        or (direction == "short" and live >= stop)
    )
    hit_tp = (
        (direction == "long" and live >= tp)
        or (direction == "short" and live <= tp)
    )

    reached_fair = False
    if managed["phase"] == "reversion" and fair > 0:
        if direction == "long" and live >= fair * 0.997:
            reached_fair = True
        elif direction == "short" and live <= fair * 1.003:
            reached_fair = True

    if not (hit_stop or hit_tp or reached_fair):
        return

    reason = (
        "STOP"
        if hit_stop
        else ("TP" if hit_tp else "FAIR VALUE")
    )

    if PAPER_TRADING:
        pnl_pct = (
            ((live - entry) / entry * 100)
            if direction == "long"
            else ((entry - live) / entry * 100)
        )

        # Indicatieve bruto PnL op opgeslagen notional.
        pnl_usdt = (
            managed.get("notional", 0.0)
            * (pnl_pct / 100.0)
        )

        tg(
            f"{'🟢' if pnl_pct >= 0 else '🔴'} "
            f"PAPER CLOSE {symbol} · {reason}\n"
            f"{pnl_pct:+.2f}% · ~{pnl_usdt:+.3f} USDT bruto"
        )

        with state_lock:
            state["managed"].pop(symbol, None)
            state["last_exit_ts"][symbol] = now_ms()
        save_state()
        return

    pos = get_position(symbol)
    if pos:
        body = {
            "instId": symbol,
            "marginMode": MARGIN_MODE,
            "positionSide": "net",
            "clientOrderId": f"jjax{int(time.time())}"[:32],
        }
        private_post("/api/v1/trade/close-position", body)
        tg(f"📤 LIVE CLOSE {symbol} · {reason}")

    with state_lock:
        state["managed"].pop(symbol, None)
        state["last_exit_ts"][symbol] = now_ms()
    save_state()

# ============================================================
# MAIN LOOP
# ============================================================

def ensure_fair_values(session_open, symbols):
    """
    Leg fair value vast vanaf de echte 1m session-open candle.
    Geen fallback naar last_price: liever geen entry dan een verkeerde fair value.
    """
    session_id = session_open.strftime("%Y%m%d_%H%M")
    changed = False

    for symbol in symbols:
        key = f"{symbol}_{session_id}"
        if key in state.get("fair_values", {}):
            continue

        try:
            price = session_open_price(symbol, session_open)
            if price is None or price <= 0:
                print(
                    f"Fair value nog niet gevonden voor {symbol} "
                    f"op {session_open.isoformat()}",
                    flush=True,
                )
                continue

            state.setdefault("fair_values", {})[key] = {
                "price": price,
                "ts": int(session_open.timestamp() * 1000),
                "source": "1m_session_open_candle",
            }
            changed = True

        except Exception as e:
            print(f"Fair value error {symbol}: {e}", flush=True)

        time.sleep(0.03)

    if changed:
        save_state()

def scan_once():
    symbols = valid_symbols()

    # ========================================================
    # FIX 1: POSITIES ALTIJD BEHEREN
    # Dit gebeurt vóór de session-window check.
    # ========================================================
    for symbol in list(state.get("managed", {}).keys()):
        try:
            manage_position(symbol)
        except Exception as e:
            tg(f"⚠️ Manage error {symbol}: {e}")

    # Alleen NIEUWE ENTRIES zijn beperkt tot de JJ sessie.
    session_open = get_current_session_open()
    if not is_in_session_window(session_open):
        return

    # ========================================================
    # FIX 2: ECHTE SESSION-OPEN FAIR VALUE
    # ========================================================
    ensure_fair_values(session_open, symbols)

    active = len(state.get("managed", {}))
    free_slots = max(0, MAX_ACTIVE_POSITIONS - active)
    if free_slots <= 0:
        return

    session_id = session_open.strftime("%Y%m%d_%H%M")
    candidates = []

    for symbol in symbols:
        if symbol in state.get("managed", {}):
            continue

        key = f"{symbol}_{session_id}"
        fv_data = state.get("fair_values", {}).get(key)
        if not fv_data:
            continue

        try:
            snap = strategy_snapshot(
                symbol,
                session_open,
                fv_data["price"],
            )
            if snap:
                candidates.append(snap)
        except Exception as e:
            print(f"Signal error {symbol}: {e}", flush=True)

        time.sleep(0.04)

    # Meest recente bevestigde signalen eerst.
    candidates.sort(key=lambda x: x["ts"], reverse=True)

    opened = 0
    for snap in candidates:
        if opened >= free_slots:
            break
        try:
            if open_position(snap):
                opened += 1
        except Exception as e:
            tg(f"❌ Entry error {snap['symbol']}: {e}")

def startup():
    load_state()
    load_instruments()
    symbols = valid_symbols()

    tg(
        f"🧠 {BOT_NAME} v{VERSION} STARTED\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"Fair Pricing Aggressive\n"
        f"Continuation: {CONTINUATION_MINUTES} min | "
        f"Sessie: {SESSION_LENGTH_MINUTES} min\n"
        f"Risk {RISK_PER_TRADE*100:.1f}% | RR {FIXED_RR} | "
        f"ATR stops: {USE_ATR_STOPS}\n"
        f"Displacement >= {DISPLACEMENT_BODY_MULT:.2f}x "
        f"median body ({DISPLACEMENT_LOOKBACK} candles) + "
        f"BOS({BOS_LOOKBACK})\n"
        f"Max posities: {MAX_ACTIVE_POSITIONS} | "
        f"Leverage: {LEVERAGE}x | Scan: {SCAN_SECONDS}s\n"
        f"Fair value: echte 1m session-open candle\n"
        f"Position management: 24/7\n"
        f"Symbolen: {len(symbols)}"
    )

def main():
    if Flask is not None:
        def run_health():
            app = Flask(__name__)

            @app.route("/")
            def home():
                return f"{BOT_NAME} v{VERSION} is running", 200

            app.run(
                host="0.0.0.0",
                port=int(os.getenv("PORT", 8080)),
                use_reloader=False,
            )

        threading.Thread(
            target=run_health,
            daemon=True,
        ).start()

    startup()

    while True:
        try:
            scan_once()
        except KeyboardInterrupt:
            tg(f"🛑 {BOT_NAME} gestopt")
            break
        except Exception as e:
            tg(f"⚠️ Loop error: {e}")

        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    main()
