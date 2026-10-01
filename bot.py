# LIQUIDITY DAYTRADER v1.0 — new strategy / BloFin execution shell
import os
import time
import json
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone, timedelta

import ccxt
import numpy as np
import pandas as pd
import requests

# ============================================================
# BLOFIN PRECISION STRUCTURE FUSION v6.2 - 4H + 15M LOCATION-FIRST EDITION
# ============================================================
# Strategy engine: faithful Python translation of the supplied TradingView
# Precision Sniper [WillyAlgoTrader] v2.1 defaults for a 15m chart.
#
# Execution shell: inherited from the proven BloFin V6.14 bot for account
# handling, leverage, isolated margin, contract sizing, TP/SL verification,
# Telegram, PnL tracking, cooldowns and daily risk protection. The old V6.14
# EARLY/PULLBACK/MACRO signal engine remains below only as unused support code;
# the active main loop calls ONLY the Precision Sniper engine defined near the
# bottom of this file.
#
# Written for One-way / NET position mode.
# ============================================================

# ---------------- ENV ----------------
API_KEY = os.getenv("EXCHANGE_API_KEY", "").strip()
API_SECRET = os.getenv("EXCHANGE_SECRET", "").strip()
API_PASSWORD = os.getenv("EXCHANGE_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"
LIVE_CONFIRM = os.getenv("LIVE_CONFIRM", "")
BROKER_ID = os.getenv("BLOFIN_BROKER_ID", "").strip()

# ---------------- TIMEFRAMES ----------------
TIMEFRAME_SIGNAL = "15m"
TIMEFRAME_CONFIRM = "5m"
TIMEFRAME_TREND = "1h"
TIMEFRAME_BTC_SWING = "4h"
TIMEFRAME_BTC_MACRO = "1d"
CANDLE_LIMIT = 240
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "45"))

# ---------------- COOLDOWNS ----------------
WIN_COOLDOWN_MIN = int(os.getenv("WIN_COOLDOWN_MIN", "20"))
LOSS_COOLDOWN_MIN = int(os.getenv("LOSS_COOLDOWN_MIN", "60"))
REPEAT_LOSS_COOLDOWN_MIN = int(os.getenv("REPEAT_LOSS_COOLDOWN_MIN", "180"))
LOSS_PAUSE_MIN = int(os.getenv("LOSS_PAUSE_MIN", "90"))

# ---------------- POSITION LIMITS ----------------
MAX_ACTIVE_POSITIONS = int(os.getenv("MAX_ACTIVE_POSITIONS", "4"))
MAX_ALT_LONGS = int(os.getenv("MAX_ALT_LONGS", "1"))
MAX_ALT_SHORTS = int(os.getenv("MAX_ALT_SHORTS", "1"))
MAX_EXECUTION_ATTEMPTS_PER_SCAN = int(os.getenv("MAX_EXECUTION_ATTEMPTS_PER_SCAN", "3"))

# ---------------- V6.12 FLOW OFFENSE ----------------
FLOW_THRESHOLD_BONUS = float(os.getenv("FLOW_THRESHOLD_BONUS", "2"))
FLOW_MIN_BTC_ADX = float(os.getenv("FLOW_MIN_BTC_ADX", "20"))
FLOW_SECOND_ALT_CAP = int(os.getenv("FLOW_SECOND_ALT_CAP", "2"))
FLOW_SECOND_ALT_MIN_CONFIRM = int(os.getenv("FLOW_SECOND_ALT_MIN_CONFIRM", "4"))
FLOW_SECOND_ALT_MIN_MOMENTUM = int(os.getenv("FLOW_SECOND_ALT_MIN_MOMENTUM", "2"))
FLOW_SECOND_ALT_MIN_ENTRYQ = int(os.getenv("FLOW_SECOND_ALT_MIN_ENTRYQ", "3"))
FLOW_SECOND_ALT_MIN_ADX = float(os.getenv("FLOW_SECOND_ALT_MIN_ADX", "22"))

# ---------------- V6.14 MACRO BTC CONTEXT ----------------
MACRO_THRESHOLD_BONUS = float(os.getenv("MACRO_THRESHOLD_BONUS", "2"))
MACRO_PULLBACK_MIN_CONFIRM = int(os.getenv("MACRO_PULLBACK_MIN_CONFIRM", "3"))
MACRO_PULLBACK_MIN_MOMENTUM = int(os.getenv("MACRO_PULLBACK_MIN_MOMENTUM", "2"))
MACRO_PULLBACK_MIN_ENTRYQ = int(os.getenv("MACRO_PULLBACK_MIN_ENTRYQ", "3"))
MACRO_PULLBACK_MIN_ADX = float(os.getenv("MACRO_PULLBACK_MIN_ADX", "22"))
MACRO_PULLBACK_MIN_RS = float(os.getenv("MACRO_PULLBACK_MIN_RS", "0.004"))  # 0.40%

# ---------------- V6.14 COIN LOCATION / ANTI-CHASE ----------------
# Hard ceilings for an already extended move. These are intentionally stricter
# than the old 8-bar filter because a fresh 5m signal must not reset the clock.
COIN_RUN_2H_MAX = float(os.getenv("COIN_RUN_2H_MAX", "0.035"))    # 3.5%
COIN_RUN_4H_MAX = float(os.getenv("COIN_RUN_4H_MAX", "0.050"))    # 5.0%
COIN_RUN_6H_MAX = float(os.getenv("COIN_RUN_6H_MAX", "0.070"))    # 7.0%
COIN_RUN_10H_MAX = float(os.getenv("COIN_RUN_10H_MAX", "0.090"))  # 9.0%
COIN_1H_EMA_EXT_MAX = float(os.getenv("COIN_1H_EMA_EXT_MAX", "0.025"))  # 2.5%
COIN_NEAR_10H_EXTREME = float(os.getenv("COIN_NEAR_10H_EXTREME", "0.015"))  # 1.5%
COIN_LOCATION_MIN_2H_RUN = float(os.getenv("COIN_LOCATION_MIN_2H_RUN", "0.015"))  # 1.5%
LATE_LOCK_MAX_MIN = int(os.getenv("LATE_LOCK_MAX_MIN", "180"))
LATE_LOCK_RESET_ATR = float(os.getenv("LATE_LOCK_RESET_ATR", "0.65"))
LATE_LOCK_MIN_RETRACE_ATR = float(os.getenv("LATE_LOCK_MIN_RETRACE_ATR", "0.75"))

# ---------------- SCORE ----------------
BASE_MIN_SCORE = float(os.getenv("MIN_SCORE", "68"))
HARD_MIN_SCORE = float(os.getenv("HARD_MIN_SCORE", "62"))
STRONG_REGIME_MIN_SCORE = float(os.getenv("STRONG_REGIME_MIN_SCORE", "64"))
MIN_TACTICS = int(os.getenv("MIN_TACTICS", "4"))
STRONG_MIN_TACTICS = int(os.getenv("STRONG_MIN_TACTICS", "4"))

# EARLY route is intentionally not extremely loose.
EARLY_MIN_SCORE = float(os.getenv("EARLY_MIN_SCORE", "60"))
EARLY_MIN_TACTICS = int(os.getenv("EARLY_MIN_TACTICS", "4"))
EARLY_MIN_ADX = float(os.getenv("EARLY_MIN_ADX", "20"))

# ---------------- LEVERAGE / MARGIN ----------------
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")
BASE_MARGIN_USDT = float(os.getenv("BASE_MARGIN_USDT", "10"))
MAX_MARGIN_USDT = float(os.getenv("MAX_MARGIN_USDT", "12"))
MAX_MARGIN_EQUITY_PCT = float(os.getenv("MAX_MARGIN_EQUITY_PCT", "0.18"))
# V6.14: setup-afhankelijke risk cap.
NORMAL_RISK_EQUITY_PCT = float(os.getenv("NORMAL_RISK_EQUITY_PCT", "0.016"))
A_SETUP_RISK_EQUITY_PCT = float(os.getenv("A_SETUP_RISK_EQUITY_PCT", "0.024"))
NORMAL_TARGET_NET_TP_USDT = float(os.getenv("NORMAL_TARGET_NET_TP_USDT", "1.50"))
A_SETUP_TARGET_NET_TP_USDT = float(os.getenv("A_SETUP_TARGET_NET_TP_USDT", "2.50"))
AVAILABLE_MARGIN_BUFFER = float(os.getenv("AVAILABLE_MARGIN_BUFFER", "0.90"))

# ---------------- RISK ----------------
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

# ---------------- MARKET FILTERS ----------------
VOLUME_MULT = float(os.getenv("VOLUME_MULT", "1.20"))
DONCHIAN_LOOKBACK = 12
NEAR_SWEEP_ATR = 0.20
ADX_TREND = float(os.getenv("ADX_TREND", "18"))
ADX_CHOP = float(os.getenv("ADX_CHOP", "14"))
ADX_STRONG = float(os.getenv("ADX_STRONG", "30"))
ADX_VERY_STRONG = float(os.getenv("ADX_VERY_STRONG", "40"))
RSI_LONG_MAX = float(os.getenv("RSI_LONG_MAX", "68"))
RSI_SHORT_MIN = float(os.getenv("RSI_SHORT_MIN", "32"))
RS_OVERRIDE = float(os.getenv("RS_OVERRIDE", "0.008"))
RS_WEAK = float(os.getenv("RS_WEAK", "0.004"))
MAX_EMA_EXT_PCT = float(os.getenv("MAX_EMA_EXT_PCT", "0.012"))
PULLBACK_ATR = float(os.getenv("PULLBACK_ATR", "0.45"))
IMPULSE_15M_ATR = float(os.getenv("IMPULSE_15M_ATR", "1.30"))
IMPULSE_1H_ATR = float(os.getenv("IMPULSE_1H_ATR", "1.60"))
MAX_RUN_PCT = float(os.getenv("MAX_RUN_PCT", "0.035"))
MAX_FILL_SLIP_PCT = float(os.getenv("MAX_FILL_SLIP_PCT", "0.004"))
# Alleen een premium EARLY rejection mag iets meer live drift hebben.
EARLY_MAX_FILL_SLIP_PCT = float(os.getenv("EARLY_MAX_FILL_SLIP_PCT", "0.0075"))

# ---------------- NEW TARGETED CONFLICT FILTERS ----------------
# Mild conflict: CRV example around +0.3% while short.
# Strong conflict: ETC example around -1.82% while long.
RS_CONFLICT_MILD = float(os.getenv("RS_CONFLICT_MILD", "0.0025"))   # 0.25%
RS_CONFLICT_HARD = float(os.getenv("RS_CONFLICT_HARD", "0.0100"))   # 1.00%

# For trades against BTC / RS we demand a real rejection, not just score.
CONFLICT_REQUIRE_CONFIRM = int(os.getenv("CONFLICT_REQUIRE_CONFIRM", "4"))

# EARLY rejection mechanics.
EARLY_WICK_BODY_RATIO = float(os.getenv("EARLY_WICK_BODY_RATIO", "0.65"))
EARLY_EMA_DISTANCE_ATR = float(os.getenv("EARLY_EMA_DISTANCE_ATR", "0.65"))
EARLY_SWING_LOOKBACK_5M = int(os.getenv("EARLY_SWING_LOOKBACK_5M", "6"))

# Impulse memory: do not jump in late after a large candle.
IMPULSE_MEMORY_BARS = int(os.getenv("IMPULSE_MEMORY_BARS", "4"))
IMPULSE_RESET_ATR = float(os.getenv("IMPULSE_RESET_ATR", "0.65"))

# ---------------- DAILY RISK ----------------
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "5"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
MIN_EQUITY_USDT = float(os.getenv("MIN_EQUITY_USDT", "30"))
REPAIR_EXISTING_POSITIONS = os.getenv("REPAIR_EXISTING_POSITIONS", "true").lower() == "true"
SCORE_REPORT_MIN = int(os.getenv("SCORE_REPORT_MIN", "30"))

# ---------------- LEARNING ----------------
LEARNING_FILE = os.getenv("LEARNING_FILE", "v610_learning.json")
LEARNING_MIN_TRADES = int(os.getenv("LEARNING_MIN_TRADES", "12"))
LEARNING_MAX_BONUS = float(os.getenv("LEARNING_MAX_BONUS", "3.0"))
LEARNING_MAX_PENALTY = float(os.getenv("LEARNING_MAX_PENALTY", "-3.0"))

# ---------------- SESSION ----------------
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

# ============================================================
# HELPERS
# ============================================================
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


def safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        x = float(v)
        return x if np.isfinite(x) else default
    except Exception:
        return default


def native(method_name, params=None):
    fn = getattr(exchange, method_name, None)
    if fn is None:
        raise RuntimeError(f"CCXT implicit endpoint missing: {method_name}")
    return fn(params or {})


def native_ok(resp):
    return isinstance(resp, dict) and str(resp.get("code")) == "0"


def response_error(resp):
    if not isinstance(resp, dict):
        return str(resp)
    code = str(resp.get("code", "?"))
    msg = str(resp.get("msg", ""))
    data = resp.get("data")
    if isinstance(data, dict):
        if data.get("code") not in (None, "", "0", 0):
            code = str(data.get("code"))
        if data.get("msg"):
            msg = str(data.get("msg"))
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            item_code = str(item.get("code"))
            if item_code not in ("0", "None", ""):
                code = item_code
                msg = str(item.get("msg") or msg)
                break
    return f"{code} {msg}".strip()


def blofin_inst_id(symbol):
    return symbol.split("/")[0] + "-USDT"


def floor_step(value, step):
    v, s = Decimal(str(value)), Decimal(str(step))
    if s <= 0:
        return float(v)
    return float((v / s).to_integral_value(rounding=ROUND_DOWN) * s)


def ceil_step(value, step):
    v, s = Decimal(str(value)), Decimal(str(step))
    if s <= 0:
        return float(v)
    return float((v / s).to_integral_value(rounding=ROUND_UP) * s)


def round_trade_price(price, tick, side, kind):
    if side == "long":
        return floor_step(price, tick) if kind == "sl" else ceil_step(price, tick)
    return ceil_step(price, tick) if kind == "sl" else floor_step(price, tick)


def get_instrument(symbol):
    inst = blofin_inst_id(symbol)
    resp = native("public_get_market_instruments", {"instId": inst})
    if not native_ok(resp):
        raise RuntimeError(f"Instrument lookup failed {inst}: {response_error(resp)}")
    data = resp.get("data") or []
    if not data:
        raise RuntimeError(f"No instrument metadata for {inst}")
    return data[0]


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
    return {"total_equity": total_equity, "equity": equity or total_equity,
            "balance": balance, "available": available}


def get_positions():
    resp = native("private_get_account_positions", {})
    if not native_ok(resp):
        raise RuntimeError(f"Positions API failed: {response_error(resp)}")
    out = []
    for p in resp.get("data") or []:
        pos_size = safe_float(p.get("positions", p.get("position", p.get("size", 0))))
        if abs(pos_size) > 0:
            out.append(p)
    return out


def position_symbol(p):
    return str(p.get("instId", "")).replace("-USDT", "/USDT:USDT")


def position_side(p):
    side = str(p.get("positionSide", "net")).lower()
    if side in ("long", "short"):
        return side
    pos = safe_float(p.get("positions", p.get("position", p.get("size", 0))))
    return "long" if pos > 0 else "short"


def position_entry(p):
    return safe_float(p.get("averagePrice", p.get("entryPrice", p.get("avgPrice", 0))))


def position_margin_mode(p):
    return str(p.get("marginMode", MARGIN_MODE)).lower()


def position_id(p):
    return str(p.get("positionId", "") or "")

# ============================================================
# DATA / INDICATORS
# ============================================================
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
    out["ema50_slope"] = out["ema50"] - out["ema50"].shift(3)

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
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    out["adx"] = wilder_smooth(dx, ADX_PERIOD)
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di

    out["vol_sma20"] = out["volume"].rolling(20).mean()
    out["donchian_high"] = out["high"].rolling(DONCHIAN_LOOKBACK).max().shift(1)
    out["donchian_low"] = out["low"].rolling(DONCHIAN_LOOKBACK).min().shift(1)
    out["ret1"] = out["close"].pct_change(1)
    out["ret3"] = out["close"].pct_change(3)
    out["ret6"] = out["close"].pct_change(6)
    return out


def session_state():
    hour = now_utc().hour
    if hour < NIGHT_END_UTC:
        return "NIGHT"
    if CORE_START_UTC <= hour < CORE_END_UTC:
        return "CORE"
    return "SHOULDER"

# ============================================================
# BTC REGIME
# ============================================================
btc_cache = {"ts": 0, "data": None}


def btc_regime():
    """
    V6.14 BTC hierarchy:
      1D + 4H = macro direction
      1H      = tradable regime
      15m     = immediate trigger / pullback

    This prevents a normal 15m pullback inside a larger uptrend from being
    treated exactly like a fully bearish BTC market.
    """
    now = time.time()
    if btc_cache["data"] and now - btc_cache["ts"] < 60:
        return btc_cache["data"]

    df15 = add_indicators(ohlcv_df(BTC_SYMBOL, TIMEFRAME_SIGNAL))
    df1h = add_indicators(ohlcv_df(BTC_SYMBOL, TIMEFRAME_TREND))
    df4h = add_indicators(ohlcv_df(BTC_SYMBOL, TIMEFRAME_BTC_SWING))
    df1d = add_indicators(ohlcv_df(BTC_SYMBOL, TIMEFRAME_BTC_MACRO))
    a, t, h4, d1 = df15.iloc[-2], df1h.iloc[-2], df4h.iloc[-2], df1d.iloc[-2]

    oneh_bull = bool(t["ema20"] > t["ema50"])
    oneh_bear = bool(t["ema20"] < t["ema50"])
    stack_long = bool(t["ema20"] > t["ema50"] > t["ema200"])
    stack_short = bool(t["ema20"] < t["ema50"] < t["ema200"])

    h4_bull = bool(h4["ema20"] > h4["ema50"])
    h4_bear = bool(h4["ema20"] < h4["ema50"])
    d1_bull = bool(d1["ema20"] > d1["ema50"])
    d1_bear = bool(d1["ema20"] < d1["ema50"])
    h4_stack_long = bool(h4["ema20"] > h4["ema50"] > h4["ema200"])
    h4_stack_short = bool(h4["ema20"] < h4["ema50"] < h4["ema200"])
    d1_stack_long = bool(d1["ema20"] > d1["ema50"] > d1["ema200"])
    d1_stack_short = bool(d1["ema20"] < d1["ema50"] < d1["ema200"])

    macro_bull = bool(d1_bull and h4_bull)
    macro_bear = bool(d1_bear and h4_bear)
    macro_strong_long = bool(macro_bull and (d1_stack_long or h4_stack_long))
    macro_strong_short = bool(macro_bear and (d1_stack_short or h4_stack_short))

    impulse_up = bool(a["close"] > a["ema20"] and a["ema20"] >= a["ema50"])
    impulse_down = bool(a["close"] < a["ema20"] and a["ema20"] <= a["ema50"])
    ret15 = safe_float(a["ret1"])
    ret45 = safe_float(a["ret3"])

    # Pullback = macro and 1h still agree, but 15m temporarily moves against them.
    macro_pullback_long = bool(macro_bull and oneh_bull and (impulse_down or ret15 < 0))
    macro_pullback_short = bool(macro_bear and oneh_bear and (impulse_up or ret15 > 0))

    data = {
        "bull": oneh_bull, "bear": oneh_bear,
        "stack_long": stack_long, "stack_short": stack_short,
        "impulse_up": impulse_up, "impulse_down": impulse_down,
        "ret15": ret15, "ret45": ret45, "ret90": safe_float(a["ret6"]),
        "adx": safe_float(a["adx"]), "close": safe_float(a["close"]),
        "h4_bull": h4_bull, "h4_bear": h4_bear,
        "d1_bull": d1_bull, "d1_bear": d1_bear,
        "macro_bull": macro_bull, "macro_bear": macro_bear,
        "macro_strong_long": macro_strong_long,
        "macro_strong_short": macro_strong_short,
        "macro_pullback_long": macro_pullback_long,
        "macro_pullback_short": macro_pullback_short,
        "adx1h": safe_float(t["adx"]),
        "adx4h": safe_float(h4["adx"]),
    }
    btc_cache["ts"], btc_cache["data"] = now, data
    return data

# ============================================================
# LEARNING
# ============================================================
learning = {"version": 3, "trades": 0, "wins": 0, "losses": 0, "fingerprints": {}, "symbols": {}}


def load_learning():
    global learning
    if not os.path.exists(LEARNING_FILE):
        return
    try:
        with open(LEARNING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            learning.update(data)
    except Exception as e:
        tg(f"⚠️ Learning file ignored: {e}")


def save_learning():
    try:
        tmp = LEARNING_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(learning, f, indent=2)
        os.replace(tmp, LEARNING_FILE)
    except Exception:
        pass


def fingerprint(signal):
    adx = safe_float(signal.get("adx"))
    adx_bucket = "very_strong" if adx >= 40 else "strong" if adx >= 30 else "trend" if adx >= 22 else "weak"
    structure = signal.get("entry_route", "WAIT").lower()
    return "|".join([
        str(signal.get("side", "?")), adx_bucket, structure,
        str(signal.get("btc_regime", "neutral")), str(signal.get("session", "?")),
    ])


def learning_adjustment(signal):
    if learning.get("trades", 0) < LEARNING_MIN_TRADES:
        return 0.0
    rec = learning.get("fingerprints", {}).get(fingerprint(signal))
    if not rec or int(rec.get("trades", 0)) < 5:
        return 0.0
    trades, wins, net = int(rec.get("trades", 0)), int(rec.get("wins", 0)), safe_float(rec.get("net", 0))
    wr = wins / trades
    if wr >= 0.70 and net > 0:
        return min(LEARNING_MAX_BONUS, 2.0)
    if wr <= 0.35 and net < 0:
        return max(LEARNING_MAX_PENALTY, -2.0)
    return 0.0


def adaptive_threshold(signal):
    threshold = BASE_MIN_SCORE
    adx = safe_float(signal.get("adx"))
    confirm = int(signal.get("confirm_components", 0))
    momentum = safe_float(signal.get("momentum_quality", 0))
    entry_quality = safe_float(signal.get("entry_quality", 0))
    if adx >= ADX_VERY_STRONG and confirm >= 2 and momentum >= 2 and entry_quality >= 2:
        threshold = min(threshold, STRONG_REGIME_MIN_SCORE)
    elif adx >= ADX_STRONG and confirm >= 3 and momentum >= 2:
        threshold = max(HARD_MIN_SCORE, threshold - 2)
    # V6.14: offensiever wanneer BTC en setup dezelfde kant op stromen.
    # Niet blind: flow-bonus vereist nog steeds degelijke lokale bevestiging.
    if (signal.get("btc_flow_strong") and confirm >= 3
            and momentum >= 2 and entry_quality >= 3):
        threshold = max(HARD_MIN_SCORE, threshold - FLOW_THRESHOLD_BONUS)
    # V6.14: macro-supportive BTC pullback may lower the threshold, but only
    # for a locally healthy setup. This is context, not a blind dip-buy rule.
    if (signal.get("btc_macro_pullback_support") and confirm >= MACRO_PULLBACK_MIN_CONFIRM
            and momentum >= MACRO_PULLBACK_MIN_MOMENTUM
            and entry_quality >= MACRO_PULLBACK_MIN_ENTRYQ):
        threshold = max(HARD_MIN_SCORE, threshold - MACRO_THRESHOLD_BONUS)
    threshold -= learning_adjustment(signal)
    return max(HARD_MIN_SCORE, threshold)

# ============================================================
# ENTRY HELPERS - V6.12 TARGETED CHANGES
# ============================================================
def candle_rejection(row, side):
    """True only for a meaningful wick/rejection candle, not every red/green bar."""
    o, h, l, c = map(safe_float, [row["open"], row["high"], row["low"], row["close"]])
    rng = max(h - l, 1e-12)
    body = max(abs(c - o), rng * 0.08)
    upper = h - max(o, c)
    lower = min(o, c) - l
    if side == "short":
        return upper >= body * EARLY_WICK_BODY_RATIO and c < (h + l) / 2
    return lower >= body * EARLY_WICK_BODY_RATIO and c > (h + l) / 2


def micro_break(df5, side):
    """Uses closed 5m candles only."""
    c = df5.iloc[-2]
    p = df5.iloc[-3]
    if side == "short":
        return bool(c["close"] < p["low"] or (c["close"] < p["close"] and c["close"] < c["ema20"]))
    return bool(c["close"] > p["high"] or (c["close"] > p["close"] and c["close"] > c["ema20"]))


def recent_5m_swing_touch(df5, side, atr5):
    bars = df5.iloc[-(EARLY_SWING_LOOKBACK_5M + 2):-2]
    c = df5.iloc[-2]
    if bars.empty:
        return False
    if side == "short":
        local_high = safe_float(bars["high"].max())
        return safe_float(c["high"]) >= local_high - 0.35 * atr5 or safe_float(c["high"]) >= safe_float(c["ema20"])
    local_low = safe_float(bars["low"].min())
    return safe_float(c["low"]) <= local_low + 0.35 * atr5 or safe_float(c["low"]) <= safe_float(c["ema20"])


def rs_conflict(side, rel):
    signed_against = -rel if side == "long" else rel
    return {
        "mild": signed_against >= RS_CONFLICT_MILD,
        "hard": signed_against >= RS_CONFLICT_HARD,
        "against": signed_against,
    }


def btc_conflict(side, btc):
    # Raw short-term conflict. V6.14 may later classify this as a macro pullback
    # and allow a carefully verified continuation setup.
    if side == "long":
        return bool(btc["impulse_down"] or btc["bear"])
    return bool(btc["impulse_up"] or btc["bull"])


def btc_macro_context(side, btc):
    """Return macro support/opposition and whether BTC is in a healthy pullback."""
    if side == "long":
        supportive = bool(btc.get("macro_bull"))
        opposed = bool(btc.get("macro_bear"))
        pullback = bool(btc.get("macro_pullback_long"))
        strong = bool(btc.get("macro_strong_long"))
    else:
        supportive = bool(btc.get("macro_bear"))
        opposed = bool(btc.get("macro_bull"))
        pullback = bool(btc.get("macro_pullback_short"))
        strong = bool(btc.get("macro_strong_short"))
    return supportive, opposed, pullback, strong


def btc_flow_state(side, btc):
    """
    V6.12: onderscheid tussen simpelweg niet-tegen-BTC en echt MET BTC mee.
    strong vraagt trend + korte impuls/stack + 45m richting + voldoende BTC ADX.
    """
    btc_adx = safe_float(btc.get("adx"))
    ret45 = safe_float(btc.get("ret45"))
    if side == "long":
        aligned = bool((btc.get("bull") or btc.get("stack_long") or btc.get("impulse_up"))
                       and not btc.get("impulse_down"))
        strong = bool(
            aligned
            and (btc.get("stack_long") or (btc.get("bull") and btc.get("impulse_up")))
            and ret45 > 0
            and btc_adx >= FLOW_MIN_BTC_ADX
        )
    else:
        aligned = bool((btc.get("bear") or btc.get("stack_short") or btc.get("impulse_down"))
                       and not btc.get("impulse_up"))
        strong = bool(
            aligned
            and (btc.get("stack_short") or (btc.get("bear") and btc.get("impulse_down")))
            and ret45 < 0
            and btc_adx >= FLOW_MIN_BTC_ADX
        )
    return aligned, strong


def recent_impulse_state(df15, side, atr):
    """
    Detects whether a large directional 15m impulse happened recently.
    The latest closed bar is excluded from the memory search so a new rejection
    can be evaluated separately.
    """
    start = -(IMPULSE_MEMORY_BARS + 3)
    bars = df15.iloc[start:-2]
    if bars.empty:
        return False
    for _, r in bars.iterrows():
        rng = safe_float(r["high"]) - safe_float(r["low"])
        body = safe_float(r["close"]) - safe_float(r["open"])
        if side == "long" and body > 0 and rng >= IMPULSE_15M_ATR * atr:
            return True
        if side == "short" and body < 0 and rng >= IMPULSE_15M_ATR * atr:
            return True
    return False

def coin_run_metrics(df1h, price):
    """Closed-candle 1H location metrics for 2/4/6/10 hour anti-chase logic."""
    def ret_hours(hours):
        idx = -(hours + 2)
        if len(df1h) < hours + 3:
            return 0.0
        base = safe_float(df1h["close"].iloc[idx])
        return price / base - 1.0 if base > 0 else 0.0

    closed = df1h.iloc[-12:-1] if len(df1h) >= 13 else df1h.iloc[:-1]
    high10 = safe_float(closed["high"].max(), price) if not closed.empty else price
    low10 = safe_float(closed["low"].min(), price) if not closed.empty else price
    ema20_1h = safe_float(df1h.iloc[-2]["ema20"], price)
    ema_ext = price / ema20_1h - 1.0 if ema20_1h > 0 else 0.0
    return {
        "ret2h": ret_hours(2), "ret4h": ret_hours(4),
        "ret6h": ret_hours(6), "ret10h": ret_hours(10),
        "high10h": high10, "low10h": low10, "ema_ext_1h": ema_ext,
    }


def coin_location_block(side, metrics, price):
    """Hard V6.14 chase guard. Strong score/ADX is NOT allowed to bypass it."""
    r2, r4 = metrics["ret2h"], metrics["ret4h"]
    r6, r10 = metrics["ret6h"], metrics["ret10h"]
    ema_ext = metrics["ema_ext_1h"]
    if side == "long":
        hard_run = (r2 >= COIN_RUN_2H_MAX or r4 >= COIN_RUN_4H_MAX
                    or r6 >= COIN_RUN_6H_MAX or r10 >= COIN_RUN_10H_MAX)
        near_high = metrics["high10h"] > 0 and price >= metrics["high10h"] * (1.0 - COIN_NEAR_10H_EXTREME)
        bad_location = ema_ext >= COIN_1H_EMA_EXT_MAX and near_high and r2 >= COIN_LOCATION_MIN_2H_RUN
    else:
        hard_run = (r2 <= -COIN_RUN_2H_MAX or r4 <= -COIN_RUN_4H_MAX
                    or r6 <= -COIN_RUN_6H_MAX or r10 <= -COIN_RUN_10H_MAX)
        near_low = metrics["low10h"] > 0 and price <= metrics["low10h"] * (1.0 + COIN_NEAR_10H_EXTREME)
        bad_location = ema_ext <= -COIN_1H_EMA_EXT_MAX and near_low and r2 <= -COIN_LOCATION_MIN_2H_RUN
    return bool(hard_run or bad_location), bool(hard_run), bool(bad_location)


def late_lock_active(symbol, side, price, ema20_15, atr, pullback_quality, impulse_15):
    """A failed too-late execution remains blocked until a real pullback/reset."""
    rec = late_entry_locks.get(symbol)
    if not rec or rec.get("side") != side:
        return False
    age = time.time() - safe_float(rec.get("ts"))
    if age >= LATE_LOCK_MAX_MIN * 60:
        late_entry_locks.pop(symbol, None)
        return False
    lock_price = safe_float(rec.get("price"), price)
    if side == "long":
        retraced = price <= lock_price - LATE_LOCK_MIN_RETRACE_ATR * atr
    else:
        retraced = price >= lock_price + LATE_LOCK_MIN_RETRACE_ATR * atr
    reset = bool(
        retraced
        and pullback_quality >= 2
        and abs(price - ema20_15) <= LATE_LOCK_RESET_ATR * atr
        and not impulse_15
    )
    if reset:
        late_entry_locks.pop(symbol, None)
        return False
    return True

# ============================================================
# EVALUATE SYMBOL
# ============================================================
def evaluate_symbol(symbol, btc):
    sess = session_state()
    if sess == "NIGHT" and symbol not in MAJORS:
        return {"symbol": symbol, "side": None, "score": 0, "tactics": 0,
                "threshold": BASE_MIN_SCORE, "skip": "night-alts", "price": 0,
                "atr": 0, "entry_quality": 0, "confirm_components": 0,
                "adx": 0, "rel": 0, "entry_route": "WAIT"}

    df15 = add_indicators(ohlcv_df(symbol, TIMEFRAME_SIGNAL))
    df5 = add_indicators(ohlcv_df(symbol, TIMEFRAME_CONFIRM))
    df1h = add_indicators(ohlcv_df(symbol, TIMEFRAME_TREND))
    a, a_prev = df15.iloc[-2], df15.iloc[-3]
    c, c_prev = df5.iloc[-2], df5.iloc[-3]
    t = df1h.iloc[-2]

    price, atr = safe_float(a["close"]), safe_float(a["atr"])
    if price <= 0 or atr <= 0:
        return None
    atr_pct = atr / price
    rsi, adx = safe_float(a["rsi"], 50), safe_float(a["adx"])

    # Relative strength 15/45/90m.
    rel15 = safe_float(a["ret1"]) - btc["ret15"]
    rel45 = safe_float(a["ret3"]) - btc["ret45"]
    rel90 = safe_float(a["ret6"]) - btc["ret90"]
    rel = rel15 * 0.50 + rel45 * 0.30 + rel90 * 0.20
    rs_persistent_long = rel15 > 0 and rel45 > 0
    rs_persistent_short = rel15 < 0 and rel45 < 0

    ema20_15, ema50_15 = safe_float(a["ema20"]), safe_float(a["ema50"])
    ema20_5 = safe_float(c["ema20"])
    ema20_slope = safe_float(a["ema20_slope"])
    ema20_slope_pct = ema20_slope / price

    stack_long = t["ema20"] > t["ema50"] > t["ema200"]
    stack_short = t["ema20"] < t["ema50"] < t["ema200"]
    fast_long, fast_short = t["ema20"] > t["ema50"], t["ema20"] < t["ema50"]
    trend_long = ema20_15 > ema50_15 and ema20_slope > 0
    trend_short = ema20_15 < ema50_15 and ema20_slope < 0

    volume_ok = pd.notna(a["vol_sma20"]) and a["volume"] > a["vol_sma20"] * VOLUME_MULT

    dist_ema = (price - ema20_15) / price
    abs_dist_ema = abs(dist_ema)
    near_ema = abs_dist_ema <= PULLBACK_ATR * atr / price
    touched_ema_long = min(safe_float(a["low"]), safe_float(a_prev["low"])) <= ema20_15 + PULLBACK_ATR * atr
    touched_ema_short = max(safe_float(a["high"]), safe_float(a_prev["high"])) >= ema20_15 - PULLBACK_ATR * atr
    pullback_long = near_ema or touched_ema_long
    pullback_short = near_ema or touched_ema_short

    ext_long, ext_short = dist_ema > MAX_EMA_EXT_PCT, dist_ema < -MAX_EMA_EXT_PCT
    bar15_range = safe_float(a["high"]) - safe_float(a["low"])
    bar1h_range = safe_float(t["high"]) - safe_float(t["low"])
    atr1h = safe_float(t["atr"], atr)
    impulse_15 = bar15_range > IMPULSE_15M_ATR * atr
    impulse_1h = bar1h_range > IMPULSE_1H_ATR * atr1h

    old_close = safe_float(df15["close"].iloc[-10]) if len(df15) >= 10 else 0
    run8 = price / old_close - 1 if old_close > 0 else 0.0
    coin_run = coin_run_metrics(df1h, price)

    recent_low = df15["low"].iloc[-DONCHIAN_LOOKBACK - 3:-3].min()
    recent_high = df15["high"].iloc[-DONCHIAN_LOOKBACK - 3:-3].max()
    sweep_long = safe_float(a_prev["low"]) < recent_low - NEAR_SWEEP_ATR * atr and price > recent_low
    sweep_short = safe_float(a_prev["high"]) > recent_high + NEAR_SWEEP_ATR * atr and price < recent_high
    structure_long = (pullback_long and trend_long) or sweep_long
    structure_short = (pullback_short and trend_short) or sweep_short

    # 5m confirmation.
    c_atr = safe_float(c["atr"], atr)
    bounce_5m = safe_float(c["low"]) <= ema20_5 + 0.35 * c_atr
    reject_5m = safe_float(c["high"]) >= ema20_5 - 0.35 * c_atr
    cl, cs = 0, 0
    if c["ema20"] > c["ema50"]: cl += 1
    if c["ema20"] < c["ema50"]: cs += 1
    if c["close"] > c["ema20"]: cl += 1
    if c["close"] < c["ema20"]: cs += 1
    if c["close"] > c_prev["close"]: cl += 1
    if c["close"] < c_prev["close"]: cs += 1
    if bounce_5m: cl += 1
    if reject_5m: cs += 1
    confirm_long, confirm_short = cl >= 3, cs >= 3

    atr_quality = 0.0018 <= atr_pct <= 0.035
    trending, strong_trend, very_strong_trend = adx >= ADX_TREND, adx >= ADX_STRONG, adx >= ADX_VERY_STRONG
    choppy = adx < ADX_CHOP

    mql = mqs = 0
    if ema20_slope_pct > 0.0003: mql += 1
    if ema20_slope_pct < -0.0003: mqs += 1
    if a["close"] > a_prev["close"]: mql += 1
    if a["close"] < a_prev["close"]: mqs += 1
    if btc["ret15"] > 0: mql += 1
    if btc["ret15"] < 0: mqs += 1

    rs_long, rs_strong_long = rel > RS_WEAK, rel > RS_OVERRIDE
    rs_short, rs_strong_short = rel < -RS_WEAK, rel < -RS_OVERRIDE

    # Score remains close to V6.9/V6.8.
    long_score = short_score = 0.0
    long_tactics = short_tactics = 0
    if stack_long: long_score += 16; long_tactics += 1
    elif fast_long: long_score += 7
    if stack_short: short_score += 16; short_tactics += 1
    elif fast_short: short_score += 7
    if trend_long: long_score += 12; long_tactics += 1
    if trend_short: short_score += 12; short_tactics += 1
    if volume_ok: long_score += 6; short_score += 6
    if structure_long: long_score += 17 if sweep_long else 11; long_tactics += 1
    if structure_short: short_score += 17 if sweep_short else 11; short_tactics += 1
    if confirm_long: long_score += 15 if cl >= 4 else 11; long_tactics += 1
    if confirm_short: short_score += 15 if cs >= 4 else 11; short_tactics += 1
    if atr_quality: long_score += 4; short_score += 4
    if trending: long_score += 5; short_score += 5
    if strong_trend: long_score += 3; short_score += 3
    if very_strong_trend: long_score += 3; short_score += 3
    if mql >= 2: long_score += 4; long_tactics += 1
    if mqs >= 2: short_score += 4; short_tactics += 1
    if rs_strong_long: long_score += 6; long_tactics += 1
    elif rs_long and rs_persistent_long: long_score += 3; long_tactics += 1
    if rs_strong_short: short_score += 6; short_tactics += 1
    elif rs_short and rs_persistent_short: short_score += 3; short_tactics += 1

    if long_score >= short_score:
        side, score, tactics = "long", long_score, long_tactics
        confirm_components, structure, momentum_quality = cl, structure_long, mql
        pullback_quality = 3 if near_ema else 2 if touched_ema_long else 0
        sweep, side_extended = sweep_long, ext_long
        side_trend, side_pullback = trend_long, pullback_long
    else:
        side, score, tactics = "short", short_score, short_tactics
        confirm_components, structure, momentum_quality = cs, structure_short, mqs
        pullback_quality = 3 if near_ema else 2 if touched_ema_short else 0
        sweep, side_extended = sweep_short, ext_short
        side_trend, side_pullback = trend_short, pullback_short

    entry_quality = 0
    if pullback_quality >= 3: entry_quality += 2
    elif pullback_quality >= 2: entry_quality += 1
    if not side_extended: entry_quality += 1
    if not (impulse_15 or impulse_1h): entry_quality += 1
    if abs_dist_ema < 0.008: entry_quality += 1

    btc_macro_supportive, btc_macro_opposed, btc_macro_pullback, btc_macro_strong = btc_macro_context(side, btc)
    if side == "long" and btc_macro_pullback:
        btc_label = "BTC_BULL_PULLBACK"
    elif side == "short" and btc_macro_pullback:
        btc_label = "BTC_BEAR_RALLY"
    elif btc["stack_long"]: btc_label = "BTC_BULL"
    elif btc["stack_short"]: btc_label = "BTC_BEAR"
    elif btc["bull"]: btc_label = "BTC_UP"
    elif btc["bear"]: btc_label = "BTC_DOWN"
    else: btc_label = "BTC_NEUTRAL"

    btc_flow_aligned, btc_flow_strong = btc_flow_state(side, btc)
    flow_rs_supportive = bool(rel >= 0) if side == "long" else bool(rel <= 0)

    preliminary = {
        "side": side, "adx": adx, "confirm_components": confirm_components,
        "momentum_quality": momentum_quality, "entry_quality": entry_quality,
        "pullback_quality": pullback_quality, "sweep": sweep,
        "btc_regime": btc_label, "session": sess, "entry_route": "WAIT",
        "btc_flow_aligned": btc_flow_aligned, "btc_flow_strong": btc_flow_strong,
        "flow_rs_supportive": flow_rs_supportive,
        "btc_macro_supportive": btc_macro_supportive,
        "btc_macro_opposed": btc_macro_opposed,
        "btc_macro_pullback_support": btc_macro_pullback,
        "btc_macro_strong": btc_macro_strong,
    }
    threshold = adaptive_threshold(preliminary)
    if sess == "SHOULDER":
        score -= 4

    # --------------------------------------------------------
    # V6.12: EARLY PULLBACK FAILURE
    # --------------------------------------------------------
    rejection = candle_rejection(c, side)
    m_break = micro_break(df5, side)
    swing_touch = recent_5m_swing_touch(df5, side, c_atr)
    close_to_ema = abs(safe_float(c["close"]) - ema20_5) <= EARLY_EMA_DISTANCE_ATR * c_atr

    # We want a real failed pullback: price interacted with EMA/local swing,
    # then rejection + micro break. This can trigger before full PB confirmation.
    early_rejection = bool(
        side_trend
        and adx >= EARLY_MIN_ADX
        and tactics >= EARLY_MIN_TACTICS
        and score >= EARLY_MIN_SCORE
        and momentum_quality >= 1
        and (swing_touch or close_to_ema or side_pullback)
        and rejection
        and m_break
        and not side_extended
        and not impulse_15
        and atr_quality
    )

    # Impulse memory / reset.
    recent_impulse = recent_impulse_state(df15, side, atr)
    impulse_reset = bool(
        recent_impulse
        and pullback_quality >= 2
        and abs(price - ema20_15) <= IMPULSE_RESET_ATR * atr
        and not impulse_15
    )

    # V6.14: individual coin location. A fresh 5m setup does not erase a
    # 2-10h run. This is a hard chase guard, unlike the older soft run8 rule.
    htf_location_block, htf_hard_run, htf_bad_location = coin_location_block(side, coin_run, price)
    late_lock = late_lock_active(
        symbol, side, price, ema20_15, atr, pullback_quality, impulse_15
    )

    # Normal PB route.
    required_tactics = STRONG_MIN_TACTICS if very_strong_trend else MIN_TACTICS
    pullback_route = bool(
        score >= threshold
        and tactics >= required_tactics
        and trending
        and atr_quality
        and structure
        and pullback_quality >= 2
        and confirm_components >= 3
        and not side_extended
        and not impulse_15
        and (not recent_impulse or impulse_reset)
    )

    # --------------------------------------------------------
    # V6.12: CONFLICT GATES
    # --------------------------------------------------------
    rsx = rs_conflict(side, rel)
    btc_against = symbol not in MAJORS and btc_conflict(side, btc)

    # Mild conflict requires PERFECT 5m confirmation AND explicit rejection.
    conflict_confirmation_ok = bool(
        confirm_components >= CONFLICT_REQUIRE_CONFIRM
        and rejection
        and m_break
    )

    # Hard RS conflict is blocked completely. Example: LONG with RS -1.82%.
    hard_rs_block = rsx["hard"]

    # Mild RS conflict can trade only if the reversal is extremely clear.
    mild_rs_block = rsx["mild"] and not conflict_confirmation_ok

    # V6.14 macro-pullback override. We only relax the short-term BTC conflict
    # when the larger BTC trend supports the trade AND the alt itself is strong.
    signed_rs_support = rel if side == "long" else -rel
    macro_pullback_override_ok = bool(
        btc_against
        and btc_macro_pullback
        and not btc_macro_opposed
        and not hard_rs_block
        and signed_rs_support >= MACRO_PULLBACK_MIN_RS
        and confirm_components >= MACRO_PULLBACK_MIN_CONFIRM
        and momentum_quality >= MACRO_PULLBACK_MIN_MOMENTUM
        and entry_quality >= MACRO_PULLBACK_MIN_ENTRYQ
        and adx >= MACRO_PULLBACK_MIN_ADX
        and m_break
        and not side_extended
        and not impulse_15
    )

    # True BTC conflict stays strict. A healthy macro pullback may pass via the
    # dedicated override above; otherwise V6.12's 4/4+rejection gate remains.
    btc_block = btc_against and not macro_pullback_override_ok and (not conflict_confirmation_ok or hard_rs_block)

    # RSI / run safety remains.
    rsi_block = False
    if side == "long" and rsi >= RSI_LONG_MAX and not (very_strong_trend and entry_quality >= 2):
        rsi_block = True
    if side == "short" and rsi <= RSI_SHORT_MIN and not (very_strong_trend and entry_quality >= 2):
        rsi_block = True

    run_block = (side == "long" and run8 > MAX_RUN_PCT) or (side == "short" and run8 < -MAX_RUN_PCT)
    if run_block and very_strong_trend and pullback_quality >= 2:
        run_block = False

    # Route selection: EARLY first if genuine rejection exists; otherwise PB.
    entry_route = "WAIT"
    if early_rejection:
        entry_route = "EARLY"
    elif pullback_route:
        entry_route = "PULLBACK"

    # Preserve the underlying EARLY/PULLBACK logic, but expose the reason why a
    # short-term BTC conflict was allowed: larger-trend continuation.
    if macro_pullback_override_ok and entry_route != "WAIT":
        entry_route = "MACRO"

    skip_reasons = []
    if choppy: skip_reasons.append("adx-chop")
    elif not trending and not early_rejection: skip_reasons.append(f"adx-{adx:.0f}<{ADX_TREND:.0f}")
    if hard_rs_block: skip_reasons.append("hard-rs-conflict")
    elif mild_rs_block: skip_reasons.append("rs-conflict-needs-4/4-reject")
    if btc_block: skip_reasons.append("btc-conflict-needs-4/4-reject")
    if rsi_block: skip_reasons.append("rsi-overextended")
    if side_extended: skip_reasons.append("extended")
    if htf_location_block:
        if htf_hard_run:
            skip_reasons.append("coin-2to10h-run")
        else:
            skip_reasons.append("coin-1h-bad-location")
    if late_lock: skip_reasons.append("late-lock-wait-reset")
    if run_block: skip_reasons.append("extended-run")
    if impulse_15: skip_reasons.append("current-impulse-chase")
    if recent_impulse and not impulse_reset and not early_rejection:
        skip_reasons.append("impulse-memory")
    if not atr_quality: skip_reasons.append("atr-band")

    if entry_route == "WAIT" and not skip_reasons:
        if tactics < required_tactics:
            skip_reasons.append(f"tactics-{tactics}/{required_tactics}")
        elif score < min(threshold, EARLY_MIN_SCORE if rejection else threshold):
            skip_reasons.append(f"score-{score:.0f}<{threshold:.0f}")
        else:
            skip_reasons.append("no-entry-route")

    # EARLY still must obey conflict/risk gates.
    hard_gate_names = {
        "adx-chop", "hard-rs-conflict", "rs-conflict-needs-4/4-reject",
        "btc-conflict-needs-4/4-reject", "rsi-overextended", "extended",
        "coin-2to10h-run", "coin-1h-bad-location", "late-lock-wait-reset",
        "extended-run", "current-impulse-chase", "atr-band",
    }
    if entry_route != "WAIT" and any(x in hard_gate_names for x in skip_reasons):
        entry_route = "WAIT"

    # If PB route is otherwise valid, do not leave stale score/tactic reasons.
    if entry_route != "WAIT":
        skip_reasons = [x for x in skip_reasons if x in hard_gate_names]

    final_skip = skip_reasons[0] if skip_reasons else (None if entry_route != "WAIT" else "no-entry-route")

    return {
        "symbol": symbol, "side": side, "score": float(score), "tactics": tactics,
        "threshold": threshold, "price": price, "atr": atr, "atr_pct": atr_pct,
        "rsi": rsi, "adx": adx, "rel": rel, "rel15": rel15, "rel45": rel45,
        "rel90": rel90, "confirm": confirm_components >= 3,
        "confirm_components": confirm_components, "structure": bool(structure),
        "pullback_quality": pullback_quality, "entry_quality": entry_quality,
        "momentum_quality": momentum_quality, "sweep": bool(sweep),
        "learning_bonus": learning_adjustment(preliminary), "skip": final_skip,
        "skip_reasons": skip_reasons, "session": sess, "btc_regime": btc_label,
        "run8": run8, "volume_ok": bool(volume_ok), "timestamp": time.time(),
        "ret2h": coin_run["ret2h"], "ret4h": coin_run["ret4h"],
        "ret6h": coin_run["ret6h"], "ret10h": coin_run["ret10h"],
        "ema_ext_1h": coin_run["ema_ext_1h"],
        "htf_location_block": htf_location_block,
        "late_lock": late_lock,
        "entry_route": entry_route, "early_rejection": early_rejection,
        "rejection": rejection, "micro_break": m_break,
        "recent_impulse": recent_impulse, "impulse_reset": impulse_reset,
        "rs_conflict_mild": rsx["mild"], "rs_conflict_hard": rsx["hard"],
        "btc_conflict": btc_against,
        "btc_conflict_overridden": macro_pullback_override_ok,
        "btc_flow_aligned": btc_flow_aligned, "btc_flow_strong": btc_flow_strong,
        "flow_rs_supportive": flow_rs_supportive,
        "btc_macro_supportive": btc_macro_supportive,
        "btc_macro_opposed": btc_macro_opposed,
        "btc_macro_pullback_support": btc_macro_pullback,
        "btc_macro_strong": btc_macro_strong,
    }

# ============================================================
# TRADE LEVELS / SIZING
# ============================================================
def calculate_trade_levels(entry, atr, side, tick):
    risk_distance = max(atr * ATR_SL_MULT, entry * MIN_STOP_PCT)
    risk_distance = min(risk_distance, entry * MAX_STOP_PCT)
    if side == "long":
        raw_sl, raw_tp = entry - risk_distance, entry + risk_distance * TP_R_MULT
    else:
        raw_sl, raw_tp = entry + risk_distance, entry - risk_distance * TP_R_MULT
    sl = round_trade_price(raw_sl, tick, side, "sl")
    tp = round_trade_price(raw_tp, tick, side, "tp")
    if side == "long" and not (sl < entry < tp): return None
    if side == "short" and not (tp < entry < sl): return None
    return sl, tp, abs(entry - sl)


def roundtrip_cost(notional):
    return notional * (TAKER_FEE_RATE * 2 + SLIPPAGE_RATE)


def is_a_setup(signal):
    """
    V6.14 premium A-sizing. MACRO route is deliberately NORMAL sizing.

    Route 1 - EARLY A:
      dezelfde strenge V6.12 voorwaarden: 4/4, rejection, micro-break,
      momentum >=2, EntryQ>=3, geen BTC-conflict.

    Route 2 - FLOW PULLBACK A:
      alleen wanneer BTC sterk dezelfde kant op stroomt én de alt zelf niet
      relatief tegen die richting in loopt. Vereist 4/4, EntryQ>=4,
      momentum>=2, tactics>=5 en ADX>=25.
    """
    if signal.get("entry_route") == "MACRO":
        return False

    no_risk_conflict = bool(
        not signal.get("btc_conflict")
        and not signal.get("rs_conflict_hard")
        and not (bool(signal.get("recent_impulse")) and not bool(signal.get("impulse_reset")))
    )
    if not no_risk_conflict:
        return False

    common_4of4 = bool(
        int(signal.get("confirm_components", 0)) >= 4
        and int(signal.get("momentum_quality", 0)) >= 2
        and int(signal.get("tactics", 0)) >= 4
    )

    early_a = bool(
        signal.get("entry_route") == "EARLY"
        and common_4of4
        and bool(signal.get("rejection"))
        and bool(signal.get("micro_break"))
        and int(signal.get("entry_quality", 0)) >= 3
        and safe_float(signal.get("adx")) >= max(EARLY_MIN_ADX, 22.0)
    )

    flow_pullback_a = bool(
        signal.get("entry_route") == "PULLBACK"
        and signal.get("btc_flow_strong")
        and signal.get("flow_rs_supportive")
        and common_4of4
        and int(signal.get("entry_quality", 0)) >= 4
        and int(signal.get("tactics", 0)) >= 5
        and safe_float(signal.get("adx")) >= 25.0
    )

    return early_a or flow_pullback_a


def flow_second_alt_ok(signal):
    """Only the second correlated alt may use the offensive flow exception."""
    return bool(
        signal.get("btc_flow_strong")
        and signal.get("flow_rs_supportive")
        and not signal.get("btc_conflict")
        and not signal.get("rs_conflict_hard")
        and int(signal.get("confirm_components", 0)) >= FLOW_SECOND_ALT_MIN_CONFIRM
        and int(signal.get("momentum_quality", 0)) >= FLOW_SECOND_ALT_MIN_MOMENTUM
        and int(signal.get("entry_quality", 0)) >= FLOW_SECOND_ALT_MIN_ENTRYQ
        and safe_float(signal.get("adx")) >= FLOW_SECOND_ALT_MIN_ADX
    )

def sizing_profile(signal):
    if is_a_setup(signal):
        return {
            "name": "A",
            "risk_pct": A_SETUP_RISK_EQUITY_PCT,
            "target_net_tp": A_SETUP_TARGET_NET_TP_USDT,
        }
    return {
        "name": "NORMAL",
        "risk_pct": NORMAL_RISK_EQUITY_PCT,
        "target_net_tp": NORMAL_TARGET_NET_TP_USDT,
    }


def allowed_fill_slip(signal):
    # De ruimere 0.75% geldt NIET voor elke EARLY setup.
    # Alleen A-setups met echte 4/4 rejection + micro-break krijgen dit.
    if is_a_setup(signal):
        return EARLY_MAX_FILL_SLIP_PCT
    return MAX_FILL_SLIP_PCT


def calculate_contracts(symbol, entry, sl, equity, available, risk_pct, target_net_tp):
    meta = get_instrument(symbol)
    cv = safe_float(meta.get("contractValue"))
    min_size = safe_float(meta.get("minSize"))
    lot = safe_float(meta.get("lotSize"))
    max_market = safe_float(meta.get("maxMarketSize", 0))
    if cv <= 0 or min_size <= 0 or lot <= 0:
        raise RuntimeError(f"Bad contract metadata for {symbol}")

    stop_pct = abs(entry - sl) / entry
    if stop_pct <= 0:
        return None

    # 1) Absolute risk cap.
    risk_budget = equity * risk_pct
    risk_notional_cap = risk_budget / stop_pct

    # 2) Margin caps. BASE_MARGIN remains a hard target ceiling; MAX_MARGIN
    #    and equity/available caps remain additional safety ceilings.
    margin_cap = min(
        BASE_MARGIN_USDT,
        MAX_MARGIN_USDT,
        equity * MAX_MARGIN_EQUITY_PCT,
        max(0.0, available * AVAILABLE_MARGIN_BUFFER),
    )
    margin_notional_cap = margin_cap * LEVERAGE

    # 3) Work backwards from desired NET TP. This is a target, not a promise.
    cost_rate = TAKER_FEE_RATE * 2 + SLIPPAGE_RATE
    net_tp_rate = TP_R_MULT * stop_pct - cost_rate
    if net_tp_rate <= 0:
        return None
    desired_notional = target_net_tp / net_tp_rate

    target_notional = min(
        desired_notional,
        risk_notional_cap,
        margin_notional_cap,
    )
    if target_notional <= 0:
        return None

    contracts = floor_step(target_notional / (cv * entry), lot)
    if contracts < min_size:
        contracts = min_size
    if max_market > 0:
        contracts = min(contracts, floor_step(max_market, lot))
    if contracts < min_size:
        return None

    notional = contracts * cv * entry
    margin = notional / LEVERAGE

    # Never let exchange minimum size silently exceed our safety caps.
    if notional * stop_pct > risk_budget + 1e-9:
        return None
    if margin > margin_cap + 1e-9:
        return None

    expected_tp = notional * TP_R_MULT * stop_pct
    cost = roundtrip_cost(notional)
    expected_net_tp = expected_tp - cost
    expected_sl = notional * stop_pct
    expected_net_sl = expected_sl + cost

    if expected_net_tp < MIN_NET_TP_USDT:
        return None

    return {
        "contracts": contracts,
        "contract_value": cv,
        "notional": notional,
        "margin": margin,
        "expected_tp_usdt": expected_tp,
        "expected_net_tp_usdt": expected_net_tp,
        "expected_sl_usdt": expected_sl,
        "expected_net_sl_usdt": expected_net_sl,
        "roundtrip_cost": cost,
        "risk_budget": risk_budget,
        "risk_pct": risk_pct,
        "target_net_tp": target_net_tp,
    }

# ============================================================
# TPSL / PROTECTION
# ============================================================
def pending_tpsl(inst_id=None):
    params = {"limit": "100"}
    if inst_id: params["instId"] = inst_id
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
        if str(o.get("state", "")).lower() not in ("live", "effective"): continue
        if o.get("instId") != inst: continue
        if str(o.get("positionSide", "net")).lower() != pos_side: continue
        opid = str(o.get("positionId", "") or "")
        if pid and opid and opid != pid: continue
        has_tp = safe_float(o.get("tpTriggerPrice")) > 0
        has_sl = safe_float(o.get("slTriggerPrice")) > 0
        if has_tp and has_sl: return True, o
    return False, None


def all_positions_protected():
    try:
        return all(has_live_protection(p)[0] for p in get_positions())
    except Exception:
        return False


def tpsl_prices_valid(side, sl, tp, last):
    if last <= 0: return False
    return sl < last < tp if side == "long" else tp < last < sl


def place_tpsl_for_position(p, sl, tp, client_tag="v611"):
    side = position_side(p)
    pside = str(p.get("positionSide", "net")).lower()
    if pside not in ("net", "long", "short"): pside = "net"
    payload = {
        "instId": p["instId"], "marginMode": position_margin_mode(p),
        "positionSide": pside, "side": "sell" if side == "long" else "buy",
        "tpTriggerPrice": str(tp), "tpOrderPrice": "-1", "tpTriggerPriceType": "mark",
        "slTriggerPrice": str(sl), "slOrderPrice": "-1", "slTriggerPriceType": "mark",
        "size": "-1", "reduceOnly": "true",
        "clientOrderId": f"{client_tag}{int(time.time() * 1000) % 100000000}",
    }
    pid = position_id(p)
    if pid: payload["positionId"] = pid
    if BROKER_ID: payload["brokerId"] = BROKER_ID
    resp = native("private_post_trade_order_tpsl", payload)
    if not native_ok(resp): return False, response_error(resp)
    time.sleep(0.8)
    ok, found = has_live_protection(p)
    if not ok: return False, "TPSL accepted but both TP+SL not visible"
    return True, found


def repair_one_position(p):
    symbol, side, entry = position_symbol(p), position_side(p), position_entry(p)
    if entry <= 0: return False
    if has_live_protection(p)[0]: return True
    df = add_indicators(ohlcv_df(symbol, TIMEFRAME_SIGNAL))
    atr = safe_float(df.iloc[-2]["atr"])
    tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
    levels = calculate_trade_levels(entry, atr, side, tick)
    if not levels: return False
    sl, tp, _ = levels
    last = safe_float(exchange.fetch_ticker(symbol).get("last"))
    if not tpsl_prices_valid(side, sl, tp, last): return False
    return place_tpsl_for_position(p, sl, tp, client_tag="repair")[0]


def repair_existing_positions():
    positions = get_positions()
    if not positions: return True
    tg(f"🛡️ V6.14 STARTUP\nOpen positions: {len(positions)}\nChecking TP + SL...")
    for p in positions:
        symbol, side = position_symbol(p), position_side(p)
        try:
            if has_live_protection(p)[0]:
                tg(f"✅ {symbol} {side.upper()}: TP + SL confirmed.")
            elif repair_one_position(p):
                tg(f"🛡️ REPAIRED {symbol} {side.upper()}")
            else:
                tg(f"🚨 UNPROTECTED {symbol} {side.upper()}")
        except Exception as e:
            tg(f"🚨 PROTECTION ERROR {symbol}\n{e}")
    return all_positions_protected()


def set_leverage(symbol, leverage):
    resp = native("private_post_account_set_leverage", {
        "instId": blofin_inst_id(symbol), "leverage": str(leverage), "marginMode": MARGIN_MODE,
    })
    return (True, resp) if native_ok(resp) else (False, response_error(resp))


def find_new_position(symbol, before_ids, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for p in get_positions():
                if position_symbol(p) != symbol: continue
                pid = position_id(p)
                if (pid and pid not in before_ids) or not pid: return p
        except Exception:
            pass
        time.sleep(0.5)
    return None

# ============================================================
# STATE
# ============================================================
protection_lock = False
loss_pause_until = 0
consecutive_losses = 0
symbol_last_trade = {}
symbol_last_result = {}
symbol_loss_streak = {}
late_entry_locks = {}
seen_closed_history_ids = set()
last_score_report = 0

daily = {
    "date": None, "start_equity": None, "trades": 0, "wins": 0, "losses": 0, "net": 0.0,
    "long_trades": 0, "long_wins": 0, "long_net": 0.0,
    "short_trades": 0, "short_wins": 0, "short_net": 0.0,
}


def reset_daily_if_needed(current_equity=None):
    today = amsterdam_today()
    if daily["date"] != today:
        daily.update({
            "date": today, "start_equity": current_equity, "trades": 0, "wins": 0,
            "losses": 0, "net": 0.0, "long_trades": 0, "long_wins": 0,
            "long_net": 0.0, "short_trades": 0, "short_wins": 0, "short_net": 0.0,
        })
        return True
    if daily["start_equity"] is None and current_equity is not None:
        daily["start_equity"] = current_equity
    return False

# ============================================================
# OPEN TRADE
# ============================================================
def open_trade(signal):
    global protection_lock, _v62_last_entry_time
    symbol, side = signal["symbol"], signal["side"]
    account = futures_account()
    equity, available = account["total_equity"], account["available"]
    reset_daily_if_needed(equity)
    if equity < MIN_EQUITY_USDT:
        tg(f"⛔ RISK STOP\nEquity ${equity:.2f} < ${MIN_EQUITY_USDT:.2f}")
        return False
    if available <= 0 or (not PAPER_TRADING and protection_lock): return False

    tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
    live = safe_float(exchange.fetch_ticker(symbol).get("last"), signal["price"])
    signal_px = signal["price"]
    if live <= 0 or signal_px <= 0: return False
    slip = (live - signal_px) / signal_px
    fill_limit = allowed_fill_slip(signal)
    if side == "long" and slip > fill_limit:
        late_entry_locks[symbol] = {"side": side, "ts": time.time(), "price": live}
        tg(
            f"⏭️ {symbol} LONG too late {slip * 100:.2f}% "
            f"> {fill_limit * 100:.2f}% "
            f"({'A' if is_a_setup(signal) else 'NORMAL'})\n"
            f"🔒 V6.14 late-lock until real pullback/reset"
        )
        return False
    if side == "short" and slip < -fill_limit:
        late_entry_locks[symbol] = {"side": side, "ts": time.time(), "price": live}
        tg(
            f"⏭️ {symbol} SHORT too late {abs(slip) * 100:.2f}% "
            f"> {fill_limit * 100:.2f}% "
            f"({'A' if is_a_setup(signal) else 'NORMAL'})\n"
            f"🔒 V6.14 late-lock until real pullback/reset"
        )
        return False

    entry = live
    levels = calculate_trade_levels(entry, signal["atr"], side, tick)
    if not levels: return False
    sl, tp, _ = levels
    stop_pct = abs(entry - sl) / entry
    if stop_pct < MIN_STOP_PCT: return False
    profile = sizing_profile(signal)
    sizing = calculate_contracts(
        symbol, entry, sl, equity, available,
        profile["risk_pct"], profile["target_net_tp"]
    )
    if not sizing: return False
    if sizing["margin"] > available * AVAILABLE_MARGIN_BUFFER: return False

    before_ids = {position_id(p) for p in get_positions() if position_id(p)}
    lev_ok, lev_result = set_leverage(symbol, LEVERAGE)
    if not lev_ok:
        tg(f"⛔ LEVERAGE FAILED\n{symbol}\nRequested: {LEVERAGE}x\n{lev_result}\nTrade skipped.")
        return False

    payload = {
        "instId": blofin_inst_id(symbol), "marginMode": MARGIN_MODE, "positionSide": "net",
        "side": "buy" if side == "long" else "sell", "orderType": "market",
        "size": str(sizing["contracts"]),
        "tpTriggerPrice": str(tp), "tpOrderPrice": "-1", "tpTriggerPriceType": "mark",
        "slTriggerPrice": str(sl), "slOrderPrice": "-1", "slTriggerPriceType": "mark",
        "clientOrderId": f"v614{int(time.time() * 1000) % 1000000000}",
    }
    if BROKER_ID: payload["brokerId"] = BROKER_ID

    route_label = (
        "🚀 EARLY REJECTION" if signal.get("entry_route") == "EARLY"
        else "🧭 MACRO FLOW" if signal.get("entry_route") == "MACRO"
        else "🪵 PULLBACK"
    )
    conflict_text = []
    if signal.get("rs_conflict_mild"): conflict_text.append("RS-CONFLICT VERIFIED")
    if signal.get("btc_conflict_overridden"):
        conflict_text.append("BTC PULLBACK OVERRIDDEN BY MACRO")
    elif signal.get("btc_conflict"):
        conflict_text.append("BTC-CONFLICT VERIFIED")
    conflict_line = ("\n" + " / ".join(conflict_text)) if conflict_text else ""

    message = (
        f"{'🧪 PAPER V6.14' if PAPER_TRADING else '🟢 V6.14 LIVE ORDER'}\n"
        f"{symbol} {side.upper()}\n━━━━━━━━━━━━\nENTRY: {route_label}\n"
        f"Score {signal['score']:.0f}/{signal['threshold']:.0f}\n"
        f"Directional tactics {signal['tactics']}\n5m {signal['confirm_components']}/4\n"
        f"ADX {signal['adx']:.0f}\nRS {signal['rel'] * 100:+.2f}%\nRSI {signal['rsi']:.0f}\n"
        f"Pullback {signal['pullback_quality']}/3\nEntryQ {signal['entry_quality']}\n"
        f"Momentum {signal['momentum_quality']}\nBTC {signal['btc_regime']}\n"
        f"BTC flow: {'🌊 STRONG' if signal.get('btc_flow_strong') else '➡️ ALIGNED' if signal.get('btc_flow_aligned') else '↔️ NEUTRAL/AGAINST'}\n"
        f"BTC macro: {'🧭 SUPPORT + PULLBACK' if signal.get('btc_macro_pullback_support') else '🧭 SUPPORT' if signal.get('btc_macro_supportive') else '⛔ OPPOSED' if signal.get('btc_macro_opposed') else '↔️ MIXED'}\n"
        f"Coin run: 2h {signal.get('ret2h',0)*100:+.2f}% | 4h {signal.get('ret4h',0)*100:+.2f}% | 6h {signal.get('ret6h',0)*100:+.2f}% | 10h {signal.get('ret10h',0)*100:+.2f}%\n"
        f"1H EMA20 extension: {signal.get('ema_ext_1h',0)*100:+.2f}%\n"
        f"5m rejection: {'YES' if signal.get('rejection') else 'NO'}\n"
        f"5m micro-break: {'YES' if signal.get('micro_break') else 'NO'}\n"
        f"Recent impulse: {'YES' if signal.get('recent_impulse') else 'NO'}\n"
        f"Impulse reset: {'YES' if signal.get('impulse_reset') else 'NO'}"
        f"{conflict_line}\n━━━━━━━━━━━━\nEntry ~ {entry:.8g}\nSL {sl:.8g}\nTP {tp:.8g}\n"
        f"Stop {stop_pct * 100:.2f}%\nSizing {profile['name']} | Risk cap {profile['risk_pct'] * 100:.1f}%\n"
        f"Target net TP ${profile['target_net_tp']:.2f}\n"
        f"ExpTP gross ${sizing['expected_tp_usdt']:.2f}\n"
        f"ExpTP net ~${sizing['expected_net_tp_usdt']:.2f}\n"
        f"ExpSL + cost ~${sizing['expected_net_sl_usdt']:.2f}\n"
        f"Cost ${sizing['roundtrip_cost']:.2f}\nNotional ${sizing['notional']:.2f}\n"
        f"Margin ${sizing['margin']:.2f}\nFill drift {abs(slip) * 100:.2f}%/{fill_limit * 100:.2f}%\n{LEVERAGE}x"
    )

    if PAPER_TRADING:
        tg(message)
        return True

    resp = native("private_post_trade_order", payload)
    if not native_ok(resp):
        err = response_error(resp)
        if "152012" in err:
            protection_lock = True
            tg(f"🛑 152012 LOCK\n{err}")
        else:
            tg(f"❌ LIVE ORDER FAILED\n{symbol} {side.upper()}\n{err}")
        return False

    tg(message)
    new_pos = find_new_position(symbol, before_ids, timeout=8)
    if new_pos is None:
        tg(f"⚠️ {symbol}: order accepted, position not visible yet.")
        return True
    if has_live_protection(new_pos)[0]:
        tg(f"🛡️ PROTECTION CONFIRMED\n{symbol} {side.upper()}\nTP + SL visible.")
        return True

    last = safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side, sl, tp, last):
        ok, result = place_tpsl_for_position(new_pos, sl, tp, client_tag="fallback")
        if ok:
            tg(f"🛡️ FALLBACK TP + SL CONFIRMED\n{symbol}")
            return True
        tg(f"🚨 CRITICAL PROTECTION FAILURE\n{symbol}\n{result}\nNEW TRADES PAUSED.")
        protection_lock = True
        return True
    tg(f"🚨 {symbol}: position exists but fallback TP/SL cannot safely be placed.\nNEW TRADES PAUSED.")
    protection_lock = True
    return True

# ============================================================
# HISTORY / STATS
# ============================================================
def closed_positions_history(limit=100):
    merged = []
    for state in ("closed", "liquidated", "adl"):
        try:
            resp = native("private_get_account_positions_history", {"state": state, "limit": str(limit)})
            if native_ok(resp): merged.extend(resp.get("data") or [])
        except Exception:
            continue
    return merged


def close_reason(record):
    raw = str(record.get("closeType", record.get("state", ""))).lower()
    mapping = {"tp": "TAKE PROFIT", "take_profit": "TAKE PROFIT", "sl": "STOP LOSS",
               "stop_loss": "STOP LOSS", "liquidated": "LIQUIDATION", "liquidation": "LIQUIDATION",
               "adl": "ADL", "manual": "MANUAL", "mannual": "MANUAL", "close": "CLOSED"}
    return mapping.get(raw, raw.upper() if raw else "CLOSED")


def fmt_duration(start_ms, end_ms):
    start, end = safe_float(start_ms), safe_float(end_ms)
    if start <= 0 or end <= 0 or end < start: return "unknown"
    seconds = int((end - start) / 1000)
    hours, rem = divmod(seconds, 3600); minutes, secs = divmod(rem, 60)
    if hours: return f"{hours}h {minutes}m"
    if minutes: return f"{minutes}m {secs}s"
    return f"{secs}s"


def infer_side(record):
    side = str(record.get("positionSide", "net")).lower()
    if side in ("long", "short"): return side
    entry, exit_p, pnl = safe_float(record.get("openAveragePrice")), safe_float(record.get("closeAveragePrice")), safe_float(record.get("realizedPnl"))
    if entry and exit_p:
        moved_up = exit_p >= entry
        return "long" if ((moved_up and pnl >= 0) or (not moved_up and pnl < 0)) else "short"
    return "net"


def update_learning(record, net_pnl):
    global learning
    side = infer_side(record)
    fp = f"{side}|history"
    rec = learning.setdefault("fingerprints", {}).setdefault(fp, {"trades": 0, "wins": 0, "losses": 0, "net": 0.0})
    rec["trades"] += 1
    rec["wins" if net_pnl >= 0 else "losses"] += 1
    rec["net"] += net_pnl
    learning["trades"] = int(learning.get("trades", 0)) + 1
    learning["wins" if net_pnl >= 0 else "losses"] = int(learning.get("wins" if net_pnl >= 0 else "losses", 0)) + 1
    save_learning()


def notify_closed_trade(record):
    global consecutive_losses, loss_pause_until, sniper_side_loss_streak
    reset_daily_if_needed()
    symbol = str(record.get("instId", "")).replace("-USDT", "/USDT:USDT")
    side = infer_side(record)
    entry = safe_float(record.get("openAveragePrice")); exit_price = safe_float(record.get("closeAveragePrice"))
    gross_pnl = safe_float(record.get("realizedPnl")); fee = abs(safe_float(record.get("fee")))
    net_pnl = gross_pnl - fee
    pnl_ratio = safe_float(record.get("realizedPnlRatio")) * 100
    duration = fmt_duration(record.get("createTime"), record.get("updateTime"))
    reason, size = close_reason(record), safe_float(record.get("closePositions"))

    daily["trades"] += 1; daily["net"] += net_pnl
    bucket = "short" if side == "short" else "long"
    daily[f"{bucket}_trades"] += 1; daily[f"{bucket}_net"] += net_pnl
    if net_pnl >= 0:
        daily["wins"] += 1; daily[f"{bucket}_wins"] += 1; consecutive_losses = 0
        sniper_side_loss_streak[bucket] = 0
        symbol_last_result[symbol] = "win"; symbol_loss_streak[symbol] = 0
    else:
        daily["losses"] += 1; consecutive_losses += 1
        sniper_side_loss_streak[bucket] = sniper_side_loss_streak.get(bucket, 0) + 1
        symbol_last_result[symbol] = "loss"; symbol_loss_streak[symbol] = symbol_loss_streak.get(symbol, 0) + 1
        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            loss_pause_until = time.time() + LOSS_PAUSE_MIN * 60
    symbol_last_trade[symbol] = time.time()
    update_learning(record, net_pnl)

    wr_long = daily["long_wins"] / daily["long_trades"] * 100 if daily["long_trades"] else 0
    wr_short = daily["short_wins"] / daily["short_trades"] * 100 if daily["short_trades"] else 0
    pause = f"\n⏸ {MAX_CONSECUTIVE_LOSSES} losses → pauze {LOSS_PAUSE_MIN} min" if consecutive_losses >= MAX_CONSECUTIVE_LOSSES else ""
    icon = "🟢" if net_pnl >= 0 else "🔴"
    tg(
        f"{icon} STRUCTURE FUSION CLOSED\n\n{symbol}\n{side.upper()}\n━━━━━━━━━━━━\n"
        f"Entry: {entry:.8g}\nExit: {exit_price:.8g}\nSize: {size:g}\n\n"
        f"💰 Realized: {'+' if gross_pnl >= 0 else ''}${gross_pnl:.2f}\n"
        f"📊 Ratio: {pnl_ratio:+.2f}%\n💸 Fee: -${fee:.2f}\n"
        f"💵 Net: {'+' if net_pnl >= 0 else ''}${net_pnl:.2f}\n\n🎯 {reason}\n⏱ {duration}\n\n"
        f"📅 TODAY Amsterdam\nTrades: {daily['trades']} | W/L {daily['wins']}/{daily['losses']} | Net ${daily['net']:.2f}\n"
        f"LONG: {daily['long_trades']} / {wr_long:.0f}% / ${daily['long_net']:.2f}\n"
        f"SHORT: {daily['short_trades']} / {wr_short:.0f}% / ${daily['short_net']:.2f}\n"
        f"Side streak L/S: {sniper_side_loss_streak.get('long',0)}/{sniper_side_loss_streak.get('short',0)}{pause}"
    )


def seed_closed_history():
    reset_daily_if_needed()
    try:
        for r in closed_positions_history(100):
            hid = str(r.get("historyId", ""))
            if hid: seen_closed_history_ids.add(hid)
        tg(f"📚 PRECISION SNIPER PNL TRACKER READY\nKnown closes: {len(seen_closed_history_ids)}\nLearning trades: {learning.get('trades', 0)}")
    except Exception as e:
        tg(f"⚠️ PNL tracker warning\n{e}")


def check_for_closed_positions():
    records = closed_positions_history(100)
    new_records = []
    for r in reversed(records):
        hid = str(r.get("historyId", ""))
        if not hid or hid in seen_closed_history_ids: continue
        seen_closed_history_ids.add(hid); new_records.append(r)
    for r in new_records: notify_closed_trade(r)
    if len(seen_closed_history_ids) > 2500:
        seen_closed_history_ids.clear()
        for r in records:
            hid = str(r.get("historyId", ""))
            if hid: seen_closed_history_ids.add(hid)


def check_daily_risk(equity):
    reset_daily_if_needed(equity)
    start = daily["start_equity"]
    if not start or start <= 0: return True, "OK"
    dd = (start - equity) / start * 100
    if dd >= DAILY_LOSS_LIMIT_PCT:
        return False, f"Daily risk stop {dd:.2f}% vs start ${start:.2f}"
    return True, "OK"


def cooldown_ok(symbol):
    last = symbol_last_trade.get(symbol, 0)
    if symbol_loss_streak.get(symbol, 0) >= 2: needed = REPEAT_LOSS_COOLDOWN_MIN * 60
    elif symbol_last_result.get(symbol) == "loss": needed = LOSS_COOLDOWN_MIN * 60
    else: needed = WIN_COOLDOWN_MIN * 60
    return time.time() - last >= needed


def count_alt_sides(positions):
    longs = shorts = 0
    for p in positions:
        if position_symbol(p) in MAJORS: continue
        if position_side(p) == "short": shorts += 1
        else: longs += 1
    return longs, shorts

# ============================================================
# REPORTING
# ============================================================
def sniper_portfolio_allows(signal, positions):
    """Diversification/quality gate while still allowing up to MAX_ACTIVE_POSITIONS."""
    side = signal.get("side")
    if side not in ("long", "short"):
        return False, "no-side"
    same_side = sum(1 for p in positions if position_side(p) == side)
    ratio = safe_float(signal.get("score")) / max(safe_float(signal.get("score_max"), 1.0), 1e-9)
    htf_aligned = signal.get("htf_state") == "ALIGNED"

    # The fourth same-direction position is allowed only when it is genuinely
    # premium and supported by the 1h trend. This prevents four correlated B
    # trades from behaving like one oversized market bet.
    if same_side >= SNIPER_MAX_SAME_SIDE:
        if not (ratio >= SNIPER_FOURTH_SAME_SIDE_MIN_RATIO and htf_aligned):
            return False, "same-side-4th-needs-A+-1h"

    # After repeated losses in one direction, do not keep firing ordinary
    # signals into the same regime. Premium aligned setups can still trade.
    if sniper_side_loss_streak.get(side, 0) >= SNIPER_SIDE_LOSS_TIGHTEN_AFTER:
        if not (ratio >= 0.80 and htf_aligned and safe_float(signal.get("adx")) >= 20):
            return False, f"{side}-loss-streak-needs-A+-1h"
    return True, "OK"


def maybe_score_report(rows):
    global last_score_report
    if time.time() - last_score_report < SCORE_REPORT_MIN * 60: return
    last_score_report = time.time()
    ranked = sorted(rows, key=lambda x: (x.get("score", 0), x.get("entry_quality", 0), x.get("adx", 0)), reverse=True)[:10]
    lines = [f"📡 V6.14 {session_state()} {now_utc().strftime('%H:%M')} UTC"]
    if not ranked: lines.append("geen scans")
    for r in ranked:
        route = r.get("entry_route", "WAIT")
        route_tag = "🚀EARLY" if route == "EARLY" else "🧭MACRO" if route == "MACRO" else "↩️PB" if route == "PULLBACK" else "WAIT"
        mark = "🟢 READY" if not r.get("skip") else f"⏭ {r.get('skip')}"
        conflict = " ⚠️RS" if r.get("rs_conflict_mild") else ""
        flow = " 🌊" if r.get("btc_flow_strong") else " ➡️" if r.get("btc_flow_aligned") else ""
        macro = " 🧭↩️" if r.get("btc_macro_pullback_support") else " 🧭" if r.get("btc_macro_supportive") else ""
        runloc = ""
        if r.get("htf_location_block") or abs(safe_float(r.get("ret2h"))) >= 0.02:
            runloc = f" 2h{safe_float(r.get('ret2h'))*100:+.1f}%/4h{safe_float(r.get('ret4h'))*100:+.1f}%"
        if r.get("late_lock"):
            runloc += " 🔒"
        lines.append(
            f"{r['symbol'].split('/')[0]} {str(r.get('side') or '-').upper()} "
            f"{r.get('score', 0):.0f}/{r.get('threshold', BASE_MIN_SCORE):.0f} {route_tag} "
            f"t{r.get('tactics', 0)} 5m{r.get('confirm_components', 0)}/4 "
            f"ADX{r.get('adx', 0):.0f} RS{r.get('rel', 0) * 100:+.1f}%{conflict}{flow}{macro}{runloc} {mark}"
        )
    tg("\n".join(lines))


def startup_diagnostics():
    acc = futures_account(); reset_daily_if_needed(acc["total_equity"])
    tg(
        f"🚀 BLOFIN V6.14 STARTED\nMode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"Position mode expected: NET / ONE-WAY\nEquity: ${acc['total_equity']:.2f}\nAvailable: ${acc['available']:.2f}\n"
        f"Day start: ${daily['start_equity']:.2f}\nBase score: {BASE_MIN_SCORE:.0f}\nHard floor: {HARD_MIN_SCORE:.0f}\n"
        f"EARLY score: {EARLY_MIN_SCORE:.0f}\n"
        f"Fill normal/A-EARLY: {MAX_FILL_SLIP_PCT * 100:.2f}%/{EARLY_MAX_FILL_SLIP_PCT * 100:.2f}%\n"
        f"Risk normal/A: {NORMAL_RISK_EQUITY_PCT * 100:.1f}%/{A_SETUP_RISK_EQUITY_PCT * 100:.1f}%\n"
        f"Target net normal/A: ${NORMAL_TARGET_NET_TP_USDT:.2f}/${A_SETUP_TARGET_NET_TP_USDT:.2f}\n"
        f"RS mild conflict: {RS_CONFLICT_MILD * 100:.2f}%\n"
        f"RS hard conflict: {RS_CONFLICT_HARD * 100:.2f}%\nConflict confirm: {CONFLICT_REQUIRE_CONFIRM}/4 + rejection\n"
        f"Max positions: {MAX_ACTIVE_POSITIONS}\nAlt cap normal L{MAX_ALT_LONGS}/S{MAX_ALT_SHORTS} | strong-flow cap {FLOW_SECOND_ALT_CAP}\n"
        f"Flow threshold bonus: -{FLOW_THRESHOLD_BONUS:.0f} pts | BTC ADX >= {FLOW_MIN_BTC_ADX:.0f}\n"
        f"BTC hierarchy: 1D+4H macro | 1H regime | 15m trigger\n"
        f"Macro PB gate: {MACRO_PULLBACK_MIN_CONFIRM}/4, M>={MACRO_PULLBACK_MIN_MOMENTUM}, EntryQ>={MACRO_PULLBACK_MIN_ENTRYQ}, ADX>={MACRO_PULLBACK_MIN_ADX:.0f}, RS>={MACRO_PULLBACK_MIN_RS*100:.1f}% + micro-break\n"
        f"Coin chase guard: 2h {COIN_RUN_2H_MAX*100:.1f}% | 4h {COIN_RUN_4H_MAX*100:.1f}% | 6h {COIN_RUN_6H_MAX*100:.1f}% | 10h {COIN_RUN_10H_MAX*100:.1f}%\n"
        f"1H location: EMA20 ext {COIN_1H_EMA_EXT_MAX*100:.1f}% + within {COIN_NEAR_10H_EXTREME*100:.1f}% of 10h extreme | late-lock {LATE_LOCK_MAX_MIN}m max\n"
        f"Late-lock reset: >= {LATE_LOCK_MIN_RETRACE_ATR:.2f} ATR retrace + back near 15m EMA20\n"
        f"2nd flow-alt requires {FLOW_SECOND_ALT_MIN_CONFIRM}/4, M>={FLOW_SECOND_ALT_MIN_MOMENTUM}, EntryQ>={FLOW_SECOND_ALT_MIN_ENTRYQ}, ADX>={FLOW_SECOND_ALT_MIN_ADX:.0f}\n"
        f"Max execution attempts/scan: {MAX_EXECUTION_ATTEMPTS_PER_SCAN}\nDaily cap: {DAILY_LOSS_LIMIT_PCT:.0f}%"
    )


def protection_watch():
    global protection_lock
    if PAPER_TRADING:
        protection_lock = False; return
    positions = get_positions()
    if not positions:
        protection_lock = False; return
    if REPAIR_EXISTING_POSITIONS:
        for p in positions:
            try:
                if not has_live_protection(p)[0] and repair_one_position(p):
                    tg(f"🛡️ REPAIRED {position_symbol(p)} {position_side(p).upper()}")
            except Exception:
                pass
    protection_lock = not all_positions_protected()


# ============================================================
# PRECISION SNIPER V2.1 — LIVE TRANSLATION LAYER
# ============================================================
# This section replaces the V6.14 entry engine with the supplied TradingView
# Precision Sniper v2.1 logic while retaining the battle-tested BloFin account,
# sizing, leverage, TP/SL verification, PnL tracking and daily risk controls.
#
# Pine defaults translated for the bot's 15m chart:
#   Auto preset -> Default: EMA 9/21/55, RSI 13, ATR 14, min evidence 5/10,
#   SL 1.5 ATR, volume spike 1.2x, ADX trend 20, ATR/mean high-vol > 1.3.
#   HTF is OFF because the Pine htfInput default is empty.
#   Session is INFO ONLY / 24x7, so no night-alt block.
#   VWAP Auto is ON for the Default preset; here it is UTC-day anchored VWAP.
#   confirmWindow=0: the crossover's CLOSED 15m candle is the only candidate bar.
#   TP1/TP2 are milestones (0% partial by default), TP3 closes the position.
#   Step stop: TP1 -> entry, TP2 -> TP1, effective as soon as the bot observes it.
# ============================================================

SNIPER_VERSION = "2.2.0"
SNIPER_TF = "15m"
SNIPER_FAST = int(os.getenv("SNIPER_FAST", "9"))
SNIPER_SLOW = int(os.getenv("SNIPER_SLOW", "21"))
SNIPER_TREND = int(os.getenv("SNIPER_TREND", "55"))
SNIPER_RSI_LEN = int(os.getenv("SNIPER_RSI_LEN", "13"))
SNIPER_ATR_LEN = int(os.getenv("SNIPER_ATR_LEN", "14"))
SNIPER_ATR_MEAN_LEN = int(os.getenv("SNIPER_ATR_MEAN_LEN", "42"))
SNIPER_VOL_MEAN_LEN = int(os.getenv("SNIPER_VOL_MEAN_LEN", "20"))
SNIPER_VOLUME_SPIKE = float(os.getenv("SNIPER_VOLUME_SPIKE", "1.20"))
SNIPER_ADX_TREND = float(os.getenv("SNIPER_ADX_TREND", "20"))
SNIPER_VOL_THRESHOLD = float(os.getenv("SNIPER_VOL_THRESHOLD", "1.30"))
SNIPER_RSI_UPPER = float(os.getenv("SNIPER_RSI_UPPER", "75"))
SNIPER_RSI_LOWER = float(os.getenv("SNIPER_RSI_LOWER", "25"))
SNIPER_SL_MULT = float(os.getenv("SNIPER_SL_MULT", "1.50"))
SNIPER_STRUCTURE_BUFFER = float(os.getenv("SNIPER_STRUCTURE_BUFFER", "0.20"))
SNIPER_STRUCTURE_CAP = float(os.getenv("SNIPER_STRUCTURE_CAP", "1.50"))
SNIPER_SWING_LOOKBACK = int(os.getenv("SNIPER_SWING_LOOKBACK", "10"))
SNIPER_MIN_EVIDENCE_RATIO = float(os.getenv("SNIPER_MIN_EVIDENCE_RATIO", "0.50"))
SNIPER_MAX_FILL_DRIFT = float(os.getenv("SNIPER_MAX_FILL_DRIFT", "0.004"))
SNIPER_RISK_EQUITY_PCT = float(os.getenv("SNIPER_RISK_EQUITY_PCT", "0.016"))
SNIPER_TARGET_NET_TP_USDT = float(os.getenv("SNIPER_TARGET_NET_TP_USDT", "1.50"))
SNIPER_STEP_STOPS = os.getenv("SNIPER_STEP_STOPS", "true").lower() == "true"
SNIPER_MIN_RISK_TICKS = int(os.getenv("SNIPER_MIN_RISK_TICKS", "2"))
SNIPER_REPORT_MIN = int(os.getenv("SNIPER_REPORT_MIN", "15"))

# ---------------- V2.2 SMART CONTEXT ----------------
# Keep the original crossover/evidence engine, but improve WHERE and WHEN we enter.
SNIPER_HTF_TF = os.getenv("SNIPER_HTF_TF", "1h")
SNIPER_HTF_NEUTRAL_MIN_RATIO = float(os.getenv("SNIPER_HTF_NEUTRAL_MIN_RATIO", "0.65"))  # A
SNIPER_COUNTER_MIN_RATIO = float(os.getenv("SNIPER_COUNTER_MIN_RATIO", "0.80"))          # A+
SNIPER_COUNTER_MIN_ADX = float(os.getenv("SNIPER_COUNTER_MIN_ADX", "20"))
SNIPER_COUNTER_MIN_BODY_ATR = float(os.getenv("SNIPER_COUNTER_MIN_BODY_ATR", "0.30"))
SNIPER_MIN_CROSS_SLOPE_ATR = float(os.getenv("SNIPER_MIN_CROSS_SLOPE_ATR", "0.025"))
SNIPER_MAX_ENTRY_EXTENSION_ATR = float(os.getenv("SNIPER_MAX_ENTRY_EXTENSION_ATR", "0.70"))
SNIPER_RETEST_TRIGGER_ATR = float(os.getenv("SNIPER_RETEST_TRIGGER_ATR", "0.35"))
SNIPER_RETEST_MAX_BARS = int(os.getenv("SNIPER_RETEST_MAX_BARS", "3"))
SNIPER_RETEST_MIN_RATIO = float(os.getenv("SNIPER_RETEST_MIN_RATIO", "0.65"))
SNIPER_RETEST_MIN_ADX = float(os.getenv("SNIPER_RETEST_MIN_ADX", "15"))
SNIPER_MAX_SAME_SIDE = int(os.getenv("SNIPER_MAX_SAME_SIDE", "3"))
SNIPER_FOURTH_SAME_SIDE_MIN_RATIO = float(os.getenv("SNIPER_FOURTH_SAME_SIDE_MIN_RATIO", "0.80"))
SNIPER_SIDE_LOSS_TIGHTEN_AFTER = int(os.getenv("SNIPER_SIDE_LOSS_TIGHTEN_AFTER", "2"))

# TP3 is the live full-position take-profit, matching Pine defaults.
TP_R_MULT = 3.0
ATR_SL_MULT = SNIPER_SL_MULT

sniper_last_report = 0
sniper_last_signal_bar = {}
sniper_step_notified = {}
sniper_managed_positions = set()
# Extended crossover setups wait for a controlled retest instead of being chased.
sniper_retest_pending = {}
# Direction-specific loss memory: after repeated losses, same-side entries must be premium.
sniper_side_loss_streak = {"long": 0, "short": 0}


def _rma(series, length):
    return series.ewm(alpha=1 / length, adjust=False).mean()


def sniper_indicators(df):
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=SNIPER_FAST, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=SNIPER_SLOW, adjust=False).mean()
    out["ema_trend"] = out["close"].ewm(span=SNIPER_TREND, adjust=False).mean()

    prev_close = out["close"].shift(1)
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - prev_close).abs(),
        (out["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = _rma(tr, SNIPER_ATR_LEN)
    out["atr_mean"] = out["atr"].rolling(SNIPER_ATR_MEAN_LEN).mean()

    delta = out["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = _rma(gain, SNIPER_RSI_LEN) / _rma(loss, SNIPER_RSI_LEN).replace(0, np.nan)
    out["sniper_rsi"] = 100 - 100 / (1 + rs)

    ema12 = out["close"].ewm(span=12, adjust=False).mean()
    ema26 = out["close"].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    out["macd_hist"] = macd - signal

    up_move = out["high"].diff()
    down_move = -out["low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=out.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=out.index)
    atr_w = _rma(tr, 14)
    plus_di = 100 * _rma(plus_dm, 14) / atr_w.replace(0, np.nan)
    minus_di = 100 * _rma(minus_dm, 14) / atr_w.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    out["sniper_adx"] = _rma(dx, 14)
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di
    out["vol_mean"] = out["volume"].rolling(SNIPER_VOL_MEAN_LEN).mean()

    # TradingView's intraday session VWAP is approximated with a UTC-day anchor,
    # consistent with the Pine script's default Etc/UTC context.
    utc_day = pd.to_datetime(out["ts"], unit="ms", utc=True).dt.floor("D")
    typical = (out["high"] + out["low"] + out["close"]) / 3.0
    pv = typical * out["volume"]
    out["vwap"] = pv.groupby(utc_day).cumsum() / out["volume"].groupby(utc_day).cumsum().replace(0, np.nan)
    return out


def sniper_grade(ratio):
    if ratio >= 0.80: return "A+"
    if ratio >= 0.65: return "A"
    if ratio >= 0.50: return "B"
    return "C"


def sniper_plan(df, side):
    """Pine makePlan() translated for the last CLOSED 15m candle."""
    a = df.iloc[-2]
    entry = safe_float(a["close"])
    atr = safe_float(a["atr"])
    if entry <= 0 or atr <= 0:
        return None
    d = 1 if side == "long" else -1
    atr_distance = atr * SNIPER_SL_MULT
    # current closed bar + previous swingLookback bars = lookback + 1 bars.
    window = df.iloc[-(SNIPER_SWING_LOOKBACK + 2):-1]
    swing_low = safe_float(window["low"].min())
    swing_high = safe_float(window["high"].max())
    atr_stop = entry - d * atr_distance
    struct_stop = swing_low - atr * SNIPER_STRUCTURE_BUFFER if d == 1 else swing_high + atr * SNIPER_STRUCTURE_BUFFER
    raw_stop = min(atr_stop, struct_stop) if d == 1 else max(atr_stop, struct_stop)
    capped = abs(entry - raw_stop) > atr_distance * SNIPER_STRUCTURE_CAP
    bounded = entry - d * atr_distance * SNIPER_STRUCTURE_CAP if capped else raw_stop
    risk = abs(entry - bounded)
    return {
        "signal_entry": entry,
        "raw_stop": bounded,
        "risk_distance": risk,
        "capped": capped,
        "origin": "ATR capped" if capped else ("Structure" if abs(raw_stop - atr_stop) > 1e-12 else "ATR"),
        "atr": atr,
    }


def sniper_htf_context(symbol):
    """Last CLOSED 1h regime. It is context, not a prediction."""
    try:
        hdf = sniper_indicators(ohlcv_df(symbol, SNIPER_HTF_TF))
        if len(hdf) < max(SNIPER_TREND * 3, 80) + 2:
            return {"dir": 0, "adx": 0.0, "rsi": 50.0, "close": 0.0, "ema_trend": 0.0}
        h = hdf.iloc[-2]
        close = safe_float(h["close"])
        ef, es, et = safe_float(h["ema_fast"]), safe_float(h["ema_slow"]), safe_float(h["ema_trend"])
        direction = 1 if ef > es and close > et else -1 if ef < es and close < et else 0
        return {
            "dir": direction,
            "adx": safe_float(h["sniper_adx"]),
            "rsi": safe_float(h["sniper_rsi"], 50),
            "close": close,
            "ema_trend": et,
        }
    except Exception:
        return {"dir": 0, "adx": 0.0, "rsi": 50.0, "close": 0.0, "ema_trend": 0.0}


def sniper_evidence(a, p, side):
    d = 1 if side == "long" else -1
    close = safe_float(a["close"])
    rsi = safe_float(a["sniper_rsi"], 50)
    hist = safe_float(a["macd_hist"])
    hist_prev = safe_float(p["macd_hist"])
    adx = safe_float(a["sniper_adx"])
    plus_di = safe_float(a["plus_di"])
    minus_di = safe_float(a["minus_di"])
    atr = safe_float(a["atr"])
    atr_mean = safe_float(a["atr_mean"])
    vol_mean = safe_float(a["vol_mean"])
    volume = safe_float(a["volume"])
    vwap = safe_float(a["vwap"])
    volume_above = vol_mean > 0 and volume > vol_mean * SNIPER_VOLUME_SPIKE
    trending = adx >= SNIPER_ADX_TREND
    score = 0.0
    if (close > safe_float(a["ema_trend"])) if d == 1 else (close < safe_float(a["ema_trend"])): score += 1
    if (50 < rsi < SNIPER_RSI_UPPER) if d == 1 else (SNIPER_RSI_LOWER < rsi < 50): score += 1
    if (hist > 0) if d == 1 else (hist < 0): score += 1
    if (hist > hist_prev) if d == 1 else (hist < hist_prev): score += 1
    if trending and ((plus_di > minus_di) if d == 1 else (minus_di > plus_di)): score += 1
    if volume_above: score += 1
    if (close > vwap) if d == 1 else (close < vwap): score += 1
    return score, 7.0, rsi, adx, atr, (atr / atr_mean if atr_mean > 0 else 999.0), volume_above


def evaluate_symbol(symbol, btc=None):
    """Precision Sniper v2.2: crossover + smart 1h context + anti-chase retest."""
    df = sniper_indicators(ohlcv_df(symbol, SNIPER_TF))
    warmup = max(SNIPER_TREND * 3, SNIPER_ATR_LEN + SNIPER_ATR_MEAN_LEN)
    if len(df) < warmup + 3:
        return None
    a, p = df.iloc[-2], df.iloc[-3]
    if any(pd.isna(a.get(k)) for k in ["atr", "atr_mean", "sniper_rsi", "macd_hist", "sniper_adx", "ema_fast", "ema_slow", "ema_trend"]):
        return None

    bull_cross = safe_float(a["ema_fast"]) > safe_float(a["ema_slow"]) and safe_float(p["ema_fast"]) <= safe_float(p["ema_slow"])
    bear_cross = safe_float(a["ema_fast"]) < safe_float(a["ema_slow"]) and safe_float(p["ema_fast"]) >= safe_float(p["ema_slow"])
    cross_side = "long" if bull_cross else "short" if bear_cross else None
    signal_ts = int(a["ts"])

    # If a crossover candle was already extended, wait up to N closed 15m bars
    # for price to revisit EMA9 and reclaim/reject it instead of chasing the cross.
    pending = sniper_retest_pending.get(symbol)
    route = "CROSS"
    side = cross_side
    if side is None and pending:
        bars_since = max(0, int((signal_ts - int(pending["ts"])) / (15 * 60 * 1000)))
        pd_side = pending["side"]
        d0 = 1 if pd_side == "long" else -1
        alignment = safe_float(a["ema_fast"]) > safe_float(a["ema_slow"]) if d0 == 1 else safe_float(a["ema_fast"]) < safe_float(a["ema_slow"])
        atr0 = safe_float(a["atr"])
        near_fast = abs(safe_float(a["close"]) - safe_float(a["ema_fast"])) <= SNIPER_RETEST_TRIGGER_ATR * atr0
        touched = safe_float(a["low"]) <= safe_float(a["ema_fast"]) + 0.10 * atr0 if d0 == 1 else safe_float(a["high"]) >= safe_float(a["ema_fast"]) - 0.10 * atr0
        reclaim = safe_float(a["close"]) > safe_float(a["ema_fast"]) and safe_float(a["close"]) > safe_float(a["open"]) if d0 == 1 else safe_float(a["close"]) < safe_float(a["ema_fast"]) and safe_float(a["close"]) < safe_float(a["open"])
        if bars_since > SNIPER_RETEST_MAX_BARS or not alignment:
            sniper_retest_pending.pop(symbol, None)
        elif bars_since >= 1 and near_fast and touched and reclaim:
            side = pd_side
            route = "RETEST"

    if side is None:
        return {
            "symbol": symbol, "side": None, "score": 0.0, "score_max": 7.0,
            "grade": "-", "skip": "no-cross", "entry_route": "WAIT",
            "price": safe_float(a["close"]), "atr": safe_float(a["atr"]),
            "signal_bar_ts": signal_ts, "adx": safe_float(a["sniper_adx"]),
        }

    d = 1 if side == "long" else -1
    close = safe_float(a["close"])
    score, score_max, rsi, adx, atr, vol_ratio, volume_above = sniper_evidence(a, p, side)
    ratio = score / score_max
    grade = sniper_grade(ratio)
    plan = sniper_plan(df, side)
    htf = sniper_htf_context(symbol)
    htf_aligned = htf["dir"] == d
    htf_opposed = htf["dir"] == -d
    htf_state = "ALIGNED" if htf_aligned else "AGAINST" if htf_opposed else "NEUTRAL"

    momentum = close > safe_float(a["ema_fast"]) and close > safe_float(a["ema_slow"]) if d == 1 else close < safe_float(a["ema_fast"]) and close < safe_float(a["ema_slow"])
    high_vol = vol_ratio > SNIPER_VOL_THRESHOLD
    body_atr = d * (close - safe_float(a["open"])) / atr if atr > 0 else 0.0
    slope_atr = d * (safe_float(a["ema_fast"]) - safe_float(p["ema_fast"])) / atr if atr > 0 else 0.0
    extension_atr = d * (close - safe_float(a["ema_fast"])) / atr if atr > 0 else 999.0

    skip = None
    if high_vol:
        skip = "high-vol"
    elif not momentum:
        skip = "price-momentum-lost"
    elif (d == 1 and rsi >= SNIPER_RSI_UPPER) or (d == -1 and rsi <= SNIPER_RSI_LOWER):
        skip = "rsi-extreme"
    elif slope_atr < SNIPER_MIN_CROSS_SLOPE_ATR:
        skip = f"weak-cross-slope-{slope_atr:.2f}ATR"
    elif route == "CROSS" and extension_atr > SNIPER_MAX_ENTRY_EXTENSION_ATR:
        sniper_retest_pending[symbol] = {"side": side, "ts": signal_ts}
        skip = f"wait-retest-ext-{extension_atr:.2f}ATR"
    elif route == "RETEST" and (ratio < SNIPER_RETEST_MIN_RATIO or (adx < SNIPER_RETEST_MIN_ADX and not volume_above)):
        skip = "retest-quality"
    elif htf_opposed and not (ratio >= SNIPER_COUNTER_MIN_RATIO and volume_above and adx >= SNIPER_COUNTER_MIN_ADX and body_atr >= SNIPER_COUNTER_MIN_BODY_ATR):
        skip = "1h-against-needs-A+-vol"
    elif not htf_aligned and not htf_opposed and ratio < SNIPER_HTF_NEUTRAL_MIN_RATIO:
        skip = "1h-neutral-needs-A"
    elif ratio + 1e-9 < SNIPER_MIN_EVIDENCE_RATIO:
        skip = f"evidence-{score:.1f}/{score_max:.1f}"
    elif not plan or plan["risk_distance"] <= 0:
        skip = "invalid-plan"
    else:
        tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
        if plan["risk_distance"] < SNIPER_MIN_RISK_TICKS * tick - tick * 1e-6:
            skip = "risk-too-small"

    # A successful retest consumes the pending setup. A new valid non-extended
    # cross also supersedes any older pending setup.
    if route == "RETEST" and skip is None:
        sniper_retest_pending.pop(symbol, None)
    elif route == "CROSS" and skip != f"wait-retest-ext-{extension_atr:.2f}ATR":
        sniper_retest_pending.pop(symbol, None)

    return {
        "symbol": symbol, "side": side, "score": score, "score_max": score_max,
        "threshold": SNIPER_MIN_EVIDENCE_RATIO * score_max, "grade": grade,
        "skip": skip, "entry_route": route if skip is None else "WAIT",
        "price": close, "atr": atr, "adx": adx, "rsi": rsi,
        "signal_bar_ts": signal_ts, "vol_ratio": vol_ratio,
        "volume_above": volume_above, "vwap": safe_float(a["vwap"]),
        "risk_distance": plan["risk_distance"] if plan else 0,
        "stop_origin": plan["origin"] if plan else "N/A", "structure_capped": bool(plan["capped"]) if plan else False,
        "htf_dir": htf["dir"], "htf_state": htf_state, "htf_adx": htf["adx"],
        "body_atr": body_atr, "slope_atr": slope_atr, "extension_atr": extension_atr,
        "tactics": int(round(score)), "entry_quality": int(round(ratio * 5)),
        "confirm_components": 4 if skip is None else 0, "momentum_quality": 2 if momentum else 0,
        "pullback_quality": 0, "rel": 0.0, "btc_regime": "SNIPER-SMART-CONTEXT",
    }

def is_a_setup(signal):
    # Pine grades are evidence labels, not risk-sizing tiers. Keep sizing neutral
    # so we can evaluate the strategy rather than confounding it with bigger bets.
    return False


def sizing_profile(signal):
    return {"name": "SNIPER", "risk_pct": SNIPER_RISK_EQUITY_PCT, "target_net_tp": SNIPER_TARGET_NET_TP_USDT}


def allowed_fill_slip(signal):
    return SNIPER_MAX_FILL_DRIFT


def sniper_live_levels(signal, live, tick):
    risk = safe_float(signal.get("risk_distance"))
    if live <= 0 or risk <= 0:
        return None
    side = signal["side"]
    d = 1 if side == "long" else -1
    raw_sl = live - d * risk
    raw_t1 = live + d * risk
    raw_t2 = live + d * risk * 2.0
    raw_t3 = live + d * risk * 3.0
    sl = round_trade_price(raw_sl, tick, side, "sl")
    t1 = round_trade_price(raw_t1, tick, side, "tp")
    t2 = round_trade_price(raw_t2, tick, side, "tp")
    t3 = round_trade_price(raw_t3, tick, side, "tp")
    if side == "long" and not (sl < live < t1 < t2 < t3): return None
    if side == "short" and not (t3 < t2 < t1 < live < sl): return None
    return sl, t1, t2, t3, abs(live - sl)


def open_trade(signal):
    global protection_lock
    symbol, side = signal["symbol"], signal["side"]
    account = futures_account()
    equity, available = account["total_equity"], account["available"]
    reset_daily_if_needed(equity)
    if equity < MIN_EQUITY_USDT:
        tg(f"⛔ RISK STOP\nEquity ${equity:.2f} < ${MIN_EQUITY_USDT:.2f}")
        return False
    if available <= 0 or (not PAPER_TRADING and protection_lock):
        return False

    tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
    live = safe_float(exchange.fetch_ticker(symbol).get("last"), signal["price"])
    signal_px = safe_float(signal["price"])
    if live <= 0 or signal_px <= 0:
        return False
    drift = (live - signal_px) / signal_px
    d = 1 if side == "long" else -1
    adverse_drift = d * drift
    if adverse_drift > SNIPER_MAX_FILL_DRIFT:
        tg(f"⏭️ {symbol} {side.upper()} SNIPER too late\nAdverse drift {adverse_drift*100:.2f}% > {SNIPER_MAX_FILL_DRIFT*100:.2f}%")
        return False

    levels = sniper_live_levels(signal, live, tick)
    if not levels:
        return False
    sl, tp1, tp2, tp3, risk = levels
    stop_pct = risk / live
    profile = sizing_profile(signal)
    sizing = calculate_contracts(symbol, live, sl, equity, available, profile["risk_pct"], profile["target_net_tp"])
    if not sizing:
        return False

    before_ids = {position_id(p) for p in get_positions() if position_id(p)}
    lev_ok, lev_result = set_leverage(symbol, LEVERAGE)
    if not lev_ok:
        tg(f"⛔ LEVERAGE FAILED\n{symbol}\nRequested: {LEVERAGE}x\n{lev_result}\nTrade skipped.")
        return False

    payload = {
        "instId": blofin_inst_id(symbol), "marginMode": MARGIN_MODE, "positionSide": "net",
        "side": "buy" if side == "long" else "sell", "orderType": "market",
        "size": str(sizing["contracts"]),
        "tpTriggerPrice": str(tp3), "tpOrderPrice": "-1", "tpTriggerPriceType": "mark",
        "slTriggerPrice": str(sl), "slOrderPrice": "-1", "slTriggerPriceType": "mark",
        "clientOrderId": f"ps21{int(time.time()*1000)%1000000000}",
    }
    if BROKER_ID: payload["brokerId"] = BROKER_ID

    msg = (
        f"{'🧪 PAPER' if PAPER_TRADING else '🎯 PRECISION SNIPER LIVE'} v{SNIPER_VERSION}\n"
        f"{symbol} {side.upper()} · Grade {signal['grade']} · {signal.get('entry_route','CROSS')}\n"
        f"Evidence {signal['score']:.1f}/{signal['score_max']:.1f} ({signal['score']/signal['score_max']*100:.0f}%)\n"
        f"ADX {signal['adx']:.1f} · RSI {signal['rsi']:.1f} · ATR/mean {signal['vol_ratio']:.2f}x\n"
        f"1H: {signal.get('htf_state','N/A')} ADX{signal.get('htf_adx',0):.0f} · Vol spike: {'YES' if signal['volume_above'] else 'NO'}\n"
        f"Cross slope {signal.get('slope_atr',0):.2f} ATR · Extension {signal.get('extension_atr',0):.2f} ATR · Body {signal.get('body_atr',0):.2f} ATR\n"
        f"Stop: {signal['stop_origin']}\n"
        f"Entry ~ {live:.8g}\nSL {sl:.8g}\nTP1 {tp1:.8g} → SL to entry\n"
        f"TP2 {tp2:.8g} → SL to TP1\nTP3 {tp3:.8g} → full exit\n"
        f"Initial risk {stop_pct*100:.2f}% · Risk cap {profile['risk_pct']*100:.1f}%\n"
        f"Expected TP3 net ~${sizing['expected_net_tp_usdt']:.2f}\n"
        f"Expected SL + cost ~${sizing['expected_net_sl_usdt']:.2f}\n"
        f"Notional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x\n"
        f"Fill drift signed {drift*100:+.2f}% · adverse {max(0.0, adverse_drift)*100:.2f}%/{SNIPER_MAX_FILL_DRIFT*100:.2f}%"
    )
    if PAPER_TRADING:
        tg(msg)
        return True

    resp = native("private_post_trade_order", payload)
    if not native_ok(resp):
        err = response_error(resp)
        if "152012" in err:
            protection_lock = True
        tg(f"❌ PRECISION SNIPER ORDER FAILED\n{symbol} {side.upper()}\n{err}")
        return False

    tg(msg)
    new_pos = find_new_position(symbol, before_ids, timeout=8)
    if new_pos is None:
        tg(f"⚠️ {symbol}: order accepted, position not visible yet.")
        return True
    if has_live_protection(new_pos)[0]:
        pid = position_id(new_pos)
        if pid: sniper_managed_positions.add(pid)
        tg(f"🛡️ SNIPER PROTECTION CONFIRMED\n{symbol} {side.upper()}\nTP3 + SL visible.")
        return True

    last = safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side, sl, tp3, last):
        ok, result = place_tpsl_for_position(new_pos, sl, tp3, client_tag="psfb")
        if ok:
            pid = position_id(new_pos)
            if pid: sniper_managed_positions.add(pid)
            tg(f"🛡️ SNIPER FALLBACK TP3 + SL CONFIRMED\n{symbol}")
            return True
        tg(f"🚨 CRITICAL SNIPER PROTECTION FAILURE\n{symbol}\n{result}\nNEW TRADES PAUSED.")
        protection_lock = True
        return True
    tg(f"🚨 {symbol}: position exists but fallback protection cannot safely be placed.\nNEW TRADES PAUSED.")
    protection_lock = True
    return True


def amend_tpsl_stop(p, new_sl):
    ok, order = has_live_protection(p)
    if not ok or not order:
        return False, "No live TP/SL to amend"
    tpsl_id = str(order.get("tpslId", order.get("algoId", "")) or "")
    client_id = str(order.get("clientOrderId", "") or "")
    if not tpsl_id and not client_id:
        return False, "TP/SL id missing"
    payload = {
        "instId": p["instId"],
        "requestId": f"psa{int(time.time()*1000)%1000000000}",
        "newSlTriggerPrice": str(new_sl),
        "newSlTriggerPriceType": "mark",
        "newSlOrderPrice": "-1",
    }
    if tpsl_id: payload["tpslId"] = tpsl_id
    else: payload["clientOrderId"] = client_id
    resp = native("private_post_trade_amend_tpsl", payload)
    if not native_ok(resp):
        return False, response_error(resp)
    time.sleep(0.5)
    ok2, order2 = has_live_protection(p)
    if not ok2:
        return False, "Amend accepted but TP+SL not visible"
    actual = safe_float(order2.get("slTriggerPrice"))
    return (abs(actual - new_sl) <= max(abs(new_sl)*1e-8, 1e-12)), order2


def sniper_recent_extreme(symbol, side):
    """Catch TP milestone touches between 45s scans using the latest 1m candles."""
    try:
        rows = exchange.fetch_ohlcv(symbol, timeframe="1m", limit=3)
        if rows:
            if side == "long": return max(safe_float(r[2]) for r in rows)
            return min(safe_float(r[3]) for r in rows)
    except Exception:
        pass
    return safe_float(exchange.fetch_ticker(symbol).get("last"))


def manage_sniper_step_stops():
    """Pine default: TP1->entry, TP2->TP1, TP3 remains full-position TP."""
    if PAPER_TRADING or not SNIPER_STEP_STOPS:
        return
    for p in get_positions():
        symbol = position_symbol(p)
        side = position_side(p)
        entry = position_entry(p)
        pid = position_id(p)
        if symbol not in SYMBOLS or entry <= 0:
            continue
        # Never reinterpret an older V6.x position as a Sniper trade. During the
        # same process we know exactly which positions this engine opened. After
        # a process restart, only auto-adopt positions whose active TPSL carries
        # our ps21/psfb client tag; otherwise leave their protection untouched.
        if pid not in sniper_managed_positions:
            try:
                _, probe = has_live_protection(p)
                cid = str((probe or {}).get("clientOrderId", "") or "")
                if cid.startswith(("ps21", "psfb")):
                    sniper_managed_positions.add(pid)
                else:
                    continue
            except Exception:
                continue
        ok, order = has_live_protection(p)
        if not ok or not order:
            continue
        tp3 = safe_float(order.get("tpTriggerPrice"))
        current_sl = safe_float(order.get("slTriggerPrice"))
        if tp3 <= 0 or current_sl <= 0:
            continue
        risk = abs(tp3 - entry) / 3.0
        if risk <= 0:
            continue
        d = 1 if side == "long" else -1
        tp1 = entry + d * risk
        tp2 = entry + d * 2.0 * risk
        tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
        extreme = sniper_recent_extreme(symbol, side)
        hit1 = extreme >= tp1 if side == "long" else extreme <= tp1
        hit2 = extreme >= tp2 if side == "long" else extreme <= tp2
        desired = None
        stage = None
        if hit2:
            desired = round_trade_price(tp1, tick, side, "sl")
            stage = "TP2"
        elif hit1:
            desired = round_trade_price(entry, tick, side, "sl")
            stage = "TP1"
        if desired is None:
            continue
        improves = desired > current_sl + tick*0.1 if side == "long" else desired < current_sl - tick*0.1
        if not improves:
            continue
        ok_amend, result = amend_tpsl_stop(p, desired)
        key = f"{position_id(p)}:{stage}:{desired}"
        if ok_amend:
            if not sniper_step_notified.get(key):
                tg(f"🪜 SNIPER STEP STOP\n{symbol} {side.upper()}\n{stage} touched → SL moved to {desired:.8g}\nTP3 stays active.")
                sniper_step_notified[key] = True
        else:
            tg(f"⚠️ SNIPER STEP AMEND FAILED\n{symbol} {side.upper()}\n{stage} → {desired:.8g}\n{result}")


def repair_one_position(p):
    """Emergency startup protection; uses 1.5 ATR / 3R if original plan is unavailable."""
    symbol, side, entry = position_symbol(p), position_side(p), position_entry(p)
    if entry <= 0:
        return False
    if has_live_protection(p)[0]:
        return True
    df = sniper_indicators(ohlcv_df(symbol, SNIPER_TF))
    atr = safe_float(df.iloc[-2]["atr"])
    tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
    risk = atr * SNIPER_SL_MULT
    if atr <= 0 or risk <= 0:
        return False
    d = 1 if side == "long" else -1
    sl = round_trade_price(entry - d*risk, tick, side, "sl")
    tp3 = round_trade_price(entry + d*risk*3.0, tick, side, "tp")
    last = safe_float(exchange.fetch_ticker(symbol).get("last"))
    if not tpsl_prices_valid(side, sl, tp3, last):
        return False
    return place_tpsl_for_position(p, sl, tp3, client_tag="psrepair")[0]


def maybe_score_report(rows):
    global sniper_last_report
    now = time.time()
    crosses = [r for r in rows if r.get("side")]
    if not crosses or now - sniper_last_report < SNIPER_REPORT_MIN * 60:
        return
    sniper_last_report = now
    lines = [f"🎯 PRECISION SNIPER v{SNIPER_VERSION} · crossover scan"]
    for r in crosses[:10]:
        state = "🟢 READY" if not r.get("skip") else f"⏭ {r.get('skip')}"
        lines.append(
            f"{r['symbol'].split('/')[0]} {r['side'].upper()} {r['grade']} "
            f"{r['score']:.1f}/{r['score_max']:.1f} ADX{r['adx']:.0f} RSI{r['rsi']:.0f} "
            f"1H:{r.get('htf_state','-')} Ext{r.get('extension_atr',0):.2f} ATRμ{r['vol_ratio']:.2f} {state}"
        )
    tg("\n".join(lines))


def startup_diagnostics():
    acc = futures_account()
    reset_daily_if_needed(acc["total_equity"])
    tg(
        f"🎯 PRECISION SNIPER BOT STARTED v{SNIPER_VERSION}\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"Engine: Precision Sniper + v2.2 Smart Context\n"
        f"Signal: 15m CLOSED candle · EMA {SNIPER_FAST}/{SNIPER_SLOW}/{SNIPER_TREND}\n"
        f"Mandatory trigger: EMA{SNIPER_FAST}/{SNIPER_SLOW} crossover + price momentum\n"
        f"Evidence: trend/RSI/MACD/accel/ADX/volume/VWAP · base >= {SNIPER_MIN_EVIDENCE_RATIO*100:.0f}%\n"
        f"Smart 1H gate: neutral >=A | countertrend only A+ + volume + ADX{SNIPER_COUNTER_MIN_ADX:.0f}\n"
        f"Anti-chase: >{SNIPER_MAX_ENTRY_EXTENSION_ATR:.2f} ATR waits for EMA9 retest up to {SNIPER_RETEST_MAX_BARS} bars\n"
        f"High-vol block: ATR/mean > {SNIPER_VOL_THRESHOLD:.2f}x\n"
        f"SL: {SNIPER_SL_MULT:.1f} ATR + structure, capped at {SNIPER_STRUCTURE_CAP:.1f}x ATR-stop\n"
        f"TP ladder: 1R milestone → BE | 2R → lock 1R | 3R full exit\n"
        f"Risk cap: {SNIPER_RISK_EQUITY_PCT*100:.1f}% · Max positions: {MAX_ACTIVE_POSITIONS} · same-side soft cap {SNIPER_MAX_SAME_SIDE}\n"
        f"Equity: ${acc['total_equity']:.2f} · Available: ${acc['available']:.2f}\n"
        f"Daily loss cap: {DAILY_LOSS_LIMIT_PCT:.0f}%"
    )


def protection_watch():
    global protection_lock
    if PAPER_TRADING:
        protection_lock = False
        return
    positions = get_positions()
    if not positions:
        protection_lock = False
        return
    # Do not rewrite protection on positions inherited from another strategy.
    # Only positions opened by this Sniper process may be auto-repaired.
    for p in positions:
        try:
            if has_live_protection(p)[0]:
                continue
            pid = position_id(p)
            if REPAIR_EXISTING_POSITIONS and pid in sniper_managed_positions and repair_one_position(p):
                tg(f"🛡️ SNIPER REPAIRED {position_symbol(p)} {position_side(p).upper()}")
        except Exception:
            pass
    protection_lock = not all_positions_protected()

# ============================================================
# MAIN — PRECISION SNIPER ONLY
# ============================================================
def main():
    global protection_lock
    load_learning()
    startup_diagnostics()
    if not PAPER_TRADING:
        seed_closed_history()
        try:
            inherited = get_positions()
            if inherited:
                # Safe strategy handover: leave V6.x/other-strategy TP/SL untouched.
                # The Sniper engine starts managing step stops only for positions it opens itself.
                protection_lock = not all_positions_protected()
                tg(f"ℹ️ STRATEGY HANDOVER\n{len(inherited)} existing position(s) detected. Existing TP/SL left untouched." +
                   ("\n⚠️ One or more positions are unprotected: new trades locked." if protection_lock else "\n✅ Existing protection verified."))
            else:
                protection_lock = False
        except Exception as e:
            protection_lock = True
            tg(f"🚨 STARTUP PROTECTION CHECK FAILED\n{e}")

    while True:
        try:
            if not PAPER_TRADING:
                try:
                    check_for_closed_positions()
                except Exception as e:
                    tg(f"⚠️ PNL TRACKER ERROR\n{e}")
                try:
                    manage_sniper_step_stops()
                except Exception as e:
                    tg(f"⚠️ SNIPER STEP MANAGER ERROR\n{e}")

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
                protection_watch()
                positions = get_positions()
            if protection_lock and not PAPER_TRADING:
                time.sleep(SCAN_SECONDS)
                continue

            scanned, candidates = [], []
            existing_symbols = {position_symbol(p) for p in positions}
            for symbol in SYMBOLS:
                if not cooldown_ok(symbol):
                    continue
                try:
                    signal = evaluate_symbol(symbol)
                    if not signal:
                        continue
                    scanned.append(signal)
                    if signal.get("side") and not signal.get("skip") and signal.get("entry_route") in ("CROSS", "RETEST", "FAILED-CROSS"):
                        # A closed crossover bar is a one-shot Precision Sniper event.
                        key = int(signal.get("signal_bar_ts", 0))
                        if sniper_last_signal_bar.get(symbol) == key:
                            continue
                        if symbol in existing_symbols:
                            sniper_last_signal_bar[symbol] = key
                            continue
                        candidates.append(signal)
                except Exception:
                    continue

            maybe_score_report(scanned)
            # Pine grades are descriptive. For simultaneous candidates, rank by
            # evidence ratio, then ADX; no BTC-flow score is mixed into this test.
            candidates.sort(key=lambda x: (
                1 if x.get("htf_state") == "ALIGNED" else 0,
                1 if x.get("entry_route") == "RETEST" else 0,
                x["score"] / max(x["score_max"], 1e-9),
                1 if x.get("volume_above") else 0,
                x["adx"],
            ), reverse=True)

            attempts = 0
            for signal in candidates:
                if len(get_positions()) >= MAX_ACTIVE_POSITIONS:
                    break
                if attempts >= MAX_EXECUTION_ATTEMPTS_PER_SCAN:
                    break
                current_positions = get_positions()
                allowed, why = sniper_portfolio_allows(signal, current_positions)
                if not allowed:
                    sniper_last_signal_bar[signal["symbol"]] = int(signal.get("signal_bar_ts", 0))
                    tg(f"⏭️ {signal['symbol']} {signal['side'].upper()} SMART PORTFOLIO\n{why}")
                    continue
                attempts += 1
                opened = open_trade(signal)
                sniper_last_signal_bar[signal["symbol"]] = int(signal.get("signal_bar_ts", 0))
                if opened:
                    symbol_last_trade[signal["symbol"]] = time.time()
                    # Preserve the existing bot behavior: one successful new order
                    # per 45s scan; a second candidate can enter on the next scan.
                    break

            time.sleep(SCAN_SECONDS)

        except KeyboardInterrupt:
            tg("🛑 Precision Sniper bot stopped.")
            raise
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if "152002" in msg:
                time.sleep(20)
                continue
            tg(f"⚠️ PRECISION SNIPER LOOP ERROR\n{msg}")
            time.sleep(30)


# ============================================================
# PRECISION SNIPER V3.0 — REGIME-FIRST EXECUTION ENGINE
# ============================================================
# V3 deliberately stops treating every EMA9/21 cross as a trade candidate.
# A cross is now only a TRIGGER inside a confirmed multi-timeframe regime.
# Core order/protection/risk infrastructure from v2.2 is retained.

SNIPER_VERSION = "3.0.0"
SNIPER_V3_MIN_RATIO = float(os.getenv("SNIPER_V3_MIN_RATIO", "0.71"))          # >= 5/7 (A)
SNIPER_V3_ELITE_RATIO = float(os.getenv("SNIPER_V3_ELITE_RATIO", "0.85"))      # >= 6/7 (A+)
SNIPER_V3_MIN_SLOPE_ATR = float(os.getenv("SNIPER_V3_MIN_SLOPE_ATR", "0.08"))
SNIPER_V3_MIN_BODY_ATR = float(os.getenv("SNIPER_V3_MIN_BODY_ATR", "0.20"))
SNIPER_V3_MAX_CROSS_EXT_ATR = float(os.getenv("SNIPER_V3_MAX_CROSS_EXT_ATR", "0.55"))
SNIPER_V3_MIN_ADX = float(os.getenv("SNIPER_V3_MIN_ADX", "18"))
SNIPER_V3_STRONG_HTF_ADX = float(os.getenv("SNIPER_V3_STRONG_HTF_ADX", "25"))
SNIPER_V3_LONG_RSI_MIN = float(os.getenv("SNIPER_V3_LONG_RSI_MIN", "53"))
SNIPER_V3_LONG_RSI_MAX = float(os.getenv("SNIPER_V3_LONG_RSI_MAX", "68"))
SNIPER_V3_SHORT_RSI_MIN = float(os.getenv("SNIPER_V3_SHORT_RSI_MIN", "32"))
SNIPER_V3_SHORT_RSI_MAX = float(os.getenv("SNIPER_V3_SHORT_RSI_MAX", "47"))
SNIPER_V3_RUN2_MAX_ATR = float(os.getenv("SNIPER_V3_RUN2_MAX_ATR", "3.0"))
SNIPER_V3_RUN4_MAX_ATR = float(os.getenv("SNIPER_V3_RUN4_MAX_ATR", "4.5"))
SNIPER_V3_RETEST_MIN_RATIO = float(os.getenv("SNIPER_V3_RETEST_MIN_RATIO", "0.71"))
SNIPER_V3_MAX_SAME_SIDE = int(os.getenv("SNIPER_V3_MAX_SAME_SIDE", "2"))
SNIPER_V3_THIRD_SIDE_RATIO = float(os.getenv("SNIPER_V3_THIRD_SIDE_RATIO", "0.85"))
SNIPER_V3_BTC_GATE = os.getenv("SNIPER_V3_BTC_GATE", "true").lower() == "true"
SNIPER_V3_BTC_SYMBOL = os.getenv("SNIPER_V3_BTC_SYMBOL", "BTC/USDT:USDT")

_sniper_btc_cache = {"ts": 0.0, "data": None}


def _sniper_tf_state(df):
    if df is None or len(df) < max(SNIPER_TREND * 3, 80) + 2:
        return {"dir": 0, "adx": 0.0, "rsi": 50.0, "close": 0.0, "ema_trend": 0.0}
    h = df.iloc[-2]
    c = safe_float(h["close"])
    ef, es, et = safe_float(h["ema_fast"]), safe_float(h["ema_slow"]), safe_float(h["ema_trend"])
    # Stronger than v2.2: the full EMA stack must agree, not just fast/slow + price/EMA55.
    direction = 1 if ef > es > et and c > ef else -1 if ef < es < et and c < ef else 0
    return {
        "dir": direction,
        "adx": safe_float(h["sniper_adx"]),
        "rsi": safe_float(h["sniper_rsi"], 50),
        "close": c,
        "ema_trend": et,
    }


def sniper_v3_context(symbol):
    try:
        h1 = _sniper_tf_state(sniper_indicators(ohlcv_df(symbol, "1h")))
        h4 = _sniper_tf_state(sniper_indicators(ohlcv_df(symbol, "4h")))
        return h1, h4
    except Exception:
        return ({"dir": 0, "adx": 0.0, "rsi": 50.0, "close": 0.0, "ema_trend": 0.0},
                {"dir": 0, "adx": 0.0, "rsi": 50.0, "close": 0.0, "ema_trend": 0.0})


def sniper_v3_btc_context():
    now = time.time()
    if _sniper_btc_cache.get("data") is not None and now - _sniper_btc_cache.get("ts", 0) < 60:
        return _sniper_btc_cache["data"]
    h1, h4 = sniper_v3_context(SNIPER_V3_BTC_SYMBOL)
    data = {"h1": h1, "h4": h4}
    _sniper_btc_cache.update({"ts": now, "data": data})
    return data


def _sniper_v3_btc_gate(symbol, d, ratio, volume_above):
    if not SNIPER_V3_BTC_GATE or symbol == SNIPER_V3_BTC_SYMBOL:
        return True, "BTC-self/off"
    btc = sniper_v3_btc_context()
    b1, b4 = btc["h1"]["dir"], btc["h4"]["dir"]
    # If both BTC layers point against the alt trade, do not fight the market.
    if b1 == -d and b4 == -d:
        return False, "btc-1h+4h-against"
    # One opposing BTC layer is tolerated only for an elite, volume-confirmed setup.
    if b1 == -d or b4 == -d:
        if ratio < SNIPER_V3_ELITE_RATIO or not volume_above:
            return False, "btc-partly-against-needs-A+-vol"
    return True, f"BTC1H{b1:+d}/4H{b4:+d}"


def evaluate_symbol(symbol, btc=None):
    """V3: regime first -> location -> trigger. No neutral/countertrend 1H entries."""
    df = sniper_indicators(ohlcv_df(symbol, SNIPER_TF))
    warmup = max(SNIPER_TREND * 3, SNIPER_ATR_LEN + SNIPER_ATR_MEAN_LEN)
    if len(df) < warmup + 20:
        return None
    a, p = df.iloc[-2], df.iloc[-3]
    if any(pd.isna(a.get(k)) for k in ["atr", "atr_mean", "sniper_rsi", "macd_hist", "sniper_adx", "ema_fast", "ema_slow", "ema_trend"]):
        return None

    bull_cross = safe_float(a["ema_fast"]) > safe_float(a["ema_slow"]) and safe_float(p["ema_fast"]) <= safe_float(p["ema_slow"])
    bear_cross = safe_float(a["ema_fast"]) < safe_float(a["ema_slow"]) and safe_float(p["ema_fast"]) >= safe_float(p["ema_slow"])
    cross_side = "long" if bull_cross else "short" if bear_cross else None
    signal_ts = int(a["ts"])

    pending = sniper_retest_pending.get(symbol)
    route = "CROSS"
    side = cross_side
    if side is None and pending:
        bars_since = max(0, int((signal_ts - int(pending["ts"])) / (15 * 60 * 1000)))
        pd_side = pending["side"]
        d0 = 1 if pd_side == "long" else -1
        alignment = safe_float(a["ema_fast"]) > safe_float(a["ema_slow"]) if d0 == 1 else safe_float(a["ema_fast"]) < safe_float(a["ema_slow"])
        atr0 = safe_float(a["atr"])
        touched = safe_float(a["low"]) <= safe_float(a["ema_fast"]) + 0.10 * atr0 if d0 == 1 else safe_float(a["high"]) >= safe_float(a["ema_fast"]) - 0.10 * atr0
        reclaim = (safe_float(a["close"]) > safe_float(a["ema_fast"]) and safe_float(a["close"]) > safe_float(a["open"])) if d0 == 1 else (safe_float(a["close"]) < safe_float(a["ema_fast"]) and safe_float(a["close"]) < safe_float(a["open"]))
        if bars_since > SNIPER_RETEST_MAX_BARS or not alignment:
            sniper_retest_pending.pop(symbol, None)
        elif bars_since >= 1 and touched and reclaim:
            side = pd_side
            route = "RETEST"

    if side is None:
        return {
            "symbol": symbol, "side": None, "score": 0.0, "score_max": 7.0, "grade": "-",
            "skip": "no-cross", "entry_route": "WAIT", "price": safe_float(a["close"]),
            "atr": safe_float(a["atr"]), "signal_bar_ts": signal_ts, "adx": safe_float(a["sniper_adx"]),
        }

    d = 1 if side == "long" else -1
    close = safe_float(a["close"])
    score, score_max, rsi, adx, atr, vol_ratio, volume_above = sniper_evidence(a, p, side)
    ratio = score / max(score_max, 1e-9)
    grade = sniper_grade(ratio)
    plan = sniper_plan(df, side)
    h1, h4 = sniper_v3_context(symbol)
    h1_state = "ALIGNED" if h1["dir"] == d else "AGAINST" if h1["dir"] == -d else "NEUTRAL"
    h4_state = "ALIGNED" if h4["dir"] == d else "AGAINST" if h4["dir"] == -d else "NEUTRAL"

    ef, es, et = safe_float(a["ema_fast"]), safe_float(a["ema_slow"]), safe_float(a["ema_trend"])
    hist, hist_prev = safe_float(a["macd_hist"]), safe_float(p["macd_hist"])
    momentum = (close > ef > es) if d == 1 else (close < ef < es)
    trend55 = close > et if d == 1 else close < et
    macd_sign = hist > 0 if d == 1 else hist < 0
    macd_accel = hist > hist_prev if d == 1 else hist < hist_prev
    body_atr = d * (close - safe_float(a["open"])) / atr if atr > 0 else 0.0
    slope_atr = d * (ef - safe_float(p["ema_fast"])) / atr if atr > 0 else 0.0
    extension_atr = d * (close - ef) / atr if atr > 0 else 999.0
    high_vol = vol_ratio > SNIPER_VOL_THRESHOLD

    # Location memory: how far this coin already travelled before the trigger.
    close_2h = safe_float(df.iloc[-10]["close"]) if len(df) >= 10 else close
    close_4h = safe_float(df.iloc[-18]["close"]) if len(df) >= 18 else close
    run2_atr = d * (close - close_2h) / atr if atr > 0 else 0.0
    run4_atr = d * (close - close_4h) / atr if atr > 0 else 0.0

    rsi_ok = (SNIPER_V3_LONG_RSI_MIN <= rsi <= SNIPER_V3_LONG_RSI_MAX) if d == 1 else (SNIPER_V3_SHORT_RSI_MIN <= rsi <= SNIPER_V3_SHORT_RSI_MAX)
    adx_ok = adx >= SNIPER_V3_MIN_ADX or (volume_above and h1["adx"] >= SNIPER_V3_STRONG_HTF_ADX)
    btc_ok, btc_note = _sniper_v3_btc_gate(symbol, d, ratio, volume_above)

    skip = None
    wait_retest = False
    if high_vol:
        skip = "high-vol"
    elif not momentum or not trend55:
        skip = "15m-trend-not-clean"
    elif not (macd_sign and macd_accel):
        skip = "macd-not-confirmed"
    elif not rsi_ok:
        skip = f"rsi-quality-{rsi:.0f}"
    elif h1["dir"] != d:
        skip = f"1h-{h1_state.lower()}-blocked"
    elif h4["dir"] == -d:
        skip = "4h-against-blocked"
    elif h4["dir"] == 0 and ratio < SNIPER_V3_ELITE_RATIO:
        skip = "4h-neutral-needs-A+"
    elif ratio + 1e-9 < SNIPER_V3_MIN_RATIO:
        skip = f"needs-A-{score:.1f}/{score_max:.1f}"
    elif not adx_ok:
        skip = f"weak-trend-adx{adx:.0f}"
    elif slope_atr < SNIPER_V3_MIN_SLOPE_ATR:
        skip = f"weak-cross-slope-{slope_atr:.2f}ATR"
    elif body_atr < SNIPER_V3_MIN_BODY_ATR:
        skip = f"weak-trigger-body-{body_atr:.2f}ATR"
    elif not btc_ok:
        skip = btc_note
    elif route == "CROSS" and (extension_atr > SNIPER_V3_MAX_CROSS_EXT_ATR or run2_atr > SNIPER_V3_RUN2_MAX_ATR or run4_atr > SNIPER_V3_RUN4_MAX_ATR):
        sniper_retest_pending[symbol] = {"side": side, "ts": signal_ts}
        skip = f"wait-retest-loc-ext{extension_atr:.2f}-run2{run2_atr:.1f}R"
        wait_retest = True
    elif route == "RETEST" and ratio < SNIPER_V3_RETEST_MIN_RATIO:
        skip = "retest-needs-A"
    elif not plan or plan["risk_distance"] <= 0:
        skip = "invalid-plan"
    else:
        tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
        if plan["risk_distance"] < SNIPER_MIN_RISK_TICKS * tick - tick * 1e-6:
            skip = "risk-too-small"

    if route == "RETEST" and skip is None:
        sniper_retest_pending.pop(symbol, None)
    elif route == "CROSS" and not wait_retest:
        sniper_retest_pending.pop(symbol, None)

    quality = (
        ratio * 100
        + (12 if h4["dir"] == d else 0)
        + (8 if volume_above else 0)
        + min(adx, 35) * 0.35
        + min(h1["adx"], 40) * 0.20
        - max(0.0, extension_atr - 0.35) * 12
        - max(0.0, run2_atr - 1.5) * 3
    )

    return {
        "symbol": symbol, "side": side, "score": score, "score_max": score_max,
        "threshold": SNIPER_V3_MIN_RATIO * score_max, "grade": grade,
        "skip": skip, "entry_route": route if skip is None else "WAIT",
        "price": close, "atr": atr, "adx": adx, "rsi": rsi, "signal_bar_ts": signal_ts,
        "vol_ratio": vol_ratio, "volume_above": volume_above, "vwap": safe_float(a["vwap"]),
        "risk_distance": plan["risk_distance"] if plan else 0,
        "stop_origin": plan["origin"] if plan else "N/A", "structure_capped": bool(plan["capped"]) if plan else False,
        "htf_dir": h1["dir"], "htf_state": h1_state, "htf_adx": h1["adx"],
        "h4_dir": h4["dir"], "h4_state": h4_state, "h4_adx": h4["adx"],
        "body_atr": body_atr, "slope_atr": slope_atr, "extension_atr": extension_atr,
        "run2_atr": run2_atr, "run4_atr": run4_atr, "btc_note": btc_note,
        "quality": quality, "tactics": int(round(score)), "entry_quality": int(round(ratio * 5)),
        "confirm_components": 4 if skip is None else 0, "momentum_quality": 2 if momentum else 0,
        "pullback_quality": 0, "rel": 0.0, "btc_regime": "SNIPER-V3-REGIME",
    }


def sizing_profile(signal):
    # Keep risk controlled but let the cleanest A+ aligned setups use the existing 1.6% cap.
    ratio = safe_float(signal.get("score")) / max(safe_float(signal.get("score_max"), 1.0), 1e-9)
    elite = ratio >= SNIPER_V3_ELITE_RATIO and signal.get("h4_state") == "ALIGNED"
    risk_pct = SNIPER_RISK_EQUITY_PCT if elite else min(SNIPER_RISK_EQUITY_PCT, 0.014)
    target = SNIPER_TARGET_NET_TP_USDT if elite else min(SNIPER_TARGET_NET_TP_USDT, 1.25)
    return {"name": "V3-ELITE" if elite else "V3-A", "risk_pct": risk_pct, "target_net_tp": target}


def sniper_portfolio_allows(signal, positions):
    side = signal.get("side")
    if side not in ("long", "short"):
        return False, "no-side"
    same_side = sum(1 for p in positions if position_side(p) == side)
    ratio = safe_float(signal.get("score")) / max(safe_float(signal.get("score_max"), 1.0), 1e-9)
    # Normal portfolio: max two correlated positions in one direction.
    # A third is possible only for elite 1H+4H alignment. Four total positions remain allowed.
    if same_side >= SNIPER_V3_MAX_SAME_SIDE:
        if not (ratio >= SNIPER_V3_THIRD_SIDE_RATIO and signal.get("htf_state") == "ALIGNED" and signal.get("h4_state") == "ALIGNED" and signal.get("volume_above")):
            return False, "same-side>=2-needs-elite-1h4h-vol"
    if same_side >= 3:
        return False, "max-3-same-direction"
    if sniper_side_loss_streak.get(side, 0) >= 2:
        if not (ratio >= SNIPER_V3_ELITE_RATIO and signal.get("h4_state") == "ALIGNED" and signal.get("volume_above") and safe_float(signal.get("adx")) >= SNIPER_V3_MIN_ADX):
            return False, f"{side}-loss-streak-elite-only"
    return True, "OK"


def maybe_score_report(rows):
    global sniper_last_report
    now = time.time()
    crosses = [r for r in rows if r.get("side")]
    if not crosses or now - sniper_last_report < SNIPER_REPORT_MIN * 60:
        return
    sniper_last_report = now
    lines = [f"🎯 PRECISION SNIPER v{SNIPER_VERSION} · REGIME-FIRST"]
    ranked = sorted(crosses, key=lambda r: safe_float(r.get("quality")), reverse=True)
    for r in ranked[:10]:
        state = "🟢 READY" if not r.get("skip") else f"⏭ {r.get('skip')}"
        lines.append(
            f"{r['symbol'].split('/')[0]} {r['side'].upper()} {r['grade']} {r['score']:.1f}/{r['score_max']:.1f} "
            f"ADX{r['adx']:.0f} RSI{r['rsi']:.0f} 1H:{r.get('htf_state','-')} 4H:{r.get('h4_state','-')} "
            f"Ext{r.get('extension_atr',0):.2f} Run2{r.get('run2_atr',0):.1f}ATR {state}"
        )
    tg("\n".join(lines))


def startup_diagnostics():
    acc = futures_account()
    reset_daily_if_needed(acc["total_equity"])
    tg(
        f"🎯 PRECISION SNIPER BOT STARTED v{SNIPER_VERSION}\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"Engine: REGIME-FIRST — 1H/4H context → 15m location → EMA9/21 trigger\n"
        f"Base quality: Grade A >= {SNIPER_V3_MIN_RATIO*100:.0f}% · A+ >= {SNIPER_V3_ELITE_RATIO*100:.0f}%\n"
        f"1H: MUST align · 4H: never against; neutral requires A+\n"
        f"Trigger: MACD sign+accel · RSI quality · slope >= {SNIPER_V3_MIN_SLOPE_ATR:.2f} ATR · body >= {SNIPER_V3_MIN_BODY_ATR:.2f} ATR\n"
        f"Trend quality: ADX >= {SNIPER_V3_MIN_ADX:.0f} OR volume spike + strong 1H ADX\n"
        f"Anti-chase: cross extension > {SNIPER_V3_MAX_CROSS_EXT_ATR:.2f} ATR or large 2h/4h run → RETEST only\n"
        f"BTC gate: {'ON' if SNIPER_V3_BTC_GATE else 'OFF'} · both 1H+4H against = blocked\n"
        f"TP ladder: 1R→BE · 2R→lock 1R · 3R full exit\n"
        f"Risk: A 1.4% max · elite A+ {SNIPER_RISK_EQUITY_PCT*100:.1f}% max · Max positions {MAX_ACTIVE_POSITIONS}\n"
        f"Portfolio: normal max 2 same-side; 3rd only elite 1H+4H+volume\n"
        f"Equity: ${acc['total_equity']:.2f} · Available: ${acc['available']:.2f}\n"
        f"Daily loss cap: {DAILY_LOSS_LIMIT_PCT:.0f}%"
    )



# ============================================================
# PRECISION SNIPER V4.0 — FAILED-CROSS REVERSAL ENGINE
# ============================================================
# Radical change from V2/V3:
#   - EMA9/21 crossover is NOT an entry anymore.
#   - A crossover becomes a trap candidate.
#   - We only trade the OPPOSITE direction after the cross demonstrably fails.
#   - 5m must confirm the reversal.
#   - 1H/4H may support the reversal, but both may never strongly support the failed side.
#   - Mean-reversion exits are faster: 0.75R -> BE, 1.25R -> +0.50R, 2R final.
#   - Stale trades are actively closed; no more 48-hour dead positions.
# This is not a blind signal inversion. It waits for evidence that the original
# crossover has trapped late entrants before taking the other side.

SNIPER_VERSION = "4.0.0"
V4_CROSS_LOOKBACK_BARS = int(os.getenv("V4_CROSS_LOOKBACK_BARS", "4"))
V4_MIN_TRAP_SCORE = float(os.getenv("V4_MIN_TRAP_SCORE", "6.0"))
V4_ELITE_TRAP_SCORE = float(os.getenv("V4_ELITE_TRAP_SCORE", "8.0"))
V4_MIN_5M_CONFIRM = int(os.getenv("V4_MIN_5M_CONFIRM", "2"))
V4_MAX_EXTENSION_ATR = float(os.getenv("V4_MAX_EXTENSION_ATR", "0.65"))
V4_MAX_VOL_RATIO = float(os.getenv("V4_MAX_VOL_RATIO", "1.60"))
V4_MIN_BODY_ATR = float(os.getenv("V4_MIN_BODY_ATR", "0.22"))
V4_STRONG_BODY_ATR = float(os.getenv("V4_STRONG_BODY_ATR", "0.38"))
V4_STOP_ATR = float(os.getenv("V4_STOP_ATR", "1.20"))
V4_STOP_CAP_ATR = float(os.getenv("V4_STOP_CAP_ATR", "1.65"))
V4_TP_FINAL_R = float(os.getenv("V4_TP_FINAL_R", "2.0"))
V4_TP1_R = float(os.getenv("V4_TP1_R", "0.75"))
V4_TP2_R = float(os.getenv("V4_TP2_R", "1.25"))
V4_LOCK_R = float(os.getenv("V4_LOCK_R", "0.50"))
V4_NORMAL_RISK_PCT = float(os.getenv("V4_NORMAL_RISK_PCT", "0.014"))
V4_ELITE_RISK_PCT = float(os.getenv("V4_ELITE_RISK_PCT", "0.016"))
V4_NORMAL_TARGET_NET = float(os.getenv("V4_NORMAL_TARGET_NET", "1.25"))
V4_ELITE_TARGET_NET = float(os.getenv("V4_ELITE_TARGET_NET", "1.50"))
V4_SOFT_TIME_EXIT_MIN = int(os.getenv("V4_SOFT_TIME_EXIT_MIN", "180"))
V4_HARD_TIME_EXIT_MIN = int(os.getenv("V4_HARD_TIME_EXIT_MIN", "360"))
V4_SOFT_EXIT_MAX_R = float(os.getenv("V4_SOFT_EXIT_MAX_R", "0.35"))
V4_MAX_SAME_SIDE = int(os.getenv("V4_MAX_SAME_SIDE", "2"))
# calculate_contracts() uses this global; V4 final target is 2R, not the old 3R.
TP_R_MULT = V4_TP_FINAL_R


def _v4_closed_cross(df):
    """Most recent EMA9/21 cross 1..N closed 15m bars ago; current bar cannot be the cross."""
    closed = df.iloc[:-1]
    if len(closed) < V4_CROSS_LOOKBACK_BARS + 4:
        return None
    last_i = len(closed) - 1
    for age in range(1, V4_CROSS_LOOKBACK_BARS + 1):
        i = last_i - age
        cur, prev = closed.iloc[i], closed.iloc[i-1]
        ef, es = safe_float(cur["ema_fast"]), safe_float(cur["ema_slow"])
        pef, pes = safe_float(prev["ema_fast"]), safe_float(prev["ema_slow"])
        bull = ef > es and pef <= pes
        bear = ef < es and pef >= pes
        if bull or bear:
            return {"age": age, "dir": 1 if bull else -1, "bar": cur, "index": i}
    return None


def _v4_5m_confirm(symbol, d):
    """Three simple closed-5m reversal components; require >=2."""
    try:
        f = sniper_indicators(ohlcv_df(symbol, "5m"))
        if len(f) < 30:
            return 0, False, False, False
        a, p = f.iloc[-2], f.iloc[-3]
        c, o = safe_float(a["close"]), safe_float(a["open"])
        ef = safe_float(a["ema_fast"])
        directional = c > o and c > ef if d == 1 else c < o and c < ef
        micro = c > safe_float(p["high"]) if d == 1 else c < safe_float(p["low"])
        hist_turn = safe_float(a["macd_hist"]) > safe_float(p["macd_hist"]) if d == 1 else safe_float(a["macd_hist"]) < safe_float(p["macd_hist"])
        n = int(directional) + int(micro) + int(hist_turn)
        return n, directional, micro, hist_turn
    except Exception:
        return 0, False, False, False


def _v4_reversal_plan(df, side):
    a = df.iloc[-2]
    d = 1 if side == "long" else -1
    entry = safe_float(a["close"])
    atr = safe_float(a["atr"])
    if entry <= 0 or atr <= 0:
        return None
    recent = df.iloc[-7:-1]  # 6 closed 15m bars
    structural = (safe_float(recent["low"].min()) - 0.12*atr) if d == 1 else (safe_float(recent["high"].max()) + 0.12*atr)
    atr_stop = entry - d * V4_STOP_ATR * atr
    raw = min(atr_stop, structural) if d == 1 else max(atr_stop, structural)
    max_risk = V4_STOP_CAP_ATR * atr
    risk = min(abs(entry - raw), max_risk)
    risk = max(risk, 0.80 * atr)
    return {"risk_distance": risk, "origin": "V4 trap structure", "atr": atr}


def evaluate_symbol(symbol, btc=None):
    """V4: find a recent cross, prove it failed, then trade the opposite side."""
    df = sniper_indicators(ohlcv_df(symbol, SNIPER_TF))
    if len(df) < max(SNIPER_TREND * 3, 190):
        return None
    a, p = df.iloc[-2], df.iloc[-3]
    cross = _v4_closed_cross(df)
    signal_ts = int(a["ts"])
    if not cross:
        return {"symbol": symbol, "side": None, "score": 0.0, "score_max": 10.0,
                "grade": "-", "skip": "no-failed-cross", "entry_route": "WAIT",
                "price": safe_float(a["close"]), "atr": safe_float(a["atr"]),
                "signal_bar_ts": signal_ts, "adx": safe_float(a["sniper_adx"])}

    failed_dir = int(cross["dir"])          # direction of original crossover
    d = -failed_dir                         # V4 trades opposite direction
    side = "long" if d == 1 else "short"
    close, open_ = safe_float(a["close"]), safe_float(a["open"])
    high, low = safe_float(a["high"]), safe_float(a["low"])
    atr = safe_float(a["atr"])
    atr_mean = safe_float(a["atr_mean"])
    ef, es = safe_float(a["ema_fast"]), safe_float(a["ema_slow"])
    hist, hist_prev = safe_float(a["macd_hist"]), safe_float(p["macd_hist"])
    rsi, adx = safe_float(a["sniper_rsi"], 50), safe_float(a["sniper_adx"])
    vol_mean, volume = safe_float(a["vol_mean"]), safe_float(a["volume"])
    volume_above = vol_mean > 0 and volume > vol_mean * SNIPER_VOLUME_SPIKE
    vol_ratio = atr / atr_mean if atr_mean > 0 else 99.0
    if atr <= 0:
        return None

    # Failure evidence: price has lost the fast EMA and preferably the slow EMA,
    # candle body points in reversal direction, MACD rolls with us, RSI crosses midline.
    lost_fast = close > ef if d == 1 else close < ef
    lost_slow = close > es if d == 1 else close < es
    body_atr = d * (close - open_) / atr
    macd_turn = hist > hist_prev if d == 1 else hist < hist_prev
    macd_side = hist > 0 if d == 1 else hist < 0
    rsi_turn = rsi >= 50 if d == 1 else rsi <= 50
    candle_range = max(high - low, 1e-12)
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low
    reject_wick = lower_wick / candle_range >= 0.28 if d == 1 else upper_wick / candle_range >= 0.28
    extension_atr = d * (close - ef) / atr

    # 1H/4H are veto/context layers, not entry triggers.
    h1, h4 = sniper_v3_context(symbol)
    both_back_failed_cross = h1["dir"] == failed_dir and h4["dir"] == failed_dir
    reversal_htf = h1["dir"] == d or h4["dir"] == d
    five_n, five_dir, five_micro, five_hist = _v4_5m_confirm(symbol, d)

    # How far the original crossover travelled before failing. A small failed move is
    # better than fading a mature trend that is still structurally intact.
    cb = cross["bar"]
    cross_close = safe_float(cb["close"])
    post = df.iloc[cross["index"]:len(df)-1]
    if failed_dir == 1:
        excursion = max(0.0, safe_float(post["high"].max()) - cross_close) / atr
    else:
        excursion = max(0.0, cross_close - safe_float(post["low"].min())) / atr

    score = 0.0
    score += 1.0 if lost_fast else 0.0
    score += 2.0 if lost_slow else 0.0
    score += 1.0 if body_atr >= V4_MIN_BODY_ATR else 0.0
    score += 1.0 if macd_turn else 0.0
    score += 0.5 if macd_side else 0.0
    score += 1.0 if rsi_turn else 0.0
    score += 0.5 if reject_wick else 0.0
    score += 0.5 if volume_above else 0.0
    score += 1.5 if five_n >= V4_MIN_5M_CONFIRM else 0.0
    score += 1.0 if reversal_htf else 0.0
    score_max = 10.0
    grade = "A+" if score >= 8 else "A" if score >= 7 else "B" if score >= 6 else "C"

    plan = _v4_reversal_plan(df, side)
    skip = None
    if both_back_failed_cross:
        skip = "1h+4h-still-support-failed-side"
    elif not lost_fast:
        skip = "cross-not-failed-yet"
    elif not (lost_slow or body_atr >= V4_STRONG_BODY_ATR):
        skip = "failure-not-deep-enough"
    elif body_atr < V4_MIN_BODY_ATR:
        skip = f"weak-reversal-body-{body_atr:.2f}ATR"
    elif not macd_turn:
        skip = "macd-not-turning"
    elif five_n < V4_MIN_5M_CONFIRM:
        skip = f"5m-confirm-{five_n}/3"
    elif vol_ratio > V4_MAX_VOL_RATIO:
        skip = f"volatility-{vol_ratio:.2f}x"
    elif extension_atr > V4_MAX_EXTENSION_ATR:
        skip = f"reversal-already-ran-{extension_atr:.2f}ATR"
    elif score < V4_MIN_TRAP_SCORE:
        skip = f"trap-score-{score:.1f}<{V4_MIN_TRAP_SCORE:.1f}"
    elif not plan or plan["risk_distance"] <= 0:
        skip = "invalid-trap-plan"

    quality = score*10 + min(adx, 35)*0.25 + (6 if reversal_htf else 0) + (4 if volume_above else 0) - max(0, excursion-1.0)*3
    return {
        "symbol": symbol, "side": side, "score": score, "score_max": score_max,
        "threshold": V4_MIN_TRAP_SCORE, "grade": grade, "skip": skip,
        "entry_route": "FAILED-CROSS" if skip is None else "WAIT",
        "price": close, "atr": atr, "adx": adx, "rsi": rsi, "signal_bar_ts": int(cross["bar"]["ts"]),
        "vol_ratio": vol_ratio, "volume_above": volume_above, "vwap": safe_float(a["vwap"]),
        "risk_distance": plan["risk_distance"] if plan else 0.0,
        "stop_origin": plan["origin"] if plan else "N/A", "structure_capped": False,
        "htf_dir": h1["dir"], "htf_state": "ALIGNED" if h1["dir"]==d else "AGAINST" if h1["dir"]==-d else "NEUTRAL",
        "htf_adx": h1["adx"], "h4_dir": h4["dir"],
        "h4_state": "ALIGNED" if h4["dir"]==d else "AGAINST" if h4["dir"]==-d else "NEUTRAL",
        "h4_adx": h4["adx"], "body_atr": body_atr, "slope_atr": 0.0,
        "extension_atr": extension_atr, "cross_age": cross["age"], "failed_dir": failed_dir,
        "excursion_atr": excursion, "five_confirm": five_n, "five_micro": five_micro,
        "five_directional": five_dir, "five_hist": five_hist, "quality": quality,
        "tactics": int(round(score)), "entry_quality": five_n,
        "confirm_components": five_n, "momentum_quality": 2 if lost_fast and macd_turn else 0,
        "pullback_quality": 0, "rel": 0.0, "btc_regime": "V4-FAILED-CROSS",
    }


def sizing_profile(signal):
    elite = safe_float(signal.get("score")) >= V4_ELITE_TRAP_SCORE and int(signal.get("five_confirm", 0)) == 3
    return {
        "name": "V4-ELITE" if elite else "V4-TRAP",
        "risk_pct": V4_ELITE_RISK_PCT if elite else V4_NORMAL_RISK_PCT,
        "target_net_tp": V4_ELITE_TARGET_NET if elite else V4_NORMAL_TARGET_NET,
    }


def sniper_live_levels(signal, live, tick):
    risk = safe_float(signal.get("risk_distance"))
    if live <= 0 or risk <= 0:
        return None
    side = signal["side"]
    d = 1 if side == "long" else -1
    sl = round_trade_price(live - d*risk, tick, side, "sl")
    t1 = round_trade_price(live + d*risk*V4_TP1_R, tick, side, "tp")
    t2 = round_trade_price(live + d*risk*V4_TP2_R, tick, side, "tp")
    t3 = round_trade_price(live + d*risk*V4_TP_FINAL_R, tick, side, "tp")
    if side == "long" and not (sl < live < t1 < t2 < t3): return None
    if side == "short" and not (t3 < t2 < t1 < live < sl): return None
    return sl, t1, t2, t3, abs(live-sl)


def sniper_portfolio_allows(signal, positions):
    side = signal.get("side")
    if side not in ("long", "short"):
        return False, "no-side"
    same = sum(1 for p in positions if position_side(p) == side)
    elite = safe_float(signal.get("score")) >= V4_ELITE_TRAP_SCORE and int(signal.get("five_confirm",0)) == 3
    if same >= V4_MAX_SAME_SIDE and not elite:
        return False, "same-side>=2-needs-elite-trap"
    if same >= 3:
        return False, "max-3-same-direction"
    return True, "OK"


def open_trade(signal):
    global protection_lock
    symbol, side = signal["symbol"], signal["side"]
    account = futures_account(); equity, available = account["total_equity"], account["available"]
    reset_daily_if_needed(equity)
    if equity < MIN_EQUITY_USDT or available <= 0 or (not PAPER_TRADING and protection_lock):
        return False
    tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
    live = safe_float(exchange.fetch_ticker(symbol).get("last"), signal["price"])
    signal_px = safe_float(signal["price"])
    if live <= 0 or signal_px <= 0: return False
    d = 1 if side == "long" else -1
    drift = (live-signal_px)/signal_px
    adverse = d*drift
    if adverse > SNIPER_MAX_FILL_DRIFT:
        tg(f"⏭️ {symbol} {side.upper()} V4 too late\nAdverse drift {adverse*100:.2f}% > {SNIPER_MAX_FILL_DRIFT*100:.2f}%")
        return False
    levels = sniper_live_levels(signal, live, tick)
    if not levels: return False
    sl,t1,t2,t3,risk = levels
    profile = sizing_profile(signal)
    sizing = calculate_contracts(symbol, live, sl, equity, available, profile["risk_pct"], profile["target_net_tp"])
    if not sizing or sizing["margin"] > available*AVAILABLE_MARGIN_BUFFER: return False
    before_ids={position_id(p) for p in get_positions() if position_id(p)}
    oklev, levres = set_leverage(symbol, LEVERAGE)
    if not oklev:
        tg(f"⛔ LEVERAGE FAILED\n{symbol}\n{levres}"); return False
    payload={
        "instId":blofin_inst_id(symbol),"marginMode":MARGIN_MODE,"positionSide":"net",
        "side":"buy" if side=="long" else "sell","orderType":"market","size":str(sizing["contracts"]),
        "tpTriggerPrice":str(t3),"tpOrderPrice":"-1","tpTriggerPriceType":"mark",
        "slTriggerPrice":str(sl),"slOrderPrice":"-1","slTriggerPriceType":"mark",
        "clientOrderId":f"ps40{int(time.time()*1000)%1000000000}",
    }
    if BROKER_ID: payload["brokerId"]=BROKER_ID
    failed_name="BULL CROSS" if signal.get("failed_dir")==1 else "BEAR CROSS"
    msg=(
        f"{'🧪 PAPER' if PAPER_TRADING else '🔄 PRECISION REVERSAL LIVE'} v{SNIPER_VERSION}\n"
        f"{symbol} {side.upper()} · {signal['grade']} · FAILED-CROSS\n"
        f"Original signal FAILED: {failed_name} · {signal.get('cross_age',0)} bar(s) ago\n"
        f"Trap score {signal['score']:.1f}/10 · 5m {signal.get('five_confirm',0)}/3\n"
        f"1H {signal.get('htf_state')} · 4H {signal.get('h4_state')} · ADX {signal['adx']:.1f} · RSI {signal['rsi']:.1f}\n"
        f"Failure body {signal.get('body_atr',0):.2f} ATR · excursion {signal.get('excursion_atr',0):.2f} ATR\n"
        f"Entry ~ {live:.8g}\nSL {sl:.8g}\nTP1 {t1:.8g} → BE\nTP2 {t2:.8g} → lock {V4_LOCK_R:.2f}R\nTP final {t3:.8g} → full exit\n"
        f"Risk {risk/live*100:.2f}% · sizing {profile['name']} {profile['risk_pct']*100:.1f}% equity cap\n"
        f"Expected final TP net ~${sizing['expected_net_tp_usdt']:.2f} · SL+cost ~${sizing['expected_net_sl_usdt']:.2f}\n"
        f"Notional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x\n"
        f"Adverse drift {max(0,adverse)*100:.2f}%/{SNIPER_MAX_FILL_DRIFT*100:.2f}%"
    )
    if PAPER_TRADING:
        tg(msg); return True
    resp=native("private_post_trade_order",payload)
    if not native_ok(resp):
        err=response_error(resp)
        if "152012" in err: protection_lock=True
        tg(f"❌ V4 ORDER FAILED\n{symbol} {side.upper()}\n{err}"); return False
    tg(msg)
    new_pos=find_new_position(symbol,before_ids,timeout=8)
    if new_pos is None:
        tg(f"⚠️ {symbol}: order accepted, position not visible yet."); return True
    pid=position_id(new_pos)
    if pid: sniper_managed_positions.add(pid)
    if has_live_protection(new_pos)[0]:
        tg(f"🛡️ V4 PROTECTION CONFIRMED\n{symbol} {side.upper()}\nTP final + SL visible."); return True
    last=safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side,sl,t3,last):
        ok,res=place_tpsl_for_position(new_pos,sl,t3,client_tag="ps40")
        if ok:
            tg(f"🛡️ V4 FALLBACK PROTECTION CONFIRMED\n{symbol}"); return True
        protection_lock=True; tg(f"🚨 V4 PROTECTION FAILURE\n{symbol}\n{res}\nNEW TRADES PAUSED."); return True
    protection_lock=True; tg(f"🚨 {symbol}: V4 position exists but safe fallback protection failed.\nNEW TRADES PAUSED."); return True


def _v4_managed(p):
    pid=position_id(p)
    if pid in sniper_managed_positions: return True
    try:
        _,probe=has_live_protection(p)
        cid=str((probe or {}).get("clientOrderId","") or "")
        if cid.startswith(("ps40",)):
            if pid: sniper_managed_positions.add(pid)
            return True
        # Restart-safe adoption: V4 positions have final TP at ~2R from entry.
        # Older Precision Sniper versions used 3R, so this avoids taking over them.
        entry = position_entry(p)
        tp = safe_float((probe or {}).get("tpTriggerPrice"))
        sl = safe_float((probe or {}).get("slTriggerPrice"))
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if entry > 0 and risk > 0 and tp > 0 else 0.0
        if 1.80 <= rr <= 2.20:
            if pid: sniper_managed_positions.add(pid)
            return True
    except Exception: pass
    return False


def _v4_close_position(p, reason):
    payload={
        "instId":p["instId"], "marginMode":position_margin_mode(p),
        "positionSide":str(p.get("positionSide","net") or "net"),
        "clientOrderId":f"ps40x{int(time.time()*1000)%1000000000}",
    }
    pid=position_id(p)
    if pid: payload["positionId"]=pid
    if BROKER_ID: payload["brokerId"]=BROKER_ID
    resp=native("private_post_trade_close_position",payload)
    if native_ok(resp):
        tg(f"⏱️ V4 ACTIVE EXIT\n{position_symbol(p)} {position_side(p).upper()}\n{reason}\nClosed at market.")
        return True
    tg(f"⚠️ V4 ACTIVE EXIT FAILED\n{position_symbol(p)}\n{response_error(resp)}")
    return False


def manage_sniper_step_stops():
    """V4: faster mean-reversion management plus a real stale-trade exit."""
    if PAPER_TRADING: return
    now_ms=int(time.time()*1000)
    for p in get_positions():
        if not _v4_managed(p): continue
        symbol,side,entry=position_symbol(p),position_side(p),position_entry(p)
        if entry<=0: continue
        ok,order=has_live_protection(p)
        if not ok or not order: continue
        tp=safe_float(order.get("tpTriggerPrice")); current_sl=safe_float(order.get("slTriggerPrice"))
        if tp<=0 or current_sl<=0: continue
        risk=abs(tp-entry)/V4_TP_FINAL_R
        if risk<=0: continue
        d=1 if side=="long" else -1
        extreme=sniper_recent_extreme(symbol,side)
        current=safe_float(exchange.fetch_ticker(symbol).get("last"),entry)
        current_r=d*(current-entry)/risk
        hit1=extreme>=entry+d*risk*V4_TP1_R if d==1 else extreme<=entry+d*risk*V4_TP1_R
        hit2=extreme>=entry+d*risk*V4_TP2_R if d==1 else extreme<=entry+d*risk*V4_TP2_R
        tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
        desired=None; stage=None
        if hit2:
            desired=round_trade_price(entry+d*risk*V4_LOCK_R,tick,side,"sl"); stage="TP2"
        elif hit1:
            desired=round_trade_price(entry,tick,side,"sl"); stage="TP1"
        if desired is not None:
            improves=desired>current_sl+tick*0.1 if d==1 else desired<current_sl-tick*0.1
            if improves:
                ok2,res=amend_tpsl_stop(p,desired)
                key=f"{position_id(p)}:{stage}:{desired}"
                if ok2 and not sniper_step_notified.get(key):
                    tg(f"🪜 V4 STEP STOP\n{symbol} {side.upper()}\n{stage} → SL {desired:.8g}\nFinal TP remains active.")
                    sniper_step_notified[key]=True
        created=safe_float(p.get("createTime",p.get("cTime",0)))
        if created>0:
            age_min=(now_ms-created)/60000.0
            if age_min>=V4_HARD_TIME_EXIT_MIN:
                _v4_close_position(p,f"Hard time exit {age_min:.0f}m — V4 does not hold dead reversals.")
                continue
            if age_min>=V4_SOFT_TIME_EXIT_MIN and current_r<V4_SOFT_EXIT_MAX_R:
                _v4_close_position(p,f"Soft time exit {age_min:.0f}m · only {current_r:+.2f}R progress.")
                continue


def maybe_score_report(rows):
    global sniper_last_report
    now=time.time(); active=[r for r in rows if r.get("side")]
    if not active or now-sniper_last_report<SNIPER_REPORT_MIN*60: return
    sniper_last_report=now
    ranked=sorted(active,key=lambda r:safe_float(r.get("quality")),reverse=True)[:10]
    lines=[f"🔄 PRECISION SNIPER v{SNIPER_VERSION} · FAILED-CROSS REVERSAL"]
    for r in ranked:
        state="🟢 REVERSE" if not r.get("skip") else f"⏭ {r.get('skip')}"
        orig="BULL" if r.get("failed_dir")==1 else "BEAR"
        lines.append(f"{r['symbol'].split('/')[0]} {r['side'].upper()} {r['grade']} trap {r['score']:.1f}/10 5m{r.get('five_confirm',0)}/3 orig:{orig} age{r.get('cross_age',0)} 1H:{r.get('htf_state','-')} 4H:{r.get('h4_state','-')} {state}")
    tg("\n".join(lines))


def startup_diagnostics():
    acc=futures_account(); reset_daily_if_needed(acc["total_equity"])
    tg(
        f"🔄 PRECISION SNIPER BOT STARTED v{SNIPER_VERSION}\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"ENGINE FLIPPED: crossover = trap candidate, NEVER direct entry\n"
        f"Entry: opposite side only after 15m cross failure + >= {V4_MIN_5M_CONFIRM}/3 closed-5m reversal confirmation\n"
        f"Trap score >= {V4_MIN_TRAP_SCORE:.1f}/10 · elite >= {V4_ELITE_TRAP_SCORE:.1f}/10\n"
        f"1H+4H both still support original cross = BLOCK\n"
        f"Anti-chase reversal extension <= {V4_MAX_EXTENSION_ATR:.2f} ATR\n"
        f"Exit: {V4_TP1_R:.2f}R→BE · {V4_TP2_R:.2f}R→lock {V4_LOCK_R:.2f}R · {V4_TP_FINAL_R:.2f}R full TP\n"
        f"Time exit: {V4_SOFT_TIME_EXIT_MIN}m if <{V4_SOFT_EXIT_MAX_R:.2f}R progress · hard {V4_HARD_TIME_EXIT_MIN}m\n"
        f"Risk: normal {V4_NORMAL_RISK_PCT*100:.1f}% · elite {V4_ELITE_RISK_PCT*100:.1f}% · max positions {MAX_ACTIVE_POSITIONS}\n"
        f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}"
    )


# ============================================================
# PRECISION SNIPER V5.0 — ADAPTIVE REGIME ROUTER
# ============================================================
# V5 stops predicting from EMA crosses altogether.
# It routes each market into one of two playbooks:
#   TREND  -> 4H/1H aligned trend, 15m pullback, 5m continuation trigger.
#   RANGE  -> low/mixed HTF trend, 15m liquidity sweep, 5m rejection trigger.
# Mixed/noisy regimes are skipped. Crossovers are informational only.

SNIPER_VERSION = "5.0.0"
V5_TREND_MIN_ADX = float(os.getenv("V5_TREND_MIN_ADX", "18"))
V5_RANGE_MAX_ADX = float(os.getenv("V5_RANGE_MAX_ADX", "22"))
V5_MIN_5M_CONFIRM = int(os.getenv("V5_MIN_5M_CONFIRM", "2"))
V5_TREND_MIN_QUALITY = float(os.getenv("V5_TREND_MIN_QUALITY", "74"))
V5_SWEEP_MIN_QUALITY = float(os.getenv("V5_SWEEP_MIN_QUALITY", "76"))
V5_MAX_VOL_RATIO = float(os.getenv("V5_MAX_VOL_RATIO", "1.55"))
V5_TREND_MAX_EXT_ATR = float(os.getenv("V5_TREND_MAX_EXT_ATR", "0.60"))
V5_PULLBACK_LOOKBACK = int(os.getenv("V5_PULLBACK_LOOKBACK", "4"))
V5_SWEEP_LOOKBACK = int(os.getenv("V5_SWEEP_LOOKBACK", "12"))
V5_TREND_STOP_ATR = float(os.getenv("V5_TREND_STOP_ATR", "1.10"))
V5_SWEEP_STOP_ATR = float(os.getenv("V5_SWEEP_STOP_ATR", "0.95"))
V5_TREND_FINAL_R = float(os.getenv("V5_TREND_FINAL_R", "2.50"))
V5_SWEEP_FINAL_R = float(os.getenv("V5_SWEEP_FINAL_R", "1.60"))
V5_TREND_TP1_R = float(os.getenv("V5_TREND_TP1_R", "0.85"))
V5_TREND_TP2_R = float(os.getenv("V5_TREND_TP2_R", "1.50"))
V5_SWEEP_TP1_R = float(os.getenv("V5_SWEEP_TP1_R", "0.65"))
V5_SWEEP_TP2_R = float(os.getenv("V5_SWEEP_TP2_R", "1.05"))
V5_TREND_LOCK_R = float(os.getenv("V5_TREND_LOCK_R", "0.55"))
V5_SWEEP_LOCK_R = float(os.getenv("V5_SWEEP_LOCK_R", "0.30"))
V5_TREND_RISK_PCT = float(os.getenv("V5_TREND_RISK_PCT", "0.014"))
V5_SWEEP_RISK_PCT = float(os.getenv("V5_SWEEP_RISK_PCT", "0.012"))
V5_ELITE_RISK_PCT = float(os.getenv("V5_ELITE_RISK_PCT", "0.016"))
V5_TREND_TARGET_NET = float(os.getenv("V5_TREND_TARGET_NET", "1.50"))
V5_SWEEP_TARGET_NET = float(os.getenv("V5_SWEEP_TARGET_NET", "1.10"))
V5_TREND_SOFT_EXIT_MIN = int(os.getenv("V5_TREND_SOFT_EXIT_MIN", "240"))
V5_TREND_HARD_EXIT_MIN = int(os.getenv("V5_TREND_HARD_EXIT_MIN", "480"))
V5_SWEEP_SOFT_EXIT_MIN = int(os.getenv("V5_SWEEP_SOFT_EXIT_MIN", "90"))
V5_SWEEP_HARD_EXIT_MIN = int(os.getenv("V5_SWEEP_HARD_EXIT_MIN", "210"))
V5_MAX_SAME_SIDE = int(os.getenv("V5_MAX_SAME_SIDE", "2"))
V5_BTC_GATE = os.getenv("V5_BTC_GATE", "true").lower() == "true"

_v5_position_meta = {}
_v5_last_report = 0.0


def _v5_tf_detail(symbol, tf):
    try:
        df = sniper_indicators(ohlcv_df(symbol, tf))
        if len(df) < 80:
            return {"dir":0,"adx":0.0,"rsi":50.0,"close":0.0,"atr":0.0,"ef":0.0,"es":0.0,"et":0.0,"hist":0.0}
        a, p = df.iloc[-2], df.iloc[-3]
        c = safe_float(a["close"]); ef=safe_float(a["ema_fast"]); es=safe_float(a["ema_slow"]); et=safe_float(a["ema_trend"])
        d = 1 if ef > es > et and c > es else -1 if ef < es < et and c < es else 0
        return {"dir":d,"adx":safe_float(a["sniper_adx"]),"rsi":safe_float(a["sniper_rsi"],50),
                "close":c,"atr":safe_float(a["atr"]),"ef":ef,"es":es,"et":et,
                "hist":safe_float(a["macd_hist"]),"hist_prev":safe_float(p["macd_hist"])}
    except Exception:
        return {"dir":0,"adx":0.0,"rsi":50.0,"close":0.0,"atr":0.0,"ef":0.0,"es":0.0,"et":0.0,"hist":0.0,"hist_prev":0.0}


def _v5_regime(symbol):
    h1 = _v5_tf_detail(symbol, "1h")
    h4 = _v5_tf_detail(symbol, "4h")
    aligned = h1["dir"] != 0 and h1["dir"] == h4["dir"]
    strong = aligned and (h1["adx"] >= V5_TREND_MIN_ADX or h4["adx"] >= V5_TREND_MIN_ADX)
    if strong:
        return "TREND", h1["dir"], h1, h4
    # Range is deliberately conservative: no clean aligned HTF stack and low-ish 1H ADX.
    if not aligned and h1["adx"] <= V5_RANGE_MAX_ADX:
        return "RANGE", 0, h1, h4
    return "MIXED", 0, h1, h4


def _v5_btc_gate(symbol, d):
    if not V5_BTC_GATE or symbol == SNIPER_V3_BTC_SYMBOL:
        return True, "BTC-self/off"
    btc = sniper_v3_btc_context()
    b1, b4 = btc["h1"]["dir"], btc["h4"]["dir"]
    # Do not take an alt directly against a fully aligned BTC macro impulse.
    if b1 == -d and b4 == -d:
        return False, f"BTC1H{b1:+d}/4H{b4:+d}-against"
    return True, f"BTC1H{b1:+d}/4H{b4:+d}"


def _v5_5m_confirm(symbol, d):
    try:
        f = sniper_indicators(ohlcv_df(symbol, "5m"))
        if len(f) < 40: return 0, {}
        a,p = f.iloc[-2],f.iloc[-3]
        c,o = safe_float(a["close"]),safe_float(a["open"])
        ef,es = safe_float(a["ema_fast"]),safe_float(a["ema_slow"])
        directional = (c>o and c>ef) if d==1 else (c<o and c<ef)
        stack = (ef>es) if d==1 else (ef<es)
        micro = c>safe_float(p["high"]) if d==1 else c<safe_float(p["low"])
        hist = safe_float(a["macd_hist"])>safe_float(p["macd_hist"]) if d==1 else safe_float(a["macd_hist"])<safe_float(p["macd_hist"])
        # Four components; 2 is acceptable, 3+ is preferred.
        n=int(directional)+int(stack)+int(micro)+int(hist)
        return n,{"directional":directional,"stack":stack,"micro":micro,"hist":hist}
    except Exception:
        return 0,{}


def _v5_plan(df, side, mode):
    a=df.iloc[-2]; d=1 if side=="long" else -1
    entry=safe_float(a["close"]); atr=safe_float(a["atr"])
    if entry<=0 or atr<=0: return None
    if mode=="TREND":
        recent=df.iloc[-9:-1]
        structure=safe_float(recent["low"].min())-0.10*atr if d==1 else safe_float(recent["high"].max())+0.10*atr
        atr_stop=entry-d*V5_TREND_STOP_ATR*atr
        raw=min(atr_stop,structure) if d==1 else max(atr_stop,structure)
        risk=min(abs(entry-raw),1.55*atr); risk=max(risk,0.78*atr)
        final_r=V5_TREND_FINAL_R
    else:
        # Sweep stop belongs just beyond the rejection extreme, but never absurdly tight/wide.
        extreme=safe_float(a["low"])-0.10*atr if d==1 else safe_float(a["high"])+0.10*atr
        atr_stop=entry-d*V5_SWEEP_STOP_ATR*atr
        raw=min(atr_stop,extreme) if d==1 else max(atr_stop,extreme)
        risk=min(abs(entry-raw),1.25*atr); risk=max(risk,0.60*atr)
        final_r=V5_SWEEP_FINAL_R
    return {"risk_distance":risk,"atr":atr,"origin":f"V5 {mode.lower()} structure","final_r":final_r}


def _v5_trend_candidate(symbol, df, h1, h4, d):
    a,p=df.iloc[-2],df.iloc[-3]
    side="long" if d==1 else "short"
    atr=safe_float(a["atr"]); atr_mean=safe_float(a["atr_mean"])
    if atr<=0: return None
    c,o,h,l=safe_float(a["close"]),safe_float(a["open"]),safe_float(a["high"]),safe_float(a["low"])
    ef,es,et=safe_float(a["ema_fast"]),safe_float(a["ema_slow"]),safe_float(a["ema_trend"])
    rsi=safe_float(a["sniper_rsi"],50); adx=safe_float(a["sniper_adx"])
    hist=safe_float(a["macd_hist"]); histp=safe_float(p["macd_hist"])
    volm=safe_float(a["vol_mean"]); vol=safe_float(a["volume"]); vol_spike=volm>0 and vol>=volm*1.10
    vol_ratio=atr/atr_mean if atr_mean>0 else 99
    recent=df.iloc[-(V5_PULLBACK_LOOKBACK+2):-1]
    # Pullback must have interacted with EMA21/EMA9 zone recently, then current closed candle reclaims direction.
    if d==1:
        touched=(recent["low"] <= recent["ema_slow"] + 0.12*recent["atr"]).any()
        reclaim=c>ef and c>es and c>o
        trend15=ef>es>et and c>et
        rsi_ok=50<=rsi<=69
        macd_turn=hist>histp
    else:
        touched=(recent["high"] >= recent["ema_slow"] - 0.12*recent["atr"]).any()
        reclaim=c<ef and c<es and c<o
        trend15=ef<es<et and c<et
        rsi_ok=31<=rsi<=50
        macd_turn=hist<histp
    body=d*(c-o)/atr
    extension=d*(c-es)/atr
    five_n,_=_v5_5m_confirm(symbol,d)
    btc_ok,btc_note=_v5_btc_gate(symbol,d)
    q=0.0
    q+=22 if touched else 0
    q+=18 if reclaim else 0
    q+=14 if trend15 else 0
    q+=12 if macd_turn else 0
    q+=10 if rsi_ok else 0
    q+=10 if five_n>=V5_MIN_5M_CONFIRM else 0
    q+=6 if five_n>=3 else 0
    q+=4 if vol_spike else 0
    q+=min(4,max(0,(h1["adx"]-V5_TREND_MIN_ADX)*0.4))
    skip=None
    if not touched: skip="no-15m-pullback"
    elif not reclaim: skip="pullback-not-reclaimed"
    elif not trend15: skip="15m-stack-not-restored"
    elif not macd_turn: skip="15m-macd-not-turning"
    elif not rsi_ok: skip=f"rsi-{rsi:.0f}-not-reset"
    elif five_n<V5_MIN_5M_CONFIRM: skip=f"5m-confirm-{five_n}/4"
    elif vol_ratio>V5_MAX_VOL_RATIO: skip=f"volatility-{vol_ratio:.2f}x"
    elif extension>V5_TREND_MAX_EXT_ATR: skip=f"trend-entry-chased-{extension:.2f}ATR"
    elif body<0.10: skip=f"weak-reclaim-body-{body:.2f}ATR"
    elif not btc_ok: skip=btc_note
    elif q<V5_TREND_MIN_QUALITY: skip=f"quality-{q:.0f}<{V5_TREND_MIN_QUALITY:.0f}"
    plan=_v5_plan(df,side,"TREND")
    if not plan: skip="invalid-plan"
    return {"symbol":symbol,"side":side,"score":q,"score_max":100.0,"threshold":V5_TREND_MIN_QUALITY,
            "grade":"A+" if q>=88 else "A" if q>=V5_TREND_MIN_QUALITY else "C","skip":skip,
            "entry_route":"TREND-PULLBACK" if skip is None else "WAIT","strategy":"TREND",
            "price":c,"atr":atr,"adx":adx,"rsi":rsi,"signal_bar_ts":int(a["ts"]),
            "vol_ratio":vol_ratio,"volume_above":vol_spike,"risk_distance":plan["risk_distance"] if plan else 0,
            "stop_origin":plan["origin"] if plan else "N/A","final_r":V5_TREND_FINAL_R,
            "htf_state":"ALIGNED","h4_state":"ALIGNED","htf_adx":h1["adx"],"h4_adx":h4["adx"],
            "five_confirm":five_n,"extension_atr":extension,"body_atr":body,"btc_note":btc_note,
            "quality":q + min(h1["adx"],35)*0.25 + min(h4["adx"],35)*0.20}


def _v5_sweep_candidate(symbol, df, h1, h4):
    a,p=df.iloc[-2],df.iloc[-3]
    atr=safe_float(a["atr"]); atr_mean=safe_float(a["atr_mean"])
    if atr<=0:return None
    c,o,h,l=safe_float(a["close"]),safe_float(a["open"]),safe_float(a["high"]),safe_float(a["low"])
    prior=df.iloc[-(V5_SWEEP_LOOKBACK+2):-2]
    prior_hi=safe_float(prior["high"].max()); prior_lo=safe_float(prior["low"].min())
    bull_sweep=l<prior_lo-0.03*atr and c>prior_lo
    bear_sweep=h>prior_hi+0.03*atr and c<prior_hi
    if not bull_sweep and not bear_sweep:
        return {"symbol":symbol,"side":None,"score":0,"score_max":100,"grade":"-","skip":"range-no-liquidity-sweep",
                "entry_route":"WAIT","strategy":"RANGE","price":c,"atr":atr,"adx":safe_float(a["sniper_adx"]),"signal_bar_ts":int(a["ts"])}
    d=1 if bull_sweep else -1; side="long" if d==1 else "short"
    rng=max(h-l,1e-12); lower=min(o,c)-l; upper=h-max(o,c)
    wick=(lower/rng) if d==1 else (upper/rng)
    body=d*(c-o)/atr
    rsi=safe_float(a["sniper_rsi"],50); adx=safe_float(a["sniper_adx"])
    hist=safe_float(a["macd_hist"]); histp=safe_float(p["macd_hist"])
    macd_turn=hist>histp if d==1 else hist<histp
    rsi_ok=rsi>=38 if d==1 else rsi<=62
    volm=safe_float(a["vol_mean"]); vol=safe_float(a["volume"]); vol_spike=volm>0 and vol>=volm*1.05
    vol_ratio=atr/atr_mean if atr_mean>0 else 99
    five_n,_=_v5_5m_confirm(symbol,d)
    btc_ok,btc_note=_v5_btc_gate(symbol,d)
    reclaim_depth=(c-prior_lo)/atr if d==1 else (prior_hi-c)/atr
    q=38.0
    q+=16 if wick>=0.32 else 8 if wick>=0.22 else 0
    q+=12 if body>=0.12 else 5 if body>0 else 0
    q+=10 if macd_turn else 0
    q+=8 if rsi_ok else 0
    q+=10 if five_n>=V5_MIN_5M_CONFIRM else 0
    q+=4 if five_n>=3 else 0
    q+=4 if vol_spike else 0
    skip=None
    if wick<0.22: skip=f"sweep-wick-{wick:.2f}"
    elif body<=0: skip="sweep-no-rejection-body"
    elif not macd_turn: skip="sweep-macd-not-turning"
    elif five_n<V5_MIN_5M_CONFIRM: skip=f"5m-confirm-{five_n}/4"
    elif vol_ratio>V5_MAX_VOL_RATIO: skip=f"volatility-{vol_ratio:.2f}x"
    elif not btc_ok: skip=btc_note
    elif q<V5_SWEEP_MIN_QUALITY: skip=f"quality-{q:.0f}<{V5_SWEEP_MIN_QUALITY:.0f}"
    plan=_v5_plan(df,side,"SWEEP")
    if not plan: skip="invalid-plan"
    return {"symbol":symbol,"side":side,"score":q,"score_max":100.0,"threshold":V5_SWEEP_MIN_QUALITY,
            "grade":"A+" if q>=90 else "A" if q>=V5_SWEEP_MIN_QUALITY else "C","skip":skip,
            "entry_route":"RANGE-SWEEP" if skip is None else "WAIT","strategy":"SWEEP",
            "price":c,"atr":atr,"adx":adx,"rsi":rsi,"signal_bar_ts":int(a["ts"]),
            "vol_ratio":vol_ratio,"volume_above":vol_spike,"risk_distance":plan["risk_distance"] if plan else 0,
            "stop_origin":plan["origin"] if plan else "N/A","final_r":V5_SWEEP_FINAL_R,
            "htf_state":"RANGE","h4_state":"RANGE","htf_adx":h1["adx"],"h4_adx":h4["adx"],
            "five_confirm":five_n,"wick_ratio":wick,"reclaim_depth":reclaim_depth,"body_atr":body,
            "btc_note":btc_note,"quality":q}


def evaluate_symbol(symbol, btc=None):
    df=sniper_indicators(ohlcv_df(symbol,SNIPER_TF))
    if len(df)<max(SNIPER_TREND*3,190): return None
    regime,d,h1,h4=_v5_regime(symbol)
    a=df.iloc[-2]
    if regime=="TREND": return _v5_trend_candidate(symbol,df,h1,h4,d)
    if regime=="RANGE": return _v5_sweep_candidate(symbol,df,h1,h4)
    return {"symbol":symbol,"side":None,"score":0,"score_max":100,"grade":"-","skip":"mixed-regime-no-trade",
            "entry_route":"WAIT","strategy":"MIXED","price":safe_float(a["close"]),"atr":safe_float(a["atr"]),
            "adx":safe_float(a["sniper_adx"]),"signal_bar_ts":int(a["ts"]),"htf_adx":h1["adx"],"h4_adx":h4["adx"]}


def sizing_profile(signal):
    mode=signal.get("strategy","TREND")
    elite=safe_float(signal.get("score"))>=90 and int(signal.get("five_confirm",0))>=3
    if elite: return {"name":"V5-ELITE","risk_pct":V5_ELITE_RISK_PCT,"target_net_tp":V5_TREND_TARGET_NET if mode=="TREND" else V5_SWEEP_TARGET_NET}
    if mode=="SWEEP": return {"name":"V5-SWEEP","risk_pct":V5_SWEEP_RISK_PCT,"target_net_tp":V5_SWEEP_TARGET_NET}
    return {"name":"V5-TREND","risk_pct":V5_TREND_RISK_PCT,"target_net_tp":V5_TREND_TARGET_NET}


def calculate_contracts_v5(symbol,entry,sl,equity,available,risk_pct,target_net_tp,tp_r):
    meta=get_instrument(symbol); cv=safe_float(meta.get("contractValue")); min_size=safe_float(meta.get("minSize")); lot=safe_float(meta.get("lotSize")); max_market=safe_float(meta.get("maxMarketSize",0))
    if cv<=0 or min_size<=0 or lot<=0:return None
    stop_pct=abs(entry-sl)/entry
    if stop_pct<=0:return None
    risk_budget=equity*risk_pct; risk_notional_cap=risk_budget/stop_pct
    margin_cap=min(BASE_MARGIN_USDT,MAX_MARGIN_USDT,equity*MAX_MARGIN_EQUITY_PCT,max(0.0,available*AVAILABLE_MARGIN_BUFFER))
    margin_notional_cap=margin_cap*LEVERAGE
    cost_rate=TAKER_FEE_RATE*2+SLIPPAGE_RATE; net_tp_rate=tp_r*stop_pct-cost_rate
    if net_tp_rate<=0:return None
    desired_notional=target_net_tp/net_tp_rate
    target_notional=min(desired_notional,risk_notional_cap,margin_notional_cap)
    contracts=floor_step(target_notional/(cv*entry),lot)
    if contracts<min_size:contracts=min_size
    if max_market>0:contracts=min(contracts,floor_step(max_market,lot))
    if contracts<min_size:return None
    notional=contracts*cv*entry; margin=notional/LEVERAGE
    if notional*stop_pct>risk_budget+1e-9 or margin>margin_cap+1e-9:return None
    cost=roundtrip_cost(notional); expected_tp=notional*tp_r*stop_pct
    if expected_tp-cost<MIN_NET_TP_USDT:return None
    return {"contracts":contracts,"notional":notional,"margin":margin,"expected_net_tp_usdt":expected_tp-cost,
            "expected_net_sl_usdt":notional*stop_pct+cost,"risk_budget":risk_budget}


def _v5_levels(signal,live,tick):
    side=signal["side"]; d=1 if side=="long" else -1; risk=safe_float(signal.get("risk_distance")); mode=signal.get("strategy")
    if live<=0 or risk<=0:return None
    if mode=="SWEEP": tp1r,tp2r,finalr=V5_SWEEP_TP1_R,V5_SWEEP_TP2_R,V5_SWEEP_FINAL_R
    else: tp1r,tp2r,finalr=V5_TREND_TP1_R,V5_TREND_TP2_R,V5_TREND_FINAL_R
    sl=round_trade_price(live-d*risk,tick,side,"sl"); t1=round_trade_price(live+d*risk*tp1r,tick,side,"tp"); t2=round_trade_price(live+d*risk*tp2r,tick,side,"tp"); t3=round_trade_price(live+d*risk*finalr,tick,side,"tp")
    return sl,t1,t2,t3,risk,finalr


def sniper_portfolio_allows(signal,positions):
    side=signal.get("side"); same=sum(1 for p in positions if position_side(p)==side)
    if same>=V5_MAX_SAME_SIDE:return False,f"max-{V5_MAX_SAME_SIDE}-same-side"
    # After two losses on one side, require an elite setup rather than banning the direction.
    if sniper_side_loss_streak.get(side,0)>=2 and safe_float(signal.get("score"))<90:
        return False,f"{side}-loss-streak-needs-elite"
    return True,"OK"


def open_trade(signal):
    global protection_lock
    symbol,side=signal["symbol"],signal["side"]; mode=signal.get("strategy","TREND")
    acc=futures_account(); equity,available=acc["total_equity"],acc["available"]; reset_daily_if_needed(equity)
    if equity<MIN_EQUITY_USDT or available<=0 or (not PAPER_TRADING and protection_lock):return False
    tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001); live=safe_float(exchange.fetch_ticker(symbol).get("last"),signal["price"]); sig=safe_float(signal["price"])
    if live<=0 or sig<=0:return False
    d=1 if side=="long" else -1; adverse=d*((live-sig)/sig)
    if adverse>SNIPER_MAX_FILL_DRIFT:
        tg(f"⏭️ {symbol} {side.upper()} V5 too late\nAdverse drift {adverse*100:.2f}% > {SNIPER_MAX_FILL_DRIFT*100:.2f}%");return False
    levels=_v5_levels(signal,live,tick)
    if not levels:return False
    sl,t1,t2,t3,risk,finalr=levels; profile=sizing_profile(signal)
    sizing=calculate_contracts_v5(symbol,live,sl,equity,available,profile["risk_pct"],profile["target_net_tp"],finalr)
    if not sizing or sizing["margin"]>available*AVAILABLE_MARGIN_BUFFER:return False
    before_ids={position_id(p) for p in get_positions() if position_id(p)}
    oklev,levres=set_leverage(symbol,LEVERAGE)
    if not oklev:tg(f"⛔ LEVERAGE FAILED\n{symbol}\n{levres}");return False
    tag="ps5t" if mode=="TREND" else "ps5s"
    payload={"instId":blofin_inst_id(symbol),"marginMode":MARGIN_MODE,"positionSide":"net","side":"buy" if side=="long" else "sell","orderType":"market","size":str(sizing["contracts"]),"tpTriggerPrice":str(t3),"tpOrderPrice":"-1","tpTriggerPriceType":"mark","slTriggerPrice":str(sl),"slOrderPrice":"-1","slTriggerPriceType":"mark","clientOrderId":f"{tag}{int(time.time()*1000)%1000000000}"}
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    msg=(f"{'🧪 PAPER' if PAPER_TRADING else '🧠 V5 ADAPTIVE ENTRY'} v{SNIPER_VERSION}\n{symbol} {side.upper()} · {mode} · {signal['grade']} {signal['score']:.0f}/100\n"
         f"5m confirm {signal.get('five_confirm',0)}/4 · 1H ADX {signal.get('htf_adx',0):.1f} · 4H ADX {signal.get('h4_adx',0):.1f}\nBTC {signal.get('btc_note','-')}\n"
         f"Entry ~ {live:.8g}\nSL {sl:.8g}\nTP1 {t1:.8g}\nTP2 {t2:.8g}\nTP final {t3:.8g} ({finalr:.2f}R)\n"
         f"Risk cap {profile['risk_pct']*100:.1f}% · expected TP net ~${sizing['expected_net_tp_usdt']:.2f} · SL+cost ~${sizing['expected_net_sl_usdt']:.2f}\n"
         f"Notional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x\nAdverse drift {max(0,adverse)*100:.2f}%/{SNIPER_MAX_FILL_DRIFT*100:.2f}%")
    if PAPER_TRADING:
        tg(msg);_v62_last_entry_time=time.time();return True
    resp=native("private_post_trade_order",payload)
    if not native_ok(resp):
        err=response_error(resp)
        if "152012" in err:protection_lock=True
        tg(f"❌ V5 ORDER FAILED\n{symbol} {side.upper()}\n{err}");return False
    tg(msg); new_pos=find_new_position(symbol,before_ids,timeout=8)
    if new_pos is None:tg(f"⚠️ {symbol}: V5 order accepted, position not visible yet.");return True
    pid=position_id(new_pos)
    if pid:
        sniper_managed_positions.add(pid); _v5_position_meta[pid]={"mode":mode,"risk":risk,"final_r":finalr,"tp1":t1,"tp2":t2,"opened":time.time()}
    if has_live_protection(new_pos)[0]:tg(f"🛡️ V5 PROTECTION CONFIRMED\n{symbol} {side.upper()} · {mode}\nTP final + SL visible.");return True
    last=safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side,sl,t3,last):
        ok,res=place_tpsl_for_position(new_pos,sl,t3,client_tag=tag)
        if ok:tg(f"🛡️ V5 FALLBACK PROTECTION CONFIRMED\n{symbol}");return True
        protection_lock=True;tg(f"🚨 V5 PROTECTION FAILURE\n{symbol}\n{res}\nNEW TRADES PAUSED.");return True
    protection_lock=True;tg(f"🚨 {symbol}: V5 position exists but safe fallback protection failed.\nNEW TRADES PAUSED.");return True


def _v5_managed(p):
    pid=position_id(p)
    if pid in sniper_managed_positions or pid in _v5_position_meta:return True
    try:
        ok,probe=has_live_protection(p); cid=str((probe or {}).get("clientOrderId","") or "")
        if cid.startswith(("ps5t","ps5s")):
            if pid:sniper_managed_positions.add(pid)
            return True
    except Exception:pass
    return False


def _v5_close_position(p,reason):
    payload={"instId":p["instId"],"marginMode":position_margin_mode(p),"positionSide":str(p.get("positionSide","net") or "net"),"clientOrderId":f"ps5x{int(time.time()*1000)%1000000000}"}
    pid=position_id(p)
    if pid:payload["positionId"]=pid
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    resp=native("private_post_trade_close_position",payload)
    if native_ok(resp):tg(f"⚡ V5 ACTIVE EXIT\n{position_symbol(p)} {position_side(p).upper()}\n{reason}\nClosed at market.");return True
    tg(f"⚠️ V5 ACTIVE EXIT FAILED\n{position_symbol(p)}\n{response_error(resp)}");return False


def manage_sniper_step_stops():
    if PAPER_TRADING:return
    now=time.time()
    for p in get_positions():
        if not _v5_managed(p):continue
        symbol,side,entry=position_symbol(p),position_side(p),position_entry(p); pid=position_id(p)
        if entry<=0:continue
        ok,order=has_live_protection(p)
        if not ok or not order:continue
        tp=safe_float(order.get("tpTriggerPrice")); current_sl=safe_float(order.get("slTriggerPrice"))
        if tp<=0 or current_sl<=0:continue
        meta=_v5_position_meta.get(pid,{})
        mode=meta.get("mode")
        if not mode:
            # Best-effort restart adoption only while the original stop is still intact.
            base_risk=abs(entry-current_sl); rr=abs(tp-entry)/base_risk if base_risk>0 else 0
            mode="SWEEP" if 1.35<=rr<=1.85 else "TREND" if 2.15<=rr<=2.85 else None
            if not mode:continue
            meta={"mode":mode,"risk":base_risk,"final_r":V5_SWEEP_FINAL_R if mode=="SWEEP" else V5_TREND_FINAL_R,"opened":safe_float(p.get("createTime"))/1000.0}; _v5_position_meta[pid]=meta
        risk=safe_float(meta.get("risk")); d=1 if side=="long" else -1
        if risk<=0:continue
        current=safe_float(exchange.fetch_ticker(symbol).get("last"),entry); current_r=d*(current-entry)/risk
        extreme=sniper_recent_extreme(symbol,side); tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
        if mode=="SWEEP":tp1r,tp2r,lockr=V5_SWEEP_TP1_R,V5_SWEEP_TP2_R,V5_SWEEP_LOCK_R; soft,hard=V5_SWEEP_SOFT_EXIT_MIN,V5_SWEEP_HARD_EXIT_MIN
        else:tp1r,tp2r,lockr=V5_TREND_TP1_R,V5_TREND_TP2_R,V5_TREND_LOCK_R; soft,hard=V5_TREND_SOFT_EXIT_MIN,V5_TREND_HARD_EXIT_MIN
        hit1=extreme>=entry+d*risk*tp1r if d==1 else extreme<=entry+d*risk*tp1r
        hit2=extreme>=entry+d*risk*tp2r if d==1 else extreme<=entry+d*risk*tp2r
        desired=None;stage=None
        if hit2:desired=round_trade_price(entry+d*risk*lockr,tick,side,"sl");stage="TP2"
        elif hit1:desired=round_trade_price(entry,tick,side,"sl");stage="TP1"
        # Trend positions get an EMA21/ATR ratchet after TP2; this lets winners run without giving the trend back.
        if mode=="TREND" and hit2:
            try:
                df=sniper_indicators(ohlcv_df(symbol,"15m")); a=df.iloc[-2]; atr=safe_float(a["atr"]); es=safe_float(a["ema_slow"])
                trail=es-d*0.25*atr
                trail=round_trade_price(trail,tick,side,"sl")
                if d==1:desired=max(desired,trail)
                else:desired=min(desired,trail)
                stage="TP2+EMA21"
            except Exception:pass
        if desired is not None:
            improves=desired>current_sl+tick*0.1 if d==1 else desired<current_sl-tick*0.1
            if improves:
                ok2,res=amend_tpsl_stop(p,desired); key=f"{pid}:{stage}:{desired}"
                if ok2 and not sniper_step_notified.get(key):tg(f"🪜 V5 STEP STOP\n{symbol} {side.upper()} · {mode}\n{stage} → SL {desired:.8g}");sniper_step_notified[key]=True
        opened=safe_float(meta.get("opened")) or safe_float(p.get("createTime"))/1000.0
        age=(now-opened)/60 if opened>0 else 0
        # Thesis invalidation: trend setup loses 15m EMA55 with MACD turning against it.
        if mode=="TREND" and age>=30:
            try:
                df=sniper_indicators(ohlcv_df(symbol,"15m")); a,pr=df.iloc[-2],df.iloc[-3]; c=safe_float(a["close"]); et=safe_float(a["ema_trend"]); hist=safe_float(a["macd_hist"]); hp=safe_float(pr["macd_hist"])
                invalid=(c<et and hist<hp) if d==1 else (c>et and hist>hp)
                if invalid and current_r<0.35:_v5_close_position(p,f"TREND thesis invalidated after {age:.0f}m ({current_r:+.2f}R).");continue
            except Exception:pass
        if age>=hard:_v5_close_position(p,f"{mode} hard time exit {age:.0f}m ({current_r:+.2f}R).");continue
        soft_floor=0.20 if mode=="SWEEP" else 0.25
        if age>=soft and current_r<soft_floor:_v5_close_position(p,f"{mode} no follow-through after {age:.0f}m ({current_r:+.2f}R).");continue



def repair_one_position(p):
    """V5 emergency protection repair. Preserve the V5 strategy's own final-R profile."""
    symbol,side,entry=position_symbol(p),position_side(p),position_entry(p)
    if entry<=0:return False
    if has_live_protection(p)[0]:return True
    pid=position_id(p); meta=_v5_position_meta.get(pid,{})
    mode=meta.get("mode","TREND"); risk=safe_float(meta.get("risk"))
    if risk<=0:
        try:
            df=sniper_indicators(ohlcv_df(symbol,SNIPER_TF)); atr=safe_float(df.iloc[-2]["atr"]); risk=(V5_SWEEP_STOP_ATR if mode=="SWEEP" else V5_TREND_STOP_ATR)*atr
        except Exception:return False
    if risk<=0:return False
    finalr=V5_SWEEP_FINAL_R if mode=="SWEEP" else V5_TREND_FINAL_R; d=1 if side=="long" else -1
    tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
    sl=round_trade_price(entry-d*risk,tick,side,"sl"); tp=round_trade_price(entry+d*risk*finalr,tick,side,"tp")
    last=safe_float(exchange.fetch_ticker(symbol).get("last"))
    if not tpsl_prices_valid(side,sl,tp,last):return False
    tag="ps5s" if mode=="SWEEP" else "ps5t"
    return place_tpsl_for_position(p,sl,tp,client_tag=tag)[0]

def seed_closed_history_v5():
    """V6.2: rebuild risk/PnL only from the intentional V6 risk epoch onward.

    Old V2-V5 closes earlier today remain known (so they are not re-notified), but they
    no longer count against the fresh Structure Fusion daily risk budget. The epoch is
    a fixed timestamp, so ordinary process restarts cannot repeatedly erase losses.
    On later Amsterdam dates, the normal start-of-day boundary is used.
    """
    global consecutive_losses, loss_pause_until
    reset_daily_if_needed()
    records=closed_positions_history(100)
    today=amsterdam_today(); scoped=[]
    try:
        from zoneinfo import ZoneInfo
        tz=ZoneInfo("Europe/Amsterdam")
    except Exception:
        tz=timezone(timedelta(hours=2))

    # Normal day boundary.
    now_local=datetime.now(tz)
    day_start=now_local.replace(hour=0,minute=0,second=0,microsecond=0)
    risk_start=day_start
    if V6_RISK_EPOCH_LOCAL:
        try:
            epoch=datetime.fromisoformat(V6_RISK_EPOCH_LOCAL)
            if epoch.tzinfo is None: epoch=epoch.replace(tzinfo=tz)
            else: epoch=epoch.astimezone(tz)
            # The manual reset only overrides the day boundary on that same day.
            if epoch.date()==now_local.date() and epoch>risk_start:
                risk_start=epoch
        except Exception as e:
            tg(f"⚠️ V6 risk epoch parse warning\n{V6_RISK_EPOCH_LOCAL}\n{e}")

    for r in records:
        hid=str(r.get("historyId",""))
        if hid: seen_closed_history_ids.add(hid)
        ts=safe_float(r.get("updateTime"))/1000.0
        if ts<=0: continue
        dt=datetime.fromtimestamp(ts,tz)
        if dt.date().isoformat()==today and dt>=risk_start:
            scoped.append(r)

    daily.update({"trades":0,"wins":0,"losses":0,"net":0.0,
                  "long_trades":0,"long_wins":0,"long_net":0.0,
                  "short_trades":0,"short_wins":0,"short_net":0.0})
    consecutive_losses=0; sniper_side_loss_streak["long"]=0; sniper_side_loss_streak["short"]=0
    last_loss_ts=0.0
    for r in sorted(scoped,key=lambda x:safe_float(x.get("updateTime"))):
        side=infer_side(r); bucket="short" if side=="short" else "long"
        gross=safe_float(r.get("realizedPnl")); fee=abs(safe_float(r.get("fee"))); net=gross-fee
        daily["trades"]+=1; daily["net"]+=net; daily[f"{bucket}_trades"]+=1; daily[f"{bucket}_net"]+=net
        if net>=0:
            daily["wins"]+=1; daily[f"{bucket}_wins"]+=1; consecutive_losses=0; sniper_side_loss_streak[bucket]=0; last_loss_ts=0.0
        else:
            daily["losses"]+=1; consecutive_losses+=1; sniper_side_loss_streak[bucket]=sniper_side_loss_streak.get(bucket,0)+1
            last_loss_ts=safe_float(r.get("updateTime"))/1000.0
    # A process restart must never erase an already-active consecutive-loss pause.
    loss_pause_until=0.0
    if consecutive_losses>=MAX_CONSECUTIVE_LOSSES and last_loss_ts>0:
        loss_pause_until=max(0.0,last_loss_ts+LOSS_PAUSE_MIN*60)
    try:
        current_equity=futures_account()["total_equity"]
        # If no closes occurred since the reset this is exactly current equity: 0.00% DD.
        daily["start_equity"]=current_equity-daily["net"]
    except Exception:
        pass
    tg(f"📚 V6.2 RISK BASELINE READY\nRisk window starts: {risk_start.strftime('%Y-%m-%d %H:%M')} Amsterdam\n"
       f"Closes since reset: {daily['trades']} · W/L {daily['wins']}/{daily['losses']} · Net ${daily['net']:.2f}\n"
       f"Risk baseline: ${safe_float(daily.get('start_equity')):.2f}" + (f"\nLoss pause remains active until {datetime.fromtimestamp(loss_pause_until,tz).strftime('%H:%M')}" if loss_pause_until>time.time() else ""))


def maybe_score_report(rows):
    global _v5_last_report
    now=time.time()
    interesting=[r for r in rows if r and (r.get("side") or r.get("strategy") in ("TREND","RANGE"))]
    if not interesting or now-_v5_last_report<SNIPER_REPORT_MIN*60:return
    _v5_last_report=now
    ranked=sorted(interesting,key=lambda r:safe_float(r.get("quality",r.get("score",0))),reverse=True)[:10]
    lines=[f"🧠 PRECISION SNIPER v{SNIPER_VERSION} · ADAPTIVE REGIME ROUTER"]
    for r in ranked:
        state="🟢 READY" if r.get("side") and not r.get("skip") else f"⏭ {r.get('skip','wait')}"
        lines.append(f"{r['symbol'].split('/')[0]} {str(r.get('side') or '-').upper()} {r.get('strategy','-')} Q{safe_float(r.get('score')):.0f} 5m{r.get('five_confirm','-')} ADX{safe_float(r.get('adx')):.0f} {state}")
    tg("\n".join(lines))


def startup_diagnostics():
    acc=futures_account();reset_daily_if_needed(acc["total_equity"])
    tg(f"🧠 PRECISION SNIPER BOT STARTED v{SNIPER_VERSION}\nMode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
       f"NEW ENGINE: ADAPTIVE REGIME ROUTER — NO crossover entries, NO blind reversal\n"
       f"TREND: 4H+1H aligned → 15m pullback/reclaim → >= {V5_MIN_5M_CONFIRM}/4 5m confirm · Q>={V5_TREND_MIN_QUALITY:.0f}\n"
       f"RANGE: low/mixed HTF → 15m liquidity sweep/rejection → >= {V5_MIN_5M_CONFIRM}/4 5m confirm · Q>={V5_SWEEP_MIN_QUALITY:.0f}\n"
       f"MIXED regime = NO TRADE\nBTC macro gate: {'ON' if V5_BTC_GATE else 'OFF'}\n"
       f"TREND exit: {V5_TREND_TP1_R:.2f}R→BE · {V5_TREND_TP2_R:.2f}R→lock/trail · {V5_TREND_FINAL_R:.2f}R final · hard {V5_TREND_HARD_EXIT_MIN}m\n"
       f"SWEEP exit: {V5_SWEEP_TP1_R:.2f}R→BE · {V5_SWEEP_TP2_R:.2f}R→lock · {V5_SWEEP_FINAL_R:.2f}R final · hard {V5_SWEEP_HARD_EXIT_MIN}m\n"
       f"Risk caps: trend {V5_TREND_RISK_PCT*100:.1f}% · sweep {V5_SWEEP_RISK_PCT*100:.1f}% · elite {V5_ELITE_RISK_PCT*100:.1f}% · max positions {MAX_ACTIVE_POSITIONS}\n"
       f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}")


def main():
    global protection_lock
    load_learning();startup_diagnostics()
    if not PAPER_TRADING:
        try:seed_closed_history_v5()
        except Exception as e:tg(f"⚠️ V5 PNL seed warning\n{e}")
        try:
            inherited=get_positions();protection_lock=not all_positions_protected() if inherited else False
            if inherited:tg(f"ℹ️ V5 HANDOVER\n{len(inherited)} existing position(s). Existing protection left untouched."+("\n⚠️ Unprotected position detected: entries locked." if protection_lock else "\n✅ Protection verified."))
        except Exception as e:protection_lock=True;tg(f"🚨 STARTUP PROTECTION CHECK FAILED\n{e}")
    while True:
        try:
            if not PAPER_TRADING:
                try:check_for_closed_positions()
                except Exception as e:tg(f"⚠️ PNL TRACKER ERROR\n{e}")
                try:manage_sniper_step_stops()
                except Exception as e:tg(f"⚠️ V5 MANAGER ERROR\n{e}")
            acc=futures_account();ok,reason=check_daily_risk(acc["total_equity"])
            if not ok:tg(f"🛑 {reason}");time.sleep(120);continue
            if time.time()<loss_pause_until:time.sleep(SCAN_SECONDS);continue
            positions=get_positions()
            if not PAPER_TRADING:protection_watch();positions=get_positions()
            if protection_lock and not PAPER_TRADING:time.sleep(SCAN_SECONDS);continue
            scanned=[];candidates=[];existing={position_symbol(p) for p in positions}
            for symbol in SYMBOLS:
                if not cooldown_ok(symbol):continue
                try:
                    s=evaluate_symbol(symbol)
                    if not s:continue
                    scanned.append(s)
                    if s.get("side") and not s.get("skip") and s.get("entry_route") in ("TREND-PULLBACK","RANGE-SWEEP"):
                        key=int(s.get("signal_bar_ts",0))
                        if sniper_last_signal_bar.get(symbol)==key:continue
                        if symbol in existing:sniper_last_signal_bar[symbol]=key;continue
                        candidates.append(s)
                except Exception:continue
            maybe_score_report(scanned)
            candidates.sort(key=lambda x:(safe_float(x.get("quality")),1 if x.get("strategy")=="TREND" else 0,int(x.get("five_confirm",0))),reverse=True)
            attempts=0
            for s in candidates:
                if len(get_positions())>=MAX_ACTIVE_POSITIONS or attempts>=MAX_EXECUTION_ATTEMPTS_PER_SCAN:break
                allowed,why=sniper_portfolio_allows(s,get_positions())
                if not allowed:
                    sniper_last_signal_bar[s["symbol"]]=int(s.get("signal_bar_ts",0));tg(f"⏭️ {s['symbol']} {s['side'].upper()} V5 PORTFOLIO\n{why}");continue
                attempts+=1;opened=open_trade(s);sniper_last_signal_bar[s["symbol"]]=int(s.get("signal_bar_ts",0))
                if opened:symbol_last_trade[s["symbol"]]=time.time();break
            time.sleep(SCAN_SECONDS)
        except KeyboardInterrupt:tg("🛑 Precision Sniper V5 stopped.");raise
        except Exception as e:
            msg=f"{type(e).__name__}: {e}"
            if "152002" in msg:time.sleep(20);continue
            tg(f"⚠️ PRECISION SNIPER V5 LOOP ERROR\n{msg}");time.sleep(30)



# ============================================================
# PRECISION STRUCTURE FUSION V6.2 — 4H + 15M LOCATION-FIRST
# ============================================================
# Design rules translated from the supplied chart examples:
#   1) 4H decides directional regime; it never times the entry.
#   2) 15M maps key support/resistance and uses EMA21 as directional filter.
#   3) Entries require price-action structure: role reversal, liquidity sweep,
#      FVG interaction, or H&S / inverse-H&S neckline break.
#   4) At least three independent confirmations must agree.
#   5) Final target is 3R; stop is structural and never widened after entry.
# No 1H and no 5M data are used by the active V6 engine.

SNIPER_VERSION = "6.2.0"

# V6.1 risk baseline: intentional reset when Structure Fusion became the active strategy.
# This timestamp is persistent across process restarts, so a restart does NOT reset risk again.
# On following Amsterdam calendar days the normal midnight daily reset applies.
V6_RISK_EPOCH_LOCAL = os.getenv("V6_RISK_EPOCH_LOCAL", "2026-09-27T21:30:00").strip()
V6_DAILY_RISK_REMINDER_MIN = int(os.getenv("V6_DAILY_RISK_REMINDER_MIN", "60"))
_v6_daily_risk_alerted = False
_v6_daily_risk_last_notice = 0.0
V6_MIN_QUALITY = float(os.getenv("V6_MIN_QUALITY", "78"))
V6_ELITE_QUALITY = float(os.getenv("V6_ELITE_QUALITY", "90"))
V6_MIN_CONFIRMATIONS = int(os.getenv("V6_MIN_CONFIRMATIONS", "3"))
V6_4H_MIN_ADX = float(os.getenv("V6_4H_MIN_ADX", "16"))
V6_MAX_VOL_RATIO = float(os.getenv("V6_MAX_VOL_RATIO", "1.60"))
V6_ZONE_LOOKBACK = int(os.getenv("V6_ZONE_LOOKBACK", "64"))
V6_SWEEP_LOOKBACK = int(os.getenv("V6_SWEEP_LOOKBACK", "20"))
V6_FVG_LOOKBACK = int(os.getenv("V6_FVG_LOOKBACK", "14"))
V6_HNS_LOOKBACK = int(os.getenv("V6_HNS_LOOKBACK", "80"))
V6_ZONE_TOL_ATR = float(os.getenv("V6_ZONE_TOL_ATR", "0.22"))
V6_STOP_BUFFER_ATR = float(os.getenv("V6_STOP_BUFFER_ATR", "0.12"))
V6_MIN_STOP_ATR = float(os.getenv("V6_MIN_STOP_ATR", "0.65"))
V6_MAX_STOP_ATR = float(os.getenv("V6_MAX_STOP_ATR", "1.60"))
V6_MAX_ENTRY_EXTENSION_ATR = float(os.getenv("V6_MAX_ENTRY_EXTENSION_ATR", "0.45"))
# V6.2 LOCATION-FIRST: do not buy the top / sell the bottom of an already mature 4H impulse.
V62_MAX_4H_EMA21_EXT_ATR = float(os.getenv("V62_MAX_4H_EMA21_EXT_ATR", "1.05"))
V62_EDGE_4H_RANGE_POS = float(os.getenv("V62_EDGE_4H_RANGE_POS", "0.88"))
V62_EDGE_MIN_EXT_ATR = float(os.getenv("V62_EDGE_MIN_EXT_ATR", "0.55"))
V62_MAX_4H_IMPULSE3_ATR = float(os.getenv("V62_MAX_4H_IMPULSE3_ATR", "1.75"))
V62_EMA21_RESET_LOOKBACK = int(os.getenv("V62_EMA21_RESET_LOOKBACK", "6"))
V62_EMA21_RESET_TOL_ATR = float(os.getenv("V62_EMA21_RESET_TOL_ATR", "0.18"))
V62_MAX_ADVERSE_DRIFT = float(os.getenv("V62_MAX_ADVERSE_DRIFT", "0.0015"))  # 0.15%
V62_GLOBAL_ENTRY_GAP_MIN = int(os.getenv("V62_GLOBAL_ENTRY_GAP_MIN", "8"))
_v62_last_entry_time = 0.0
V6_FINAL_R = float(os.getenv("V6_FINAL_R", "3.0"))
V6_TP1_R = float(os.getenv("V6_TP1_R", "1.0"))
V6_TP2_R = float(os.getenv("V6_TP2_R", "2.0"))
V6_LOCK_R = float(os.getenv("V6_LOCK_R", "1.0"))
V6_RISK_PCT = float(os.getenv("V6_RISK_PCT", "0.017"))
V6_ELITE_RISK_PCT = float(os.getenv("V6_ELITE_RISK_PCT", "0.020"))
V6_TARGET_NET = float(os.getenv("V6_TARGET_NET", "2.10"))
V6_MAX_SAME_SIDE = int(os.getenv("V6_MAX_SAME_SIDE", "2"))
V6_BTC_CONTEXT = os.getenv("V6_BTC_CONTEXT", "true").lower() == "true"
V6_SOFT_EXIT_MIN = int(os.getenv("V6_SOFT_EXIT_MIN", "240"))
V6_HARD_EXIT_MIN = int(os.getenv("V6_HARD_EXIT_MIN", "720"))
V6_REPORT_MIN = int(os.getenv("V6_REPORT_MIN", "15"))
V6_BTC_SYMBOL = os.getenv("V6_BTC_SYMBOL", "BTC/USDT:USDT")

_v6_position_meta = {}
_v6_last_report = 0.0


def _v6_df(symbol, tf):
    df = sniper_indicators(ohlcv_df(symbol, tf))
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    return df


def _v6_closed(df):
    # Exchange OHLCV normally contains the currently-forming candle as the last row.
    return df.iloc[:-1].copy()


def _v6_pivots(df, left=2, right=2):
    highs, lows = [], []
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    for i in range(left, len(df)-right):
        if h[i] >= np.max(h[i-left:i+right+1]) and h[i] > np.max(np.r_[h[i-left:i], h[i+1:i+right+1]]):
            highs.append((i, h[i]))
        if l[i] <= np.min(l[i-left:i+right+1]) and l[i] < np.min(np.r_[l[i-left:i], l[i+1:i+right+1]]):
            lows.append((i, l[i]))
    return highs, lows


def _v6_4h_context(symbol):
    try:
        df = _v6_df(symbol, "4h")
        cdf = _v6_closed(df)
        if len(cdf) < 210:
            return {"dir":0,"score":0,"adx":0.0,"close":0.0,"ema21":0.0,"ema55":0.0,"ema200":0.0,"structure":0,"atr":0.0,"range20_high":0.0,"range20_low":0.0,"range_pos20":0.5,"close3":0.0}
        a, p = cdf.iloc[-1], cdf.iloc[-2]
        c = safe_float(a["close"]); e21=safe_float(a["ema_slow"]); e55=safe_float(a["ema_trend"]); e200=safe_float(a["ema200"])
        slope21 = e21 - safe_float(cdf.iloc[-4]["ema_slow"])
        hist = safe_float(a["macd_hist"]); histp=safe_float(p["macd_hist"])
        adx = safe_float(a["sniper_adx"])
        atr4 = max(safe_float(a["atr"]), 1e-12)
        recent20 = cdf.iloc[-20:]
        hi20 = safe_float(recent20["high"].max()); lo20 = safe_float(recent20["low"].min())
        span20 = max(hi20-lo20, 1e-12)
        range_pos20 = max(0.0, min(1.0, (c-lo20)/span20))
        close3 = safe_float(cdf.iloc[-4]["close"]) if len(cdf) >= 4 else c
        recent = cdf.iloc[-70:]
        ph, pl = _v6_pivots(recent,2,2)
        structure=0
        if len(ph)>=2 and len(pl)>=2:
            hh=ph[-1][1] > ph[-2][1]; hl=pl[-1][1] > pl[-2][1]
            lh=ph[-1][1] < ph[-2][1]; ll=pl[-1][1] < pl[-2][1]
            structure = 1 if hh and hl else -1 if lh and ll else 0
        bull = int(c>e21) + int(e21>e55) + int(e55>e200) + int(slope21>0) + int(hist>=histp) + int(structure==1)
        bear = int(c<e21) + int(e21<e55) + int(e55<e200) + int(slope21<0) + int(hist<=histp) + int(structure==-1)
        d = 1 if bull>=4 and bull>bear else -1 if bear>=4 and bear>bull else 0
        strength=max(bull,bear)
        return {"dir":d,"score":strength,"bull":bull,"bear":bear,"adx":adx,"close":c,"ema21":e21,"ema55":e55,"ema200":e200,"structure":structure,
                "atr":atr4,"range20_high":hi20,"range20_low":lo20,"range_pos20":range_pos20,"close3":close3}
    except Exception:
        return {"dir":0,"score":0,"adx":0.0,"close":0.0,"ema21":0.0,"ema55":0.0,"ema200":0.0,"structure":0,"atr":0.0,"range20_high":0.0,"range20_low":0.0,"range_pos20":0.5,"close3":0.0}


def _v62_4h_location(h4, d, live_price=None):
    """Return (ok, reason, metrics). Directional 4H location gate.

    It deliberately rejects mature impulses even when trend and 15M confirmations look good.
    This is the fix for entries such as buying RENDER near a 4H local high after the move
    has already travelled far from the 4H EMA21.
    """
    atr=max(safe_float(h4.get("atr")),1e-12)
    px=safe_float(live_price) if safe_float(live_price)>0 else safe_float(h4.get("close"))
    e21=safe_float(h4.get("ema21")); hi=safe_float(h4.get("range20_high")); lo=safe_float(h4.get("range20_low"))
    close3=safe_float(h4.get("close3"),px)
    if px<=0 or e21<=0 or hi<=lo:
        return False,"4H-location-unavailable",{}
    ext=d*(px-e21)/atr
    pos=max(0.0,min(1.0,(px-lo)/max(hi-lo,1e-12)))
    edge=pos if d==1 else 1.0-pos
    impulse3=d*(px-close3)/atr
    metrics={"h4_ext_atr":ext,"h4_range_pos":pos,"h4_edge":edge,"h4_impulse3_atr":impulse3}
    if ext>V62_MAX_4H_EMA21_EXT_ATR:
        return False,f"4H-too-far-from-EMA21-{ext:.2f}ATR",metrics
    if edge>V62_EDGE_4H_RANGE_POS and ext>V62_EDGE_MIN_EXT_ATR:
        return False,f"4H-at-range-edge-{edge:.0%}-ext{ext:.2f}ATR",metrics
    if impulse3>V62_MAX_4H_IMPULSE3_ATR and ext>0.40:
        return False,f"4H-impulse-already-ran-{impulse3:.2f}ATR",metrics
    return True,"4H-location-OK",metrics

def _v62_recent_ema21_reset(cdf,d,atr):
    """Require a recent 15M reset toward EMA21 before entering with the 4H trend."""
    n=max(2,V62_EMA21_RESET_LOOKBACK)
    recent=cdf.iloc[-(n+1):-1] if len(cdf)>n+1 else cdf.iloc[:-1]
    if recent.empty:return False
    tol=V62_EMA21_RESET_TOL_ATR*atr
    if d==1:
        return bool((recent["low"] <= recent["ema_slow"] + tol).any())
    return bool((recent["high"] >= recent["ema_slow"] - tol).any())

def _v62_live_location_ok(symbol,side,live):
    d=1 if side=="long" else -1
    h4=_v6_4h_context(symbol)
    if h4.get("dir")!=d:
        return False,"4H-direction-changed",{}
    return _v62_4h_location(h4,d,live)

def _v6_btc_gate(symbol, d):
    if not V6_BTC_CONTEXT or symbol == V6_BTC_SYMBOL:
        return True, "BTC self/off"
    try:
        h4=_v6_4h_context(V6_BTC_SYMBOL)
        b15=_v6_df(V6_BTC_SYMBOL,"15m"); a=_v6_closed(b15).iloc[-1]
        c=safe_float(a["close"]); e21=safe_float(a["ema_slow"])
        d15=1 if c>e21 else -1 if c<e21 else 0
        if h4["dir"] == -d and d15 == -d and h4["score"]>=4:
            return False, f"BTC 4H/15M against ({h4['dir']:+d}/{d15:+d})"
        return True, f"BTC 4H/15M {h4['dir']:+d}/{d15:+d}"
    except Exception:
        return True, "BTC context unavailable"


def _v6_candle_pattern(cdf, d):
    if len(cdf)<4:return False,"none",0.0
    a,b,c=cdf.iloc[-3],cdf.iloc[-2],cdf.iloc[-1]
    atr=max(safe_float(c["atr"]),1e-12)
    ao,ac=safe_float(a["open"]),safe_float(a["close"])
    bo,bc=safe_float(b["open"]),safe_float(b["close"])
    co,cc=safe_float(c["open"]),safe_float(c["close"])
    ch,cl=safe_float(c["high"]),safe_float(c["low"])
    cr=max(ch-cl,1e-12); body=abs(cc-co); upper=ch-max(co,cc); lower=min(co,cc)-cl
    if d==-1:
        star=(ac>ao and abs(bc-bo)<=abs(ac-ao)*0.60 and cc<co and cc <= (ao+ac)/2)
        rejection=upper/cr>=0.38 and cc<co
        engulf=cc<co and co>=bc and cc<=bo
        if star:return True,"evening-star",min(1.0,abs(cc-co)/atr)
        if engulf:return True,"bearish-engulf",min(1.0,body/atr)
        if rejection:return True,"upper-wick-reject",upper/cr
    else:
        star=(ac<ao and abs(bc-bo)<=abs(ac-ao)*0.60 and cc>co and cc >= (ao+ac)/2)
        rejection=lower/cr>=0.38 and cc>co
        engulf=cc>co and co<=bc and cc>=bo
        if star:return True,"morning-star",min(1.0,abs(cc-co)/atr)
        if engulf:return True,"bullish-engulf",min(1.0,body/atr)
        if rejection:return True,"lower-wick-reject",lower/cr
    return False,"none",0.0


def _v6_role_reversal(cdf, d, atr):
    look=cdf.iloc[-min(len(cdf),V6_ZONE_LOOKBACK):]
    ph,pl=_v6_pivots(look,2,2)
    current=look.iloc[-1]; cc=safe_float(current["close"]); hi=safe_float(current["high"]); lo=safe_float(current["low"])
    tol=V6_ZONE_TOL_ATR*atr
    pivots=ph if d==1 else pl  # long: old resistance -> support; short: old support -> resistance
    for idx,level in reversed(pivots[-8:]):
        if idx>=len(look)-5:continue
        after=look.iloc[idx+1:-1]
        if after.empty:continue
        if d==1:
            broken=(after["close"]>level+0.04*atr).any()
            touched=(lo<=level+tol and hi>=level-tol)
            held=cc>level and cc>safe_float(current["ema_slow"])
        else:
            broken=(after["close"]<level-0.04*atr).any()
            touched=(hi>=level-tol and lo<=level+tol)
            held=cc<level and cc<safe_float(current["ema_slow"])
        if broken and touched and held:
            return True,level
    return False,0.0


def _v6_liquidity_sweep(cdf,d,atr):
    if len(cdf)<V6_SWEEP_LOOKBACK+3:return False,0.0,0.0
    a=cdf.iloc[-1]; prior=cdf.iloc[-(V6_SWEEP_LOOKBACK+1):-1]
    hi=safe_float(prior["high"].max()); lo=safe_float(prior["low"].min())
    c=safe_float(a["close"]); ah=safe_float(a["high"]); al=safe_float(a["low"])
    if d==1 and al<lo-0.03*atr and c>lo:
        return True,lo,(lo-al)/atr
    if d==-1 and ah>hi+0.03*atr and c<hi:
        return True,hi,(ah-hi)/atr
    return False,0.0,0.0


def _v6_fvg(cdf,d,atr):
    # Search recent 3-candle imbalances and require the current/recent price to interact with the zone.
    if len(cdf)<V6_FVG_LOOKBACK+4:return False,0.0,0.0
    start=max(2,len(cdf)-V6_FVG_LOOKBACK-2)
    cur=cdf.iloc[-1]; ch=safe_float(cur["high"]); cl=safe_float(cur["low"]); cc=safe_float(cur["close"])
    best=None
    for i in range(start,len(cdf)-1):
        left=cdf.iloc[i-2]; right=cdf.iloc[i]
        if d==1:
            # Bullish FVG: right low above left high.
            zlo=safe_float(left["high"]); zhi=safe_float(right["low"])
            if zhi-zlo >= 0.06*atr:
                interacted=cl<=zhi+0.05*atr and ch>=zlo-0.05*atr and cc>=zlo
                if interacted:best=(zlo,zhi)
        else:
            # Bearish FVG: right high below left low.
            zlo=safe_float(right["high"]); zhi=safe_float(left["low"])
            if zhi-zlo >= 0.06*atr:
                interacted=ch>=zlo-0.05*atr and cl<=zhi+0.05*atr and cc<=zhi
                if interacted:best=(zlo,zhi)
    return (True,best[0],best[1]) if best else (False,0.0,0.0)


def _v6_hns(cdf,d,atr):
    look=cdf.iloc[-min(len(cdf),V6_HNS_LOOKBACK):]
    ph,pl=_v6_pivots(look,2,2)
    cc=safe_float(look.iloc[-1]["close"])
    if d==-1 and len(ph)>=3:
        # Test recent triples, newest first, for left shoulder / head / right shoulder.
        for p1,p2,p3 in reversed([ph[i:i+3] for i in range(max(0,len(ph)-6),len(ph)-2)]):
            if not (p1[0]<p2[0]<p3[0]):continue
            shoulders=abs(p1[1]-p3[1])<=0.65*atr
            head=p2[1]>=max(p1[1],p3[1])+0.18*atr
            lows1=look.iloc[p1[0]:p2[0]+1]["low"]; lows2=look.iloc[p2[0]:p3[0]+1]["low"]
            if lows1.empty or lows2.empty:continue
            neck=(safe_float(lows1.min())+safe_float(lows2.min()))/2
            if shoulders and head and cc<neck-0.03*atr:
                return True,neck
    if d==1 and len(pl)>=3:
        for p1,p2,p3 in reversed([pl[i:i+3] for i in range(max(0,len(pl)-6),len(pl)-2)]):
            if not (p1[0]<p2[0]<p3[0]):continue
            shoulders=abs(p1[1]-p3[1])<=0.65*atr
            head=p2[1]<=min(p1[1],p3[1])-0.18*atr
            highs1=look.iloc[p1[0]:p2[0]+1]["high"]; highs2=look.iloc[p2[0]:p3[0]+1]["high"]
            if highs1.empty or highs2.empty:continue
            neck=(safe_float(highs1.max())+safe_float(highs2.max()))/2
            if shoulders and head and cc>neck+0.03*atr:
                return True,neck
    return False,0.0


def _v6_plan(cdf,side,route,zone_level=0.0):
    a=cdf.iloc[-1]; d=1 if side=="long" else -1
    entry=safe_float(a["close"]); atr=safe_float(a["atr"])
    if entry<=0 or atr<=0:return None
    recent=cdf.iloc[-12:]
    struct=safe_float(recent["low"].min())-V6_STOP_BUFFER_ATR*atr if d==1 else safe_float(recent["high"].max())+V6_STOP_BUFFER_ATR*atr
    if zone_level>0:
        zone_stop=zone_level-d*V6_STOP_BUFFER_ATR*atr
        struct=min(struct,zone_stop) if d==1 else max(struct,zone_stop)
    raw=abs(entry-struct)
    risk=max(V6_MIN_STOP_ATR*atr,min(raw,V6_MAX_STOP_ATR*atr))
    return {"risk_distance":risk,"atr":atr,"origin":f"V6 {route} structure","final_r":V6_FINAL_R}


def _v6_candidate(symbol):
    h4=_v6_4h_context(symbol); d=h4.get("dir",0)
    df=_v6_df(symbol,"15m"); cdf=_v6_closed(df)
    if len(cdf)<210:return None
    a,p=cdf.iloc[-1],cdf.iloc[-2]
    c=safe_float(a["close"]); atr=safe_float(a["atr"]); atrm=safe_float(a["atr_mean"])
    if atr<=0:return None
    if d==0:
        return {"symbol":symbol,"side":None,"score":0,"quality":0,"grade":"-","skip":"4H-no-clean-direction","entry_route":"WAIT","strategy":"STRUCTURE","price":c,"atr":atr,"signal_bar_ts":int(a["ts"]),"h4_adx":h4.get("adx",0)}
    side="long" if d==1 else "short"
    e21=safe_float(a["ema_slow"]); e21p=safe_float(cdf.iloc[-4]["ema_slow"])
    ema_ok=(c>e21 and e21>e21p) if d==1 else (c<e21 and e21<e21p)
    extension=d*(c-e21)/atr
    location_ok,location_note,location_metrics=_v62_4h_location(h4,d)
    ema21_reset=_v62_recent_ema21_reset(cdf,d,atr)
    role,role_level=_v6_role_reversal(cdf,d,atr)
    sweep,sweep_level,sweep_depth=_v6_liquidity_sweep(cdf,d,atr)
    fvg,fvg_lo,fvg_hi=_v6_fvg(cdf,d,atr)
    hns,neck=_v6_hns(cdf,d,atr)
    candle,candle_name,candle_strength=_v6_candle_pattern(cdf,d)
    hist=safe_float(a["macd_hist"]); hp=safe_float(p["macd_hist"])
    macd_ok=(hist>hp) if d==1 else (hist<hp)
    rsi=safe_float(a["sniper_rsi"],50); rsi_ok=(50<=rsi<=70) if d==1 else (30<=rsi<=50)
    adx=safe_float(a["sniper_adx"]); adx_ok=adx>=15
    volm=safe_float(a["vol_mean"]); vol=safe_float(a["volume"]); vol_ok=volm>0 and vol>=volm*1.08
    vol_ratio=atr/atrm if atrm>0 else 99
    btc_ok,btc_note=_v6_btc_gate(symbol,d)

    # Primary structure is mandatory. This prevents indicator-only entries.
    primary = role or sweep or hns
    confirmations = int(role)+int(sweep)+int(fvg)+int(hns)+int(candle)+int(macd_ok)+int(vol_ok)
    q=0.0
    q += 24 if h4.get("score",0)>=5 else 20
    q += 14 if ema_ok else 0
    q += 18 if role else 0
    q += 17 if sweep else 0
    q += 13 if fvg else 0
    q += 20 if hns else 0
    q += 11 if candle else 0
    q += 8 if macd_ok else 0
    q += 5 if rsi_ok else 0
    q += 4 if adx_ok else 0
    q += 4 if vol_ok else 0
    q=min(q,100.0)

    if hns:route="HNS-NECKLINE"
    elif sweep and fvg:route="SWEEP-FVG"
    elif role and candle:route="DOUBLE-CONFIRM"
    elif role:route="ROLE-REVERSAL"
    else:route="STRUCTURE"
    level=neck if hns else sweep_level if sweep else role_level if role else 0.0
    plan=_v6_plan(cdf,side,route,level)

    skip=None
    if h4.get("adx",0)<V6_4H_MIN_ADX and h4.get("score",0)<5:skip=f"4H-weak-ADX-{h4.get('adx',0):.0f}"
    elif not location_ok:skip=location_note
    elif not ema_ok:skip="15M-EMA21-filter"
    elif not ema21_reset:skip="15M-no-recent-EMA21-reset"
    elif not primary:skip="no-key-structure"
    elif confirmations<V6_MIN_CONFIRMATIONS:skip=f"confirmations-{confirmations}<{V6_MIN_CONFIRMATIONS}"
    elif not macd_ok:skip="15M-momentum-not-confirmed"
    elif vol_ratio>V6_MAX_VOL_RATIO:skip=f"volatility-{vol_ratio:.2f}x"
    elif extension>V6_MAX_ENTRY_EXTENSION_ATR:skip=f"entry-extended-{extension:.2f}ATR"
    elif not btc_ok:skip=btc_note
    elif q<V6_MIN_QUALITY:skip=f"quality-{q:.0f}<{V6_MIN_QUALITY:.0f}"
    elif not plan:skip="invalid-plan"

    return {"symbol":symbol,"side":side,"score":q,"score_max":100.0,"quality":q,"threshold":V6_MIN_QUALITY,
            "grade":"A+" if q>=V6_ELITE_QUALITY else "A" if q>=V6_MIN_QUALITY else "C","skip":skip,
            "entry_route":route if skip is None else "WAIT","strategy":"STRUCTURE","price":c,"atr":atr,"adx":adx,"rsi":rsi,
            "signal_bar_ts":int(a["ts"]),"risk_distance":plan["risk_distance"] if plan else 0.0,
            "stop_origin":plan["origin"] if plan else "N/A","final_r":V6_FINAL_R,"h4_score":h4.get("score",0),"h4_adx":h4.get("adx",0),
            "confirmations":confirmations,"role":role,"sweep":sweep,"fvg":fvg,"hns":hns,"candle":candle_name if candle else "-",
            "macd_ok":macd_ok,"volume_above":vol_ok,"extension_atr":extension,"btc_note":btc_note,
            "ema21_reset":ema21_reset,"location_note":location_note,
            "h4_ext_atr":safe_float(location_metrics.get("h4_ext_atr")),"h4_range_pos":safe_float(location_metrics.get("h4_range_pos")),
            "h4_impulse3_atr":safe_float(location_metrics.get("h4_impulse3_atr")),
            "zone_level":level,"fvg_zone":(fvg_lo,fvg_hi) if fvg else None,"sweep_depth":sweep_depth}


def evaluate_symbol(symbol, btc=None):
    return _v6_candidate(symbol)


def sizing_profile(signal):
    elite=safe_float(signal.get("score"))>=V6_ELITE_QUALITY and int(signal.get("confirmations",0))>=4
    return {"name":"V6-ELITE" if elite else "V6-STRUCTURE","risk_pct":V6_ELITE_RISK_PCT if elite else V6_RISK_PCT,"target_net_tp":V6_TARGET_NET}


def _v6_levels(signal,live,tick):
    side=signal["side"];d=1 if side=="long" else -1;risk=safe_float(signal.get("risk_distance"))
    if live<=0 or risk<=0:return None
    sl=round_trade_price(live-d*risk,tick,side,"sl")
    t1=round_trade_price(live+d*risk*V6_TP1_R,tick,side,"tp")
    t2=round_trade_price(live+d*risk*V6_TP2_R,tick,side,"tp")
    t3=round_trade_price(live+d*risk*V6_FINAL_R,tick,side,"tp")
    return sl,t1,t2,t3,risk


def sniper_portfolio_allows(signal,positions):
    side=signal.get("side");same=sum(1 for p in positions if position_side(p)==side)
    if same>=V6_MAX_SAME_SIDE:return False,f"max-{V6_MAX_SAME_SIDE}-same-side"
    if sniper_side_loss_streak.get(side,0)>=2 and safe_float(signal.get("score"))<V6_ELITE_QUALITY:
        return False,f"{side}-loss-streak-needs-elite"
    return True,"OK"


def open_trade(signal):
    global protection_lock
    symbol,side=signal["symbol"],signal["side"]
    acc=futures_account();equity,available=acc["total_equity"],acc["available"];reset_daily_if_needed(equity)
    if equity<MIN_EQUITY_USDT or available<=0 or (not PAPER_TRADING and protection_lock):return False
    if time.time()-_v62_last_entry_time < V62_GLOBAL_ENTRY_GAP_MIN*60:
        return False
    tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
    live=safe_float(exchange.fetch_ticker(symbol).get("last"),signal["price"]);sig=safe_float(signal["price"])
    if live<=0 or sig<=0:return False
    d=1 if side=="long" else -1;adverse=d*((live-sig)/sig)
    if adverse>V62_MAX_ADVERSE_DRIFT:
        tg(f"⏭️ {symbol} {side.upper()} V6.2 TOO LATE\nAdverse drift {adverse*100:.2f}% > {V62_MAX_ADVERSE_DRIFT*100:.2f}%");return False
    live_loc_ok,live_loc_note,live_loc=_v62_live_location_ok(symbol,side,live)
    if not live_loc_ok:
        tg(f"⏭️ {symbol} {side.upper()} V6.2 LOCATION BLOCK\n{live_loc_note}\nNo chase at 4H extreme.");return False
    levels=_v6_levels(signal,live,tick)
    if not levels:return False
    sl,t1,t2,t3,risk=levels;profile=sizing_profile(signal)
    sizing=calculate_contracts_v5(symbol,live,sl,equity,available,profile["risk_pct"],profile["target_net_tp"],V6_FINAL_R)
    if not sizing or sizing["margin"]>available*AVAILABLE_MARGIN_BUFFER:return False
    before_ids={position_id(p) for p in get_positions() if position_id(p)}
    oklev,levres=set_leverage(symbol,LEVERAGE)
    if not oklev:tg(f"⛔ LEVERAGE FAILED\n{symbol}\n{levres}");return False
    tag="ps6s"
    payload={"instId":blofin_inst_id(symbol),"marginMode":MARGIN_MODE,"positionSide":"net","side":"buy" if side=="long" else "sell","orderType":"market","size":str(sizing["contracts"]),
             "tpTriggerPrice":str(t3),"tpOrderPrice":"-1","tpTriggerPriceType":"mark","slTriggerPrice":str(sl),"slOrderPrice":"-1","slTriggerPriceType":"mark",
             "clientOrderId":f"{tag}{int(time.time()*1000)%1000000000}"}
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    flags=[]
    for k,label in (("role","ROLE"),("sweep","SWEEP"),("fvg","FVG"),("hns","H&S")):
        if signal.get(k):flags.append(label)
    if signal.get("candle") not in (None,"-","none"):flags.append(str(signal.get("candle")))
    msg=(f"{'🧪 PAPER' if PAPER_TRADING else '🧠 V6 STRUCTURE ENTRY'} v{SNIPER_VERSION}\n{symbol} {side.upper()} · {signal.get('entry_route')} · {signal['grade']} {signal['score']:.0f}/100\n"
         f"4H score {signal.get('h4_score',0)}/6 · ADX {signal.get('h4_adx',0):.1f} · EMA21 ext {signal.get('h4_ext_atr',0):.2f}ATR · range {signal.get('h4_range_pos',0)*100:.0f}%\n"
         f"15M confirmations {signal.get('confirmations',0)} · EMA21 reset {'YES' if signal.get('ema21_reset') else 'NO'} · {' + '.join(flags) if flags else '-'}\n"
         f"BTC {signal.get('btc_note','-')}\nEntry ~ {live:.8g}\nSL {sl:.8g}\nTP1 {t1:.8g} (1R)\nTP2 {t2:.8g} (2R)\nTP3 {t3:.8g} (3R)\n"
         f"Risk cap {profile['risk_pct']*100:.1f}% · expected TP net ~${sizing['expected_net_tp_usdt']:.2f} · SL+cost ~${sizing['expected_net_sl_usdt']:.2f}\n"
         f"Notional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x\nAdverse drift {max(0,adverse)*100:.2f}%/{SNIPER_MAX_FILL_DRIFT*100:.2f}%")
    if PAPER_TRADING:tg(msg);return True
    resp=native("private_post_trade_order",payload)
    if not native_ok(resp):
        err=response_error(resp)
        if "152012" in err:protection_lock=True
        tg(f"❌ V6 ORDER FAILED\n{symbol} {side.upper()}\n{err}");return False
    _v62_last_entry_time=time.time();tg(msg);new_pos=find_new_position(symbol,before_ids,timeout=8)
    if new_pos is None:tg(f"⚠️ {symbol}: V6 order accepted, position not visible yet.");return True
    pid=position_id(new_pos)
    if pid:
        sniper_managed_positions.add(pid);_v6_position_meta[pid]={"risk":risk,"tp1":t1,"tp2":t2,"opened":time.time(),"route":signal.get("entry_route"),"side":side}
    if has_live_protection(new_pos)[0]:tg(f"🛡️ V6 PROTECTION CONFIRMED\n{symbol} {side.upper()}\n3R TP + structural SL visible.");return True
    last=safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side,sl,t3,last):
        ok,res=place_tpsl_for_position(new_pos,sl,t3,client_tag=tag)
        if ok:tg(f"🛡️ V6 FALLBACK PROTECTION CONFIRMED\n{symbol}");return True
        protection_lock=True;tg(f"🚨 V6 PROTECTION FAILURE\n{symbol}\n{res}\nNEW TRADES PAUSED.");return True
    protection_lock=True;tg(f"🚨 {symbol}: V6 position exists but safe fallback protection failed.\nNEW TRADES PAUSED.");return True


def _v6_managed(p):
    pid=position_id(p)
    if pid in sniper_managed_positions or pid in _v6_position_meta:return True
    try:
        ok,probe=has_live_protection(p);cid=str((probe or {}).get("clientOrderId","") or "")
        if cid.startswith("ps6"):
            if pid:sniper_managed_positions.add(pid)
            return True
    except Exception:pass
    return False


def _v6_close_position(p,reason):
    payload={"instId":p["instId"],"marginMode":position_margin_mode(p),"positionSide":str(p.get("positionSide","net") or "net"),"clientOrderId":f"ps6x{int(time.time()*1000)%1000000000}"}
    pid=position_id(p)
    if pid:payload["positionId"]=pid
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    resp=native("private_post_trade_close_position",payload)
    if native_ok(resp):tg(f"⚡ V6 ACTIVE EXIT\n{position_symbol(p)} {position_side(p).upper()}\n{reason}\nClosed at market.");return True
    tg(f"⚠️ V6 ACTIVE EXIT FAILED\n{position_symbol(p)}\n{response_error(resp)}");return False


def manage_sniper_step_stops():
    if PAPER_TRADING:return
    now=time.time()
    for p in get_positions():
        if not _v6_managed(p):continue
        symbol,side,entry=position_symbol(p),position_side(p),position_entry(p);pid=position_id(p)
        if entry<=0:continue
        ok,order=has_live_protection(p)
        if not ok or not order:continue
        tp=safe_float(order.get("tpTriggerPrice"));current_sl=safe_float(order.get("slTriggerPrice"))
        if tp<=0 or current_sl<=0:continue
        meta=_v6_position_meta.get(pid,{})
        risk=safe_float(meta.get("risk"))
        if risk<=0:
            base=abs(tp-entry)/V6_FINAL_R
            if base<=0:continue
            risk=base;meta={"risk":risk,"opened":safe_float(p.get("createTime"))/1000.0,"route":"restart-adopt"};_v6_position_meta[pid]=meta
        d=1 if side=="long" else -1;tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
        current=safe_float(exchange.fetch_ticker(symbol).get("last"));ext=sniper_recent_extreme(symbol,side)
        favorable=max(current,ext) if d==1 else min(current,ext)
        current_r=d*(current-entry)/risk;best_r=d*(favorable-entry)/risk
        desired=None;stage=None
        if best_r>=V6_TP2_R:
            desired=round_trade_price(entry+d*V6_LOCK_R*risk,tick,side,"sl");stage="2R → lock +1R"
        elif best_r>=V6_TP1_R:
            desired=round_trade_price(entry,tick,side,"sl");stage="1R → break-even"
        if desired is not None:
            improves=desired>current_sl+tick*0.1 if d==1 else desired<current_sl-tick*0.1
            if improves:
                ok2,res=amend_tpsl_stop(p,desired);key=f"{pid}:{stage}"
                if ok2 and not sniper_step_notified.get(key):tg(f"🪜 V6 STEP STOP\n{symbol} {side.upper()}\n{stage} · SL {desired:.8g}");sniper_step_notified[key]=True
        opened=safe_float(meta.get("opened")) or safe_float(p.get("createTime"))/1000.0;age=(now-opened)/60 if opened>0 else 0
        # Only 15M + 4H are consulted for thesis exits.
        if age>=45 and current_r<0.35:
            try:
                cdf=_v6_closed(_v6_df(symbol,"15m"));a,pr=cdf.iloc[-1],cdf.iloc[-2]
                c=safe_float(a["close"]);e21=safe_float(a["ema_slow"]);pc=safe_float(pr["close"]);pe21=safe_float(pr["ema_slow"])
                lost15=(c<e21 and pc<pe21) if d==1 else (c>e21 and pc>pe21)
                h4=_v6_4h_context(symbol);macro_flip=h4.get("dir",0)==-d
                if lost15 and macro_flip:
                    _v6_close_position(p,f"4H/15M thesis invalidated after {age:.0f}m ({current_r:+.2f}R).");continue
            except Exception:pass
        if age>=V6_HARD_EXIT_MIN:_v6_close_position(p,f"Hard time exit {age:.0f}m ({current_r:+.2f}R).");continue
        if age>=V6_SOFT_EXIT_MIN and current_r<0.20:_v6_close_position(p,f"No follow-through after {age:.0f}m ({current_r:+.2f}R).");continue


def repair_one_position(p):
    symbol,side,entry=position_symbol(p),position_side(p),position_entry(p)
    if entry<=0:return False
    if has_live_protection(p)[0]:return True
    pid=position_id(p);meta=_v6_position_meta.get(pid,{});risk=safe_float(meta.get("risk"))
    if risk<=0:
        try:
            cdf=_v6_closed(_v6_df(symbol,"15m"));atr=safe_float(cdf.iloc[-1]["atr"]);risk=max(V6_MIN_STOP_ATR*atr,min(1.0*atr,V6_MAX_STOP_ATR*atr))
        except Exception:return False
    d=1 if side=="long" else -1;tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
    sl=round_trade_price(entry-d*risk,tick,side,"sl");tp=round_trade_price(entry+d*risk*V6_FINAL_R,tick,side,"tp")
    last=safe_float(exchange.fetch_ticker(symbol).get("last"))
    if not tpsl_prices_valid(side,sl,tp,last):return False
    return place_tpsl_for_position(p,sl,tp,client_tag="ps6s")[0]


def maybe_score_report(rows):
    global _v6_last_report
    now=time.time();interesting=[r for r in rows if r and (r.get("side") or r.get("skip"))]
    if not interesting or now-_v6_last_report<V6_REPORT_MIN*60:return
    _v6_last_report=now
    ranked=sorted(interesting,key=lambda r:safe_float(r.get("quality",r.get("score",0))),reverse=True)[:10]
    lines=[f"🧠 PRECISION STRUCTURE FUSION v{SNIPER_VERSION} · 4H + 15M ONLY"]
    for r in ranked:
        state="🟢 READY" if r.get("side") and not r.get("skip") else f"⏭ {r.get('skip','wait')}"
        lines.append(f"{r['symbol'].split('/')[0]} {str(r.get('side') or '-').upper()} {r.get('entry_route','-')} Q{safe_float(r.get('score')):.0f} C{r.get('confirmations','-')} 4H{r.get('h4_score','-')}/6 {state}")
    tg("\n".join(lines))


def startup_diagnostics():
    acc=futures_account();reset_daily_if_needed(acc["total_equity"])
    tg(f"🧠 PRECISION STRUCTURE FUSION STARTED v{SNIPER_VERSION}\nMode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
       f"TIMEFRAMES: 4H + 15M ONLY — no 1H, no 5M\n"
       f"4H: EMA21/55/200 + structure + MACD/ADX + LOCATION gate (no buying tops / selling bottoms)\n"
       f"15M: recent EMA21 reset required + role reversal + liquidity sweep + FVG + H&S + candle rejection\n"
       f"Anti-chase: 15M ext <= {V6_MAX_ENTRY_EXTENSION_ATR:.2f} ATR · 4H EMA21 ext <= {V62_MAX_4H_EMA21_EXT_ATR:.2f} ATR · live adverse drift <= {V62_MAX_ADVERSE_DRIFT*100:.2f}%\n"
       f"Entry requires primary market structure + >= {V6_MIN_CONFIRMATIONS} confirmations + Q >= {V6_MIN_QUALITY:.0f}\n"
       f"TP model: 1R→BE · 2R→lock +1R · 3R final\n"
       f"Risk caps: normal {V6_RISK_PCT*100:.1f}% · elite {V6_ELITE_RISK_PCT*100:.1f}% · max positions {MAX_ACTIVE_POSITIONS} · max same side {V6_MAX_SAME_SIDE}\n"
       f"BTC context (4H+15M only): {'ON' if V6_BTC_CONTEXT else 'OFF'}\n"
       f"Risk reset epoch: {V6_RISK_EPOCH_LOCAL or 'normal day start'} Amsterdam\n"
       f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}")


def main():
    global protection_lock
    load_learning();startup_diagnostics()
    if not PAPER_TRADING:
        try:seed_closed_history_v5()
        except Exception as e:tg(f"⚠️ V6 PNL seed warning\n{e}")
        try:
            inherited=get_positions();protection_lock=not all_positions_protected() if inherited else False
            if inherited:tg(f"ℹ️ V6 HANDOVER\n{len(inherited)} existing position(s). Existing protection left untouched."+("\n⚠️ Unprotected position detected: entries locked." if protection_lock else "\n✅ Protection verified."))
        except Exception as e:protection_lock=True;tg(f"🚨 STARTUP PROTECTION CHECK FAILED\n{e}")
    while True:
        try:
            if not PAPER_TRADING:
                try:check_for_closed_positions()
                except Exception as e:tg(f"⚠️ PNL TRACKER ERROR\n{e}")
                try:manage_sniper_step_stops()
                except Exception as e:tg(f"⚠️ V6 MANAGER ERROR\n{e}")
            acc=futures_account();ok,reason=check_daily_risk(acc["total_equity"])
            if not ok:
                global _v6_daily_risk_alerted, _v6_daily_risk_last_notice
                now_ts=time.time()
                if (not _v6_daily_risk_alerted) or (now_ts-_v6_daily_risk_last_notice >= V6_DAILY_RISK_REMINDER_MIN*60):
                    tg(f"🛑 {reason}\nNew entries paused. Existing positions remain protected.\nNext reminder in {V6_DAILY_RISK_REMINDER_MIN} min.")
                    _v6_daily_risk_alerted=True; _v6_daily_risk_last_notice=now_ts
                time.sleep(120);continue
            elif _v6_daily_risk_alerted:
                tg("🟢 DAILY RISK STOP CLEARED\nNew entries are allowed again.")
                _v6_daily_risk_alerted=False; _v6_daily_risk_last_notice=0.0
            if time.time()<loss_pause_until:time.sleep(SCAN_SECONDS);continue
            positions=get_positions()
            if not PAPER_TRADING:protection_watch();positions=get_positions()
            if protection_lock and not PAPER_TRADING:time.sleep(SCAN_SECONDS);continue
            scanned=[];candidates=[];existing={position_symbol(p) for p in positions}
            for symbol in SYMBOLS:
                if not cooldown_ok(symbol):continue
                try:
                    s=evaluate_symbol(symbol)
                    if not s:continue
                    scanned.append(s)
                    if s.get("side") and not s.get("skip") and s.get("entry_route") not in ("WAIT",None):
                        key=int(s.get("signal_bar_ts",0))
                        if sniper_last_signal_bar.get(symbol)==key:continue
                        if symbol in existing:sniper_last_signal_bar[symbol]=key;continue
                        candidates.append(s)
                except Exception:continue
            maybe_score_report(scanned)
            candidates.sort(key=lambda x:(safe_float(x.get("quality")),int(x.get("confirmations",0)),safe_float(x.get("h4_score"))),reverse=True)
            attempts=0
            for s in candidates:
                if len(get_positions())>=MAX_ACTIVE_POSITIONS or attempts>=MAX_EXECUTION_ATTEMPTS_PER_SCAN:break
                allowed,why=sniper_portfolio_allows(s,get_positions())
                if not allowed:
                    sniper_last_signal_bar[s["symbol"]]=int(s.get("signal_bar_ts",0));tg(f"⏭️ {s['symbol']} {s['side'].upper()} LDT PORTFOLIO\n{why}");continue
                attempts+=1;opened=open_trade(s);sniper_last_signal_bar[s["symbol"]]=int(s.get("signal_bar_ts",0))
                if opened:symbol_last_trade[s["symbol"]]=time.time();break
            time.sleep(SCAN_SECONDS)
        except KeyboardInterrupt:tg("🛑 Precision Structure Fusion V6 stopped.");raise
        except Exception as e:
            msg=f"{type(e).__name__}: {e}"
            if "152002" in msg:time.sleep(20);continue
            tg(f"⚠️ PRECISION STRUCTURE FUSION V6 LOOP ERROR\n{msg}");time.sleep(30)



# ============================================================
# V6.3 — 4H EBP -> 15M FVG PRECISION RETRACE ENGINE
# ============================================================
# Added from the supplied strategy video. The active engine still uses ONLY
# 4H + 15M chart data. Live ticker price is used solely to execute the entry.
#
# Bullish EBP (4H):
#   - candle sweeps below the prior 4H low
#   - candle closes ABOVE the prior 4H open
# Bearish EBP is the exact mirror.
# Then, inside that completed 4H EBP candle, locate a directional 15M FVG.
# We DO NOT chase the impulse. The setup becomes actionable only when live
# price retraces into the FVG. Stop goes beyond the 15M impulse extreme and
# target is 2R, matching the video.
#
# Innovation vs blindly placing exchange limit orders:
#   the bot keeps the FVG setup "armed" and only submits the protected market
#   order when price is actually inside the FVG. This avoids stale limit orders
#   filling hours later after the 4H thesis has already changed.

SNIPER_VERSION = "6.3.0"
V63_EBP_ENABLED = os.getenv("V63_EBP_ENABLED", "true").lower() == "true"
V63_EBP_MAX_AGE_HOURS = float(os.getenv("V63_EBP_MAX_AGE_HOURS", "7.9"))
V63_EBP_MIN_SWEEP_ATR = float(os.getenv("V63_EBP_MIN_SWEEP_ATR", "0.03"))
V63_EBP_MIN_FVG_ATR = float(os.getenv("V63_EBP_MIN_FVG_ATR", "0.06"))
V63_EBP_ENTRY_FRACTION = float(os.getenv("V63_EBP_ENTRY_FRACTION", "0.50"))  # midpoint reference
V63_EBP_TOUCH_TOL_ATR = float(os.getenv("V63_EBP_TOUCH_TOL_ATR", "0.04"))
V63_EBP_STOP_BUFFER_ATR = float(os.getenv("V63_EBP_STOP_BUFFER_ATR", "0.12"))
V63_EBP_MIN_STOP_ATR = float(os.getenv("V63_EBP_MIN_STOP_ATR", "0.45"))
V63_EBP_MAX_STOP_ATR = float(os.getenv("V63_EBP_MAX_STOP_ATR", "1.35"))
V63_EBP_FINAL_R = float(os.getenv("V63_EBP_FINAL_R", "2.0"))
V63_EBP_RISK_PCT = float(os.getenv("V63_EBP_RISK_PCT", "0.017"))
V63_EBP_ELITE_RISK_PCT = float(os.getenv("V63_EBP_ELITE_RISK_PCT", "0.020"))
V63_EBP_REQUIRE_BTC = os.getenv("V63_EBP_REQUIRE_BTC", "true").lower() == "true"
V63_EBP_MIN_QUALITY = float(os.getenv("V63_EBP_MIN_QUALITY", "82"))
V63_EBP_RECLAIM_EMA21 = os.getenv("V63_EBP_RECLAIM_EMA21", "false").lower() == "true"


def _v63_h4_ebp(symbol):
    """Return the newest still-valid completed 4H EBP candle, or None."""
    try:
        cdf = _v6_closed(_v6_df(symbol, "4h"))
        if len(cdf) < 205:
            return None
        # The newest completed EBP is preferred. Looking back two completed H4
        # bars lets the next H4 candle retrace into the 15M FVG as in the video.
        for off in (1, 2):
            i = len(cdf) - off
            if i <= 0:
                continue
            a, p = cdf.iloc[i], cdf.iloc[i-1]
            atr = max(safe_float(a["atr"]), 1e-12)
            o, h, l, c = map(safe_float, (a["open"], a["high"], a["low"], a["close"]))
            po, ph, pl = map(safe_float, (p["open"], p["high"], p["low"]))
            bull_sweep = l < pl - V63_EBP_MIN_SWEEP_ATR * atr
            bear_sweep = h > ph + V63_EBP_MIN_SWEEP_ATR * atr
            bull = bull_sweep and c > po
            bear = bear_sweep and c < po
            if not (bull or bear):
                continue
            ts = int(a["ts"])
            age_h = max(0.0, (time.time()*1000 - (ts + 4*60*60*1000)) / 3600000.0)
            if age_h > V63_EBP_MAX_AGE_HOURS:
                continue
            d = 1 if bull else -1
            # Quality of the displacement/reclaim itself.
            body = abs(c-o)/atr
            reclaim = (c-po)/atr if d == 1 else (po-c)/atr
            sweep = (pl-l)/atr if d == 1 else (h-ph)/atr
            return {"dir":d,"ts":ts,"end_ts":ts+4*60*60*1000,"open":o,"high":h,"low":l,"close":c,
                    "atr":atr,"body_atr":body,"reclaim_atr":reclaim,"sweep_atr":sweep,"age_h":age_h}
    except Exception:
        return None
    return None


def _v63_fvg_inside_ebp(symbol, ebp):
    """Find the best directional 15M FVG formed inside the EBP's 4H window."""
    try:
        cdf = _v6_closed(_v6_df(symbol, "15m"))
        if len(cdf) < 30:
            return None
        inside = cdf[(cdf["ts"] >= ebp["ts"]) & (cdf["ts"] < ebp["end_ts"])].copy()
        if len(inside) < 3:
            return None
        d = ebp["dir"]
        candidates=[]
        for i in range(2, len(inside)):
            left, mid, right = inside.iloc[i-2], inside.iloc[i-1], inside.iloc[i]
            atr15=max(safe_float(right["atr"]),1e-12)
            if d == 1:
                lo=safe_float(left["high"]); hi=safe_float(right["low"])
                impulse=safe_float(mid["close"])-safe_float(mid["open"])
                valid=hi>lo and (hi-lo)>=V63_EBP_MIN_FVG_ATR*atr15 and impulse>0
            else:
                lo=safe_float(right["high"]); hi=safe_float(left["low"])
                impulse=safe_float(mid["open"])-safe_float(mid["close"])
                valid=hi>lo and (hi-lo)>=V63_EBP_MIN_FVG_ATR*atr15 and impulse>0
            if not valid:
                continue
            width=(hi-lo)/atr15
            displacement=impulse/atr15
            # Prefer a clean gap created later in the EBP displacement, with
            # meaningful width and a strong middle candle.
            score=width*1.5 + displacement + i/len(inside)*0.35
            candidates.append((score,lo,hi,atr15,int(right["ts"]),i))
        if not candidates:
            return None
        _,lo,hi,atr15,fts,idx=max(candidates,key=lambda x:x[0])
        entry=lo+(hi-lo)*V63_EBP_ENTRY_FRACTION
        # Structural stop uses the 15M impulse swing INSIDE the H4 EBP candle.
        upto=inside.iloc[:idx+1]
        if d==1:
            extreme=safe_float(upto["low"].min())
            raw_stop=extreme - V63_EBP_STOP_BUFFER_ATR*atr15
            risk=entry-raw_stop
        else:
            extreme=safe_float(upto["high"].max())
            raw_stop=extreme + V63_EBP_STOP_BUFFER_ATR*atr15
            risk=raw_stop-entry
        risk=max(V63_EBP_MIN_STOP_ATR*atr15,min(risk,V63_EBP_MAX_STOP_ATR*atr15))
        stop=entry-d*risk
        return {"lo":lo,"hi":hi,"entry":entry,"atr":atr15,"ts":fts,"risk":risk,"stop":stop,"extreme":extreme}
    except Exception:
        return None


def _v63_ebp_candidate(symbol):
    if not V63_EBP_ENABLED:
        return None
    ebp=_v63_h4_ebp(symbol)
    if not ebp:
        return None
    d=ebp["dir"]; side="long" if d==1 else "short"
    fvg=_v63_fvg_inside_ebp(symbol,ebp)
    if not fvg:
        return {"symbol":symbol,"side":side,"score":0,"quality":0,"grade":"-","skip":"EBP-no-15M-FVG",
                "entry_route":"WAIT","strategy":"EBP-FVG","price":ebp["close"],"atr":ebp["atr"],"signal_bar_ts":ebp["ts"],
                "confirmations":1,"h4_score":"EBP"}
    try:
        c15=_v6_closed(_v6_df(symbol,"15m")); a=c15.iloc[-1]
        atr15=max(safe_float(a["atr"]),1e-12); close15=safe_float(a["close"]); ema21=safe_float(a["ema_slow"])
        live=safe_float(exchange.fetch_ticker(symbol).get("last"),close15)
    except Exception:
        return None
    tol=V63_EBP_TOUCH_TOL_ATR*atr15
    # The strategy is explicitly a retracement entry. Above the bullish FVG or
    # below the bearish FVG = ARMED/WAIT. Through the far side = invalidated.
    if d==1:
        invalid = live < fvg["lo"]-tol
        touched = fvg["lo"]-tol <= live <= fvg["hi"]+tol
        chase = live > fvg["hi"]+tol
    else:
        invalid = live > fvg["hi"]+tol
        touched = fvg["lo"]-tol <= live <= fvg["hi"]+tol
        chase = live < fvg["lo"]-tol
    btc_ok,btc_note=_v6_btc_gate(symbol,d) if V63_EBP_REQUIRE_BTC else (True,"BTC gate off")
    ema_ok=(close15>=ema21) if d==1 else (close15<=ema21)
    # EMA21 is a bonus for EBP, not mandatory by default: the video entry is
    # defined by H4 sweep/reclaim + M15 FVG retrace, not an EMA cross.
    q=55.0
    q += min(12.0, ebp["sweep_atr"]*20.0)
    q += min(10.0, max(0.0,ebp["reclaim_atr"])*20.0)
    q += min(8.0, max(0.0,ebp["body_atr"])*8.0)
    q += 7.0 if ema_ok else 0.0
    q += 8.0 if btc_ok else 0.0
    q=min(100.0,q)
    skip=None
    if invalid: skip="EBP-FVG-invalidated"
    elif not btc_ok: skip=btc_note
    elif V63_EBP_RECLAIM_EMA21 and not ema_ok: skip="EBP-15M-EMA21-not-reclaimed"
    elif q<V63_EBP_MIN_QUALITY: skip=f"EBP-quality-{q:.0f}<{V63_EBP_MIN_QUALITY:.0f}"
    elif not touched: skip="EBP-FVG-ARMED-wait-retrace" if chase else "EBP-FVG-wait-touch"
    # This route deliberately does NOT use the V6.2 4H extension blocker: an EBP
    # is itself a 4H liquidity sweep/reclaim event. The anti-chase mechanism is
    # the FVG retracement requirement.
    return {"symbol":symbol,"side":side,"score":q,"score_max":100.0,"quality":q,"threshold":V63_EBP_MIN_QUALITY,
            "grade":"A+" if q>=92 else "A" if q>=V63_EBP_MIN_QUALITY else "C","skip":skip,
            "entry_route":"EBP-FVG" if skip is None else "WAIT","strategy":"EBP-FVG","price":fvg["entry"],"live_price":live,
            "atr":atr15,"adx":safe_float(a["sniper_adx"]),"rsi":safe_float(a["sniper_rsi"],50),"signal_bar_ts":int(ebp["ts"]),
            "risk_distance":fvg["risk"],"stop_origin":"V6.3 EBP 4H sweep + 15M FVG structure","final_r":V63_EBP_FINAL_R,
            "h4_score":"EBP","h4_adx":0.0,"confirmations":3+int(ema_ok)+int(btc_ok),"role":False,"sweep":True,"fvg":True,"hns":False,
            "candle":"4H-EBP","macd_ok":True,"volume_above":False,"extension_atr":0.0,"btc_note":btc_note,"ema21_reset":True,
            "location_note":"EBP retrace location","h4_ext_atr":0.0,"h4_range_pos":0.5,"h4_impulse3_atr":0.0,
            "zone_level":fvg["entry"],"fvg_zone":(fvg["lo"],fvg["hi"]),"sweep_depth":ebp["sweep_atr"],
            "ebp_age_h":ebp["age_h"],"ebp_sweep_atr":ebp["sweep_atr"],"ebp_reclaim_atr":ebp["reclaim_atr"]}


# Keep the existing Structure-Fusion engine and ADD EBP-FVG as a second route.
_v62_structure_candidate = _v6_candidate

def _v6_candidate(symbol):
    ebp=_v63_ebp_candidate(symbol)
    base=_v62_structure_candidate(symbol)
    # A live EBP retracement has priority because it has a precise pre-defined
    # location. Otherwise a normal V6.2 Structure Fusion setup can still trade.
    if ebp and ebp.get("side") and not ebp.get("skip"):
        return ebp
    if base and base.get("side") and not base.get("skip"):
        return base
    # For reports, show the more actionable EBP ARMED state when present.
    if ebp and str(ebp.get("skip","")).startswith("EBP-FVG-ARMED"):
        return ebp
    return base if base is not None else ebp


def evaluate_symbol(symbol, btc=None):
    return _v6_candidate(symbol)


def sizing_profile(signal):
    if signal.get("strategy")=="EBP-FVG":
        elite=safe_float(signal.get("score"))>=92
        return {"name":"V63-EBP-ELITE" if elite else "V63-EBP","risk_pct":V63_EBP_ELITE_RISK_PCT if elite else V63_EBP_RISK_PCT,
                "target_net_tp":V6_TARGET_NET}
    elite=safe_float(signal.get("score"))>=V6_ELITE_QUALITY and int(signal.get("confirmations",0))>=4
    return {"name":"V6-ELITE" if elite else "V6-STRUCTURE","risk_pct":V6_ELITE_RISK_PCT if elite else V6_RISK_PCT,"target_net_tp":V6_TARGET_NET}


def _v6_levels(signal,live,tick):
    side=signal["side"];d=1 if side=="long" else -1;risk=safe_float(signal.get("risk_distance"))
    final_r=safe_float(signal.get("final_r"),V6_FINAL_R) or V6_FINAL_R
    if live<=0 or risk<=0:return None
    sl=round_trade_price(live-d*risk,tick,side,"sl")
    t1r=0.75 if signal.get("strategy")=="EBP-FVG" else V6_TP1_R
    t2r=1.25 if signal.get("strategy")=="EBP-FVG" else V6_TP2_R
    t1=round_trade_price(live+d*risk*t1r,tick,side,"tp")
    t2=round_trade_price(live+d*risk*t2r,tick,side,"tp")
    t3=round_trade_price(live+d*risk*final_r,tick,side,"tp")
    return sl,t1,t2,t3,risk


# V6.3 execution override. Fixes the V6.2 _v62_last_entry_time scope bug and
# uses each strategy's own final-R in sizing/protection.
def open_trade(signal):
    global protection_lock, _v62_last_entry_time
    symbol,side=signal["symbol"],signal["side"]
    acc=futures_account();equity,available=acc["total_equity"],acc["available"];reset_daily_if_needed(equity)
    if equity<MIN_EQUITY_USDT or available<=0 or (not PAPER_TRADING and protection_lock):return False
    if time.time()-_v62_last_entry_time < V62_GLOBAL_ENTRY_GAP_MIN*60:return False
    tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
    live=safe_float(exchange.fetch_ticker(symbol).get("last"),signal["price"]);sig=safe_float(signal["price"])
    if live<=0 or sig<=0:return False
    d=1 if side=="long" else -1
    is_ebp=signal.get("strategy")=="EBP-FVG"
    if is_ebp:
        z=signal.get("fvg_zone") or (0,0);zlo,zhi=safe_float(z[0]),safe_float(z[1]);tol=V63_EBP_TOUCH_TOL_ATR*safe_float(signal.get("atr"))
        if not (zlo-tol <= live <= zhi+tol):
            return False
        adverse=0.0  # entry is intentionally on the retrace into the zone
    else:
        adverse=d*((live-sig)/sig)
        if adverse>V62_MAX_ADVERSE_DRIFT:
            tg(f"⏭️ {symbol} {side.upper()} V6.3 TOO LATE\nAdverse drift {adverse*100:.2f}% > {V62_MAX_ADVERSE_DRIFT*100:.2f}%");return False
        live_loc_ok,live_loc_note,_=_v62_live_location_ok(symbol,side,live)
        if not live_loc_ok:
            tg(f"⏭️ {symbol} {side.upper()} V6.3 LOCATION BLOCK\n{live_loc_note}\nNo chase at 4H extreme.");return False
    levels=_v6_levels(signal,live,tick)
    if not levels:return False
    sl,t1,t2,t3,risk=levels;profile=sizing_profile(signal);final_r=safe_float(signal.get("final_r"),V6_FINAL_R) or V6_FINAL_R
    sizing=calculate_contracts_v5(symbol,live,sl,equity,available,profile["risk_pct"],profile["target_net_tp"],final_r)
    if not sizing or sizing["margin"]>available*AVAILABLE_MARGIN_BUFFER:return False
    before_ids={position_id(p) for p in get_positions() if position_id(p)}
    oklev,levres=set_leverage(symbol,LEVERAGE)
    if not oklev:tg(f"⛔ LEVERAGE FAILED\n{symbol}\n{levres}");return False
    tag="ps63e" if is_ebp else "ps63s"
    payload={"instId":blofin_inst_id(symbol),"marginMode":MARGIN_MODE,"positionSide":"net","side":"buy" if side=="long" else "sell","orderType":"market","size":str(sizing["contracts"]),
             "tpTriggerPrice":str(t3),"tpOrderPrice":"-1","tpTriggerPriceType":"mark","slTriggerPrice":str(sl),"slOrderPrice":"-1","slTriggerPriceType":"mark",
             "clientOrderId":f"{tag}{int(time.time()*1000)%1000000000}"}
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    if is_ebp:
        zlo,zhi=signal.get("fvg_zone",(0,0))
        details=(f"4H EBP sweep {signal.get('ebp_sweep_atr',0):.2f}ATR · reclaim {signal.get('ebp_reclaim_atr',0):.2f}ATR · age {signal.get('ebp_age_h',0):.1f}h\n"
                 f"15M FVG {zlo:.8g} → {zhi:.8g} · RETRACE TOUCHED\n"
                 f"Entry {live:.8g} · SL {sl:.8g}\nTP-A {t1:.8g} (0.75R) · TP-B {t2:.8g} (1.25R) · FINAL {t3:.8g} ({final_r:.1f}R)")
    else:
        flags=[]
        for k,label in (("role","ROLE"),("sweep","SWEEP"),("fvg","FVG"),("hns","H&S")):
            if signal.get(k):flags.append(label)
        details=(f"4H score {signal.get('h4_score',0)}/6 · ADX {signal.get('h4_adx',0):.1f} · EMA21 ext {signal.get('h4_ext_atr',0):.2f}ATR\n"
                 f"15M confirmations {signal.get('confirmations',0)} · {' + '.join(flags) if flags else '-'}\n"
                 f"Entry {live:.8g} · SL {sl:.8g} · TP1 {t1:.8g} · TP2 {t2:.8g} · FINAL {t3:.8g} ({final_r:.1f}R)")
    msg=(f"{'🧪 PAPER' if PAPER_TRADING else '🎯 V6.6 ENTRY'}\n{symbol} {side.upper()} · {signal.get('entry_route')} · {signal.get('grade')} {signal.get('score',0):.0f}/100\n"
         f"{details}\nBTC {signal.get('btc_note','-')}\nRisk cap {profile['risk_pct']*100:.1f}% · expected final net ~${sizing['expected_net_tp_usdt']:.2f}\n"
         f"Notional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x")
    if PAPER_TRADING:tg(msg);_v62_last_entry_time=time.time();return True
    resp=native("private_post_trade_order",payload)
    if not native_ok(resp):
        err=response_error(resp)
        if "152012" in err:protection_lock=True
        tg(f"❌ V6.4 ORDER FAILED\n{symbol} {side.upper()}\n{err}");return False
    _v62_last_entry_time=time.time();tg(msg);new_pos=find_new_position(symbol,before_ids,timeout=8)
    if new_pos is None:tg(f"⚠️ {symbol}: V6.3 order accepted, position not visible yet.");return True
    pid=position_id(new_pos)
    if pid:
        sniper_managed_positions.add(pid);_v6_position_meta[pid]={"risk":risk,"tp1":t1,"tp2":t2,"opened":time.time(),"route":signal.get("entry_route"),"side":side,"final_r":final_r,"strategy":signal.get("strategy")}
    if has_live_protection(new_pos)[0]:tg(f"🛡️ V6.6 PROTECTION CONFIRMED\n{symbol} {side.upper()}\n{final_r:.1f}R TP + structural SL visible.");return True
    last=safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side,sl,t3,last):
        ok,res=place_tpsl_for_position(new_pos,sl,t3,client_tag=tag)
        if ok:tg(f"🛡️ V6.6 FALLBACK PROTECTION CONFIRMED\n{symbol}");return True
        protection_lock=True;tg(f"🚨 V6.6 PROTECTION FAILURE\n{symbol}\n{res}\nNEW TRADES PAUSED.");return True
    protection_lock=True;tg(f"🚨 {symbol}: V6.3 position exists but safe fallback protection failed.\nNEW TRADES PAUSED.");return True


def maybe_score_report(rows):
    global _v6_last_report
    now=time.time();interesting=[r for r in rows if r and (r.get("side") or r.get("skip"))]
    if not interesting or now-_v6_last_report<V6_REPORT_MIN*60:return
    _v6_last_report=now
    ranked=sorted(interesting,key=lambda r:safe_float(r.get("quality",r.get("score",0))),reverse=True)[:10]
    lines=[f"🧠 STRUCTURE FUSION v{SNIPER_VERSION} · 4H + 15M ONLY"]
    for r in ranked:
        state="🟢 READY" if r.get("side") and not r.get("skip") else f"⏭ {r.get('skip','wait')}"
        if r.get("strategy")=="EBP-FVG":
            z=r.get("fvg_zone") or (0,0)
            lines.append(f"{r['symbol'].split('/')[0]} {str(r.get('side') or '-').upper()} EBP-FVG Q{safe_float(r.get('score')):.0f} zone {safe_float(z[0]):.5g}-{safe_float(z[1]):.5g} {state}")
        else:
            lines.append(f"{r['symbol'].split('/')[0]} {str(r.get('side') or '-').upper()} {r.get('entry_route','-')} Q{safe_float(r.get('score')):.0f} C{r.get('confirmations','-')} {state}")
    tg("\n".join(lines))


def startup_diagnostics():
    acc=futures_account();reset_daily_if_needed(acc["total_equity"])
    tg(f"🧠 PRECISION STRUCTURE FUSION STARTED v{SNIPER_VERSION}\nMode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
       f"TIMEFRAMES: 4H + 15M ONLY — no 1H, no 5M\n"
       f"NEW: 4H EBP sweep/reclaim → 15M FVG → WAIT FOR RETRACE → protected entry\n"
       f"EBP rule LONG: sweep prior 4H low + close above prior 4H open; SHORT mirrored\n"
       f"EBP target: {V63_EBP_FINAL_R:.1f}R · no chasing: price must return into the 15M FVG\n"
       f"Existing engine: 4H location + 15M role reversal / sweep / FVG / H&S\n"
       f"Risk caps: structure {V6_RISK_PCT*100:.1f}% · EBP {V63_EBP_RISK_PCT*100:.1f}% · elite {max(V6_ELITE_RISK_PCT,V63_EBP_ELITE_RISK_PCT)*100:.1f}%\n"
       f"Max positions {MAX_ACTIVE_POSITIONS} · max same side {V6_MAX_SAME_SIDE} · entry spacing {V62_GLOBAL_ENTRY_GAP_MIN}m\n"
       f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}")


# ============================================================
# V6.4 — ADAPTIVE RETRACE ROUTER
# ============================================================
# Overnight V6.3 diagnostics showed a healthy anti-chase gate, but too many
# symbols died in WAIT because the normal Structure route required role/sweep/
# H&S while the EBP route is intentionally rare. V6.4 does NOT loosen the
# anti-chase rules. It adds a third, location-first continuation route:
#
#   strong 4H trend -> 15M reset into EMA21 -> 15M reclaim -> momentum returns
#   -> at least one price-action trigger (micro break / FVG touch / rejection)
#
# This creates entries AFTER a pullback instead of buying/selling an extended
# impulse. It uses only 4H + 15M market structure.

SNIPER_VERSION = "6.5.0"
# V6.5 SIZING BOOST:
# - leverage stays 10x
# - structural stops and entry filters are unchanged
# - risk budget is increased ~40-50%, with stronger A+ setups receiving more
# - the existing BASE_MARGIN_USDT=10 ceiling remains the hard per-position target cap
# - risk-based sizing can still choose less than $10 margin when the structural stop is wider
V64_TREND_RESET_ENABLED = os.getenv("V64_TREND_RESET_ENABLED", "true").lower() == "true"
V64_MIN_H4_SCORE = int(os.getenv("V64_MIN_H4_SCORE", "5"))
V64_MIN_H4_ADX = float(os.getenv("V64_MIN_H4_ADX", "17"))
V64_MIN_QUALITY = float(os.getenv("V64_MIN_QUALITY", "82"))
V64_ELITE_QUALITY = float(os.getenv("V64_ELITE_QUALITY", "92"))
V64_MAX_15M_EXT_ATR = float(os.getenv("V64_MAX_15M_EXT_ATR", "0.30"))
V64_MAX_4H_EXT_ATR = float(os.getenv("V64_MAX_4H_EXT_ATR", "0.90"))
V64_RECLAIM_TOL_ATR = float(os.getenv("V64_RECLAIM_TOL_ATR", "0.16"))
V64_RESET_LOOKBACK = int(os.getenv("V64_RESET_LOOKBACK", "8"))
V64_MIN_STOP_ATR = float(os.getenv("V64_MIN_STOP_ATR", "0.55"))
V64_MAX_STOP_ATR = float(os.getenv("V64_MAX_STOP_ATR", "1.25"))
V64_STOP_BUFFER_ATR = float(os.getenv("V64_STOP_BUFFER_ATR", "0.10"))
V64_FINAL_R = float(os.getenv("V64_FINAL_R", "2.5"))
V64_RISK_PCT = float(os.getenv("V64_RISK_PCT", "0.014"))
V64_ELITE_RISK_PCT = float(os.getenv("V64_ELITE_RISK_PCT", "0.017"))
V64_REPORT_MIN = int(os.getenv("V64_REPORT_MIN", "30"))
V64_REQUIRE_BTC = os.getenv("V64_REQUIRE_BTC", "true").lower() == "true"


def _v64_recent_reset(cdf, d, atr):
    n=max(3,V64_RESET_LOOKBACK)
    recent=cdf.iloc[-(n+1):-1] if len(cdf)>n+1 else cdf.iloc[:-1]
    if recent.empty:return False
    tol=V64_RECLAIM_TOL_ATR*atr
    if d==1:
        return bool((recent["low"] <= recent["ema_slow"] + tol).any())
    return bool((recent["high"] >= recent["ema_slow"] - tol).any())


def _v64_trend_reset_candidate(symbol):
    if not V64_TREND_RESET_ENABLED:return None
    try:
        h4=_v6_4h_context(symbol);d=int(h4.get("dir",0))
        if d==0:return None
        side="long" if d==1 else "short"
        df=_v6_df(symbol,"15m");cdf=_v6_closed(df)
        if len(cdf)<210:return None
        a,p=cdf.iloc[-1],cdf.iloc[-2]
        c=safe_float(a["close"]);o=safe_float(a["open"]);hi=safe_float(a["high"]);lo=safe_float(a["low"])
        pc=safe_float(p["close"]);ph=safe_float(p["high"]);pl=safe_float(p["low"])
        atr=max(safe_float(a["atr"]),1e-12);e21=safe_float(a["ema_slow"]);pe21=safe_float(p["ema_slow"])
        if c<=0 or e21<=0:return None

        loc_ok,loc_note,loc=_v62_4h_location(h4,d)
        h4_ext=safe_float(loc.get("h4_ext_atr"),99)
        h4_strong=(int(h4.get("score",0))>=V64_MIN_H4_SCORE and safe_float(h4.get("adx"))>=V64_MIN_H4_ADX)
        reset=_v64_recent_reset(cdf,d,atr)
        ext=d*(c-e21)/atr
        ema_slope=d*(e21-pe21)>0
        correct_side=(c>e21) if d==1 else (c<e21)
        touched_now=(lo<=e21+V64_RECLAIM_TOL_ATR*atr) if d==1 else (hi>=e21-V64_RECLAIM_TOL_ATR*atr)
        crossed_now=(pc<=pe21 and c>e21) if d==1 else (pc>=pe21 and c<e21)
        reclaim=correct_side and (touched_now or crossed_now)
        micro_break=(c>ph+0.02*atr) if d==1 else (c<pl-0.02*atr)
        candle,candle_name,_=_v6_candle_pattern(cdf,d)
        fvg,flo,fhi=_v6_fvg(cdf,d,atr)
        hist=safe_float(a["macd_hist"]);hp=safe_float(p["macd_hist"])
        macd_ok=(hist>hp) if d==1 else (hist<hp)
        volm=safe_float(a["vol_mean"]);vol=safe_float(a["volume"]);vol_ok=volm>0 and vol>=0.95*volm
        body=abs(c-o)/atr
        body_ok=body>=0.18
        btc_ok,btc_note=_v6_btc_gate(symbol,d) if V64_REQUIRE_BTC else (True,"BTC gate off")
        pa_trigger=micro_break or candle or fvg

        q=0.0
        q+=26 if int(h4.get("score",0))>=6 else 22 if int(h4.get("score",0))>=5 else 0
        q+=8 if safe_float(h4.get("adx"))>=22 else 5 if safe_float(h4.get("adx"))>=V64_MIN_H4_ADX else 0
        q+=12 if loc_ok and h4_ext<=V64_MAX_4H_EXT_ATR else 0
        q+=14 if reset else 0
        q+=17 if reclaim else 0
        q+=8 if ema_slope else 0
        q+=10 if macd_ok else 0
        q+=8 if micro_break else 0
        q+=7 if candle else 0
        q+=6 if fvg else 0
        q+=4 if vol_ok else 0
        q+=3 if body_ok else 0
        q=min(100.0,q)

        skip=None
        if not h4_strong:skip=f"TR-4H-not-strong-{h4.get('score',0)}/6-ADX{safe_float(h4.get('adx')):.0f}"
        elif not loc_ok:skip=f"TR-ARMED-{loc_note}"
        elif h4_ext>V64_MAX_4H_EXT_ATR:skip=f"TR-ARMED-4H-ext-{h4_ext:.2f}ATR"
        elif not reset:skip="TR-wait-15M-EMA21-reset"
        elif not correct_side:skip="TR-wait-EMA21-reclaim"
        elif ext>V64_MAX_15M_EXT_ATR:skip=f"TR-too-far-after-reclaim-{ext:.2f}ATR"
        elif not reclaim:skip="TR-wait-reclaim-candle"
        elif not ema_slope:skip="TR-EMA21-slope-not-ready"
        elif not macd_ok:skip="TR-momentum-not-returned"
        elif not pa_trigger:skip="TR-wait-price-action-trigger"
        elif not body_ok:skip="TR-reclaim-body-too-small"
        elif not btc_ok:skip=btc_note
        elif q<V64_MIN_QUALITY:skip=f"TR-quality-{q:.0f}<{V64_MIN_QUALITY:.0f}"

        recent=cdf.iloc[-7:]
        if d==1:
            structural=safe_float(recent["low"].min())-V64_STOP_BUFFER_ATR*atr
            raw=c-structural
        else:
            structural=safe_float(recent["high"].max())+V64_STOP_BUFFER_ATR*atr
            raw=structural-c
        risk=max(V64_MIN_STOP_ATR*atr,min(raw,V64_MAX_STOP_ATR*atr))
        flags=[]
        if micro_break:flags.append("MICRO-BREAK")
        if fvg:flags.append("FVG")
        if candle:flags.append(candle_name.upper())
        return {"symbol":symbol,"side":side,"score":q,"score_max":100.0,"quality":q,"threshold":V64_MIN_QUALITY,
                "grade":"A+" if q>=V64_ELITE_QUALITY else "A" if q>=V64_MIN_QUALITY else "C","skip":skip,
                "entry_route":"TREND-RESET" if skip is None else "WAIT","strategy":"TREND-RESET","price":c,"atr":atr,
                "signal_bar_ts":int(a["ts"]),"risk_distance":risk,"stop_origin":"V6.4 15M EMA21 reset swing","final_r":V64_FINAL_R,
                "h4_score":h4.get("score",0),"h4_adx":h4.get("adx",0),"confirmations":int(reset)+int(reclaim)+int(macd_ok)+int(pa_trigger)+int(vol_ok),
                "role":False,"sweep":False,"fvg":fvg,"hns":False,"candle":candle_name if candle else "-","macd_ok":macd_ok,
                "volume_above":vol_ok,"extension_atr":ext,"btc_note":btc_note,"ema21_reset":reset,"location_note":loc_note,
                "h4_ext_atr":h4_ext,"h4_range_pos":safe_float(loc.get("h4_range_pos")),"h4_impulse3_atr":safe_float(loc.get("h4_impulse3_atr")),
                "zone_level":e21,"fvg_zone":(flo,fhi) if fvg else None,"sweep_depth":0.0,"v64_flags":flags}
    except Exception:
        return None


# Preserve V6.3 router, then add the Trend-Reset route without weakening EBP or
# Structure Fusion.
_v63_router_candidate = _v6_candidate

def _v6_candidate(symbol):
    # Evaluate each route independently so a rare EBP and a normal structure
    # setup remain available while Trend-Reset fills the clean pullback gap.
    ebp=_v63_ebp_candidate(symbol)
    base=_v62_structure_candidate(symbol)
    tr=_v64_trend_reset_candidate(symbol)
    ready=[x for x in (ebp,tr,base) if x and x.get("side") and not x.get("skip") and x.get("entry_route") not in (None,"WAIT")]
    if ready:
        # EBP gets a small priority bonus because its retrace zone is defined in
        # advance; otherwise choose quality rather than route name.
        return max(ready,key=lambda x:(safe_float(x.get("quality"))+ (3 if x.get("strategy")=="EBP-FVG" else 0),int(x.get("confirmations",0))))
    # For WAIT reports prefer actionable armed/retrace states over generic blocks.
    waits=[x for x in (ebp,tr,base) if x]
    def wait_rank(x):
        sk=str(x.get("skip",""))
        action=3 if ("ARMED" in sk or "wait-retrace" in sk) else 2 if sk.startswith("TR-wait") else 1
        return (action,safe_float(x.get("quality",x.get("score",0))))
    return max(waits,key=wait_rank) if waits else None


def evaluate_symbol(symbol, btc=None):
    return _v6_candidate(symbol)


def sizing_profile(signal):
    if signal.get("strategy")=="EBP-FVG":
        elite=safe_float(signal.get("score"))>=92
        return {"name":"V64-EBP-ELITE" if elite else "V64-EBP","risk_pct":V63_EBP_ELITE_RISK_PCT if elite else V63_EBP_RISK_PCT,"target_net_tp":V6_TARGET_NET}
    if signal.get("strategy")=="TREND-RESET":
        elite=safe_float(signal.get("score"))>=V64_ELITE_QUALITY
        return {"name":"V64-TR-ELITE" if elite else "V64-TREND-RESET","risk_pct":V64_ELITE_RISK_PCT if elite else V64_RISK_PCT,"target_net_tp":V6_TARGET_NET}
    elite=safe_float(signal.get("score"))>=V6_ELITE_QUALITY and int(signal.get("confirmations",0))>=4
    return {"name":"V64-STRUCT-ELITE" if elite else "V64-STRUCTURE","risk_pct":V6_ELITE_RISK_PCT if elite else V6_RISK_PCT,"target_net_tp":V6_TARGET_NET}


def maybe_score_report(rows):
    global _v6_last_report
    from collections import Counter
    now=time.time();interesting=[r for r in rows if r]
    if not interesting or now-_v6_last_report<V64_REPORT_MIN*60:return
    _v6_last_report=now
    ready=[r for r in interesting if r.get("side") and not r.get("skip") and r.get("entry_route") not in (None,"WAIT")]
    armed=[r for r in interesting if "ARMED" in str(r.get("skip","")) or str(r.get("skip","")).startswith("TR-wait")]
    blockers=Counter()
    for r in interesting:
        sk=str(r.get("skip") or "READY")
        if "too-far-from-EMA21" in sk or "4H-ext" in sk or "range-edge" in sk:bucket="4H extended"
        elif "no-key-structure" in sk:bucket="no key structure"
        elif "EMA21" in sk:bucket="15M EMA21/reset"
        elif "momentum" in sk:bucket="momentum"
        elif "confirmations" in sk:bucket="confirmations"
        elif "BTC" in sk:bucket="BTC context"
        elif sk=="READY":bucket="ready"
        else:bucket=sk.split("-")[0][:22]
        blockers[bucket]+=1
    ranked=sorted(interesting,key=lambda r:(0 if r.get("skip") else 1,safe_float(r.get("quality",r.get("score",0)))),reverse=True)[:8]
    lines=[f"🧠 STRUCTURE FUSION v{SNIPER_VERSION} · 4H + 15M",f"Ready {len(ready)} · Armed/retrace {len(armed)} · scanned {len(interesting)}"]
    if blockers:lines.append("Main waits: "+" · ".join(f"{k} {v}" for k,v in blockers.most_common(4)))
    for r in ranked:
        state="🟢 READY" if not r.get("skip") else f"⏭ {r.get('skip')}"
        route=r.get("strategy",r.get("entry_route","-"))
        lines.append(f"{r['symbol'].split('/')[0]} {str(r.get('side') or '-').upper()} {route} Q{safe_float(r.get('score')):.0f} {state}")
    tg("\n".join(lines))


def startup_diagnostics():
    acc=futures_account();reset_daily_if_needed(acc["total_equity"])
    tg(f"🧠 PRECISION STRUCTURE FUSION STARTED v{SNIPER_VERSION}\nMode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
       f"TIMEFRAMES: 4H + 15M ONLY — no 1H, no 5M\n"
       f"ROUTE 1 EBP-FVG: 4H sweep/reclaim → 15M FVG → retrace entry\n"
       f"ROUTE 2 TREND-RESET: strong 4H → 15M EMA21 reset/reclaim → momentum + price action\n"
       f"ROUTE 3 STRUCTURE: role reversal / sweep / FVG / H&S\n"
       f"Anti-chase stays ON: 4H location gate + 15M extension gate + live drift check\n"
       f"Trend-Reset: H4 >= {V64_MIN_H4_SCORE}/6, ADX >= {V64_MIN_H4_ADX:.0f}, 15M ext <= {V64_MAX_15M_EXT_ATR:.2f}ATR, Q >= {V64_MIN_QUALITY:.0f}\n"
       f"V6.5 SIZING BOOST · leverage stays {LEVERAGE}x · margin ceiling ${min(BASE_MARGIN_USDT, MAX_MARGIN_USDT):.2f}\n"\
       f"Risk caps: Trend-Reset {V64_RISK_PCT*100:.1f}% / elite {V64_ELITE_RISK_PCT*100:.1f}% · Structure {V6_RISK_PCT*100:.1f}% / elite {V6_ELITE_RISK_PCT*100:.1f}% · EBP {V63_EBP_RISK_PCT*100:.1f}% / elite {V63_EBP_ELITE_RISK_PCT*100:.1f}%\n"
       f"Max positions {MAX_ACTIVE_POSITIONS} · max same side {V6_MAX_SAME_SIDE} · entry spacing {V62_GLOBAL_ENTRY_GAP_MIN}m\n"
       f"Scanner summary every {V64_REPORT_MIN}m instead of Telegram walls.\n"
       f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}")


# ============================================================
# V6.6 — CONFIRM OR FLIP / MARKET-REGIME REPAIR
# ============================================================
# Why this exists:
# The 2026-09-28 live review showed three EBP-FVG shorts (ORDI, APT, IMX)
# being allowed while BTC 15M was bullish (+0/+1 in Telegram). The old BTC
# gate only blocked when BOTH BTC 4H and 15M were strongly opposite.
# EBP also entered on the first live FVG touch without waiting for a CLOSED
# 15M rejection candle. That made "A/A+" quality too optimistic.
#
# V6.6 changes:
# 1) BTC 15M opposition + non-supportive BTC 4H now blocks alt entries.
# 2) EBP-FVG needs a CLOSED 15M rejection/confirmation after the retrace.
# 3) A failed EBP can flip direction ONLY when 4H trend + 15M break + BTC all
#    confirm the opposite direction. No blind signal inversion.
# 4) Same-side cold mode is stricter after a poor intraday side record.
# 5) Risk is temporarily capped lower until the repaired entries prove edge.
# 6) BloFin instrument metadata is cached/retried so the position manager does
#    not repeatedly depend on /market/instruments every management cycle.

SNIPER_VERSION = "6.6.0"

V66_EBP_CONFIRM_LOOKBACK = int(os.getenv("V66_EBP_CONFIRM_LOOKBACK", "4"))
V66_EBP_CONFIRM_TOL_ATR = float(os.getenv("V66_EBP_CONFIRM_TOL_ATR", "0.06"))
V66_EBP_MIN_CONFIRM_SCORE = int(os.getenv("V66_EBP_MIN_CONFIRM_SCORE", "4"))
V66_FLIP_ENABLED = os.getenv("V66_FLIP_ENABLED", "true").lower() == "true"
V66_FLIP_MIN_H4_SCORE = int(os.getenv("V66_FLIP_MIN_H4_SCORE", "5"))
V66_FLIP_MIN_H4_ADX = float(os.getenv("V66_FLIP_MIN_H4_ADX", "17"))
V66_FLIP_RISK_PCT = float(os.getenv("V66_FLIP_RISK_PCT", "0.010"))
V66_MAX_EBP_RISK_PCT = float(os.getenv("V66_MAX_EBP_RISK_PCT", "0.010"))
V66_MAX_TREND_RISK_PCT = float(os.getenv("V66_MAX_TREND_RISK_PCT", "0.011"))
V66_MAX_STRUCTURE_RISK_PCT = float(os.getenv("V66_MAX_STRUCTURE_RISK_PCT", "0.012"))
V66_SIDE_COLD_MIN_TRADES = int(os.getenv("V66_SIDE_COLD_MIN_TRADES", "3"))
V66_SIDE_COLD_MAX_WR = float(os.getenv("V66_SIDE_COLD_MAX_WR", "0.25"))
V66_SIDE_COLD_MIN_LOSS = float(os.getenv("V66_SIDE_COLD_MIN_LOSS", "1.00"))
V66_INSTRUMENT_CACHE_TTL = int(os.getenv("V66_INSTRUMENT_CACHE_TTL", "21600"))

_v66_instrument_cache = {}

# Preserve the V6.5 implementations before overriding them.
_v65_get_instrument = get_instrument
_v65_ebp_candidate = _v63_ebp_candidate
_v65_sizing_profile = sizing_profile


def get_instrument(symbol):
    """Cached BloFin contract metadata with retry + stale-cache fallback."""
    now = time.time()
    cached = _v66_instrument_cache.get(symbol)
    if cached and now - cached["ts"] < V66_INSTRUMENT_CACHE_TTL:
        return cached["data"]

    last_err = None
    for delay in (0.0, 0.7, 1.5):
        if delay:
            time.sleep(delay)
        try:
            data = _v65_get_instrument(symbol)
            _v66_instrument_cache[symbol] = {"ts": now, "data": data}
            return data
        except Exception as e:
            last_err = e

    # Metadata such as tick/lot/contract value is stable enough to use a stale
    # cached copy when the public endpoint has a transient problem.
    if cached:
        return cached["data"]
    raise last_err if last_err else RuntimeError(f"Instrument lookup failed: {symbol}")


def _v66_btc_state(d):
    """Return (allowed, note, strongly_supportive) for an alt direction."""
    try:
        h4 = _v6_4h_context(V6_BTC_SYMBOL)
        b15 = _v6_closed(_v6_df(V6_BTC_SYMBOL, "15m"))
        a, p = b15.iloc[-1], b15.iloc[-2]
        c = safe_float(a["close"])
        e21 = safe_float(a["ema_slow"])
        d15 = 1 if c > e21 else -1 if c < e21 else 0
        h4d = int(h4.get("dir", 0))
        h4score = int(h4.get("score", 0))
        hist = safe_float(a["macd_hist"])
        hp = safe_float(p["macd_hist"])
        momentum15 = 1 if hist > hp else -1 if hist < hp else 0

        strong_support = (h4d == d and d15 == d)
        # Important repair: if BTC 15M is actively against the alt trade and
        # BTC 4H is not supporting that trade, do not fight the market rebound.
        if d15 == -d and h4d != d:
            return False, f"BTC REGIME BLOCK 4H/15M {h4d:+d}/{d15:+d}", False
        if h4d == -d and h4score >= 4 and d15 != d:
            return False, f"BTC 4H BLOCK 4H/15M {h4d:+d}/{d15:+d}", False

        if strong_support:
            return True, f"BTC support 4H/15M {h4d:+d}/{d15:+d}", True
        if d15 == d and momentum15 == d:
            return True, f"BTC 15M support 4H/15M {h4d:+d}/{d15:+d}", True
        return True, f"BTC neutral 4H/15M {h4d:+d}/{d15:+d}", False
    except Exception:
        # Do not pretend unavailable BTC data is positive confirmation.
        return True, "BTC context unavailable-neutral", False


def _v6_btc_gate(symbol, d):
    if not V6_BTC_CONTEXT or symbol == V6_BTC_SYMBOL:
        return True, "BTC self/off"
    ok, note, _ = _v66_btc_state(d)
    return ok, note


def _v66_ebp_closed_confirmation(symbol, ebp, fvg):
    """Require a closed 15M rejection after the post-EBP FVG retrace."""
    try:
        cdf = _v6_closed(_v6_df(symbol, "15m"))
        post = cdf[cdf["ts"] >= ebp["end_ts"]].tail(max(3, V66_EBP_CONFIRM_LOOKBACK))
        if post.empty:
            return False, "wait-first-closed-15M", 0, 0

        atr = max(safe_float(post.iloc[-1]["atr"]), 1e-12)
        tol = V66_EBP_CONFIRM_TOL_ATR * atr
        lo, hi = safe_float(fvg["lo"]), safe_float(fvg["hi"])
        mid = (lo + hi) / 2.0

        touched_positions = []
        for j in range(len(post)):
            r = post.iloc[j]
            if safe_float(r["low"]) <= hi + tol and safe_float(r["high"]) >= lo - tol:
                touched_positions.append(j)
        if not touched_positions:
            return False, "wait-retrace", 0, int(post.iloc[-1]["ts"])

        touch_j = touched_positions[-1]
        # Confirmation must happen on the touch candle or the very next candle.
        if len(post) - 1 - touch_j > 1:
            return False, "retrace-confirmation-stale", 0, int(post.iloc[-1]["ts"])

        a = post.iloc[-1]
        p = post.iloc[-2] if len(post) >= 2 else a
        d = int(ebp["dir"])
        o, c = safe_float(a["open"]), safe_float(a["close"])
        h, l = safe_float(a["high"]), safe_float(a["low"])
        pc, ph, pl = safe_float(p["close"]), safe_float(p["high"]), safe_float(p["low"])
        rng = max(h - l, 1e-12)
        upper = h - max(o, c)
        lower = min(o, c) - l
        hist, hp = safe_float(a["macd_hist"]), safe_float(p["macd_hist"])
        ema21 = safe_float(a["ema_slow"])

        if d == 1:
            body_dir = c > o
            zone_reject = c >= mid and lower / rng >= 0.18
            micro_break = c > ph + 0.02 * atr
            macd_turn = hist > hp
            ema_side = c >= ema21
            close_not_failed = c >= lo - tol
        else:
            body_dir = c < o
            zone_reject = c <= mid and upper / rng >= 0.18
            micro_break = c < pl - 0.02 * atr
            macd_turn = hist < hp
            ema_side = c <= ema21
            close_not_failed = c <= hi + tol

        score = int(body_dir) + int(zone_reject) + int(micro_break) + int(macd_turn) + int(ema_side)
        confirmed = (
            close_not_failed
            and body_dir
            and macd_turn
            and (zone_reject or micro_break)
            and score >= V66_EBP_MIN_CONFIRM_SCORE
        )
        why = f"15M-confirm {score}/5 body={int(body_dir)} reject={int(zone_reject)} break={int(micro_break)} macd={int(macd_turn)} ema={int(ema_side)}"
        return confirmed, why, score, int(a["ts"])
    except Exception as e:
        return False, f"confirm-error-{type(e).__name__}", 0, 0


def _v63_ebp_candidate(symbol):
    """V6.6 EBP: location + CLOSED rejection + stricter BTC regime."""
    base = _v65_ebp_candidate(symbol)
    if not base or not base.get("side"):
        return base

    d = 1 if base.get("side") == "long" else -1
    btc_ok, btc_note, btc_support = _v66_btc_state(d) if V63_EBP_REQUIRE_BTC else (True, "BTC gate off", False)
    base["btc_note"] = btc_note

    # The old score awarded +8 merely because BTC did not hard-block. Remove
    # that bonus when BTC is only neutral/unavailable.
    if not btc_support:
        base["score"] = max(0.0, safe_float(base.get("score")) - 8.0)
        base["quality"] = base["score"]
        base["grade"] = "A+" if base["score"] >= 92 else "A" if base["score"] >= V63_EBP_MIN_QUALITY else "C"

    if not btc_ok:
        base["skip"] = btc_note
        base["entry_route"] = "WAIT"
        return base

    ebp = _v63_h4_ebp(symbol)
    fvg = _v63_fvg_inside_ebp(symbol, ebp) if ebp else None
    if not ebp or not fvg:
        return base

    confirmed, why, conf_score, conf_ts = _v66_ebp_closed_confirmation(symbol, ebp, fvg)
    base["v66_confirm"] = why
    base["confirmations"] = conf_score
    if conf_ts:
        # Makes each closed confirmation a one-shot event rather than reusing
        # the same 4H EBP timestamp forever.
        base["signal_bar_ts"] = conf_ts

    if safe_float(base.get("score")) < V63_EBP_MIN_QUALITY:
        base["skip"] = f"EBP-quality-{safe_float(base.get('score')):.0f}<{V63_EBP_MIN_QUALITY:.0f}"
        base["entry_route"] = "WAIT"
        return base

    if not confirmed:
        if "invalidated" not in str(base.get("skip", "")):
            base["skip"] = f"EBP-TOUCHED-wait-15M-rejection · {why}"
            base["entry_route"] = "WAIT"
        return base

    # Confirmation is present. Still require current live price to be at/very
    # near the FVG; we do not chase after the confirmation candle.
    try:
        live = safe_float(exchange.fetch_ticker(symbol).get("last"), base.get("live_price"))
        atr = max(safe_float(base.get("atr")), 1e-12)
        zlo, zhi = base.get("fvg_zone") or (0, 0)
        tol = V63_EBP_TOUCH_TOL_ATR * atr
        if not (safe_float(zlo) - tol <= live <= safe_float(zhi) + tol):
            base["skip"] = "EBP-confirmed-but-price-left-zone"
            base["entry_route"] = "WAIT"
            return base
        base["live_price"] = live
    except Exception:
        base["skip"] = "EBP-live-price-unavailable"
        base["entry_route"] = "WAIT"
        return base

    base["score"] = min(100.0, safe_float(base.get("score")) + 4.0)
    base["quality"] = base["score"]
    base["grade"] = "A+" if base["score"] >= 92 else "A"
    base["skip"] = None
    base["entry_route"] = "EBP-CONFIRMED"
    return base


def _v66_failed_ebp_flip_candidate(symbol):
    """Opposite trade only after the EBP thesis actually fails + full confirmation."""
    if not V66_FLIP_ENABLED:
        return None
    try:
        ebp = _v63_h4_ebp(symbol)
        if not ebp:
            return None
        fvg = _v63_fvg_inside_ebp(symbol, ebp)
        if not fvg:
            return None

        original_d = int(ebp["dir"])
        d = -original_d
        side = "long" if d == 1 else "short"
        df = _v6_closed(_v6_df(symbol, "15m"))
        if len(df) < 210:
            return None
        a, p = df.iloc[-1], df.iloc[-2]
        atr = max(safe_float(a["atr"]), 1e-12)
        c, o = safe_float(a["close"]), safe_float(a["open"])
        pc, ph, pl = safe_float(p["close"]), safe_float(p["high"]), safe_float(p["low"])
        e21, pe21 = safe_float(a["ema_slow"]), safe_float(p["ema_slow"])
        hist, hp = safe_float(a["macd_hist"]), safe_float(p["macd_hist"])
        live = safe_float(exchange.fetch_ticker(symbol).get("last"), c)
        tol = V63_EBP_TOUCH_TOL_ATR * atr

        # Original EBP must be genuinely invalidated through the far FVG side.
        failed = live > safe_float(fvg["hi"]) + tol if original_d == -1 else live < safe_float(fvg["lo"]) - tol
        if not failed:
            return None

        h4 = _v6_4h_context(symbol)
        h4_ok = int(h4.get("dir", 0)) == d and int(h4.get("score", 0)) >= V66_FLIP_MIN_H4_SCORE and safe_float(h4.get("adx")) >= V66_FLIP_MIN_H4_ADX
        loc_ok, loc_note, loc = _v62_4h_location(h4, d, live)
        ema_ok = (c > e21 and e21 > pe21) if d == 1 else (c < e21 and e21 < pe21)
        macd_ok = hist > hp if d == 1 else hist < hp
        micro_break = c > ph + 0.03 * atr if d == 1 else c < pl - 0.03 * atr
        body_ok = (c > o) if d == 1 else (c < o)
        btc_ok, btc_note, btc_support = _v66_btc_state(d)

        q = 30 + (22 if h4_ok else 0) + (14 if loc_ok else 0) + (12 if ema_ok else 0) + (10 if macd_ok else 0) + (12 if micro_break else 0) + (5 if body_ok else 0) + (10 if btc_support else 0)
        q = min(100.0, q)

        skip = None
        if not h4_ok: skip = "FLIP-wait-4H-opposite-trend"
        elif not loc_ok: skip = f"FLIP-{loc_note}"
        elif not ema_ok: skip = "FLIP-wait-15M-EMA21"
        elif not macd_ok: skip = "FLIP-wait-momentum"
        elif not micro_break: skip = "FLIP-wait-micro-break"
        elif not body_ok: skip = "FLIP-wait-directional-candle"
        elif not btc_ok or not btc_support: skip = f"FLIP-{btc_note}"
        elif q < 92: skip = f"FLIP-quality-{q:.0f}<92"

        recent = df.iloc[-7:]
        if d == 1:
            raw = c - (safe_float(recent["low"].min()) - 0.10 * atr)
        else:
            raw = (safe_float(recent["high"].max()) + 0.10 * atr) - c
        risk = max(0.55 * atr, min(raw, 1.20 * atr))

        return {
            "symbol": symbol, "side": side, "score": q, "score_max": 100.0, "quality": q,
            "threshold": 92, "grade": "A+" if q >= 96 else "A",
            "skip": skip, "entry_route": "FAILED-EBP-FLIP" if skip is None else "WAIT",
            "strategy": "FAILED-EBP-FLIP", "price": c, "atr": atr,
            "signal_bar_ts": int(a["ts"]), "risk_distance": risk,
            "stop_origin": "V6.6 failed EBP + opposite 4H/15M confirmation", "final_r": 2.5,
            "h4_score": h4.get("score", 0), "h4_adx": h4.get("adx", 0),
            "confirmations": int(h4_ok)+int(loc_ok)+int(ema_ok)+int(macd_ok)+int(micro_break)+int(btc_support),
            "role": False, "sweep": False, "fvg": True, "hns": False,
            "candle": "failed-EBP-flip", "macd_ok": macd_ok, "volume_above": False,
            "extension_atr": 0.0, "btc_note": btc_note, "ema21_reset": True,
            "location_note": loc_note, "h4_ext_atr": safe_float(loc.get("h4_ext_atr")),
            "h4_range_pos": safe_float(loc.get("h4_range_pos")), "h4_impulse3_atr": safe_float(loc.get("h4_impulse3_atr")),
            "zone_level": safe_float(fvg["entry"]), "fvg_zone": (fvg["lo"], fvg["hi"]),
            "sweep_depth": ebp["sweep_atr"], "v66_failed_side": "long" if original_d == 1 else "short",
        }
    except Exception:
        return None


def _v6_candidate(symbol):
    """V6.6 router: confirmed EBP, failed-EBP flip, trend reset, structure."""
    ebp = _v63_ebp_candidate(symbol)
    flip = _v66_failed_ebp_flip_candidate(symbol)
    base = _v62_structure_candidate(symbol)
    tr = _v64_trend_reset_candidate(symbol)

    ready = [x for x in (ebp, flip, tr, base)
             if x and x.get("side") and not x.get("skip") and x.get("entry_route") not in (None, "WAIT")]
    if ready:
        # Do not give raw EBP an artificial priority anymore. Closed confirmation
        # and actual quality decide.
        return max(ready, key=lambda x: (safe_float(x.get("quality")), int(x.get("confirmations", 0))))

    waits = [x for x in (ebp, flip, tr, base) if x]
    def wait_rank(x):
        sk = str(x.get("skip", ""))
        action = 4 if "wait-15M-rejection" in sk else 3 if ("ARMED" in sk or "wait-retrace" in sk) else 2 if sk.startswith(("TR-wait", "FLIP-wait")) else 1
        return action, safe_float(x.get("quality", x.get("score", 0)))
    return max(waits, key=wait_rank) if waits else None


def evaluate_symbol(symbol, btc=None):
    return _v6_candidate(symbol)


def sizing_profile(signal):
    """Undo the premature V6.5 scale-up until the repaired edge proves itself."""
    base = _v65_sizing_profile(signal)
    strategy = signal.get("strategy")
    if strategy == "FAILED-EBP-FLIP":
        cap = V66_FLIP_RISK_PCT
    elif strategy == "EBP-FVG":
        cap = V66_MAX_EBP_RISK_PCT
    elif strategy == "TREND-RESET":
        cap = V66_MAX_TREND_RISK_PCT
    else:
        cap = V66_MAX_STRUCTURE_RISK_PCT
    out = dict(base)
    out["risk_pct"] = min(safe_float(base.get("risk_pct"), cap), cap)
    out["name"] = "V66-" + str(base.get("name", strategy or "SETUP"))
    return out


def sniper_portfolio_allows(signal, positions):
    """Side-aware circuit breaker; never blindly invert a losing direction."""
    side = signal.get("side")
    same = sum(1 for p in positions if position_side(p) == side)
    if same >= V6_MAX_SAME_SIDE:
        return False, f"max-{V6_MAX_SAME_SIDE}-same-side"

    # Existing loss streak becomes a stronger gate: elite score alone is no
    # longer enough; BTC must actively support that side.
    streak = int(sniper_side_loss_streak.get(side, 0))
    d = 1 if side == "long" else -1
    btc_ok, btc_note, btc_support = _v66_btc_state(d)
    if streak >= 2 and (safe_float(signal.get("score")) < 94 or not btc_ok or not btc_support):
        return False, f"{side}-loss-streak-{streak}: need Q94 + BTC support"

    # Intraday cold-side circuit breaker.
    trades = int(daily.get(f"{side}_trades", 0))
    wins = int(daily.get(f"{side}_wins", 0))
    net = safe_float(daily.get(f"{side}_net", 0))
    wr = wins / trades if trades else 1.0
    if trades >= V66_SIDE_COLD_MIN_TRADES and wr <= V66_SIDE_COLD_MAX_WR and net <= -V66_SIDE_COLD_MIN_LOSS:
        if safe_float(signal.get("score")) < 96 or not btc_support:
            return False, f"{side}-COLD {wins}/{trades} net ${net:.2f}: only Q96 + BTC support"

    return True, "OK"


def startup_diagnostics():
    acc = futures_account()
    reset_daily_if_needed(acc["total_equity"])
    tg(
        f"🧠 PRECISION STRUCTURE FUSION STARTED v{SNIPER_VERSION}\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"TIMEFRAMES: 4H + 15M ONLY\n"
        f"V6.6 CONFIRM OR FLIP\n"
        f"• EBP-FVG: retrace is NOT enough → closed 15M rejection required\n"
        f"• BTC regime: opposing BTC 15M blocks alts unless BTC 4H supports the trade\n"
        f"• Failed EBP may flip ONLY with 4H trend + 15M micro-break + BTC support\n"
        f"• Side cold-mode after repeated directional losses\n"
        f"• Instrument metadata cache/retry enabled\n"
        f"Risk caps while proving repaired edge: EBP {V66_MAX_EBP_RISK_PCT*100:.1f}% · "
        f"Trend {V66_MAX_TREND_RISK_PCT*100:.1f}% · Structure {V66_MAX_STRUCTURE_RISK_PCT*100:.1f}% · "
        f"Flip {V66_FLIP_RISK_PCT*100:.1f}%\n"
        f"Leverage {LEVERAGE}x · max positions {MAX_ACTIVE_POSITIONS} · max same side {V6_MAX_SAME_SIDE}\n"
        f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}"
    )


# ============================================================
# V6.7 — PRECISION RETRACE / FRESH-FVG / TRUE SIDE ACCOUNTING
# ============================================================
# Review of the 2026-09-29 overnight run:
# - WIF EBP LONG reached +1R, then closed around entry; fees made it net negative.
# - ORDI EBP LONG reached the final target and closed +$1.16 net.
# - Both useful EBP longs had BTC 15M aligned while BTC 4H was opposite.
# - The close-history side classifier mislabeled a near-BE LONG as SHORT because
#   it inferred direction from PnL sign. In one-way mode this polluted side
#   streaks and could block the wrong direction.
#
# V6.7 therefore improves precision without simply raising thresholds:
# 1) use BloFin positions-history `side` (buy/sell) as the primary direction;
# 2) only use a still-fresh FVG from inside the EBP candle;
# 3) retrace must penetrate meaningfully into the FVG before 15M confirmation;
# 4) EBP SL stays anchored to the precomputed FVG/impulse structure;
# 5) management locks a fee/slippage buffer after a trade proves itself;
# 6) EBP management is route-specific instead of using generic 1R/2R steps.

SNIPER_VERSION = "6.7.0"

V67_FVG_MAX_PREMITIGATION = float(os.getenv("V67_FVG_MAX_PREMITIGATION", "0.55"))
V67_MIN_RETRACE_DEPTH = float(os.getenv("V67_MIN_RETRACE_DEPTH", "0.25"))
V67_EBP_BE_TRIGGER_R = float(os.getenv("V67_EBP_BE_TRIGGER_R", "0.90"))
V67_EBP_LOCK_TRIGGER_R = float(os.getenv("V67_EBP_LOCK_TRIGGER_R", "1.25"))
V67_EBP_LOCK_R = float(os.getenv("V67_EBP_LOCK_R", "0.45"))
V67_GENERIC_BE_TRIGGER_R = float(os.getenv("V67_GENERIC_BE_TRIGGER_R", "1.0"))
V67_GENERIC_LOCK_TRIGGER_R = float(os.getenv("V67_GENERIC_LOCK_TRIGGER_R", "2.0"))
V67_GENERIC_LOCK_R = float(os.getenv("V67_GENERIC_LOCK_R", "1.0"))
V67_FEE_LOCK_MULT = float(os.getenv("V67_FEE_LOCK_MULT", "1.05"))

# Preserve V6.6 functions that V6.7 wraps.
_v66_candidate_impl = _v63_ebp_candidate


def infer_side(record):
    """Use BloFin's explicit historical side before any price/PnL inference."""
    ps = str(record.get("positionSide", "") or "").lower()
    if ps in ("long", "short"):
        return ps

    # BloFin GET /account/positions-history documents `side` as buy/sell.
    # In one-way/net mode buy = long position, sell = short position.
    hist_side = str(record.get("side", "") or "").lower()
    if hist_side == "buy":
        return "long"
    if hist_side == "sell":
        return "short"

    # Last-resort fallback. Do NOT use PnL sign: a near-BE long can have a
    # negative realized PnL because of fees/funding and would be mislabeled.
    entry = safe_float(record.get("openAveragePrice"))
    exit_p = safe_float(record.get("closeAveragePrice"))
    if entry > 0 and exit_p > 0:
        # Direction cannot be known with certainty from two prices alone.
        # Prefer stored local position metadata when an exact positionId exists.
        pid = str(record.get("positionId", "") or "")
        meta = _v6_position_meta.get(pid, {}) if pid else {}
        mside = str(meta.get("side", "") or "").lower()
        if mside in ("long", "short"):
            return mside
    return "net"


def _v63_fvg_inside_ebp(symbol, ebp):
    """Select a directional 15M FVG that remained sufficiently fresh."""
    try:
        cdf = _v6_closed(_v6_df(symbol, "15m"))
        if len(cdf) < 30:
            return None
        inside = cdf[(cdf["ts"] >= ebp["ts"]) & (cdf["ts"] < ebp["end_ts"])].copy()
        if len(inside) < 3:
            return None

        d = int(ebp["dir"])
        candidates = []
        for i in range(2, len(inside)):
            left, midc, right = inside.iloc[i-2], inside.iloc[i-1], inside.iloc[i]
            atr15 = max(safe_float(right["atr"]), 1e-12)

            if d == 1:
                lo = safe_float(left["high"])
                hi = safe_float(right["low"])
                impulse = safe_float(midc["close"]) - safe_float(midc["open"])
                valid = hi > lo and (hi-lo) >= V63_EBP_MIN_FVG_ATR*atr15 and impulse > 0
            else:
                lo = safe_float(right["high"])
                hi = safe_float(left["low"])
                impulse = safe_float(midc["open"]) - safe_float(midc["close"])
                valid = hi > lo and (hi-lo) >= V63_EBP_MIN_FVG_ATR*atr15 and impulse > 0
            if not valid:
                continue

            width_px = hi - lo
            later = inside.iloc[i+1:]
            premitigation = 0.0
            invalidated_inside = False
            if not later.empty:
                if d == 1:
                    later_low = safe_float(later["low"].min())
                    premitigation = max(0.0, min(1.5, (hi - later_low) / max(width_px, 1e-12)))
                    invalidated_inside = later_low < lo - 0.04*atr15
                else:
                    later_high = safe_float(later["high"].max())
                    premitigation = max(0.0, min(1.5, (later_high - lo) / max(width_px, 1e-12)))
                    invalidated_inside = later_high > hi + 0.04*atr15

            # If the gap was already deeply filled during the same 4H EBP
            # candle, the later "retrace" is no longer a clean first return.
            if invalidated_inside or premitigation > V67_FVG_MAX_PREMITIGATION:
                continue

            width_atr = width_px / atr15
            displacement = impulse / atr15
            freshness = max(0.0, 1.0 - premitigation)
            score = width_atr*1.5 + displacement + i/len(inside)*0.35 + freshness*0.75
            candidates.append((score, lo, hi, atr15, int(right["ts"]), i, premitigation))

        if not candidates:
            return None

        _, lo, hi, atr15, fts, idx, premitigation = max(candidates, key=lambda x: x[0])
        entry = lo + (hi-lo)*V63_EBP_ENTRY_FRACTION
        upto = inside.iloc[:idx+1]
        if d == 1:
            extreme = safe_float(upto["low"].min())
            raw_stop = extreme - V63_EBP_STOP_BUFFER_ATR*atr15
            raw_risk = entry - raw_stop
        else:
            extreme = safe_float(upto["high"].max())
            raw_stop = extreme + V63_EBP_STOP_BUFFER_ATR*atr15
            raw_risk = raw_stop - entry

        risk = max(V63_EBP_MIN_STOP_ATR*atr15, min(raw_risk, V63_EBP_MAX_STOP_ATR*atr15))
        stop = entry - d*risk
        return {
            "lo": lo, "hi": hi, "entry": entry, "atr": atr15, "ts": fts,
            "risk": risk, "stop": stop, "extreme": extreme,
            "premitigation": premitigation, "freshness": max(0.0, 1.0-premitigation),
        }
    except Exception:
        return None


def _v66_ebp_closed_confirmation(symbol, ebp, fvg):
    """Closed 15M confirmation + meaningful FVG penetration."""
    try:
        cdf = _v6_closed(_v6_df(symbol, "15m"))
        post = cdf[cdf["ts"] >= ebp["end_ts"]].tail(max(3, V66_EBP_CONFIRM_LOOKBACK))
        if post.empty:
            return False, "wait-first-closed-15M", 0, 0

        atr = max(safe_float(post.iloc[-1]["atr"]), 1e-12)
        tol = V66_EBP_CONFIRM_TOL_ATR * atr
        lo, hi = safe_float(fvg["lo"]), safe_float(fvg["hi"])
        width = max(hi-lo, 1e-12)
        mid = (lo+hi)/2.0
        d = int(ebp["dir"])

        touched_positions = []
        best_depth = 0.0
        for j in range(len(post)):
            r = post.iloc[j]
            rl, rh = safe_float(r["low"]), safe_float(r["high"])
            overlaps = rl <= hi + tol and rh >= lo - tol
            if not overlaps:
                continue
            if d == 1:
                depth = (hi - rl) / width
            else:
                depth = (rh - lo) / width
            depth = max(0.0, min(1.5, depth))
            if depth >= V67_MIN_RETRACE_DEPTH:
                touched_positions.append(j)
                best_depth = max(best_depth, depth)

        if not touched_positions:
            return False, f"wait-deeper-FVG-retrace<{V67_MIN_RETRACE_DEPTH:.0%}", 0, int(post.iloc[-1]["ts"])

        touch_j = touched_positions[-1]
        if len(post)-1-touch_j > 1:
            return False, "retrace-confirmation-stale", 0, int(post.iloc[-1]["ts"])

        a = post.iloc[-1]
        p = post.iloc[-2] if len(post) >= 2 else a
        o, c = safe_float(a["open"]), safe_float(a["close"])
        h, l = safe_float(a["high"]), safe_float(a["low"])
        ph, pl = safe_float(p["high"]), safe_float(p["low"])
        rng = max(h-l, 1e-12)
        upper = h-max(o,c)
        lower = min(o,c)-l
        hist, hp = safe_float(a["macd_hist"]), safe_float(p["macd_hist"])
        ema21 = safe_float(a["ema_slow"])

        if d == 1:
            body_dir = c > o
            zone_reject = c >= mid and lower/rng >= 0.18
            micro_break = c > ph + 0.02*atr
            macd_turn = hist > hp
            ema_side = c >= ema21
            close_not_failed = c >= lo-tol
        else:
            body_dir = c < o
            zone_reject = c <= mid and upper/rng >= 0.18
            micro_break = c < pl - 0.02*atr
            macd_turn = hist < hp
            ema_side = c <= ema21
            close_not_failed = c <= hi+tol

        score = int(body_dir)+int(zone_reject)+int(micro_break)+int(macd_turn)+int(ema_side)
        confirmed = (
            close_not_failed and body_dir and macd_turn
            and (zone_reject or micro_break)
            and score >= V66_EBP_MIN_CONFIRM_SCORE
        )
        why = (
            f"15M-confirm {score}/5 · depth {best_depth:.0%} · "
            f"body={int(body_dir)} reject={int(zone_reject)} break={int(micro_break)} "
            f"macd={int(macd_turn)} ema={int(ema_side)}"
        )
        return confirmed, why, score, int(a["ts"])
    except Exception as e:
        return False, f"confirm-error-{type(e).__name__}", 0, 0


def _v63_ebp_candidate(symbol):
    """V6.6 logic + V6.7 freshness/structural-stop metadata."""
    base = _v66_candidate_impl(symbol)
    if not base or base.get("strategy") != "EBP-FVG":
        return base
    try:
        ebp = _v63_h4_ebp(symbol)
        fvg = _v63_fvg_inside_ebp(symbol, ebp) if ebp else None
        if fvg:
            base["structural_stop"] = safe_float(fvg["stop"])
            base["fvg_freshness"] = safe_float(fvg.get("freshness"), 0.0)
            base["fvg_premitigation"] = safe_float(fvg.get("premitigation"), 0.0)
            # Reward freshness modestly; it cannot turn a blocked setup into a
            # trade by itself because all V6.6 confirmation gates still apply.
            bonus = min(3.0, max(0.0, base["fvg_freshness"]*3.0))
            base["score"] = min(100.0, safe_float(base.get("score")) + bonus)
            base["quality"] = base["score"]
            base["grade"] = "A+" if base["score"] >= 92 else "A" if base["score"] >= V63_EBP_MIN_QUALITY else "C"
    except Exception:
        pass
    return base


def _v6_levels(signal, live, tick):
    side = signal["side"]
    d = 1 if side == "long" else -1
    final_r = safe_float(signal.get("final_r"), V6_FINAL_R) or V6_FINAL_R

    if signal.get("strategy") == "EBP-FVG":
        structural_sl = safe_float(signal.get("structural_stop"))
        if structural_sl <= 0:
            risk0 = safe_float(signal.get("risk_distance"))
            structural_sl = live - d*risk0
        # Structural stop must remain on the correct side of the live entry.
        if (d == 1 and structural_sl >= live) or (d == -1 and structural_sl <= live):
            return None
        sl = round_trade_price(structural_sl, tick, side, "sl")
        risk = abs(live-sl)
        if risk <= 0:
            return None
        t1 = round_trade_price(live+d*risk*0.75, tick, side, "tp")
        t2 = round_trade_price(live+d*risk*1.25, tick, side, "tp")
        t3 = round_trade_price(live+d*risk*final_r, tick, side, "tp")
        return sl, t1, t2, t3, risk

    risk = safe_float(signal.get("risk_distance"))
    if live <= 0 or risk <= 0:
        return None
    sl = round_trade_price(live-d*risk, tick, side, "sl")
    t1 = round_trade_price(live+d*risk*V6_TP1_R, tick, side, "tp")
    t2 = round_trade_price(live+d*risk*V6_TP2_R, tick, side, "tp")
    t3 = round_trade_price(live+d*risk*final_r, tick, side, "tp")
    return sl, t1, t2, t3, risk


def open_trade(signal):
    """V6.7 execution: same protected market entry, true EBP structural stop."""
    global protection_lock, _v62_last_entry_time
    symbol, side = signal["symbol"], signal["side"]
    acc = futures_account()
    equity, available = acc["total_equity"], acc["available"]
    reset_daily_if_needed(equity)
    if equity < MIN_EQUITY_USDT or available <= 0 or (not PAPER_TRADING and protection_lock):
        return False
    if time.time()-_v62_last_entry_time < V62_GLOBAL_ENTRY_GAP_MIN*60:
        return False

    tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
    live = safe_float(exchange.fetch_ticker(symbol).get("last"), signal["price"])
    sig = safe_float(signal["price"])
    if live <= 0 or sig <= 0:
        return False
    d = 1 if side == "long" else -1
    is_ebp = signal.get("strategy") == "EBP-FVG"

    if is_ebp:
        z = signal.get("fvg_zone") or (0,0)
        zlo, zhi = safe_float(z[0]), safe_float(z[1])
        tol = V63_EBP_TOUCH_TOL_ATR*safe_float(signal.get("atr"))
        if not (zlo-tol <= live <= zhi+tol):
            return False
    else:
        adverse = d*((live-sig)/sig)
        if adverse > V62_MAX_ADVERSE_DRIFT:
            tg(f"⏭️ {symbol} {side.upper()} V6.7 TOO LATE\nAdverse drift {adverse*100:.2f}% > {V62_MAX_ADVERSE_DRIFT*100:.2f}%")
            return False
        live_loc_ok, live_loc_note, _ = _v62_live_location_ok(symbol, side, live)
        if not live_loc_ok:
            tg(f"⏭️ {symbol} {side.upper()} V6.7 LOCATION BLOCK\n{live_loc_note}\nNo chase at 4H extreme.")
            return False

    levels = _v6_levels(signal, live, tick)
    if not levels:
        return False
    sl, t1, t2, t3, risk = levels
    profile = sizing_profile(signal)
    final_r = safe_float(signal.get("final_r"), V6_FINAL_R) or V6_FINAL_R
    sizing = calculate_contracts_v5(symbol, live, sl, equity, available, profile["risk_pct"], profile["target_net_tp"], final_r)
    if not sizing or sizing["margin"] > available*AVAILABLE_MARGIN_BUFFER:
        return False

    before_ids = {position_id(p) for p in get_positions() if position_id(p)}
    oklev, levres = set_leverage(symbol, LEVERAGE)
    if not oklev:
        tg(f"⛔ LEVERAGE FAILED\n{symbol}\n{levres}")
        return False

    tag = "ps67e" if is_ebp else "ps67s"
    payload = {
        "instId": blofin_inst_id(symbol), "marginMode": MARGIN_MODE, "positionSide": "net",
        "side": "buy" if side == "long" else "sell", "orderType": "market",
        "size": str(sizing["contracts"]),
        "tpTriggerPrice": str(t3), "tpOrderPrice": "-1", "tpTriggerPriceType": "mark",
        "slTriggerPrice": str(sl), "slOrderPrice": "-1", "slTriggerPriceType": "mark",
        "clientOrderId": f"{tag}{int(time.time()*1000)%1000000000}",
    }
    if BROKER_ID:
        payload["brokerId"] = BROKER_ID

    if is_ebp:
        zlo, zhi = signal.get("fvg_zone", (0,0))
        details = (
            f"4H EBP sweep {signal.get('ebp_sweep_atr',0):.2f}ATR · reclaim {signal.get('ebp_reclaim_atr',0):.2f}ATR · age {signal.get('ebp_age_h',0):.1f}h\n"
            f"15M FVG {zlo:.8g} → {zhi:.8g} · freshness {signal.get('fvg_freshness',0)*100:.0f}%\n"
            f"{signal.get('v66_confirm','closed 15M confirmation')}\n"
            f"Entry {live:.8g} · STRUCT SL {sl:.8g}\n"
            f"TP-A {t1:.8g} (0.75R) · TP-B {t2:.8g} (1.25R) · FINAL {t3:.8g} ({final_r:.1f}R)"
        )
    else:
        flags = []
        for k,label in (("role","ROLE"),("sweep","SWEEP"),("fvg","FVG"),("hns","H&S")):
            if signal.get(k):
                flags.append(label)
        details = (
            f"4H score {signal.get('h4_score',0)}/6 · ADX {signal.get('h4_adx',0):.1f} · EMA21 ext {signal.get('h4_ext_atr',0):.2f}ATR\n"
            f"15M confirmations {signal.get('confirmations',0)} · {' + '.join(flags) if flags else '-'}\n"
            f"Entry {live:.8g} · SL {sl:.8g} · TP1 {t1:.8g} · TP2 {t2:.8g} · FINAL {t3:.8g} ({final_r:.1f}R)"
        )

    msg = (
        f"{'🧪 PAPER' if PAPER_TRADING else '🎯 V6.7 ENTRY'}\n"
        f"{symbol} {side.upper()} · {signal.get('entry_route')} · {signal.get('grade')} {signal.get('score',0):.0f}/100\n"
        f"{details}\nBTC {signal.get('btc_note','-')}\n"
        f"Risk cap {profile['risk_pct']*100:.1f}% · expected final net ~${sizing['expected_net_tp_usdt']:.2f}\n"
        f"Notional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x"
    )

    if PAPER_TRADING:
        tg(msg)
        _v62_last_entry_time = time.time()
        return True

    resp = native("private_post_trade_order", payload)
    if not native_ok(resp):
        err = response_error(resp)
        if "152012" in err:
            protection_lock = True
        tg(f"❌ V6.7 ORDER FAILED\n{symbol} {side.upper()}\n{err}")
        return False

    _v62_last_entry_time = time.time()
    tg(msg)
    new_pos = find_new_position(symbol, before_ids, timeout=8)
    if new_pos is None:
        tg(f"⚠️ {symbol}: V6.7 order accepted, position not visible yet.")
        return True

    pid = position_id(new_pos)
    actual_entry = position_entry(new_pos) or live
    actual_risk = abs(actual_entry-sl)
    if actual_risk <= 0:
        actual_risk = risk
    if pid:
        sniper_managed_positions.add(pid)
        _v6_position_meta[pid] = {
            "risk": actual_risk, "planned_risk": risk, "structural_sl": sl,
            "tp1": t1, "tp2": t2, "tp3": t3, "opened": time.time(),
            "route": signal.get("entry_route"), "side": side,
            "final_r": final_r, "strategy": signal.get("strategy"),
            "planned_entry": live, "actual_entry": actual_entry,
        }

    if has_live_protection(new_pos)[0]:
        if abs(actual_entry-live)/max(live,1e-12) >= 0.001:
            eff_r = d*(t3-actual_entry)/max(actual_risk,1e-12)
            tg(f"🎯 V6.7 FILL CHECK\n{symbol} {side.upper()}\nPlanned {live:.8g} · actual {actual_entry:.8g}\nStructural SL kept {sl:.8g} · final target effective {eff_r:.2f}R")
        tg(f"🛡️ V6.7 PROTECTION CONFIRMED\n{symbol} {side.upper()}\n{final_r:.1f}R planned TP + structural SL visible.")
        return True

    last = safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side, sl, t3, last):
        ok, res = place_tpsl_for_position(new_pos, sl, t3, client_tag=tag)
        if ok:
            tg(f"🛡️ V6.7 FALLBACK PROTECTION CONFIRMED\n{symbol}")
            return True
        protection_lock = True
        tg(f"🚨 V6.7 PROTECTION FAILURE\n{symbol}\n{res}\nNEW TRADES PAUSED.")
        return True

    protection_lock = True
    tg(f"🚨 {symbol}: V6.7 position exists but safe fallback protection failed.\nNEW TRADES PAUSED.")
    return True


def manage_sniper_step_stops():
    """Route-specific, fee-aware profit protection."""
    if PAPER_TRADING:
        return
    now = time.time()
    for p in get_positions():
        try:
            if not _v6_managed(p):
                continue
            symbol, side, entry = position_symbol(p), position_side(p), position_entry(p)
            pid = position_id(p)
            if entry <= 0:
                continue
            ok, order = has_live_protection(p)
            if not ok or not order:
                continue
            tp = safe_float(order.get("tpTriggerPrice"))
            current_sl = safe_float(order.get("slTriggerPrice"))
            if tp <= 0 or current_sl <= 0:
                continue

            meta = _v6_position_meta.get(pid, {})
            risk = safe_float(meta.get("risk"))
            strategy = str(meta.get("strategy", "") or "")
            if risk <= 0:
                final_r = safe_float(meta.get("final_r"), V6_FINAL_R) or V6_FINAL_R
                base = abs(tp-entry)/max(final_r, 1e-12)
                if base <= 0:
                    continue
                risk = base
                meta = {"risk":risk, "opened":safe_float(p.get("createTime"))/1000.0, "route":"restart-adopt", "strategy":strategy}
                _v6_position_meta[pid] = meta

            d = 1 if side == "long" else -1
            tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
            current = safe_float(exchange.fetch_ticker(symbol).get("last"))
            ext = sniper_recent_extreme(symbol, side)
            favorable = max(current, ext) if d == 1 else min(current, ext)
            current_r = d*(current-entry)/risk
            best_r = d*(favorable-entry)/risk

            # Cover estimated round-trip taker fees + slippage once the trade
            # has proved itself. This prevents a nominal BE from becoming a
            # small net loser like WIF.
            fee_px = entry*(TAKER_FEE_RATE*2 + SLIPPAGE_RATE)*V67_FEE_LOCK_MULT
            fee_be = round_trade_price(entry+d*fee_px, tick, side, "sl")

            desired = None
            stage = None
            if strategy == "EBP-FVG":
                if best_r >= V67_EBP_LOCK_TRIGGER_R:
                    lock_px = entry+d*V67_EBP_LOCK_R*risk
                    desired = round_trade_price(max(lock_px, fee_be) if d==1 else min(lock_px, fee_be), tick, side, "sl")
                    stage = f"{V67_EBP_LOCK_TRIGGER_R:.2f}R → lock +{V67_EBP_LOCK_R:.2f}R"
                elif best_r >= V67_EBP_BE_TRIGGER_R:
                    desired = fee_be
                    stage = f"{V67_EBP_BE_TRIGGER_R:.2f}R → fee-safe BE"
            else:
                if best_r >= V67_GENERIC_LOCK_TRIGGER_R:
                    desired = round_trade_price(entry+d*V67_GENERIC_LOCK_R*risk, tick, side, "sl")
                    stage = f"{V67_GENERIC_LOCK_TRIGGER_R:.0f}R → lock +{V67_GENERIC_LOCK_R:.0f}R"
                elif best_r >= V67_GENERIC_BE_TRIGGER_R:
                    desired = fee_be
                    stage = f"{V67_GENERIC_BE_TRIGGER_R:.0f}R → fee-safe BE"

            if desired is not None:
                improves = desired > current_sl+tick*0.1 if d==1 else desired < current_sl-tick*0.1
                if improves:
                    ok2, res = amend_tpsl_stop(p, desired)
                    key = f"{pid}:{stage}"
                    if ok2 and not sniper_step_notified.get(key):
                        tg(f"🪜 V6.7 STEP STOP\n{symbol} {side.upper()}\n{stage} · SL {desired:.8g}")
                        sniper_step_notified[key] = True

            opened = safe_float(meta.get("opened")) or safe_float(p.get("createTime"))/1000.0
            age = (now-opened)/60 if opened > 0 else 0
            if age >= 45 and current_r < 0.35:
                try:
                    cdf = _v6_closed(_v6_df(symbol, "15m"))
                    a, pr = cdf.iloc[-1], cdf.iloc[-2]
                    c, e21 = safe_float(a["close"]), safe_float(a["ema_slow"])
                    pc, pe21 = safe_float(pr["close"]), safe_float(pr["ema_slow"])
                    lost15 = (c<e21 and pc<pe21) if d==1 else (c>e21 and pc>pe21)
                    h4 = _v6_4h_context(symbol)
                    macro_flip = h4.get("dir",0) == -d
                    if lost15 and macro_flip:
                        _v6_close_position(p, f"4H/15M thesis invalidated after {age:.0f}m ({current_r:+.2f}R).")
                        continue
                except Exception:
                    pass
            if age >= V6_HARD_EXIT_MIN:
                _v6_close_position(p, f"Hard time exit {age:.0f}m ({current_r:+.2f}R).")
                continue
            if age >= V6_SOFT_EXIT_MIN and current_r < 0.20:
                _v6_close_position(p, f"No follow-through after {age:.0f}m ({current_r:+.2f}R).")
                continue
        except Exception as e:
            # One symbol must never stop management of the other positions.
            tg(f"⚠️ V6.7 POSITION MANAGER\n{position_symbol(p) if isinstance(p,dict) else '-'}\n{type(e).__name__}: {e}")


def startup_diagnostics():
    acc = futures_account()
    reset_daily_if_needed(acc["total_equity"])
    tg(
        f"🧠 PRECISION STRUCTURE FUSION STARTED v{SNIPER_VERSION}\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"TIMEFRAMES: 4H + 15M ONLY\n"
        f"V6.7 PRECISION RETRACE\n"
        f"• EBP uses fresh 15M FVG only; pre-mitigation <= {V67_FVG_MAX_PREMITIGATION:.0%}\n"
        f"• Retrace must penetrate >= {V67_MIN_RETRACE_DEPTH:.0%} of FVG + closed 15M confirmation\n"
        f"• EBP stop anchored to FVG/impulse structure, not re-centered on market entry\n"
        f"• EBP {V67_EBP_BE_TRIGGER_R:.2f}R → fee-safe BE; {V67_EBP_LOCK_TRIGGER_R:.2f}R → lock +{V67_EBP_LOCK_R:.2f}R\n"
        f"• BloFin history buy/sell fixes LONG/SHORT stats and side-streak gates\n"
        f"• BTC 15M synchronization + V6.6 Confirm-or-Flip remain active\n"
        f"Risk caps unchanged: EBP {V66_MAX_EBP_RISK_PCT*100:.1f}% · Trend {V66_MAX_TREND_RISK_PCT*100:.1f}% · "
        f"Structure {V66_MAX_STRUCTURE_RISK_PCT*100:.1f}% · Flip {V66_FLIP_RISK_PCT*100:.1f}%\n"
        f"Leverage {LEVERAGE}x · max positions {MAX_ACTIVE_POSITIONS} · max same side {V6_MAX_SAME_SIDE}\n"
        f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}"
    )


# ============================================================
# V6.8 — PRE-ARM PRECISION ENGINE
# ============================================================
# Built from the observed V6.7 scan sequence:
# - strong setups often became Q99/Q100 only AFTER price had already run;
# - Q100 mixed setup quality with entry quality;
# - BTC context could be stale/inconsistent at execution;
# - too many useful near-setups were thrown away instead of being prepared.
#
# V6.8 separates:
#   SQ = setup quality
#   EQ = entry/location quality NOW
#   RR = usable reward room before nearby opposing structure
# and adds:
#   PRE-ARM -> SCOUT trigger -> PRECISION entry state flow
#   fresh BTC 15M execution veto immediately before every order
#   small-risk SCOUT entries for strong early setups
#   shadow tracking of rejected/near setups for later evidence
#
# The bot still uses only 4H + 15M chart logic. Live ticker is execution only.

SNIPER_VERSION = "6.8.0"

V68_PRECISION_MIN_SQ = float(os.getenv("V68_PRECISION_MIN_SQ", "86"))
V68_PRECISION_MIN_EQ = float(os.getenv("V68_PRECISION_MIN_EQ", "72"))
V68_PRECISION_MIN_RR = float(os.getenv("V68_PRECISION_MIN_RR", "1.45"))
V68_SCOUT_MIN_SQ = float(os.getenv("V68_SCOUT_MIN_SQ", "88"))
V68_SCOUT_MIN_EQ = float(os.getenv("V68_SCOUT_MIN_EQ", "78"))
V68_SCOUT_MIN_RR = float(os.getenv("V68_SCOUT_MIN_RR", "1.55"))
V68_SCOUT_RISK_PCT = float(os.getenv("V68_SCOUT_RISK_PCT", "0.005"))
V68_SCOUT_MIN_TRIGGER_SCORE = int(os.getenv("V68_SCOUT_MIN_TRIGGER_SCORE", "3"))
V68_MAX_LIVE_DRIFT_ATR = float(os.getenv("V68_MAX_LIVE_DRIFT_ATR", "0.22"))
V68_MAX_ENTRY_EXT_ATR = float(os.getenv("V68_MAX_ENTRY_EXT_ATR", "0.62"))
V68_BTC_EXECUTION_VETO = os.getenv("V68_BTC_EXECUTION_VETO", "true").lower() == "true"
V68_REQUIRE_BTC_MOMENTUM_NOT_AGAINST = os.getenv("V68_REQUIRE_BTC_MOMENTUM_NOT_AGAINST", "true").lower() == "true"
V68_SHADOW_ENABLED = os.getenv("V68_SHADOW_ENABLED", "true").lower() == "true"
V68_SHADOW_FILE = os.getenv("V68_SHADOW_FILE", "v68_shadow_learning.jsonl")
V68_REPORT_MIN = int(os.getenv("V68_REPORT_MIN", "15"))

_v68_base_candidate = _v6_candidate
_v68_base_sizing_profile = sizing_profile
_v68_prearms = {}
_v68_shadow = {}
_v68_last_report = 0.0


def _v68_btc_snapshot(d):
    """Fresh BTC state. 15M direction is the execution clock."""
    try:
        h4 = _v6_4h_context(V6_BTC_SYMBOL)
        cdf = _v6_closed(_v6_df(V6_BTC_SYMBOL, "15m"))
        if len(cdf) < 3:
            return {"ok": False, "d15": 0, "h4": 0, "mom": 0, "note": "BTC15 unavailable"}
        a, p = cdf.iloc[-1], cdf.iloc[-2]
        c = safe_float(a["close"])
        e21 = safe_float(a["ema_slow"])
        d15 = 1 if c > e21 else -1 if c < e21 else 0
        hist, hp = safe_float(a["macd_hist"]), safe_float(p["macd_hist"])
        mom = 1 if hist > hp else -1 if hist < hp else 0
        h4d = int(h4.get("dir", 0))
        aligned = d15 == d
        mom_ok = (mom != -d) if V68_REQUIRE_BTC_MOMENTUM_NOT_AGAINST else True
        ok = aligned and mom_ok
        return {
            "ok": ok, "d15": d15, "h4": h4d, "mom": mom,
            "note": f"BTC fresh 4H/15M/mom {h4d:+d}/{d15:+d}/{mom:+d}"
        }
    except Exception as e:
        # Execution safety is fail-closed: if we cannot verify BTC now, no order.
        return {"ok": False, "d15": 0, "h4": 0, "mom": 0,
                "note": f"BTC fresh check unavailable ({type(e).__name__})"}


def _v68_pivot_reward_room(symbol, side, live, risk, final_r):
    """Estimate free room to a meaningful recent opposing pivot, expressed in R."""
    if live <= 0 or risk <= 0:
        return 0.0, 0.0
    d = 1 if side == "long" else -1
    levels = []
    try:
        c15 = _v6_closed(_v6_df(symbol, "15m")).tail(70).reset_index(drop=True)
        ph, pl = _v6_pivots(c15, 2, 2)
        if d == 1:
            levels += [float(v) for _, v in ph if float(v) > live]
        else:
            levels += [float(v) for _, v in pl if float(v) < live]
    except Exception:
        pass
    try:
        c4 = _v6_closed(_v6_df(symbol, "4h")).tail(45).reset_index(drop=True)
        ph4, pl4 = _v6_pivots(c4, 2, 2)
        if d == 1:
            levels += [float(v) for _, v in ph4 if float(v) > live]
        else:
            levels += [float(v) for _, v in pl4 if float(v) < live]
    except Exception:
        pass

    if levels:
        obstacle = min(levels) if d == 1 else max(levels)
        obstacle_r = d * (obstacle - live) / risk
        # Ignore microscopic pivots that are effectively inside spread/noise.
        if obstacle_r >= 0.35:
            return max(0.0, min(final_r, obstacle_r)), obstacle
    return float(final_r), 0.0


def _v68_entry_metrics(signal, live=None):
    """Return independent SQ, EQ and reward-room metrics."""
    side = signal.get("side")
    if side not in ("long", "short"):
        return {"sq": safe_float(signal.get("score")), "eq": 0.0, "rr": 0.0,
                "net_rr": 0.0, "live": 0.0, "ext": 99.0, "drift_atr": 99.0}
    d = 1 if side == "long" else -1
    symbol = signal["symbol"]
    try:
        cdf = _v6_closed(_v6_df(symbol, "15m"))
        a = cdf.iloc[-1]
        atr = max(safe_float(a["atr"]), 1e-12)
        c = safe_float(a["close"])
        e21 = safe_float(a["ema_slow"])
        if live is None:
            live = safe_float(exchange.fetch_ticker(symbol).get("last"), c)
        live = safe_float(live, c)
        sig_px = safe_float(signal.get("price"), c)
        ext = d * (live - e21) / atr
        drift_atr = d * (live - sig_px) / atr

        eq = 100.0
        # Best entries are close to the reset/retest, not after the impulse.
        if ext > 0.20:
            eq -= min(48.0, (ext - 0.20) * 70.0)
        elif ext < -0.12:
            eq -= min(30.0, abs(ext + 0.12) * 55.0)
        if drift_atr > 0.05:
            eq -= min(30.0, (drift_atr - 0.05) * 85.0)

        h4_ext = safe_float(signal.get("h4_ext_atr"))
        if h4_ext > 0.65:
            eq -= min(28.0, (h4_ext - 0.65) * 55.0)

        rp = safe_float(signal.get("h4_range_pos"), 0.5)
        edge = rp if d == 1 else 1.0-rp
        if edge > 0.80:
            eq -= min(30.0, (edge - 0.80) * 120.0)

        # Reward FVG/retest entries that are still physically near their zone.
        z = signal.get("fvg_zone")
        if z and isinstance(z, (tuple, list)) and len(z) == 2:
            lo, hi = safe_float(z[0]), safe_float(z[1])
            if lo <= live <= hi:
                eq += 5.0

        risk = safe_float(signal.get("risk_distance"))
        structural = safe_float(signal.get("structural_stop"))
        if structural > 0:
            risk = abs(live-structural)
        if risk <= 0:
            risk = max(0.55*atr, 1e-12)

        final_r = safe_float(signal.get("final_r"), 2.0) or 2.0
        rr, obstacle = _v68_pivot_reward_room(symbol, side, live, risk, final_r)
        fee_r = live * (2*TAKER_FEE_RATE + SLIPPAGE_RATE) / max(risk, 1e-12)
        net_rr = max(0.0, rr - fee_r)
        eq = max(0.0, min(100.0, eq))

        return {"sq": safe_float(signal.get("score")), "eq": eq, "rr": rr,
                "net_rr": net_rr, "obstacle": obstacle, "live": live,
                "ext": ext, "drift_atr": drift_atr, "risk": risk, "atr": atr}
    except Exception:
        return {"sq": safe_float(signal.get("score")), "eq": 0.0, "rr": 0.0,
                "net_rr": 0.0, "live": 0.0, "ext": 99.0, "drift_atr": 99.0}


def _v68_scout_trigger(signal):
    """Early 15M trigger: price action can lead MACD, but must not be blind."""
    try:
        side = signal["side"]
        d = 1 if side == "long" else -1
        cdf = _v6_closed(_v6_df(signal["symbol"], "15m"))
        a, p = cdf.iloc[-1], cdf.iloc[-2]
        c, o = safe_float(a["close"]), safe_float(a["open"])
        pc = safe_float(p["close"])
        e21 = safe_float(a["ema_slow"])
        rsi, prsi = safe_float(a["sniper_rsi"], 50), safe_float(p["sniper_rsi"], 50)
        hist, hp = safe_float(a["macd_hist"]), safe_float(p["macd_hist"])
        body_dir = (c > o) if d == 1 else (c < o)
        close_progress = (c > pc) if d == 1 else (c < pc)
        rsi_turn = (rsi > prsi) if d == 1 else (rsi < prsi)
        ema_side = (c > e21) if d == 1 else (c < e21)
        macd_not_worsening = (hist >= hp) if d == 1 else (hist <= hp)
        score = sum(map(int, (body_dir, close_progress, rsi_turn, ema_side, macd_not_worsening)))
        return score >= V68_SCOUT_MIN_TRIGGER_SCORE, score, (
            f"15M early {score}/5 body={int(body_dir)} progress={int(close_progress)} "
            f"rsi={int(rsi_turn)} ema={int(ema_side)} macd={int(macd_not_worsening)}"
        )
    except Exception as e:
        return False, 0, f"early-trigger-{type(e).__name__}"


def _v68_shadow_write(event):
    if not V68_SHADOW_ENABLED:
        return
    try:
        import json
        with open(V68_SHADOW_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _v68_shadow_track(signal, metrics):
    """Track near/rejected setups without risking money."""
    if not V68_SHADOW_ENABLED or not signal or signal.get("side") not in ("long", "short"):
        return
    symbol, side = signal["symbol"], signal["side"]
    key = f"{symbol}|{side}|{int(signal.get('signal_bar_ts',0))}|{signal.get('strategy','-')}"
    now = time.time()
    live = safe_float(metrics.get("live"))
    risk = safe_float(metrics.get("risk"))
    if live <= 0 or risk <= 0:
        return

    # Update existing observations for this symbol/side.
    for k, rec in list(_v68_shadow.items()):
        if rec["symbol"] != symbol or rec["side"] != side or rec.get("done"):
            continue
        d = 1 if side == "long" else -1
        r_now = d*(live-rec["entry"])/max(rec["risk"], 1e-12)
        rec["mfe"] = max(rec.get("mfe", r_now), r_now)
        rec["mae"] = min(rec.get("mae", r_now), r_now)
        age = now-rec["ts"]
        for sec, label in ((1800,"30m"),(3600,"1h"),(7200,"2h"),(14400,"4h")):
            if age >= sec and label not in rec["snapshots"]:
                rec["snapshots"][label] = round(r_now, 3)
                _v68_shadow_write({"type":"shadow_snapshot","key":k,"age":label,
                                   "r":round(r_now,3),"mfe":round(rec["mfe"],3),
                                   "mae":round(rec["mae"],3),"ts":int(now)})
        if age >= 14400:
            rec["done"] = True

    if key in _v68_shadow:
        return
    # Only collect setups that were at least plausible.
    if safe_float(metrics.get("sq")) < 75:
        return
    rec = {
        "symbol":symbol, "side":side, "strategy":signal.get("strategy"),
        "reason":str(signal.get("skip") or "READY"), "entry":live, "risk":risk,
        "sq":round(safe_float(metrics.get("sq")),1),
        "eq":round(safe_float(metrics.get("eq")),1),
        "rr":round(safe_float(metrics.get("net_rr")),2),
        "ts":now, "mfe":0.0, "mae":0.0, "snapshots":{}, "done":False,
    }
    _v68_shadow[key] = rec
    _v68_shadow_write({"type":"shadow_open","key":key,**{k:v for k,v in rec.items() if k not in ("snapshots","done")}})


def _v68_can_prearm(signal, metrics):
    if not signal or signal.get("side") not in ("long","short"):
        return False
    sq, eq = safe_float(metrics.get("sq")), safe_float(metrics.get("eq"))
    if sq < V68_SCOUT_MIN_SQ or eq < V68_SCOUT_MIN_EQ:
        return False
    sk = str(signal.get("skip") or "")
    # Never scout a mature/chased setup or a macro-location rejection.
    hard = ("extended", "too-far", "range-edge", "impulse-already-ran",
            "BTC", "quality-", "4H-no-clean", "4H-weak", "COLD")
    if any(x in sk for x in hard):
        return False
    eligible = (
        "momentum-not-confirmed" in sk or
        "TR-momentum-not-returned" in sk or
        "TR-wait-price-action-trigger" in sk or
        "TR-wait-reclaim-candle" in sk or
        "wait-15M-rejection" in sk
    )
    return eligible


def _v6_candidate(symbol):
    """V6.8 router wrapper: SQ/EQ/RR + PRE-ARM/SCOUT + fresh BTC scan gate."""
    base = _v68_base_candidate(symbol)
    if not base:
        return None
    if base.get("side") not in ("long","short"):
        return base

    metrics = _v68_entry_metrics(base)
    base["setup_quality"] = metrics["sq"]
    base["entry_quality"] = metrics["eq"]
    base["reward_room_r"] = metrics["rr"]
    base["net_reward_room_r"] = metrics["net_rr"]
    base["v68_ext_atr"] = metrics["ext"]
    base["v68_drift_atr"] = metrics["drift_atr"]
    base["v68_mode"] = "PRECISION"
    # Ranking quality now reflects both idea and price; SQ remains untouched.
    base["quality"] = 0.60*metrics["sq"] + 0.40*metrics["eq"]

    _v68_shadow_track(base, metrics)

    d = 1 if base["side"] == "long" else -1
    btc = _v68_btc_snapshot(d)
    base["btc_fresh_note"] = btc["note"]

    # A normal ready signal must also be a good entry NOW.
    if not base.get("skip") and base.get("entry_route") not in (None, "WAIT"):
        if metrics["sq"] < V68_PRECISION_MIN_SQ:
            base["skip"] = f"V68-SQ-{metrics['sq']:.0f}<{V68_PRECISION_MIN_SQ:.0f}"
        elif metrics["eq"] < V68_PRECISION_MIN_EQ:
            base["skip"] = f"V68-EQ-{metrics['eq']:.0f}<{V68_PRECISION_MIN_EQ:.0f}"
        elif metrics["net_rr"] < V68_PRECISION_MIN_RR:
            base["skip"] = f"V68-RR-{metrics['net_rr']:.2f}<{V68_PRECISION_MIN_RR:.2f}"
        elif metrics["ext"] > V68_MAX_ENTRY_EXT_ATR:
            base["skip"] = f"V68-entry-extended-{metrics['ext']:.2f}ATR"
        elif not btc["ok"]:
            base["skip"] = f"V68-BTC15-WAIT · {btc['note']}"
        if base.get("skip"):
            base["entry_route"] = "WAIT"
            return base
        base["entry_route"] = "PRECISION-" + str(base.get("entry_route"))
        return base

    # PRE-ARM a strong setup before the old full-confirmation point.
    if _v68_can_prearm(base, metrics):
        key = f"{symbol}|{base['side']}|{base.get('strategy','-')}"
        _v68_prearms[key] = {
            "ts": time.time(), "price": metrics["live"], "sq": metrics["sq"],
            "eq": metrics["eq"], "signal_bar_ts": int(base.get("signal_bar_ts",0))
        }
        trigger, trig_score, trig_note = _v68_scout_trigger(base)
        base["v68_trigger"] = trig_note
        base["v68_mode"] = "PRE-ARM"

        if (trigger and btc["ok"] and metrics["net_rr"] >= V68_SCOUT_MIN_RR
                and metrics["ext"] <= V68_MAX_ENTRY_EXT_ATR):
            base["skip"] = None
            base["entry_route"] = "SCOUT-EARLY"
            base["v68_mode"] = "SCOUT"
            base["confirmations"] = max(int(base.get("confirmations",0)), trig_score)
            return base

        wait_bits = []
        if not trigger: wait_bits.append(f"trigger {trig_score}/5")
        if not btc["ok"]: wait_bits.append("BTC15")
        if metrics["net_rr"] < V68_SCOUT_MIN_RR: wait_bits.append(f"RR {metrics['net_rr']:.2f}")
        base["skip"] = "V68-PRE-ARM · wait " + ", ".join(wait_bits or ["precision"])
        base["entry_route"] = "WAIT"
    return base


def evaluate_symbol(symbol, btc=None):
    return _v6_candidate(symbol)


def sizing_profile(signal):
    """Scout risks half-size; precision keeps repaired V6.7 caps."""
    if signal.get("v68_mode") == "SCOUT" or str(signal.get("entry_route","")).startswith("SCOUT"):
        return {"name":"V68-SCOUT","risk_pct":V68_SCOUT_RISK_PCT,"target_net_tp":V6_TARGET_NET}
    return _v68_base_sizing_profile(signal)


def sniper_portfolio_allows(signal, positions):
    """V6.8 portfolio gate uses true SQ and fresh BTC, not a blended display score."""
    side = signal.get("side")
    same = sum(1 for p in positions if position_side(p) == side)
    if same >= V6_MAX_SAME_SIDE:
        return False, f"max-{V6_MAX_SAME_SIDE}-same-side"

    d = 1 if side == "long" else -1
    btc = _v68_btc_snapshot(d)
    if not btc["ok"]:
        return False, f"fresh BTC15 veto: {btc['note']}"

    sq = safe_float(signal.get("setup_quality"), safe_float(signal.get("score")))
    streak = int(sniper_side_loss_streak.get(side, 0))
    # Scouts are not allowed to fight an active directional loss streak.
    if signal.get("v68_mode") == "SCOUT" and streak >= 2:
        return False, f"{side}-loss-streak-{streak}: scout disabled"
    if streak >= 2 and sq < 94:
        return False, f"{side}-loss-streak-{streak}: precision needs SQ94"

    trades = int(daily.get(f"{side}_trades", 0))
    wins = int(daily.get(f"{side}_wins", 0))
    net = safe_float(daily.get(f"{side}_net", 0))
    wr = wins/trades if trades else 1.0
    if trades >= V66_SIDE_COLD_MIN_TRADES and wr <= V66_SIDE_COLD_MAX_WR and net <= -V66_SIDE_COLD_MIN_LOSS:
        if signal.get("v68_mode") == "SCOUT" or sq < 96:
            return False, f"{side}-COLD {wins}/{trades} net ${net:.2f}: precision SQ96 only"
    return True, "OK"


def open_trade(signal):
    """V6.8: revalidate direction, price quality, RR and BTC immediately before order."""
    global protection_lock, _v62_last_entry_time
    symbol, side = signal["symbol"], signal["side"]
    d = 1 if side == "long" else -1

    acc = futures_account()
    equity, available = acc["total_equity"], acc["available"]
    reset_daily_if_needed(equity)
    if equity < MIN_EQUITY_USDT or available <= 0 or (not PAPER_TRADING and protection_lock):
        return False
    if time.time()-_v62_last_entry_time < V62_GLOBAL_ENTRY_GAP_MIN*60:
        return False

    # CRITICAL: do not trust scanner BTC state. Fetch it again right now.
    btc = _v68_btc_snapshot(d)
    if V68_BTC_EXECUTION_VETO and not btc["ok"]:
        tg(f"⛔ {symbol} {side.upper()} V6.8 BTC15 EXECUTION VETO\n{btc['note']}\nOrder cancelled before exchange submission.")
        return False

    live = safe_float(exchange.fetch_ticker(symbol).get("last"), signal.get("price"))
    metrics = _v68_entry_metrics(signal, live)
    min_eq = V68_SCOUT_MIN_EQ if signal.get("v68_mode") == "SCOUT" else V68_PRECISION_MIN_EQ
    min_rr = V68_SCOUT_MIN_RR if signal.get("v68_mode") == "SCOUT" else V68_PRECISION_MIN_RR
    if metrics["eq"] < min_eq:
        tg(f"⏭️ {symbol} {side.upper()} V6.8 ENTRY QUALITY BLOCK\nSQ {metrics['sq']:.0f} · EQ {metrics['eq']:.0f}<{min_eq:.0f} · RR {metrics['net_rr']:.2f}")
        return False
    if metrics["net_rr"] < min_rr:
        tg(f"⏭️ {symbol} {side.upper()} V6.8 RR BLOCK\nSQ {metrics['sq']:.0f} · EQ {metrics['eq']:.0f} · net RR {metrics['net_rr']:.2f}<{min_rr:.2f}")
        return False
    if metrics["drift_atr"] > V68_MAX_LIVE_DRIFT_ATR or metrics["ext"] > V68_MAX_ENTRY_EXT_ATR:
        tg(f"⏭️ {symbol} {side.upper()} V6.8 NO CHASE\nDrift {metrics['drift_atr']:.2f}ATR · extension {metrics['ext']:.2f}ATR")
        return False

    # Keep the live 4H location check for non-EBP routes.
    if signal.get("strategy") != "EBP-FVG":
        loc_ok, loc_note, _ = _v62_live_location_ok(symbol, side, live)
        if not loc_ok:
            tg(f"⏭️ {symbol} {side.upper()} V6.8 LOCATION BLOCK\n{loc_note}")
            return False

    tick = safe_float(get_instrument(symbol).get("tickSize"), 0.0001)
    levels = _v6_levels(signal, live, tick)
    if not levels:
        return False
    sl, t1, t2, t3, risk = levels

    # Final RR check from the ACTUAL proposed stop, including estimated fees/slippage.
    final_r = safe_float(signal.get("final_r"), V6_FINAL_R) or V6_FINAL_R
    room_r, obstacle = _v68_pivot_reward_room(symbol, side, live, risk, final_r)
    fee_r = live*(2*TAKER_FEE_RATE + SLIPPAGE_RATE)/max(risk,1e-12)
    actual_net_rr = max(0.0, room_r-fee_r)
    if actual_net_rr < min_rr:
        tg(f"⏭️ {symbol} {side.upper()} V6.8 FINAL RR BLOCK\nNet room {actual_net_rr:.2f}R < {min_rr:.2f}R")
        return False

    profile = sizing_profile(signal)
    sizing = calculate_contracts_v5(symbol, live, sl, equity, available,
                                    profile["risk_pct"], profile["target_net_tp"], final_r)
    if not sizing or sizing["margin"] > available*AVAILABLE_MARGIN_BUFFER:
        return False

    before_ids = {position_id(p) for p in get_positions() if position_id(p)}
    oklev, levres = set_leverage(symbol, LEVERAGE)
    if not oklev:
        tg(f"⛔ LEVERAGE FAILED\n{symbol}\n{levres}")
        return False

    mode = signal.get("v68_mode", "PRECISION")
    tag = "ps68c" if mode == "SCOUT" else "ps68p"
    payload = {
        "instId": blofin_inst_id(symbol), "marginMode": MARGIN_MODE, "positionSide": "net",
        "side": "buy" if side == "long" else "sell", "orderType": "market",
        "size": str(sizing["contracts"]),
        "tpTriggerPrice": str(t3), "tpOrderPrice": "-1", "tpTriggerPriceType": "mark",
        "slTriggerPrice": str(sl), "slOrderPrice": "-1", "slTriggerPriceType": "mark",
        "clientOrderId": f"{tag}{int(time.time()*1000)%1000000000}",
    }
    if BROKER_ID:
        payload["brokerId"] = BROKER_ID

    msg = (
        f"{'🧪 PAPER' if PAPER_TRADING else '🎯 V6.8 '+mode}\n"
        f"{symbol} {side.upper()} · {signal.get('entry_route')}\n"
        f"SQ {metrics['sq']:.0f}/100 · EQ {metrics['eq']:.0f}/100 · NET RR {actual_net_rr:.2f}R\n"
        f"{btc['note']}\n"
        f"Entry {live:.8g} · SL {sl:.8g} · TP {t3:.8g}\n"
        f"Risk {profile['risk_pct']*100:.2f}% · expected final net ~${sizing['expected_net_tp_usdt']:.2f}\n"
        f"Notional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x"
    )

    if PAPER_TRADING:
        tg(msg)
        _v62_last_entry_time = time.time()
        return True

    resp = native("private_post_trade_order", payload)
    if not native_ok(resp):
        err = response_error(resp)
        if "152012" in err:
            protection_lock = True
        tg(f"❌ V6.8 ORDER FAILED\n{symbol} {side.upper()}\n{err}")
        return False

    _v62_last_entry_time = time.time()
    tg(msg)
    new_pos = find_new_position(symbol, before_ids, timeout=8)
    if new_pos is None:
        tg(f"⚠️ {symbol}: V6.8 order accepted, position not visible yet.")
        return True

    pid = position_id(new_pos)
    actual_entry = position_entry(new_pos) or live
    actual_risk = abs(actual_entry-sl) or risk
    if pid:
        sniper_managed_positions.add(pid)
        _v6_position_meta[pid] = {
            "risk":actual_risk, "planned_risk":risk, "structural_sl":sl,
            "tp1":t1, "tp2":t2, "tp3":t3, "opened":time.time(),
            "route":signal.get("entry_route"), "side":side, "final_r":final_r,
            "strategy":signal.get("strategy"), "planned_entry":live,
            "actual_entry":actual_entry, "v68_mode":mode,
            "sq":metrics["sq"], "eq":metrics["eq"], "net_rr":actual_net_rr,
        }

    if has_live_protection(new_pos)[0]:
        tg(f"🛡️ V6.8 PROTECTION CONFIRMED\n{symbol} {side.upper()} · {mode}\nSL/TP visible.")
        return True

    last = safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side, sl, t3, last):
        ok, res = place_tpsl_for_position(new_pos, sl, t3, client_tag=tag)
        if ok:
            tg(f"🛡️ V6.8 FALLBACK PROTECTION CONFIRMED\n{symbol}")
            return True
        protection_lock = True
        tg(f"🚨 V6.8 PROTECTION FAILURE\n{symbol}\n{res}\nNEW TRADES PAUSED.")
        return True

    protection_lock = True
    tg(f"🚨 {symbol}: V6.8 position exists but protection could not be verified.\nNEW TRADES PAUSED.")
    return True


def maybe_score_report(rows):
    """V6.8 report shows why a high-SQ setup is or is not actually tradable."""
    global _v68_last_report
    from collections import Counter
    now = time.time()
    interesting = [r for r in rows if r]
    if not interesting or now-_v68_last_report < V68_REPORT_MIN*60:
        return
    _v68_last_report = now

    ready = [r for r in interesting if r.get("side") and not r.get("skip")
             and r.get("entry_route") not in (None,"WAIT")]
    pre = [r for r in interesting if str(r.get("skip","")).startswith("V68-PRE-ARM")]
    blockers = Counter()
    for r in interesting:
        sk = str(r.get("skip") or "READY")
        if "PRE-ARM" in sk: bucket = "pre-arm"
        elif "BTC" in sk: bucket = "BTC15"
        elif "RR-" in sk or "RR BLOCK" in sk: bucket = "reward room"
        elif "EQ-" in sk or "extended" in sk or "too-far" in sk: bucket = "entry/location"
        elif "4H" in sk: bucket = "4H"
        elif "momentum" in sk: bucket = "momentum"
        elif "EBP" in sk: bucket = "EBP"
        else: bucket = sk.split("-")[0][:20]
        blockers[bucket] += 1

    ranked = sorted(
        interesting,
        key=lambda r: (
            1 if not r.get("skip") else 0,
            1 if str(r.get("skip","")).startswith("V68-PRE-ARM") else 0,
            safe_float(r.get("setup_quality"), r.get("score")),
            safe_float(r.get("entry_quality"))
        ),
        reverse=True
    )[:8]

    lines = [
        f"🧠 STRUCTURE FUSION v{SNIPER_VERSION} · PRE-ARM PRECISION",
        f"Ready {len(ready)} · Pre-armed {len(pre)} · scanned {len(interesting)}",
    ]
    if blockers:
        lines.append("Main waits: " + " · ".join(f"{k} {v}" for k,v in blockers.most_common(4)))
    for r in ranked:
        status = "🟢 " + str(r.get("entry_route")) if not r.get("skip") else "🟡 PRE-ARM" if str(r.get("skip","")).startswith("V68-PRE-ARM") else "⏩ " + str(r.get("skip"))
        lines.append(
            f"{r.get('symbol')} {str(r.get('side') or '-').upper()} {r.get('strategy','-')} "
            f"SQ{safe_float(r.get('setup_quality'),r.get('score')):.0f} "
            f"EQ{safe_float(r.get('entry_quality')):.0f} "
            f"RR{safe_float(r.get('net_reward_room_r')):.2f} {status}"
        )
    tg("\n".join(lines))


def startup_diagnostics():
    acc = futures_account()
    reset_daily_if_needed(acc["total_equity"])
    tg(
        f"🧠 PRECISION STRUCTURE FUSION STARTED v{SNIPER_VERSION}\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"TIMEFRAMES: 4H + 15M ONLY\n"
        f"V6.8 PRE-ARM PRECISION\n"
        f"• SQ setup quality is separate from EQ entry quality\n"
        f"• Reward room is checked in R before every order\n"
        f"• Fresh BTC 15M direction is a HARD execution veto\n"
        f"• Strong near-setups can PRE-ARM and trigger a 0.5% SCOUT entry\n"
        f"• Mature/extended setups are cancelled, never chased\n"
        f"• Rejected/near setups are shadow-tracked at 30m/1h/2h/4h\n"
        f"Precision: SQ>={V68_PRECISION_MIN_SQ:.0f} EQ>={V68_PRECISION_MIN_EQ:.0f} RR>={V68_PRECISION_MIN_RR:.2f}R\n"
        f"Scout: SQ>={V68_SCOUT_MIN_SQ:.0f} EQ>={V68_SCOUT_MIN_EQ:.0f} RR>={V68_SCOUT_MIN_RR:.2f}R · risk {V68_SCOUT_RISK_PCT*100:.2f}%\n"
        f"Leverage {LEVERAGE}x · max positions {MAX_ACTIVE_POSITIONS} · max same side {V6_MAX_SAME_SIDE}\n"
        f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}"
    )

# ============================================================
# LIQUIDITY DAYTRADER v1.0 — CRT / MSS / IFVG / BREAKER
# ============================================================
# NEW STRATEGY ENGINE. The old V6 strategy code above is retained ONLY as the
# battle-tested BloFin execution/protection/PnL shell. None of the V6 signal
# engines are called after the overrides in this section.
#
# Design:
#   CORE : 1H parent Range Candle -> 5M sweep/reclaim -> MSS -> IFVG/Breaker retest
#   FAST : 15M parent Range Candle -> 1M sweep/reclaim -> MSS -> IFVG/Breaker retest
#   CONT : true range breakout -> pullback/retest -> continuation trigger
#
# Price-action families from the supplied cheat sheet are not treated as 20
# independent magic patterns. They are grouped into objective mechanics:
#   - QM / 2B / fakeout / V-tail -> liquidity sweep + reclaim + MSS
#   - Flag / continuation QM     -> breakout + retest continuation
#   - Compression                -> context/quality bonus, never a standalone entry
#
# IMPORTANT: the social-media "99% winrate" claim is NOT encoded as an
# assumption. Every setup still needs positive net R:R and is risk-capped.

SNIPER_VERSION = "LDT-1.0.0"

# New bot limits — intentionally conservative while the new edge is validated.
MAX_ACTIVE_POSITIONS = int(os.getenv("LDT_MAX_ACTIVE_POSITIONS", "2"))
LDT_MAX_SAME_SIDE = int(os.getenv("LDT_MAX_SAME_SIDE", "1"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("LDT_DAILY_LOSS_LIMIT_PCT", "4.0"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("LDT_MAX_CONSECUTIVE_LOSSES", "3"))
LOSS_PAUSE_MIN = int(os.getenv("LDT_LOSS_PAUSE_MIN", "90"))

LDT_CORE_RISK = float(os.getenv("LDT_CORE_RISK", "0.008"))       # 0.80%
LDT_FAST_RISK = float(os.getenv("LDT_FAST_RISK", "0.0045"))      # 0.45%
LDT_CONT_RISK = float(os.getenv("LDT_CONT_RISK", "0.0065"))      # 0.65%
LDT_ELITE_RISK = float(os.getenv("LDT_ELITE_RISK", "0.010"))     # hard ceiling 1.00%
LDT_CORE_TARGET_NET = float(os.getenv("LDT_CORE_TARGET_NET", "0.75"))
LDT_FAST_TARGET_NET = float(os.getenv("LDT_FAST_TARGET_NET", "0.50"))
LDT_MIN_NET_RR = float(os.getenv("LDT_MIN_NET_RR", "1.65"))
LDT_MIN_SETUP_QUALITY = float(os.getenv("LDT_MIN_SETUP_QUALITY", "72"))
LDT_MIN_ENTRY_QUALITY = float(os.getenv("LDT_MIN_ENTRY_QUALITY", "68"))
LDT_REPORT_MIN = int(os.getenv("LDT_REPORT_MIN", "15"))
LDT_MAX_SIGNAL_AGE_MIN = int(os.getenv("LDT_MAX_SIGNAL_AGE_MIN", "180"))
LDT_SWEEP_MIN_ATR = float(os.getenv("LDT_SWEEP_MIN_ATR", "0.03"))
LDT_SWEEP_MAX_ATR = float(os.getenv("LDT_SWEEP_MAX_ATR", "0.90"))
LDT_STOP_BUFFER_ATR = float(os.getenv("LDT_STOP_BUFFER_ATR", "0.08"))
LDT_MAX_STOP_ATR = float(os.getenv("LDT_MAX_STOP_ATR", "1.45"))
LDT_MIN_STOP_ATR = float(os.getenv("LDT_MIN_STOP_ATR", "0.18"))
LDT_ZONE_TOL_ATR = float(os.getenv("LDT_ZONE_TOL_ATR", "0.08"))
LDT_DISPLACEMENT_ATR = float(os.getenv("LDT_DISPLACEMENT_ATR", "0.42"))
LDT_VOL_MULT = float(os.getenv("LDT_VOL_MULT", "1.05"))
LDT_BREAKOUT_ATR = float(os.getenv("LDT_BREAKOUT_ATR", "0.10"))
LDT_BREAKOUT_BODY_ATR = float(os.getenv("LDT_BREAKOUT_BODY_ATR", "0.55"))
LDT_LIVE_DRIFT_ATR = float(os.getenv("LDT_LIVE_DRIFT_ATR", "0.18"))
LDT_BTC_CONTEXT_WEIGHT = float(os.getenv("LDT_BTC_CONTEXT_WEIGHT", "5"))
LDT_REQUIRE_1M_FAST_TRIGGER = os.getenv("LDT_REQUIRE_1M_FAST_TRIGGER", "true").lower() == "true"
LDT_ENABLE_FAST = os.getenv("LDT_ENABLE_FAST", "true").lower() == "true"
LDT_ENABLE_CONTINUATION = os.getenv("LDT_ENABLE_CONTINUATION", "true").lower() == "true"
LDT_NEW_RISK_EPOCH_LOCAL = os.getenv("LDT_RISK_EPOCH_LOCAL", "2026-09-30T12:06:00+02:00").strip()
if LDT_NEW_RISK_EPOCH_LOCAL:
    V6_RISK_EPOCH_LOCAL = LDT_NEW_RISK_EPOCH_LOCAL

_ldt_meta = {}
_ldt_last_report = 0.0
_ldt_last_state = {}


def _ldt_df(symbol, tf, limit=None):
    rows = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit or CANDLE_LIMIT)
    if len(rows) < 30:
        raise RuntimeError(f"not enough {tf} candles")
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().reset_index(drop=True)
    pc = df["close"].shift(1)
    tr = pd.concat([(df["high"]-df["low"]), (df["high"]-pc).abs(), (df["low"]-pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["volma"] = df["volume"].rolling(20).mean()
    return df


def _ldt_closed(df):
    # CCXT/BloFin normally returns the live candle as the final row.
    return df.iloc[:-1].copy().reset_index(drop=True) if len(df) > 1 else df.copy()


def _ldt_pivots(df, left=2, right=2):
    highs, lows = [], []
    for i in range(left, len(df)-right):
        h = safe_float(df.iloc[i]["high"]); l = safe_float(df.iloc[i]["low"])
        if h >= max(safe_float(x) for x in df.iloc[i-left:i+right+1]["high"]): highs.append((i,h))
        if l <= min(safe_float(x) for x in df.iloc[i-left:i+right+1]["low"]): lows.append((i,l))
    return highs, lows


def _ldt_compression(df, side):
    """Compression from the cheat sheet: shrinking bars pushing toward a level."""
    if len(df) < 6: return False
    x = df.tail(5)
    ranges = (x["high"]-x["low"]).values
    shrinking = sum(ranges[i] <= ranges[i-1]*1.08 for i in range(1,len(ranges))) >= 3
    closes = x["close"].values
    directional = closes[-1] > closes[0] if side == "long" else closes[-1] < closes[0]
    return bool(shrinking and directional)


def _ldt_fvgs(df, start=2, end=None):
    """Return bullish/bearish 3-candle FVGs."""
    out=[]; end = len(df) if end is None else min(end, len(df))
    for i in range(max(2,start), end):
        a, c = df.iloc[i-2], df.iloc[i]
        if safe_float(c["low"]) > safe_float(a["high"]):
            out.append({"dir":1,"lo":safe_float(a["high"]),"hi":safe_float(c["low"]),"i":i})
        if safe_float(c["high"]) < safe_float(a["low"]):
            out.append({"dir":-1,"lo":safe_float(c["high"]),"hi":safe_float(a["low"]),"i":i})
    return out


def _ldt_ifvg(df, d, sweep_i, mss_i):
    """Actual inversion FVG: an opposite FVG that is closed through after the MSS."""
    gaps = _ldt_fvgs(df, max(2,sweep_i-8), mss_i+1)
    candidates=[]
    for g in gaps:
        if g["dir"] == d: continue
        later = df.iloc[g["i"]:mss_i+1]
        if d == 1:
            inverted = any(safe_float(v) > g["hi"] for v in later["close"])
        else:
            inverted = any(safe_float(v) < g["lo"] for v in later["close"])
        if inverted: candidates.append(g)
    if not candidates: return None
    g=candidates[-1]
    return {"lo":g["lo"],"hi":g["hi"],"kind":"IFVG"}


def _ldt_directional_fvg(df, d, sweep_i, mss_i):
    gaps=[g for g in _ldt_fvgs(df,max(2,sweep_i),mss_i+2) if g["dir"]==d]
    if not gaps:return None
    g=gaps[-1]; return {"lo":g["lo"],"hi":g["hi"],"kind":"FVG"}


def _ldt_breaker(df, d, sweep_i, mss_i):
    """Last opposing candle before the displacement/MSS break."""
    lo=max(0,sweep_i-2); hi=min(mss_i+1,len(df))
    for i in range(hi-1,lo-1,-1):
        r=df.iloc[i]; bullish=safe_float(r["close"])>safe_float(r["open"])
        opposing = bullish if d==-1 else not bullish
        if opposing:
            body_lo=min(safe_float(r["open"]),safe_float(r["close"]))
            body_hi=max(safe_float(r["open"]),safe_float(r["close"]))
            return {"lo":body_lo,"hi":body_hi,"kind":"BREAKER","i":i}
    return None


def _ldt_zone_merge(a,b):
    if a and b:
        lo=max(a["lo"],b["lo"]); hi=min(a["hi"],b["hi"])
        if lo < hi:
            return {"lo":lo,"hi":hi,"kind":"IFVG+BREAKER","confluence":2}
        # No overlap: use the narrower zone; the second remains confluence evidence.
        wa=a["hi"]-a["lo"]; wb=b["hi"]-b["lo"]
        z=dict(a if wa<=wb else b); z["confluence"]=1; return z
    if a or b:
        z=dict(a or b); z["confluence"]=1; return z
    return None


def _ldt_cr_time_bonus(parent):
    """Key CRT times are a bonus, not a magical hard filter. Timezone: New York."""
    try:
        from zoneinfo import ZoneInfo
        dt=datetime.fromtimestamp(parent["ts"]/1000.0, ZoneInfo("America/New_York"))
        key_hours={1,3,5,9,13}
        return (5 if dt.hour in key_hours else 0), dt.strftime("%H:%M NY")
    except Exception:
        return 0,"time-n/a"


def _ldt_parent_candidates(symbol, tf):
    df=_ldt_closed(_ldt_df(symbol,tf,120))
    if len(df)<35:return []
    out=[]
    # Recent closed candles only. Older untouched parents are allowed but decay in quality.
    for j in range(max(20,len(df)-6),len(df)):
        r=df.iloc[j]; atr=safe_float(r["atr"])
        rng=safe_float(r["high"])-safe_float(r["low"])
        if atr<=0 or rng<=0:continue
        ratio=rng/atr
        if 0.55 <= ratio <= 2.20:
            out.append({"tf":tf,"ts":int(r["ts"]),"high":safe_float(r["high"]),"low":safe_float(r["low"]),
                        "mid":(safe_float(r["high"])+safe_float(r["low"]))/2,"atr":atr,
                        "range":rng,"ratio":ratio,"age":len(df)-1-j,"end_ts":int(r["ts"])+(3600000 if tf=="1h" else 900000 if tf=="15m" else 0)})
    return list(reversed(out))


def _ldt_find_sweep_mss(symbol, parent, ltf):
    df=_ldt_closed(_ldt_df(symbol,ltf,220))
    if len(df)<40:return None
    atr=safe_float(df.iloc[-1]["atr"])
    if atr<=0:return None
    # Only lower-timeframe candles after the parent opened; this also keeps stale ranges out.
    sub=df[df["ts"]>=parent.get("end_ts",parent["ts"])].copy().reset_index(drop=True)
    if len(sub)<8:return None
    ph,pl=parent["high"],parent["low"]
    best=None
    for i in range(max(3,len(sub)-35),len(sub)):
        r=sub.iloc[i]
        # Sweep high and close back inside -> SHORT hypothesis.
        for d in (-1,1):
            if d==-1:
                depth=(safe_float(r["high"])-ph)/atr
                swept=safe_float(r["high"])>ph and safe_float(r["close"])<ph
            else:
                depth=(pl-safe_float(r["low"]))/atr
                swept=safe_float(r["low"])<pl and safe_float(r["close"])>pl
            if not swept or depth<LDT_SWEEP_MIN_ATR or depth>LDT_SWEEP_MAX_ATR:continue
            pre=sub.iloc[max(0,i-7):i]
            if len(pre)<3:continue
            mss_level=(safe_float(pre["low"].min()) if d==-1 else safe_float(pre["high"].max()))
            mss_i=None
            for k in range(i+1,len(sub)):
                c=safe_float(sub.iloc[k]["close"])
                if (d==-1 and c<mss_level) or (d==1 and c>mss_level):
                    mss_i=k;break
            if mss_i is None:continue
            mr=sub.iloc[mss_i]; mat=safe_float(mr["atr"],atr) or atr
            body=abs(safe_float(mr["close"])-safe_float(mr["open"]))/max(mat,1e-12)
            vol=safe_float(mr["volume"]); vma=safe_float(mr["volma"])
            displacement=body>=LDT_DISPLACEMENT_ATR
            vol_ok=(vma<=0 or vol>=vma*LDT_VOL_MULT)
            # We accept clean structure shift with either displacement or volume, strongest with both.
            if not (displacement or vol_ok):continue
            actual_i0 = max(0, len(df)-len(sub))
            sweep_abs=actual_i0+i; mss_abs=actual_i0+mss_i
            ifvg=_ldt_ifvg(df,d,sweep_abs,mss_abs)
            fvg=_ldt_directional_fvg(df,d,sweep_abs,mss_abs)
            breaker=_ldt_breaker(df,d,sweep_abs,mss_abs)
            zone=_ldt_zone_merge(ifvg or fvg,breaker)
            if not zone:continue
            sweep_ext=safe_float(r["high"] if d==-1 else r["low"])
            age_bars=len(sub)-1-mss_i
            setup={"d":d,"side":"long" if d==1 else "short","sweep_i":i,"mss_i":mss_i,
                   "mss_level":mss_level,"sweep_extreme":sweep_ext,"sweep_depth_atr":depth,
                   "body_atr":body,"vol_ok":vol_ok,"displacement":displacement,"zone":zone,
                   "ifvg":ifvg,"fvg":fvg,"breaker":breaker,"age_bars":age_bars,"df":df,"sub":sub,"atr":atr}
            if best is None or (zone.get("confluence",0),-age_bars)>(best["zone"].get("confluence",0),-best["age_bars"]):
                best=setup
    return best


def _ldt_break_retest(symbol,parent,ltf):
    if not LDT_ENABLE_CONTINUATION:return None
    df=_ldt_closed(_ldt_df(symbol,ltf,180)); atr=safe_float(df.iloc[-1]["atr"])
    if len(df)<35 or atr<=0:return None
    sub=df[df["ts"]>=parent.get("end_ts",parent["ts"])].copy().reset_index(drop=True)
    if len(sub)<8:return None
    for i in range(max(2,len(sub)-24),len(sub)):
        r=sub.iloc[i]; body=abs(safe_float(r["close"])-safe_float(r["open"]))/atr
        vma=safe_float(r["volma"]); vol=safe_float(r["volume"])
        for d in (1,-1):
            level=parent["high"] if d==1 else parent["low"]
            broke=(safe_float(r["close"])>level+LDT_BREAKOUT_ATR*atr if d==1 else safe_float(r["close"])<level-LDT_BREAKOUT_ATR*atr)
            if not broke or body<LDT_BREAKOUT_BODY_ATR:continue
            if vma>0 and vol<vma*LDT_VOL_MULT:continue
            # Retest after the breakout: wick into/through level, close back on breakout side.
            for k in range(i+1,len(sub)):
                q=sub.iloc[k]
                touched=(safe_float(q["low"])<=level+LDT_ZONE_TOL_ATR*atr if d==1 else safe_float(q["high"])>=level-LDT_ZONE_TOL_ATR*atr)
                held=(safe_float(q["close"])>level if d==1 else safe_float(q["close"])<level)
                if touched and held:
                    breaker=_ldt_breaker(df,d,max(0,len(df)-len(sub)+i),max(0,len(df)-len(sub)+k))
                    z=breaker or {"lo":level-LDT_ZONE_TOL_ATR*atr,"hi":level+LDT_ZONE_TOL_ATR*atr,"kind":"RANGE-RETEST","confluence":1}
                    return {"d":d,"side":"long" if d==1 else "short","break_i":i,"retest_i":k,
                            "level":level,"zone":z,"atr":atr,"df":df,"sub":sub,"age_bars":len(sub)-1-k,
                            "body_atr":body,"vol_ok":True,"compression":_ldt_compression(sub.iloc[max(0,i-6):i+1],"long" if d==1 else "short")}
    return None


def _ldt_micro_trigger(symbol, side, tf):
    """Last-mile trigger: do not catch a falling knife on the zone touch."""
    df=_ldt_closed(_ldt_df(symbol,tf,100))
    if len(df)<25:return False,0,"no-data"
    a,p=df.iloc[-1],df.iloc[-2]; d=1 if side=="long" else -1
    body_ok=(safe_float(a["close"])>safe_float(a["open"])) if d==1 else (safe_float(a["close"])<safe_float(a["open"]))
    progress=(safe_float(a["close"])>safe_float(p["high"])) if d==1 else (safe_float(a["close"])<safe_float(p["low"]))
    ema_ok=(safe_float(a["close"])>safe_float(a["ema21"])) if d==1 else (safe_float(a["close"])<safe_float(a["ema21"]))
    wick=(min(safe_float(a["open"]),safe_float(a["close"]))-safe_float(a["low"])) if d==1 else (safe_float(a["high"])-max(safe_float(a["open"]),safe_float(a["close"])))
    body=abs(safe_float(a["close"])-safe_float(a["open"]))
    reject=wick>=max(body*0.7,1e-12)
    score=sum(map(int,(body_ok,progress,ema_ok,reject)))
    return score>=2,score,f"{tf} trigger {score}/4 body={int(body_ok)} break={int(progress)} ema={int(ema_ok)} reject={int(reject)}"


def _ldt_btc_context(side):
    """BTC is context, not a binary permission switch."""
    try:
        d=1 if side=="long" else -1
        df=_ldt_closed(_ldt_df(BTC_SYMBOL,"15m",100)); a,p=df.iloc[-1],df.iloc[-2]
        direction=1 if safe_float(a["close"])>safe_float(a["ema21"]) else -1
        slope=1 if safe_float(a["ema21"])>safe_float(p["ema21"]) else -1
        score=(1 if direction==d else -1)+(1 if slope==d else -1)
        return score, f"BTC15 dir/slope {direction:+d}/{slope:+d}"
    except Exception:
        return 0,"BTC context unavailable"


def _ldt_zone_entry_quality(live,zone,atr,d):
    if live<=0 or atr<=0:return 0.0
    lo,hi=zone["lo"],zone["hi"]; mid=(lo+hi)/2
    dist=abs(live-mid)/atr
    eq=100-min(70,dist*100)
    if lo-LDT_ZONE_TOL_ATR*atr <= live <= hi+LDT_ZONE_TOL_ATR*atr: eq+=8
    # Penalize already crossing completely through the zone against the thesis.
    if d==1 and live<lo-LDT_ZONE_TOL_ATR*atr:eq-=35
    if d==-1 and live>hi+LDT_ZONE_TOL_ATR*atr:eq-=35
    return max(0,min(100,eq))


def _ldt_build_reversal(symbol,parent,setup,route,ltf,trigger_tf):
    d=setup["d"]; side=setup["side"]; atr=setup["atr"]; zone=setup["zone"]
    live=safe_float(exchange.fetch_ticker(symbol).get("last"))
    eq=_ldt_zone_entry_quality(live,zone,atr,d)
    stop_raw=setup["sweep_extreme"]-d*LDT_STOP_BUFFER_ATR*atr
    risk=d*(live-stop_raw)
    if risk<=0:return None
    risk_atr=risk/atr
    target=parent["high"] if d==1 else parent["low"]
    gross_rr=d*(target-live)/risk
    cost_r=live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12)
    net_rr=gross_rr-cost_r
    btc_score,btc_note=_ldt_btc_context(side)
    time_bonus,cr_time=_ldt_cr_time_bonus(parent)
    sq=64 + time_bonus + min(10,setup["sweep_depth_atr"]*18) + (8 if setup["displacement"] else 0) + (5 if setup["vol_ok"] else 0)
    sq += 9 if setup["zone"].get("confluence",0)>=2 else 3
    sq += btc_score*LDT_BTC_CONTEXT_WEIGHT/2
    sq -= min(12,setup["age_bars"]*1.5)
    sq=max(0,min(100,sq))
    in_zone=zone["lo"]-LDT_ZONE_TOL_ATR*atr <= live <= zone["hi"]+LDT_ZONE_TOL_ATR*atr
    trig_ok,trig_score,trig_note=_ldt_micro_trigger(symbol,side,trigger_tf)
    skip=None
    if setup["age_bars"]*({"1m":1,"5m":5,"15m":15}.get(ltf,5)) > LDT_MAX_SIGNAL_AGE_MIN: skip="stale-MSS"
    elif risk_atr<LDT_MIN_STOP_ATR or risk_atr>LDT_MAX_STOP_ATR: skip=f"stop-{risk_atr:.2f}ATR"
    elif net_rr<LDT_MIN_NET_RR: skip=f"RR-{net_rr:.2f}<{LDT_MIN_NET_RR:.2f}"
    elif sq<LDT_MIN_SETUP_QUALITY: skip=f"SQ-{sq:.0f}<{LDT_MIN_SETUP_QUALITY:.0f}"
    elif eq<LDT_MIN_ENTRY_QUALITY: skip=f"wait-zone EQ{eq:.0f}"
    elif not in_zone: skip="ARMED-wait-retest"
    elif route=="FAST" and LDT_REQUIRE_1M_FAST_TRIGGER and not trig_ok: skip=f"wait-{trig_note}"
    elif route=="CORE" and not trig_ok: skip=f"wait-{trig_note}"
    return {"symbol":symbol,"side":side,"strategy":"CRT-MSS","entry_route":None if skip else f"CRT-{route}",
            "skip":skip,"price":live,"score":sq,"quality":0.65*sq+0.35*eq,"setup_quality":sq,"entry_quality":eq,
            "confirmations":trig_score+(2 if setup["zone"].get("confluence",0)>=2 else 1),"h4_score":0,
            "signal_bar_ts":int(setup["sub"].iloc[setup["mss_i"]]["ts"]),"atr":atr,
            "structural_stop":stop_raw,"target_price":target,"risk_distance":risk,"final_r":gross_rr,
            "net_rr":net_rr,"zone":zone,"parent":parent,"route":route,"btc_note":btc_note,
            "trigger_note":trig_note,"pattern_family":"QM/2B/FAKEOUT -> MSS",
            "sweep_depth_atr":setup["sweep_depth_atr"],"mss_level":setup["mss_level"],"cr_time":cr_time}


def _ldt_build_cont(symbol,parent,setup,route,ltf,trigger_tf):
    d=setup["d"]; side=setup["side"]; atr=setup["atr"]; zone=setup["zone"]
    live=safe_float(exchange.fetch_ticker(symbol).get("last")); eq=_ldt_zone_entry_quality(live,zone,atr,d)
    # Stop behind the retest/zone and parent boundary.
    if d==1: stop_raw=min(zone["lo"],setup["level"])-LDT_STOP_BUFFER_ATR*atr
    else: stop_raw=max(zone["hi"],setup["level"])+LDT_STOP_BUFFER_ATR*atr
    risk=d*(live-stop_raw)
    if risk<=0:return None
    risk_atr=risk/atr
    # Continuation target: 2.2R unless a measured parent-range objective is closer.
    measured=setup["level"]+d*parent["range"]
    measured_rr=d*(measured-live)/risk
    gross_rr=max(0,min(2.5,measured_rr))
    target=live+d*risk*gross_rr
    cost_r=live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12); net_rr=gross_rr-cost_r
    btc_score,btc_note=_ldt_btc_context(side)
    time_bonus,cr_time=_ldt_cr_time_bonus(parent)
    sq=67 + time_bonus + min(10,setup["body_atr"]*8) + 6 + (6 if setup.get("compression") else 0) + btc_score*LDT_BTC_CONTEXT_WEIGHT/2
    sq=max(0,min(100,sq)); in_zone=zone["lo"]-LDT_ZONE_TOL_ATR*atr<=live<=zone["hi"]+LDT_ZONE_TOL_ATR*atr
    trig_ok,trig_score,trig_note=_ldt_micro_trigger(symbol,side,trigger_tf)
    skip=None
    if risk_atr<LDT_MIN_STOP_ATR or risk_atr>LDT_MAX_STOP_ATR:skip=f"stop-{risk_atr:.2f}ATR"
    elif net_rr<LDT_MIN_NET_RR:skip=f"RR-{net_rr:.2f}<{LDT_MIN_NET_RR:.2f}"
    elif sq<LDT_MIN_SETUP_QUALITY:skip=f"SQ-{sq:.0f}"
    elif eq<LDT_MIN_ENTRY_QUALITY:skip=f"wait-zone EQ{eq:.0f}"
    elif not in_zone:skip="ARMED-wait-break-retest"
    elif not trig_ok:skip=f"wait-{trig_note}"
    return {"symbol":symbol,"side":side,"strategy":"BREAK-RETEST","entry_route":None if skip else f"CONT-{route}",
            "skip":skip,"price":live,"score":sq,"quality":0.65*sq+0.35*eq,"setup_quality":sq,"entry_quality":eq,
            "confirmations":trig_score+(1 if setup.get("compression") else 0),"h4_score":0,
            "signal_bar_ts":int(setup["sub"].iloc[setup["retest_i"]]["ts"]),"atr":atr,
            "structural_stop":stop_raw,"target_price":target,"risk_distance":risk,"final_r":gross_rr,
            "net_rr":net_rr,"zone":zone,"parent":parent,"route":route,"btc_note":btc_note,
            "trigger_note":trig_note,"pattern_family":"FLAG/CONTINUATION + RETEST","cr_time":cr_time}


def evaluate_symbol(symbol, btc=None):
    """New bot only: CRT liquidity reversal first, breakout/retest second."""
    candidates=[]
    pairs=[("CORE","1h","5m","1m")]
    if LDT_ENABLE_FAST:pairs.append(("FAST","15m","1m","1m"))
    for route,ptf,ltf,ttf in pairs:
        try:
            parents=_ldt_parent_candidates(symbol,ptf)
        except Exception:
            continue
        for parent in parents[:3]:
            try:
                rev=_ldt_find_sweep_mss(symbol,parent,ltf)
                if rev:
                    s=_ldt_build_reversal(symbol,parent,rev,route,ltf,ttf)
                    if s:candidates.append(s)
                cont=_ldt_break_retest(symbol,parent,ltf)
                if cont:
                    s=_ldt_build_cont(symbol,parent,cont,route,ltf,ttf)
                    if s:candidates.append(s)
            except Exception:
                continue
    if not candidates:return None
    # Prefer ready, then armed closest to entry, then higher net RR/setup quality.
    candidates.sort(key=lambda s:(1 if not s.get("skip") else 0,
                                  1 if str(s.get("skip","")).startswith("ARMED") else 0,
                                  safe_float(s.get("entry_quality")),safe_float(s.get("net_rr")),safe_float(s.get("setup_quality"))),reverse=True)
    return candidates[0]


def sizing_profile(signal):
    route=signal.get("route"); strategy=signal.get("strategy")
    elite=(safe_float(signal.get("setup_quality"))>=90 and safe_float(signal.get("entry_quality"))>=85 and safe_float(signal.get("net_rr"))>=2.2)
    if elite:return {"name":"LDT-ELITE","risk_pct":LDT_ELITE_RISK,"target_net_tp":LDT_CORE_TARGET_NET}
    if route=="FAST":return {"name":"LDT-FAST","risk_pct":LDT_FAST_RISK,"target_net_tp":LDT_FAST_TARGET_NET}
    if strategy=="BREAK-RETEST":return {"name":"LDT-CONT","risk_pct":LDT_CONT_RISK,"target_net_tp":LDT_CORE_TARGET_NET}
    return {"name":"LDT-CORE","risk_pct":LDT_CORE_RISK,"target_net_tp":LDT_CORE_TARGET_NET}


def sniper_portfolio_allows(signal, positions):
    # LDT v1.2.1 portfolio gate. Keep the new strategy isolated from legacy
    # V6 directional-loss streak state. Risk is already controlled by the
    # daily loss gate, consecutive-loss pause, max positions and same-side cap.
    if len(positions)>=MAX_ACTIVE_POSITIONS:
        return False,f"max-{MAX_ACTIVE_POSITIONS}-positions"
    side=signal.get("side")
    same=sum(1 for p in positions if position_side(p)==side)
    if same>=LDT_MAX_SAME_SIDE:
        return False,f"max-{LDT_MAX_SAME_SIDE}-same-side"
    return True,"OK"


def _ldt_levels(signal,live,tick):
    side=signal["side"]; d=1 if side=="long" else -1
    sl=safe_float(signal.get("structural_stop")); target=safe_float(signal.get("target_price"))
    if live<=0 or sl<=0 or target<=0:return None
    risk=d*(live-sl); reward=d*(target-live)
    if risk<=0 or reward<=0:return None
    sl=round_trade_price(sl,tick,side,"sl"); target=round_trade_price(target,tick,side,"tp")
    risk=abs(live-sl); final_r=abs(target-live)/max(risk,1e-12)
    return sl,target,risk,final_r


def open_trade(signal):
    global protection_lock, _v62_last_entry_time
    symbol,side=signal["symbol"],signal["side"]; d=1 if side=="long" else -1
    acc=futures_account(); equity,available=acc["total_equity"],acc["available"]
    reset_daily_if_needed(equity)
    if equity<MIN_EQUITY_USDT or available<=0 or (not PAPER_TRADING and protection_lock):return False
    if time.time()-_v62_last_entry_time < 4*60:return False

    # Recompute the actual entry geometry at execution. No stale zone fills.
    live=safe_float(exchange.fetch_ticker(symbol).get("last"),signal.get("price")); atr=safe_float(signal.get("atr"))
    zone=signal.get("zone") or {}; eq=_ldt_zone_entry_quality(live,zone,atr,d)
    if eq<LDT_MIN_ENTRY_QUALITY:
        tg(f"⏭️ LDT {symbol} {side.upper()} ENTRY MOVED\nEQ {eq:.0f} < {LDT_MIN_ENTRY_QUALITY:.0f} · no chase")
        return False
    signal_px=safe_float(signal.get("price"),live); drift=d*(live-signal_px)/max(atr,1e-12)
    if drift>LDT_LIVE_DRIFT_ATR:
        tg(f"⏭️ LDT {symbol} {side.upper()} TOO LATE\nAdverse drift {drift:.2f}ATR > {LDT_LIVE_DRIFT_ATR:.2f}ATR")
        return False

    tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
    levels=_ldt_levels(signal,live,tick)
    if not levels:return False
    sl,tp,risk,final_r=levels
    cost_r=live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12); net_rr=final_r-cost_r
    if net_rr<LDT_MIN_NET_RR:
        tg(f"⏭️ LDT {symbol} {side.upper()} RR BLOCK\nNet RR {net_rr:.2f} < {LDT_MIN_NET_RR:.2f}")
        return False

    profile=sizing_profile(signal)
    sizing=calculate_contracts_v5(symbol,live,sl,equity,available,profile["risk_pct"],profile["target_net_tp"],final_r)
    if not sizing or sizing["margin"]>available*AVAILABLE_MARGIN_BUFFER:return False
    before_ids={position_id(p) for p in get_positions() if position_id(p)}
    oklev,levres=set_leverage(symbol,LEVERAGE)
    if not oklev:tg(f"⛔ LDT LEVERAGE FAILED\n{symbol}\n{levres}");return False

    tag="ldt10"
    payload={"instId":blofin_inst_id(symbol),"marginMode":MARGIN_MODE,"positionSide":"net",
             "side":"buy" if side=="long" else "sell","orderType":"market","size":str(sizing["contracts"]),
             "tpTriggerPrice":str(tp),"tpOrderPrice":"-1","tpTriggerPriceType":"mark",
             "slTriggerPrice":str(sl),"slOrderPrice":"-1","slTriggerPriceType":"mark",
             "clientOrderId":f"{tag}{int(time.time()*1000)%1000000000}"}
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    msg=(f"🎯 {'PAPER ' if PAPER_TRADING else ''}LIQUIDITY DAYTRADER v1\n{symbol} {side.upper()} · {signal.get('entry_route')}\n"
         f"{signal.get('pattern_family')}\nSQ {safe_float(signal.get('setup_quality')):.0f} · EQ {eq:.0f} · net RR {net_rr:.2f}R\n"
         f"Parent {signal.get('parent',{}).get('tf','-')} · {signal.get('cr_time','-')} H/L {safe_float(signal.get('parent',{}).get('high')):.8g}/{safe_float(signal.get('parent',{}).get('low')):.8g}\n"
         f"Zone {zone.get('kind','-')} {safe_float(zone.get('lo')):.8g}-{safe_float(zone.get('hi')):.8g}\n"
         f"{signal.get('trigger_note','')} · {signal.get('btc_note','')}\n"
         f"Entry {live:.8g} · SL {sl:.8g} · TP {tp:.8g}\nRisk cap {profile['risk_pct']*100:.2f}% · expected net TP ~${sizing['expected_net_tp_usdt']:.2f}\n"
         f"Notional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x")
    if PAPER_TRADING:tg(msg);_v62_last_entry_time=time.time();return True
    resp=native("private_post_trade_order",payload)
    if not native_ok(resp):
        err=response_error(resp)
        if "152012" in err:protection_lock=True
        tg(f"❌ LDT ORDER FAILED\n{symbol} {side.upper()}\n{err}");return False
    _v62_last_entry_time=time.time();tg(msg)
    new_pos=find_new_position(symbol,before_ids,timeout=8)
    if new_pos is None:tg(f"⚠️ {symbol}: LDT order accepted, position not visible yet.");return True
    pid=position_id(new_pos)
    if pid:
        sniper_managed_positions.add(pid)
        _ldt_meta[pid]={"risk":risk,"entry":position_entry(new_pos) or live,"sl":sl,"tp":tp,"final_r":final_r,
                        "side":side,"opened":time.time(),"strategy":signal.get("strategy"),"route":signal.get("entry_route")}
    if has_live_protection(new_pos)[0]:tg(f"🛡️ LDT PROTECTION CONFIRMED\n{symbol} {side.upper()}\nSL/TP visible.");return True
    last=safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side,sl,tp,last):
        ok,res=place_tpsl_for_position(new_pos,sl,tp,client_tag=tag)
        if ok:tg(f"🛡️ LDT FALLBACK PROTECTION CONFIRMED\n{symbol}");return True
        protection_lock=True;tg(f"🚨 LDT PROTECTION FAILURE\n{symbol}\n{res}\nNEW TRADES PAUSED.");return True
    protection_lock=True;tg(f"🚨 {symbol}: LDT position exists but protection could not be verified.\nNEW TRADES PAUSED.");return True


def repair_one_position(p):
    pid=position_id(p); meta=_ldt_meta.get(pid)
    if not meta:return False
    if has_live_protection(p)[0]:return True
    return place_tpsl_for_position(p,meta["sl"],meta["tp"],client_tag="ldt10r")[0]


def manage_sniper_step_stops():
    """LDT management: protect after proof, but do not suffocate normal retests."""
    if PAPER_TRADING:return
    for p in get_positions():
        try:
            pid=position_id(p); meta=_ldt_meta.get(pid)
            if not meta:continue
            symbol=position_symbol(p); side=position_side(p); d=1 if side=="long" else -1
            entry=safe_float(meta.get("entry")) or position_entry(p); risk=safe_float(meta.get("risk"))
            if entry<=0 or risk<=0:continue
            ok,order=has_live_protection(p)
            if not ok or not order:continue
            current_sl=safe_float(order.get("slTriggerPrice")); tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
            current=safe_float(exchange.fetch_ticker(symbol).get("last")); ext=sniper_recent_extreme(symbol,side)
            favorable=max(current,ext) if d==1 else min(current,ext); best_r=d*(favorable-entry)/risk
            fee_px=entry*(TAKER_FEE_RATE*2+SLIPPAGE_RATE)*1.10
            desired=None;stage=None
            if best_r>=1.75:
                desired=round_trade_price(entry+d*0.75*risk,tick,side,"sl");stage="+1.75R → lock +0.75R"
            elif best_r>=1.00:
                desired=round_trade_price(entry+d*fee_px,tick,side,"sl");stage="+1R → fee-safe BE"
            if desired is not None:
                improves=desired>current_sl+tick*0.1 if d==1 else desired<current_sl-tick*0.1
                if improves:
                    ok2,_=amend_tpsl_stop(p,desired)
                    key=f"ldt:{pid}:{stage}"
                    if ok2 and not sniper_step_notified.get(key):tg(f"🪜 LDT STEP STOP\n{symbol} {side.upper()}\n{stage} · SL {desired:.8g}");sniper_step_notified[key]=True
        except Exception as e:
            tg(f"⚠️ LDT POSITION MANAGER\n{position_symbol(p) if isinstance(p,dict) else '-'}\n{type(e).__name__}: {e}")


def maybe_score_report(rows):
    global _ldt_last_report
    now=time.time()
    if now-_ldt_last_report<LDT_REPORT_MIN*60 or not rows:return
    _ldt_last_report=now
    ready=[r for r in rows if r.get("side") and not r.get("skip") and r.get("entry_route")]
    armed=[r for r in rows if "ARMED" in str(r.get("skip","")) or "wait-zone" in str(r.get("skip",""))]
    waits={}
    for r in rows:
        sk=str(r.get("skip") or "READY")
        key="READY" if sk=="READY" else "zone/retest" if ("ARMED" in sk or "wait-zone" in sk) else "RR" if sk.startswith("RR") else "trigger" if sk.startswith("wait-") else "structure"
        waits[key]=waits.get(key,0)+1
    ranked=sorted(rows,key=lambda r:(1 if not r.get("skip") else 0,safe_float(r.get("entry_quality")),safe_float(r.get("setup_quality")),safe_float(r.get("net_rr"))),reverse=True)[:8]
    lines=[f"🧠 LIQUIDITY DAYTRADER v1.0 · CRT/MSS","Ready %d · Armed/retest %d · scanned %d"%(len(ready),len(armed),len(rows)),
           "States: "+" · ".join(f"{k} {v}" for k,v in sorted(waits.items(),key=lambda x:x[1],reverse=True)[:4])]
    for r in ranked:
        status="🟢 "+str(r.get("entry_route")) if not r.get("skip") else "🟡 "+str(r.get("skip"))
        lines.append(f"{r.get('symbol')} {str(r.get('side')).upper()} {r.get('strategy')} SQ{safe_float(r.get('setup_quality')):.0f} EQ{safe_float(r.get('entry_quality')):.0f} RR{safe_float(r.get('net_rr')):.2f} {status}")
    tg("\n".join(lines))


def startup_diagnostics():
    acc=futures_account();reset_daily_if_needed(acc["total_equity"])
    tg(f"🧠 LIQUIDITY DAYTRADER STARTED v1.0\n"
       f"NEW strategy — old Structure Fusion signals are disabled.\n"
       f"CORE: 1H Range Candle → 5M sweep/reclaim → MSS → IFVG/Breaker → retest\n"
       f"FAST: 15M Range Candle → 1M structure/retest ({'ON' if LDT_ENABLE_FAST else 'OFF'})\n"
       f"CONT: breakout → pullback → continuation ({'ON' if LDT_ENABLE_CONTINUATION else 'OFF'})\n"
       f"Pattern families: QM/2B/fakeout · flag/continuation · compression context\n"
       f"Min net RR {LDT_MIN_NET_RR:.2f}R · max positions {MAX_ACTIVE_POSITIONS} · max same side {LDT_MAX_SAME_SIDE}\n"
       f"Risk CORE {LDT_CORE_RISK*100:.2f}% · FAST {LDT_FAST_RISK*100:.2f}% · CONT {LDT_CONT_RISK*100:.2f}% · ELITE cap {LDT_ELITE_RISK*100:.2f}%\n"
       f"Daily loss stop {DAILY_LOSS_LIMIT_PCT:.1f}% · 3 losses → {LOSS_PAUSE_MIN}m pause\n"
       f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}\n"
       f"Risk epoch: {V6_RISK_EPOCH_LOCAL or 'Amsterdam day start'}")


def notify_closed_trade(record):
    global consecutive_losses, loss_pause_until
    reset_daily_if_needed()
    symbol=str(record.get("instId","")).replace("-USDT","/USDT:USDT"); side=infer_side(record)
    entry=safe_float(record.get("openAveragePrice")); exit_price=safe_float(record.get("closeAveragePrice"))
    gross=safe_float(record.get("realizedPnl")); fee=abs(safe_float(record.get("fee"))); net=gross-fee
    duration=fmt_duration(record.get("createTime"),record.get("updateTime")); reason=close_reason(record)
    bucket="short" if side=="short" else "long"
    daily["trades"]+=1;daily["net"]+=net;daily[f"{bucket}_trades"]+=1;daily[f"{bucket}_net"]+=net
    if net>=0:
        daily["wins"]+=1;daily[f"{bucket}_wins"]+=1;consecutive_losses=0;sniper_side_loss_streak[bucket]=0;symbol_last_result[symbol]="win";symbol_loss_streak[symbol]=0
    else:
        daily["losses"]+=1;consecutive_losses+=1;sniper_side_loss_streak[bucket]=sniper_side_loss_streak.get(bucket,0)+1;symbol_last_result[symbol]="loss";symbol_loss_streak[symbol]=symbol_loss_streak.get(symbol,0)+1
        if consecutive_losses>=MAX_CONSECUTIVE_LOSSES:loss_pause_until=time.time()+LOSS_PAUSE_MIN*60
    symbol_last_trade[symbol]=time.time();update_learning(record,net)
    tg(f"{'🟢' if net>=0 else '🔴'} LIQUIDITY DAYTRADER CLOSED\n{symbol} {side.upper()}\n"
       f"Entry {entry:.8g} → Exit {exit_price:.8g}\nGross ${gross:+.2f} · Fee -${fee:.2f} · Net ${net:+.2f}\n"
       f"{reason} · {duration}\nTODAY: {daily['trades']} trades · W/L {daily['wins']}/{daily['losses']} · Net ${daily['net']:.2f}\n"
       f"Side loss streak L/S {sniper_side_loss_streak.get('long',0)}/{sniper_side_loss_streak.get('short',0)}"
       + (f"\n⏸ {MAX_CONSECUTIVE_LOSSES} losses → pause {LOSS_PAUSE_MIN} min" if consecutive_losses>=MAX_CONSECUTIVE_LOSSES else ""))

# ============================================================
# LIQUIDITY DAYTRADER v1.2 — STRUCTURE VERIFIED
# ============================================================
# v1.2 is a deliberate correction of v1.0 based on the first live run:
#   1) previous-candle "break" is NOT accepted as BOS/MSS anymore;
#   2) sweep->MSS must break a confirmed swing with displacement;
#   3) last-mile trigger must break a confirmed micro swing with displacement;
#   4) targets use first meaningful opposing liquidity before the far CR target;
#   5) position MFE starts at the actual fill; no pre-entry candle extremes;
#   6) every stop amendment is checked against the current market price;
#   7) execution re-validates the structural trigger immediately before order submit.

LDT_VERSION = "1.2.1"
LDT11_MICRO_LEFT = int(os.getenv("LDT11_MICRO_LEFT", "2"))
LDT11_MICRO_RIGHT = int(os.getenv("LDT11_MICRO_RIGHT", "2"))
LDT11_MICRO_BREAK_ATR = float(os.getenv("LDT11_MICRO_BREAK_ATR", "0.015"))
LDT11_MICRO_BODY_ATR = float(os.getenv("LDT11_MICRO_BODY_ATR", "0.22"))
LDT11_MICRO_CLOSE_LOCATION = float(os.getenv("LDT11_MICRO_CLOSE_LOCATION", "0.68"))
LDT11_MSS_BODY_ATR = float(os.getenv("LDT11_MSS_BODY_ATR", "0.35"))
LDT11_TARGET_MIN_PIVOT_R = float(os.getenv("LDT11_TARGET_MIN_PIVOT_R", "0.75"))
LDT11_STOP_MARK_BUFFER_TICKS = int(os.getenv("LDT11_STOP_MARK_BUFFER_TICKS", "3"))
LDT11_CORE_RISK = float(os.getenv("LDT11_CORE_RISK", "0.0060"))
LDT11_FAST_RISK = float(os.getenv("LDT11_FAST_RISK", "0.0035"))
LDT11_CONT_RISK = float(os.getenv("LDT11_CONT_RISK", "0.0050"))
LDT11_ELITE_RISK = float(os.getenv("LDT11_ELITE_RISK", "0.0080"))


def _ldt11_last_swing_before(df, upto_i, want):
    """Last confirmed pivot before upto_i. want='high' or 'low'."""
    if upto_i is None or upto_i < 4:
        return None
    x = df.iloc[:upto_i].copy().reset_index(drop=True)
    highs, lows = _ldt_pivots(x, LDT11_MICRO_LEFT, LDT11_MICRO_RIGHT)
    arr = highs if want == "high" else lows
    return arr[-1] if arr else None


def _ldt11_bar_strength(row, d, atr):
    o=safe_float(row["open"]); h=safe_float(row["high"]); l=safe_float(row["low"]); c=safe_float(row["close"])
    rng=max(h-l,1e-12); body=abs(c-o)/max(atr,1e-12)
    close_loc=(c-l)/rng if d==1 else (h-c)/rng
    directional=(c>o) if d==1 else (c<o)
    return body,close_loc,directional


def _ldt_find_sweep_mss(symbol, parent, ltf):
    """v1.2: external CR sweep/reclaim followed by a CONFIRMED swing MSS + displacement."""
    df=_ldt_closed(_ldt_df(symbol,ltf,240))
    if len(df)<45:return None
    atr=safe_float(df.iloc[-1]["atr"])
    if atr<=0:return None
    sub=df[df["ts"]>=parent.get("end_ts",parent["ts"])].copy().reset_index(drop=True)
    if len(sub)<10:return None
    ph,pl=parent["high"],parent["low"]; best=None
    for i in range(max(4,len(sub)-40),len(sub)):
        r=sub.iloc[i]
        for d in (-1,1):
            if d==-1:
                depth=(safe_float(r["high"])-ph)/atr
                swept=safe_float(r["high"])>ph and safe_float(r["close"])<ph
                pivot=_ldt11_last_swing_before(sub,i,"low")
            else:
                depth=(pl-safe_float(r["low"]))/atr
                swept=safe_float(r["low"])<pl and safe_float(r["close"])>pl
                pivot=_ldt11_last_swing_before(sub,i,"high")
            if not swept or depth<LDT_SWEEP_MIN_ATR or depth>LDT_SWEEP_MAX_ATR or not pivot:continue
            _,mss_level=pivot; mss_i=None; mss_strength=None
            for k in range(i+1,len(sub)):
                mr=sub.iloc[k]; mat=safe_float(mr["atr"],atr) or atr
                c=safe_float(mr["close"])
                broke=(c < mss_level-LDT11_MICRO_BREAK_ATR*mat) if d==-1 else (c > mss_level+LDT11_MICRO_BREAK_ATR*mat)
                if not broke:continue
                body,close_loc,directional=_ldt11_bar_strength(mr,d,mat)
                if not directional or body<LDT11_MSS_BODY_ATR or close_loc<LDT11_MICRO_CLOSE_LOCATION:continue
                mss_i=k; mss_strength=(body,close_loc); break
            if mss_i is None:continue
            mr=sub.iloc[mss_i]; vol=safe_float(mr["volume"]); vma=safe_float(mr["volma"])
            vol_ok=(vma<=0 or vol>=vma*LDT_VOL_MULT)
            actual_i0=max(0,len(df)-len(sub)); sweep_abs=actual_i0+i; mss_abs=actual_i0+mss_i
            ifvg=_ldt_ifvg(df,d,sweep_abs,mss_abs); fvg=_ldt_directional_fvg(df,d,sweep_abs,mss_abs)
            breaker=_ldt_breaker(df,d,sweep_abs,mss_abs); zone=_ldt_zone_merge(ifvg or fvg,breaker)
            if not zone:continue
            sweep_ext=safe_float(r["high"] if d==-1 else r["low"]); age_bars=len(sub)-1-mss_i
            setup={"d":d,"side":"long" if d==1 else "short","sweep_i":i,"mss_i":mss_i,
                   "mss_level":mss_level,"sweep_extreme":sweep_ext,"sweep_depth_atr":depth,
                   "body_atr":mss_strength[0],"close_location":mss_strength[1],"vol_ok":vol_ok,
                   "displacement":True,"zone":zone,"ifvg":ifvg,"fvg":fvg,"breaker":breaker,
                   "age_bars":age_bars,"df":df,"sub":sub,"atr":atr,"mss_verified":True}
            rank=(zone.get("confluence",0),mss_strength[0],mss_strength[1],-age_bars)
            if best is None or rank>best.get("_rank",(-1,0,0,-999)):
                setup["_rank"]=rank; best=setup
    return best


def _ldt_micro_trigger(symbol, side, tf):
    """v1.2 structural trigger: confirmed swing BOS/MSS + displacement, never a 2/4 candle score."""
    df=_ldt_closed(_ldt_df(symbol,tf,140))
    if len(df)<35:return False,0,f"{tf} no-structure-data"
    d=1 if side=="long" else -1; a=df.iloc[-1]; atr=safe_float(a["atr"])
    if atr<=0:return False,0,f"{tf} ATR unavailable"
    # Pivot must be fully confirmed before the current trigger candle.
    prior=df.iloc[:-1].copy().reset_index(drop=True)
    highs,lows=_ldt_pivots(prior,LDT11_MICRO_LEFT,LDT11_MICRO_RIGHT)
    piv=(highs[-1] if d==1 and highs else lows[-1] if d==-1 and lows else None)
    if not piv:return False,0,f"{tf} wait-confirmed-swing"
    _,level=piv; c=safe_float(a["close"])
    broke=(c>level+LDT11_MICRO_BREAK_ATR*atr) if d==1 else (c<level-LDT11_MICRO_BREAK_ATR*atr)
    body,close_loc,directional=_ldt11_bar_strength(a,d,atr)
    displacement=directional and body>=LDT11_MICRO_BODY_ATR and close_loc>=LDT11_MICRO_CLOSE_LOCATION
    ema_side=(c>safe_float(a["ema21"])) if d==1 else (c<safe_float(a["ema21"]))
    vol=safe_float(a["volume"]); vma=safe_float(a["volma"]); vol_ok=(vma<=0 or vol>=vma)
    ok=bool(broke and displacement)
    strength=int(broke)+int(displacement)+int(ema_side)+int(vol_ok)
    note=(f"{tf} STRUCT {'CONFIRMED' if ok else 'WAIT'} swing={level:.8g} "
          f"BOS={int(broke)} disp={body:.2f}ATR close={close_loc:.0%} EMA={int(ema_side)} vol={int(vol_ok)}")
    return ok,strength,note


def _ldt11_first_liquidity_target(symbol, side, live, stop, hard_target, tf):
    """Nearest confirmed opposing swing-liquidity on the path to the far target."""
    d=1 if side=="long" else -1; risk=d*(live-stop)
    if live<=0 or risk<=0:return hard_target,"far-target",0.0
    try:
        df=_ldt_closed(_ldt_df(symbol,tf,220)); highs,lows=_ldt_pivots(df,2,2)
        levels=[v for _,v in (highs if d==1 else lows)]
        if d==1:
            valid=[v for v in levels if v>live and (hard_target<=live or v<=hard_target)]
            valid.sort()
        else:
            valid=[v for v in levels if v<live and (hard_target>=live or v>=hard_target)]
            valid.sort(reverse=True)
        # Keep the FIRST real obstacle, even if it blocks the trade on RR. That is intentional.
        if valid:
            t=valid[0]; rr=d*(t-live)/risk
            return t,"first-swing-liquidity",rr
    except Exception:
        pass
    rr=d*(hard_target-live)/risk
    return hard_target,"far-CR/measured-target",rr


def _ldt_build_reversal(symbol,parent,setup,route,ltf,trigger_tf):
    d=setup["d"]; side=setup["side"]; atr=setup["atr"]; zone=setup["zone"]
    live=safe_float(exchange.fetch_ticker(symbol).get("last")); eq=_ldt_zone_entry_quality(live,zone,atr,d)
    stop_raw=setup["sweep_extreme"]-d*LDT_STOP_BUFFER_ATR*atr; risk=d*(live-stop_raw)
    if risk<=0:return None
    risk_atr=risk/atr; far_target=parent["high"] if d==1 else parent["low"]
    target,target_kind,gross_rr=_ldt11_first_liquidity_target(symbol,side,live,stop_raw,far_target,ltf)
    cost_r=live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12); net_rr=gross_rr-cost_r
    btc_score,btc_note=_ldt_btc_context(side); time_bonus,cr_time=_ldt_cr_time_bonus(parent)
    sq=60+time_bonus+min(9,setup["sweep_depth_atr"]*16)+10+min(8,setup.get("body_atr",0)*8)
    sq += 8 if zone.get("confluence",0)>=2 else 3; sq += 3 if setup.get("vol_ok") else 0
    sq += btc_score*LDT_BTC_CONTEXT_WEIGHT/2; sq-=min(10,setup["age_bars"]*1.25); sq=max(0,min(95,sq))
    in_zone=zone["lo"]-LDT_ZONE_TOL_ATR*atr<=live<=zone["hi"]+LDT_ZONE_TOL_ATR*atr
    trig_ok,trig_score,trig_note=_ldt_micro_trigger(symbol,side,trigger_tf); skip=None
    if setup["age_bars"]*({"1m":1,"5m":5,"15m":15}.get(ltf,5))>LDT_MAX_SIGNAL_AGE_MIN:skip="stale-MSS"
    elif risk_atr<LDT_MIN_STOP_ATR or risk_atr>LDT_MAX_STOP_ATR:skip=f"stop-{risk_atr:.2f}ATR"
    elif net_rr<LDT_MIN_NET_RR:skip=f"RR-first-liq-{net_rr:.2f}<{LDT_MIN_NET_RR:.2f}"
    elif sq<LDT_MIN_SETUP_QUALITY:skip=f"STR-{sq:.0f}<{LDT_MIN_SETUP_QUALITY:.0f}"
    elif eq<LDT_MIN_ENTRY_QUALITY:skip=f"wait-zone EQ{eq:.0f}"
    elif not in_zone:skip="ARMED-wait-retest"
    elif not trig_ok:skip=f"wait-{trig_note}"
    return {"symbol":symbol,"side":side,"strategy":"CRT-MSS","entry_route":None if skip else f"CRT-{route}-SV",
            "skip":skip,"price":live,"score":sq,"quality":0.60*sq+0.40*eq,"setup_quality":sq,"entry_quality":eq,
            "confirmations":trig_score+2,"h4_score":0,"signal_bar_ts":int(setup["sub"].iloc[setup["mss_i"]]["ts"]),
            "atr":atr,"structural_stop":stop_raw,"target_price":target,"far_target":far_target,"target_kind":target_kind,
            "risk_distance":risk,"final_r":gross_rr,"net_rr":net_rr,"zone":zone,"parent":parent,"route":route,
            "trigger_tf":trigger_tf,"btc_note":btc_note,"trigger_note":trig_note,
            "pattern_family":"CR SWEEP -> SWING MSS -> IFVG/BREAKER RETEST",
            "sweep_depth_atr":setup["sweep_depth_atr"],"mss_level":setup["mss_level"],"cr_time":cr_time}


def _ldt_build_cont(symbol,parent,setup,route,ltf,trigger_tf):
    d=setup["d"]; side=setup["side"]; atr=setup["atr"]; zone=setup["zone"]
    live=safe_float(exchange.fetch_ticker(symbol).get("last")); eq=_ldt_zone_entry_quality(live,zone,atr,d)
    stop_raw=(min(zone["lo"],setup["level"])-LDT_STOP_BUFFER_ATR*atr) if d==1 else (max(zone["hi"],setup["level"])+LDT_STOP_BUFFER_ATR*atr)
    risk=d*(live-stop_raw)
    if risk<=0:return None
    risk_atr=risk/atr; far_target=setup["level"]+d*parent["range"]
    target,target_kind,gross_rr=_ldt11_first_liquidity_target(symbol,side,live,stop_raw,far_target,ltf)
    cost_r=live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12); net_rr=gross_rr-cost_r
    btc_score,btc_note=_ldt_btc_context(side); time_bonus,cr_time=_ldt_cr_time_bonus(parent)
    sq=62+time_bonus+min(10,setup["body_atr"]*8)+7+(5 if setup.get("compression") else 0)+btc_score*LDT_BTC_CONTEXT_WEIGHT/2
    sq=max(0,min(95,sq)); in_zone=zone["lo"]-LDT_ZONE_TOL_ATR*atr<=live<=zone["hi"]+LDT_ZONE_TOL_ATR*atr
    trig_ok,trig_score,trig_note=_ldt_micro_trigger(symbol,side,trigger_tf); skip=None
    if risk_atr<LDT_MIN_STOP_ATR or risk_atr>LDT_MAX_STOP_ATR:skip=f"stop-{risk_atr:.2f}ATR"
    elif net_rr<LDT_MIN_NET_RR:skip=f"RR-first-liq-{net_rr:.2f}<{LDT_MIN_NET_RR:.2f}"
    elif sq<LDT_MIN_SETUP_QUALITY:skip=f"STR-{sq:.0f}"
    elif eq<LDT_MIN_ENTRY_QUALITY:skip=f"wait-zone EQ{eq:.0f}"
    elif not in_zone:skip="ARMED-wait-break-retest"
    elif not trig_ok:skip=f"wait-{trig_note}"
    return {"symbol":symbol,"side":side,"strategy":"BREAK-RETEST","entry_route":None if skip else f"CONT-{route}-SV",
            "skip":skip,"price":live,"score":sq,"quality":0.60*sq+0.40*eq,"setup_quality":sq,"entry_quality":eq,
            "confirmations":trig_score+(1 if setup.get("compression") else 0),"h4_score":0,
            "signal_bar_ts":int(setup["sub"].iloc[setup["retest_i"]]["ts"]),"atr":atr,
            "structural_stop":stop_raw,"target_price":target,"far_target":far_target,"target_kind":target_kind,
            "risk_distance":risk,"final_r":gross_rr,"net_rr":net_rr,"zone":zone,"parent":parent,"route":route,
            "trigger_tf":trigger_tf,"btc_note":btc_note,"trigger_note":trig_note,
            "pattern_family":"BREAK -> RETEST -> SWING BOS/DISPLACEMENT","cr_time":cr_time}


def sizing_profile(signal):
    route=signal.get("route"); strategy=signal.get("strategy")
    elite=(safe_float(signal.get("setup_quality"))>=90 and safe_float(signal.get("entry_quality"))>=85 and safe_float(signal.get("net_rr"))>=2.2)
    if elite:return {"name":"LDT11-ELITE","risk_pct":LDT11_ELITE_RISK,"target_net_tp":LDT_CORE_TARGET_NET}
    if route=="FAST":return {"name":"LDT11-FAST","risk_pct":LDT11_FAST_RISK,"target_net_tp":LDT_FAST_TARGET_NET}
    if strategy=="BREAK-RETEST":return {"name":"LDT11-CONT","risk_pct":LDT11_CONT_RISK,"target_net_tp":LDT_CORE_TARGET_NET}
    return {"name":"LDT11-CORE","risk_pct":LDT11_CORE_RISK,"target_net_tp":LDT_CORE_TARGET_NET}


def open_trade(signal):
    global protection_lock, _v62_last_entry_time
    symbol,side=signal["symbol"],signal["side"]; d=1 if side=="long" else -1
    acc=futures_account(); equity,available=acc["total_equity"],acc["available"]; reset_daily_if_needed(equity)
    if equity<MIN_EQUITY_USDT or available<=0 or (not PAPER_TRADING and protection_lock):return False
    if time.time()-_v62_last_entry_time<4*60:return False
    # v1.2: structure must STILL be confirmed immediately before exchange submission.
    trig_ok,trig_score,trig_note=_ldt_micro_trigger(symbol,side,signal.get("trigger_tf") or "1m")
    if not trig_ok:
        tg(f"⏭️ LDT v1.2 {symbol} {side.upper()} STRUCTURE EXPIRED\n{trig_note}\nOrder cancelled before submission."); return False
    live=safe_float(exchange.fetch_ticker(symbol).get("last"),signal.get("price")); atr=safe_float(signal.get("atr")); zone=signal.get("zone") or {}
    eq=_ldt_zone_entry_quality(live,zone,atr,d)
    if eq<LDT_MIN_ENTRY_QUALITY:
        tg(f"⏭️ LDT v1.2 {symbol} {side.upper()} ENTRY MOVED\nEQ {eq:.0f} < {LDT_MIN_ENTRY_QUALITY:.0f} · no chase");return False
    signal_px=safe_float(signal.get("price"),live); drift=d*(live-signal_px)/max(atr,1e-12)
    if drift>LDT_LIVE_DRIFT_ATR:
        tg(f"⏭️ LDT v1.2 {symbol} {side.upper()} TOO LATE\nAdverse drift {drift:.2f}ATR > {LDT_LIVE_DRIFT_ATR:.2f}ATR");return False
    tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001); sl=safe_float(signal.get("structural_stop"))
    if live<=0 or sl<=0:return False
    risk=d*(live-sl)
    if risk<=0:return False
    # Recalculate first-liquidity target from the ACTUAL execution price.
    hard=safe_float(signal.get("far_target") or signal.get("target_price")); ltf="1m" if signal.get("route")=="FAST" else "5m"
    target,target_kind,gross_rr=_ldt11_first_liquidity_target(symbol,side,live,sl,hard,ltf)
    sl=round_trade_price(sl,tick,side,"sl"); target=round_trade_price(target,tick,side,"tp")
    risk=abs(live-sl); final_r=d*(target-live)/max(risk,1e-12)
    cost_r=live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12); net_rr=final_r-cost_r
    if net_rr<LDT_MIN_NET_RR:
        tg(f"⏭️ LDT v1.2 {symbol} {side.upper()} FIRST-LIQUIDITY RR BLOCK\n{target_kind} · net RR {net_rr:.2f} < {LDT_MIN_NET_RR:.2f}");return False
    profile=sizing_profile(signal); sizing=calculate_contracts_v5(symbol,live,sl,equity,available,profile["risk_pct"],profile["target_net_tp"],final_r)
    if not sizing or sizing["margin"]>available*AVAILABLE_MARGIN_BUFFER:return False
    before_ids={position_id(p) for p in get_positions() if position_id(p)}; oklev,levres=set_leverage(symbol,LEVERAGE)
    if not oklev:tg(f"⛔ LDT v1.2 LEVERAGE FAILED\n{symbol}\n{levres}");return False
    tag="ldt11"; payload={"instId":blofin_inst_id(symbol),"marginMode":MARGIN_MODE,"positionSide":"net","side":"buy" if side=="long" else "sell","orderType":"market","size":str(sizing["contracts"]),"tpTriggerPrice":str(target),"tpOrderPrice":"-1","tpTriggerPriceType":"mark","slTriggerPrice":str(sl),"slOrderPrice":"-1","slTriggerPriceType":"mark","clientOrderId":f"{tag}{int(time.time()*1000)%1000000000}"}
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    msg=(f"🎯 {'PAPER ' if PAPER_TRADING else ''}LIQUIDITY DAYTRADER v1.2 STRUCTURE VERIFIED\n{symbol} {side.upper()} · {signal.get('entry_route')}\n"
         f"{signal.get('pattern_family')}\nSTR {safe_float(signal.get('setup_quality')):.0f}/95 · EQ {eq:.0f} · first-liq net RR {net_rr:.2f}R\n"
         f"Target {target_kind} · {target:.8g}\n{trig_note}\n{signal.get('btc_note','')}\n"
         f"Entry {live:.8g} · SL {sl:.8g} · TP1/full {target:.8g}\nRisk cap {profile['risk_pct']*100:.2f}% · expected net TP ~${sizing['expected_net_tp_usdt']:.2f}\n"
         f"Notional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x")
    if PAPER_TRADING:tg(msg);_v62_last_entry_time=time.time();return True
    resp=native("private_post_trade_order",payload)
    if not native_ok(resp):
        err=response_error(resp)
        if "152012" in err:protection_lock=True
        tg(f"❌ LDT v1.2 ORDER FAILED\n{symbol} {side.upper()}\n{err}");return False
    _v62_last_entry_time=time.time();tg(msg);new_pos=find_new_position(symbol,before_ids,timeout=8)
    if new_pos is None:tg(f"⚠️ {symbol}: v1.2 order accepted, position not visible yet.");return True
    pid=position_id(new_pos); fill=position_entry(new_pos) or live
    if pid:
        sniper_managed_positions.add(pid);_ldt_meta[pid]={"risk":risk,"entry":fill,"sl":sl,"tp":target,"final_r":final_r,"side":side,"opened":time.time(),"strategy":signal.get("strategy"),"route":signal.get("entry_route"),"mfe_price":fill,"mfe_r":0.0,"version":"1.1"}
    if has_live_protection(new_pos)[0]:tg(f"🛡️ LDT v1.2 PROTECTION CONFIRMED\n{symbol} {side.upper()}\nSL/TP visible.");return True
    last=safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side,sl,target,last):
        ok,res=place_tpsl_for_position(new_pos,sl,target,client_tag=tag)
        if ok:tg(f"🛡️ LDT v1.2 FALLBACK PROTECTION CONFIRMED\n{symbol}");return True
        protection_lock=True;tg(f"🚨 LDT v1.2 PROTECTION FAILURE\n{symbol}\n{res}\nNEW TRADES PAUSED.");return True
    protection_lock=True;tg(f"🚨 {symbol}: v1.2 position exists but protection could not be verified.\nNEW TRADES PAUSED.");return True


def manage_sniper_step_stops():
    """v1.2: MFE is observed only AFTER fill. Stop amendments can never cross the live market."""
    if PAPER_TRADING:return
    for p in get_positions():
        try:
            pid=position_id(p); meta=_ldt_meta.get(pid)
            if not meta:continue
            symbol=position_symbol(p); side=position_side(p); d=1 if side=="long" else -1
            entry=safe_float(meta.get("entry")) or position_entry(p); risk=safe_float(meta.get("risk"))
            if entry<=0 or risk<=0:continue
            ok,order=has_live_protection(p)
            if not ok or not order:continue
            current_sl=safe_float(order.get("slTriggerPrice")); tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
            ticker=exchange.fetch_ticker(symbol); current=safe_float(ticker.get("last"))
            # ONLY prices observed by this manager after fill contribute to MFE.
            old_mfe=safe_float(meta.get("mfe_price"),entry)
            mfe=max(old_mfe,current) if d==1 else min(old_mfe,current); meta["mfe_price"]=mfe
            best_r=max(0.0,d*(mfe-entry)/risk); meta["mfe_r"]=best_r
            fee_px=entry*(TAKER_FEE_RATE*2+SLIPPAGE_RATE)*1.10; desired=None;stage=None
            if best_r>=1.75:desired=round_trade_price(entry+d*0.75*risk,tick,side,"sl");stage="observed +1.75R → lock +0.75R"
            elif best_r>=1.00:desired=round_trade_price(entry+d*fee_px,tick,side,"sl");stage="observed +1R → fee-safe BE"
            if desired is None:continue
            improves=desired>current_sl+tick*0.1 if d==1 else desired<current_sl-tick*0.1
            if not improves:continue
            # Hard safety: BloFin LONG SL must remain below market; SHORT SL above market.
            buf=max(tick*LDT11_STOP_MARK_BUFFER_TICKS,abs(current)*1e-6)
            safe_side=(desired < current-buf) if d==1 else (desired > current+buf)
            if not safe_side:
                continue
            ok2,res=amend_tpsl_stop(p,desired);key=f"ldt11:{pid}:{stage}"
            if ok2 and not sniper_step_notified.get(key):
                tg(f"🪜 LDT v1.2 STEP STOP\n{symbol} {side.upper()}\n{stage} · MFE {best_r:.2f}R · SL {desired:.8g}");sniper_step_notified[key]=True
        except Exception as e:
            tg(f"⚠️ LDT v1.2 POSITION MANAGER\n{position_symbol(p) if isinstance(p,dict) else '-'}\n{type(e).__name__}: {e}")


def maybe_score_report(rows):
    global _ldt_last_report
    now=time.time()
    if now-_ldt_last_report<LDT_REPORT_MIN*60 or not rows:return
    _ldt_last_report=now;ready=[r for r in rows if r.get("side") and not r.get("skip") and r.get("entry_route")]
    armed=[r for r in rows if "ARMED" in str(r.get("skip","")) or "wait-zone" in str(r.get("skip",""))]
    waits={}
    for r in rows:
        sk=str(r.get("skip") or "READY"); key="READY" if sk=="READY" else "zone/retest" if ("ARMED" in sk or "wait-zone" in sk) else "RR/liquidity" if sk.startswith("RR") else "structure" if sk.startswith("wait-") else "other"
        waits[key]=waits.get(key,0)+1
    ranked=sorted(rows,key=lambda r:(1 if not r.get("skip") else 0,safe_float(r.get("entry_quality")),safe_float(r.get("net_rr")),safe_float(r.get("setup_quality"))),reverse=True)[:8]
    lines=["🧠 LIQUIDITY DAYTRADER v1.2 · STRUCTURE VERIFIED",f"Ready {len(ready)} · Armed/retest {len(armed)} · scanned {len(rows)}","States: "+" · ".join(f"{k} {v}" for k,v in sorted(waits.items(),key=lambda x:x[1],reverse=True)[:4])]
    for r in ranked:
        status="🟢 "+str(r.get("entry_route")) if not r.get("skip") else "🟡 "+str(r.get("skip"));lines.append(f"{r.get('symbol')} {str(r.get('side')).upper()} {r.get('strategy')} STR{safe_float(r.get('setup_quality')):.0f}/95 EQ{safe_float(r.get('entry_quality')):.0f} RR1st{safe_float(r.get('net_rr')):.2f} {status}")
    tg("\n".join(lines))


def startup_diagnostics():
    acc=futures_account();reset_daily_if_needed(acc["total_equity"])
    tg(f"🧠 LIQUIDITY DAYTRADER STARTED v1.2 · STRUCTURE VERIFIED\n"
       f"v1.0 candle-score trigger removed.\n"
       f"CORE: 1H CR → 5M external sweep/reclaim → confirmed swing MSS → IFVG/Breaker → 1M swing BOS/displacement\n"
       f"FAST: 15M CR → 1M verified structure ({'ON' if LDT_ENABLE_FAST else 'OFF'})\n"
       f"CONT: breakout → retest → confirmed micro swing BOS/displacement ({'ON' if LDT_ENABLE_CONTINUATION else 'OFF'})\n"
       f"TP = first meaningful swing liquidity; far CR/measured target is no longer assumed free space.\n"
       f"MFE starts at actual fill; pre-entry highs/lows can never move the stop.\n"
       f"Min first-liquidity net RR {LDT_MIN_NET_RR:.2f}R · max positions {MAX_ACTIVE_POSITIONS} · max same side {LDT_MAX_SAME_SIDE}\n"
       f"Risk CORE {LDT11_CORE_RISK*100:.2f}% · FAST {LDT11_FAST_RISK*100:.2f}% · CONT {LDT11_CONT_RISK*100:.2f}% · ELITE cap {LDT11_ELITE_RISK*100:.2f}%\n"
       f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}")



# ============================================================
# LIQUIDITY DAYTRADER v1.2 — STRUCTURE VALIDITY WINDOW
# ============================================================
# v1.2 keeps the strict swing-based BOS/MSS from v1.1, but a valid structural
# break no longer has to occur on the newest closed trigger candle.
# A confirmation remains valid for a small number of CLOSED trigger candles,
# provided that:
#   - the opposite structural invalidation swing has not been closed through;
#   - the setup is still in/near its entry zone (existing EQ/no-chase gates);
#   - first-liquidity RR is still sufficient at execution;
#   - the live market has not already traded materially through invalidation.
# This fixes READY -> EXPIRED merely because the next 1m candle pauses/retests.

LDT_VERSION = "1.2.1"
LDT12_STRUCTURE_VALID_BARS = int(os.getenv("LDT12_STRUCTURE_VALID_BARS", "4"))
LDT12_INVALIDATION_ATR_BUFFER = float(os.getenv("LDT12_INVALIDATION_ATR_BUFFER", "0.04"))
LDT12_LIVE_INVALIDATION_ATR = float(os.getenv("LDT12_LIVE_INVALIDATION_ATR", "0.08"))


def _ldt12_structure_state(symbol, side, tf):
    """Return the newest still-valid swing BOS/MSS in the recent closed-candle window.

    Confirmation candle must satisfy the same strict v1.1 displacement rules.
    The confirmation may be 0..N-1 bars old. It stays valid until price CLOSES
    through the opposite pre-BOS swing (with a small ATR buffer). The current
    forming/live price is checked separately at execution.
    """
    df=_ldt_closed(_ldt_df(symbol,tf,180))
    if len(df)<40:
        return {"ok":False,"note":f"{tf} no-structure-data","age":999}
    d=1 if side=="long" else -1
    last_i=len(df)-1
    start=max(8, last_i-max(1,LDT12_STRUCTURE_VALID_BARS)+1)
    candidates=[]

    for i in range(start,last_i+1):
        row=df.iloc[i]; atr=safe_float(row["atr"])
        if atr<=0: continue
        # Only pivots fully confirmed BEFORE this candidate BOS candle are eligible.
        prior=df.iloc[:i].copy().reset_index(drop=True)
        highs,lows=_ldt_pivots(prior,LDT11_MICRO_LEFT,LDT11_MICRO_RIGHT)
        break_piv=(highs[-1] if d==1 and highs else lows[-1] if d==-1 and lows else None)
        invalid_piv=(lows[-1] if d==1 and lows else highs[-1] if d==-1 and highs else None)
        if not break_piv: continue
        _,level=break_piv
        c=safe_float(row["close"])
        broke=(c>level+LDT11_MICRO_BREAK_ATR*atr) if d==1 else (c<level-LDT11_MICRO_BREAK_ATR*atr)
        body,close_loc,directional=_ldt11_bar_strength(row,d,atr)
        displacement=directional and body>=LDT11_MICRO_BODY_ATR and close_loc>=LDT11_MICRO_CLOSE_LOCATION
        if not (broke and displacement): continue

        # True structure invalidation uses the opposite confirmed swing that existed
        # before the BOS. If unavailable, fall back to the BOS candle extreme.
        invalidation=(invalid_piv[1] if invalid_piv else safe_float(row["low"] if d==1 else row["high"]))
        age=last_i-i
        held=True; invalidated_at=None
        for k in range(i+1,last_i+1):
            rr=df.iloc[k]; katr=safe_float(rr["atr"],atr) or atr; cc=safe_float(rr["close"])
            if d==1 and cc < invalidation-LDT12_INVALIDATION_ATR_BUFFER*katr:
                held=False; invalidated_at=k; break
            if d==-1 and cc > invalidation+LDT12_INVALIDATION_ATR_BUFFER*katr:
                held=False; invalidated_at=k; break
        if not held: continue

        latest=df.iloc[-1]; latest_close=safe_float(latest["close"])
        ema_side=(latest_close>safe_float(latest["ema21"])) if d==1 else (latest_close<safe_float(latest["ema21"]))
        vol=safe_float(row["volume"]); vma=safe_float(row["volma"]); vol_ok=(vma<=0 or vol>=vma)
        candidates.append({
            "ok":True,"age":age,"level":level,"invalidation":invalidation,"atr":atr,
            "body":body,"close_loc":close_loc,"ema_side":ema_side,"vol_ok":vol_ok,
            "confirm_ts":int(safe_float(row.get("ts"),0)),"confirm_i":i,
        })

    if not candidates:
        # Preserve a useful current-candle diagnostic for Telegram/scans.
        a=df.iloc[-1]; atr=safe_float(a["atr"])
        prior=df.iloc[:-1].copy().reset_index(drop=True)
        highs,lows=_ldt_pivots(prior,LDT11_MICRO_LEFT,LDT11_MICRO_RIGHT)
        piv=(highs[-1] if d==1 and highs else lows[-1] if d==-1 and lows else None)
        if not piv:
            return {"ok":False,"note":f"{tf} STRUCT WAIT · no confirmed swing in {LDT12_STRUCTURE_VALID_BARS}-bar window","age":999}
        _,level=piv; c=safe_float(a["close"])
        broke=(c>level+LDT11_MICRO_BREAK_ATR*atr) if d==1 else (c<level-LDT11_MICRO_BREAK_ATR*atr)
        body,close_loc,directional=_ldt11_bar_strength(a,d,atr)
        ema_side=(c>safe_float(a["ema21"])) if d==1 else (c<safe_float(a["ema21"]))
        vol=safe_float(a["volume"]); vma=safe_float(a["volma"]); vol_ok=(vma<=0 or vol>=vma)
        note=(f"{tf} STRUCT WAIT window={LDT12_STRUCTURE_VALID_BARS} swing={level:.8g} "
              f"BOS={int(broke)} disp={body:.2f}ATR close={close_loc:.0%} EMA={int(ema_side)} vol={int(vol_ok)}")
        return {"ok":False,"note":note,"age":999,"level":level,"atr":atr}

    # Newest valid confirmation wins.
    st=max(candidates,key=lambda x:x["confirm_i"])
    st["strength"]=2+int(st["ema_side"])+int(st["vol_ok"])
    st["note"]=(f"{tf} STRUCT HOLD age={st['age']}/{LDT12_STRUCTURE_VALID_BARS-1} "
                f"BOS={st['level']:.8g} inv={st['invalidation']:.8g} "
                f"disp={st['body']:.2f}ATR close={st['close_loc']:.0%} "
                f"EMA={int(st['ema_side'])} vol={int(st['vol_ok'])}")
    return st


def _ldt_micro_trigger(symbol, side, tf):
    """v1.2: strict BOS/MSS confirmation with a multi-candle validity window."""
    st=_ldt12_structure_state(symbol,side,tf)
    return bool(st.get("ok")),int(st.get("strength",0)),str(st.get("note",f"{tf} STRUCT WAIT"))


# Keep a reference to the inherited v1.2-labelled position manager; only entry validation changes.

def open_trade(signal):
    global protection_lock, _v62_last_entry_time
    symbol,side=signal["symbol"],signal["side"]; d=1 if side=="long" else -1
    acc=futures_account(); equity,available=acc["total_equity"],acc["available"]; reset_daily_if_needed(equity)
    if equity<MIN_EQUITY_USDT or available<=0 or (not PAPER_TRADING and protection_lock):return False
    if time.time()-_v62_last_entry_time<4*60:return False

    # v1.2: a recent strict BOS/MSS may remain valid across pause/retest candles.
    # It expires only by age or actual structural invalidation, not merely BOS=0 on the newest candle.
    st=_ldt12_structure_state(symbol,side,signal.get("trigger_tf") or "1m")
    if not st.get("ok"):
        tg(f"⏭️ LDT v1.2 {symbol} {side.upper()} STRUCTURE NOT VALID\n{st.get('note','')}\nOrder cancelled before submission."); return False

    live=safe_float(exchange.fetch_ticker(symbol).get("last"),signal.get("price")); atr=safe_float(signal.get("atr")); zone=signal.get("zone") or {}
    # Hard live-price invalidation guard: don't wait for candle close if price has already
    # materially crossed the structural invalidation swing.
    inv=safe_float(st.get("invalidation")); statr=safe_float(st.get("atr"),atr) or atr
    if inv>0:
        live_invalid=(live < inv-LDT12_LIVE_INVALIDATION_ATR*statr) if d==1 else (live > inv+LDT12_LIVE_INVALIDATION_ATR*statr)
        if live_invalid:
            tg(f"⏭️ LDT v1.2 {symbol} {side.upper()} LIVE STRUCTURE INVALIDATED\n"
               f"mark {live:.8g} crossed invalidation {inv:.8g} · no order"); return False

    eq=_ldt_zone_entry_quality(live,zone,atr,d)
    if eq<LDT_MIN_ENTRY_QUALITY:
        tg(f"⏭️ LDT v1.2 {symbol} {side.upper()} ENTRY MOVED\nEQ {eq:.0f} < {LDT_MIN_ENTRY_QUALITY:.0f} · no chase");return False
    signal_px=safe_float(signal.get("price"),live); drift=d*(live-signal_px)/max(atr,1e-12)
    if drift>LDT_LIVE_DRIFT_ATR:
        tg(f"⏭️ LDT v1.2 {symbol} {side.upper()} TOO LATE\nAdverse drift {drift:.2f}ATR > {LDT_LIVE_DRIFT_ATR:.2f}ATR");return False
    tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001); sl=safe_float(signal.get("structural_stop"))
    if live<=0 or sl<=0:return False
    risk=d*(live-sl)
    if risk<=0:return False
    hard=safe_float(signal.get("far_target") or signal.get("target_price")); ltf="1m" if signal.get("route")=="FAST" else "5m"
    target,target_kind,gross_rr=_ldt11_first_liquidity_target(symbol,side,live,sl,hard,ltf)
    sl=round_trade_price(sl,tick,side,"sl"); target=round_trade_price(target,tick,side,"tp")
    risk=abs(live-sl); final_r=d*(target-live)/max(risk,1e-12)
    cost_r=live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12); net_rr=final_r-cost_r
    if net_rr<LDT_MIN_NET_RR:
        tg(f"⏭️ LDT v1.2 {symbol} {side.upper()} FIRST-LIQUIDITY RR BLOCK\n{target_kind} · net RR {net_rr:.2f} < {LDT_MIN_NET_RR:.2f}");return False
    profile=sizing_profile(signal); sizing=calculate_contracts_v5(symbol,live,sl,equity,available,profile["risk_pct"],profile["target_net_tp"],final_r)
    if not sizing or sizing["margin"]>available*AVAILABLE_MARGIN_BUFFER:return False
    before_ids={position_id(p) for p in get_positions() if position_id(p)}; oklev,levres=set_leverage(symbol,LEVERAGE)
    if not oklev:tg(f"⛔ LDT v1.2 LEVERAGE FAILED\n{symbol}\n{levres}");return False
    tag="ldt12"; payload={"instId":blofin_inst_id(symbol),"marginMode":MARGIN_MODE,"positionSide":"net","side":"buy" if side=="long" else "sell","orderType":"market","size":str(sizing["contracts"]),"tpTriggerPrice":str(target),"tpOrderPrice":"-1","tpTriggerPriceType":"mark","slTriggerPrice":str(sl),"slOrderPrice":"-1","slTriggerPriceType":"mark","clientOrderId":f"{tag}{int(time.time()*1000)%1000000000}"}
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    msg=(f"🎯 {'PAPER ' if PAPER_TRADING else ''}LIQUIDITY DAYTRADER v1.2 STRUCTURE WINDOW\n{symbol} {side.upper()} · {signal.get('entry_route')}\n"
         f"{signal.get('pattern_family')}\nSTR {safe_float(signal.get('setup_quality')):.0f}/95 · EQ {eq:.0f} · first-liq net RR {net_rr:.2f}R\n"
         f"Target {target_kind} · {target:.8g}\n{st.get('note','')}\n{signal.get('btc_note','')}\n"
         f"Entry {live:.8g} · SL {sl:.8g} · TP1/full {target:.8g}\nRisk cap {profile['risk_pct']*100:.2f}% · expected net TP ~${sizing['expected_net_tp_usdt']:.2f}\n"
         f"Notional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x")
    if PAPER_TRADING:tg(msg);_v62_last_entry_time=time.time();return True
    resp=native("private_post_trade_order",payload)
    if not native_ok(resp):
        err=response_error(resp)
        if "152012" in err:protection_lock=True
        tg(f"❌ LDT v1.2 ORDER FAILED\n{symbol} {side.upper()}\n{err}");return False
    _v62_last_entry_time=time.time();tg(msg);new_pos=find_new_position(symbol,before_ids,timeout=8)
    if new_pos is None:tg(f"⚠️ {symbol}: v1.2 order accepted, position not visible yet.");return True
    pid=position_id(new_pos); fill=position_entry(new_pos) or live
    if pid:
        sniper_managed_positions.add(pid);_ldt_meta[pid]={"risk":risk,"entry":fill,"sl":sl,"tp":target,"final_r":final_r,"side":side,"opened":time.time(),"strategy":signal.get("strategy"),"route":signal.get("entry_route"),"mfe_price":fill,"mfe_r":0.0,"version":"1.2","structure_confirm_ts":st.get("confirm_ts"),"structure_invalidation":inv}
    if has_live_protection(new_pos)[0]:tg(f"🛡️ LDT v1.2 PROTECTION CONFIRMED\n{symbol} {side.upper()}\nSL/TP visible.");return True
    last=safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side,sl,target,last):
        ok,res=place_tpsl_for_position(new_pos,sl,target,client_tag=tag)
        if ok:tg(f"🛡️ LDT v1.2 FALLBACK PROTECTION CONFIRMED\n{symbol}");return True
        protection_lock=True;tg(f"🚨 LDT v1.2 PROTECTION FAILURE\n{symbol}\n{res}\nNEW TRADES PAUSED.");return True
    protection_lock=True;tg(f"🚨 {symbol}: v1.2 position exists but protection could not be verified.\nNEW TRADES PAUSED.");return True


def maybe_score_report(rows):
    global _ldt_last_report
    now=time.time()
    if now-_ldt_last_report<LDT_REPORT_MIN*60 or not rows:return
    _ldt_last_report=now;ready=[r for r in rows if r.get("side") and not r.get("skip") and r.get("entry_route")]
    armed=[r for r in rows if "ARMED" in str(r.get("skip","")) or "wait-zone" in str(r.get("skip",""))]
    waits={}
    for r in rows:
        sk=str(r.get("skip") or "READY"); key="READY" if sk=="READY" else "zone/retest" if ("ARMED" in sk or "wait-zone" in sk) else "RR/liquidity" if sk.startswith("RR") else "structure" if sk.startswith("wait-") else "other"
        waits[key]=waits.get(key,0)+1
    ranked=sorted(rows,key=lambda r:(1 if not r.get("skip") else 0,safe_float(r.get("entry_quality")),safe_float(r.get("net_rr")),safe_float(r.get("setup_quality"))),reverse=True)[:8]
    lines=["🧠 LIQUIDITY DAYTRADER v1.2.1 · STRUCTURE WINDOW",f"Ready {len(ready)} · Armed/retest {len(armed)} · scanned {len(rows)}","States: "+" · ".join(f"{k} {v}" for k,v in sorted(waits.items(),key=lambda x:x[1],reverse=True)[:4])]
    for r in ranked:
        status="🟢 "+str(r.get("entry_route")) if not r.get("skip") else "🟡 "+str(r.get("skip"));lines.append(f"{r.get('symbol')} {str(r.get('side')).upper()} {r.get('strategy')} STR{safe_float(r.get('setup_quality')):.0f}/95 EQ{safe_float(r.get('entry_quality')):.0f} RR1st{safe_float(r.get('net_rr')):.2f} {status}")
    tg("\n".join(lines))


def startup_diagnostics():
    acc=futures_account();reset_daily_if_needed(acc["total_equity"])
    tg(f"🧠 LIQUIDITY DAYTRADER STARTED v1.2.1 · STRUCTURE WINDOW\n"
       f"Strict swing BOS/MSS retained; confirmation valid for {LDT12_STRUCTURE_VALID_BARS} closed trigger candles.\n"
       f"No repeated BOS required on every next 1M candle. Structure expires only by age or real swing invalidation.\n"
       f"CORE: 1H CR → 5M sweep/reclaim → MSS → IFVG/Breaker → 1M structure window\n"
       f"FAST: 15M CR → 1M structure window ({'ON' if LDT_ENABLE_FAST else 'OFF'})\n"
       f"CONT: breakout → retest → swing BOS/displacement → validity window ({'ON' if LDT_ENABLE_CONTINUATION else 'OFF'})\n"
       f"Execution still rechecks EQ/no-chase + live invalidation + first-liquidity RR.\n"
       f"Min first-liquidity net RR {LDT_MIN_NET_RR:.2f}R · max positions {MAX_ACTIVE_POSITIONS} · max same side {LDT_MAX_SAME_SIDE}\n"
       f"Risk CORE {LDT11_CORE_RISK*100:.2f}% · FAST {LDT11_FAST_RISK*100:.2f}% · CONT {LDT11_CONT_RISK*100:.2f}% · ELITE cap {LDT11_ELITE_RISK*100:.2f}%\n"
       f"Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}")


# ============================================================
# LIQUIDITY DAYTRADER v1.3 — STATEFUL LIQUIDITY HUNTER
# ============================================================
# Architectural change from v1.2.1:
#   - setups are no longer treated as one-scan snapshots;
#   - a valid thesis is stored and followed across scans;
#   - state progression: TRACKING -> WAIT_RETRACE -> IN_ZONE ->
#     STRUCT_HOLD/READY -> EXECUTE, or INVALIDATED/EXPIRED;
#   - 4-candle structure window from v1.2.1 is retained;
#   - RR/EQ/stop/live invalidation are re-evaluated continuously;
#   - all viable candidates per symbol are considered, avoiding a high-EQ
#     but already-spent setup hiding a better state-worthy setup.

LDT_VERSION = "1.3.0"
LDT13_STATE_FILE = os.getenv("LDT13_STATE_FILE", "ldt13_state.json")
LDT13_CORE_TTL_MIN = int(os.getenv("LDT13_CORE_TTL_MIN", "120"))
LDT13_CONT_TTL_MIN = int(os.getenv("LDT13_CONT_TTL_MIN", "90"))
LDT13_FAST_TTL_MIN = int(os.getenv("LDT13_FAST_TTL_MIN", "35"))
LDT13_MIN_TRACK_RR = float(os.getenv("LDT13_MIN_TRACK_RR", str(LDT_MIN_NET_RR)))
LDT13_MIN_TRACK_STR = float(os.getenv("LDT13_MIN_TRACK_STR", str(LDT_MIN_SETUP_QUALITY)))
LDT13_REPLACE_EDGE = float(os.getenv("LDT13_REPLACE_EDGE", "5"))
LDT13_NOTIFY_TRANSITIONS = os.getenv("LDT13_NOTIFY_TRANSITIONS", "true").lower() == "true"

_ldt13_states = {}
_ldt13_loaded = False
_ldt13_last_report = 0.0
_ldt13_base_open_trade = open_trade


def _ldt13_jsonable(v):
    if isinstance(v, dict):
        return {str(k): _ldt13_jsonable(x) for k, x in v.items() if k not in ("df", "sub")}
    if isinstance(v, (list, tuple)):
        return [_ldt13_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    try:
        if hasattr(v, "item"):
            return v.item()
    except Exception:
        pass
    try:
        return float(v)
    except Exception:
        return str(v)


def _ldt13_save():
    try:
        tmp = LDT13_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_ldt13_jsonable(_ldt13_states), f, ensure_ascii=False, indent=2)
        os.replace(tmp, LDT13_STATE_FILE)
    except Exception:
        pass


def _ldt13_load():
    global _ldt13_loaded, _ldt13_states
    if _ldt13_loaded:
        return
    _ldt13_loaded = True
    try:
        if os.path.exists(LDT13_STATE_FILE):
            with open(LDT13_STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                _ldt13_states = raw
    except Exception:
        _ldt13_states = {}


def _ldt13_ttl_min(signal):
    if str(signal.get("route")) == "FAST":
        return LDT13_FAST_TTL_MIN
    if str(signal.get("strategy")) == "BREAK-RETEST":
        return LDT13_CONT_TTL_MIN
    return LDT13_CORE_TTL_MIN


def _ldt13_scan_candidates(symbol):
    """Return ALL current LDT candidates instead of only the best snapshot."""
    candidates = []
    pairs = [("CORE", "1h", "5m", "1m")]
    if LDT_ENABLE_FAST:
        pairs.append(("FAST", "15m", "1m", "1m"))
    for route, ptf, ltf, ttf in pairs:
        try:
            parents = _ldt_parent_candidates(symbol, ptf)
        except Exception:
            continue
        for parent in parents[:3]:
            try:
                rev = _ldt_find_sweep_mss(symbol, parent, ltf)
                if rev:
                    s = _ldt_build_reversal(symbol, parent, rev, route, ltf, ttf)
                    if s:
                        candidates.append(s)
                if LDT_ENABLE_CONTINUATION:
                    cont = _ldt_break_retest(symbol, parent, ltf)
                    if cont:
                        s = _ldt_build_cont(symbol, parent, cont, route, ltf, ttf)
                        if s:
                            candidates.append(s)
            except Exception:
                continue
    return candidates


def _ldt13_risk_atr(signal):
    atr = safe_float(signal.get("atr"))
    risk = abs(safe_float(signal.get("risk_distance")))
    if atr <= 0:
        return 999.0
    return risk / atr


def _ldt13_trackable(signal):
    if not signal or not signal.get("side"):
        return False
    sk = str(signal.get("skip") or "")
    if sk.startswith("stale") or sk.startswith("stop-"):
        return False
    if safe_float(signal.get("setup_quality")) < LDT13_MIN_TRACK_STR:
        return False
    ra = _ldt13_risk_atr(signal)
    if ra < LDT_MIN_STOP_ATR or ra > LDT_MAX_STOP_ATR:
        return False
    # A new thesis must still have real economic room when discovered.
    if safe_float(signal.get("net_rr")) < LDT13_MIN_TRACK_RR:
        return False
    return True


def _ldt13_candidate_rank(s):
    sk = str(s.get("skip") or "")
    ready = 1 if not sk and s.get("entry_route") else 0
    structural = 1 if ("STRUCT" in str(s.get("trigger_note", "")) or sk.startswith("wait-")) else 0
    return (ready, structural, safe_float(s.get("setup_quality")), safe_float(s.get("entry_quality")), safe_float(s.get("net_rr")))


def _ldt13_best_trackable(candidates):
    xs = [s for s in candidates if _ldt13_trackable(s)]
    return max(xs, key=_ldt13_candidate_rank) if xs else None


def _ldt13_best_diagnostic(candidates):
    if not candidates:
        return None
    return max(candidates, key=lambda s: (
        1 if not s.get("skip") else 0,
        safe_float(s.get("setup_quality")),
        safe_float(s.get("entry_quality")),
        safe_float(s.get("net_rr")),
    ))


def _ldt13_new_state(signal):
    now = time.time()
    return {
        "symbol": signal.get("symbol"),
        "side": signal.get("side"),
        "strategy": signal.get("strategy"),
        "route": signal.get("route"),
        "created": now,
        "updated": now,
        "expires": now + 60 * _ldt13_ttl_min(signal),
        "state": "TRACKING",
        "last_state": "",
        "signal": _ldt13_jsonable(signal),
        "initial_target": safe_float(signal.get("target_price")),
        "initial_far_target": safe_float(signal.get("far_target") or signal.get("target_price")),
        "best_rr": safe_float(signal.get("net_rr")),
        "best_eq": safe_float(signal.get("entry_quality")),
        "transitions": 0,
    }


def _ldt13_transition(st, new_state, note=""):
    old = str(st.get("state") or "")
    st["last_state"] = old
    st["state"] = new_state
    st["state_note"] = note
    st["updated"] = time.time()
    if old != new_state:
        st["transitions"] = int(st.get("transitions", 0)) + 1
        if LDT13_NOTIFY_TRANSITIONS and new_state in ("IN_ZONE", "STRUCT_HOLD", "READY", "INVALIDATED", "EXPIRED"):
            sym = st.get("symbol"); side = str(st.get("side", "")).upper()
            tg(f"🧭 LDT v1.3 {sym} {side} · {new_state}\n{note}")
    return old != new_state


def _ldt13_merge_fresh(st, fresh):
    if not fresh:
        return
    oldsig = st.get("signal") or {}
    # Preserve the thesis identity, but refresh all current economics/zone fields.
    merged = dict(oldsig)
    merged.update(_ldt13_jsonable(fresh))
    st["signal"] = merged
    st["updated"] = time.time()
    st["best_rr"] = max(safe_float(st.get("best_rr")), safe_float(fresh.get("net_rr")))
    st["best_eq"] = max(safe_float(st.get("best_eq")), safe_float(fresh.get("entry_quality")))


def _ldt13_same_thesis(st, s):
    return bool(s and str(st.get("side")) == str(s.get("side")) and str(st.get("strategy")) == str(s.get("strategy")) and str(st.get("route")) == str(s.get("route")))


def _ldt13_refresh_state(st):
    """Re-price an existing thesis without demanding the entire setup be rediscovered."""
    signal = dict(st.get("signal") or {})
    symbol = st.get("symbol"); side = st.get("side")
    if not symbol or side not in ("long", "short"):
        _ldt13_transition(st, "INVALIDATED", "missing thesis identity")
        return None
    d = 1 if side == "long" else -1
    now = time.time()
    if now > safe_float(st.get("expires")):
        _ldt13_transition(st, "EXPIRED", f"thesis age > {_ldt13_ttl_min(signal)} min")
        return None
    try:
        live = safe_float(exchange.fetch_ticker(symbol).get("last"), safe_float(signal.get("price")))
    except Exception:
        live = safe_float(signal.get("price"))
    atr = safe_float(signal.get("atr")); stop = safe_float(signal.get("structural_stop")); zone = signal.get("zone") or {}
    if live <= 0 or atr <= 0 or stop <= 0 or not zone:
        _ldt13_transition(st, "INVALIDATED", "missing live/ATR/stop/zone")
        return None

    # The original structural stop is the thesis invalidation. A tiny live buffer
    # prevents a one-tick print from killing the thesis prematurely.
    invalid_buf = LDT12_LIVE_INVALIDATION_ATR * atr
    if (d == 1 and live < stop - invalid_buf) or (d == -1 and live > stop + invalid_buf):
        _ldt13_transition(st, "INVALIDATED", f"price {live:.8g} crossed thesis invalidation {stop:.8g}")
        return None

    risk = d * (live - stop)
    if risk <= 0:
        _ldt13_transition(st, "INVALIDATED", "price crossed structural stop")
        return None
    risk_atr = risk / atr
    if risk_atr > LDT_MAX_STOP_ATR:
        _ldt13_transition(st, "WAIT_RETRACE", f"stop currently {risk_atr:.2f}ATR > {LDT_MAX_STOP_ATR:.2f}ATR")
    elif risk_atr < LDT_MIN_STOP_ATR:
        _ldt13_transition(st, "WAIT_RETRACE", f"stop currently {risk_atr:.2f}ATR < {LDT_MIN_STOP_ATR:.2f}ATR")

    initial_target = safe_float(st.get("initial_target") or signal.get("target_price"))
    # The first-liquidity objective is frozen when the thesis is born. Do not
    # move the goalposts forward after price already delivered the original target.
    if initial_target > 0 and ((d == 1 and live >= initial_target) or (d == -1 and live <= initial_target)):
        _ldt13_transition(st, "EXPIRED", f"original first liquidity {initial_target:.8g} already delivered")
        return None
    far = initial_target or safe_float(st.get("initial_far_target") or signal.get("far_target") or signal.get("target_price"))
    ltf = "1m" if str(signal.get("route")) == "FAST" else "5m"
    target, target_kind, gross_rr = _ldt11_first_liquidity_target(symbol, side, live, stop, far, ltf)
    cost_r = live * (2 * TAKER_FEE_RATE + SLIPPAGE_RATE) / max(risk, 1e-12)
    net_rr = gross_rr - cost_r
    eq = _ldt_zone_entry_quality(live, zone, atr, d)
    lo = safe_float(zone.get("lo")); hi = safe_float(zone.get("hi"))
    in_zone = (lo - LDT_ZONE_TOL_ATR * atr <= live <= hi + LDT_ZONE_TOL_ATR * atr)

    trigger_tf = signal.get("trigger_tf") or "1m"
    struct = _ldt12_structure_state(symbol, side, trigger_tf)
    struct_ok = bool(struct.get("ok"))

    signal.update({
        "price": live,
        "entry_quality": eq,
        "quality": 0.60 * safe_float(signal.get("setup_quality")) + 0.40 * eq,
        "risk_distance": risk,
        "target_price": target,
        "target_kind": target_kind,
        "final_r": gross_rr,
        "net_rr": net_rr,
        "trigger_note": struct.get("note", ""),
        "confirmations": int(struct.get("strength", 0)) + 1,
        "ldt13_state": st.get("state"),
        "ldt13_age_min": (now - safe_float(st.get("created"))) / 60.0,
    })
    st["signal"] = _ldt13_jsonable(signal)
    st["best_rr"] = max(safe_float(st.get("best_rr")), net_rr)
    st["best_eq"] = max(safe_float(st.get("best_eq")), eq)

    if risk_atr < LDT_MIN_STOP_ATR or risk_atr > LDT_MAX_STOP_ATR:
        signal["skip"] = f"TRACK-stop-{risk_atr:.2f}ATR"
        signal["entry_route"] = None
        return signal
    if net_rr < LDT_MIN_NET_RR:
        _ldt13_transition(st, "WAIT_RETRACE", f"first-liq RR {net_rr:.2f}R; wait for better price")
        signal["skip"] = f"TRACK-RR {net_rr:.2f}<{LDT_MIN_NET_RR:.2f}"
        signal["entry_route"] = None
        return signal
    if not in_zone or eq < LDT_MIN_ENTRY_QUALITY:
        _ldt13_transition(st, "WAIT_RETRACE", f"EQ {eq:.0f}; waiting zone {lo:.8g}-{hi:.8g}")
        signal["skip"] = f"TRACK-wait-retrace EQ{eq:.0f}"
        signal["entry_route"] = None
        return signal
    if not struct_ok:
        _ldt13_transition(st, "IN_ZONE", f"zone reached · {struct.get('note','waiting structure')}")
        signal["skip"] = f"TRACK-in-zone · {struct.get('note','STRUCT WAIT')}"
        signal["entry_route"] = None
        return signal

    age = int(struct.get("age", 0))
    _ldt13_transition(st, "READY" if age == 0 else "STRUCT_HOLD", struct.get("note", "structure valid"))
    signal["skip"] = None
    base_route = str(signal.get("entry_route") or "")
    if not base_route:
        base_route = ("CRT-" + str(signal.get("route", "CORE")) + "-SV") if signal.get("strategy") == "CRT-MSS" else ("CONT-" + str(signal.get("route", "CORE")) + "-SV")
    signal["entry_route"] = base_route + "-STATE"
    # Let main retry on a later minute if execution revalidation blocks this attempt.
    signal["signal_bar_ts"] = int(time.time() // 60)
    return signal


def evaluate_symbol(symbol, btc=None):
    """v1.3: discover once, then follow the thesis across later scans."""
    _ldt13_load()
    candidates = _ldt13_scan_candidates(symbol)
    fresh = _ldt13_best_trackable(candidates)
    st = _ldt13_states.get(symbol)

    if st:
        # A strong opposite thesis may replace an old one; don't keep stale directional bias forever.
        if fresh and str(fresh.get("side")) != str(st.get("side")):
            oldq = safe_float((st.get("signal") or {}).get("setup_quality"))
            if safe_float(fresh.get("setup_quality")) >= oldq + LDT13_REPLACE_EDGE:
                _ldt13_transition(st, "INVALIDATED", f"replaced by stronger {fresh.get('side')} thesis")
                st = _ldt13_new_state(fresh)
                _ldt13_states[symbol] = st
        elif fresh and _ldt13_same_thesis(st, fresh):
            _ldt13_merge_fresh(st, fresh)

        out = _ldt13_refresh_state(st)
        if out is None or st.get("state") in ("INVALIDATED", "EXPIRED"):
            _ldt13_states.pop(symbol, None)
            _ldt13_save()
            # If a new independent thesis exists, start it immediately.
            if fresh and _ldt13_trackable(fresh):
                st = _ldt13_new_state(fresh); _ldt13_states[symbol] = st
                out = _ldt13_refresh_state(st)
            else:
                return _ldt13_best_diagnostic(candidates)
        _ldt13_save()
        return out

    if fresh:
        st = _ldt13_new_state(fresh)
        _ldt13_states[symbol] = st
        out = _ldt13_refresh_state(st)
        _ldt13_save()
        return out

    return _ldt13_best_diagnostic(candidates)


def open_trade(signal):
    """v1.3 delegates exchange execution to the hardened v1.2.1 shell after a final state refresh."""
    symbol = signal.get("symbol")
    st = _ldt13_states.get(symbol)
    if st:
        fresh = _ldt13_refresh_state(st)
        _ldt13_save()
        if not fresh or fresh.get("skip") or not fresh.get("entry_route"):
            tg(f"⏭️ LDT v1.3 {symbol} {str(signal.get('side')).upper()} STATE CHANGED\n"
               f"{(fresh or {}).get('skip') or st.get('state_note','not ready')}\nOrder cancelled before submission.")
            return False
        signal = fresh
    # Reset snapshot price to the current state price so the inherited no-chase
    # guard measures execution drift, not thesis age.
    try:
        signal["price"] = safe_float(exchange.fetch_ticker(symbol).get("last"), signal.get("price"))
    except Exception:
        pass
    opened = _ldt13_base_open_trade(signal)
    if opened and symbol in _ldt13_states:
        _ldt13_states.pop(symbol, None)
        _ldt13_save()
    return opened


def maybe_score_report(rows):
    global _ldt13_last_report
    now = time.time()
    if now - _ldt13_last_report < LDT_REPORT_MIN * 60 or not rows:
        return
    _ldt13_last_report = now
    _ldt13_load()
    ready = [r for r in rows if r.get("side") and not r.get("skip") and r.get("entry_route")]
    tracked = [r for r in rows if str(r.get("skip", "")).startswith("TRACK")]
    state_counts = {}
    for st in _ldt13_states.values():
        k = str(st.get("state") or "TRACKING")
        state_counts[k] = state_counts.get(k, 0) + 1
    ranked = sorted(rows, key=lambda r: (
        1 if not r.get("skip") else 0,
        1 if str(r.get("skip", "")).startswith("TRACK") else 0,
        safe_float(r.get("entry_quality")), safe_float(r.get("net_rr")), safe_float(r.get("setup_quality"))
    ), reverse=True)[:9]
    state_text = " · ".join(f"{k} {v}" for k, v in sorted(state_counts.items(), key=lambda x: x[1], reverse=True)[:5]) or "none"
    lines = [
        "🧠 LIQUIDITY DAYTRADER v1.3 · STATEFUL LIQUIDITY HUNTER",
        f"Ready {len(ready)} · Tracked {len(_ldt13_states)} · scanned {len(rows)}",
        f"Theses: {state_text}",
    ]
    for r in ranked:
        st = _ldt13_states.get(r.get("symbol"), {})
        age = (now - safe_float(st.get("created"), now)) / 60.0 if st else 0.0
        if not r.get("skip"):
            status = "🟢 EXECUTE"
        elif str(r.get("skip", "")).startswith("TRACK"):
            status = f"🟠 {st.get('state','TRACK')} {age:.0f}m"
        else:
            status = "🟡 " + str(r.get("skip"))
        lines.append(
            f"{r.get('symbol')} {str(r.get('side')).upper()} {r.get('strategy')} "
            f"STR{safe_float(r.get('setup_quality')):.0f}/95 EQ{safe_float(r.get('entry_quality')):.0f} "
            f"RR1st{safe_float(r.get('net_rr')):.2f} {status}"
        )
    tg("\n".join(lines))


def startup_diagnostics():
    _ldt13_load()
    acc = futures_account(); reset_daily_if_needed(acc["total_equity"])
    tg(
        "🧠 LIQUIDITY DAYTRADER STARTED v1.3 · STATEFUL LIQUIDITY HUNTER\n"
        "A setup is now remembered and followed across scans instead of requiring every condition simultaneously.\n"
        "State flow: TRACKING → WAIT_RETRACE → IN_ZONE → STRUCT_HOLD/READY → EXECUTE.\n"
        "Invalidation: structural stop crossed, first liquidity already delivered, or thesis TTL expires.\n"
        f"TTL CORE {LDT13_CORE_TTL_MIN}m · CONT {LDT13_CONT_TTL_MIN}m · FAST {LDT13_FAST_TTL_MIN}m.\n"
        f"Structure confirmation window remains {LDT12_STRUCTURE_VALID_BARS} closed 1m candles.\n"
        f"Min first-liquidity net RR {LDT_MIN_NET_RR:.2f}R · stop {LDT_MIN_STOP_ATR:.2f}-{LDT_MAX_STOP_ATR:.2f}ATR.\n"
        f"Risk CORE {LDT11_CORE_RISK*100:.2f}% · FAST {LDT11_FAST_RISK*100:.2f}% · CONT {LDT11_CONT_RISK*100:.2f}% · ELITE cap {LDT11_ELITE_RISK*100:.2f}%.\n"
        f"Max positions {MAX_ACTIVE_POSITIONS} · max same side {LDT_MAX_SAME_SIDE}.\n"
        f"Restored tracked theses: {len(_ldt13_states)} · Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}"
    )



# ============================================================
# LIQUIDITY DAYTRADER v1.3.1 — TRADING-CARD PROSPECTIVE HUNTER
# ============================================================
# Fixes the v1.3 state-machine bootstrap problem:
#   - a thesis is allowed to exist BEFORE the current market price offers 1.65R;
#   - the bot calculates RR@ZONE from the planned IFVG/Breaker/retest zone;
#   - actual exchange execution STILL requires live first-liquidity net RR >= 1.65R;
#   - the supplied Institutional Price Action cheat-sheet is used as an
#     objective pattern-family layer (QM/2B/Fakeout, MPL/Re-entry, Flag/
#     Continuation QM, Compression/Can-Can, V-tail, 3-Drive, Double SSR).
# The card layer ranks and explains a thesis; it never bypasses BOS/MSS,
# structural invalidation, stop geometry, live RR, or protection checks.

LDT_VERSION = "1.3.1"
LDT13_STATE_FILE = os.getenv("LDT131_STATE_FILE", "ldt131_state.json")
LDT131_MIN_PROSPECTIVE_RR = float(os.getenv("LDT131_MIN_PROSPECTIVE_RR", str(LDT_MIN_NET_RR)))
LDT131_MIN_CARD_EVIDENCE = int(os.getenv("LDT131_MIN_CARD_EVIDENCE", "2"))
LDT131_MIN_THESIS_STR = float(os.getenv("LDT131_MIN_THESIS_STR", "70"))
LDT131_RETRACE_TARGET_LOOKBACK = int(os.getenv("LDT131_RETRACE_TARGET_LOOKBACK", "30"))
LDT131_CARD_BONUS_MAX = float(os.getenv("LDT131_CARD_BONUS_MAX", "8"))

# Fresh state namespace: do not import incompatible v1.3 snapshot states.
_ldt13_states = {}
_ldt13_loaded = False
_ldt13_last_report = 0.0
_ldt131_base_scan_candidates = _ldt13_scan_candidates
_ldt131_base_open_trade = open_trade


def _ldt131_ltf(signal):
    if str(signal.get("route")) == "FAST":
        return "1m"
    return "5m"


def _ldt131_card_profile(signal):
    """Translate the supplied cheat-sheet into objective, non-magical evidence."""
    side = str(signal.get("side") or "")
    if side not in ("long", "short"):
        return {"score": 0, "families": [], "label": "NONE"}
    d = 1 if side == "long" else -1
    strategy = str(signal.get("strategy") or "")
    zone = signal.get("zone") or {}
    families = []
    score = 0

    if strategy == "CRT-MSS":
        families.append("QM/2B-FAKEOUT")
        score += 2
        if safe_float(signal.get("sweep_depth_atr")) >= 0.10:
            families.append("STOP-HUNT")
            score += 1
        if "IFVG" in str(zone.get("kind", "")) or safe_float(zone.get("confluence")) >= 2:
            families.append("QM-REENTRY/MPL")
            score += 2
    elif strategy == "BREAK-RETEST":
        families.append("CONT-QM/FLAG")
        score += 2
        if "BREAKER" in str(zone.get("kind", "")):
            families.append("FLAG-RETEST")
            score += 1

    tf = _ldt131_ltf(signal)
    try:
        df = _ldt_closed(_ldt_df(signal.get("symbol"), tf, 100))
        atr = safe_float(df.iloc[-1]["atr"])
        if atr > 0:
            # Compression / Can-Can family: several shrinking bars pressing one way.
            if _ldt_compression(df.tail(7), side):
                families.append("COMPRESSION/CAN-CAN")
                score += 1

            highs, lows = _ldt_pivots(df.tail(70).reset_index(drop=True), 2, 2)
            piv = highs if d == -1 else lows
            vals = [safe_float(v) for _, v in piv[-4:]]
            # 3-Drive: successive liquidity pushes into the eventual reversal side.
            if len(vals) >= 3:
                tri = vals[-3:]
                drives = (tri[0] < tri[1] < tri[2]) if d == -1 else (tri[0] > tri[1] > tri[2])
                if drives:
                    families.append("3-DRIVE")
                    score += 1
                # Double SSR / ruler-like equal liquidity shelf.
                spread = max(tri) - min(tri)
                if spread <= 0.30 * atr:
                    families.append("DOUBLE-SSR/RULER")
                    score += 1

            # V-tail / DM-shadow style rejection near a local extreme.
            recent = df.tail(10)
            for _, r in recent.iterrows():
                o = safe_float(r["open"]); c = safe_float(r["close"])
                h = safe_float(r["high"]); l = safe_float(r["low"])
                body = max(abs(c-o), 0.05*atr)
                upper = h-max(o,c); lower = min(o,c)-l
                if d == -1 and upper >= 1.35*body and h >= safe_float(recent["high"].max())-0.10*atr:
                    families.append("V-TAIL/DM-SHADOW")
                    score += 1; break
                if d == 1 and lower >= 1.35*body and l <= safe_float(recent["low"].min())+0.10*atr:
                    families.append("V-TAIL/DM-SHADOW")
                    score += 1; break
    except Exception:
        pass

    # Deduplicate while preserving order.
    families = list(dict.fromkeys(families))
    score = min(8, score)
    return {"score": score, "families": families, "label": "+".join(families[:3]) if families else "BASE-STRUCTURE"}


def _ldt131_prospective_plan(signal):
    """Evaluate economics at the PLANNED retrace zone, not at the chased live price."""
    side = str(signal.get("side") or "")
    zone = signal.get("zone") or {}
    if side not in ("long", "short") or not zone:
        return None
    d = 1 if side == "long" else -1
    atr = safe_float(signal.get("atr")); stop = safe_float(signal.get("structural_stop"))
    lo = safe_float(zone.get("lo")); hi = safe_float(zone.get("hi"))
    if atr <= 0 or stop <= 0 or lo <= 0 or hi <= 0 or hi < lo:
        return None

    # Mid-zone is deliberately conservative. We do not assume a perfect edge fill.
    entry = (lo + hi) / 2.0
    risk = d * (entry - stop)
    if risk <= 0:
        return None
    risk_atr = risk / atr
    if risk_atr < LDT_MIN_STOP_ATR or risk_atr > LDT_MAX_STOP_ATR:
        return {"ok": False, "entry": entry, "risk_atr": risk_atr, "net_rr": -99.0,
                "target": 0.0, "target_kind": "invalid-stop-geometry"}

    far = safe_float(signal.get("far_target") or signal.get("target_price"))
    tf = _ldt131_ltf(signal)
    target = 0.0; kind = "prospective-liquidity"; gross = -99.0
    try:
        target, kind, gross = _ldt11_first_liquidity_target(signal.get("symbol"), side, entry, stop, far, tf)
    except Exception:
        pass

    # If the snapshot's old first-liquidity is already behind price, use the
    # recent delivered extreme as the natural retest target after a retrace.
    # This mirrors the cheat-sheet's re-entry / fakeout logic without chasing.
    if gross <= 0:
        try:
            df = _ldt_closed(_ldt_df(signal.get("symbol"), tf, 100))
            recent = df.tail(max(12, LDT131_RETRACE_TARGET_LOOKBACK))
            alt = safe_float(recent["high"].max()) if d == 1 else safe_float(recent["low"].min())
            alt_gross = d * (alt - entry) / max(risk, 1e-12)
            if alt_gross > gross:
                target, gross, kind = alt, alt_gross, "recent-delivery-liquidity"
        except Exception:
            pass

    cost_r = entry * (2 * TAKER_FEE_RATE + SLIPPAGE_RATE) / max(risk, 1e-12)
    net_rr = gross - cost_r
    return {"ok": bool(net_rr >= LDT131_MIN_PROSPECTIVE_RR), "entry": entry, "risk_atr": risk_atr,
            "target": target, "target_kind": kind, "gross_rr": gross, "net_rr": net_rr,
            "zone_lo": lo, "zone_hi": hi}


def _ldt131_annotate(signal):
    if not signal:
        return signal
    card = _ldt131_card_profile(signal)
    plan = _ldt131_prospective_plan(signal)
    signal["card_score"] = int(card.get("score", 0))
    signal["card_families"] = card.get("families", [])
    signal["card_label"] = card.get("label", "BASE-STRUCTURE")
    if plan:
        signal["prospective_entry"] = safe_float(plan.get("entry"))
        signal["prospective_rr"] = safe_float(plan.get("net_rr"))
        signal["prospective_target"] = safe_float(plan.get("target"))
        signal["prospective_target_kind"] = plan.get("target_kind")
        signal["prospective_risk_atr"] = safe_float(plan.get("risk_atr"))
        signal["prospective_ok"] = bool(plan.get("ok"))
    else:
        signal["prospective_rr"] = -99.0
        signal["prospective_ok"] = False
    # Small ranking bonus only; card recognition never makes an invalid order valid.
    signal["card_rank_quality"] = safe_float(signal.get("setup_quality")) + min(LDT131_CARD_BONUS_MAX, safe_float(signal.get("card_score")))
    return signal


def _ldt13_scan_candidates(symbol):
    xs = _ldt131_base_scan_candidates(symbol)
    return [_ldt131_annotate(s) for s in xs]


def _ldt13_trackable(signal):
    """v1.3.1: track a strong thesis when RR@planned-zone is valid, even if RR NOW is poor."""
    if not signal or not signal.get("side"):
        return False
    sk = str(signal.get("skip") or "")
    if sk.startswith("stale"):
        return False
    if safe_float(signal.get("setup_quality")) < LDT131_MIN_THESIS_STR:
        return False
    if int(signal.get("card_score", 0)) < LDT131_MIN_CARD_EVIDENCE:
        return False
    pra = safe_float(signal.get("prospective_risk_atr"), 999.0)
    if pra < LDT_MIN_STOP_ATR or pra > LDT_MAX_STOP_ATR:
        return False
    if safe_float(signal.get("prospective_rr"), -99.0) < LDT131_MIN_PROSPECTIVE_RR:
        return False
    return True


def _ldt13_candidate_rank(s):
    return (
        safe_float(s.get("card_score")),
        safe_float(s.get("prospective_rr")),
        safe_float(s.get("card_rank_quality")),
        safe_float(s.get("entry_quality")),
    )


def _ldt13_new_state(signal):
    now = time.time()
    sig = _ldt13_jsonable(signal)
    # Freeze the prospective first-liquidity objective born from the planned zone.
    ptarget = safe_float(signal.get("prospective_target") or signal.get("target_price"))
    return {
        "symbol": signal.get("symbol"), "side": signal.get("side"),
        "strategy": signal.get("strategy"), "route": signal.get("route"),
        "created": now, "updated": now, "expires": now + 60 * _ldt13_ttl_min(signal),
        "state": "WAIT_RETRACE", "last_state": "", "signal": sig,
        "initial_target": ptarget,
        "initial_far_target": safe_float(signal.get("far_target") or signal.get("target_price")),
        "planned_entry": safe_float(signal.get("prospective_entry")),
        "prospective_rr": safe_float(signal.get("prospective_rr")),
        "card_score": int(signal.get("card_score", 0)),
        "card_label": signal.get("card_label", "BASE-STRUCTURE"),
        "best_rr": safe_float(signal.get("net_rr")), "best_eq": safe_float(signal.get("entry_quality")),
        "transitions": 0,
    }


def _ldt13_refresh_state(st):
    """v1.3.1 state refresh: thesis persists through bad RR NOW; execution remains strict."""
    signal = dict(st.get("signal") or {})
    symbol = st.get("symbol"); side = st.get("side")
    if not symbol or side not in ("long", "short"):
        _ldt13_transition(st, "INVALIDATED", "missing thesis identity"); return None
    d = 1 if side == "long" else -1
    now = time.time()
    if now > safe_float(st.get("expires")):
        _ldt13_transition(st, "EXPIRED", f"thesis age > {_ldt13_ttl_min(signal)} min"); return None
    try:
        live = safe_float(exchange.fetch_ticker(symbol).get("last"), safe_float(signal.get("price")))
    except Exception:
        live = safe_float(signal.get("price"))
    atr = safe_float(signal.get("atr")); stop = safe_float(signal.get("structural_stop")); zone = signal.get("zone") or {}
    if live <= 0 or atr <= 0 or stop <= 0 or not zone:
        _ldt13_transition(st, "INVALIDATED", "missing live/ATR/stop/zone"); return None

    invalid_buf = LDT12_LIVE_INVALIDATION_ATR * atr
    if (d == 1 and live < stop - invalid_buf) or (d == -1 and live > stop + invalid_buf):
        _ldt13_transition(st, "INVALIDATED", f"price {live:.8g} crossed thesis invalidation {stop:.8g}"); return None

    lo = safe_float(zone.get("lo")); hi = safe_float(zone.get("hi"))
    eq = _ldt_zone_entry_quality(live, zone, atr, d)
    in_zone = (lo - LDT_ZONE_TOL_ATR * atr <= live <= hi + LDT_ZONE_TOL_ATR * atr)
    planned = safe_float(st.get("planned_entry") or signal.get("prospective_entry") or (lo+hi)/2.0)
    prospective_rr = safe_float(st.get("prospective_rr") or signal.get("prospective_rr"))

    # Recompute live economics only for execution readiness. Poor RR NOW is a
    # WAIT_RETRACE condition, not a reason to forget the thesis.
    risk = d * (live - stop)
    risk_atr = risk / atr if risk > 0 else -1.0
    live_target = 0.0; target_kind = "waiting-retrace"; gross_rr = -99.0; net_rr = -99.0
    if risk > 0:
        hard = safe_float(st.get("initial_far_target") or signal.get("far_target") or signal.get("target_price"))
        try:
            live_target, target_kind, gross_rr = _ldt11_first_liquidity_target(symbol, side, live, stop, hard, _ldt131_ltf(signal))
            cost_r = live * (2 * TAKER_FEE_RATE + SLIPPAGE_RATE) / max(risk, 1e-12)
            net_rr = gross_rr - cost_r
        except Exception:
            pass

    struct = _ldt12_structure_state(symbol, side, signal.get("trigger_tf") or "1m")
    struct_ok = bool(struct.get("ok"))
    signal.update({
        "price": live, "entry_quality": eq,
        "quality": 0.60 * safe_float(signal.get("setup_quality")) + 0.40 * eq,
        "risk_distance": max(risk, 0.0), "target_price": live_target or safe_float(st.get("initial_target")),
        "target_kind": target_kind, "final_r": gross_rr, "net_rr": net_rr,
        "trigger_note": struct.get("note", ""), "confirmations": int(struct.get("strength", 0)) + 1,
        "ldt13_state": st.get("state"), "ldt13_age_min": (now-safe_float(st.get("created")))/60.0,
        "prospective_entry": planned, "prospective_rr": prospective_rr,
        "card_score": int(st.get("card_score", signal.get("card_score", 0))),
        "card_label": st.get("card_label", signal.get("card_label", "BASE-STRUCTURE")),
    })
    st["signal"] = _ldt13_jsonable(signal)
    st["best_rr"] = max(safe_float(st.get("best_rr")), net_rr)
    st["best_eq"] = max(safe_float(st.get("best_eq")), eq)

    # If current price is not executable, keep stalking the planned zone.
    if risk <= 0 or risk_atr < LDT_MIN_STOP_ATR or risk_atr > LDT_MAX_STOP_ATR:
        _ldt13_transition(st, "WAIT_RETRACE", f"card {signal.get('card_label')} · wait zone; live stop {risk_atr:.2f}ATR")
        signal["skip"] = f"TRACK-wait-retrace · RR@zone {prospective_rr:.2f}"
        signal["entry_route"] = None; return signal
    if not in_zone or eq < LDT_MIN_ENTRY_QUALITY:
        _ldt13_transition(st, "WAIT_RETRACE", f"card {signal.get('card_label')} · EQ {eq:.0f}; planned {lo:.8g}-{hi:.8g}")
        signal["skip"] = f"TRACK-wait-retrace EQ{eq:.0f} · RR@zone {prospective_rr:.2f}"
        signal["entry_route"] = None; return signal
    if net_rr < LDT_MIN_NET_RR:
        _ldt13_transition(st, "IN_ZONE", f"zone reached but live first-liq RR {net_rr:.2f}R")
        signal["skip"] = f"TRACK-in-zone RRnow {net_rr:.2f}<{LDT_MIN_NET_RR:.2f}"
        signal["entry_route"] = None; return signal
    if not struct_ok:
        _ldt13_transition(st, "IN_ZONE", f"{signal.get('card_label')} · zone reached · {struct.get('note','waiting structure')}")
        signal["skip"] = f"TRACK-in-zone · {struct.get('note','STRUCT WAIT')}"
        signal["entry_route"] = None; return signal

    age = int(struct.get("age", 0))
    _ldt13_transition(st, "READY" if age == 0 else "STRUCT_HOLD", struct.get("note", "structure valid"))
    signal["skip"] = None
    base_route = ("CRT-" + str(signal.get("route", "CORE")) + "-SV") if signal.get("strategy") == "CRT-MSS" else ("CONT-" + str(signal.get("route", "CORE")) + "-SV")
    signal["entry_route"] = base_route + "-CARD-STATE"
    signal["signal_bar_ts"] = int(time.time() // 60)
    return signal


def open_trade(signal):
    """v1.3.1 final state refresh, then the hardened v1.2.1 exchange shell."""
    symbol = signal.get("symbol")
    st = _ldt13_states.get(symbol)
    if st:
        fresh = _ldt13_refresh_state(st); _ldt13_save()
        if not fresh or fresh.get("skip") or not fresh.get("entry_route"):
            tg(f"⏭️ LDT v1.3.1 {symbol} {str(signal.get('side')).upper()} STATE CHANGED\n"
               f"{(fresh or {}).get('skip') or st.get('state_note','not ready')}\nOrder cancelled before submission.")
            return False
        signal = fresh
    try:
        signal["price"] = safe_float(exchange.fetch_ticker(symbol).get("last"), signal.get("price"))
    except Exception:
        pass
    opened = _ldt13_base_open_trade(signal)
    if opened and symbol in _ldt13_states:
        _ldt13_states.pop(symbol, None); _ldt13_save()
    return opened


def maybe_score_report(rows):
    global _ldt13_last_report
    now = time.time()
    if now - _ldt13_last_report < LDT_REPORT_MIN * 60 or not rows:
        return
    _ldt13_last_report = now; _ldt13_load()
    ready = [r for r in rows if r.get("side") and not r.get("skip") and r.get("entry_route")]
    state_counts = {}
    for st in _ldt13_states.values():
        k = str(st.get("state") or "WAIT_RETRACE"); state_counts[k] = state_counts.get(k, 0) + 1
    ranked = sorted(rows, key=lambda r: (
        1 if not r.get("skip") else 0,
        1 if r.get("symbol") in _ldt13_states else 0,
        safe_float(r.get("card_score")), safe_float(r.get("prospective_rr")),
        safe_float(r.get("entry_quality")), safe_float(r.get("setup_quality"))
    ), reverse=True)[:10]
    state_text = " · ".join(f"{k} {v}" for k,v in sorted(state_counts.items(), key=lambda x:x[1], reverse=True)[:5]) or "none"
    lines = [
        "🧠 LIQUIDITY DAYTRADER v1.3.1 · TRADING-CARD PROSPECTIVE",
        f"Ready {len(ready)} · Tracked {len(_ldt13_states)} · scanned {len(rows)}",
        f"Theses: {state_text}",
    ]
    for r in ranked:
        st = _ldt13_states.get(r.get("symbol"), {})
        age = (now-safe_float(st.get("created"),now))/60.0 if st else 0.0
        if not r.get("skip"):
            status = "🟢 EXECUTE"
        elif st:
            status = f"🟠 {st.get('state','WAIT_RETRACE')} {age:.0f}m"
        else:
            status = "🟡 " + str(r.get("skip"))
        lines.append(
            f"{r.get('symbol')} {str(r.get('side')).upper()} {r.get('strategy')} "
            f"CARD[{r.get('card_label','-')}] C{int(r.get('card_score',0))} "
            f"STR{safe_float(r.get('setup_quality')):.0f}/95 EQ{safe_float(r.get('entry_quality')):.0f} "
            f"RRnow{safe_float(r.get('net_rr')):.2f} RR@zone{safe_float(r.get('prospective_rr')):.2f} {status}"
        )
    tg("\n".join(lines))


def startup_diagnostics():
    _ldt13_load(); acc = futures_account(); reset_daily_if_needed(acc["total_equity"])
    tg(
        "🧠 LIQUIDITY DAYTRADER STARTED v1.3.1 · TRADING-CARD PROSPECTIVE HUNTER\n"
        "The supplied Institutional Price Action card now actively classifies/ranks theses: "
        "QM/2B/Fakeout, QM Re-entry/MPL, Flag/Continuation QM, Compression/Can-Can, V-tail, 3-Drive and Double SSR.\n"
        "Card patterns are context, not magic signals: execution still requires live zone + valid BOS/MSS + first-liquidity RR.\n"
        "Key fix: a thesis may be TRACKED when RR NOW is poor, provided RR@planned retrace zone is valid.\n"
        f"Min RR@zone to track {LDT131_MIN_PROSPECTIVE_RR:.2f}R · min LIVE RR to execute {LDT_MIN_NET_RR:.2f}R.\n"
        f"Structure window {LDT12_STRUCTURE_VALID_BARS} closed 1m candles · thesis TTL CORE {LDT13_CORE_TTL_MIN}m / CONT {LDT13_CONT_TTL_MIN}m / FAST {LDT13_FAST_TTL_MIN}m.\n"
        f"Risk CORE {LDT11_CORE_RISK*100:.2f}% · FAST {LDT11_FAST_RISK*100:.2f}% · CONT {LDT11_CONT_RISK*100:.2f}% · ELITE cap {LDT11_ELITE_RISK*100:.2f}%.\n"
        f"Max positions {MAX_ACTIVE_POSITIONS} · max same side {LDT_MAX_SAME_SIDE}.\n"
        f"Restored v1.3.1 theses: {len(_ldt13_states)} · Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}"
    )


# ==================== LDT v1.4 MULTI-ENGINE TRADING CARD ====================
# Three independent engines selected by price-action family:
#   REVERSAL    : CRT / QM / 2B / stop-hunt -> MSS -> IFVG/Breaker retest
#   CONTINUATION: range/flag break -> retest/reclaim -> continuation
#   MOMENTUM    : compression / Can-Can -> displacement breakout -> first hold/retest
# The execution/protection shell remains hardened; route-specific economics replace
# the old one-size-fits-all entry funnel.

LDT_VERSION = "1.4.0"
LDT13_STATE_FILE = os.getenv("LDT14_STATE_FILE", "ldt14_state.json")

LDT14_REV_LIVE_RR = float(os.getenv("LDT14_REV_LIVE_RR", "1.65"))
LDT14_CONT_LIVE_RR = float(os.getenv("LDT14_CONT_LIVE_RR", "1.45"))
LDT14_MOM_LIVE_RR = float(os.getenv("LDT14_MOM_LIVE_RR", "1.35"))
LDT14_REV_TRACK_RR = float(os.getenv("LDT14_REV_TRACK_RR", "1.50"))
LDT14_CONT_TRACK_RR = float(os.getenv("LDT14_CONT_TRACK_RR", "1.30"))
LDT14_MOM_TRACK_RR = float(os.getenv("LDT14_MOM_TRACK_RR", "1.20"))
LDT14_REV_TTL_MIN = int(os.getenv("LDT14_REV_TTL_MIN", "90"))
LDT14_CONT_TTL_MIN = int(os.getenv("LDT14_CONT_TTL_MIN", "65"))
LDT14_MOM_TTL_MIN = int(os.getenv("LDT14_MOM_TTL_MIN", "40"))
LDT14_MIN_CARD_REV = int(os.getenv("LDT14_MIN_CARD_REV", "2"))
LDT14_MIN_CARD_CONT = int(os.getenv("LDT14_MIN_CARD_CONT", "2"))
LDT14_MIN_CARD_MOM = int(os.getenv("LDT14_MIN_CARD_MOM", "2"))
LDT14_MIN_STR_REV = float(os.getenv("LDT14_MIN_STR_REV", "72"))
LDT14_MIN_STR_CONT = float(os.getenv("LDT14_MIN_STR_CONT", "72"))
LDT14_MIN_STR_MOM = float(os.getenv("LDT14_MIN_STR_MOM", "74"))
LDT14_CONT_RECLAIM_BODY_ATR = float(os.getenv("LDT14_CONT_RECLAIM_BODY_ATR", "0.18"))
LDT14_MOM_BODY_ATR = float(os.getenv("LDT14_MOM_BODY_ATR", "0.34"))
LDT14_MOM_BREAK_ATR = float(os.getenv("LDT14_MOM_BREAK_ATR", "0.06"))
LDT14_MOM_ZONE_ATR = float(os.getenv("LDT14_MOM_ZONE_ATR", "0.14"))
LDT14_MOM_VOL_MULT = float(os.getenv("LDT14_MOM_VOL_MULT", "0.90"))
LDT14_REV_RISK = float(os.getenv("LDT14_REV_RISK", "0.0050"))
LDT14_CONT_RISK = float(os.getenv("LDT14_CONT_RISK", "0.0045"))
LDT14_MOM_RISK = float(os.getenv("LDT14_MOM_RISK", "0.0035"))
LDT14_ELITE_RISK = float(os.getenv("LDT14_ELITE_RISK", "0.0060"))

# Fresh namespace: do not restore v1.3/v1.3.1 theses into the new route model.
_ldt13_states = {}
_ldt13_loaded = False
_ldt13_last_report = 0.0
_ldt14_exchange_open_base = _ldt13_base_open_trade


def _ldt14_engine(signal):
    e = str((signal or {}).get("engine") or "").upper()
    if e in ("REVERSAL", "CONTINUATION", "MOMENTUM"):
        return e
    s = str((signal or {}).get("strategy") or "")
    if s == "CRT-MSS": return "REVERSAL"
    if s == "MOMENTUM-RELEASE": return "MOMENTUM"
    return "CONTINUATION"


def _ldt14_live_rr_min(signal):
    e = _ldt14_engine(signal)
    return LDT14_REV_LIVE_RR if e == "REVERSAL" else LDT14_MOM_LIVE_RR if e == "MOMENTUM" else LDT14_CONT_LIVE_RR


def _ldt14_track_rr_min(signal):
    e = _ldt14_engine(signal)
    return LDT14_REV_TRACK_RR if e == "REVERSAL" else LDT14_MOM_TRACK_RR if e == "MOMENTUM" else LDT14_CONT_TRACK_RR


def _ldt14_ttl(signal):
    e = _ldt14_engine(signal)
    return LDT14_REV_TTL_MIN if e == "REVERSAL" else LDT14_MOM_TTL_MIN if e == "MOMENTUM" else LDT14_CONT_TTL_MIN


def _ldt14_card_min(signal):
    e = _ldt14_engine(signal)
    return LDT14_MIN_CARD_REV if e == "REVERSAL" else LDT14_MIN_CARD_MOM if e == "MOMENTUM" else LDT14_MIN_CARD_CONT


def _ldt14_str_min(signal):
    e = _ldt14_engine(signal)
    return LDT14_MIN_STR_REV if e == "REVERSAL" else LDT14_MIN_STR_MOM if e == "MOMENTUM" else LDT14_MIN_STR_CONT


def _ldt14_target(symbol, side, live, stop, hard_target, tf, engine):
    """Route-specific target model. Reversal respects first obstacle; trend routes may target the
    first meaningful external liquidity after tiny internal pivots already broken by the move."""
    d = 1 if side == "long" else -1
    risk = d * (live - stop)
    if live <= 0 or risk <= 0:
        return hard_target, "invalid-risk", 0.0
    if engine == "REVERSAL":
        return _ldt11_first_liquidity_target(symbol, side, live, stop, hard_target, tf)
    min_pivot_r = 0.85 if engine == "CONTINUATION" else 1.00
    try:
        df = _ldt_closed(_ldt_df(symbol, tf, 220)); highs, lows = _ldt_pivots(df, 2, 2)
        levels = [v for _, v in (highs if d == 1 else lows)]
        if d == 1:
            levels = sorted(v for v in levels if v > live)
        else:
            levels = sorted((v for v in levels if v < live), reverse=True)
        for v in levels:
            rr = d * (v - live) / risk
            if rr >= min_pivot_r:
                if hard_target > 0 and ((d == 1 and v > hard_target) or (d == -1 and v < hard_target)):
                    break
                return v, "external-swing-liquidity", rr
    except Exception:
        pass
    rr = d * (hard_target - live) / risk if hard_target > 0 else 0.0
    return hard_target, "measured/external-target", rr


def _ldt14_momentum_candidate(symbol):
    """Trading-card Compression/Can-Can engine on 15m with a 5m breakout-hold zone."""
    try:
        df15 = _ldt_closed(_ldt_df(symbol, "15m", 140))
        df5 = _ldt_closed(_ldt_df(symbol, "5m", 180))
        if len(df15) < 35 or len(df5) < 45: return None
        best = None
        for i in range(max(7, len(df15)-4), len(df15)):
            b = df15.iloc[i]; atr15 = safe_float(b["atr"])
            if atr15 <= 0: continue
            pre = df15.iloc[i-6:i].copy()
            if len(pre) < 6: continue
            ranges = (pre["high"] - pre["low"]).astype(float).values
            shrinking = sum(ranges[j] <= ranges[j-1]*1.10 for j in range(1, len(ranges))) >= 3
            shelf_hi = safe_float(pre["high"].max()); shelf_lo = safe_float(pre["low"].min())
            width = shelf_hi - shelf_lo
            if not shrinking or width <= 0 or width > 3.0*atr15: continue
            o=safe_float(b["open"]); c=safe_float(b["close"]); h=safe_float(b["high"]); l=safe_float(b["low"])
            rng=max(h-l,1e-12); body=abs(c-o)/atr15
            v=safe_float(b["volume"]); vma=safe_float(b["volma"])
            for d in (1,-1):
                level = shelf_hi if d==1 else shelf_lo
                broke = c > level + LDT14_MOM_BREAK_ATR*atr15 if d==1 else c < level - LDT14_MOM_BREAK_ATR*atr15
                directional = c > o if d==1 else c < o
                close_loc = (c-l)/rng if d==1 else (h-c)/rng
                vol_ok = vma <= 0 or v >= vma*LDT14_MOM_VOL_MULT
                if not (broke and directional and body >= LDT14_MOM_BODY_ATR and close_loc >= 0.68 and vol_ok): continue
                after = df5[df5["ts"] >= int(b["ts"])].copy().reset_index(drop=True)
                atr5 = safe_float(df5.iloc[-1]["atr"])
                if atr5 <= 0: continue
                zone = {"lo": level-LDT14_MOM_ZONE_ATR*atr5, "hi": level+LDT14_MOM_ZONE_ATR*atr5,
                        "kind":"COMPRESSION-BREAK-RETEST", "confluence":2}
                recent = after.tail(12) if len(after) else df5.tail(12)
                if d==1:
                    local = safe_float(recent["low"].min()) if len(recent) else zone["lo"]
                    stop = min(zone["lo"], local) - 0.08*atr5
                else:
                    local = safe_float(recent["high"].max()) if len(recent) else zone["hi"]
                    stop = max(zone["hi"], local) + 0.08*atr5
                live = safe_float(exchange.fetch_ticker(symbol).get("last")); side="long" if d==1 else "short"
                risk = d*(live-stop)
                if risk <= 0: continue
                measured = level + d*max(width, 1.6*atr15)
                btc_score,btc_note=_ldt_btc_context(side)
                sq = 70 + min(10, body*10) + (6 if close_loc>=0.80 else 3) + (5 if vol_ok else 0) + (5 if shrinking else 0) + btc_score*LDT_BTC_CONTEXT_WEIGHT/2
                sq=max(0,min(100,sq)); eq=_ldt_zone_entry_quality(live,zone,atr5,d)
                gross=d*(measured-live)/risk; cost=live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12); net=gross-cost
                cand={"symbol":symbol,"side":side,"strategy":"MOMENTUM-RELEASE","engine":"MOMENTUM",
                      "entry_route":None,"skip":"MOM-wait-first-hold","price":live,"score":sq,
                      "quality":0.60*sq+0.40*eq,"setup_quality":sq,"entry_quality":eq,"confirmations":3,
                      "h4_score":0,"signal_bar_ts":int(b["ts"]),"atr":atr5,"structural_stop":stop,
                      "target_price":measured,"far_target":measured,"risk_distance":risk,"final_r":gross,"net_rr":net,
                      "zone":zone,"route":"MOM","btc_note":btc_note,
                      "trigger_note":f"15m compression release body={body:.2f}ATR close={close_loc:.0%} vol={int(vol_ok)}",
                      "pattern_family":"COMPRESSION/CAN-CAN -> DISPLACEMENT -> FIRST HOLD",
                      "compression_width":width,"breakout_level":level,"breakout_ts":int(b["ts"])}
                rank=(sq,body,close_loc,-(len(df15)-1-i))
                if best is None or rank>best[0]: best=(rank,cand)
        return best[1] if best else None
    except Exception:
        return None


def _ldt14_card_profile(signal):
    base = _ldt131_card_profile(signal)
    fam=list(base.get("families",[])); score=int(base.get("score",0)); e=_ldt14_engine(signal)
    if e=="MOMENTUM":
        if "COMPRESSION/CAN-CAN" not in fam: fam.insert(0,"COMPRESSION/CAN-CAN")
        if "FLAG-RETEST" not in fam: fam.append("FLAG-RETEST")
        score=max(score,4)
    elif e=="CONTINUATION":
        if "CONT-QM/FLAG" not in fam: fam.insert(0,"CONT-QM/FLAG")
        score=max(score,2)
    elif e=="REVERSAL":
        score=max(score,2)
    fam=list(dict.fromkeys(fam)); score=min(8,score)
    return {"score":score,"families":fam,"label":"+".join(fam[:3]) if fam else "BASE-STRUCTURE"}


def _ldt14_prospective_plan(signal):
    side=str(signal.get("side") or ""); zone=signal.get("zone") or {}
    if side not in ("long","short") or not zone: return None
    d=1 if side=="long" else -1; atr=safe_float(signal.get("atr")); stop=safe_float(signal.get("structural_stop"))
    lo=safe_float(zone.get("lo")); hi=safe_float(zone.get("hi"))
    if atr<=0 or stop<=0 or lo<=0 or hi<=0 or hi<lo: return None
    entry=(lo+hi)/2.0; risk=d*(entry-stop)
    if risk<=0: return None
    risk_atr=risk/atr
    if risk_atr<LDT_MIN_STOP_ATR or risk_atr>LDT_MAX_STOP_ATR:
        return {"ok":False,"entry":entry,"risk_atr":risk_atr,"net_rr":-99.0,"target":0.0,"target_kind":"invalid-stop-geometry"}
    engine=_ldt14_engine(signal); hard=safe_float(signal.get("far_target") or signal.get("target_price")); tf="1m" if str(signal.get("route"))=="FAST" else "5m"
    target,kind,gross=_ldt14_target(signal.get("symbol"),side,entry,stop,hard,tf,engine)
    cost=entry*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12); net=gross-cost
    return {"ok":bool(net>=_ldt14_track_rr_min(signal)),"entry":entry,"risk_atr":risk_atr,"target":target,
            "target_kind":kind,"gross_rr":gross,"net_rr":net,"zone_lo":lo,"zone_hi":hi}


def _ldt14_annotate(signal):
    if not signal: return signal
    signal["engine"]=_ldt14_engine(signal)
    card=_ldt14_card_profile(signal); plan=_ldt14_prospective_plan(signal)
    signal["card_score"]=int(card["score"]); signal["card_families"]=card["families"]; signal["card_label"]=card["label"]
    if plan:
        signal["prospective_entry"]=safe_float(plan.get("entry")); signal["prospective_rr"]=safe_float(plan.get("net_rr")); signal["prospective_target"]=safe_float(plan.get("target")); signal["prospective_target_kind"]=plan.get("target_kind"); signal["prospective_risk_atr"]=safe_float(plan.get("risk_atr")); signal["prospective_ok"]=bool(plan.get("ok"))
    else:
        signal["prospective_rr"]=-99.0; signal["prospective_ok"]=False; signal["prospective_risk_atr"]=999.0
    signal["card_rank_quality"]=safe_float(signal.get("setup_quality"))+min(8,safe_float(signal.get("card_score")))
    return signal


def _ldt13_scan_candidates(symbol):
    xs=[]
    try:
        xs.extend(_ldt131_base_scan_candidates(symbol))
    except Exception:
        pass
    try:
        m=_ldt14_momentum_candidate(symbol)
        if m: xs.append(m)
    except Exception:
        pass
    return [_ldt14_annotate(s) for s in xs if s]


def _ldt13_trackable(signal):
    if not signal or not signal.get("side"): return False
    if str(signal.get("skip") or "").startswith("stale"): return False
    if safe_float(signal.get("setup_quality")) < _ldt14_str_min(signal): return False
    if int(signal.get("card_score",0)) < _ldt14_card_min(signal): return False
    pra=safe_float(signal.get("prospective_risk_atr"),999.0)
    if pra<LDT_MIN_STOP_ATR or pra>LDT_MAX_STOP_ATR: return False
    if safe_float(signal.get("prospective_rr"),-99.0) < _ldt14_track_rr_min(signal): return False
    return True


def _ldt13_candidate_rank(s):
    e=_ldt14_engine(s); engine_bias={"REVERSAL":3,"CONTINUATION":2,"MOMENTUM":1}.get(e,0)
    return (int(s.get("card_score",0)), safe_float(s.get("prospective_rr")), safe_float(s.get("card_rank_quality")), engine_bias, safe_float(s.get("entry_quality")))


def _ldt13_new_state(signal):
    now=time.time(); sig=_ldt13_jsonable(signal); ptarget=safe_float(signal.get("prospective_target") or signal.get("target_price"))
    return {"symbol":signal.get("symbol"),"side":signal.get("side"),"strategy":signal.get("strategy"),"route":signal.get("route"),
            "engine":_ldt14_engine(signal),"created":now,"updated":now,"expires":now+60*_ldt14_ttl(signal),"state":"WAIT_RETRACE","last_state":"","signal":sig,
            "initial_target":ptarget,"initial_far_target":safe_float(signal.get("far_target") or signal.get("target_price")),
            "planned_entry":safe_float(signal.get("prospective_entry")),"prospective_rr":safe_float(signal.get("prospective_rr")),
            "card_score":int(signal.get("card_score",0)),"card_label":signal.get("card_label","BASE-STRUCTURE"),
            "best_rr":safe_float(signal.get("net_rr")),"best_eq":safe_float(signal.get("entry_quality")),"transitions":0}


def _ldt13_transition(st,new_state,note=""):
    old=str(st.get("state") or ""); st["last_state"]=old; st["state"]=new_state; st["state_note"]=note; st["updated"]=time.time()
    if old!=new_state:
        st["transitions"]=int(st.get("transitions",0))+1
        if LDT13_NOTIFY_TRANSITIONS and new_state in ("IN_ZONE","TRIGGER_HOLD","READY","INVALIDATED","EXPIRED"):
            tg(f"🧭 LDT v1.4 {st.get('symbol')} {str(st.get('side','')).upper()} · {st.get('engine','-')} · {new_state}\n{note}")
    return old!=new_state


def _ldt14_reclaim_trigger(symbol, side, tf, zone, engine):
    """Route-specific last-mile trigger. Reversal stays strict; trend routes can use a clean
    reclaim/hold candle because the higher-timeframe break itself already supplied structure."""
    if engine=="REVERSAL":
        st=_ldt12_structure_state(symbol,side,tf); return bool(st.get("ok")),st
    strict=_ldt12_structure_state(symbol,side,tf)
    if strict.get("ok"): return True,strict
    try:
        df=_ldt_closed(_ldt_df(symbol,tf,120)); a=df.iloc[-1]; p=df.iloc[-2]; d=1 if side=="long" else -1; atr=safe_float(a["atr"])
        if atr<=0: return False,strict
        body,close_loc,directional=_ldt11_bar_strength(a,d,atr); c=safe_float(a["close"]); o=safe_float(a["open"])
        ema_ok=c>safe_float(a["ema21"]) if d==1 else c<safe_float(a["ema21"])
        prev_break=c>safe_float(p["high"]) if d==1 else c<safe_float(p["low"])
        lo=safe_float(zone.get("lo")); hi=safe_float(zone.get("hi")); held=c>=lo if d==1 else c<=hi
        wick=(min(o,c)-safe_float(a["low"])) if d==1 else (safe_float(a["high"])-max(o,c))
        reject=wick>=max(abs(c-o)*0.55,0.05*atr)
        min_body=LDT14_CONT_RECLAIM_BODY_ATR if engine=="CONTINUATION" else 0.16
        ok=bool(directional and body>=min_body and close_loc>=0.65 and ema_ok and held and (prev_break or reject))
        note=f"{tf} {engine} RECLAIM {'CONFIRMED' if ok else 'WAIT'} body={body:.2f}ATR close={close_loc:.0%} EMA={int(ema_ok)} prev={int(prev_break)} reject={int(reject)}"
        return ok,{"ok":ok,"note":note,"strength":int(directional)+int(ema_ok)+int(prev_break)+int(reject),"age":0,"invalidation":0.0,"atr":atr}
    except Exception:
        return False,strict


def _ldt14_refresh_state(st):
    signal=dict(st.get("signal") or {}); symbol=st.get("symbol"); side=st.get("side")
    if not symbol or side not in ("long","short"): _ldt13_transition(st,"INVALIDATED","missing thesis identity"); return None
    d=1 if side=="long" else -1; now=time.time(); engine=str(st.get("engine") or _ldt14_engine(signal)); signal["engine"]=engine
    if now>safe_float(st.get("expires")): _ldt13_transition(st,"EXPIRED",f"{engine} thesis age > {_ldt14_ttl(signal)} min"); return None
    try: live=safe_float(exchange.fetch_ticker(symbol).get("last"),safe_float(signal.get("price")))
    except Exception: live=safe_float(signal.get("price"))
    atr=safe_float(signal.get("atr")); stop=safe_float(signal.get("structural_stop")); zone=signal.get("zone") or {}
    if live<=0 or atr<=0 or stop<=0 or not zone: _ldt13_transition(st,"INVALIDATED","missing live/ATR/stop/zone"); return None
    invalid_buf=LDT12_LIVE_INVALIDATION_ATR*atr
    if (d==1 and live<stop-invalid_buf) or (d==-1 and live>stop+invalid_buf): _ldt13_transition(st,"INVALIDATED",f"price {live:.8g} crossed thesis invalidation {stop:.8g}"); return None
    lo=safe_float(zone.get("lo")); hi=safe_float(zone.get("hi")); eq=_ldt_zone_entry_quality(live,zone,atr,d)
    in_zone=lo-LDT_ZONE_TOL_ATR*atr<=live<=hi+LDT_ZONE_TOL_ATR*atr
    risk=d*(live-stop); risk_atr=risk/atr if risk>0 else -1.0
    hard=safe_float(st.get("initial_far_target") or signal.get("far_target") or signal.get("target_price")); tf="1m" if str(signal.get("route"))=="FAST" else "5m"
    target=0.0; kind="waiting"; gross=-99.0; net=-99.0
    if risk>0:
        target,kind,gross=_ldt14_target(symbol,side,live,stop,hard,tf,engine); net=gross-live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12)
    trig_ok,trig=_ldt14_reclaim_trigger(symbol,side,signal.get("trigger_tf") or "1m",zone,engine)
    signal.update({"price":live,"entry_quality":eq,"quality":0.60*safe_float(signal.get("setup_quality"))+0.40*eq,
                   "risk_distance":max(risk,0.0),"target_price":target or safe_float(st.get("initial_target")),"target_kind":kind,
                   "final_r":gross,"net_rr":net,"trigger_note":trig.get("note",""),"confirmations":int(trig.get("strength",0))+1,
                   "ldt13_state":st.get("state"),"ldt13_age_min":(now-safe_float(st.get("created")))/60.0,
                   "prospective_entry":safe_float(st.get("planned_entry")),"prospective_rr":safe_float(st.get("prospective_rr")),
                   "card_score":int(st.get("card_score",signal.get("card_score",0))),"card_label":st.get("card_label",signal.get("card_label","BASE-STRUCTURE")),"engine":engine})
    st["signal"]=_ldt13_jsonable(signal); st["best_rr"]=max(safe_float(st.get("best_rr")),net); st["best_eq"]=max(safe_float(st.get("best_eq")),eq)
    if risk<=0 or risk_atr<LDT_MIN_STOP_ATR or risk_atr>LDT_MAX_STOP_ATR:
        _ldt13_transition(st,"WAIT_RETRACE",f"{engine} · live stop {risk_atr:.2f}ATR"); signal["skip"]=f"TRACK-wait-retrace · RR@zone {safe_float(st.get('prospective_rr')):.2f}"; signal["entry_route"]=None; return signal
    if not in_zone or eq<LDT_MIN_ENTRY_QUALITY:
        _ldt13_transition(st,"WAIT_RETRACE",f"{engine} · EQ {eq:.0f} · zone {lo:.8g}-{hi:.8g}"); signal["skip"]=f"TRACK-wait-zone EQ{eq:.0f}"; signal["entry_route"]=None; return signal
    rrmin=_ldt14_live_rr_min(signal)
    if net<rrmin:
        _ldt13_transition(st,"IN_ZONE",f"{engine} zone reached · route RR {net:.2f}<{rrmin:.2f}"); signal["skip"]=f"TRACK-in-zone RR {net:.2f}<{rrmin:.2f}"; signal["entry_route"]=None; return signal
    if not trig_ok:
        _ldt13_transition(st,"IN_ZONE",f"{engine} · {trig.get('note','waiting trigger')}"); signal["skip"]=f"TRACK-in-zone · {trig.get('note','WAIT TRIGGER')}"; signal["entry_route"]=None; return signal
    age=int(trig.get("age",0)); _ldt13_transition(st,"READY" if age==0 else "TRIGGER_HOLD",trig.get("note","trigger valid"))
    signal["skip"]=None; signal["entry_route"]={"REVERSAL":"REV-CARD","CONTINUATION":"CONT-CARD","MOMENTUM":"MOM-CARD"}[engine]; signal["signal_bar_ts"]=int(time.time()//60)
    return signal


def evaluate_symbol(symbol, btc=None):
    _ldt13_load(); candidates=_ldt13_scan_candidates(symbol); fresh=_ldt13_best_trackable(candidates); st=_ldt13_states.get(symbol)
    if st:
        if fresh and str(fresh.get("side"))!=str(st.get("side")):
            oldq=safe_float((st.get("signal") or {}).get("card_rank_quality") or (st.get("signal") or {}).get("setup_quality"))
            newq=safe_float(fresh.get("card_rank_quality") or fresh.get("setup_quality"))
            if newq>=oldq+LDT13_REPLACE_EDGE:
                _ldt13_transition(st,"INVALIDATED",f"replaced by stronger {_ldt14_engine(fresh)} {fresh.get('side')} thesis"); st=_ldt13_new_state(fresh); _ldt13_states[symbol]=st
        elif fresh and _ldt13_same_thesis(st,fresh) and _ldt14_engine(fresh)==str(st.get("engine")):
            _ldt13_merge_fresh(st,fresh)
        out=_ldt14_refresh_state(st)
        if out is None or st.get("state") in ("INVALIDATED","EXPIRED"):
            _ldt13_states.pop(symbol,None); _ldt13_save()
            if fresh and _ldt13_trackable(fresh): st=_ldt13_new_state(fresh); _ldt13_states[symbol]=st; out=_ldt14_refresh_state(st)
            else: return _ldt13_best_diagnostic(candidates)
        _ldt13_save(); return out
    if fresh:
        st=_ldt13_new_state(fresh); _ldt13_states[symbol]=st; out=_ldt14_refresh_state(st); _ldt13_save(); return out
    return _ldt13_best_diagnostic(candidates)


def sizing_profile(signal):
    e=_ldt14_engine(signal); elite=safe_float(signal.get("setup_quality"))>=90 and safe_float(signal.get("entry_quality"))>=85 and safe_float(signal.get("net_rr"))>=2.0
    if elite: return {"name":"LDT14-ELITE","risk_pct":LDT14_ELITE_RISK,"target_net_tp":LDT_CORE_TARGET_NET}
    if e=="MOMENTUM": return {"name":"LDT14-MOM","risk_pct":LDT14_MOM_RISK,"target_net_tp":LDT_FAST_TARGET_NET}
    if e=="CONTINUATION": return {"name":"LDT14-CONT","risk_pct":LDT14_CONT_RISK,"target_net_tp":LDT_CORE_TARGET_NET}
    return {"name":"LDT14-REV","risk_pct":LDT14_REV_RISK,"target_net_tp":LDT_CORE_TARGET_NET}


def open_trade(signal):
    """v1.4 final route-specific revalidation + hardened BloFin protected market order."""
    global protection_lock, _v62_last_entry_time
    symbol=signal.get("symbol"); st=_ldt13_states.get(symbol)
    if st:
        fresh=_ldt14_refresh_state(st); _ldt13_save()
        if not fresh or fresh.get("skip") or not fresh.get("entry_route"):
            tg(f"⏭️ LDT v1.4 {symbol} {str(signal.get('side')).upper()} STATE CHANGED\n{(fresh or {}).get('skip') or st.get('state_note','not ready')}\nOrder cancelled before submission."); return False
        signal=fresh
    side=signal["side"]; d=1 if side=="long" else -1; engine=_ldt14_engine(signal)
    acc=futures_account(); equity,available=acc["total_equity"],acc["available"]; reset_daily_if_needed(equity)
    if equity<MIN_EQUITY_USDT or available<=0 or (not PAPER_TRADING and protection_lock): return False
    if time.time()-_v62_last_entry_time<4*60: return False
    live=safe_float(exchange.fetch_ticker(symbol).get("last"),signal.get("price")); atr=safe_float(signal.get("atr")); zone=signal.get("zone") or {}
    trig_ok,trig=_ldt14_reclaim_trigger(symbol,side,signal.get("trigger_tf") or "1m",zone,engine)
    if not trig_ok: tg(f"⏭️ LDT v1.4 {symbol} {side.upper()} {engine} TRIGGER CHANGED\n{trig.get('note','')}\nOrder cancelled before submission."); return False
    eq=_ldt_zone_entry_quality(live,zone,atr,d)
    if eq<LDT_MIN_ENTRY_QUALITY: tg(f"⏭️ LDT v1.4 {symbol} {side.upper()} ENTRY MOVED\nEQ {eq:.0f} < {LDT_MIN_ENTRY_QUALITY:.0f} · no chase"); return False
    tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001); sl=safe_float(signal.get("structural_stop"))
    if live<=0 or sl<=0: return False
    risk=d*(live-sl); risk_atr=risk/max(atr,1e-12)
    if risk<=0 or risk_atr<LDT_MIN_STOP_ATR or risk_atr>LDT_MAX_STOP_ATR: return False
    hard=safe_float(signal.get("far_target") or signal.get("target_price")); tf="1m" if str(signal.get("route"))=="FAST" else "5m"
    target,target_kind,gross=_ldt14_target(symbol,side,live,sl,hard,tf,engine)
    sl=round_trade_price(sl,tick,side,"sl"); target=round_trade_price(target,tick,side,"tp"); risk=abs(live-sl); final_r=d*(target-live)/max(risk,1e-12)
    net=final_r-live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12); rrmin=_ldt14_live_rr_min(signal)
    if net<rrmin: tg(f"⏭️ LDT v1.4 {symbol} {side.upper()} {engine} RR BLOCK\n{target_kind} · net RR {net:.2f} < {rrmin:.2f}"); return False
    profile=sizing_profile(signal); sizing=calculate_contracts_v5(symbol,live,sl,equity,available,profile["risk_pct"],profile["target_net_tp"],final_r)
    if not sizing or sizing["margin"]>available*AVAILABLE_MARGIN_BUFFER: return False
    before_ids={position_id(p) for p in get_positions() if position_id(p)}; oklev,levres=set_leverage(symbol,LEVERAGE)
    if not oklev: tg(f"⛔ LDT v1.4 LEVERAGE FAILED\n{symbol}\n{levres}"); return False
    tag="ldt14"; payload={"instId":blofin_inst_id(symbol),"marginMode":MARGIN_MODE,"positionSide":"net","side":"buy" if side=="long" else "sell","orderType":"market","size":str(sizing["contracts"]),"tpTriggerPrice":str(target),"tpOrderPrice":"-1","tpTriggerPriceType":"mark","slTriggerPrice":str(sl),"slOrderPrice":"-1","slTriggerPriceType":"mark","clientOrderId":f"{tag}{int(time.time()*1000)%1000000000}"}
    if BROKER_ID: payload["brokerId"]=BROKER_ID
    msg=(f"🎯 {'PAPER ' if PAPER_TRADING else ''}LIQUIDITY DAYTRADER v1.4 MULTI-ENGINE\n{symbol} {side.upper()} · {engine} · {signal.get('entry_route')}\nCARD[{signal.get('card_label','-')}] C{int(signal.get('card_score',0))}\nSTR {safe_float(signal.get('setup_quality')):.0f}/95 · EQ {eq:.0f} · route net RR {net:.2f}R\nTarget {target_kind} · {target:.8g}\n{trig.get('note','')}\nEntry {live:.8g} · SL {sl:.8g} · TP {target:.8g}\nRisk cap {profile['risk_pct']*100:.2f}% · expected net TP ~${sizing['expected_net_tp_usdt']:.2f}\nNotional ${sizing['notional']:.2f} · Margin ${sizing['margin']:.2f} · {LEVERAGE}x")
    if PAPER_TRADING: tg(msg); _v62_last_entry_time=time.time(); return True
    resp=native("private_post_trade_order",payload)
    if not native_ok(resp):
        err=response_error(resp)
        if "152012" in err: protection_lock=True
        tg(f"❌ LDT v1.4 ORDER FAILED\n{symbol} {side.upper()}\n{err}"); return False
    _v62_last_entry_time=time.time(); tg(msg); new_pos=find_new_position(symbol,before_ids,timeout=8)
    if new_pos is None: tg(f"⚠️ {symbol}: v1.4 order accepted, position not visible yet."); return True
    pid=position_id(new_pos); fill=position_entry(new_pos) or live
    if pid:
        sniper_managed_positions.add(pid); _ldt_meta[pid]={"risk":risk,"entry":fill,"sl":sl,"tp":target,"final_r":final_r,"side":side,"opened":time.time(),"strategy":signal.get("strategy"),"route":signal.get("entry_route"),"engine":engine,"mfe_price":fill,"mfe_r":0.0,"version":"1.4"}
    if has_live_protection(new_pos)[0]: tg(f"🛡️ LDT v1.4 PROTECTION CONFIRMED\n{symbol} {side.upper()}\nSL/TP visible."); opened=True
    else:
        last=safe_float(exchange.fetch_ticker(symbol).get("last")); opened=True
        if tpsl_prices_valid(side,sl,target,last):
            ok,res=place_tpsl_for_position(new_pos,sl,target,client_tag=tag)
            if ok: tg(f"🛡️ LDT v1.4 FALLBACK PROTECTION CONFIRMED\n{symbol}")
            else: protection_lock=True; tg(f"🚨 LDT v1.4 PROTECTION FAILURE\n{symbol}\n{res}\nNEW TRADES PAUSED.")
        else: protection_lock=True; tg(f"🚨 {symbol}: v1.4 position exists but protection could not be verified.\nNEW TRADES PAUSED.")
    if opened and symbol in _ldt13_states: _ldt13_states.pop(symbol,None); _ldt13_save()
    return True


def maybe_score_report(rows):
    global _ldt13_last_report
    now=time.time()
    if now-_ldt13_last_report<LDT_REPORT_MIN*60 or not rows: return
    _ldt13_last_report=now; _ldt13_load(); ready=[r for r in rows if r.get("side") and not r.get("skip") and r.get("entry_route")]
    counts={}; engines={}
    for st in _ldt13_states.values():
        k=str(st.get("state") or "WAIT_RETRACE"); counts[k]=counts.get(k,0)+1; e=str(st.get("engine") or "-"); engines[e]=engines.get(e,0)+1
    ranked=sorted(rows,key=lambda r:(1 if not r.get("skip") else 0,1 if r.get("symbol") in _ldt13_states else 0,safe_float(r.get("card_score")),safe_float(r.get("prospective_rr")),safe_float(r.get("entry_quality"))),reverse=True)[:10]
    state_text=" · ".join(f"{k} {v}" for k,v in sorted(counts.items(),key=lambda x:x[1],reverse=True)[:5]) or "none"; eng_text=" · ".join(f"{k} {v}" for k,v in engines.items()) or "none"
    lines=["🧠 LIQUIDITY DAYTRADER v1.4 · MULTI-ENGINE TRADING CARD",f"Ready {len(ready)} · Tracked {len(_ldt13_states)} · scanned {len(rows)}",f"Engines: {eng_text}",f"States: {state_text}"]
    for r in ranked:
        st=_ldt13_states.get(r.get("symbol"),{}); age=(now-safe_float(st.get("created"),now))/60 if st else 0; e=_ldt14_engine(r)
        if not r.get("skip"): status="🟢 EXECUTE"
        elif st: status=f"🟠 {st.get('state','WAIT_RETRACE')} {age:.0f}m"
        else: status="🟡 "+str(r.get("skip"))
        pr=safe_float(r.get("prospective_rr"),-99.0); prtxt="N/A" if pr<=-90 else f"{pr:.2f}"
        lines.append(f"{r.get('symbol')} {str(r.get('side')).upper()} {e} · CARD[{r.get('card_label','-')}] C{int(r.get('card_score',0))} STR{safe_float(r.get('setup_quality')):.0f} EQ{safe_float(r.get('entry_quality')):.0f} RRnow{safe_float(r.get('net_rr')):.2f} RR@zone{prtxt} {status}")
    tg("\n".join(lines))


def startup_diagnostics():
    _ldt13_load(); acc=futures_account(); reset_daily_if_needed(acc["total_equity"])
    tg("🧠 LIQUIDITY DAYTRADER STARTED v1.4 · MULTI-ENGINE TRADING CARD\n"
       "Trading-card route selector: REVERSAL (QM/2B/stop-hunt), CONTINUATION (QM/Flag/retest), MOMENTUM (Compression/Can-Can release).\n"
       "Each engine has its own trigger + target economics; no universal perfect-setup funnel.\n"
       f"LIVE RR mins: REV {LDT14_REV_LIVE_RR:.2f}R · CONT {LDT14_CONT_LIVE_RR:.2f}R · MOM {LDT14_MOM_LIVE_RR:.2f}R.\n"
       f"TRACK RR@zone mins: REV {LDT14_REV_TRACK_RR:.2f}R · CONT {LDT14_CONT_TRACK_RR:.2f}R · MOM {LDT14_MOM_TRACK_RR:.2f}R.\n"
       f"TTL: REV {LDT14_REV_TTL_MIN}m · CONT {LDT14_CONT_TTL_MIN}m · MOM {LDT14_MOM_TTL_MIN}m.\n"
       f"Risk: REV {LDT14_REV_RISK*100:.2f}% · CONT {LDT14_CONT_RISK*100:.2f}% · MOM {LDT14_MOM_RISK*100:.2f}% · ELITE cap {LDT14_ELITE_RISK*100:.2f}%.\n"
       f"Max positions {MAX_ACTIVE_POSITIONS} · max same side {LDT_MAX_SAME_SIDE}.\n"
       f"Fresh v1.4 theses: {len(_ldt13_states)} · Equity ${acc['total_equity']:.2f} · Available ${acc['available']:.2f}")


# ==================== LDT v1.5 ADAPTIVE TRADING CARD ====================
# Overnight v1.4 audit corrections:
# 1) freeze thesis geometry (zone/stop/target) instead of moving it every scan;
# 2) do not recycle the same expired/invalidated setup without a fresh signal bar;
# 3) route entries through a 15m regime classifier so the trading-card family selects
#    the right engine rather than merely adding another filter;
# 4) use conservative RR@zone (worst acceptable fill inside the zone);
# 5) reject fee-heavy micro-stops even when headline RR looks attractive;
# 6) fee-aware profit protection: after +1R-ish, lock a genuinely positive net result.

LDT_VERSION = "1.5.0"
LDT13_STATE_FILE = os.getenv("LDT15_STATE_FILE", "ldt15_state.json")
LDT15_DEAD_FILE = os.getenv("LDT15_DEAD_FILE", "ldt15_dead_theses.json")
LDT15_REARM_COOLDOWN_MIN = int(os.getenv("LDT15_REARM_COOLDOWN_MIN", "12"))
LDT15_MAX_FEE_R = float(os.getenv("LDT15_MAX_FEE_R", "0.32"))
LDT15_MAX_TRACKED = int(os.getenv("LDT15_MAX_TRACKED", "12"))
LDT15_FRESH_REPLACE_MIN = int(os.getenv("LDT15_FRESH_REPLACE_MIN", "8"))
LDT15_CONT_TREND_LIVE_RR = float(os.getenv("LDT15_CONT_TREND_LIVE_RR", "1.35"))
LDT15_MOM_TREND_LIVE_RR = float(os.getenv("LDT15_MOM_TREND_LIVE_RR", "1.30"))
LDT15_CONT_COUNTER_LIVE_RR = float(os.getenv("LDT15_CONT_COUNTER_LIVE_RR", "1.60"))
LDT15_MANAGER_STAGE1_R = float(os.getenv("LDT15_MANAGER_STAGE1_R", "1.15"))
LDT15_MANAGER_STAGE2_R = float(os.getenv("LDT15_MANAGER_STAGE2_R", "1.80"))
LDT15_MANAGER_LOCK1_R = float(os.getenv("LDT15_MANAGER_LOCK1_R", "0.12"))
LDT15_MANAGER_LOCK2_R = float(os.getenv("LDT15_MANAGER_LOCK2_R", "0.75"))

_ldt13_states = {}
_ldt13_loaded = False
_ldt13_last_report = 0.0
_ldt15_dead = {}
_ldt15_dead_loaded = False
_ldt15_base_open_trade = open_trade


def _ldt15_load_dead():
    global _ldt15_dead_loaded, _ldt15_dead
    if _ldt15_dead_loaded: return
    _ldt15_dead_loaded=True
    try:
        if os.path.exists(LDT15_DEAD_FILE):
            with open(LDT15_DEAD_FILE,'r',encoding='utf-8') as f:
                x=json.load(f)
            if isinstance(x,dict): _ldt15_dead=x
    except Exception:
        _ldt15_dead={}


def _ldt15_save_dead():
    try:
        tmp=LDT15_DEAD_FILE+'.tmp'
        with open(tmp,'w',encoding='utf-8') as f: json.dump(_ldt13_jsonable(_ldt15_dead),f,ensure_ascii=False,indent=2)
        os.replace(tmp,LDT15_DEAD_FILE)
    except Exception: pass


def _ldt15_mark_dead(st, reason=''):
    if not st: return
    _ldt15_load_dead(); sym=str(st.get('symbol') or '')
    if not sym: return
    sig=st.get('signal') or {}
    _ldt15_dead[sym]={
        'ts':time.time(),'side':st.get('side'),'engine':st.get('engine'),
        'signal_bar_ts':int(st.get('origin_signal_ts') or sig.get('signal_bar_ts') or 0),
        'reason':reason or st.get('state_note','')
    }
    _ldt15_save_dead()


def _ldt15_is_recycled(signal):
    _ldt15_load_dead(); sym=str((signal or {}).get('symbol') or '')
    x=_ldt15_dead.get(sym)
    if not x: return False
    if time.time()-safe_float(x.get('ts')) > LDT15_REARM_COOLDOWN_MIN*60: return False
    if str(x.get('side'))!=str(signal.get('side')) or str(x.get('engine'))!=_ldt14_engine(signal): return False
    old_ts=int(x.get('signal_bar_ts') or 0); new_ts=int(signal.get('signal_bar_ts') or 0)
    return new_ts<=old_ts


def _ldt15_regime(symbol, side=None):
    """15m regime: trend / range / expansion. It routes card families, not a hard BTC-style veto."""
    try:
        df=_ldt_closed(_ldt_df(symbol,'15m',140)).copy()
        if len(df)<65: return {'label':'UNKNOWN','dir':0,'strength':0,'atr_ratio':1.0,'fit':0}
        df['ema55']=df['close'].ewm(span=55,adjust=False).mean()
        a=df.iloc[-1]; p3=df.iloc[-4]
        atr=safe_float(a['atr']); atr_mean=safe_float(df['atr'].tail(20).mean()) or atr
        close=safe_float(a['close']); e21=safe_float(a['ema21']); e55=safe_float(a['ema55'])
        slope=(e21-safe_float(p3['ema21']))/max(atr,1e-12)
        atr_ratio=atr/max(atr_mean,1e-12)
        body=abs(safe_float(a['close'])-safe_float(a['open']))/max(atr,1e-12)
        rng=max(safe_float(a['high'])-safe_float(a['low']),1e-12)
        close_up=(close-safe_float(a['low']))/rng; close_dn=(safe_float(a['high'])-close)/rng
        trend_dir=1 if close>e21>e55 and slope>0.10 else -1 if close<e21<e55 and slope<-0.10 else 0
        expansion_dir=1 if body>=0.55 and close_up>=0.72 and atr_ratio>=1.02 else -1 if body>=0.55 and close_dn>=0.72 and atr_ratio>=1.02 else 0
        if expansion_dir: label='EXPANSION_UP' if expansion_dir==1 else 'EXPANSION_DOWN'; rdir=expansion_dir; strength=3
        elif trend_dir: label='TREND_UP' if trend_dir==1 else 'TREND_DOWN'; rdir=trend_dir; strength=2 if abs(slope)<0.35 else 3
        elif atr_ratio<0.88: label='COMPRESSION'; rdir=0; strength=1
        else: label='RANGE'; rdir=0; strength=1
        fit=0
        if side in ('long','short'):
            d=1 if side=='long' else -1
            fit=2 if rdir==d else -2 if rdir==-d else 0
        return {'label':label,'dir':rdir,'strength':strength,'atr_ratio':atr_ratio,'slope':slope,'fit':fit}
    except Exception:
        return {'label':'UNKNOWN','dir':0,'strength':0,'atr_ratio':1.0,'fit':0}


def _ldt14_live_rr_min(signal):
    e=_ldt14_engine(signal); fit=int((signal or {}).get('regime_fit',0)); card=int((signal or {}).get('card_score',0))
    if e=='REVERSAL': return LDT14_REV_LIVE_RR
    if e=='CONTINUATION':
        if fit>=2 and card>=3: return LDT15_CONT_TREND_LIVE_RR
        if fit<=-2: return LDT15_CONT_COUNTER_LIVE_RR
        return LDT14_CONT_LIVE_RR
    if fit>=2 and card>=4: return LDT15_MOM_TREND_LIVE_RR
    return LDT14_MOM_LIVE_RR


def _ldt14_track_rr_min(signal):
    e=_ldt14_engine(signal); fit=int((signal or {}).get('regime_fit',0)); card=int((signal or {}).get('card_score',0))
    if e=='REVERSAL': return LDT14_REV_TRACK_RR
    if e=='CONTINUATION' and fit>=2 and card>=3: return min(LDT14_CONT_TRACK_RR,1.22)
    if e=='MOMENTUM' and fit>=2 and card>=4: return min(LDT14_MOM_TRACK_RR,1.15)
    return LDT14_MOM_TRACK_RR if e=='MOMENTUM' else LDT14_CONT_TRACK_RR


def _ldt14_prospective_plan(signal):
    side=str((signal or {}).get('side') or ''); zone=(signal or {}).get('zone') or {}
    if side not in ('long','short') or not zone: return None
    d=1 if side=='long' else -1; atr=safe_float(signal.get('atr')); stop=safe_float(signal.get('structural_stop'))
    lo=safe_float(zone.get('lo')); hi=safe_float(zone.get('hi'))
    if atr<=0 or stop<=0 or lo<=0 or hi<=0 or hi<lo: return None
    # Conservative fill: long at upper edge, short at lower edge. If economics only work at the
    # perfect midpoint, the thesis is not worth occupying a tracking slot.
    entry=hi if d==1 else lo
    risk=d*(entry-stop)
    if risk<=0: return None
    risk_atr=risk/atr
    fee_r=entry*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12)
    if risk_atr<LDT_MIN_STOP_ATR or risk_atr>LDT_MAX_STOP_ATR:
        return {'ok':False,'entry':entry,'risk_atr':risk_atr,'fee_r':fee_r,'net_rr':-99.0,'target':0.0,'target_kind':'invalid-stop-geometry'}
    engine=_ldt14_engine(signal); hard=safe_float(signal.get('far_target') or signal.get('target_price')); tf='1m' if str(signal.get('route'))=='FAST' else '5m'
    target,kind,gross=_ldt14_target(signal.get('symbol'),side,entry,stop,hard,tf,engine)
    net=gross-fee_r
    return {'ok':bool(net>=_ldt14_track_rr_min(signal) and fee_r<=LDT15_MAX_FEE_R),'entry':entry,'risk_atr':risk_atr,'fee_r':fee_r,'target':target,'target_kind':kind,'gross_rr':gross,'net_rr':net,'zone_lo':lo,'zone_hi':hi}


def _ldt14_annotate(signal):
    if not signal: return signal
    signal['engine']=_ldt14_engine(signal)
    card=_ldt14_card_profile(signal)
    signal['card_score']=int(card['score']); signal['card_families']=card['families']; signal['card_label']=card['label']
    reg=_ldt15_regime(signal.get('symbol'),signal.get('side'))
    signal['regime']=reg.get('label'); signal['regime_dir']=int(reg.get('dir',0)); signal['regime_fit']=int(reg.get('fit',0)); signal['regime_strength']=int(reg.get('strength',0)); signal['regime_atr_ratio']=safe_float(reg.get('atr_ratio'),1.0)
    plan=_ldt14_prospective_plan(signal)
    if plan:
        signal['prospective_entry']=safe_float(plan.get('entry')); signal['prospective_rr']=safe_float(plan.get('net_rr')); signal['prospective_target']=safe_float(plan.get('target')); signal['prospective_target_kind']=plan.get('target_kind'); signal['prospective_risk_atr']=safe_float(plan.get('risk_atr')); signal['prospective_fee_r']=safe_float(plan.get('fee_r')); signal['prospective_ok']=bool(plan.get('ok'))
    else:
        signal['prospective_rr']=-99.0; signal['prospective_ok']=False; signal['prospective_risk_atr']=999.0; signal['prospective_fee_r']=99.0
    # Reversal is allowed against trend only with stronger liquidity evidence. Continuation and
    # momentum get a bonus only when the market regime agrees with their direction.
    e=signal['engine']; fit=int(signal.get('regime_fit',0)); route_bonus=0
    if e=='REVERSAL': route_bonus=2 if fit<=0 else 0
    elif fit>=2: route_bonus=6
    elif fit<=-2: route_bonus=-8
    signal['route_fit_score']=route_bonus
    signal['card_rank_quality']=safe_float(signal.get('setup_quality'))+min(8,safe_float(signal.get('card_score')))+route_bonus
    return signal


def _ldt13_trackable(signal):
    if not signal or not signal.get('side') or _ldt15_is_recycled(signal): return False
    if str(signal.get('skip') or '').startswith('stale'): return False
    if safe_float(signal.get('setup_quality'))<_ldt14_str_min(signal): return False
    if int(signal.get('card_score',0))<_ldt14_card_min(signal): return False
    e=_ldt14_engine(signal); fit=int(signal.get('regime_fit',0)); card=int(signal.get('card_score',0))
    if e=='CONTINUATION' and fit<=-2 and card<4: return False
    if e=='MOMENTUM' and fit<0: return False
    if e=='REVERSAL' and fit>=2 and card<5: return False
    pra=safe_float(signal.get('prospective_risk_atr'),999.0); fee_r=safe_float(signal.get('prospective_fee_r'),99.0)
    if pra<LDT_MIN_STOP_ATR or pra>LDT_MAX_STOP_ATR or fee_r>LDT15_MAX_FEE_R: return False
    if safe_float(signal.get('prospective_rr'),-99.0)<_ldt14_track_rr_min(signal): return False
    return True


def _ldt15_utility(s):
    if not s: return -999.0
    rr=max(-2.0,min(5.0,safe_float(s.get('prospective_rr'),-2.0)))
    return (safe_float(s.get('setup_quality'))*0.35 + safe_float(s.get('entry_quality'))*0.10 + int(s.get('card_score',0))*3.0 + int(s.get('route_fit_score',0))*1.5 + rr*5.0 - safe_float(s.get('prospective_fee_r'),1.0)*12.0)


def _ldt13_candidate_rank(s):
    return (_ldt15_utility(s),safe_float(s.get('prospective_rr')),safe_float(s.get('setup_quality')),safe_float(s.get('entry_quality')))


def _ldt13_new_state(signal):
    now=time.time(); sig=_ldt13_jsonable(signal); zone=dict(signal.get('zone') or {}); ptarget=safe_float(signal.get('prospective_target') or signal.get('target_price'))
    return {'symbol':signal.get('symbol'),'side':signal.get('side'),'strategy':signal.get('strategy'),'route':signal.get('route'),'engine':_ldt14_engine(signal),
            'created':now,'updated':now,'expires':now+60*_ldt14_ttl(signal),'state':'WAIT_RETRACE','last_state':'','signal':sig,
            'origin_signal_ts':int(signal.get('signal_bar_ts') or 0),'frozen_zone':_ldt13_jsonable(zone),'frozen_stop':safe_float(signal.get('structural_stop')),
            'initial_target':ptarget,'initial_far_target':safe_float(signal.get('far_target') or signal.get('target_price')),
            'planned_entry':safe_float(signal.get('prospective_entry')),'prospective_rr':safe_float(signal.get('prospective_rr')),
            'prospective_fee_r':safe_float(signal.get('prospective_fee_r')),'card_score':int(signal.get('card_score',0)),'card_label':signal.get('card_label','BASE-STRUCTURE'),
            'regime':signal.get('regime','UNKNOWN'),'regime_fit':int(signal.get('regime_fit',0)),'utility':_ldt15_utility(signal),
            'best_rr':safe_float(signal.get('net_rr')),'best_eq':safe_float(signal.get('entry_quality')),'transitions':0}


def _ldt13_merge_fresh(st,fresh):
    if not fresh: return
    old=dict(st.get('signal') or {}); merged=dict(old); merged.update(_ldt13_jsonable(fresh))
    # Thesis geometry is immutable. A genuinely newer setup must replace the state, not drag the
    # old zone/stop along with price.
    merged['zone']=_ldt13_jsonable(st.get('frozen_zone') or old.get('zone') or {})
    merged['structural_stop']=safe_float(st.get('frozen_stop') or old.get('structural_stop'))
    merged['far_target']=safe_float(st.get('initial_far_target') or old.get('far_target') or old.get('target_price'))
    merged['target_price']=safe_float(st.get('initial_target') or old.get('target_price'))
    merged['signal_bar_ts']=int(st.get('origin_signal_ts') or old.get('signal_bar_ts') or 0)
    st['signal']=merged; st['updated']=time.time(); st['best_rr']=max(safe_float(st.get('best_rr')),safe_float(fresh.get('net_rr'))); st['best_eq']=max(safe_float(st.get('best_eq')),safe_float(fresh.get('entry_quality')))


def _ldt14_refresh_state(st):
    signal=dict(st.get('signal') or {}); symbol=st.get('symbol'); side=st.get('side')
    if not symbol or side not in ('long','short'): _ldt13_transition(st,'INVALIDATED','missing thesis identity'); return None
    d=1 if side=='long' else -1; now=time.time(); engine=str(st.get('engine') or _ldt14_engine(signal)); signal['engine']=engine
    if now>safe_float(st.get('expires')): _ldt13_transition(st,'EXPIRED',f'{engine} thesis age > {_ldt14_ttl(signal)} min'); return None
    try: live=safe_float(exchange.fetch_ticker(symbol).get('last'),safe_float(signal.get('price')))
    except Exception: live=safe_float(signal.get('price'))
    atr=safe_float(signal.get('atr')); stop=safe_float(st.get('frozen_stop') or signal.get('structural_stop')); zone=dict(st.get('frozen_zone') or signal.get('zone') or {})
    signal['structural_stop']=stop; signal['zone']=zone; signal['far_target']=safe_float(st.get('initial_far_target') or signal.get('far_target')); signal['target_price']=safe_float(st.get('initial_target') or signal.get('target_price'))
    if live<=0 or atr<=0 or stop<=0 or not zone: _ldt13_transition(st,'INVALIDATED','missing live/ATR/frozen geometry'); return None
    invalid_buf=LDT12_LIVE_INVALIDATION_ATR*atr
    if (d==1 and live<stop-invalid_buf) or (d==-1 and live>stop+invalid_buf): _ldt13_transition(st,'INVALIDATED',f'price {live:.8g} crossed frozen thesis invalidation {stop:.8g}'); return None
    reg=_ldt15_regime(symbol,side); signal['regime']=reg.get('label'); signal['regime_fit']=int(reg.get('fit',0)); signal['regime_strength']=int(reg.get('strength',0)); signal['regime_atr_ratio']=safe_float(reg.get('atr_ratio'),1.0)
    card=int(st.get('card_score',signal.get('card_score',0))); signal['card_score']=card; signal['card_label']=st.get('card_label',signal.get('card_label','BASE-STRUCTURE'))
    # If a trend-following thesis is now strongly counter-regime, wait rather than execute.
    regime_block=(engine=='MOMENTUM' and int(signal['regime_fit'])<0) or (engine=='CONTINUATION' and int(signal['regime_fit'])<=-2 and card<4) or (engine=='REVERSAL' and int(signal['regime_fit'])>=2 and card<5)
    lo=safe_float(zone.get('lo')); hi=safe_float(zone.get('hi')); eq=_ldt_zone_entry_quality(live,zone,atr,d); in_zone=lo-LDT_ZONE_TOL_ATR*atr<=live<=hi+LDT_ZONE_TOL_ATR*atr
    risk=d*(live-stop); risk_atr=risk/atr if risk>0 else -1.0; fee_r=live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12) if risk>0 else 99.0
    hard=safe_float(st.get('initial_far_target') or signal.get('far_target') or signal.get('target_price')); tf='1m' if str(signal.get('route'))=='FAST' else '5m'
    target=0.0; kind='waiting'; gross=-99.0; net=-99.0
    if risk>0:
        target,kind,gross=_ldt14_target(symbol,side,live,stop,hard,tf,engine); net=gross-fee_r
    trig_ok,trig=_ldt14_reclaim_trigger(symbol,side,signal.get('trigger_tf') or '1m',zone,engine)
    signal.update({'price':live,'entry_quality':eq,'quality':0.60*safe_float(signal.get('setup_quality'))+0.40*eq,'risk_distance':max(risk,0.0),'target_price':target or safe_float(st.get('initial_target')),
                   'target_kind':kind,'final_r':gross,'net_rr':net,'fee_drag_r':fee_r,'trigger_note':trig.get('note',''),'confirmations':int(trig.get('strength',0))+1,
                   'ldt13_state':st.get('state'),'ldt13_age_min':(now-safe_float(st.get('created')))/60.0,'prospective_entry':safe_float(st.get('planned_entry')),'prospective_rr':safe_float(st.get('prospective_rr')),
                   'prospective_fee_r':safe_float(st.get('prospective_fee_r')),'signal_bar_ts':int(st.get('origin_signal_ts') or signal.get('signal_bar_ts') or 0)})
    st['signal']=_ldt13_jsonable(signal); st['best_rr']=max(safe_float(st.get('best_rr')),net); st['best_eq']=max(safe_float(st.get('best_eq')),eq)
    if regime_block:
        _ldt13_transition(st,'WAIT_RETRACE',f'{engine} · regime {signal["regime"]} against route'); signal['skip']=f'TRACK-regime {signal["regime"]}'; signal['entry_route']=None; return signal
    if risk<=0 or risk_atr<LDT_MIN_STOP_ATR or risk_atr>LDT_MAX_STOP_ATR:
        _ldt13_transition(st,'WAIT_RETRACE',f'{engine} · live stop {risk_atr:.2f}ATR'); signal['skip']=f'TRACK-wait-retrace · RR@zone {safe_float(st.get("prospective_rr")):.2f}'; signal['entry_route']=None; return signal
    if fee_r>LDT15_MAX_FEE_R:
        _ldt13_transition(st,'IN_ZONE' if in_zone else 'WAIT_RETRACE',f'{engine} · fee drag {fee_r:.2f}R>{LDT15_MAX_FEE_R:.2f}R'); signal['skip']=f'fee-drag-{fee_r:.2f}R'; signal['entry_route']=None; return signal
    if not in_zone or eq<LDT_MIN_ENTRY_QUALITY:
        _ldt13_transition(st,'WAIT_RETRACE',f'{engine} · EQ {eq:.0f} · frozen zone {lo:.8g}-{hi:.8g}'); signal['skip']=f'TRACK-wait-zone EQ{eq:.0f}'; signal['entry_route']=None; return signal
    rrmin=_ldt14_live_rr_min(signal)
    if net<rrmin:
        _ldt13_transition(st,'IN_ZONE',f'{engine} zone reached · adaptive RR {net:.2f}<{rrmin:.2f}'); signal['skip']=f'TRACK-in-zone RR {net:.2f}<{rrmin:.2f}'; signal['entry_route']=None; return signal
    if not trig_ok:
        _ldt13_transition(st,'IN_ZONE',f'{engine} · {trig.get("note","waiting trigger")}'); signal['skip']=f'TRACK-in-zone · {trig.get("note","WAIT TRIGGER")}'; signal['entry_route']=None; return signal
    age=int(trig.get('age',0)); _ldt13_transition(st,'READY' if age==0 else 'TRIGGER_HOLD',trig.get('note','trigger valid'))
    signal['skip']=None; signal['entry_route']={'REVERSAL':'REV-CARD','CONTINUATION':'CONT-CARD','MOMENTUM':'MOM-CARD'}[engine]
    return signal


def _ldt15_can_add_state(fresh):
    if len(_ldt13_states)<LDT15_MAX_TRACKED: return True
    # Replace only a clearly weaker tracked thesis; this keeps the bot focused instead of carrying
    # 15-20 stale ideas through the night.
    weakest_sym=None; weakest=1e9
    for sym,st in _ldt13_states.items():
        u=safe_float(st.get('utility'),-999.0)
        if u<weakest: weakest=u; weakest_sym=sym
    nu=_ldt15_utility(fresh)
    if weakest_sym and nu>=weakest+5.0:
        old=_ldt13_states.pop(weakest_sym,None)
        if old: _ldt15_mark_dead(old,'evicted by stronger thesis')
        return True
    return False


def evaluate_symbol(symbol, btc=None):
    _ldt13_load(); _ldt15_load_dead(); candidates=_ldt13_scan_candidates(symbol); fresh=_ldt13_best_trackable(candidates); st=_ldt13_states.get(symbol)
    if st:
        # A genuinely newer structure may replace a frozen thesis. Merely rediscovering the same
        # parent/zone may not reset its clock.
        if fresh:
            old_ts=int(st.get('origin_signal_ts') or (st.get('signal') or {}).get('signal_bar_ts') or 0); new_ts=int(fresh.get('signal_bar_ts') or 0)
            age_min=(time.time()-safe_float(st.get('created')))/60.0
            stronger=_ldt15_utility(fresh)>=safe_float(st.get('utility'),-999.0)+2.0
            if str(fresh.get('side'))!=str(st.get('side')):
                if _ldt15_utility(fresh)>=safe_float(st.get('utility'),-999.0)+LDT13_REPLACE_EDGE:
                    _ldt15_mark_dead(st,'replaced opposite thesis'); st=_ldt13_new_state(fresh); _ldt13_states[symbol]=st
            elif new_ts>old_ts and age_min>=LDT15_FRESH_REPLACE_MIN and stronger:
                _ldt15_mark_dead(st,'promoted to fresh structure'); st=_ldt13_new_state(fresh); _ldt13_states[symbol]=st
            elif _ldt13_same_thesis(st,fresh) and _ldt14_engine(fresh)==str(st.get('engine')):
                _ldt13_merge_fresh(st,fresh)
        out=_ldt14_refresh_state(st)
        if out is None or st.get('state') in ('INVALIDATED','EXPIRED'):
            reason=st.get('state_note',''); _ldt15_mark_dead(st,reason); _ldt13_states.pop(symbol,None); _ldt13_save()
            if fresh and _ldt13_trackable(fresh) and not _ldt15_is_recycled(fresh) and _ldt15_can_add_state(fresh):
                st=_ldt13_new_state(fresh); _ldt13_states[symbol]=st; out=_ldt14_refresh_state(st)
            else: return _ldt13_best_diagnostic(candidates)
        _ldt13_save(); return out
    if fresh and not _ldt15_is_recycled(fresh) and _ldt15_can_add_state(fresh):
        st=_ldt13_new_state(fresh); _ldt13_states[symbol]=st; out=_ldt14_refresh_state(st); _ldt13_save(); return out
    return _ldt13_best_diagnostic(candidates)


def open_trade(signal):
    # Extra execution vetoes before the v1.4 hardened order shell.
    symbol=signal.get('symbol'); st=_ldt13_states.get(symbol)
    if st:
        fresh=_ldt14_refresh_state(st); _ldt13_save()
        if not fresh or fresh.get('skip') or not fresh.get('entry_route'):
            tg(f"⏭️ LDT v1.5 {symbol} {str(signal.get('side')).upper()} STATE CHANGED\n{(fresh or {}).get('skip') or st.get('state_note','not ready')}\nOrder cancelled before submission."); return False
        signal=fresh
    side=signal.get('side'); live=safe_float(exchange.fetch_ticker(symbol).get('last'),signal.get('price')); stop=safe_float(signal.get('structural_stop')); d=1 if side=='long' else -1
    risk=d*(live-stop)
    if risk<=0: return False
    fee_r=live*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12)
    if fee_r>LDT15_MAX_FEE_R:
        tg(f"⏭️ LDT v1.5 {symbol} {str(side).upper()} FEE-DRAG VETO\nEstimated round-trip cost {fee_r:.2f}R > {LDT15_MAX_FEE_R:.2f}R"); return False
    reg=_ldt15_regime(symbol,side); engine=_ldt14_engine(signal); card=int(signal.get('card_score',0)); fit=int(reg.get('fit',0))
    if (engine=='MOMENTUM' and fit<0) or (engine=='CONTINUATION' and fit<=-2 and card<4) or (engine=='REVERSAL' and fit>=2 and card<5):
        tg(f"⏭️ LDT v1.5 {symbol} {str(side).upper()} REGIME VETO\n{engine} vs {reg.get('label')} · CARD C{card}"); return False
    signal['regime']=reg.get('label'); signal['regime_fit']=fit; signal['fee_drag_r']=fee_r
    return _ldt15_base_open_trade(signal)


def manage_sniper_step_stops():
    """v1.5: fee-aware positive-net lock. A +1R excursion should not become a fee-only loss."""
    if PAPER_TRADING:return
    for p in get_positions():
        try:
            pid=position_id(p); meta=_ldt_meta.get(pid)
            if not meta:continue
            symbol=position_symbol(p); side=position_side(p); d=1 if side=='long' else -1
            entry=safe_float(meta.get('entry')) or position_entry(p); risk=safe_float(meta.get('risk'))
            if entry<=0 or risk<=0:continue
            ok,order=has_live_protection(p)
            if not ok or not order:continue
            current_sl=safe_float(order.get('slTriggerPrice')); tick=safe_float(get_instrument(symbol).get('tickSize'),0.0001); current=safe_float(exchange.fetch_ticker(symbol).get('last'))
            old_mfe=safe_float(meta.get('mfe_price'),entry); mfe=max(old_mfe,current) if d==1 else min(old_mfe,current); meta['mfe_price']=mfe
            best_r=max(0.0,d*(mfe-entry)/risk); meta['mfe_r']=best_r
            fee_r=entry*(2*TAKER_FEE_RATE+SLIPPAGE_RATE)/max(risk,1e-12); desired=None; stage=None
            if best_r>=LDT15_MANAGER_STAGE2_R:
                lock_r=max(LDT15_MANAGER_LOCK2_R,fee_r+0.25); desired=round_trade_price(entry+d*lock_r*risk,tick,side,'sl'); stage=f'+{best_r:.2f}R → lock +{lock_r:.2f}R'
            elif best_r>=LDT15_MANAGER_STAGE1_R:
                lock_r=max(LDT15_MANAGER_LOCK1_R,fee_r+0.08); lock_r=min(lock_r,0.45); desired=round_trade_price(entry+d*lock_r*risk,tick,side,'sl'); stage=f'+{best_r:.2f}R → fee-safe +{lock_r:.2f}R'
            if desired is None:continue
            improves=desired>current_sl+tick*0.1 if d==1 else desired<current_sl-tick*0.1
            if not improves:continue
            buf=max(tick*LDT11_STOP_MARK_BUFFER_TICKS,abs(current)*1e-6); safe_side=(desired<current-buf) if d==1 else (desired>current+buf)
            if not safe_side:continue
            ok2,res=amend_tpsl_stop(p,desired); key=f'ldt15:{pid}:{stage}'
            if ok2 and not sniper_step_notified.get(key):
                tg(f"🪜 LDT v1.5 PROFIT LOCK\n{symbol} {side.upper()}\n{stage} · MFE {best_r:.2f}R · fee drag {fee_r:.2f}R · SL {desired:.8g}"); sniper_step_notified[key]=True
        except Exception as e:
            tg(f"⚠️ LDT v1.5 POSITION MANAGER\n{position_symbol(p) if isinstance(p,dict) else '-'}\n{type(e).__name__}: {e}")


def maybe_score_report(rows):
    global _ldt13_last_report
    now=time.time()
    if now-_ldt13_last_report<LDT_REPORT_MIN*60 or not rows:return
    _ldt13_last_report=now; _ldt13_load(); ready=[r for r in rows if r.get('side') and not r.get('skip') and r.get('entry_route')]
    counts={}; engines={}
    for st in _ldt13_states.values():
        k=str(st.get('state') or 'WAIT_RETRACE'); counts[k]=counts.get(k,0)+1; e=str(st.get('engine') or '-'); engines[e]=engines.get(e,0)+1
    ranked=sorted(rows,key=lambda r:(1 if not r.get('skip') else 0,1 if r.get('symbol') in _ldt13_states else 0,_ldt15_utility(r)),reverse=True)[:10]
    state_text=' · '.join(f'{k} {v}' for k,v in sorted(counts.items(),key=lambda x:x[1],reverse=True)[:5]) or 'none'; eng_text=' · '.join(f'{k} {v}' for k,v in engines.items()) or 'none'
    lines=['🧠 LIQUIDITY DAYTRADER v1.5 · ADAPTIVE TRADING CARD',f"Ready {len(ready)} · Tracked {len(_ldt13_states)}/{LDT15_MAX_TRACKED} · scanned {len(rows)}",f"Today {daily.get('trades',0)} trades · W/L {daily.get('wins',0)}/{daily.get('losses',0)} · Net ${safe_float(daily.get('net')):.2f}",f'Engines: {eng_text}',f'States: {state_text}']
    for r in ranked:
        st=_ldt13_states.get(r.get('symbol'),{}); age=(now-safe_float(st.get('created'),now))/60 if st else 0; e=_ldt14_engine(r)
        if not r.get('skip'):status='🟢 EXECUTE'
        elif st:status=f"🟠 {st.get('state','WAIT_RETRACE')} {age:.0f}m"
        else:status='🟡 '+str(r.get('skip'))
        pr=safe_float(r.get('prospective_rr'),-99.0); prtxt='N/A' if pr<=-90 else f'{pr:.2f}'; fr=safe_float(r.get('prospective_fee_r',r.get('fee_drag_r',-1)))
        frtxt='-' if fr<0 or fr>10 else f'{fr:.2f}R'
        lines.append(f"{r.get('symbol')} {str(r.get('side')).upper()} {e} · {r.get('regime','?')} fit{int(r.get('regime_fit',0)):+d} · C{int(r.get('card_score',0))} STR{safe_float(r.get('setup_quality')):.0f} EQ{safe_float(r.get('entry_quality')):.0f} RRnow{safe_float(r.get('net_rr')):.2f} RR@zone{prtxt} fee{frtxt} {status}")
    tg('\n'.join(lines))


def startup_diagnostics():
    _ldt13_load(); _ldt15_load_dead(); acc=futures_account(); reset_daily_if_needed(acc['total_equity'])
    tg('🧠 LIQUIDITY DAYTRADER STARTED v1.5 · ADAPTIVE TRADING CARD\n'
       'Overnight audit: frozen thesis geometry · stale-thesis recycle lock · 15m regime routing · conservative RR@zone · fee-drag veto · positive-net profit lock.\n'
       f'Adaptive live RR: REV {LDT14_REV_LIVE_RR:.2f}R · CONT {LDT15_CONT_TREND_LIVE_RR:.2f}-{LDT15_CONT_COUNTER_LIVE_RR:.2f}R · MOM {LDT15_MOM_TREND_LIVE_RR:.2f}-{LDT14_MOM_LIVE_RR:.2f}R.\n'
       f'Max fee drag {LDT15_MAX_FEE_R:.2f}R · tracked cap {LDT15_MAX_TRACKED} · same stale thesis cooldown {LDT15_REARM_COOLDOWN_MIN}m.\n'
       f'Risk: REV {LDT14_REV_RISK*100:.2f}% · CONT {LDT14_CONT_RISK*100:.2f}% · MOM {LDT14_MOM_RISK*100:.2f}% · ELITE {LDT14_ELITE_RISK*100:.2f}%.\n'
       f'Max positions {MAX_ACTIVE_POSITIONS} · max same side {LDT_MAX_SAME_SIDE}.\n'
       f'Fresh v1.5 theses {len(_ldt13_states)} · Equity ${acc["total_equity"]:.2f} · Available ${acc["available"]:.2f}')

if __name__ == "__main__":
    main()
