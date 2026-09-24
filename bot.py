import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone, timedelta

import ccxt
import numpy as np
import pandas as pd
import requests

# ============================================================
# BLOFIN V7.0 — ADAPTIVE FLOW
# ============================================================
# V7.0 keeps V6.6 risk controls and adds:
# - closed-candle regime + market breadth filter
# - 1h relative-strength persistence
# - volatility-compression -> breakout/release tactic
# - 5m micro-breakout / reclaim confirmation
# - order-book imbalance + spread filter on finalists
# - funding-rate crowding filter on finalists
# - adaptive TP R (trend strength / volatility)
# - adaptive daily risk throttle
# - correct short-side price rounding
# - TP+SL protection must BOTH exist
# - optional profit-lock trailing stop after +R
# - leverage verification before a live entry
# - lower API load: microstructure only on finalists
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
MIN_SCORE = float(os.getenv("MIN_SCORE", "64"))
MIN_TACTICS = int(os.getenv("MIN_TACTICS", "4"))
FINALIST_COUNT = int(os.getenv("FINALIST_COUNT", "8"))

LEVERAGE = int(os.getenv("LEVERAGE", "10"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")
BASE_MARGIN_USDT = float(os.getenv("BASE_MARGIN_USDT", "10"))
MAX_MARGIN_USDT = float(os.getenv("MAX_MARGIN_USDT", "12"))
MAX_MARGIN_EQUITY_PCT = float(os.getenv("MAX_MARGIN_EQUITY_PCT", "0.18"))
MAX_RISK_EQUITY_PCT = float(os.getenv("MAX_RISK_EQUITY_PCT", "0.010"))
AVAILABLE_MARGIN_BUFFER = float(os.getenv("AVAILABLE_MARGIN_BUFFER", "0.90"))

ATR_PERIOD = 14
ADX_PERIOD = 14
RSI_PERIOD = 14
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.40"))
TP_R_BASE = float(os.getenv("TP_R_BASE", "1.80"))
TP_R_MIN = float(os.getenv("TP_R_MIN", "1.60"))
TP_R_MAX = float(os.getenv("TP_R_MAX", "2.10"))
MIN_STOP_PCT = float(os.getenv("MIN_STOP_PCT", "0.0045"))
MAX_STOP_PCT = float(os.getenv("MAX_STOP_PCT", "0.028"))
MIN_NET_TP_USDT = float(os.getenv("MIN_NET_TP_USDT", "0.50"))
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.0006"))
SLIPPAGE_RATE = float(os.getenv("SLIPPAGE_RATE", "0.0003"))

VOLUME_MULT = float(os.getenv("VOLUME_MULT", "1.20"))
DONCHIAN_LOOKBACK = 12
NEAR_SWEEP_ATR = 0.20
ADX_TREND = float(os.getenv("ADX_TREND", "18"))
ADX_STRONG = float(os.getenv("ADX_STRONG", "26"))
ADX_CHOP = float(os.getenv("ADX_CHOP", "14"))
RSI_LONG_MAX = float(os.getenv("RSI_LONG_MAX", "68"))
RSI_SHORT_MIN = float(os.getenv("RSI_SHORT_MIN", "32"))
RS_OVERRIDE = float(os.getenv("RS_OVERRIDE", "0.008"))
RS_1H_THRESHOLD = float(os.getenv("RS_1H_THRESHOLD", "0.012"))
MAX_EMA_EXT_PCT = float(os.getenv("MAX_EMA_EXT_PCT", "0.012"))
PULLBACK_ATR = float(os.getenv("PULLBACK_ATR", "0.45"))
IMPULSE_15M_ATR = float(os.getenv("IMPULSE_15M_ATR", "1.30"))
IMPULSE_1H_ATR = float(os.getenv("IMPULSE_1H_ATR", "1.60"))
MAX_RUN_PCT = float(os.getenv("MAX_RUN_PCT", "0.035"))
MAX_FILL_SLIP_PCT = float(os.getenv("MAX_FILL_SLIP_PCT", "0.0025"))

COMPRESSION_FACTOR = float(os.getenv("COMPRESSION_FACTOR", "0.75"))
RELEASE_RANGE_ATR = float(os.getenv("RELEASE_RANGE_ATR", "0.95"))
BOOK_DEPTH = int(os.getenv("BOOK_DEPTH", "10"))
BOOK_IMBALANCE_HARD = float(os.getenv("BOOK_IMBALANCE_HARD", "0.35"))
MAX_BOOK_SPREAD_PCT = float(os.getenv("MAX_BOOK_SPREAD_PCT", "0.0015"))
FUNDING_EXTREME = float(os.getenv("FUNDING_EXTREME", "0.0010"))

DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "4"))
DAILY_SOFT_DD_PCT = float(os.getenv("DAILY_SOFT_DD_PCT", "2"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
MIN_EQUITY_USDT = float(os.getenv("MIN_EQUITY_USDT", "30"))
REPAIR_EXISTING_POSITIONS = os.getenv("REPAIR_EXISTING_POSITIONS", "true").lower() == "true"
SCORE_REPORT_MIN = int(os.getenv("SCORE_REPORT_MIN", "30"))

TRAIL_ENABLE = os.getenv("TRAIL_ENABLE", "true").lower() == "true"
TRAIL_ACTIVATE_R = float(os.getenv("TRAIL_ACTIVATE_R", "1.15"))
TRAIL_MIN_CALLBACK = float(os.getenv("TRAIL_MIN_CALLBACK", "0.004"))
TRAIL_MAX_CALLBACK = float(os.getenv("TRAIL_MAX_CALLBACK", "0.012"))

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


def tg(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10
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
        if v is None or v == "": return default
        return float(v)
    except Exception:
        return default


def fmt(x, n=4):
    try: return f"{float(x):.{n}f}"
    except Exception: return str(x)


def native(method_name, params=None):
    fn = getattr(exchange, method_name, None)
    if fn is None:
        raise RuntimeError(f"CCXT implicit endpoint missing: {method_name}")
    return fn(params or {})


def native_ok(resp):
    return isinstance(resp, dict) and str(resp.get("code")) == "0"


def response_error(resp):
    if not isinstance(resp, dict): return str(resp)
    code = str(resp.get("code", "?")); msg = str(resp.get("msg", ""))
    data = resp.get("data")
    if isinstance(data, dict):
        if str(data.get("code")) not in ("None", "", "0"):
            code = str(data.get("code"))
        if data.get("msg"): msg = str(data.get("msg"))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("code")) not in ("None", "", "0"):
                code = str(item.get("code")); msg = str(item.get("msg") or msg); break
    return f"{code} {msg}".strip()


def blofin_inst_id(symbol):
    return symbol.split("/")[0] + "-USDT"


def get_instrument(symbol):
    resp = native("public_get_market_instruments", {"instId": blofin_inst_id(symbol)})
    if not native_ok(resp) or not (resp.get("data") or []):
        raise RuntimeError(f"Instrument lookup failed: {response_error(resp)}")
    return resp["data"][0]


def floor_step(value, step):
    v, s = Decimal(str(value)), Decimal(str(step))
    return float((v / s).to_integral_value(rounding=ROUND_DOWN) * s) if s > 0 else float(v)


def ceil_step(value, step):
    v, s = Decimal(str(value)), Decimal(str(step))
    return float((v / s).to_integral_value(rounding=ROUND_UP) * s) if s > 0 else float(v)


def round_price_for_side(price, tick, direction):
    # For longs: SL/TP are safely rounded down. For shorts: TP down, SL up.
    return ceil_step(price, tick) if direction == "up" else floor_step(price, tick)


def futures_account():
    resp = native("private_get_account_balance", {"productType": "USDT-FUTURES"})
    if not native_ok(resp): raise RuntimeError(f"Balance API failed: {response_error(resp)}")
    data = resp.get("data") or {}; details = data.get("details") or []
    usdt = next((d for d in details if d.get("currency") == "USDT"), {})
    total = safe_float(data.get("totalEquity")); equity = safe_float(usdt.get("equity"))
    balance = safe_float(usdt.get("balance")); available = safe_float(usdt.get("available"))
    if total <= 0: total = equity or balance
    return {"total_equity": total, "equity": equity or total, "balance": balance, "available": available}


def get_positions():
    resp = native("private_get_account_positions", {})
    if not native_ok(resp): raise RuntimeError(f"Positions API failed: {response_error(resp)}")
    out = []
    for p in resp.get("data") or []:
        if abs(safe_float(p.get("positions", p.get("position", p.get("size", 0))))) > 0:
            out.append(p)
    return out


def position_symbol(p): return str(p.get("instId", "")).replace("-USDT", "/USDT:USDT")


def position_side(p):
    s = str(p.get("positionSide", "net")).lower()
    if s in ("long", "short"): return s
    n = safe_float(p.get("positions", p.get("position", p.get("size", 0))))
    return "long" if n > 0 else "short"


def position_size(p): return abs(safe_float(p.get("positions", p.get("position", p.get("size", 0)))))

def position_entry(p): return safe_float(p.get("averagePrice", p.get("entryPrice", p.get("avgPrice", 0))))

def position_id(p): return str(p.get("positionId", "") or "")

def position_margin_mode(p): return str(p.get("marginMode", MARGIN_MODE)).lower()


def ohlcv_df(symbol, timeframe):
    rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=CANDLE_LIMIT)
    if len(rows) < 80: raise RuntimeError(f"Not enough candles for {symbol} {timeframe}")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    for c in ["open", "high", "low", "close", "volume"]: df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna().reset_index(drop=True)


def wilder(series, period): return series.ewm(alpha=1 / period, adjust=False).mean()


def add_indicators(df):
    o = df.copy()
    o["ema20"] = o.close.ewm(span=20, adjust=False).mean()
    o["ema50"] = o.close.ewm(span=50, adjust=False).mean()
    o["ema200"] = o.close.ewm(span=200, adjust=False).mean()
    o["ema20_slope"] = o.ema20 - o.ema20.shift(3)
    prev = o.close.shift(1)
    tr = pd.concat([o.high-o.low, (o.high-prev).abs(), (o.low-prev).abs()], axis=1).max(axis=1)
    o["atr"] = tr.rolling(ATR_PERIOD).mean()
    up = o.close.diff().clip(lower=0); down = -o.close.diff().clip(upper=0)
    rs = wilder(up, RSI_PERIOD) / wilder(down, RSI_PERIOD).replace(0, np.nan)
    o["rsi"] = 100 - 100 / (1 + rs)
    upm = o.high.diff(); dnm = -o.low.diff()
    plus = np.where((upm > dnm) & (upm > 0), upm, 0.0)
    minus = np.where((dnm > upm) & (dnm > 0), dnm, 0.0)
    atrw = wilder(tr, ADX_PERIOD)
    pdi = 100*wilder(pd.Series(plus, index=o.index), ADX_PERIOD)/atrw.replace(0,np.nan)
    mdi = 100*wilder(pd.Series(minus, index=o.index), ADX_PERIOD)/atrw.replace(0,np.nan)
    dx = 100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    o["adx"] = wilder(dx, ADX_PERIOD); o["plus_di"] = pdi; o["minus_di"] = mdi
    o["vol_sma20"] = o.volume.rolling(20).mean()
    o["donchian_high"] = o.high.rolling(DONCHIAN_LOOKBACK).max().shift(1)
    o["donchian_low"] = o.low.rolling(DONCHIAN_LOOKBACK).min().shift(1)
    o["ret15"] = o.close.pct_change(3)
    o["ret1h"] = o.close.pct_change(4)
    o["bb_mid"] = o.close.rolling(20).mean()
    o["bb_std"] = o.close.rolling(20).std()
    o["bb_width"] = 4*o.bb_std/o.close
    o["bb_med60"] = o.bb_width.rolling(60).median()
    o["compression"] = o.bb_width < (o.bb_med60 * COMPRESSION_FACTOR)
    return o


btc_cache = {"ts": 0, "data": None}
funding_cache = {}
book_cache = {}


def btc_regime():
    if btc_cache["data"] and time.time()-btc_cache["ts"] < 60: return btc_cache["data"]
    d15 = add_indicators(ohlcv_df(BTC_SYMBOL, TIMEFRAME_SIGNAL)); d1 = add_indicators(ohlcv_df(BTC_SYMBOL, TIMEFRAME_TREND))
    a, t = d15.iloc[-2], d1.iloc[-2]
    x = {
        "bull": bool(t.ema20 > t.ema50), "bear": bool(t.ema20 < t.ema50),
        "impulse_up": bool(a.close > a.ema20 and a.ema20 >= a.ema50),
        "impulse_down": bool(a.close < a.ema20 and a.ema20 <= a.ema50),
        "ret15": safe_float(a.ret15), "ret1h": safe_float(a.ret1h), "adx": safe_float(a.adx),
        "close": safe_float(a.close),
    }
    btc_cache.update(ts=time.time(), data=x); return x


def session_state():
    h = now_utc().hour
    if h < NIGHT_END_UTC: return "NIGHT"
    if CORE_START_UTC <= h < CORE_END_UTC: return "CORE"
    return "SHOULDER"


def evaluate_symbol(symbol, btc):
    sess = session_state()
    if sess == "NIGHT" and symbol not in MAJORS:
        return {"symbol":symbol,"side":None,"score":0,"tactics":0,"skip":"night-alts","price":0,"atr":0}
    d15 = add_indicators(ohlcv_df(symbol, TIMEFRAME_SIGNAL)); d5 = add_indicators(ohlcv_df(symbol, TIMEFRAME_CONFIRM)); d1 = add_indicators(ohlcv_df(symbol, TIMEFRAME_TREND))
    a, ap, c, cp, t = d15.iloc[-2], d15.iloc[-3], d5.iloc[-2], d5.iloc[-3], d1.iloc[-2]
    price, atr = safe_float(a.close), safe_float(a.atr)
    if price <= 0 or atr <= 0: return None
    rsi, adx = safe_float(a.rsi,50), safe_float(a.adx)
    rel15 = safe_float(a.ret15) - btc["ret15"]
    rel1h = safe_float(a.ret1h) - btc["ret1h"]
    ema20 = safe_float(a.ema20); dist_ema = (price-ema20)/price
    slope_up = safe_float(a.ema20_slope) > 0; slope_down = safe_float(a.ema20_slope) < 0
    stack_l = t.ema20 > t.ema50 > t.ema200; stack_s = t.ema20 < t.ema50 < t.ema200
    fast_l = t.ema20 > t.ema50; fast_s = t.ema20 < t.ema50
    ema_l = a.ema20 > a.ema50 and price >= ema20 and slope_up
    ema_s = a.ema20 < a.ema50 and price <= ema20 and slope_down
    volume_ok = bool(pd.notna(a.vol_sma20) and a.volume > a.vol_sma20*VOLUME_MULT)
    atr_pct = atr/price
    atr_quality = 0.0018 <= atr_pct <= 0.035
    near_l = abs(price-ema20) <= PULLBACK_ATR*atr or min(float(a.low),float(ap.low)) <= ema20+PULLBACK_ATR*atr
    near_s = abs(price-ema20) <= PULLBACK_ATR*atr or max(float(a.high),float(ap.high)) >= ema20-PULLBACK_ATR*atr
    ext_l, ext_s = dist_ema > MAX_EMA_EXT_PCT, dist_ema < -MAX_EMA_EXT_PCT
    recent_low = d15.low.iloc[-DONCHIAN_LOOKBACK-3:-3].min(); recent_high = d15.high.iloc[-DONCHIAN_LOOKBACK-3:-3].max()
    sweep_l = ap.low < recent_low-NEAR_SWEEP_ATR*atr and a.close > recent_low
    sweep_s = ap.high > recent_high+NEAR_SWEEP_ATR*atr and a.close < recent_high
    breakout_l = bool(pd.notna(a.donchian_high) and a.close > a.donchian_high and not ext_l)
    breakout_s = bool(pd.notna(a.donchian_low) and a.close < a.donchian_low and not ext_s)
    pull_l = near_l and ema_l; pull_s = near_s and ema_s
    structure_l = pull_l or sweep_l or breakout_l; structure_s = pull_s or sweep_s or breakout_s
    c_atr = safe_float(c.atr, atr)
    bounce5 = c.low <= c.ema20+0.35*c_atr
    reject5 = c.high >= c.ema20-0.35*c_atr
    micro_l = c.close > cp.high; micro_s = c.close < cp.low
    reclaim_l = c.low < cp.low and c.close > cp.close; reclaim_s = c.high > cp.high and c.close < cp.close
    confirm_l = c.ema20 > c.ema50 and c.close > c.ema20 and (c.close > cp.close or micro_l or reclaim_l) and (bounce5 or micro_l or reclaim_l)
    confirm_s = c.ema20 < c.ema50 and c.close < c.ema20 and (c.close < cp.close or micro_s or reclaim_s) and (reject5 or micro_s or reclaim_s)
    was_compressed = bool(d15.compression.iloc[-5:-2].any())
    release_l = was_compressed and breakout_l and (a.high-a.low) >= RELEASE_RANGE_ATR*atr and volume_ok
    release_s = was_compressed and breakout_s and (a.high-a.low) >= RELEASE_RANGE_ATR*atr and volume_ok
    impulse15 = (a.high-a.low) > IMPULSE_15M_ATR*atr
    atr1h = safe_float(t.atr, atr); impulse1h = (t.high-t.low) > IMPULSE_1H_ATR*atr1h
    run8 = safe_float(d15.close.iloc[-2]/d15.close.iloc[-10]-1) if len(d15)>=10 else 0
    trending = adx >= ADX_TREND

    ls=ss=lt=st=0
    if stack_l: ls+=14; lt+=1
    elif fast_l: ls+=7
    if stack_s: ss+=14; st+=1
    elif fast_s: ss+=7
    if ema_l: ls+=12; lt+=1
    if ema_s: ss+=12; st+=1
    if structure_l: ls += 15 if sweep_l else (13 if release_l else 10); lt+=1
    if structure_s: ss += 15 if sweep_s else (13 if release_s else 10); st+=1
    if confirm_l: ls+=14; lt+=1
    if confirm_s: ss+=14; st+=1
    if rel1h > RS_1H_THRESHOLD: ls+=7; lt+=1
    elif rel1h > 0.004: ls+=4
    if rel1h < -RS_1H_THRESHOLD: ss+=7; st+=1
    elif rel1h < -0.004: ss+=4
    if release_l: ls+=7; lt+=1
    if release_s: ss+=7; st+=1
    if volume_ok: ls+=8; ss+=8
    if atr_quality: ls+=4; ss+=4
    if trending: ls+=4; ss+=4
    if rel15 > RS_OVERRIDE: ls+=4
    if rel15 < -RS_OVERRIDE: ss+=4

    side = "long" if ls >= ss else "short"; score = float(max(ls,ss)); tactics = lt if side=="long" else st
    confirm = confirm_l if side=="long" else confirm_s; structure = structure_l if side=="long" else structure_s
    skip = None
    if sess=="SHOULDER": score-=3
    if adx < ADX_CHOP: skip="adx-chop"
    if not trending: skip = skip or "weak-adx"
    if side=="long" and rsi>=RSI_LONG_MAX: skip=skip or "rsi-overbought"
    if side=="short" and rsi<=RSI_SHORT_MIN: skip=skip or "rsi-oversold"
    if side=="long" and (ext_l or not structure): skip=skip or "no-pullback-or-structure"
    if side=="short" and (ext_s or not structure): skip=skip or "no-pullback-or-structure"
    if impulse15 or impulse1h: skip=skip or "impulse-chase"
    if side=="long" and run8>MAX_RUN_PCT: skip=skip or "extended-run"
    if side=="short" and run8<-MAX_RUN_PCT: skip=skip or "extended-run"
    if not confirm: skip=skip or "no-5m-confirm"
    if tactics<MIN_TACTICS: skip=skip or f"tactics-{tactics}/{MIN_TACTICS}"
    if score<MIN_SCORE: skip=skip or f"score-{score:.0f}<{MIN_SCORE:.0f}"
    if not atr_quality: skip=skip or "atr-band"
    if symbol not in MAJORS:
        if side=="long" and btc["impulse_down"] and rel1h <= 0: skip=skip or "btc-against-long"
        if side=="short" and btc["impulse_up"] and rel1h >= 0: skip=skip or "btc-against-short"
    return {
        "symbol":symbol,"side":side,"score":score,"tactics":tactics,"price":price,"atr":atr,"atr_pct":atr_pct,
        "rsi":rsi,"adx":adx,"rel":rel15,"rel1h":rel1h,"confirm":bool(confirm),"structure":bool(structure),
        "release":bool(release_l if side=="long" else release_s),"skip":skip,"session":sess,
    }


def market_breadth(rows):
    valid=[r for r in rows if r.get("symbol") not in MAJORS and r.get("skip") != "night-alts"]
    if not valid: return {"bull":0.5,"bear":0.5,"n":0}
    bull=sum(1 for r in valid if r.get("rel1h",0)>0 and r.get("side")=="long")/len(valid)
    bear=sum(1 for r in valid if r.get("rel1h",0)<0 and r.get("side")=="short")/len(valid)
    return {"bull":bull,"bear":bear,"n":len(valid)}


def apply_breadth_filter(signal, breadth):
    if signal["symbol"] in MAJORS: return signal
    s=signal["side"]
    # Only a soft regime penalty, not a blanket long/short ban.
    if s=="long" and breadth["bear"]>0.62: signal["score"]-=5; signal["skip"] = signal["skip"] or "weak-breadth"
    if s=="short" and breadth["bull"]>0.62: signal["score"]-=5; signal["skip"] = signal["skip"] or "strong-breadth"
    if signal["score"] < MIN_SCORE and not signal.get("skip"): signal["skip"]=f"score-{signal['score']:.0f}<{MIN_SCORE:.0f}"
    return signal


def orderbook_metrics(symbol):
    cached=book_cache.get(symbol)
    if cached and time.time()-cached["ts"]<15: return cached
    resp=native("public_get_market_books", {"instId":blofin_inst_id(symbol),"size":str(BOOK_DEPTH)})
    if not native_ok(resp) or not (resp.get("data") or []): raise RuntimeError(response_error(resp))
    b=resp["data"][0]; bids=b.get("bids") or []; asks=b.get("asks") or []
    bn=sum(safe_float(x[0])*safe_float(x[1]) for x in bids); an=sum(safe_float(x[0])*safe_float(x[1]) for x in asks)
    bid=safe_float(bids[0][0]) if bids else 0; ask=safe_float(asks[0][0]) if asks else 0
    mid=(bid+ask)/2 if bid and ask else 0
    spread=(ask-bid)/mid if mid>0 else 1
    imb=(bn-an)/(bn+an) if bn+an>0 else 0
    x={"ts":time.time(),"imbalance":imb,"spread":spread}; book_cache[symbol]=x; return x


def funding_rate(symbol):
    c=funding_cache.get(symbol)
    if c and time.time()-c["ts"]<300: return c["rate"]
    resp=native("public_get_market_funding_rate", {"instId":blofin_inst_id(symbol)})
    if not native_ok(resp) or not (resp.get("data") or []): raise RuntimeError(response_error(resp))
    rate=safe_float(resp["data"][0].get("fundingRate")); funding_cache[symbol]={"ts":time.time(),"rate":rate}; return rate


def microstructure_ok(signal):
    if signal["symbol"] in MAJORS: return True,0.0,0.0
    try:
        b=orderbook_metrics(signal["symbol"]); f=funding_rate(signal["symbol"])
        imb, spread=b["imbalance"], b["spread"]
        if spread>MAX_BOOK_SPREAD_PCT: return False,imb,f
        if signal["side"]=="long" and imb < -BOOK_IMBALANCE_HARD: return False,imb,f
        if signal["side"]=="short" and imb > BOOK_IMBALANCE_HARD: return False,imb,f
        if signal["side"]=="long" and f > FUNDING_EXTREME: return False,imb,f
        if signal["side"]=="short" and f < -FUNDING_EXTREME: return False,imb,f
        return True,imb,f
    except Exception:
        # Public microstructure data is an enhancer, never a reason to crash the scanner.
        return True,0.0,0.0


def adaptive_tp_r(signal):
    r=TP_R_BASE
    if signal.get("adx",0)>=ADX_STRONG: r+=0.20
    elif signal.get("adx",0)<21: r-=0.15
    if signal.get("release"): r+=0.10
    if signal.get("atr_pct",0)>0.018: r-=0.10
    return max(TP_R_MIN,min(TP_R_MAX,r))


def calculate_trade_levels(entry, atr, side, tick, tp_r):
    risk=max(atr*ATR_SL_MULT,entry*MIN_STOP_PCT); risk=min(risk,entry*MAX_STOP_PCT)
    if side=="long":
        sl=round_price_for_side(entry-risk,tick,"down"); tp=round_price_for_side(entry+risk*tp_r,tick,"down")
        if not sl<entry<tp:return None
    else:
        sl=round_price_for_side(entry+risk,tick,"up"); tp=round_price_for_side(entry-risk*tp_r,tick,"down")
        if not tp<entry<sl:return None
    return sl,tp,abs(entry-sl),tp_r


def roundtrip_cost(notional): return notional*((TAKER_FEE_RATE*2)+SLIPPAGE_RATE)


def calculate_contracts(symbol,entry,sl,equity,available,tp_r,risk_factor):
    meta=get_instrument(symbol); cv=safe_float(meta.get("contractValue")); mn=safe_float(meta.get("minSize")); lot=safe_float(meta.get("lotSize")); mx=safe_float(meta.get("maxMarketSize"),0)
    if min(cv,mn,lot)<=0: raise RuntimeError(f"Bad contract metadata {symbol}")
    stop_pct=abs(entry-sl)/entry; risk_budget=equity*MAX_RISK_EQUITY_PCT*risk_factor
    risk_notional=risk_budget/stop_pct if stop_pct>0 else 0
    margin_cap=min(BASE_MARGIN_USDT,MAX_MARGIN_USDT,equity*MAX_MARGIN_EQUITY_PCT)
    target=min(risk_notional,margin_cap*LEVERAGE,(available*AVAILABLE_MARGIN_BUFFER)*LEVERAGE)
    if target<=0:return None
    contracts=floor_step(target/(cv*entry),lot)
    if contracts<mn: contracts=mn
    if mx>0: contracts=min(contracts,floor_step(mx,lot))
    notional=contracts*cv*entry; margin=notional/LEVERAGE
    if margin>available*AVAILABLE_MARGIN_BUFFER+1e-9 or margin>MAX_MARGIN_USDT+1e-9:return None
    expected_tp=notional*tp_r*stop_pct; cost=roundtrip_cost(notional)
    if expected_tp-cost<MIN_NET_TP_USDT:return None
    return {"contracts":contracts,"contract_value":cv,"notional":notional,"margin":margin,"expected_tp_usdt":expected_tp,"roundtrip_cost":cost,"stop_pct":stop_pct}


def set_leverage(symbol,lev):
    resp=native("private_post_account_set_leverage",{"instId":blofin_inst_id(symbol),"leverage":str(lev),"marginMode":MARGIN_MODE,"positionSide":"net"})
    return native_ok(resp),response_error(resp)


def verify_leverage(symbol,lev):
    try:
        resp=native("private_get_account_batch_leverage_info",{"instId":blofin_inst_id(symbol),"marginMode":MARGIN_MODE})
        if not native_ok(resp): return False
        rows=resp.get("data") or []
        row=next((x for x in rows if x.get("instId")==blofin_inst_id(symbol)),None)
        return row is not None and abs(safe_float(row.get("leverage"))-lev)<1e-9
    except Exception:
        return False


def pending_tpsl(inst_id=None):
    p={"limit":"100"}
    if inst_id:p["instId"]=inst_id
    resp=native("private_get_trade_orders_tpsl_pending",p)
    if not native_ok(resp): raise RuntimeError(f"TPSL lookup failed: {response_error(resp)}")
    return resp.get("data") or []


def has_live_protection(p):
    inst=p.get("instId"); pside=str(p.get("positionSide","net")).lower(); pid=position_id(p); has_tp=has_sl=False; found=None
    try: orders=pending_tpsl(inst)
    except Exception:return False,None
    for o in orders:
        if str(o.get("state","")).lower()!="live" or o.get("instId")!=inst:continue
        if str(o.get("positionSide","net")).lower()!=pside:continue
        opid=str(o.get("positionId","") or "")
        if pid and opid and opid!=pid:continue
        if safe_float(o.get("tpTriggerPrice"))>0:has_tp=True;found=found or o
        if safe_float(o.get("slTriggerPrice"))>0:has_sl=True;found=found or o
    return has_tp and has_sl,found


def tpsl_prices_valid(side,sl,tp,last): return last>0 and ((side=="long" and sl<last<tp) or (side=="short" and tp<last<sl))


def place_tpsl_for_position(p,sl,tp,tag="v70"):
    payload={"instId":p["instId"],"marginMode":position_margin_mode(p),"positionSide":str(p.get("positionSide","net")).lower(),"side":"sell" if position_side(p)=="long" else "buy","tpTriggerPrice":str(tp),"tpOrderPrice":"-1","tpTriggerPriceType":"mark","slTriggerPrice":str(sl),"slOrderPrice":"-1","slTriggerPriceType":"mark","size":"-1","reduceOnly":"true","clientOrderId":f"{tag}{int(time.time())%100000000}"}
    pid=position_id(p)
    if pid:payload["positionId"]=pid
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    resp=native("private_post_trade_order_tpsl",payload)
    if not native_ok(resp):return False,response_error(resp)
    time.sleep(0.6)
    ok,found=has_live_protection(p)
    return (True,found) if ok else (False,"TPSL accepted but TP+SL not both visible")


def place_trailing_stop(p, entry, risk_distance, atr_pct):
    if not TRAIL_ENABLE:return False
    side=position_side(p); current=safe_float(exchange.fetch_ticker(position_symbol(p)).get("last"))
    activation=entry+risk_distance*TRAIL_ACTIVATE_R if side=="long" else entry-risk_distance*TRAIL_ACTIVATE_R
    if (side=="long" and current<activation) or (side=="short" and current>activation): return False
    callback=max(TRAIL_MIN_CALLBACK,min(TRAIL_MAX_CALLBACK,atr_pct*0.90))
    inst=p["instId"]; payload={"instId":inst,"marginMode":position_margin_mode(p),"positionSide":str(p.get("positionSide","net")).lower(),"side":"sell" if side=="long" else "buy","orderType":"trailing_stop","size":"-1","callbackRatio":f"{callback:.3f}","reduceOnly":"true","clientOrderId":f"tr{int(time.time())%100000000}"}
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    pid=position_id(p)
    if pid:payload["positionId"]=pid
    try:
        resp=native("private_post_trade_order_algo",payload)
        if native_ok(resp):
            tg(f"🔒 TRAIL ARMED\n{position_symbol(p)} {side.upper()}\nActivation {activation:.6g}\nCallback {callback*100:.2f}%")
            return True
    except Exception:pass
    return False


def manage_positions(positions):
    if PAPER_TRADING or not positions:return
    for p in positions:
        try:
            symbol=position_symbol(p); entry=position_entry(p); side=position_side(p); last=safe_float(exchange.fetch_ticker(symbol).get("last"))
            if not entry or not last:continue
            ok,order=has_live_protection(p)
            if not ok:continue
            sl=safe_float(order.get("slTriggerPrice")); tp=safe_float(order.get("tpTriggerPrice"))
            if not sl:continue
            risk=abs(entry-sl); r=((last-entry)/risk) if side=="long" else ((entry-last)/risk)
            if r>=TRAIL_ACTIVATE_R:
                # Only arm one full-position trailing stop; inspect pending algo orders first.
                try:
                    pending=native("private_get_trade_orders_algo_pending",{"instId":p["instId"],"orderType":"trailing_stop","limit":"100"})
                    exists=any(str(x.get("state"))=="live" and str(x.get("positionId","") or "") in ("",position_id(p)) for x in (pending.get("data") or [])) if native_ok(pending) else False
                except Exception: exists=False
                if not exists:
                    atr_pct=abs(last-entry)/entry if entry else 0.01
                    place_trailing_stop(p,entry,risk,atr_pct)
        except Exception:
            continue


def repair_existing_positions():
    positions=get_positions(); ok_all=True
    for p in positions:
        symbol=position_symbol(p); entry=position_entry(p); side=position_side(p)
        if not entry:continue
        protected,_=has_live_protection(p)
        if protected:continue
        try:
            df=add_indicators(ohlcv_df(symbol,TIMEFRAME_SIGNAL)); atr=safe_float(df.iloc[-2].atr); tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001)
            levels=calculate_trade_levels(entry,atr,side,tick,TP_R_BASE)
            if not levels:ok_all=False;continue
            sl,tp,_,_=levels; last=safe_float(exchange.fetch_ticker(symbol).get("last"))
            if not tpsl_prices_valid(side,sl,tp,last):
                ok_all=False
                continue
            good,_=place_tpsl_for_position(p,sl,tp,"repair")
            ok_all=ok_all and good
            if good:tg(f"🛡️ REPAIRED {symbol} {side.upper()}\nSL {sl}\nTP {tp}")
        except Exception as e:
            ok_all=False;tg(f"🚨 PROTECTION ERROR {symbol}\n{e}")
    return ok_all


def closed_positions_history(limit=100):
    out=[]
    for state in ("closed","liquidated","adl"):
        try:
            r=native("private_get_account_positions_history",{"state":state,"limit":str(limit)})
            if native_ok(r):out.extend(r.get("data") or [])
        except Exception:pass
    return out


def close_reason(r):
    raw=str(r.get("closeType",r.get("state",""))).lower()
    return {"tp":"TAKE PROFIT","take_profit":"TAKE PROFIT","sl":"STOP LOSS","stop_loss":"STOP LOSS","liquidated":"LIQUIDATION","liquidation":"LIQUIDATION","adl":"ADL","partial_liquidated":"PARTIAL LIQ","partially_closed":"PARTIAL CLOSE","manual":"MANUAL","mannual":"MANUAL","close":"CLOSED"}.get(raw,raw.upper() or "CLOSED")


def infer_side(r):
    s=str(r.get("positionSide","net")).lower()
    if s in ("long","short"):return s
    side=str(r.get("side","")).lower()
    if side=="sell":return "long"
    if side=="buy":return "short"
    return "net"


def fmt_duration(a,b):
    a=safe_float(a);b=safe_float(b)
    if a<=0 or b<=0 or b<a:return "unknown"
    sec=int((b-a)/1000);h,rem=divmod(sec,3600);m,s=divmod(rem,60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


daily={"date":None,"start_equity":None,"trades":0,"wins":0,"losses":0,"net":0.0,"long_trades":0,"long_wins":0,"long_net":0.0,"short_trades":0,"short_wins":0,"short_net":0.0}
seen_closed=set(); symbol_last_trade={}; symbol_last_result={}; symbol_loss_streak={}; consecutive_losses=0; loss_pause_until=0; protection_lock=False; last_score_report=0


def reset_daily_if_needed(eq=None):
    today=amsterdam_today()
    if daily["date"]!=today:
        daily.update(date=today,start_equity=eq,trades=0,wins=0,losses=0,net=0.0,long_trades=0,long_wins=0,long_net=0.0,short_trades=0,short_wins=0,short_net=0.0);return True
    if daily["start_equity"] is None and eq is not None:daily["start_equity"]=eq
    return False


def notify_closed_trade(r):
    global consecutive_losses,loss_pause_until
    reset_daily_if_needed();symbol=str(r.get("instId","")).replace("-USDT","/USDT:USDT");side=infer_side(r)
    gross=safe_float(r.get("realizedPnl"));fee=abs(safe_float(r.get("fee")));net=gross-fee
    daily["trades"]+=1;daily["net"]+=net
    if side=="short":daily["short_trades"]+=1;daily["short_net"]+=net;daily["short_wins"]+=net>=0
    else:daily["long_trades"]+=1;daily["long_net"]+=net;daily["long_wins"]+=net>=0
    symbol_last_trade[symbol]=time.time()
    if net>=0:
        daily["wins"]+=1;consecutive_losses=0;symbol_last_result[symbol]="win";symbol_loss_streak[symbol]=0
    else:
        daily["losses"]+=1;consecutive_losses+=1;symbol_last_result[symbol]="loss";symbol_loss_streak[symbol]=symbol_loss_streak.get(symbol,0)+1
        if consecutive_losses>=MAX_CONSECUTIVE_LOSSES:loss_pause_until=time.time()+LOSS_PAUSE_MIN*60
    tg(f"{'🟢' if net>=0 else '🔴'} CLOSED\n{symbol} {side.upper()}\nNet ${net:+.2f}\nReason {close_reason(r)}\nToday ${daily['net']:+.2f} | {daily['wins']}W/{daily['losses']}L")


def seed_closed_history():
    reset_daily_if_needed()
    for r in closed_positions_history(100):
        hid=str(r.get("historyId",""))
        if hid:seen_closed.add(hid)


def check_for_closed_positions():
    for r in reversed(closed_positions_history(100)):
        hid=str(r.get("historyId",""))
        if hid and hid not in seen_closed:
            seen_closed.add(hid);notify_closed_trade(r)
    if len(seen_closed)>2500:seen_closed.clear()


def check_daily_risk(eq):
    reset_daily_if_needed(eq);start=daily["start_equity"]
    if not start:return True,"OK",1.0
    dd=max(0,(start-eq)/start*100)
    if dd>=DAILY_LOSS_LIMIT_PCT:return False,f"Daily hard stop {dd:.2f}%",0
    factor=1.0 if dd<DAILY_SOFT_DD_PCT else 0.50
    return True,f"DD {dd:.2f}% risk x{factor:.2f}",factor


def cooldown_ok(symbol):
    last=symbol_last_trade.get(symbol,0);streak=symbol_loss_streak.get(symbol,0)
    need=REPEAT_LOSS_COOLDOWN_MIN*60 if streak>=2 else LOSS_COOLDOWN_MIN*60 if symbol_last_result.get(symbol)=="loss" else WIN_COOLDOWN_MIN*60
    return time.time()-last>=need


def count_alt_sides(pos):
    l=s=0
    for p in pos:
        if position_symbol(p) in MAJORS:continue
        if position_side(p)=="short":s+=1
        else:l+=1
    return l,s


def open_trade(sig,risk_factor):
    global protection_lock
    symbol,side=sig["symbol"],sig["side"];acc=futures_account();eq=acc["total_equity"];avail=acc["available"]
    if eq<MIN_EQUITY_USDT or avail<=0:return False
    tick=safe_float(get_instrument(symbol).get("tickSize"),0.0001);live=safe_float(exchange.fetch_ticker(symbol).get("last"),sig["price"])
    drift=(live-sig["price"])/sig["price"]
    if (side=="long" and drift>MAX_FILL_SLIP_PCT) or (side=="short" and drift<-MAX_FILL_SLIP_PCT):return False
    tp_r=adaptive_tp_r(sig);levels=calculate_trade_levels(live,sig["atr"],side,tick,tp_r)
    if not levels:return False
    sl,tp,risk_dist,_=levels;sizing=calculate_contracts(symbol,live,sl,eq,avail,tp_r,risk_factor)
    if not sizing:return False
    lev_ok,lev_err=(True,"") if PAPER_TRADING else set_leverage(symbol,LEVERAGE)
    if not lev_ok and not ("110006" in lev_err or "110019" in lev_err):return False
    if not PAPER_TRADING and not verify_leverage(symbol,LEVERAGE):
        tg(f"⛔ {symbol}: leverage {LEVERAGE}x could not be verified");return False
    if PAPER_TRADING:
        tg(f"🧪 PAPER {symbol} {side.upper()}\nScore {sig['score']:.0f} t{sig['tactics']}\nADX {sig['adx']:.0f} RS1h {sig['rel1h']*100:+.2f}%\nTP {tp_r:.2f}R\nSL {sl:.6g} TP {tp:.6g}")
        return True
    payload={"instId":blofin_inst_id(symbol),"marginMode":MARGIN_MODE,"positionSide":"net","side":"buy" if side=="long" else "sell","orderType":"market","size":str(sizing["contracts"]),"tpTriggerPrice":str(tp),"tpOrderPrice":"-1","tpTriggerPriceType":"mark","slTriggerPrice":str(sl),"slOrderPrice":"-1","slTriggerPriceType":"mark","clientOrderId":f"v70{int(time.time()*1000)%1000000000}"}
    if BROKER_ID:payload["brokerId"]=BROKER_ID
    resp=native("private_post_trade_order",payload)
    if not native_ok(resp):
        err=response_error(resp)
        if "152012" in err:protection_lock=True
        tg(f"❌ ORDER FAILED {symbol} {side.upper()}\n{err}");return False
    tg(f"🟢 V7.0 {symbol} {side.upper()}\nScore {sig['score']:.0f} t{sig['tactics']} | ADX {sig['adx']:.0f}\nRS1h {sig['rel1h']*100:+.2f}% | Book/funding checked\nEntry ~{live:.6g}\nSL {sl:.6g} | TP {tp:.6g} ({tp_r:.2f}R)\nNotional ${sizing['notional']:.2f} | Margin ${sizing['margin']:.2f} | {LEVERAGE}x")
    new=None;deadline=time.time()+8
    while time.time()<deadline:
        try:
            for p in get_positions():
                if position_symbol(p)==symbol:new=p;break
        except Exception:pass
        if new:break
        time.sleep(.5)
    if not new:return True
    protected,_=has_live_protection(new)
    if protected:return True
    last=safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side,sl,tp,last):
        good,res=place_tpsl_for_position(new,sl,tp,"fallback")
        if good:return True
        protection_lock=True;tg(f"🚨 PROTECTION FAILURE {symbol}\n{res}\nNEW TRADES PAUSED")
    return False


def startup():
    acc=futures_account();reset_daily_if_needed(acc["total_equity"])
    tg(f"🚀 BLOFIN V7.0\nMode {'PAPER' if PAPER_TRADING else 'LIVE'}\nEquity ${acc['total_equity']:.2f}\nAvailable ${acc['available']:.2f}\nScore {MIN_SCORE:.0f} | tactics {MIN_TACTICS}+\nMax positions {MAX_ACTIVE_POSITIONS}\nRisk/trade {MAX_RISK_EQUITY_PCT*100:.2f}%\nDaily hard stop {DAILY_LOSS_LIMIT_PCT:.1f}%\nTrailing {'ON' if TRAIL_ENABLE else 'OFF'}")


def maybe_score_report(rows,breadth):
    global last_score_report
    if time.time()-last_score_report<SCORE_REPORT_MIN*60:return
    last_score_report=time.time();ranked=sorted(rows,key=lambda x:x.get("score",0),reverse=True)[:8]
    lines=[f"📡 V7.0 {session_state()} {now_utc().strftime('%H:%M')} UTC | breadth L{breadth['bull']*100:.0f}/S{breadth['bear']*100:.0f}"]
    for r in ranked:lines.append(f"{r['symbol'].split('/')[0]} {str(r.get('side') or '-').upper()} {r.get('score',0):.0f} t{r.get('tactics',0)} ADX{r.get('adx',0):.0f} RS1h{r.get('rel1h',0)*100:+.1f}% {'READY' if not r.get('skip') else r.get('skip')}")
    tg("\n".join(lines))


def main():
    global protection_lock,loss_pause_until
    startup()
    if not PAPER_TRADING:
        seed_closed_history()
        try:protection_lock=not repair_existing_positions()
        except Exception as e:protection_lock=True;tg(f"🚨 STARTUP PROTECTION FAILED\n{e}")
    while True:
        try:
            if not PAPER_TRADING:
                try:check_for_closed_positions()
                except Exception:pass
            positions=get_positions()
            if not PAPER_TRADING:
                manage_positions(positions)
                unprotected=False
                for p in positions:
                    if has_live_protection(p)[0]:continue
                    unprotected=True
                protection_lock=unprotected
            acc=futures_account();ok,_,risk_factor=check_daily_risk(acc["total_equity"])
            if not ok or time.time()<loss_pause_until or len(positions)>=MAX_ACTIVE_POSITIONS or (protection_lock and not PAPER_TRADING):
                time.sleep(SCAN_SECONDS);continue
            btc=btc_regime();rows=[]
            for symbol in SYMBOLS:
                if not cooldown_ok(symbol):continue
                try:
                    x=evaluate_symbol(symbol,btc)
                    if x:rows.append(x)
                except Exception:continue
            breadth=market_breadth(rows)
            candidates=[]
            for x in rows:
                apply_breadth_filter(x,breadth)
                if not x.get("skip"):candidates.append(x)
            candidates.sort(key=lambda x:(x["score"],x["tactics"]),reverse=True)
            maybe_score_report(rows,breadth)
            selected=None
            for x in candidates[:FINALIST_COUNT]:
                if x["symbol"] in {position_symbol(p) for p in positions}:continue
                good,imb,fund=microstructure_ok(x)
                x["book_imbalance"]=imb;x["funding"]=fund
                if good:
                    selected=x;break
            if selected:
                cur=get_positions();
                if len(cur)<MAX_ACTIVE_POSITIONS:
                    al,as_=count_alt_sides(cur)
                    if selected["symbol"] in MAJORS or (selected["side"]=="long" and al<MAX_ALT_LONGS) or (selected["side"]=="short" and as_<MAX_ALT_SHORTS):
                        open_trade(selected,risk_factor);symbol_last_trade[selected["symbol"]]=time.time()
            time.sleep(SCAN_SECONDS)
        except KeyboardInterrupt:
            tg("🛑 V7.0 stopped.");raise
        except Exception as e:
            if "152002" not in str(e):tg(f"⚠️ V7.0 LOOP ERROR\n{type(e).__name__}: {e}")
            time.sleep(30)


if __name__=="__main__":main()
