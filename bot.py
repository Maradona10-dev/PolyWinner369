#!/usr/bin/env python3
"""
JJ SIMON - FAIR PRICING THEORY BOT v1.4.2 (Strikte Minimaal 12 USDT Marge per Trade)
BloFin Futures - AGGRESSIVE & TREND RUNNER
========================================

Geoptimaliseerde instellingen:
- Minimaal 12 USDT marge per trade (keihard afgedwongen, geen centenwerk)
- Maximaal 2 actieve posities tegelijk
- Runner activeert pas op 1.60R
- Locked winst bij runner: minimaal 0.75R gegarandeerd
- Ruimere trailing (0.75R) om uitzakken te voorkomen
- Max target op 3.00R voor de uitschieters
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
# CONFIG - STRIKT MINIMAAL 12 USDT MARGE PER TRADE
# ============================================================

VERSION = "1.4.2-StriktMin12USDT-Margin"
BOT_NAME = "JJ Fair Pricing Profit Runner"

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

DISPLACEMENT_BODY_MULT = float(os.getenv("JJ_DISPLACEMENT_BODY_MULT", "1.25"))
DISPLACEMENT_LOOKBACK = int(os.getenv("JJ_DISPLACEMENT_LOOKBACK", "12"))
BOS_LOOKBACK = int(os.getenv("JJ_BOS_LOOKBACK", "4"))

RISK_PER_TRADE = float(os.getenv("JJ_RISK_PER_TRADE", "0.018"))
FIXED_RR = float(os.getenv("JJ_FIXED_RR", "2.00"))

# ----- STRIKT MINIMUM MARGE PER TRADE (12 USDT) -----
MIN_MARGIN_USDT = float(os.getenv("JJ_MIN_MARGIN_USDT", "12.0"))

# ----- PROFIT RUNNER -----
RUNNER_ACTIVATE_R = float(os.getenv("JJ_RUNNER_ACTIVATE_R", "1.60"))
RUNNER_TRAIL_R = float(os.getenv("JJ_RUNNER_TRAIL_R", "0.75"))
RUNNER_LOCK_R = float(os.getenv("JJ_RUNNER_LOCK_R", "0.75"))
RUNNER_MAX_TARGET_R = float(os.getenv("JJ_RUNNER_MAX_TARGET_R", "3.00"))
FAIR_VALUE_MIN_R = float(os.getenv("JJ_FAIR_VALUE_MIN_R", "0.75"))

USE_ATR_STOPS = os.getenv("JJ_USE_ATR_STOPS", "True").lower() == "true"
ATR_PERIOD = int(os.getenv("JJ_ATR_PERIOD", "14"))
ATR_SL_MULT = float(os.getenv("JJ_ATR_SL_MULT", "1.1"))

LEVERAGE = int(os.getenv("JJ_LEVERAGE", "12"))
MARGIN_MODE = os.getenv("JJ_MARGIN_MODE", "isolated").strip().lower()
MAX_ACTIVE_POSITIONS = int(os.getenv("JJ_MAX_ACTIVE_POSITIONS", "2"))
SCAN_SECONDS = int(os.getenv("JJ_SCAN_SECONDS", "15"))
REENTRY_COOLDOWN_MIN = int(os.getenv("JJ_REENTRY_COOLDOWN_MIN", "2"))

SESSION_OPENS_UTC = [
    {"hour": 1, "minute": 30},    # China (Shanghai) open (01:30 UTC)
    {"hour": 13, "minute": 0},    # Zuid-Amerika (São Paulo/B3) open (13:00 UTC)
    {"hour": 13, "minute": 30},   # New York open (13:30 UTC)
]

STATE_FILE = "jj_fair_pricing_profit_runner_v142_state.json"

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
    target_ts = int(session_open.timestamp() * 1000)
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
    if phase == "continuation":
        if signal["direction"] == "long" and signal["close"] > fair_value:
            valid = True
        elif signal["direction"] == "short" and signal["close"] < fair_value:
            valid = True
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
# SIZING & ORDERS (STRIKTE MINIMALE MARGE VAN 12 USDT)
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

    # Strikte afdwinging: minimaal 12 USDT marge * hefboom = totale notionele positie
    target_margin = MIN_MARGIN_USDT  # 12.0 USDT
    notional = target_margin * LEVERAGE

    # Controleer of we wel voldoende beschikbaar saldo hebben (88% van available * hefboom)
    max_notional = available * 0.88 * LEVERAGE
    if notional > max_notional:
        return None  # Sla over als er geen 12 USDT marge gepakt kan worden

    risk_amount = notional * (stop_distance / entry_price)

    contracts = floor_step(
        notional / (contract_value * entry_price),
        lot_size,
    )
    
    # Controleer of de berekende contracten voldoen aan de minimum eisen van de exchange
    if contracts < min_size:
        return None

    # Dubbelcheck of de uiteindelijke notionele waarde niet te laag uitvalt door afronding
    final_notional = contracts * contract_value * entry_price
    if (final_notional / LEVERAGE) < (MIN_MARGIN_USDT * 0.95):
        return None

    return {
        "contracts": contracts,
        "notional": final_notional,
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

    if USE_ATR_STOPS:
        rows = candles(symbol, SIGNAL_BAR, 40, confirmed_only=True)
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

    sizing = calculate_size(symbol, live, equity, available, stop_dist)
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
        f"🟠 JJ STRICT MIN12 READY\n"
        f"{symbol} {direction.upper()} · {snapshot['phase'].upper()}\n"
        f"Fair Value: {fmt_price(snapshot['fair_value'])}\n"
        f"Entry ~{fmt_price(live)} | SL {fmt_price(stop)} | "
        f"Marge ~{sizing['notional']/LEVERAGE:.2f} USDT\n"
        f"Displacement: {snapshot.get('body_multiple', 0):.2f}x median body"
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
        "initial_stop_distance": stop_dist,
        "runner_active": False,
        "best_price": live,
        "fair_value_touched": False,
        "paper": PAPER_TRADING,
    }

    if PAPER_TRADING:
        with state_lock:
            state["managed"][symbol] = record
        save_state()
        tg(f"🟢 PAPER OPEN {symbol} {direction.upper()} @ {fmt_price(live)}")
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
        "clientOrderId": (f"jjm12{int(time.time())}{symbol.split('-')[0]}")[:32],
        "slTriggerPrice": decimal_str(stop),
        "slOrderPrice": "-1",
        "slTriggerPriceType": "mark",
    }

    private_post("/api/v1/trade/order", body)

    with state_lock:
        state["managed"][symbol] = record
    save_state()

    tg(f"🟢 LIVE OPEN {symbol} {direction.upper()} @ ~{fmt_price(live)}")
    return True

def manage_position(symbol):
    managed = state.get("managed", {}).get(symbol)
    if not managed:
        return

    live = last_price(symbol)
    direction = managed["direction"]
    entry = fnum(managed.get("entry_price"))
    stop = fnum(managed.get("stop"))
    fair = fnum(managed.get("fair_value"))

    risk_dist = fnum(managed.get("initial_stop_distance"))
    if risk_dist <= 0:
        risk_dist = abs(entry - stop)
    if entry <= 0 or risk_dist <= 0:
        return

    if direction == "long":
        r_now = (live - entry) / risk_dist
        hit_stop = live <= stop
    else:
        r_now = (entry - live) / risk_dist
        hit_stop = live >= stop

    best = fnum(managed.get("best_price"), entry)
    if direction == "long":
        best = max(best, live)
    else:
        best = min(best, live)
    managed["best_price"] = best

    fair_touched = bool(managed.get("fair_value_touched", False))
    if managed.get("phase") == "reversion" and fair > 0 and not fair_touched:
        touched = (
            (direction == "long" and live >= fair)
            or (direction == "short" and live <= fair)
        )
        if touched:
            managed["fair_value_touched"] = True
            if r_now >= FAIR_VALUE_MIN_R:
                tg(
                    f"🎯 FAIR VALUE {symbol} geraakt · {r_now:+.2f}R\n"
                    f"Positie blijft open voor Profit Runner."
                )

    runner_active = bool(managed.get("runner_active", False))
    if not runner_active and r_now >= RUNNER_ACTIVATE_R:
        managed["runner_active"] = True
        runner_active = True
        tg(
            f"🚀 PROFIT RUNNER {symbol} actief · {r_now:+.2f}R\n"
            f"Lock minimaal +{RUNNER_LOCK_R:.2f}R · "
            f"trail {RUNNER_TRAIL_R:.2f}R · target {RUNNER_MAX_TARGET_
