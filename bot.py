#!/usr/init/env python3
"""
JJ SIMON - EBP STRATEGY BOT v2.1 (60 Munten Editie)
BloFin Futures - Volledige Integratie met Pandas EBP Logica
==============================================================
- Detecteert Bullish & Bearish EBP signalen op 60 munten
- Strikte minimale marge per trade (12.0 USDT)
- Automatische risico- en orderafhandeling voor BloFin
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
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
import pandas as pd
import numpy as np

try:
    from flask import Flask
except Exception:
    Flask = None

# ============================================================
# CONFIGURATIE & PARAMETERS
# ============================================================

VERSION = "2.1-EBP-60Coins"
BOT_NAME = "JJ EBP Strategy Bot"

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

SIGNAL_BAR = os.getenv("JJ_SIGNAL_BAR", "15m")  # 15 minuten candles
MIN_MARGIN_USDT = float(os.getenv("JJ_MIN_MARGIN_USDT", "12.0"))
FIXED_RR = float(os.getenv("JJ_FIXED_RR", "2.00"))
LEVERAGE = int(os.getenv("JJ_LEVERAGE", "12"))
MARGIN_MODE = os.getenv("JJ_MARGIN_MODE", "isolated").strip().lower()
MAX_ACTIVE_POSITIONS = int(os.getenv("JJ_MAX_ACTIVE_POSITIONS", "2"))
SCAN_SECONDS = int(os.getenv("JJ_SCAN_SECONDS", "30"))

STATE_FILE = "jj_ebp_bot_state.json"

# Uitgebreide lijst van exact 60 sterke munten
DEFAULT_SYMBOLS = [
    # Majors & Layer 1s (1-15)
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
    "ADA-USDT", "AVAX-USDT", "DOT-USDT", "NEAR-USDT", "SUI-USDT",
    "APT-USDT", "SEI-USDT", "FTM-USDT", "HBAR-USDT", "THETA-USDT",
    # Populaire Altcoins & DeFi (16-35)
    "LINK-USDT", "ARB-USDT", "OP-USDT", "INJ-USDT", "TIA-USDT",
    "FET-USDT", "RENDER-USDT", "TAO-USDT", "STRK-USDT", "MANTA-USDT",
    "ALT-USDT", "DYM-USDT", "ZETA-USDT", "PORTAL-USDT", "AXL-USDT",
    "ENA-USDT", "CRV-USDT", "LDO-USDT", "JUP-USDT", "ORDI-USDT",
    # Memes & High Volume (36-60)
    "DOGE-USDT", "SHIB-USDT", "PEPE-USDT", "WIF-USDT", "WLD-USDT",
    "COR-USDT", "PUMP-USDT", "BOME-USDT", "MEW-USDT", "PNUT-USDT",
    "ACT-USDT", "GOAT-USDT", "NEIRO-USDT", "POPCAT-USDT", "FLOKI-USDT",
    "BONK-USDT", "CHZ-USDT", "RUNE-USDT", "ATOM-USDT", "ALGO-USDT",
    "SAND-USDT", "MANA-USDT", "AXS-USDT", "GALA-USDT", "GMT-USDT"
]

_env_symbols = os.getenv("JJ_SYMBOLS", "").strip()
SYMBOLS = (
    [x.strip().upper() for x in _env_symbols.split(",") if x.strip()]
    if _env_symbols else DEFAULT_SYMBOLS
)

# ============================================================
# HELPERS & STATE MANAGEMENT
# ============================================================

session = requests.Session()
instrument_cache = {}
state_lock = threading.Lock()

state = {
    "managed": {},
    "last_exit_ts": {},
}

def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def now_ms():
    return int(time.time() * 1000)

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
# BLOFIN API AUTH & REQUESTS
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
# MARKET DATA & INSTRUMENTS
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

def get_candles_df(symbol, bar, limit=60):
    data = public_get(
        "/api/v1/market/candles",
        {"instId": symbol, "bar": bar, "limit": str(limit)},
    ).get("data", [])

    rows = []
    for c in data:
        if not isinstance(c, list) or len(c) < 6:
            continue
        rows.append({
            "ts": int(c[0]),
            "open": fnum(c[1]),
            "high": fnum(c[2]),
            "low": fnum(c[3]),
            "close": fnum(c[4]),
            "volume": fnum(c[5]),
        })
    
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.sort_values("ts").reset_index(drop=True)
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("datetime", inplace=True)
    return df

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
    available = 0.0
    for d in data.get("details", []) or []:
        if d.get("currency") == "USDT":
            available = fnum(d.get("available"))
            break
    return available

def set_leverage(symbol):
    body = {
        "instId": symbol,
        "leverage": str(LEVERAGE),
        "marginMode": MARGIN_MODE,
    }
    return private_post("/api/v1/account/set-leverage", body)

# ============================================================
# EBP STRATEGY LOGIC (PANDAS)
# ============================================================

def detect_bullish_ebp(df: pd.DataFrame) -> pd.Series:
    prev_open = df['open'].shift(1)
    prev_low  = df['low'].shift(1)
    
    sweep = df['low'] < prev_low
    close_above_prior_open = df['close'] > prev_open
    
    return sweep & close_above_prior_open

def detect_bearish_ebp(df: pd.DataFrame) -> pd.Series:
    prev_open = df['open'].shift(1)
    prev_high = df['high'].shift(1)
    
    sweep = df['high'] > prev_high
    close_below_prior_open = df['close'] < prev_open
    
    return sweep & close_below_prior_open

def check_symbol_ebp(symbol):
    df = get_candles_df(symbol, SIGNAL_BAR, limit=50)
    if len(df) < 10:
        return None

    df['bullish_ebp'] = detect_bullish_ebp(df)
    df['bearish_ebp'] = detect_bearish_ebp(df)

    last_row = df.iloc[-1]

    if last_row['bullish_ebp']:
        return {
            "symbol": symbol,
            "direction": "long",
            "close": last_row['close'],
            "low": last_row['low'],
            "high": last_row['high'],
            "ts": int(last_row.name.timestamp() * 1000)
        }
    elif last_row['bearish_ebp']:
        return {
            "symbol": symbol,
            "direction": "short",
            "close": last_row['close'],
            "low": last_row['low'],
            "high": last_row['high'],
            "ts": int(last_row.name.timestamp() * 1000)
        }
    
    return None

# ============================================================
# SIZING & ORDERS (MINIMUM 12 USDT MARGE)
# ============================================================

def calculate_size(symbol, entry_price, available, stop_distance):
    meta = instrument(symbol)
    contract_value = fnum(meta.get("contractValue"))
    min_size = fnum(meta.get("minSize"))
    lot_size = fnum(meta.get("lotSize"))

    if min(contract_value, min_size, lot_size, entry_price, stop_distance) <= 0:
        return None

    notional = MIN_MARGIN_USDT * LEVERAGE
    max_notional = available * 0.88 * LEVERAGE
    if notional > max_notional:
        return None

    contracts = floor_step(
        notional / (contract_value * entry_price),
        lot_size,
    )
    
    if contracts < min_size:
        return None

    final_notional = contracts * contract_value * entry_price
    return {
        "contracts": contracts,
        "notional": final_notional,
    }

def open_position(signal):
    symbol = signal["symbol"]
    direction = signal["direction"]
    live = last_price(symbol)
    available = account()

    if direction == "long":
        stop = signal["low"] * 0.998
        stop_dist = live - stop
        if stop_dist <= 0:
            stop_dist = live * 0.008
            stop = live - stop_dist
        tp = live + stop_dist * FIXED_RR
        side = "buy"
    else:
        stop = signal["high"] * 1.002
        stop_dist = stop - live
        if stop_dist <= 0:
            stop_dist = live * 0.008
            stop = live + stop_dist
        tp = live - stop_dist * FIXED_RR
        side = "sell"

    sizing = calculate_size(symbol, live, available, stop_dist)
    if not sizing:
        return False

    tg(
        f"🚨 EBP SIGNAL DETECTED!\n"
        f"Coin: {symbol} | Richting: {direction.upper()}\n"
        f"Entry: {fmt_price(live)} | SL: {fmt_price(stop)} | TP: {fmt_price(tp)}\n"
        f"Marge: ~{MIN_MARGIN_USDT} USDT (Notional: {sizing['notional']:.2f})"
    )

    record = {
        "entry_ts": now_ms(),
        "entry_price": live,
        "direction": direction,
        "stop": stop,
        "tp": tp,
        "contracts": sizing["contracts"],
        "notional": sizing["notional"],
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
        "clientOrderId": (f"jjebp{int(time.time())}{symbol.split('-')[0]}")[:32],
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

def manage_positions():
    managed = list(state.get("managed", {}).items())
    for symbol, managed_pos in managed:
        live = last_price(symbol)
        direction = managed_pos["direction"]
        stop = fnum(managed_pos["stop"])
        tp = fnum(managed_pos["tp"])

        hit_stop = (direction == "long" and live <= stop) or (direction == "short" and live >= stop)
        hit_tp = (direction == "long" and live >= tp) or (direction == "short" and live <= tp)

        if hit_stop or hit_tp:
            reason = "TP REACHED 🎯" if hit_tp else "STOP LOSS 🛑"
            tg(f"🔒 CLOSE {symbol} · Reden: {reason} · Prijs: {fmt_price(live)}")
            
            with state_lock:
                state["managed"].pop(symbol, None)
            save_state()

# ============================================================
# MAIN LOOP
# ============================================================

def scan_once():
    manage_positions()

    active = len(state.get("managed", {}))
    if active >= MAX_ACTIVE_POSITIONS:
        return

    symbols = valid_symbols()
    for symbol in symbols:
        if symbol in state.get("managed", {}):
            continue

        try:
            signal = check_symbol_ebp(symbol)
            if signal:
                open_position(signal)
                if len(state.get("managed", {})) >= MAX_ACTIVE_POSITIONS:
                    break
        except Exception as e:
            print(f"Scan error {symbol}: {e}", flush=True)
        
        time.sleep(0.04)

def startup():
    load_state()
    load_instruments()
    symbols = valid_symbols()
    
    mode_str = "PAPER" if PAPER_TRADING else "LIVE"
    
    tg(
        f"🧠 {BOT_NAME} v{VERSION} STARTED\n"
        f"Mode: {mode_str}\n"
        f"Actieve munten: {len(symbols)} | Min Marge: {MIN_MARGIN_USDT} USDT"
    )

def main():
    if Flask is not None:
        def run_health():
            app = Flask(__name__)
            @app.route("/")
            def home():
                return f"{BOT_NAME} v{VERSION} is running", 200
            app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), use_reloader=False)
        threading.Thread(target=run_health, daemon=True).start()

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
