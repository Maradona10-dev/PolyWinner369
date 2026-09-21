import os
import time
import math
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone

import ccxt
import numpy as np
import pandas as pd
import requests

# ============================================================
# BLOFIN V6.4 - LIVE FUTURES BOT
# ============================================================
# Main changes vs V6.3:
# - Detects fully closed positions from BloFin position history.
# - Sends Telegram PnL notification immediately after a close is detected.
# - Reports realized PnL, fee, estimated net PnL, return, close reason and duration.
# - Maintains session/day win/loss and net PnL counters.
# - Seeds existing history at startup so old trades are not reported as new.
# - Uses BloFin native futures endpoints through CCXT implicit API.
# - Correct contract sizing using contractValue/lotSize/minSize.
# - Checks REAL available futures USDT before every entry.
# - Uses native attached TP/SL on the opening order.
# - Verifies TP/SL through BloFin pending TPSL endpoint.
# - Repairs existing unprotected positions at startup.
# - If BloFin returns 152012, bot stops opening new positions.
# - No artificial 1-hour trade cooldown.
# ============================================================

# ----------------------------
# Environment
# ----------------------------
API_KEY = os.getenv("EXCHANGE_API_KEY", "").strip()
API_SECRET = os.getenv("EXCHANGE_SECRET", "").strip()
API_PASSWORD = os.getenv("EXCHANGE_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"
LIVE_CONFIRM = os.getenv("LIVE_CONFIRM", "")

# IMPORTANT: do not invent this value.
# If your API key is bound to a broker and BloFin requires brokerId,
# put the brokerId supplied by BloFin here.
BROKER_ID = os.getenv("BLOFIN_BROKER_ID", "").strip()

# ----------------------------
# Strategy / risk
# ----------------------------
TIMEFRAME_SIGNAL = "15m"
TIMEFRAME_CONFIRM = "5m"
TIMEFRAME_TREND = "1h"
CANDLE_LIMIT = 240

SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "45"))
SYMBOL_COOLDOWN_MIN = int(os.getenv("SYMBOL_COOLDOWN_MIN", "15"))

MAX_ACTIVE_POSITIONS = int(os.getenv("MAX_ACTIVE_POSITIONS", "2"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "55"))

LEVERAGE = int(os.getenv("LEVERAGE", "10"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")
BASE_MARGIN_USDT = float(os.getenv("BASE_MARGIN_USDT", "10"))
MAX_MARGIN_USDT = float(os.getenv("MAX_MARGIN_USDT", "12"))
MAX_MARGIN_EQUITY_PCT = float(os.getenv("MAX_MARGIN_EQUITY_PCT", "0.20"))
MAX_RISK_EQUITY_PCT = float(os.getenv("MAX_RISK_EQUITY_PCT", "0.015"))
AVAILABLE_MARGIN_BUFFER = float(os.getenv("AVAILABLE_MARGIN_BUFFER", "0.90"))

ATR_PERIOD = 14
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.25"))
TP_R_MULT = float(os.getenv("TP_R_MULT", "1.80"))

VOLUME_MULT = 1.10
DONCHIAN_LOOKBACK = 12
NEAR_SWEEP_ATR = 0.15

DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "7"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
MIN_EQUITY_USDT = float(os.getenv("MIN_EQUITY_USDT", "30"))

# Optional: allow the bot to repair existing positions at startup.
REPAIR_EXISTING_POSITIONS = os.getenv("REPAIR_EXISTING_POSITIONS", "true").lower() == "true"

SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "TRX/USDT:USDT",
    "TON/USDT:USDT", "LTC/USDT:USDT", "BCH/USDT:USDT", "AVAX/USDT:USDT",
    "LINK/USDT:USDT", "DOT/USDT:USDT", "SUI/USDT:USDT", "NEAR/USDT:USDT",
    "APT/USDT:USDT", "ARB/USDT:USDT", "OP/USDT:USDT", "INJ/USDT:USDT",
    "SEI/USDT:USDT", "TIA/USDT:USDT", "ATOM/USDT:USDT", "HBAR/USDT:USDT",
    "FIL/USDT:USDT", "STX/USDT:USDT", "IMX/USDT:USDT", "STRK/USDT:USDT",
    "POL/USDT:USDT", "UNI/USDT:USDT", "AAVE/USDT:USDT", "LDO/USDT:USDT",
    "ONDO/USDT:USDT", "ENA/USDT:USDT", "CRV/USDT:USDT", "MKR/USDT:USDT",
    "RUNE/USDT:USDT", "RENDER/USDT:USDT", "FET/USDT:USDT", "TAO/USDT:USDT",
    "WLD/USDT:USDT", "JUP/USDT:USDT", "PEPE/USDT:USDT", "WIF/USDT:USDT",
    "BONK/USDT:USDT", "FLOKI/USDT:USDT", "SHIB/USDT:USDT", "ORDI/USDT:USDT",
    "GALA/USDT:USDT", "ETC/USDT:USDT", "XLM/USDT:USDT", "ALGO/USDT:USDT",
    "SAND/USDT:USDT", "APE/USDT:USDT",
]

# ----------------------------
# Exchange
# ----------------------------
if not API_KEY or not API_SECRET or not API_PASSWORD:
    raise RuntimeError("Missing EXCHANGE_API_KEY / EXCHANGE_SECRET / EXCHANGE_PASSWORD")

if not PAPER_TRADING and LIVE_CONFIRM != "YES_I_WANT_LIVE_TRADING":
    raise RuntimeError("LIVE TRADING BLOCKED. Set LIVE_CONFIRM=YES_I_WANT_LIVE_TRADING")

exchange = ccxt.blofin({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "password": API_PASSWORD,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
    },
})

exchange.load_markets()

# Keep only markets that actually exist.
SYMBOLS = [s for s in SYMBOLS if s in exchange.markets]

if not SYMBOLS:
    raise RuntimeError("No configured symbols are available on BloFin.")

# ----------------------------
# Telegram
# ----------------------------
def tg(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception:
        pass

# ----------------------------
# Helpers
# ----------------------------
def now_utc():
    return datetime.now(timezone.utc)


def fmt(x, n=4):
    try:
        return f"{float(x):.{n}f}"
    except Exception:
        return str(x)


def safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def native(method_name, params=None):
    """Call a CCXT implicit BloFin endpoint using snake_case aliases."""
    fn = getattr(exchange, method_name, None)
    if fn is None:
        raise RuntimeError(f"CCXT implicit endpoint missing: {method_name}")
    return fn(params or {})


def native_ok(resp):
    return str(resp.get("code")) == "0"


def response_error(resp):
    if not isinstance(resp, dict):
        return str(resp)
    code = str(resp.get("code", "?"))
    msg = str(resp.get("msg", ""))
    data = resp.get("data")
    if isinstance(data, dict):
        dcode = data.get("code")
        dmsg = data.get("msg")
        if dcode not in (None, "", "0"):
            code = str(dcode)
        if dmsg:
            msg = str(dmsg)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("code")) not in ("0", "None", ""):
                code = str(item.get("code"))
                msg = str(item.get("msg") or msg)
                break
    return f"{code} {msg}".strip()


def blofin_inst_id(symbol):
    return symbol.split("/")[0] + "-USDT"


def get_instrument(symbol):
    inst = blofin_inst_id(symbol)
    resp = native("public_get_market_instruments", {"instId": inst})
    if not native_ok(resp):
        raise RuntimeError(f"Instrument lookup failed {inst}: {response_error(resp)}")
    data = resp.get("data") or []
    if not data:
        raise RuntimeError(f"No instrument metadata for {inst}")
    return data[0]


def floor_step(value, step):
    v = Decimal(str(value))
    s = Decimal(str(step))
    if s <= 0:
        return float(v)
    return float((v / s).to_integral_value(rounding=ROUND_DOWN) * s)


def ceil_step(value, step):
    v = Decimal(str(value))
    s = Decimal(str(step))
    if s <= 0:
        return float(v)
    return float((v / s).to_integral_value(rounding=ROUND_UP) * s)


def round_price(price, tick):
    return floor_step(price, tick)

# ----------------------------
# Account / positions
# ----------------------------
def futures_account():
    resp = native("private_get_account_balance", {"productType": "USDT-FUTURES"})
    if not native_ok(resp):
        raise RuntimeError(f"Balance API failed: {response_error(resp)}")
    data = resp.get("data") or {}
    total_equity = safe_float(data.get("totalEquity"))
    details = data.get("details") or []
    usdt = next((d for d in details if d.get("currency") == "USDT"), {})
    balance = safe_float(usdt.get("balance"))
    available = safe_float(usdt.get("available"))
    available_equity = safe_float(usdt.get("availableEquity"))
    frozen = safe_float(usdt.get("frozen"))
    order_frozen = safe_float(usdt.get("orderFrozen"))
    equity = safe_float(usdt.get("equity"))
    if total_equity <= 0:
        total_equity = equity or balance
    return {
        "total_equity": total_equity,
        "equity": equity or total_equity,
        "balance": balance,
        "available": available,
        "available_equity": available_equity,
        "frozen": frozen,
        "order_frozen": order_frozen,
    }


def get_positions():
    resp = native("private_get_account_positions", {})
    if not native_ok(resp):
        raise RuntimeError(f"Positions API failed: {response_error(resp)}")
    raw = resp.get("data") or []
    result = []
    for p in raw:
        # BloFin can expose empty/zero positions.
        pos_size = safe_float(p.get("positions", p.get("position", p.get("size", 0))))
        if abs(pos_size) <= 0:
            continue
        result.append(p)
    return result


def active_position_count():
    return len(get_positions())


def position_symbol(p):
    inst = p.get("instId", "")
    return inst.replace("-USDT", "/USDT:USDT")


def position_side(p):
    side = str(p.get("positionSide", "net")).lower()
    if side in ("long", "short"):
        return side
    # One-way mode: positive position = long, negative = short.
    pos = safe_float(p.get("positions", p.get("position", p.get("size", 0))))
    return "long" if pos > 0 else "short"


def position_size(p):
    return abs(safe_float(p.get("positions", p.get("position", p.get("size", 0)))))


def position_entry(p):
    return safe_float(p.get("averagePrice", p.get("entryPrice", p.get("avgPrice", 0))))


def position_margin_mode(p):
    return str(p.get("marginMode", MARGIN_MODE)).lower()


def position_id(p):
    return str(p.get("positionId", "") or "")

# ----------------------------
# Market data / indicators
# ----------------------------
def ohlcv_df(symbol, timeframe):
    rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=CANDLE_LIMIT)
    if len(rows) < 60:
        raise RuntimeError(f"Not enough candles for {symbol} {timeframe}")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna().reset_index(drop=True)


def add_indicators(df):
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema200"] = out["close"].ewm(span=200, adjust=False).mean()

    prev_close = out["close"].shift(1)
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - prev_close).abs(),
        (out["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = tr.rolling(ATR_PERIOD).mean()
    out["vol_sma20"] = out["volume"].rolling(20).mean()
    out["donchian_high"] = out["high"].rolling(DONCHIAN_LOOKBACK).max().shift(1)
    out["donchian_low"] = out["low"].rolling(DONCHIAN_LOOKBACK).min().shift(1)
    return out


def evaluate_symbol(symbol):
    df15 = add_indicators(ohlcv_df(symbol, TIMEFRAME_SIGNAL))
    df5 = add_indicators(ohlcv_df(symbol, TIMEFRAME_CONFIRM))
    df1h = add_indicators(ohlcv_df(symbol, TIMEFRAME_TREND))

    a = df15.iloc[-2]   # closed candle
    a_prev = df15.iloc[-3]
    c = df5.iloc[-2]
    c_prev = df5.iloc[-3]
    t = df1h.iloc[-2]

    price = float(a["close"])
    atr = float(a["atr"])
    if not np.isfinite(atr) or atr <= 0:
        return None

    trend_long = t["ema20"] > t["ema50"] > t["ema200"]
    trend_short = t["ema20"] < t["ema50"] < t["ema200"]

    ema_long = a["ema20"] > a["ema50"] and price > a["ema20"]
    ema_short = a["ema20"] < a["ema50"] and price < a["ema20"]

    volume_ok = a["volume"] > (a["vol_sma20"] * VOLUME_MULT)

    breakout_long = price > a["donchian_high"] if pd.notna(a["donchian_high"]) else False
    breakout_short = price < a["donchian_low"] if pd.notna(a["donchian_low"]) else False

    # Liquidity sweep: previous candle pierces a local extreme and closes back through it.
    recent_low = df15["low"].iloc[-DONCHIAN_LOOKBACK-3:-3].min()
    recent_high = df15["high"].iloc[-DONCHIAN_LOOKBACK-3:-3].max()
    sweep_long = (a_prev["low"] < recent_low - NEAR_SWEEP_ATR * atr) and (a["close"] > recent_low)
    sweep_short = (a_prev["high"] > recent_high + NEAR_SWEEP_ATR * atr) and (a["close"] < recent_high)

    confirm_long = c["ema20"] > c["ema50"] and c["close"] > c["ema20"] and c["close"] > c_prev["close"]
    confirm_short = c["ema20"] < c["ema50"] and c["close"] < c["ema20"] and c["close"] < c_prev["close"]

    atr_pct = atr / price
    atr_quality = 0.0008 <= atr_pct <= 0.06

    long_score = 0
    short_score = 0

    long_score += 20 if trend_long else 0
    short_score += 20 if trend_short else 0
    long_score += 12 if ema_long else 0
    short_score += 12 if ema_short else 0
    long_score += 8 if price > a["ema20"] else 0
    short_score += 8 if price < a["ema20"] else 0
    long_score += 10 if volume_ok else 0
    short_score += 10 if volume_ok else 0
    long_score += 12 if breakout_long else 0
    short_score += 12 if breakout_short else 0
    long_score += 8 if sweep_long else 0
    short_score += 8 if sweep_short else 0
    long_score += 15 if confirm_long else 0
    short_score += 15 if confirm_short else 0
    long_score += 5 if atr_quality else 0
    short_score += 5 if atr_quality else 0

    if long_score >= short_score:
        side = "long"
        score = long_score
    else:
        side = "short"
        score = short_score

    if score < MIN_SCORE:
        return None

    return {
        "symbol": symbol,
        "side": side,
        "score": float(score),
        "price": price,
        "atr": atr,
        "trend": "UP" if trend_long else "DOWN" if trend_short else "MIXED",
        "volume": volume_ok,
        "breakout": breakout_long if side == "long" else breakout_short,
        "sweep": sweep_long if side == "long" else sweep_short,
        "confirm": confirm_long if side == "long" else confirm_short,
        "atr_pct": atr_pct,
    }

# ----------------------------
# Risk / sizing
# ----------------------------
def calculate_trade_levels(entry, atr, side, tick):
    risk_distance = atr * ATR_SL_MULT
    if side == "long":
        sl = entry - risk_distance
        tp = entry + risk_distance * TP_R_MULT
    else:
        sl = entry + risk_distance
        tp = entry - risk_distance * TP_R_MULT

    sl = round_price(sl, tick)
    tp = round_price(tp, tick)

    if side == "long" and not (sl < entry < tp):
        return None
    if side == "short" and not (tp < entry < sl):
        return None
    return sl, tp, risk_distance


def calculate_contracts(symbol, entry, sl, equity, available):
    meta = get_instrument(symbol)
    contract_value = safe_float(meta.get("contractValue"))
    min_size = safe_float(meta.get("minSize"))
    lot_size = safe_float(meta.get("lotSize"))
    max_market_size = safe_float(meta.get("maxMarketSize"), 0)

    if contract_value <= 0 or min_size <= 0 or lot_size <= 0:
        raise RuntimeError(f"Bad contract metadata for {symbol}: {meta}")

    # Risk-based notional.
    stop_pct = abs(entry - sl) / entry
    risk_budget = equity * MAX_RISK_EQUITY_PCT
    risk_notional = risk_budget / stop_pct if stop_pct > 0 else 0

    # Margin target + account cap.
    margin_cap = min(BASE_MARGIN_USDT, MAX_MARGIN_USDT, equity * MAX_MARGIN_EQUITY_PCT)
    target_notional = min(risk_notional, margin_cap * LEVERAGE)

    # Never use more than the configured fraction of actual available margin.
    max_affordable_margin = max(0.0, available * AVAILABLE_MARGIN_BUFFER)
    target_notional = min(target_notional, max_affordable_margin * LEVERAGE)

    if target_notional <= 0:
        return None

    # Linear contract: notional ~= contracts * contractValue * price.
    raw_contracts = target_notional / (contract_value * entry)
    contracts = floor_step(raw_contracts, lot_size)

    if contracts < min_size:
        # Try the minimum only if it still fits the margin budget.
        contracts = min_size

    if max_market_size > 0:
        contracts = min(contracts, max_market_size)
        contracts = floor_step(contracts, lot_size)

    if contracts < min_size:
        return None

    notional = contracts * contract_value * entry
    margin = notional / LEVERAGE

    # Final hard safety check against free margin and configured caps.
    if margin > max_affordable_margin + 1e-9:
        contracts = floor_step((max_affordable_margin * LEVERAGE) / (contract_value * entry), lot_size)
        if contracts < min_size:
            return None
        notional = contracts * contract_value * entry
        margin = notional / LEVERAGE

    if margin > MAX_MARGIN_USDT + 1e-9:
        contracts = floor_step((MAX_MARGIN_USDT * LEVERAGE) / (contract_value * entry), lot_size)
        if contracts < min_size:
            return None
        notional = contracts * contract_value * entry
        margin = notional / LEVERAGE

    return {
        "contracts": contracts,
        "contract_value": contract_value,
        "notional": notional,
        "margin": margin,
        "min_size": min_size,
        "lot_size": lot_size,
        "max_market_size": max_market_size,
    }

# ----------------------------
# TP/SL
# ----------------------------
def pending_tpsl(inst_id=None):
    params = {"limit": "100"}
    if inst_id:
        params["instId"] = inst_id
    resp = native("private_get_trade_orders_tpsl_pending", params)
    if not native_ok(resp):
        raise RuntimeError(f"TPSL lookup failed: {response_error(resp)}")
    return resp.get("data") or []


def has_live_protection(p):
    inst = p.get("instId")
    pos_side = str(p.get("positionSide", "net")).lower()
    pid = position_id(p)
    try:
        orders = pending_tpsl(inst)
    except Exception:
        return False, None

    for o in orders:
        if str(o.get("state", "")).lower() != "live":
            continue
        if o.get("instId") != inst:
            continue
        if str(o.get("positionSide", "net")).lower() != pos_side:
            continue
        opid = str(o.get("positionId", "") or "")
        if pid and opid and opid != pid:
            continue
        tp = safe_float(o.get("tpTriggerPrice"), 0)
        sl = safe_float(o.get("slTriggerPrice"), 0)
        if tp > 0 or sl > 0:
            return True, o
    return False, None


def place_tpsl_for_position(p, sl, tp, client_tag="v62"):
    inst = p["instId"]
    side = position_side(p)
    margin_mode = position_margin_mode(p)
    pside = str(p.get("positionSide", "net")).lower()
    if pside not in ("net", "long", "short"):
        pside = "net"

    order_side = "sell" if side == "long" else "buy"
    payload = {
        "instId": inst,
        "marginMode": margin_mode,
        "positionSide": pside,
        "side": order_side,
        "tpTriggerPrice": str(tp),
        "tpOrderPrice": "-1",
        "tpTriggerPriceType": "mark",
        "slTriggerPrice": str(sl),
        "slOrderPrice": "-1",
        "slTriggerPriceType": "mark",
        "size": "-1",
        "reduceOnly": "true",
        "clientOrderId": f"{client_tag}{int(time.time()) % 100000000}",
    }
    pid = position_id(p)
    if pid:
        payload["positionId"] = pid
    if BROKER_ID:
        payload["brokerId"] = BROKER_ID

    resp = native("private_post_trade_order_tpsl", payload)
    if not native_ok(resp):
        return False, response_error(resp)

    time.sleep(0.8)
    ok, found = has_live_protection(p)
    if not ok:
        return False, "TPSL API accepted request but no live protection was confirmed"
    return True, found


def repair_existing_positions():
    positions = get_positions()
    if not positions:
        return True

    tg(f"🛡️ V6.3 STARTUP\nOpen positions: {len(positions)}\nChecking TP/SL protection...")

    all_ok = True
    for p in positions:
        symbol = position_symbol(p)
        side = position_side(p)
        entry = position_entry(p)
        if not entry:
            tg(f"⚠️ {symbol}: cannot determine entry price; protection not changed.")
            all_ok = False
            continue

        protected, existing = has_live_protection(p)
        if protected:
            tg(f"✅ {symbol} {side.upper()}: existing TP/SL confirmed.")
            continue

        try:
            df = add_indicators(ohlcv_df(symbol, TIMEFRAME_SIGNAL))
            atr = float(df.iloc[-2]["atr"])
            meta = get_instrument(symbol)
            tick = safe_float(meta.get("tickSize"), 0.0001)
            levels = calculate_trade_levels(entry, atr, side, tick)
            if not levels:
                tg(f"🚨 {symbol}: could not calculate valid repair TP/SL. NEW TRADES PAUSED.")
                all_ok = False
                continue
            sl, tp, _ = levels

            # Do not place a repair order if a trigger is already on the wrong side of current price.
            last = safe_float(exchange.fetch_ticker(symbol).get("last"))
            if side == "long" and (sl >= last or tp <= last):
                tg(f"🚨 {symbol}: repair TP/SL would be invalid at current price {fmt(last)}. NEW TRADES PAUSED.")
                all_ok = False
                continue
            if side == "short" and (tp >= last or sl <= last):
                tg(f"🚨 {symbol}: repair TP/SL would be invalid at current price {fmt(last)}. NEW TRADES PAUSED.")
                all_ok = False
                continue

            ok, result = place_tpsl_for_position(p, sl, tp, client_tag="repair")
            if ok:
                tg(f"🛡️ PROTECTED {symbol} {side.upper()}\nSL {sl}\nTP {tp}")
            else:
                tg(f"🚨 PROTECTION FAILED {symbol}\n{result}\nNEW TRADES PAUSED.")
                all_ok = False
        except Exception as e:
            tg(f"🚨 PROTECTION ERROR {symbol}\n{e}\nNEW TRADES PAUSED.")
            all_ok = False

    return all_ok

# ----------------------------
# Opening orders
# ----------------------------
def set_leverage(symbol, leverage, position_side="net"):
    payload = {
        "instId": blofin_inst_id(symbol),
        "leverage": str(leverage),
        "marginMode": MARGIN_MODE,
    }
    # In one-way mode this is net; in hedge mode BloFin wants long/short.
    if position_side in ("long", "short"):
        payload["positionSide"] = position_side
    resp = native("private_post_account_set_leverage", payload)
    if not native_ok(resp):
        # Do not fail solely because leverage was already set or account has a restriction.
        return False, response_error(resp)
    return True, resp


def find_new_position(symbol, before_ids, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            positions = get_positions()
            for p in positions:
                if position_symbol(p) != symbol:
                    continue
                pid = position_id(p)
                if pid and pid not in before_ids:
                    return p
                # Non-multi-position mode may have no positionId.
                if not pid:
                    return p
        except Exception:
            pass
        time.sleep(0.5)
    return None


def open_trade(signal):
    symbol = signal["symbol"]
    side = signal["side"]
    order_side = "buy" if side == "long" else "sell"

    account = futures_account()
    equity = account["total_equity"]
    available = account["available"]

    if equity < MIN_EQUITY_USDT:
        tg(f"⛔ RISK STOP\nEquity ${equity:.2f} below minimum ${MIN_EQUITY_USDT:.2f}")
        return False

    if available <= 0:
        tg(f"⛔ NO TRADE {symbol}\nAvailable futures margin ${available:.2f}")
        return False

    meta = get_instrument(symbol)
    tick = safe_float(meta.get("tickSize"), 0.0001)
    entry = signal["price"]
    levels = calculate_trade_levels(entry, signal["atr"], side, tick)
    if not levels:
        return False
    sl, tp, _ = levels

    sizing = calculate_contracts(symbol, entry, sl, equity, available)
    if not sizing:
        tg(f"⏭️ SKIP {symbol}\nNo valid contract size fits available margin.")
        return False

    # Hard pre-order check.
    required_margin = sizing["margin"]
    if required_margin > available * AVAILABLE_MARGIN_BUFFER:
        tg(
            f"⏭️ SKIP {symbol}\n"
            f"Required margin ${required_margin:.2f} > safe available ${available * AVAILABLE_MARGIN_BUFFER:.2f}"
        )
        return False

    # Record current positions so we can find the newly created position.
    before = get_positions()
    before_ids = {position_id(p) for p in before if position_id(p)}

    # Set leverage for newly opened position.
    lev_ok, lev_err = set_leverage(symbol, LEVERAGE, "net")
    if not lev_ok:
        # Some accounts already have a compatible leverage; continue only for known setting conflicts.
        if "110006" not in lev_err and "110019" not in lev_err:
            tg(f"⚠️ Leverage setting warning {symbol}: {lev_err}")

    payload = {
        "instId": blofin_inst_id(symbol),
        "marginMode": MARGIN_MODE,
        "positionSide": "net",
        "side": order_side,
        "orderType": "market",
        "size": str(sizing["contracts"]),
        "tpTriggerPrice": str(tp),
        "tpOrderPrice": "-1",
        "tpTriggerPriceType": "mark",
        "slTriggerPrice": str(sl),
        "slOrderPrice": "-1",
        "slTriggerPriceType": "mark",
        "clientOrderId": f"v64{int(time.time() * 1000) % 1000000000}",
    }
    if BROKER_ID:
        payload["brokerId"] = BROKER_ID

    if PAPER_TRADING:
        tg(
            f"🧪 PAPER ORDER\n{symbol} {side.upper()}\n"
            f"Entry ~ {entry:.6g}\nSL {sl:.6g}\nTP {tp:.6g}\n"
            f"Contracts {sizing['contracts']}\nMargin ${required_margin:.2f}\n"
            f"Score {signal['score']:.0f}"
        )
        return True

    resp = native("private_post_trade_order", payload)
    if not native_ok(resp):
        err = response_error(resp)
        tg(f"❌ LIVE ORDER FAILED\n{symbol} {side.upper()}\n{err}")
        return False

    tg(
        f"🟢 LIVE ORDER\n{symbol} {side.upper()}\n"
        f"Score {signal['score']:.0f}\n"
        f"Entry ~ {entry:.6g}\nSL {sl:.6g}\nTP {tp:.6g}\n"
        f"Contracts {sizing['contracts']}\nNotional ${sizing['notional']:.2f}\n"
        f"Margin ${required_margin:.2f}\nLeverage {LEVERAGE}x"
    )

    # Confirm the position and protection.
    new_pos = find_new_position(symbol, before_ids, timeout=8)
    if new_pos is None:
        tg(f"⚠️ {symbol}: order accepted but position not yet visible. Protection will be rechecked on next cycle.")
        return True

    protected, _ = has_live_protection(new_pos)
    if protected:
        tg(f"🛡️ PROTECTION CONFIRMED\n{symbol} {side.upper()}\nSL {sl:.6g}\nTP {tp:.6g}")
        return True

    # The opening order should already have attached TP/SL. If not visible,
    # try the standalone TPSL endpoint once.
    ok, result = place_tpsl_for_position(new_pos, sl, tp, client_tag="fallback")
    if ok:
        tg(f"🛡️ FALLBACK PROTECTION CONFIRMED\n{symbol} {side.upper()}")
        return True

    # Do NOT pretend the position is protected.
    tg(
        f"🚨 CRITICAL PROTECTION FAILURE\n{symbol} {side.upper()} is OPEN but TP/SL is NOT CONFIRMED.\n"
        f"Reason: {result}\n\nNEW TRADES PAUSED."
    )
    return False

# ----------------------------
# Risk state
# ----------------------------
startup_equity = None
consecutive_losses = 0
symbol_last_trade = {}
protection_lock = False

# ----------------------------
# Closed-trade / PnL tracking
# ----------------------------
seen_closed_history_ids = set()
daily_trade_date = None
daily_trades = 0
daily_wins = 0
daily_losses = 0
daily_net_pnl = 0.0
daily_gross_pnl = 0.0
daily_fees = 0.0


def reset_daily_stats_if_needed():
    global daily_trade_date, daily_trades, daily_wins, daily_losses
    global daily_net_pnl, daily_gross_pnl, daily_fees
    today = datetime.now().date().isoformat()
    if daily_trade_date != today:
        daily_trade_date = today
        daily_trades = 0
        daily_wins = 0
        daily_losses = 0
        daily_net_pnl = 0.0
        daily_gross_pnl = 0.0
        daily_fees = 0.0


def closed_positions_history(limit=100):
    resp = native("private_get_account_positions_history", {
        "state": "closed",
        "limit": str(limit),
    })
    if not native_ok(resp):
        raise RuntimeError(f"Position history failed: {response_error(resp)}")
    return resp.get("data") or []


def close_reason(record):
    # BloFin position-history records don't always expose a closeType, so
    # infer it from available fields when possible.
    raw = str(record.get("closeType", record.get("state", ""))).lower()
    if raw in ("tp", "take_profit", "take-profit"):
        return "TAKE PROFIT"
    if raw in ("sl", "stop_loss", "stop-loss"):
        return "STOP LOSS"
    if raw in ("liquidated", "liquidation"):
        return "LIQUIDATION"
    if raw == "adl":
        return "ADL"
    if raw in ("mannual", "manual", "close"):
        return "MANUAL / CLOSE"
    return raw.upper() if raw else "CLOSED"


def fmt_duration(start_ms, end_ms):
    start = safe_float(start_ms)
    end = safe_float(end_ms)
    if start <= 0 or end <= 0 or end < start:
        return "unknown"
    seconds = int((end - start) / 1000)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def notify_closed_trade(record):
    global daily_trades, daily_wins, daily_losses
    global daily_net_pnl, daily_gross_pnl, daily_fees

    reset_daily_stats_if_needed()

    symbol = str(record.get("instId", "")).replace("-USDT", "/USDT:USDT")
    side = str(record.get("positionSide", "net")).upper()
    entry = safe_float(record.get("openAveragePrice"))
    exit_price = safe_float(record.get("closeAveragePrice"))
    gross_pnl = safe_float(record.get("realizedPnl"))
    fee = abs(safe_float(record.get("fee")))
    net_pnl = gross_pnl - fee
    pnl_ratio = safe_float(record.get("realizedPnlRatio")) * 100
    duration = fmt_duration(record.get("createTime"), record.get("updateTime"))
    reason = close_reason(record)
    close_positions = safe_float(record.get("closePositions"))

    daily_trades += 1
    if net_pnl >= 0:
        daily_wins += 1
    else:
        daily_losses += 1
    daily_gross_pnl += gross_pnl
    daily_fees += fee
    daily_net_pnl += net_pnl

    icon = "🟢" if net_pnl >= 0 else "🔴"
    sign = "+" if net_pnl >= 0 else ""
    gross_sign = "+" if gross_pnl >= 0 else ""

    tg(
        f"{icon} POSITION CLOSED\n\n"
        f"{symbol}\n"
        f"{side}\n"
        f"━━━━━━━━━━━━\n"
        f"Entry: {entry:.8g}\n"
        f"Exit:  {exit_price:.8g}\n"
        f"Size:  {close_positions:g} contracts\n\n"
        f"💰 Realized PnL: {gross_sign}${gross_pnl:.2f}\n"
        f"📊 PnL ratio: {pnl_ratio:+.2f}%\n"
        f"💸 Fee: -${fee:.2f}\n"
        f"━━━━━━━━━━━━\n"
        f"💵 Net PnL: {sign}${net_pnl:.2f}\n\n"
        f"🎯 Closed by: {reason}\n"
        f"⏱ Duration: {duration}\n\n"
        f"📅 TODAY\n"
        f"Trades: {daily_trades} | Wins: {daily_wins} | Losses: {daily_losses}\n"
        f"Net PnL: {'+' if daily_net_pnl >= 0 else ''}${daily_net_pnl:.2f}"
    )


def seed_closed_history():
    reset_daily_stats_if_needed()
    try:
        records = closed_positions_history(limit=100)
        for r in records:
            hid = str(r.get("historyId", ""))
            if hid:
                seen_closed_history_ids.add(hid)
        tg(f"📚 PNL TRACKER READY\nKnown closed trades: {len(seen_closed_history_ids)}")
    except Exception as e:
        tg(f"⚠️ PNL tracker startup warning\n{e}")


def check_for_closed_positions():
    """Detect newly closed BloFin position-history records.

    BloFin returns newest records first. We seed history at startup, then only
    notify for historyIds that were not previously seen."""
    reset_daily_stats_if_needed()
    records = closed_positions_history(limit=100)
    new_records = []
    for r in reversed(records):
        hid = str(r.get("historyId", ""))
        if not hid or hid in seen_closed_history_ids:
            continue
        seen_closed_history_ids.add(hid)
        new_records.append(r)

    for r in new_records:
        notify_closed_trade(r)

    # Keep memory bounded. History IDs are only needed to avoid duplicates.
    if len(seen_closed_history_ids) > 2000:
        # Rebuild from the latest records, preserving newest IDs.
        seen_closed_history_ids.clear()
        for r in records:
            hid = str(r.get("historyId", ""))
            if hid:
                seen_closed_history_ids.add(hid)

    return len(new_records)


def check_daily_risk():
    global startup_equity
    account = futures_account()
    equity = account["total_equity"]
    if startup_equity is None:
        startup_equity = equity
    if startup_equity > 0:
        drawdown_pct = (startup_equity - equity) / startup_equity * 100
        if drawdown_pct >= DAILY_LOSS_LIMIT_PCT:
            return False, f"Daily risk stop: drawdown {drawdown_pct:.2f}%"
    return True, "OK"


def cooldown_ok(symbol):
    t = symbol_last_trade.get(symbol, 0)
    return (time.time() - t) >= SYMBOL_COOLDOWN_MIN * 60

# ----------------------------
# Startup diagnostics
# ----------------------------
def startup_diagnostics():
    acc = futures_account()
    positions = get_positions()
    tg(
        f"🚀 BLOFIN V6.4 STARTED\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"Equity: ${acc['total_equity']:.2f}\n"
        f"Available: ${acc['available']:.2f}\n"
        f"Positions: {len(positions)}/{MAX_ACTIVE_POSITIONS}\n"
        f"Leverage target: {LEVERAGE}x\n"
        f"Margin target: ${BASE_MARGIN_USDT:.2f} / max ${MAX_MARGIN_USDT:.2f}\n"
        f"Risk cap: {MAX_RISK_EQUITY_PCT*100:.2f}% equity\n"
        f"BrokerId: {'configured' if BROKER_ID else 'not configured'}"
    )

    # Print metadata for currently open positions; useful for debugging sizing.
    for p in positions:
        try:
            meta = get_instrument(position_symbol(p))
            tg(
                f"📌 {position_symbol(p)} {position_side(p).upper()}\n"
                f"Entry {position_entry(p):.8g}\n"
                f"Size {position_size(p)} contracts\n"
                f"contractValue {meta.get('contractValue')}\n"
                f"minSize {meta.get('minSize')}\n"
                f"lotSize {meta.get('lotSize')}"
            )
        except Exception as e:
            tg(f"⚠️ Metadata error {position_symbol(p)}: {e}")

# ----------------------------
# Main loop
# ----------------------------
def main():
    global protection_lock, consecutive_losses

    startup_diagnostics()
    if not PAPER_TRADING:
        seed_closed_history()

    if not PAPER_TRADING and REPAIR_EXISTING_POSITIONS:
        try:
            protection_lock = not repair_existing_positions()
        except Exception as e:
            protection_lock = True
            tg(f"🚨 STARTUP PROTECTION CHECK FAILED\n{e}\nNEW TRADES PAUSED.")

    if protection_lock and not PAPER_TRADING:
        tg(
            "🛑 V6.4 SAFE MODE\n"
            "Existing position protection could not be confirmed.\n"
            "The bot will scan, but will NOT open new trades."
        )

    while True:
        try:
            # Detect positions that were closed since the previous cycle.
            if not PAPER_TRADING:
                try:
                    check_for_closed_positions()
                except Exception as e:
                    # PnL tracking must never crash the trading loop.
                    tg(f"⚠️ PNL TRACKER ERROR\n{e}")

            ok, reason = check_daily_risk()
            if not ok:
                tg(f"🛑 {reason}\nBot will keep monitoring but will not open new trades.")
                time.sleep(120)
                continue

            positions = get_positions()

            # Re-check protection of every open position every cycle.
            if not PAPER_TRADING:
                all_protected = True
                for p in positions:
                    protected, _ = has_live_protection(p)
                    if not protected:
                        all_protected = False
                        if REPAIR_EXISTING_POSITIONS:
                            # Attempt repair using fresh ATR.
                            try:
                                symbol = position_symbol(p)
                                side = position_side(p)
                                entry = position_entry(p)
                                df = add_indicators(ohlcv_df(symbol, TIMEFRAME_SIGNAL))
                                atr = float(df.iloc[-2]["atr"])
                                meta = get_instrument(symbol)
                                tick = safe_float(meta.get("tickSize"), 0.0001)
                                levels = calculate_trade_levels(entry, atr, side, tick)
                                if levels:
                                    sl, tp, _ = levels
                                    last = safe_float(exchange.fetch_ticker(symbol).get("last"))
                                    valid = (sl < last < tp) if side == "long" else (tp < last < sl)
                                    if valid:
                                        repair_ok, _ = place_tpsl_for_position(p, sl, tp, client_tag="watch")
                                        if repair_ok:
                                            all_protected = True
                                            tg(f"🛡️ REPAIRED {symbol} {side.upper()} protection confirmed.")
                            except Exception as e:
                                pass

                protection_lock = not all_protected

            if len(positions) >= MAX_ACTIVE_POSITIONS:
                time.sleep(SCAN_SECONDS)
                continue

            if protection_lock and not PAPER_TRADING:
                time.sleep(SCAN_SECONDS)
                continue

            # Scan all configured markets.
            candidates = []
            for symbol in SYMBOLS:
                if not cooldown_ok(symbol):
                    continue
                try:
                    signal = evaluate_symbol(symbol)
                    if signal:
                        candidates.append(signal)
                except Exception:
                    continue

            candidates.sort(key=lambda x: x["score"], reverse=True)

            # Take the best current opportunity only if there is room.
            for signal in candidates:
                current_positions = get_positions()
                if len(current_positions) >= MAX_ACTIVE_POSITIONS:
                    break

                # Avoid adding to a symbol that is already open.
                open_symbols = {position_symbol(p) for p in current_positions}
                if signal["symbol"] in open_symbols:
                    continue

                traded = open_trade(signal)
                symbol_last_trade[signal["symbol"]] = time.time()

                # If a live order created an unprotected position, lock immediately.
                if traded and not PAPER_TRADING:
                    try:
                        fresh_positions = get_positions()
                        for p in fresh_positions:
                            protected, _ = has_live_protection(p)
                            if not protected:
                                protection_lock = True
                                break
                    except Exception:
                        protection_lock = True

                # Only one new entry per scan cycle; no 1-hour artificial cooldown.
                break

            time.sleep(SCAN_SECONDS)

        except KeyboardInterrupt:
            tg("🛑 V6.4 stopped manually.")
            raise
        except Exception as e:
            tg(f"⚠️ V6.4 LOOP ERROR\n{type(e).__name__}: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
