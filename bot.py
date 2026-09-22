import os
import time
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta

import ccxt
import numpy as np
import pandas as pd
import requests

# ============================================================
# BLOFIN V6.6
# ============================================================
# Behoudt van V6.5:
# - contractValue / minSize / lotSize sizing
# - attached TP/SL + brokerId
# - 5m confirm verplicht, min 4/6 tactics
# - nachtblok alts, win/loss cooldown, 3-loss pauze
# - één nieuwe trade per scan
#
# Nieuw t.o.v. review V6.5:
# - Daily risk reset ECHT op Amsterdam-dag (niet startup_equity forever)
# - Shorts via relative strength i.p.v. hard BTC-bull verbod
# - ADX als chop-filter (niet als zware score)
# - RSI alleen als overextension-blok
# - EMA-slope op 15m
# - Fee = taker% * notional * 2 + slippage, geen vast $0.12
# - MAX 1 alt-long én 1 alt-short
# - Extra cooldown bij herhaald verlies op dezelfde munt
# - LONG/SHORT dagstatistiek
# - Position history: closed + liquidated + adl
# ============================================================

API_KEY = os.getenv("EXCHANGE_API_KEY", "").strip()
API_SECRET = os.getenv("EXCHANGE_SECRET", "").strip()
API_PASSWORD = os.getenv("EXCHANGE_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"
LIVE_CONFIRM = os.getenv("LIVE_CONFIRM", "")
BROKER_ID = os.getenv("BLOFIN_BROKER_ID", "").strip()

TIMEFRAME_SIGNAL = "15m"
TIMEFRAME_CONFIRM = "5m"
TIMEFRAME_TREND = "1h"
CANDLE_LIMIT = 240
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "45"))

WIN_COOLDOWN_MIN = int(os.getenv("WIN_COOLDOWN_MIN", "20"))
LOSS_COOLDOWN_MIN = int(os.getenv("LOSS_COOLDOWN_MIN", "60"))
REPEAT_LOSS_COOLDOWN_MIN = int(os.getenv("REPEAT_LOSS_COOLDOWN_MIN", "180"))
LOSS_PAUSE_MIN = int(os.getenv("LOSS_PAUSE_MIN", "90"))

MAX_ACTIVE_POSITIONS = int(os.getenv("MAX_ACTIVE_POSITIONS", "2"))
MAX_ALT_LONGS = int(os.getenv("MAX_ALT_LONGS", "1"))
MAX_ALT_SHORTS = int(os.getenv("MAX_ALT_SHORTS", "1"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "68"))
MIN_TACTICS = int(os.getenv("MIN_TACTICS", "4"))

LEVERAGE = int(os.getenv("LEVERAGE", "10"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")
BASE_MARGIN_USDT = float(os.getenv("BASE_MARGIN_USDT", "10"))
MAX_MARGIN_USDT = float(os.getenv("MAX_MARGIN_USDT", "12"))
MAX_MARGIN_EQUITY_PCT = float(os.getenv("MAX_MARGIN_EQUITY_PCT", "0.18"))
MAX_RISK_EQUITY_PCT = float(os.getenv("MAX_RISK_EQUITY_PCT", "0.012"))
AVAILABLE_MARGIN_BUFFER = float(os.getenv("AVAILABLE_MARGIN_BUFFER", "0.90"))

ATR_PERIOD = 14
ADX_PERIOD = 14
RSI_PERIOD = 14
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.40"))
TP_R_MULT = float(os.getenv("TP_R_MULT", "1.80"))
MIN_STOP_PCT = float(os.getenv("MIN_STOP_PCT", "0.0045"))
MAX_STOP_PCT = float(os.getenv("MAX_STOP_PCT", "0.028"))
MIN_NET_TP_USDT = float(os.getenv("MIN_NET_TP_USDT", "0.40"))
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.0006"))
SLIPPAGE_RATE = float(os.getenv("SLIPPAGE_RATE", "0.0003"))

VOLUME_MULT = float(os.getenv("VOLUME_MULT", "1.20"))
DONCHIAN_LOOKBACK = 12
NEAR_SWEEP_ATR = 0.20
ADX_TREND = float(os.getenv("ADX_TREND", "18"))
ADX_CHOP = float(os.getenv("ADX_CHOP", "14"))
RSI_LONG_MAX = float(os.getenv("RSI_LONG_MAX", "78"))
RSI_SHORT_MIN = float(os.getenv("RSI_SHORT_MIN", "22"))
RS_OVERRIDE = float(os.getenv("RS_OVERRIDE", "0.008"))

DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "5"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
MIN_EQUITY_USDT = float(os.getenv("MIN_EQUITY_USDT", "30"))
REPAIR_EXISTING_POSITIONS = os.getenv("REPAIR_EXISTING_POSITIONS", "true").lower() == "true"
SCORE_REPORT_MIN = int(os.getenv("SCORE_REPORT_MIN", "30"))

NIGHT_END_UTC = 6
CORE_START_UTC = 7
CORE_END_UTC = 21
BTC_SYMBOL = "BTC/USDT:USDT"
MAJORS = {"BTC/USDT:USDT", "ETH/USDT:USDT"}

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

if not API_KEY or not API_SECRET or not API_PASSWORD:
    raise RuntimeError("Missing EXCHANGE_API_KEY / EXCHANGE_SECRET / EXCHANGE_PASSWORD")
if not PAPER_TRADING and LIVE_CONFIRM != "YES_I_WANT_LIVE_TRADING":
    raise RuntimeError("LIVE TRADING BLOCKED. Set LIVE_CONFIRM=YES_I_WANT_LIVE_TRADING")

exchange = ccxt.blofin({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "password": API_PASSWORD,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})
exchange.load_markets()
SYMBOLS = [s for s in SYMBOLS if s in exchange.markets]
if not SYMBOLS:
    raise RuntimeError("No configured symbols are available on BloFin.")


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


def now_utc():
    return datetime.now(timezone.utc)


def amsterdam_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=2)).date().isoformat()


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


def round_price(price, tick):
    return floor_step(price, tick)


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
    equity = safe_float(usdt.get("equity"))
    if total_equity <= 0:
        total_equity = equity or balance
    return {
        "total_equity": total_equity,
        "equity": equity or total_equity,
        "balance": balance,
        "available": available,
    }


def get_positions():
    resp = native("private_get_account_positions", {})
    if not native_ok(resp):
        raise RuntimeError(f"Positions API failed: {response_error(resp)}")
    result = []
    for p in resp.get("data") or []:
        pos_size = safe_float(p.get("positions", p.get("position", p.get("size", 0))))
        if abs(pos_size) > 0:
            result.append(p)
    return result


def position_symbol(p):
    return str(p.get("instId", "")).replace("-USDT", "/USDT:USDT")


def position_side(p):
    side = str(p.get("positionSide", "net")).lower()
    if side in ("long", "short"):
        return side
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


def ohlcv_df(symbol, timeframe):
    rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=CANDLE_LIMIT)
    if len(rows) < 60:
        raise RuntimeError(f"Not enough candles for {symbol} {timeframe}")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna().reset_index(drop=True)


def wilder_smooth(series, period):
    return series.ewm(alpha=1 / period, adjust=False).mean()


def add_indicators(df):
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema200"] = out["close"].ewm(span=200, adjust=False).mean()
    out["ema20_slope"] = out["ema20"] - out["ema20"].shift(3)

    prev_close = out["close"].shift(1)
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - prev_close).abs(),
        (out["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = tr.rolling(ATR_PERIOD).mean()

    up = out["close"].diff().clip(lower=0)
    down = -out["close"].diff().clip(upper=0)
    rs = wilder_smooth(up, RSI_PERIOD) / wilder_smooth(down, RSI_PERIOD).replace(0, np.nan)
    out["rsi"] = 100 - (100 / (1 + rs))

    up_move = out["high"].diff()
    down_move = -out["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_w = wilder_smooth(tr, ADX_PERIOD)
    plus_di = 100 * wilder_smooth(pd.Series(plus_dm, index=out.index), ADX_PERIOD) / atr_w.replace(0, np.nan)
    minus_di = 100 * wilder_smooth(pd.Series(minus_dm, index=out.index), ADX_PERIOD) / atr_w.replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    out["adx"] = wilder_smooth(dx, ADX_PERIOD)
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di

    out["vol_sma20"] = out["volume"].rolling(20).mean()
    out["donchian_high"] = out["high"].rolling(DONCHIAN_LOOKBACK).max().shift(1)
    out["donchian_low"] = out["low"].rolling(DONCHIAN_LOOKBACK).min().shift(1)
    out["ret15"] = out["close"].pct_change(3)
    return out


def session_state():
    hour = now_utc().hour
    if hour < NIGHT_END_UTC:
        return "NIGHT"
    if CORE_START_UTC <= hour < CORE_END_UTC:
        return "CORE"
    return "SHOULDER"


btc_cache = {"ts": 0, "data": None}


def btc_regime():
    now = time.time()
    if btc_cache["data"] and now - btc_cache["ts"] < 60:
        return btc_cache["data"]
    df15 = add_indicators(ohlcv_df(BTC_SYMBOL, TIMEFRAME_SIGNAL))
    df1h = add_indicators(ohlcv_df(BTC_SYMBOL, TIMEFRAME_TREND))
    a = df15.iloc[-2]
    t = df1h.iloc[-2]
    data = {
        "bull": bool(t["ema20"] > t["ema50"]),
        "bear": bool(t["ema20"] < t["ema50"]),
        "impulse_up": bool(a["close"] > a["ema20"] and a["ema20"] >= a["ema50"]),
        "impulse_down": bool(a["close"] < a["ema20"] and a["ema20"] <= a["ema50"]),
        "ret15": float(a["ret15"]) if pd.notna(a["ret15"]) else 0.0,
        "adx": float(a["adx"]) if pd.notna(a["adx"]) else 0.0,
        "close": float(a["close"]),
    }
    btc_cache["ts"] = now
    btc_cache["data"] = data
    return data


def evaluate_symbol(symbol, btc):
    sess = session_state()
    if sess == "NIGHT" and symbol not in MAJORS:
        return {
            "symbol": symbol, "side": None, "score": 0, "tactics": 0,
            "skip": "night-alts", "price": 0, "atr": 0,
        }

    df15 = add_indicators(ohlcv_df(symbol, TIMEFRAME_SIGNAL))
    df5 = add_indicators(ohlcv_df(symbol, TIMEFRAME_CONFIRM))
    df1h = add_indicators(ohlcv_df(symbol, TIMEFRAME_TREND))

    a = df15.iloc[-2]
    a_prev = df15.iloc[-3]
    c = df5.iloc[-2]
    c_prev = df5.iloc[-3]
    t = df1h.iloc[-2]

    price = float(a["close"])
    atr = float(a["atr"])
    if not np.isfinite(atr) or atr <= 0:
        return None

    rsi = float(a["rsi"]) if pd.notna(a["rsi"]) else 50.0
    adx = float(a["adx"]) if pd.notna(a["adx"]) else 0.0
    coin_ret = float(a["ret15"]) if pd.notna(a["ret15"]) else 0.0
    rel = coin_ret - btc["ret15"]
    slope_up = float(a["ema20_slope"]) > 0
    slope_down = float(a["ema20_slope"]) < 0

    stack_long = t["ema20"] > t["ema50"] > t["ema200"]
    stack_short = t["ema20"] < t["ema50"] < t["ema200"]
    fast_long = t["ema20"] > t["ema50"]
    fast_short = t["ema20"] < t["ema50"]
    ema_long = a["ema20"] > a["ema50"] and price > a["ema20"] and slope_up
    ema_short = a["ema20"] < a["ema50"] and price < a["ema20"] and slope_down
    volume_ok = a["volume"] > (a["vol_sma20"] * VOLUME_MULT) if pd.notna(a["vol_sma20"]) else False
    breakout_long = price > a["donchian_high"] if pd.notna(a["donchian_high"]) else False
    breakout_short = price < a["donchian_low"] if pd.notna(a["donchian_low"]) else False

    recent_low = df15["low"].iloc[-DONCHIAN_LOOKBACK - 3:-3].min()
    recent_high = df15["high"].iloc[-DONCHIAN_LOOKBACK - 3:-3].max()
    sweep_long = (a_prev["low"] < recent_low - NEAR_SWEEP_ATR * atr) and (a["close"] > recent_low)
    sweep_short = (a_prev["high"] > recent_high + NEAR_SWEEP_ATR * atr) and (a["close"] < recent_high)
    structure_long = breakout_long or sweep_long
    structure_short = breakout_short or sweep_short

    confirm_long = c["ema20"] > c["ema50"] and c["close"] > c["ema20"] and c["close"] > c_prev["close"]
    confirm_short = c["ema20"] < c["ema50"] and c["close"] < c["ema20"] and c["close"] < c_prev["close"]
    atr_pct = atr / price
    atr_quality = 0.0018 <= atr_pct <= 0.035
    trending = adx >= ADX_TREND
    choppy = adx < ADX_CHOP

    long_score = short_score = 0
    long_t = short_t = 0

    if stack_long:
        long_score += 16
        long_t += 1
    elif fast_long:
        long_score += 7
    if stack_short:
        short_score += 16
        short_t += 1
    elif fast_short:
        short_score += 7

    if ema_long:
        long_score += 12
        long_t += 1
    if ema_short:
        short_score += 12
        short_t += 1
    if volume_ok:
        long_score += 10
        short_score += 10
        long_t += 1
        short_t += 1
    if structure_long:
        long_score += 16 if sweep_long else 11
        long_t += 1
    if structure_short:
        short_score += 16 if sweep_short else 11
        short_t += 1
    if confirm_long:
        long_score += 18
        long_t += 1
    if confirm_short:
        short_score += 18
        short_t += 1
    if atr_quality:
        long_score += 5
        short_score += 5
        long_t += 1
        short_t += 1
    if trending:
        long_score += 6
        short_score += 6
    if rel > RS_OVERRIDE:
        long_score += 6
    if rel < -RS_OVERRIDE:
        short_score += 6

    if long_score >= short_score:
        side, score, tactics = "long", long_score, long_t
        confirm, structure = confirm_long, structure_long
    else:
        side, score, tactics = "short", short_score, short_t
        confirm, structure = confirm_short, structure_short

    skip = None
    if sess == "SHOULDER":
        score -= 5
    if choppy:
        skip = "adx-chop"
    if side == "long" and rsi >= RSI_LONG_MAX:
        skip = skip or "rsi-overbought"
    if side == "short" and rsi <= RSI_SHORT_MIN:
        skip = skip or "rsi-oversold"

    if symbol not in MAJORS:
        strong_pos = rel >= RS_OVERRIDE
        strong_neg = rel <= -RS_OVERRIDE
        if side == "long":
            if btc["impulse_down"] and not strong_pos:
                skip = skip or "btc-against-long"
            elif btc["bear"] and not strong_pos:
                skip = skip or "btc-bear"
        else:
            if btc["impulse_up"] and not strong_neg:
                skip = skip or "btc-against-short"
            # BTC bull + weak coin = allowed short (relative-strength override)

    if not confirm:
        skip = skip or "no-5m-confirm"
    if tactics < MIN_TACTICS:
        skip = skip or f"tactics-{tactics}/{MIN_TACTICS}"
    if score < MIN_SCORE:
        skip = skip or f"score-{score:.0f}<{MIN_SCORE:.0f}"
    if not atr_quality:
        skip = skip or "atr-band"

    return {
        "symbol": symbol,
        "side": side,
        "score": float(score),
        "tactics": tactics,
        "price": price,
        "atr": atr,
        "atr_pct": atr_pct,
        "rsi": rsi,
        "adx": adx,
        "rel": rel,
        "confirm": bool(confirm),
        "structure": bool(structure),
        "skip": skip,
        "session": sess,
    }


def calculate_trade_levels(entry, atr, side, tick):
    risk_distance = max(atr * ATR_SL_MULT, entry * MIN_STOP_PCT)
    if risk_distance / entry > MAX_STOP_PCT:
        return None
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


def roundtrip_cost(notional):
    return notional * ((TAKER_FEE_RATE * 2) + SLIPPAGE_RATE)


def calculate_contracts(symbol, entry, sl, equity, available):
    meta = get_instrument(symbol)
    contract_value = safe_float(meta.get("contractValue"))
    min_size = safe_float(meta.get("minSize"))
    lot_size = safe_float(meta.get("lotSize"))
    max_market_size = safe_float(meta.get("maxMarketSize"), 0)
    if contract_value <= 0 or min_size <= 0 or lot_size <= 0:
        raise RuntimeError(f"Bad contract metadata for {symbol}")

    stop_pct = abs(entry - sl) / entry
    risk_budget = equity * MAX_RISK_EQUITY_PCT
    risk_notional = risk_budget / stop_pct if stop_pct > 0 else 0
    margin_cap = min(BASE_MARGIN_USDT, MAX_MARGIN_USDT, equity * MAX_MARGIN_EQUITY_PCT)
    target_notional = min(risk_notional, margin_cap * LEVERAGE)
    max_affordable_margin = max(0.0, available * AVAILABLE_MARGIN_BUFFER)
    target_notional = min(target_notional, max_affordable_margin * LEVERAGE)
    if target_notional <= 0:
        return None

    contracts = floor_step(target_notional / (contract_value * entry), lot_size)
    if contracts < min_size:
        contracts = min_size
    if max_market_size > 0:
        contracts = min(contracts, floor_step(max_market_size, lot_size))
    if contracts < min_size:
        return None

    notional = contracts * contract_value * entry
    margin = notional / LEVERAGE
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

    expected_tp = notional * TP_R_MULT * stop_pct
    cost = roundtrip_cost(notional)
    if expected_tp - cost < MIN_NET_TP_USDT:
        return None

    return {
        "contracts": contracts,
        "contract_value": contract_value,
        "notional": notional,
        "margin": margin,
        "expected_tp_usdt": expected_tp,
        "roundtrip_cost": cost,
    }


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
        if safe_float(o.get("tpTriggerPrice")) > 0 or safe_float(o.get("slTriggerPrice")) > 0:
            return True, o
    return False, None


def tpsl_prices_valid(side, sl, tp, last):
    if last <= 0:
        return False
    if side == "long":
        return sl < last < tp
    return tp < last < sl


def place_tpsl_for_position(p, sl, tp, client_tag="v66"):
    inst = p["instId"]
    side = position_side(p)
    pside = str(p.get("positionSide", "net")).lower()
    if pside not in ("net", "long", "short"):
        pside = "net"
    payload = {
        "instId": inst,
        "marginMode": position_margin_mode(p),
        "positionSide": pside,
        "side": "sell" if side == "long" else "buy",
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
        return False, "TPSL accepted but not visible"
    return True, found


def repair_existing_positions():
    positions = get_positions()
    if not positions:
        return True
    tg(f"🛡️ V6.6 STARTUP\nOpen positions: {len(positions)}\nChecking TP/SL...")
    all_ok = True
    for p in positions:
        symbol = position_symbol(p)
        side = position_side(p)
        entry = position_entry(p)
        if not entry:
            continue
        protected, _ = has_live_protection(p)
        if protected:
            tg(f"✅ {symbol} {side.upper()}: TP/SL confirmed.")
            continue
        try:
            df = add_indicators(ohlcv_df(symbol, TIMEFRAME_SIGNAL))
            atr = float(df.iloc[-2]["atr"])
            tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
            levels = calculate_trade_levels(entry, atr, side, tick)
            if not levels:
                all_ok = False
                continue
            sl, tp, _ = levels
            last = safe_float(exchange.fetch_ticker(symbol).get("last"))
            if not tpsl_prices_valid(side, sl, tp, last):
                tg(f"⚠️ {symbol}: prijs buiten SL/TP, geen 152002-repair.")
                continue
            ok, result = place_tpsl_for_position(p, sl, tp, client_tag="repair")
            if ok:
                tg(f"🛡️ PROTECTED {symbol} {side.upper()}\nSL {sl}\nTP {tp}")
            else:
                tg(f"🚨 PROTECTION FAILED {symbol}\n{result}")
                all_ok = False
        except Exception as e:
            tg(f"🚨 PROTECTION ERROR {symbol}\n{e}")
            all_ok = False
    return all_ok


def set_leverage(symbol, leverage, position_side="net"):
    payload = {
        "instId": blofin_inst_id(symbol),
        "leverage": str(leverage),
        "marginMode": MARGIN_MODE,
    }
    if position_side in ("long", "short"):
        payload["positionSide"] = position_side
    resp = native("private_post_account_set_leverage", payload)
    if not native_ok(resp):
        return False, response_error(resp)
    return True, resp


def find_new_position(symbol, before_ids, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for p in get_positions():
                if position_symbol(p) != symbol:
                    continue
                pid = position_id(p)
                if pid and pid not in before_ids:
                    return p
                if not pid:
                    return p
        except Exception:
            pass
        time.sleep(0.5)
    return None


protection_lock = False
loss_pause_until = 0
consecutive_losses = 0
symbol_last_trade = {}
symbol_last_result = {}
symbol_loss_streak = {}
seen_closed_history_ids = set()
last_score_report = 0

daily = {
    "date": None,
    "start_equity": None,
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "net": 0.0,
    "long_trades": 0,
    "long_wins": 0,
    "long_net": 0.0,
    "short_trades": 0,
    "short_wins": 0,
    "short_net": 0.0,
}


def reset_daily_if_needed(current_equity=None):
    today = amsterdam_today()
    if daily["date"] != today:
        daily["date"] = today
        daily["start_equity"] = current_equity
        daily["trades"] = daily["wins"] = daily["losses"] = 0
        daily["net"] = 0.0
        daily["long_trades"] = daily["long_wins"] = 0
        daily["long_net"] = 0.0
        daily["short_trades"] = daily["short_wins"] = 0
        daily["short_net"] = 0.0
        return True
    if daily["start_equity"] is None and current_equity is not None:
        daily["start_equity"] = current_equity
    return False


def open_trade(signal):
    global protection_lock
    symbol = signal["symbol"]
    side = signal["side"]
    account = futures_account()
    equity = account["total_equity"]
    available = account["available"]
    reset_daily_if_needed(equity)
    if equity < MIN_EQUITY_USDT:
        tg(f"⛔ RISK STOP\nEquity ${equity:.2f} < ${MIN_EQUITY_USDT:.2f}")
        return False
    if available <= 0:
        return False

    tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
    entry = signal["price"]
    levels = calculate_trade_levels(entry, signal["atr"], side, tick)
    if not levels:
        return False
    sl, tp, _ = levels
    stop_pct = abs(entry - sl) / entry
    if stop_pct < MIN_STOP_PCT:
        return False

    sizing = calculate_contracts(symbol, entry, sl, equity, available)
    if not sizing:
        return False
    if sizing["margin"] > available * AVAILABLE_MARGIN_BUFFER:
        return False

    before_ids = {position_id(p) for p in get_positions() if position_id(p)}
    lev_ok, lev_err = set_leverage(symbol, LEVERAGE, "net")
    if not lev_ok and "110006" not in lev_err and "110019" not in lev_err:
        tg(f"⚠️ Leverage warning {symbol}: {lev_err}")

    payload = {
        "instId": blofin_inst_id(symbol),
        "marginMode": MARGIN_MODE,
        "positionSide": "net",
        "side": "buy" if side == "long" else "sell",
        "orderType": "market",
        "size": str(sizing["contracts"]),
        "tpTriggerPrice": str(tp),
        "tpOrderPrice": "-1",
        "tpTriggerPriceType": "mark",
        "slTriggerPrice": str(sl),
        "slOrderPrice": "-1",
        "slTriggerPriceType": "mark",
        "clientOrderId": f"v66{int(time.time() * 1000) % 1000000000}",
    }
    if BROKER_ID:
        payload["brokerId"] = BROKER_ID

    if PAPER_TRADING:
        tg(
            f"🧪 PAPER {symbol} {side.upper()}\n"
            f"Score {signal['score']:.0f} t{signal['tactics']} ADX {signal.get('adx', 0):.0f}\n"
            f"RS {signal.get('rel', 0)*100:+.2f}% RSI {signal.get('rsi', 0):.0f}"
        )
        return True

    resp = native("private_post_trade_order", payload)
    if not native_ok(resp):
        err = response_error(resp)
        if "152012" in err:
            protection_lock = True
            tg(f"🛑 152012 lock\n{err}")
        else:
            tg(f"❌ LIVE ORDER FAILED\n{symbol} {side.upper()}\n{err}")
        return False

    tg(
        f"🟢 LIVE ORDER\n{symbol} {side.upper()}\n"
        f"Score {signal['score']:.0f} | t{signal['tactics']}/6 | ADX {signal.get('adx', 0):.0f}\n"
        f"RSI {signal.get('rsi', 0):.0f} | RS {signal.get('rel', 0)*100:+.2f}%\n"
        f"Entry ~ {entry:.6g}\nSL {sl:.6g}\nTP {tp:.6g}\n"
        f"Stop {stop_pct*100:.2f}% | ExpTP ${sizing['expected_tp_usdt']:.2f}\n"
        f"Cost ${sizing['roundtrip_cost']:.2f}\n"
        f"Contracts {sizing['contracts']} | Notional ${sizing['notional']:.2f}\n"
        f"Margin ${sizing['margin']:.2f} | {LEVERAGE}x"
    )

    new_pos = find_new_position(symbol, before_ids, timeout=8)
    if new_pos is None:
        tg(f"⚠️ {symbol}: order ok, positie nog niet zichtbaar.")
        return True
    if has_live_protection(new_pos)[0]:
        tg(f"🛡️ PROTECTION CONFIRMED\n{symbol} {side.upper()}\nSL {sl:.6g}\nTP {tp:.6g}")
        return True
    last = safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side, sl, tp, last):
        ok, result = place_tpsl_for_position(new_pos, sl, tp, client_tag="fallback")
        if ok:
            tg(f"🛡️ FALLBACK PROTECTION CONFIRMED\n{symbol}")
            return True
        tg(f"🚨 CRITICAL PROTECTION FAILURE\n{symbol}\n{result}\nNEW TRADES PAUSED.")
        return False
    tg(f"⚠️ {symbol}: geen fallback TPSL, prijs al buiten range.")
    return True


def closed_positions_history(limit=100):
    merged = []
    for state in ("closed", "liquidated", "adl"):
        try:
            resp = native("private_get_account_positions_history", {
                "state": state,
                "limit": str(limit),
            })
            if native_ok(resp):
                merged.extend(resp.get("data") or [])
        except Exception:
            continue
    return merged


def close_reason(record):
    raw = str(record.get("closeType", record.get("state", ""))).lower()
    mapping = {
        "tp": "TAKE PROFIT", "take_profit": "TAKE PROFIT",
        "sl": "STOP LOSS", "stop_loss": "STOP LOSS",
        "liquidated": "LIQUIDATION", "liquidation": "LIQUIDATION",
        "partially_liquidated": "PARTIAL LIQ", "adl": "ADL",
        "partially_closed": "PARTIAL CLOSE",
        "manual": "MANUAL", "mannual": "MANUAL", "close": "CLOSED",
    }
    return mapping.get(raw, raw.upper() if raw else "CLOSED")


def fmt_duration(start_ms, end_ms):
    start = safe_float(start_ms)
    end = safe_float(end_ms)
    if start <= 0 or end <= 0 or end < start:
        return "unknown"
    seconds = int((end - start) / 1000)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def infer_side(record):
    side = str(record.get("positionSide", "net")).lower()
    if side in ("long", "short"):
        return side
    entry = safe_float(record.get("openAveragePrice"))
    exit_p = safe_float(record.get("closeAveragePrice"))
    pnl = safe_float(record.get("realizedPnl"))
    if entry and exit_p:
        moved_up = exit_p >= entry
        return "long" if (moved_up and pnl >= 0) or (not moved_up and pnl < 0) else "short"
    return "net"


def notify_closed_trade(record):
    global consecutive_losses, loss_pause_until
    reset_daily_if_needed()
    symbol = str(record.get("instId", "")).replace("-USDT", "/USDT:USDT")
    side = infer_side(record)
    entry = safe_float(record.get("openAveragePrice"))
    exit_price = safe_float(record.get("closeAveragePrice"))
    gross_pnl = safe_float(record.get("realizedPnl"))
    fee = abs(safe_float(record.get("fee")))
    net_pnl = gross_pnl - fee
    pnl_ratio = safe_float(record.get("realizedPnlRatio")) * 100
    duration = fmt_duration(record.get("createTime"), record.get("updateTime"))
    reason = close_reason(record)
    size = safe_float(record.get("closePositions"))

    daily["trades"] += 1
    daily["net"] += net_pnl
    if side == "short":
        daily["short_trades"] += 1
        daily["short_net"] += net_pnl
        if net_pnl >= 0:
            daily["short_wins"] += 1
    else:
        daily["long_trades"] += 1
        daily["long_net"] += net_pnl
        if net_pnl >= 0:
            daily["long_wins"] += 1

    symbol_last_trade[symbol] = time.time()
    if net_pnl >= 0:
        daily["wins"] += 1
        consecutive_losses = 0
        symbol_last_result[symbol] = "win"
        symbol_loss_streak[symbol] = 0
    else:
        daily["losses"] += 1
        consecutive_losses += 1
        symbol_last_result[symbol] = "loss"
        symbol_loss_streak[symbol] = symbol_loss_streak.get(symbol, 0) + 1
        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            loss_pause_until = time.time() + LOSS_PAUSE_MIN * 60

    wr_long = (daily["long_wins"] / daily["long_trades"] * 100) if daily["long_trades"] else 0
    wr_short = (daily["short_wins"] / daily["short_trades"] * 100) if daily["short_trades"] else 0
    pause = ""
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        pause = f"\n⏸ {MAX_CONSECUTIVE_LOSSES} losses → pauze {LOSS_PAUSE_MIN} min"
    icon = "🟢" if net_pnl >= 0 else "🔴"
    tg(
        f"{icon} POSITION CLOSED\n\n{symbol}\n{side.upper()}\n"
        f"━━━━━━━━━━━━\n"
        f"Entry: {entry:.8g}\nExit:  {exit_price:.8g}\n"
        f"Size:  {size:g}\n\n"
        f"💰 Realized: {'+' if gross_pnl >= 0 else ''}${gross_pnl:.2f}\n"
        f"📊 Ratio: {pnl_ratio:+.2f}%\n💸 Fee: -${fee:.2f}\n"
        f"💵 Net: {'+' if net_pnl >= 0 else ''}${net_pnl:.2f}\n\n"
        f"🎯 {reason} | ⏱ {duration}\n\n"
        f"📅 TODAY Amsterdam\n"
        f"All: {daily['trades']} | {daily['wins']}W {daily['losses']}L | "
        f"{'+' if daily['net'] >= 0 else ''}${daily['net']:.2f}\n"
        f"LONG: {daily['long_trades']} / {wr_long:.0f}% / "
        f"{'+' if daily['long_net'] >= 0 else ''}${daily['long_net']:.2f}\n"
        f"SHORT: {daily['short_trades']} / {wr_short:.0f}% / "
        f"{'+' if daily['short_net'] >= 0 else ''}${daily['short_net']:.2f}"
        f"{pause}"
    )


def seed_closed_history():
    reset_daily_if_needed()
    try:
        for r in closed_positions_history(100):
            hid = str(r.get("historyId", ""))
            if hid:
                seen_closed_history_ids.add(hid)
        tg(f"📚 PNL TRACKER READY\nKnown closes: {len(seen_closed_history_ids)}")
    except Exception as e:
        tg(f"⚠️ PNL tracker warning\n{e}")


def check_for_closed_positions():
    records = closed_positions_history(100)
    new_records = []
    for r in reversed(records):
        hid = str(r.get("historyId", ""))
        if not hid or hid in seen_closed_history_ids:
            continue
        seen_closed_history_ids.add(hid)
        new_records.append(r)
    for r in new_records:
        notify_closed_trade(r)
    if len(seen_closed_history_ids) > 2500:
        seen_closed_history_ids.clear()
        for r in records:
            hid = str(r.get("historyId", ""))
            if hid:
                seen_closed_history_ids.add(hid)


def check_daily_risk(equity):
    reset_daily_if_needed(equity)
    start = daily["start_equity"]
    if not start or start <= 0:
        return True, "OK"
    dd = (start - equity) / start * 100
    if dd >= DAILY_LOSS_LIMIT_PCT:
        return False, f"Daily risk stop {dd:.2f}% vs start ${start:.2f}"
    return True, "OK"


def cooldown_ok(symbol):
    last = symbol_last_trade.get(symbol, 0)
    if symbol_loss_streak.get(symbol, 0) >= 2:
        needed = REPEAT_LOSS_COOLDOWN_MIN * 60
    elif symbol_last_result.get(symbol) == "loss":
        needed = LOSS_COOLDOWN_MIN * 60
    else:
        needed = WIN_COOLDOWN_MIN * 60
    return (time.time() - last) >= needed


def count_alt_sides(positions):
    longs = shorts = 0
    for p in positions:
        if position_symbol(p) in MAJORS:
            continue
        if position_side(p) == "short":
            shorts += 1
        else:
            longs += 1
    return longs, shorts


def startup_diagnostics():
    acc = futures_account()
    reset_daily_if_needed(acc["total_equity"])
    tg(
        f"🚀 BLOFIN V6.6 STARTED\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"Equity: ${acc['total_equity']:.2f}\n"
        f"Available: ${acc['available']:.2f}\n"
        f"Day start equity: ${daily['start_equity']:.2f}\n"
        f"MIN_SCORE {MIN_SCORE:.0f} | tactics {MIN_TACTICS}/6 | ADX>{ADX_TREND:.0f}\n"
        f"Alt cap L{MAX_ALT_LONGS}/S{MAX_ALT_SHORTS} | pos {MAX_ACTIVE_POSITIONS}\n"
        f"Fee {TAKER_FEE_RATE*100:.2f}%*2 + slip {SLIPPAGE_RATE*100:.2f}%\n"
        f"Daily cap {DAILY_LOSS_LIMIT_PCT:.0f}% from Amsterdam open"
    )


def maybe_score_report(rows):
    global last_score_report
    if time.time() - last_score_report < SCORE_REPORT_MIN * 60:
        return
    last_score_report = time.time()
    ranked = sorted(rows, key=lambda x: x.get("score", 0), reverse=True)[:8]
    lines = [f"📡 V6.6 {session_state()} {now_utc().strftime('%H:%M')} UTC"]
    if not ranked:
        lines.append("geen scans")
    for r in ranked:
        mark = "READY" if not r.get("skip") else r.get("skip")
        lines.append(
            f"{r['symbol'].split('/')[0]} {str(r.get('side') or '-').upper()} "
            f"{r.get('score', 0):.0f} t{r.get('tactics', 0)} "
            f"ADX{r.get('adx', 0):.0f} RS{r.get('rel', 0)*100:+.1f} {mark}"
        )
    tg("\n".join(lines))


def main():
    global protection_lock
    startup_diagnostics()
    if not PAPER_TRADING:
        seed_closed_history()
        try:
            protection_lock = not repair_existing_positions()
        except Exception as e:
            protection_lock = True
            tg(f"🚨 STARTUP PROTECTION FAILED\n{e}")

    while True:
        try:
            if not PAPER_TRADING:
                try:
                    check_for_closed_positions()
                except Exception as e:
                    tg(f"⚠️ PNL TRACKER ERROR\n{e}")

            acc = futures_account()
            ok, reason = check_daily_risk(acc["total_equity"])
            if not ok:
                tg(f"🛑 {reason}")
                time.sleep(120)
                continue
            if time.time() < loss_pause_until:
                time.sleep(SCAN_SECONDS)
                continue

            positions = get_positions()
            if not PAPER_TRADING:
                unprotected = False
                for p in positions:
                    if has_live_protection(p)[0]:
                        continue
                    unprotected = True
                    if not REPAIR_EXISTING_POSITIONS:
                        continue
                    try:
                        symbol = position_symbol(p)
                        side = position_side(p)
                        entry = position_entry(p)
                        df = add_indicators(ohlcv_df(symbol, TIMEFRAME_SIGNAL))
                        atr = float(df.iloc[-2]["atr"])
                        tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
                        levels = calculate_trade_levels(entry, atr, side, tick)
                        if not levels:
                            continue
                        sl, tp, _ = levels
                        last = safe_float(exchange.fetch_ticker(symbol).get("last"))
                        if not tpsl_prices_valid(side, sl, tp, last):
                            continue
                        if place_tpsl_for_position(p, sl, tp, client_tag="watch")[0]:
                            unprotected = False
                            tg(f"🛡️ REPAIRED {symbol} {side.upper()}")
                    except Exception:
                        pass
                protection_lock = unprotected

            if len(positions) >= MAX_ACTIVE_POSITIONS or (protection_lock and not PAPER_TRADING):
                time.sleep(SCAN_SECONDS)
                continue

            try:
                btc = btc_regime()
            except Exception:
                time.sleep(SCAN_SECONDS)
                continue

            scanned, candidates = [], []
            for symbol in SYMBOLS:
                if not cooldown_ok(symbol):
                    continue
                try:
                    signal = evaluate_symbol(symbol, btc)
                    if not signal:
                        continue
                    scanned.append(signal)
                    if not signal.get("skip"):
                        candidates.append(signal)
                except Exception:
                    continue

            maybe_score_report(scanned)
            candidates.sort(key=lambda x: (x["score"], x["tactics"]), reverse=True)

            for signal in candidates:
                current = get_positions()
                if len(current) >= MAX_ACTIVE_POSITIONS:
                    break
                if signal["symbol"] in {position_symbol(p) for p in current}:
                    continue
                alt_l, alt_s = count_alt_sides(current)
                if signal["symbol"] not in MAJORS:
                    if signal["side"] == "long" and alt_l >= MAX_ALT_LONGS:
                        continue
                    if signal["side"] == "short" and alt_s >= MAX_ALT_SHORTS:
                        continue
                open_trade(signal)
                symbol_last_trade[signal["symbol"]] = time.time()
                break

            time.sleep(SCAN_SECONDS)
        except KeyboardInterrupt:
            tg("🛑 V6.6 stopped.")
            raise
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if "152002" in msg:
                time.sleep(20)
                continue
            tg(f"⚠️ V6.6 LOOP ERROR\n{msg}")
            time.sleep(30)


if __name__ == "__main__":
    main()
