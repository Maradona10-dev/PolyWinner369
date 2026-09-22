import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone, timedelta

import ccxt
import numpy as np
import pandas as pd
import requests

# ============================================================
# BLOFIN V6.5 - QUALITY GATES
# ============================================================
# Waarom V6.4 's nachts verloor (uit jouw Telegram-historie):
# - MIN_SCORE 55 liet TRX/SEI/HBAR/GALA/ONDO door zonder 5m-bevestiging.
# - Score 82 zei bijna niets: bijna elke long kreeg 82.
# - Symbol-cooldown 15 min + geen SL-lock = revenge trades (SEI 2x, TRX 4x, DOGE 2x).
# - ATR-stops op TRX van ~0.1% werden opgegeten door $0.08-$0.12 fee.
# - MAX_CONSECUTIVE_LOSSES stond in config maar werd nooit gebruikt.
# - 00:00-06:00 UTC (nacht NL) is chop; avondtrades (BTC/RUNE/TAO/RENDER) waren de echte edge.
# - 152002 repair-TPSL blokkeerde/spammed terwijl posities al een TP hadden.
#
# V6.5 hard gates (niet alleen punten):
# 1. 5m confirm verplicht.
# 2. Min 4 van 6 tactieken.
# 3. BTC-regime filter voor altcoins.
# 4. Sessiefilter: geen alts 00:00-06:00 UTC.
# 5. Min stop 0.45% zodat fee niet de trade is.
# 6. Cooldown 60 min na verlies, 20 min na winst.
# 7. 3 verliezen op rij = 90 min pauze.
# 8. Geen 2e alt-long als er al een alt-long openstaat.
# 9. Repair TPSL alleen als prijs nog tussen SL en TP zit.
# 10. Dagstatistiek op Europe/Amsterdam, niet Railway-UTC-reset midden in de nacht.
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
LOSS_PAUSE_MIN = int(os.getenv("LOSS_PAUSE_MIN", "90"))

MAX_ACTIVE_POSITIONS = int(os.getenv("MAX_ACTIVE_POSITIONS", "2"))
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
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.40"))
TP_R_MULT = float(os.getenv("TP_R_MULT", "1.80"))
MIN_STOP_PCT = float(os.getenv("MIN_STOP_PCT", "0.0045"))
MAX_STOP_PCT = float(os.getenv("MAX_STOP_PCT", "0.028"))
MIN_NET_TP_USDT = float(os.getenv("MIN_NET_TP_USDT", "0.40"))
EST_FEE_USDT = float(os.getenv("EST_FEE_USDT", "0.12"))

VOLUME_MULT = float(os.getenv("VOLUME_MULT", "1.20"))
DONCHIAN_LOOKBACK = 12
NEAR_SWEEP_ATR = 0.20

DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "5"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
MIN_EQUITY_USDT = float(os.getenv("MIN_EQUITY_USDT", "30"))
REPAIR_EXISTING_POSITIONS = os.getenv("REPAIR_EXISTING_POSITIONS", "true").lower() == "true"
SCORE_REPORT_MIN = int(os.getenv("SCORE_REPORT_MIN", "30"))

NIGHT_START_UTC = 0
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
    raw = resp.get("data") or []
    result = []
    for p in raw:
        pos_size = safe_float(p.get("positions", p.get("position", p.get("size", 0))))
        if abs(pos_size) <= 0:
            continue
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


def session_state():
    hour = now_utc().hour
    if NIGHT_START_UTC <= hour < NIGHT_END_UTC:
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
    bull = bool(t["ema20"] > t["ema50"])
    bear = bool(t["ema20"] < t["ema50"])
    impulse_up = bool(a["close"] > a["ema20"] and a["ema20"] >= a["ema50"])
    impulse_down = bool(a["close"] < a["ema20"] and a["ema20"] <= a["ema50"])
    data = {
        "bull": bull,
        "bear": bear,
        "impulse_up": impulse_up,
        "impulse_down": impulse_down,
        "mixed": not bull and not bear,
    }
    btc_cache["ts"] = now
    btc_cache["data"] = data
    return data


def evaluate_symbol(symbol, btc):
    sess = session_state()
    if sess == "NIGHT" and symbol not in MAJORS:
        return {
            "symbol": symbol, "side": None, "score": 0, "skip": "night-alts-blocked",
            "tactics": 0, "price": 0, "atr": 0,
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

    stack_long = t["ema20"] > t["ema50"] > t["ema200"]
    stack_short = t["ema20"] < t["ema50"] < t["ema200"]
    fast_long = t["ema20"] > t["ema50"]
    fast_short = t["ema20"] < t["ema50"]

    ema_long = a["ema20"] > a["ema50"] and price > a["ema20"]
    ema_short = a["ema20"] < a["ema50"] and price < a["ema20"]
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

    long_score = 0
    short_score = 0
    long_tactics = 0
    short_tactics = 0

    if stack_long:
        long_score += 18
        long_tactics += 1
    elif fast_long:
        long_score += 8
    if stack_short:
        short_score += 18
        short_tactics += 1
    elif fast_short:
        short_score += 8

    if ema_long:
        long_score += 12
        long_tactics += 1
    if ema_short:
        short_score += 12
        short_tactics += 1

    if volume_ok:
        long_score += 12
        short_score += 12
        long_tactics += 1
        short_tactics += 1

    if structure_long:
        long_score += 16 if sweep_long else 12
        long_tactics += 1
    if structure_short:
        short_score += 16 if sweep_short else 12
        short_tactics += 1

    if confirm_long:
        long_score += 18
        long_tactics += 1
    if confirm_short:
        short_score += 18
        short_tactics += 1

    if atr_quality:
        long_score += 6
        short_score += 6
        long_tactics += 1
        short_tactics += 1

    if long_score >= short_score:
        side, score, tactics = "long", long_score, long_tactics
        confirm, structure = confirm_long, structure_long
    else:
        side, score, tactics = "short", short_score, short_tactics
        confirm, structure = confirm_short, structure_short

    skip = None
    if sess == "SHOULDER":
        score -= 6
    if symbol not in MAJORS:
        if side == "long" and btc["impulse_down"]:
            skip = "btc-against-long"
        elif side == "short" and btc["impulse_up"]:
            skip = "btc-against-short"
        elif side == "long" and btc["bear"] and not btc["impulse_up"]:
            skip = "btc-bear-no-alt-long"
        elif side == "short" and btc["bull"] and not btc["impulse_down"]:
            skip = "btc-bull-no-alt-short"

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
        "confirm": bool(confirm),
        "structure": bool(structure),
        "volume": bool(volume_ok),
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


def calculate_contracts(symbol, entry, sl, equity, available):
    meta = get_instrument(symbol)
    contract_value = safe_float(meta.get("contractValue"))
    min_size = safe_float(meta.get("minSize"))
    lot_size = safe_float(meta.get("lotSize"))
    max_market_size = safe_float(meta.get("maxMarketSize"), 0)
    if contract_value <= 0 or min_size <= 0 or lot_size <= 0:
        raise RuntimeError(f"Bad contract metadata for {symbol}: {meta}")

    stop_pct = abs(entry - sl) / entry
    risk_budget = equity * MAX_RISK_EQUITY_PCT
    risk_notional = risk_budget / stop_pct if stop_pct > 0 else 0
    margin_cap = min(BASE_MARGIN_USDT, MAX_MARGIN_USDT, equity * MAX_MARGIN_EQUITY_PCT)
    target_notional = min(risk_notional, margin_cap * LEVERAGE)
    max_affordable_margin = max(0.0, available * AVAILABLE_MARGIN_BUFFER)
    target_notional = min(target_notional, max_affordable_margin * LEVERAGE)
    if target_notional <= 0:
        return None

    raw_contracts = target_notional / (contract_value * entry)
    contracts = floor_step(raw_contracts, lot_size)
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

    expected_tp_usdt = notional * TP_R_MULT * stop_pct
    if expected_tp_usdt - EST_FEE_USDT < MIN_NET_TP_USDT:
        return None

    return {
        "contracts": contracts,
        "contract_value": contract_value,
        "notional": notional,
        "margin": margin,
        "min_size": min_size,
        "lot_size": lot_size,
        "expected_tp_usdt": expected_tp_usdt,
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
        tp = safe_float(o.get("tpTriggerPrice"), 0)
        sl = safe_float(o.get("slTriggerPrice"), 0)
        if tp > 0 or sl > 0:
            return True, o
    return False, None


def tpsl_prices_valid(side, sl, tp, last):
    if last <= 0:
        return False
    if side == "long":
        return sl < last < tp
    return tp < last < sl


def place_tpsl_for_position(p, sl, tp, client_tag="v65"):
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
    tg(f"🛡️ V6.5 STARTUP\nOpen positions: {len(positions)}\nChecking TP/SL protection...")
    all_ok = True
    for p in positions:
        symbol = position_symbol(p)
        side = position_side(p)
        entry = position_entry(p)
        if not entry:
            tg(f"⚠️ {symbol}: geen entry, skip repair (geen lock).")
            continue
        protected, _ = has_live_protection(p)
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
                tg(f"⚠️ {symbol}: geen geldige repair levels.")
                all_ok = False
                continue
            sl, tp, _ = levels
            last = safe_float(exchange.fetch_ticker(symbol).get("last"))
            if not tpsl_prices_valid(side, sl, tp, last):
                tg(f"⚠️ {symbol}: prijs {fmt(last)} al buiten SL/TP, geen repair (voorkomt 152002).")
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


startup_equity = None
consecutive_losses = 0
loss_pause_until = 0
symbol_last_trade = {}
symbol_last_result = {}
protection_lock = False
seen_closed_history_ids = set()
daily_trade_date = None
daily_trades = 0
daily_wins = 0
daily_losses = 0
daily_net_pnl = 0.0
last_score_report = 0


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
        return False

    meta = get_instrument(symbol)
    tick = safe_float(meta.get("tickSize"), 0.0001)
    entry = signal["price"]
    levels = calculate_trade_levels(entry, signal["atr"], side, tick)
    if not levels:
        return False
    sl, tp, risk_distance = levels
    stop_pct = abs(entry - sl) / entry
    if stop_pct < MIN_STOP_PCT:
        return False

    sizing = calculate_contracts(symbol, entry, sl, equity, available)
    if not sizing:
        return False
    required_margin = sizing["margin"]
    if required_margin > available * AVAILABLE_MARGIN_BUFFER:
        return False

    before = get_positions()
    before_ids = {position_id(p) for p in before if position_id(p)}
    lev_ok, lev_err = set_leverage(symbol, LEVERAGE, "net")
    if not lev_ok and "110006" not in lev_err and "110019" not in lev_err:
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
        "clientOrderId": f"v65{int(time.time() * 1000) % 1000000000}",
    }
    if BROKER_ID:
        payload["brokerId"] = BROKER_ID

    if PAPER_TRADING:
        tg(
            f"🧪 PAPER ORDER\n{symbol} {side.upper()}\n"
            f"Score {signal['score']:.0f} tactics {signal['tactics']}/6\n"
            f"Entry ~ {entry:.6g}\nSL {sl:.6g}\nTP {tp:.6g}\n"
            f"Stop {stop_pct*100:.2f}%\nMargin ${required_margin:.2f}"
        )
        return True

    resp = native("private_post_trade_order", payload)
    if not native_ok(resp):
        err = response_error(resp)
        if "152012" in err:
            global protection_lock
            protection_lock = True
            tg(f"🛑 152012 broker/key lock\n{err}\nNieuwe trades gestopt.")
        else:
            tg(f"❌ LIVE ORDER FAILED\n{symbol} {side.upper()}\n{err}")
        return False

    tg(
        f"🟢 LIVE ORDER\n{symbol} {side.upper()}\n"
        f"Score {signal['score']:.0f} | tactics {signal['tactics']}/6\n"
        f"Entry ~ {entry:.6g}\nSL {sl:.6g}\nTP {tp:.6g}\n"
        f"Stop {stop_pct*100:.2f}% | Exp TP ${sizing.get('expected_tp_usdt', 0):.2f}\n"
        f"Contracts {sizing['contracts']}\nNotional ${sizing['notional']:.2f}\n"
        f"Margin ${required_margin:.2f}\nLeverage {LEVERAGE}x"
    )

    new_pos = find_new_position(symbol, before_ids, timeout=8)
    if new_pos is None:
        tg(f"⚠️ {symbol}: order geaccepteerd, positie nog niet zichtbaar.")
        return True
    protected, _ = has_live_protection(new_pos)
    if protected:
        tg(f"🛡️ PROTECTION CONFIRMED\n{symbol} {side.upper()}\nSL {sl:.6g}\nTP {tp:.6g}")
        return True
    last = safe_float(exchange.fetch_ticker(symbol).get("last"))
    if tpsl_prices_valid(side, sl, tp, last):
        ok, result = place_tpsl_for_position(new_pos, sl, tp, client_tag="fallback")
        if ok:
            tg(f"🛡️ FALLBACK PROTECTION CONFIRMED\n{symbol} {side.upper()}")
            return True
        tg(f"🚨 CRITICAL PROTECTION FAILURE\n{symbol}\n{result}\nNEW TRADES PAUSED.")
        return False
    tg(f"⚠️ {symbol}: fallback TPSL overgeslagen, prijs al buiten range.")
    return True


def reset_daily_stats_if_needed():
    global daily_trade_date, daily_trades, daily_wins, daily_losses, daily_net_pnl
    today = amsterdam_today()
    if daily_trade_date != today:
        daily_trade_date = today
        daily_trades = 0
        daily_wins = 0
        daily_losses = 0
        daily_net_pnl = 0.0


def closed_positions_history(limit=100):
    resp = native("private_get_account_positions_history", {"state": "closed", "limit": str(limit)})
    if not native_ok(resp):
        raise RuntimeError(f"Position history failed: {response_error(resp)}")
    return resp.get("data") or []


def close_reason(record):
    raw = str(record.get("closeType", record.get("state", ""))).lower()
    mapping = {
        "tp": "TAKE PROFIT", "take_profit": "TAKE PROFIT", "take-profit": "TAKE PROFIT",
        "sl": "STOP LOSS", "stop_loss": "STOP LOSS", "stop-loss": "STOP LOSS",
        "liquidated": "LIQUIDATION", "liquidation": "LIQUIDATION", "adl": "ADL",
        "mannual": "MANUAL / CLOSE", "manual": "MANUAL / CLOSE", "close": "CLOSED",
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


def notify_closed_trade(record):
    global daily_trades, daily_wins, daily_losses, daily_net_pnl
    global consecutive_losses, loss_pause_until
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
    daily_net_pnl += net_pnl
    if net_pnl >= 0:
        daily_wins += 1
        consecutive_losses = 0
        symbol_last_result[symbol] = "win"
    else:
        daily_losses += 1
        consecutive_losses += 1
        symbol_last_result[symbol] = "loss"
        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            loss_pause_until = time.time() + LOSS_PAUSE_MIN * 60

    symbol_last_trade[symbol] = time.time()
    icon = "🟢" if net_pnl >= 0 else "🔴"
    sign = "+" if net_pnl >= 0 else ""
    pause_note = ""
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        pause_note = f"\n⏸ {MAX_CONSECUTIVE_LOSSES} losses: pauze {LOSS_PAUSE_MIN} min"
    tg(
        f"{icon} POSITION CLOSED\n\n{symbol}\n{side}\n"
        f"━━━━━━━━━━━━\n"
        f"Entry: {entry:.8g}\nExit:  {exit_price:.8g}\n"
        f"Size:  {close_positions:g} contracts\n\n"
        f"💰 Realized PnL: {'+' if gross_pnl >= 0 else ''}${gross_pnl:.2f}\n"
        f"📊 PnL ratio: {pnl_ratio:+.2f}%\n"
        f"💸 Fee: -${fee:.2f}\n"
        f"━━━━━━━━━━━━\n"
        f"💵 Net PnL: {sign}${net_pnl:.2f}\n\n"
        f"🎯 Closed by: {reason}\n⏱ Duration: {duration}\n\n"
        f"📅 TODAY (Amsterdam)\n"
        f"Trades: {daily_trades} | Wins: {daily_wins} | Losses: {daily_losses}\n"
        f"Net PnL: {'+' if daily_net_pnl >= 0 else ''}${daily_net_pnl:.2f}"
        f"{pause_note}"
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
    if len(seen_closed_history_ids) > 2000:
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
    last = symbol_last_trade.get(symbol, 0)
    result = symbol_last_result.get(symbol)
    needed = LOSS_COOLDOWN_MIN * 60 if result == "loss" else WIN_COOLDOWN_MIN * 60
    return (time.time() - last) >= needed


def has_open_alt_long(positions):
    for p in positions:
        if position_symbol(p) in MAJORS:
            continue
        if position_side(p) == "long":
            return True
    return False


def startup_diagnostics():
    acc = futures_account()
    positions = get_positions()
    tg(
        f"🚀 BLOFIN V6.5 STARTED\n"
        f"Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"Equity: ${acc['total_equity']:.2f}\n"
        f"Available: ${acc['available']:.2f}\n"
        f"Positions: {len(positions)}/{MAX_ACTIVE_POSITIONS}\n"
        f"MIN_SCORE {MIN_SCORE:.0f} | tactics {MIN_TACTICS}/6\n"
        f"Stop min {MIN_STOP_PCT*100:.2f}% | SL {ATR_SL_MULT}x ATR\n"
        f"Margin ${BASE_MARGIN_USDT:.0f}/${MAX_MARGIN_USDT:.0f} | {LEVERAGE}x\n"
        f"Night block alts 00-06 UTC\n"
        f"Loss pause after {MAX_CONSECUTIVE_LOSSES} | daily cap {DAILY_LOSS_LIMIT_PCT:.0f}%"
    )


def maybe_score_report(rows):
    global last_score_report
    if time.time() - last_score_report < SCORE_REPORT_MIN * 60:
        return
    last_score_report = time.time()
    ranked = sorted(rows, key=lambda x: x.get("score", 0), reverse=True)[:8]
    if not ranked:
        tg(f"📡 V6.5 SCAN {session_state()}\ngeen geldige candles")
        return
    lines = [f"📡 V6.5 SCAN {session_state()} {now_utc().strftime('%H:%M')} UTC"]
    for r in ranked:
        mark = "READY" if not r.get("skip") else r.get("skip")
        lines.append(
            f"{r['symbol'].split('/')[0]} {str(r.get('side') or '-').upper()} "
            f"{r.get('score', 0):.0f} t{r.get('tactics', 0)} {mark}"
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
            tg(f"🚨 STARTUP PROTECTION CHECK FAILED\n{e}")
        if protection_lock:
            tg("🛑 V6.5 SAFE MODE\nNieuwe trades uit tot bescherming klopt.")

    while True:
        try:
            if not PAPER_TRADING:
                try:
                    check_for_closed_positions()
                except Exception as e:
                    tg(f"⚠️ PNL TRACKER ERROR\n{e}")

            ok, reason = check_daily_risk()
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
                    protected, _ = has_live_protection(p)
                    if protected:
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
                        repair_ok, err = place_tpsl_for_position(p, sl, tp, client_tag="watch")
                        if repair_ok:
                            unprotected = False
                            tg(f"🛡️ REPAIRED {symbol} {side.upper()}")
                        elif err and "152002" in str(err):
                            pass
                    except Exception:
                        pass
                protection_lock = unprotected and any(
                    not has_live_protection(p)[0] for p in get_positions()
                )

            if len(positions) >= MAX_ACTIVE_POSITIONS or (protection_lock and not PAPER_TRADING):
                time.sleep(SCAN_SECONDS)
                continue

            try:
                btc = btc_regime()
            except Exception:
                time.sleep(SCAN_SECONDS)
                continue

            scanned = []
            candidates = []
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
            alt_long_open = has_open_alt_long(positions)

            for signal in candidates:
                current = get_positions()
                if len(current) >= MAX_ACTIVE_POSITIONS:
                    break
                open_symbols = {position_symbol(p) for p in current}
                if signal["symbol"] in open_symbols:
                    continue
                if signal["side"] == "long" and signal["symbol"] not in MAJORS and (
                    alt_long_open or has_open_alt_long(current)
                ):
                    continue
                traded = open_trade(signal)
                symbol_last_trade[signal["symbol"]] = time.time()
                break

            time.sleep(SCAN_SECONDS)
        except KeyboardInterrupt:
            tg("🛑 V6.5 stopped manually.")
            raise
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if "152002" in msg:
                time.sleep(20)
                continue
            tg(f"⚠️ V6.5 LOOP ERROR\n{msg}")
            time.sleep(30)


if __name__ == "__main__":
    main()
