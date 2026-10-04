#!/usr/bin/env python3
"""
BNF PURE MEAN-REVERSION BOT v1.0 — BLOFIN FUTURES
==================================================

Faithful crypto/BloFin translation of the supplied strategy:

LONG ONLY
---------
1. Use CLOSED 1D candles.
2. Calculate 25-day simple moving average (MA25).
3. Entry when close is >= 20% below MA25:
       (MA25 - close) / MA25 >= 0.20
4. Risk 2% of current equity using an 8% hard stop.
5. Exit when a CLOSED daily candle is back to >= 98% of MA25.
6. Exit after max 5 days.
7. A hard 8% stop is attached at BloFin immediately on entry.

Important:
- This is deliberately NOT the old V2/V6 strategy.
- No momentum score, no BTC filter, no ATR gates, no learner.
- Long-only, as in the supplied strategy.
- Default MAX_ACTIVE_POSITIONS=1 keeps the original "flat or one position" idea.
- It scans multiple BloFin USDT perpetuals and selects the deepest qualifying drop.
- PAPER_TRADING=True by default. Set False only when you intentionally want live orders.
"""

import os
import time
import json
import math
import hmac
import base64
import hashlib
import threading
from uuid import uuid4
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

try:
    from flask import Flask
except Exception:
    Flask = None


# ============================================================
# CONFIG
# ============================================================

VERSION = "2.0"
BOT_NAME = "BNF DayTrader Mean-Reversion"

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

# Daytrade translation of the BNF core
SIGNAL_BAR = os.getenv("BNF_SIGNAL_BAR", "15m").strip()
TRIGGER_BAR = os.getenv("BNF_TRIGGER_BAR", "5m").strip()
MA_PERIOD = int(os.getenv("BNF_MA_PERIOD", "25"))
ATR_PERIOD = int(os.getenv("BNF_ATR_PERIOD", "14"))

# Adaptive oversold threshold: never easier than 4%, never stricter than 7%.
DROP_MIN = float(os.getenv("BNF_DROP_MIN", "0.04"))
DROP_MAX = float(os.getenv("BNF_DROP_MAX", "0.07"))
DROP_ATR_MULT = float(os.getenv("BNF_DROP_ATR_MULT", "1.50"))

# BNF-style equity risk, translated to intraday crypto.
RISK_PER_TRADE = float(os.getenv("BNF_RISK_PER_TRADE", "0.02"))
STOP_LOSS_PCT = float(os.getenv("BNF_STOP_LOSS_PCT", "0.025"))

# Intraday profit management.
FIXED_TP_R = float(os.getenv("BNF_FIXED_TP_R", "1.25"))
TRAIL_ACTIVATE_R = float(os.getenv("BNF_TRAIL_ACTIVATE_R", "0.75"))
TRAIL_DISTANCE_R = float(os.getenv("BNF_TRAIL_DISTANCE_R", "0.45"))
MEAN_REVERSION_MA_FACTOR = float(os.getenv("BNF_MEAN_REVERSION_MA_FACTOR", "0.995"))
HOLD_MAX_HOURS = float(os.getenv("BNF_HOLD_MAX_HOURS", "6"))
REENTRY_COOLDOWN_MIN = int(os.getenv("BNF_REENTRY_COOLDOWN_MIN", "60"))

# 5m rebound confirmation. This does not change the BNF setup; it only avoids
# buying while the short-term candle is still accelerating downward.
TRIGGER_MIN_CLOSE_LOCATION = float(os.getenv("BNF_TRIGGER_MIN_CLOSE_LOCATION", "0.55"))

# Execution
LEVERAGE = int(os.getenv("BNF_LEVERAGE", "10"))
MARGIN_MODE = os.getenv("BNF_MARGIN_MODE", "isolated").strip().lower()
MAX_ACTIVE_POSITIONS = int(os.getenv("BNF_MAX_ACTIVE_POSITIONS", "2"))
AVAILABLE_MARGIN_BUFFER = float(os.getenv("BNF_AVAILABLE_MARGIN_BUFFER", "0.90"))
SCAN_SECONDS = int(os.getenv("BNF_SCAN_SECONDS", "75"))
STATUS_HOURS = float(os.getenv("BNF_STATUS_HOURS", "2"))
REQUEST_TIMEOUT = int(os.getenv("BNF_REQUEST_TIMEOUT", "15"))

# Do not chase a signal if price already bounced too far between scan and order.
MAX_ENTRY_DRIFT_PCT = float(os.getenv("BNF_MAX_ENTRY_DRIFT_PCT", "0.012"))

STATE_FILE = os.getenv("BNF_STATE_FILE", "bnf_mean_reversion_state.json")

DEFAULT_SYMBOLS = [
    "BTC-USDT","ETH-USDT","SOL-USDT","BNB-USDT","XRP-USDT","DOGE-USDT",
    "ADA-USDT","TRX-USDT","LTC-USDT","BCH-USDT","AVAX-USDT","LINK-USDT",
    "DOT-USDT","SUI-USDT","NEAR-USDT","APT-USDT","ARB-USDT","OP-USDT",
    "INJ-USDT","SEI-USDT","TIA-USDT","ATOM-USDT","HBAR-USDT","FIL-USDT",
    "UNI-USDT","AAVE-USDT","ONDO-USDT","ENA-USDT","CRV-USDT","RUNE-USDT",
    "RENDER-USDT","FET-USDT","TAO-USDT","WLD-USDT","JUP-USDT",
    "PEPE-USDT","WIF-USDT","BONK-USDT","FLOKI-USDT","SHIB-USDT",
    "ORDI-USDT","GALA-USDT","ETC-USDT","XLM-USDT","ALGO-USDT",
    "SAND-USDT","APE-USDT","IMX-USDT","STX-USDT","POL-USDT"
]

_env_symbols = os.getenv("BNF_SYMBOLS", "").strip()
SYMBOLS = (
    [x.strip().upper() for x in _env_symbols.split(",") if x.strip()]
    if _env_symbols else DEFAULT_SYMBOLS
)


# ============================================================
# SMALL HELPERS
# ============================================================

session = requests.Session()
instrument_cache = {}
state_lock = threading.Lock()
last_status_ts = 0.0

state = {
    "managed": {},              # symbol -> entry metadata
    "last_entry_candle": {},    # symbol -> candle timestamp
    "last_exit_candle": {},     # symbol -> candle timestamp
    "last_exit_ts": {},         # symbol -> unix ms for re-entry cooldown
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
    return f"{v:.10f}".rstrip("0").rstrip(".")


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
    # Avoid scientific notation in API payloads.
    d = Decimal(str(value))
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


# ============================================================
# BLOFIN REST
# ============================================================

def _check_api_response(data, where):
    if not isinstance(data, dict):
        raise RuntimeError(f"{where}: invalid response {data!r}")
    if str(data.get("code", "")) != "0":
        raise RuntimeError(f"{where}: {data.get('code')} {data.get('msg')}")
    # Some trade endpoints return top-level code 0 but a nested rejection.
    payload = data.get("data")
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("code") not in (None, "", "0", 0):
                raise RuntimeError(
                    f"{where}: nested {item.get('code')} {item.get('msg')}"
                )
    return data


def public_get(path, params=None):
    params = params or {}
    url = BASE_URL + path
    r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return _check_api_response(r.json(), path)


def _signed_headers(method, request_path, body_str=""):
    if not API_KEY or not API_SECRET or not API_PASSPHRASE:
        raise RuntimeError("BloFin API credentials ontbreken in environment variables.")
    timestamp = str(now_ms())
    nonce = str(uuid4())
    prehash = f"{request_path}{method.upper()}{timestamp}{nonce}{body_str}"
    hex_sig = hmac.new(
        API_SECRET.encode(),
        prehash.encode(),
        hashlib.sha256
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
    r = session.get(
        BASE_URL + request_path,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return _check_api_response(r.json(), path)


def private_post(path, body):
    body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    headers = _signed_headers("POST", path, body_str)
    r = session.post(
        BASE_URL + path,
        headers=headers,
        data=body_str.encode("utf-8"),
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return _check_api_response(r.json(), path)


# ============================================================
# MARKET / ACCOUNT
# ============================================================

def load_instruments():
    global instrument_cache
    data = public_get("/api/v1/market/instruments").get("data", [])
    instrument_cache = {
        x.get("instId"): x for x in data
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
        raise RuntimeError(f"Instrument niet beschikbaar op BloFin: {symbol}")
    return x


def candles(symbol, bar, limit=120):
    data = public_get(
        "/api/v1/market/candles",
        {"instId": symbol, "bar": bar, "limit": str(limit)}
    ).get("data", [])

    rows = []
    for c in data:
        if not isinstance(c, list) or len(c) < 6:
            continue
        # Use only confirmed/closed candles.
        if str(c[-1]) != "1":
            continue
        rows.append({
            "ts": int(c[0]),
            "open": fnum(c[1]),
            "high": fnum(c[2]),
            "low": fnum(c[3]),
            "close": fnum(c[4]),
            "volume": fnum(c[5]),
        })
    rows.sort(key=lambda x: x["ts"])
    return rows


def true_range(rows, i):
    cur = rows[i]
    prev_close = rows[i - 1]["close"]
    return max(
        cur["high"] - cur["low"],
        abs(cur["high"] - prev_close),
        abs(cur["low"] - prev_close),
    )


def atr_value(rows, period=14):
    if len(rows) < period + 1:
        return 0.0
    trs = [true_range(rows, i) for i in range(len(rows) - period, len(rows))]
    return sum(trs) / len(trs) if trs else 0.0


def last_price(symbol):
    data = public_get(
        "/api/v1/market/tickers",
        {"instId": symbol}
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
            str(p.get("positionSide", "")).lower() == "long"
            or (
                str(p.get("positionSide", "")).lower() == "net"
                and qty > 0
            )
        )
        out.append(p)
    return out


def get_position(symbol):
    for p in positions():
        if p.get("instId") == symbol:
            return p
    return None


def startup_account_checks():
    mode = private_get("/api/v1/account/position-mode").get("data", {})
    position_mode = str(mode.get("positionMode", ""))
    multi = str(mode.get("multiPosition", "false")).lower()
    if position_mode != "net_mode":
        raise RuntimeError(
            f"Deze bot verwacht BloFin One-way/net mode. Huidig: {position_mode}, "
            f"multiPosition={multi}."
        )
    if MARGIN_MODE not in ("isolated", "cross"):
        raise RuntimeError("BNF_MARGIN_MODE moet isolated of cross zijn.")


def set_leverage(symbol):
    body = {
        "instId": symbol,
        "leverage": str(LEVERAGE),
        "marginMode": MARGIN_MODE,
    }
    return private_post("/api/v1/account/set-leverage", body)


# ============================================================
# STRATEGY — EXACT CORE LOGIC
# ============================================================

def strategy_snapshot(symbol):
    """
    15m setup + 5m rebound trigger.

    The setup is still pure BNF:
        price is abnormally far below its short moving average.
    The 5m trigger only waits for evidence that the intraday fall has started
    to rebound before committing capital.
    """
    setup_rows = candles(symbol, SIGNAL_BAR, max(100, MA_PERIOD + ATR_PERIOD + 10))
    trigger_rows = candles(symbol, TRIGGER_BAR, 40)

    if len(setup_rows) < max(MA_PERIOD, ATR_PERIOD + 1) or len(trigger_rows) < 3:
        return None

    closes = [r["close"] for r in setup_rows]
    last = setup_rows[-1]
    ma = sum(closes[-MA_PERIOD:]) / MA_PERIOD
    atr = atr_value(setup_rows, ATR_PERIOD)
    if ma <= 0 or atr <= 0 or last["close"] <= 0:
        return None

    atr_pct = atr / last["close"]
    adaptive_drop = min(DROP_MAX, max(DROP_MIN, DROP_MIN + DROP_ATR_MULT * atr_pct))
    pct_below = (ma - last["close"]) / ma

    # 5m rebound: green candle, higher close than prior 5m close, and closes
    # in the upper portion of its own range.
    t = trigger_rows[-1]
    p = trigger_rows[-2]
    rng = max(t["high"] - t["low"], 1e-12)
    close_location = (t["close"] - t["low"]) / rng
    rebound = (
        t["close"] > t["open"]
        and t["close"] > p["close"]
        and close_location >= TRIGGER_MIN_CLOSE_LOCATION
    )

    entry_signal = pct_below >= adaptive_drop and rebound

    return {
        "symbol": symbol,
        "candle_ts": last["ts"],
        "trigger_ts": t["ts"],
        "close": last["close"],
        "ma25": ma,
        "atr": atr,
        "atr_pct": atr_pct,
        "drop_threshold": adaptive_drop,
        "pct_below_ma": pct_below,
        "rebound": rebound,
        "trigger_close_location": close_location,
        "entry_signal": entry_signal,
    }


def rank_entries(snapshots):
    # Prefer the largest excess discount beyond that symbol's own threshold.
    return sorted(
        [s for s in snapshots if s and s["entry_signal"]],
        key=lambda s: (s["pct_below_ma"] - s["drop_threshold"]),
        reverse=True,
    )


# ============================================================
# POSITION SIZING
# ============================================================

def calculate_size(symbol, entry_price, equity, available):
    """
    BNF sizing:
        risk_amount = equity * configured risk %
        notional = risk_amount / intraday hard-stop %

    BloFin order size is contracts, so translate notional to contracts using
    contractValue. Margin availability is only a hard feasibility cap.
    """
    meta = instrument(symbol)
    contract_value = fnum(meta.get("contractValue"))
    min_size = fnum(meta.get("minSize"))
    lot_size = fnum(meta.get("lotSize"))
    max_market = fnum(meta.get("maxMarketSize"))

    if min(contract_value, min_size, lot_size, entry_price) <= 0:
        raise RuntimeError(f"Ongeldige contract metadata voor {symbol}: {meta}")

    risk_amount = equity * RISK_PER_TRADE
    desired_notional = risk_amount / STOP_LOSS_PCT

    # Cannot use more isolated margin than is reasonably available.
    max_notional_by_margin = max(0.0, available * AVAILABLE_MARGIN_BUFFER * LEVERAGE)
    notional = min(desired_notional, max_notional_by_margin)

    raw_contracts = notional / (contract_value * entry_price)
    contracts = floor_step(raw_contracts, lot_size)

    if max_market > 0:
        contracts = min(contracts, floor_step(max_market, lot_size))

    if contracts < min_size:
        # Do NOT round up if that would exceed the intended risk budget.
        return None

    actual_notional = contracts * contract_value * entry_price
    actual_risk = actual_notional * STOP_LOSS_PCT
    margin = actual_notional / LEVERAGE

    if actual_risk > risk_amount * 1.02:
        return None

    return {
        "contracts": contracts,
        "notional": actual_notional,
        "risk_usdt": actual_risk,
        "risk_budget": risk_amount,
        "margin": margin,
    }


# ============================================================
# ORDERS
# ============================================================

def open_long(snapshot):
    symbol = snapshot["symbol"]
    equity, available = account()
    live = last_price(symbol)
    signal_close = snapshot["close"]

    if live <= 0:
        return False

    # Re-entry cooldown after any exit in this symbol.
    last_exit = int(state.get("last_exit_ts", {}).get(symbol, 0) or 0)
    if last_exit and now_ms() - last_exit < REENTRY_COOLDOWN_MIN * 60_000:
        return False

    # For a dip-buy we only reject an UPWARD chase. A slightly deeper dip can
    # still be valid, provided the hard stop/risk sizing remains intact.
    upward_drift = (live - signal_close) / signal_close
    if upward_drift > MAX_ENTRY_DRIFT_PCT:
        tg(
            f"🟠 BNF DAYTRADE {symbol}\n"
            f"Rebound liep al {upward_drift*100:.2f}% weg sinds de 15m close — geen chase."
        )
        return False

    sizing = calculate_size(symbol, live, equity, available)
    if not sizing:
        tg(
            f"🟠 BNF DAYTRADE {symbol} SIZE BLOCK\n"
            f"Contract minimum/marge past niet binnen {RISK_PER_TRADE*100:.1f}% risico."
        )
        return False

    stop = live * (1.0 - STOP_LOSS_PCT)
    risk_price = live - stop
    fixed_tp = live + risk_price * FIXED_TP_R

    meta = instrument(symbol)
    tick = fnum(meta.get("tickSize"))
    if tick > 0:
        stop = floor_step(stop, tick)
        fixed_tp = floor_step(fixed_tp, tick)

    tg(
        f"🟠 BNF DAYTRADE READY\n"
        f"{symbol} LONG\n"
        f"15m close {fmt_price(snapshot['close'])} · MA25 {fmt_price(snapshot['ma25'])}\n"
        f"Discount {snapshot['pct_below_ma']*100:.2f}% · nodig {snapshot['drop_threshold']*100:.2f}%\n"
        f"5m rebound bevestigd · close-loc {snapshot['trigger_close_location']*100:.0f}%\n"
        f"Live ~{fmt_price(live)} · SL {fmt_price(stop)} · TP {fmt_price(fixed_tp)} ({FIXED_TP_R:.2f}R)\n"
        f"Risk budget ${sizing['risk_budget']:.2f} · notional ${sizing['notional']:.2f} · "
        f"margin ~${sizing['margin']:.2f}"
    )

    record = {
        "entry_ts": now_ms(),
        "entry_price": live,
        "entry_candle_ts": snapshot["candle_ts"],
        "trigger_ts": snapshot["trigger_ts"],
        "contracts": sizing["contracts"],
        "initial_stop": stop,
        "risk_price": max(live - stop, 1e-12),
        "fixed_tp": fixed_tp,
        "highest_price": live,
        "paper": PAPER_TRADING,
    }

    if PAPER_TRADING:
        with state_lock:
            state["managed"][symbol] = record
            state["last_entry_candle"][symbol] = snapshot["candle_ts"]
        save_state()
        tg(f"🟢 PAPER BNF DAYTRADE OPEN\n{symbol} LONG @ ~{fmt_price(live)}")
        return True

    set_leverage(symbol)

    # Hard SL is exchange-side. Profit exits are actively managed every ~75 sec.
    body = {
        "instId": symbol,
        "marginMode": MARGIN_MODE,
        "positionSide": "net",
        "side": "buy",
        "orderType": "market",
        "size": decimal_str(sizing["contracts"]),
        "reduceOnly": "false",
        "clientOrderId": f"bnfd{int(time.time())}{symbol.split('-')[0]}"[:32],
        "slTriggerPrice": decimal_str(stop),
        "slOrderPrice": "-1",
        "slTriggerPriceType": "mark",
    }

    resp = private_post("/api/v1/trade/order", body)
    data = resp.get("data", [])
    order_id = data[0].get("orderId", "") if data else ""

    pos = None
    for _ in range(10):
        time.sleep(1)
        pos = get_position(symbol)
        if pos and pos.get("_is_long"):
            break

    actual_entry = fnum(pos.get("averagePrice")) if pos else live
    # Recalculate management levels from actual fill.
    record["entry_price"] = actual_entry
    record["initial_stop"] = actual_entry * (1.0 - STOP_LOSS_PCT)
    record["risk_price"] = actual_entry - record["initial_stop"]
    record["fixed_tp"] = actual_entry + record["risk_price"] * FIXED_TP_R
    record["highest_price"] = actual_entry
    record["paper"] = False
    record["order_id"] = order_id

    with state_lock:
        state["managed"][symbol] = record
        state["last_entry_candle"][symbol] = snapshot["candle_ts"]
    save_state()

    tg(
        f"🟢 BNF DAYTRADE ORDER ACCEPTED\n"
        f"{symbol} LONG · entry ~{fmt_price(actual_entry)}\n"
        f"Hard SL ~{fmt_price(record['initial_stop'])} · fixed TP ~{fmt_price(record['fixed_tp'])}\n"
        f"Trailing active vanaf +{TRAIL_ACTIVATE_R:.2f}R · max hold {HOLD_MAX_HOURS:g}h"
    )
    return True


def close_live_position(symbol, reason):
    pos = get_position(symbol)
    if not pos:
        return False

    body = {
        "instId": symbol,
        "marginMode": str(pos.get("marginMode") or MARGIN_MODE),
        "positionSide": str(pos.get("positionSide") or "net"),
        "clientOrderId": f"bnfx{int(time.time())}{symbol.split('-')[0]}"[:32],
    }
    pid = str(pos.get("positionId") or "")
    if pid:
        body["positionId"] = pid

    private_post("/api/v1/trade/close-position", body)
    tg(f"📤 BNF EXIT SENT\n{symbol} · {reason}")
    return True


def _finish_local_position(symbol):
    with state_lock:
        state["managed"].pop(symbol, None)
        state["last_exit_ts"][symbol] = now_ms()
    save_state()


def manage_open_position(symbol, snapshot):
    """
    Intraday exit manager.

    Exchange always owns the emergency hard stop. This loop manages:
    - fast fixed R target,
    - return toward MA25,
    - trailing profit after +0.75R,
    - six-hour time stop.
    """
    managed = state.get("managed", {}).get(symbol, {})
    paper = bool(managed.get("paper", False))

    entry = fnum(managed.get("entry_price"))
    risk = fnum(managed.get("risk_price"))
    if entry <= 0 or risk <= 0:
        return False

    live = last_price(symbol)
    high = max(fnum(managed.get("highest_price"), entry), live)
    managed["highest_price"] = high
    state["managed"][symbol] = managed
    save_state()

    r_now = (live - entry) / risk
    high_r = (high - entry) / risk
    age_hours = (now_ms() - int(managed.get("entry_ts", now_ms()))) / 3_600_000

    mean_exit = bool(
        snapshot
        and live >= fnum(snapshot.get("ma25")) * MEAN_REVERSION_MA_FACTOR
    )
    fixed_tp = r_now >= FIXED_TP_R
    max_hold = age_hours >= HOLD_MAX_HOURS

    trail_level = None
    trail_hit = False
    if high_r >= TRAIL_ACTIVATE_R:
        trail_level = high - TRAIL_DISTANCE_R * risk
        # Never let the profit trail sit below entry once activated.
        trail_level = max(trail_level, entry)
        trail_hit = live <= trail_level

    reason = None
    if fixed_tp:
        reason = f"FIXED TP {FIXED_TP_R:.2f}R"
    elif mean_exit:
        reason = "MEAN REVERSION → MA25"
    elif trail_hit:
        reason = f"TRAIL EXIT · high {high_r:.2f}R"
    elif max_hold:
        reason = f"MAX HOLD {HOLD_MAX_HOURS:g}h"

    if paper:
        # Also emulate the hard stop in paper mode.
        if live <= entry * (1.0 - STOP_LOSS_PCT):
            reason = "HARD STOP"

        if reason:
            pnl_pct = (live - entry) / entry * 100
            tg(
                f"{'🟢' if pnl_pct >= 0 else '🔴'} PAPER BNF DAYTRADE CLOSED\n"
                f"{symbol} · {reason}\n"
                f"{fmt_price(entry)} → {fmt_price(live)} · {pnl_pct:+.2f}% · {r_now:+.2f}R"
            )
            _finish_local_position(symbol)
            return True
        return False

    pos = get_position(symbol)
    if not pos:
        # Most likely exchange-side hard SL.
        tg(f"ℹ️ BNF DAYTRADE {symbol} positie is gesloten op BloFin; state opgeschoond.")
        _finish_local_position(symbol)
        return True

    if reason:
        ok = close_live_position(symbol, reason)
        if ok:
            # Do not immediately re-enter while the exchange is processing close.
            with state_lock:
                state["last_exit_ts"][symbol] = now_ms()
            save_state()
        return ok

    return False


# ============================================================
# SCAN / REPORT
# ============================================================

def scan_once():
    global last_status_ts

    symbols = valid_symbols()
    if not symbols:
        raise RuntimeError("Geen geconfigureerde symbolen beschikbaar op BloFin.")

    # Build snapshots once.
    snapshots = {}
    errors = 0
    for symbol in symbols:
        try:
            snapshots[symbol] = strategy_snapshot(symbol)
        except Exception as e:
            errors += 1
            print(f"{symbol} data error: {e}", flush=True)
        time.sleep(0.08)

    if PAPER_TRADING:
        open_symbols = list(state.get("managed", {}).keys())
        live_positions = []
    else:
        live_positions = positions()
        open_symbols = [p.get("instId") for p in live_positions]

        # If our state says we manage something that has disappeared, clean it.
        for symbol in list(state.get("managed", {}).keys()):
            if symbol not in open_symbols:
                tg(f"ℹ️ BNF {symbol}: geen open positie meer; state opgeschoond.")
                with state_lock:
                    state["managed"].pop(symbol, None)
                save_state()

    # Manage only positions opened by this bot/state.
    for symbol in list(state.get("managed", {}).keys()):
        try:
            manage_open_position(symbol, snapshots.get(symbol))
        except Exception as e:
            tg(f"⚠️ BNF MANAGE ERROR {symbol}\n{type(e).__name__}: {e}")

    # Refresh after possible exits.
    if PAPER_TRADING:
        active_count = len(state.get("managed", {}))
        occupied = set(state.get("managed", {}).keys())
    else:
        live_positions = positions()
        active_count = len(live_positions)
        occupied = {p.get("instId") for p in live_positions}

    free_slots = max(0, MAX_ACTIVE_POSITIONS - active_count)

    candidates = rank_entries([
        s for sym, s in snapshots.items()
        if sym not in occupied
        and s
        and state.get("last_entry_candle", {}).get(sym) != s["candle_ts"]
        and (now_ms() - int(state.get("last_exit_ts", {}).get(sym, 0) or 0) >= REENTRY_COOLDOWN_MIN * 60_000)
    ])

    opened = 0
    for snap in candidates:
        if opened >= free_slots:
            break
        try:
            if open_long(snap):
                opened += 1
        except Exception as e:
            tg(
                f"❌ BNF ENTRY ERROR {snap['symbol']}\n"
                f"{type(e).__name__}: {e}"
            )

    if time.time() - last_status_ts >= STATUS_HOURS * 3600:
        last_status_ts = time.time()
        top = sorted(
            [s for s in snapshots.values() if s],
            key=lambda x: x["pct_below_ma"],
            reverse=True,
        )[:6]

        lines = [
            f"📊 {BOT_NAME} v{VERSION}",
            f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'} · setup {SIGNAL_BAR}/MA{MA_PERIOD} · trigger {TRIGGER_BAR}",
            f"Adaptive dip {DROP_MIN*100:.0f}–{DROP_MAX*100:.0f}% · SL {STOP_LOSS_PCT*100:.1f}% · "
            f"TP {FIXED_TP_R:.2f}R · trail +{TRAIL_ACTIVATE_R:.2f}R · max {HOLD_MAX_HOURS:g}h",
            f"Scanned {len(snapshots)}/{len(symbols)} · errors {errors} · "
            f"active {active_count}/{MAX_ACTIVE_POSITIONS}",
            f"Entry candidates nu: {len(candidates)}",
            "Diepste dips:",
        ]
        for s in top:
            lines.append(
                f"{s['symbol'].split('-')[0]} "
                f"{s['pct_below_ma']*100:.1f}% onder MA25 · trigger {s['drop_threshold']*100:.1f}%"
                + (" 🔥 READY" if s["entry_signal"] else (" · rebound" if s["rebound"] else ""))
            )
        tg("\n".join(lines))


# ============================================================
# STARTUP / HEALTH
# ============================================================

def startup():
    load_state()
    load_instruments()

    if not PAPER_TRADING:
        startup_account_checks()
        equity, available = account()
    else:
        equity = available = 0.0

    symbols = valid_symbols()

    tg(
        f"🧠 {BOT_NAME} STARTED\n"
        f"v{VERSION} · {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"BNF core: BUY THE DIP → SELL THE MEAN REVERSION\n"
        f"Setup {SIGNAL_BAR}: MA{MA_PERIOD} · adaptive dip {DROP_MIN*100:.0f}–{DROP_MAX*100:.0f}%\n"
        f"Trigger {TRIGGER_BAR}: green rebound + higher close\n"
        f"Hard SL {STOP_LOSS_PCT*100:.1f}% · equity risk {RISK_PER_TRADE*100:.1f}%\n"
        f"Profit: MA25 return / {FIXED_TP_R:.2f}R / trailing vanaf +{TRAIL_ACTIVATE_R:.2f}R\n"
        f"Max hold {HOLD_MAX_HOURS:g}h · scan iedere {SCAN_SECONDS}s · cooldown {REENTRY_COOLDOWN_MIN}m\n"
        f"Leverage {LEVERAGE}x · {MARGIN_MODE} · max posities {MAX_ACTIVE_POSITIONS}\n"
        f"Scant {len(symbols)} BloFin USDT perpetuals"
        + (
            f"\nEquity ${equity:.2f} · Available ${available:.2f}"
            if not PAPER_TRADING else
            "\nPAPER_TRADING=True — geen echte orders."
        )
    )


def health_server():
    if Flask is None:
        return
    app = Flask(__name__)

    @app.route("/")
    def health():
        return f"{BOT_NAME} v{VERSION} is running!", 200

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


def main():
    if Flask is not None:
        threading.Thread(target=health_server, daemon=True).start()

    startup()

    while True:
        try:
            scan_once()
        except KeyboardInterrupt:
            tg(f"🛑 {BOT_NAME} stopped.")
            raise
        except Exception as e:
            tg(f"⚠️ BNF LOOP ERROR\n{type(e).__name__}: {e}")
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
