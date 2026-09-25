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
# BLOFIN V6.14 - MACRO FLOW + COIN LOCATION GUARD
# ============================================================
# Gebouwd op V6.13. V6.14 voegt coin-locatie / anti-chase toe:
# 1) BTC-context blijft multi-timeframe: 1D+4H macro, 1H regime, 15m trigger.
# 2) Iedere coin krijgt nu óók een eigen 1H-locatiecheck over 2/4/6/10 uur.
#    Een LONG wordt hard geblokkeerd als de coin al te ver omhoog is gerend;
#    SHORT gespiegeld na een te grote daling. Dit kan NIET door score/ADX worden
#    overruled.
# 3) Extra top/bottom-locatiecheck: ver van 1H EMA20 + dicht bij recente 10h-high/low
#    = niet meer achter de beweging aan springen.
# 4) Als live execution zegt 'too late', krijgt dat symbool een persistent late-lock.
#    Een volgende scan mag de meetlat dus niet simpelweg opnieuw leggen. De lock
#    verdwijnt pas na een echte reset/pullback richting 15m EMA20.
# 5) EARLY/PULLBACK/MACRO routes, A-sizing, BTC flow, protection, leverage-check
#    en daily risk cap blijven verder intact.
#
# Geschreven voor One-way / NET position mode.
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
MAX_ACTIVE_POSITIONS = int(os.getenv("MAX_ACTIVE_POSITIONS", "2"))
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
    global protection_lock
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
    global consecutive_losses, loss_pause_until
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
        symbol_last_result[symbol] = "win"; symbol_loss_streak[symbol] = 0
    else:
        daily["losses"] += 1; consecutive_losses += 1
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
        f"{icon} POSITION CLOSED V6.14\n\n{symbol}\n{side.upper()}\n━━━━━━━━━━━━\n"
        f"Entry: {entry:.8g}\nExit: {exit_price:.8g}\nSize: {size:g}\n\n"
        f"💰 Realized: {'+' if gross_pnl >= 0 else ''}${gross_pnl:.2f}\n"
        f"📊 Ratio: {pnl_ratio:+.2f}%\n💸 Fee: -${fee:.2f}\n"
        f"💵 Net: {'+' if net_pnl >= 0 else ''}${net_pnl:.2f}\n\n🎯 {reason}\n⏱ {duration}\n\n"
        f"📅 TODAY Amsterdam\nTrades: {daily['trades']} | W/L {daily['wins']}/{daily['losses']} | Net ${daily['net']:.2f}\n"
        f"LONG: {daily['long_trades']} / {wr_long:.0f}% / ${daily['long_net']:.2f}\n"
        f"SHORT: {daily['short_trades']} / {wr_short:.0f}% / ${daily['short_net']:.2f}{pause}"
    )


def seed_closed_history():
    reset_daily_if_needed()
    try:
        for r in closed_positions_history(100):
            hid = str(r.get("historyId", ""))
            if hid: seen_closed_history_ids.add(hid)
        tg(f"📚 V6.14 PNL TRACKER READY\nKnown closes: {len(seen_closed_history_ids)}\nLearning trades: {learning.get('trades', 0)}")
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
# MAIN
# ============================================================
def main():
    global protection_lock
    load_learning()
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
                try: check_for_closed_positions()
                except Exception as e: tg(f"⚠️ PNL TRACKER ERROR\n{e}")

            acc = futures_account()
            ok, reason = check_daily_risk(acc["total_equity"])
            if not ok:
                tg(f"🛑 {reason}"); time.sleep(120); continue
            if time.time() < loss_pause_until:
                time.sleep(SCAN_SECONDS); continue

            positions = get_positions()
            if not PAPER_TRADING: protection_watch()
            positions = get_positions()
            if len(positions) >= MAX_ACTIVE_POSITIONS:
                time.sleep(SCAN_SECONDS); continue
            if protection_lock and not PAPER_TRADING:
                time.sleep(SCAN_SECONDS); continue

            try:
                btc = btc_regime()
            except Exception as e:
                tg(f"⚠️ BTC REGIME ERROR\n{e}"); time.sleep(SCAN_SECONDS); continue

            scanned, candidates = [], []
            for symbol in SYMBOLS:
                if not cooldown_ok(symbol): continue
                try:
                    signal = evaluate_symbol(symbol, btc)
                    if not signal: continue
                    scanned.append(signal)
                    if not signal.get("skip") and signal.get("entry_route") in ("EARLY", "PULLBACK", "MACRO"):
                        candidates.append(signal)
                except Exception:
                    continue

            maybe_score_report(scanned)

            # EARLY gets a small ranking preference because its purpose is timing,
            # but score/quality still dominate inside the route.
            candidates.sort(key=lambda x: (
                1 if x.get("btc_flow_strong") else 0,
                1 if x.get("btc_macro_pullback_support") else 0,
                1 if is_a_setup(x) else 0,
                1 if x.get("entry_route") in ("EARLY", "MACRO") else 0,
                x["score"], x["entry_quality"], x["confirm_components"], x["adx"], x["tactics"],
            ), reverse=True)

            execution_attempts = 0
            for signal in candidates:
                current = get_positions()
                if len(current) >= MAX_ACTIVE_POSITIONS: break
                existing_symbols = {position_symbol(p) for p in current}
                if signal["symbol"] in existing_symbols: continue
                alt_l, alt_s = count_alt_sides(current)
                if signal["symbol"] not in MAJORS:
                    # V6.12: normal cap remains 1 per alt direction. A second
                    # same-direction alt is allowed only when BTC flow is strong
                    # and this new signal itself passes the strict flow gate.
                    long_cap = MAX_ALT_LONGS
                    short_cap = MAX_ALT_SHORTS
                    if flow_second_alt_ok(signal):
                        if signal["side"] == "long":
                            long_cap = max(long_cap, FLOW_SECOND_ALT_CAP)
                        else:
                            short_cap = max(short_cap, FLOW_SECOND_ALT_CAP)
                    if signal["side"] == "long" and alt_l >= long_cap: continue
                    if signal["side"] == "short" and alt_s >= short_cap: continue
                if execution_attempts >= MAX_EXECUTION_ATTEMPTS_PER_SCAN: break
                execution_attempts += 1
                opened = open_trade(signal)
                if opened:
                    symbol_last_trade[signal["symbol"]] = time.time()
                    break

            time.sleep(SCAN_SECONDS)

        except KeyboardInterrupt:
            tg("🛑 V6.14 stopped.")
            raise
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if "152002" in msg:
                time.sleep(20); continue
            tg(f"⚠️ V6.14 LOOP ERROR\n{msg}")
            time.sleep(30)


if __name__ == "__main__":
    main()
