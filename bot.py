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
# BLOFIN V6.8 ADAPTIVE MOMENTUM ENGINE
# ============================================================
#
# Verbeteringen t.o.v. V6.7:
# - SyntaxError clientOrderId opgelost
# - TP + SL moeten BEIDE aanwezig zijn
# - Directionele tick rounding voor SL/TP
# - Volume/ATR zijn quality factors, geen directional tactics
# - Multi-horizon relative strength: 15m / 45m / 90m
# - Persistent relative-strength tactic
# - Hard leverage failure: geen order bij leverage-fout
# - Protection-lock controleert ALLE posities opnieuw
# - Mislukte execution veroorzaakt geen cooldown
# - Maximaal 3 execution attempts per scan
# - Eén SUCCESVOLLE nieuwe trade per scan
# - Adaptive regime scoring
# - Momentum continuation
# - Gradual pullback scoring
# - Multi-factor 5m confirmation
# - Relative-strength failure detection
# - Entry-quality scoring
# - Second-chance continuation
# - Conservative learning
# - Broker-side TP/SL
# - Existing-position repair
# - Daily Amsterdam risk reset
# - LONG / SHORT statistics
# - Per-symbol loss streak
#
# Geschreven voor One-way / NET position mode.
# ============================================================


# ============================================================
# ENV
# ============================================================

API_KEY = os.getenv("EXCHANGE_API_KEY", "").strip()
API_SECRET = os.getenv("EXCHANGE_SECRET", "").strip()
API_PASSWORD = os.getenv("EXCHANGE_PASSWORD", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PAPER_TRADING = os.getenv(
    "PAPER_TRADING",
    "false"
).lower() == "true"

LIVE_CONFIRM = os.getenv("LIVE_CONFIRM", "")

BROKER_ID = os.getenv(
    "BLOFIN_BROKER_ID",
    ""
).strip()


# ============================================================
# TIMEFRAMES
# ============================================================

TIMEFRAME_SIGNAL = "15m"
TIMEFRAME_CONFIRM = "5m"
TIMEFRAME_TREND = "1h"

CANDLE_LIMIT = 240

SCAN_SECONDS = int(
    os.getenv("SCAN_SECONDS", "45")
)


# ============================================================
# COOLDOWNS
# ============================================================

WIN_COOLDOWN_MIN = int(
    os.getenv("WIN_COOLDOWN_MIN", "20")
)

LOSS_COOLDOWN_MIN = int(
    os.getenv("LOSS_COOLDOWN_MIN", "60")
)

REPEAT_LOSS_COOLDOWN_MIN = int(
    os.getenv(
        "REPEAT_LOSS_COOLDOWN_MIN",
        "180"
    )
)

LOSS_PAUSE_MIN = int(
    os.getenv("LOSS_PAUSE_MIN", "90")
)


# ============================================================
# POSITION LIMITS
# ============================================================

MAX_ACTIVE_POSITIONS = int(
    os.getenv("MAX_ACTIVE_POSITIONS", "2")
)

MAX_ALT_LONGS = int(
    os.getenv("MAX_ALT_LONGS", "1")
)

MAX_ALT_SHORTS = int(
    os.getenv("MAX_ALT_SHORTS", "1")
)

MAX_EXECUTION_ATTEMPTS_PER_SCAN = int(
    os.getenv(
        "MAX_EXECUTION_ATTEMPTS_PER_SCAN",
        "3"
    )
)


# ============================================================
# ADAPTIVE SCORE
# ============================================================

BASE_MIN_SCORE = float(
    os.getenv("MIN_SCORE", "68")
)

HARD_MIN_SCORE = float(
    os.getenv("HARD_MIN_SCORE", "62")
)

STRONG_REGIME_MIN_SCORE = float(
    os.getenv(
        "STRONG_REGIME_MIN_SCORE",
        "64"
    )
)

MIN_TACTICS = int(
    os.getenv("MIN_TACTICS", "4")
)

STRONG_MIN_TACTICS = int(
    os.getenv("STRONG_MIN_TACTICS", "4")
)


# ============================================================
# LEVERAGE / MARGIN
# ============================================================

LEVERAGE = int(
    os.getenv("LEVERAGE", "10")
)

MARGIN_MODE = os.getenv(
    "MARGIN_MODE",
    "isolated"
)

BASE_MARGIN_USDT = float(
    os.getenv("BASE_MARGIN_USDT", "10")
)

MAX_MARGIN_USDT = float(
    os.getenv("MAX_MARGIN_USDT", "12")
)

MAX_MARGIN_EQUITY_PCT = float(
    os.getenv(
        "MAX_MARGIN_EQUITY_PCT",
        "0.18"
    )
)

MAX_RISK_EQUITY_PCT = float(
    os.getenv(
        "MAX_RISK_EQUITY_PCT",
        "0.012"
    )
)

AVAILABLE_MARGIN_BUFFER = float(
    os.getenv(
        "AVAILABLE_MARGIN_BUFFER",
        "0.90"
    )
)


# ============================================================
# RISK / TP / SL
# ============================================================

ATR_PERIOD = 14
ADX_PERIOD = 14
RSI_PERIOD = 14

ATR_SL_MULT = float(
    os.getenv("ATR_SL_MULT", "1.40")
)

TP_R_MULT = float(
    os.getenv("TP_R_MULT", "1.80")
)

MIN_STOP_PCT = float(
    os.getenv("MIN_STOP_PCT", "0.0045")
)

MAX_STOP_PCT = float(
    os.getenv("MAX_STOP_PCT", "0.028")
)

MIN_NET_TP_USDT = float(
    os.getenv(
        "MIN_NET_TP_USDT",
        "0.40"
    )
)

TAKER_FEE_RATE = float(
    os.getenv(
        "TAKER_FEE_RATE",
        "0.0006"
    )
)

SLIPPAGE_RATE = float(
    os.getenv(
        "SLIPPAGE_RATE",
        "0.0003"
    )
)


# ============================================================
# MARKET FILTERS
# ============================================================

VOLUME_MULT = float(
    os.getenv("VOLUME_MULT", "1.20")
)

DONCHIAN_LOOKBACK = 12
NEAR_SWEEP_ATR = 0.20

ADX_TREND = float(
    os.getenv("ADX_TREND", "18")
)

ADX_CHOP = float(
    os.getenv("ADX_CHOP", "14")
)

ADX_STRONG = float(
    os.getenv("ADX_STRONG", "30")
)

ADX_VERY_STRONG = float(
    os.getenv("ADX_VERY_STRONG", "40")
)

RSI_LONG_MAX = float(
    os.getenv("RSI_LONG_MAX", "68")
)

RSI_SHORT_MIN = float(
    os.getenv("RSI_SHORT_MIN", "32")
)

RS_OVERRIDE = float(
    os.getenv("RS_OVERRIDE", "0.008")
)

RS_WEAK = float(
    os.getenv("RS_WEAK", "0.004")
)

MAX_EMA_EXT_PCT = 0.012
PULLBACK_ATR = 0.45
IMPULSE_15M_ATR = 1.30
IMPULSE_1H_ATR = 1.60
MAX_RUN_PCT = 0.035
MAX_FILL_SLIP_PCT = 0.004


# ============================================================
# DAILY RISK
# ============================================================

DAILY_LOSS_LIMIT_PCT = float(
    os.getenv(
        "DAILY_LOSS_LIMIT_PCT",
        "5"
    )
)

MAX_CONSECUTIVE_LOSSES = int(
    os.getenv(
        "MAX_CONSECUTIVE_LOSSES",
        "3"
    )
)

MIN_EQUITY_USDT = float(
    os.getenv("MIN_EQUITY_USDT", "30")
)


# ============================================================
# PROTECTION
# ============================================================

REPAIR_EXISTING_POSITIONS = (
    os.getenv(
        "REPAIR_EXISTING_POSITIONS",
        "true"
    ).lower() == "true"
)


# ============================================================
# REPORTING
# ============================================================

SCORE_REPORT_MIN = int(
    os.getenv("SCORE_REPORT_MIN", "30")
)


# ============================================================
# LEARNING
# ============================================================

LEARNING_FILE = os.getenv(
    "LEARNING_FILE",
    "v68_learning.json"
)

LEARNING_MIN_TRADES = int(
    os.getenv("LEARNING_MIN_TRADES", "12")
)

LEARNING_MAX_BONUS = float(
    os.getenv(
        "LEARNING_MAX_BONUS",
        "3.0"
    )
)

LEARNING_MAX_PENALTY = float(
    os.getenv(
        "LEARNING_MAX_PENALTY",
        "-3.0"
    )
)


# ============================================================
# SESSION
# ============================================================

NIGHT_END_UTC = 6
CORE_START_UTC = 7
CORE_END_UTC = 21


# ============================================================
# SYMBOLS
# ============================================================

BTC_SYMBOL = "BTC/USDT:USDT"

MAJORS = {
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
}

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "DOGE/USDT:USDT",
    "ADA/USDT:USDT",
    "TRX/USDT:USDT",
    "TON/USDT:USDT",
    "LTC/USDT:USDT",
    "BCH/USDT:USDT",
    "AVAX/USDT:USDT",
    "LINK/USDT:USDT",
    "DOT/USDT:USDT",
    "SUI/USDT:USDT",
    "NEAR/USDT:USDT",
    "APT/USDT:USDT",
    "ARB/USDT:USDT",
    "OP/USDT:USDT",
    "INJ/USDT:USDT",
    "SEI/USDT:USDT",
    "TIA/USDT:USDT",
    "ATOM/USDT:USDT",
    "HBAR/USDT:USDT",
    "FIL/USDT:USDT",
    "STX/USDT:USDT",
    "IMX/USDT:USDT",
    "STRK/USDT:USDT",
    "POL/USDT:USDT",
    "UNI/USDT:USDT",
    "AAVE/USDT:USDT",
    "LDO/USDT:USDT",
    "ONDO/USDT:USDT",
    "ENA/USDT:USDT",
    "CRV/USDT:USDT",
    "MKR/USDT:USDT",
    "RUNE/USDT:USDT",
    "RENDER/USDT:USDT",
    "FET/USDT:USDT",
    "TAO/USDT:USDT",
    "WLD/USDT:USDT",
    "JUP/USDT:USDT",
    "PEPE/USDT:USDT",
    "WIF/USDT:USDT",
    "BONK/USDT:USDT",
    "FLOKI/USDT:USDT",
    "SHIB/USDT:USDT",
    "ORDI/USDT:USDT",
    "GALA/USDT:USDT",
    "ETC/USDT:USDT",
    "XLM/USDT:USDT",
    "ALGO/USDT:USDT",
    "SAND/USDT:USDT",
    "APE/USDT:USDT",
]


# ============================================================
# SAFETY
# ============================================================

if not API_KEY or not API_SECRET or not API_PASSWORD:
    raise RuntimeError(
        "Missing EXCHANGE_API_KEY / "
        "EXCHANGE_SECRET / EXCHANGE_PASSWORD"
    )

if (
    not PAPER_TRADING
    and LIVE_CONFIRM
    != "YES_I_WANT_LIVE_TRADING"
):
    raise RuntimeError(
        "LIVE TRADING BLOCKED. "
        "Set LIVE_CONFIRM="
        "YES_I_WANT_LIVE_TRADING"
    )


# ============================================================
# EXCHANGE
# ============================================================

exchange = ccxt.blofin({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "password": API_PASSWORD,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap"
    },
})

exchange.load_markets()

SYMBOLS = [
    s for s in SYMBOLS
    if s in exchange.markets
]

if not SYMBOLS:
    raise RuntimeError(
        "No configured symbols are available on BloFin."
    )


# ============================================================
# TELEGRAM
# ============================================================

def tg(message: str):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=10,
        )
    except Exception:
        pass


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def amsterdam_today():

    try:
        from zoneinfo import ZoneInfo

        return datetime.now(
            ZoneInfo("Europe/Amsterdam")
        ).date().isoformat()

    except Exception:
        return (
            datetime.now(timezone.utc)
            + timedelta(hours=2)
        ).date().isoformat()


def safe_float(v, default=0.0):

    try:
        if v is None or v == "":
            return default

        return float(v)

    except Exception:
        return default


def native(method_name, params=None):

    fn = getattr(
        exchange,
        method_name,
        None
    )

    if fn is None:
        raise RuntimeError(
            f"CCXT implicit endpoint missing: "
            f"{method_name}"
        )

    return fn(params or {})


def native_ok(resp):

    return (
        isinstance(resp, dict)
        and str(resp.get("code")) == "0"
    )


def response_error(resp):

    if not isinstance(resp, dict):
        return str(resp)

    code = str(
        resp.get("code", "?")
    )

    msg = str(
        resp.get("msg", "")
    )

    data = resp.get("data")

    if isinstance(data, dict):

        dcode = data.get("code")
        dmsg = data.get("msg")

        if dcode not in (
            None,
            "",
            "0",
        ):
            code = str(dcode)

        if dmsg:
            msg = str(dmsg)

    elif isinstance(data, list):

        for item in data:

            if not isinstance(item, dict):
                continue

            item_code = str(
                item.get("code")
            )

            if item_code not in (
                "0",
                "None",
                "",
            ):
                code = item_code
                msg = str(
                    item.get("msg")
                    or msg
                )
                break

    return f"{code} {msg}".strip()


def blofin_inst_id(symbol):

    return (
        symbol.split("/")[0]
        + "-USDT"
    )


def floor_step(value, step):

    v = Decimal(str(value))
    s = Decimal(str(step))

    if s <= 0:
        return float(v)

    return float(
        (
            v / s
        ).to_integral_value(
            rounding=ROUND_DOWN
        ) * s
    )


def ceil_step(value, step):

    v = Decimal(str(value))
    s = Decimal(str(step))

    if s <= 0:
        return float(v)

    return float(
        (
            v / s
        ).to_integral_value(
            rounding=ROUND_UP
        ) * s
    )


def round_trade_price(
    price,
    tick,
    side,
    kind
):

    if side == "long":

        if kind == "sl":
            return floor_step(
                price,
                tick
            )

        return ceil_step(
            price,
            tick
        )

    if kind == "sl":
        return ceil_step(
            price,
            tick
        )

    return floor_step(
        price,
        tick
    )


# ============================================================
# INSTRUMENT
# ============================================================

def get_instrument(symbol):

    inst = blofin_inst_id(symbol)

    resp = native(
        "public_get_market_instruments",
        {
            "instId": inst
        }
    )

    if not native_ok(resp):
        raise RuntimeError(
            f"Instrument lookup failed "
            f"{inst}: {response_error(resp)}"
        )

    data = resp.get("data") or []

    if not data:
        raise RuntimeError(
            f"No instrument metadata for {inst}"
        )

    return data[0]


# ============================================================
# ACCOUNT
# ============================================================

def futures_account():

    resp = native(
        "private_get_account_balance",
        {
            "productType": "USDT-FUTURES"
        }
    )

    if not native_ok(resp):
        raise RuntimeError(
            f"Balance API failed: "
            f"{response_error(resp)}"
        )

    data = resp.get("data") or {}

    total_equity = safe_float(
        data.get("totalEquity")
    )

    details = data.get("details") or []

    usdt = next(
        (
            d for d in details
            if d.get("currency") == "USDT"
        ),
        {},
    )

    balance = safe_float(
        usdt.get("balance")
    )

    available = safe_float(
        usdt.get("available")
    )

    equity = safe_float(
        usdt.get("equity")
    )

    if total_equity <= 0:
        total_equity = equity or balance

    return {
        "total_equity": total_equity,
        "equity": equity or total_equity,
        "balance": balance,
        "available": available,
    }


# ============================================================
# POSITIONS
# ============================================================

def get_positions():

    resp = native(
        "private_get_account_positions",
        {}
    )

    if not native_ok(resp):
        raise RuntimeError(
            f"Positions API failed: "
            f"{response_error(resp)}"
        )

    result = []

    for p in resp.get("data") or []:

        pos_size = safe_float(
            p.get(
                "positions",
                p.get(
                    "position",
                    p.get("size", 0)
                )
            )
        )

        if abs(pos_size) > 0:
            result.append(p)

    return result


def position_symbol(p):

    return str(
        p.get("instId", "")
    ).replace(
        "-USDT",
        "/USDT:USDT"
    )


def position_side(p):

    side = str(
        p.get(
            "positionSide",
            "net"
        )
    ).lower()

    if side in ("long", "short"):
        return side

    pos = safe_float(
        p.get(
            "positions",
            p.get(
                "position",
                p.get("size", 0)
            )
        )
    )

    return (
        "long"
        if pos > 0
        else "short"
    )


def position_entry(p):

    return safe_float(
        p.get(
            "averagePrice",
            p.get(
                "entryPrice",
                p.get(
                    "avgPrice",
                    0
                )
            )
        )
    )


def position_margin_mode(p):

    return str(
        p.get(
            "marginMode",
            MARGIN_MODE
        )
    ).lower()


def position_id(p):

    return str(
        p.get(
            "positionId",
            ""
        ) or ""
    )


# ============================================================
# MARKET DATA
# ============================================================

def ohlcv_df(symbol, timeframe):

    rows = exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=CANDLE_LIMIT
    )

    if len(rows) < 60:
        raise RuntimeError(
            f"Not enough candles for "
            f"{symbol} {timeframe}"
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    )

    for c in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:

        df[c] = pd.to_numeric(
            df[c],
            errors="coerce"
        )

    return (
        df.dropna()
        .reset_index(drop=True)
    )


# ============================================================
# INDICATORS
# ============================================================

def wilder_smooth(
    series,
    period
):

    return series.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


def add_indicators(df):

    out = df.copy()

    out["ema20"] = (
        out["close"]
        .ewm(
            span=20,
            adjust=False
        )
        .mean()
    )

    out["ema50"] = (
        out["close"]
        .ewm(
            span=50,
            adjust=False
        )
        .mean()
    )

    out["ema200"] = (
        out["close"]
        .ewm(
            span=200,
            adjust=False
        )
        .mean()
    )

    out["ema20_slope"] = (
        out["ema20"]
        - out["ema20"].shift(3)
    )

    out["ema50_slope"] = (
        out["ema50"]
        - out["ema50"].shift(3)
    )

    prev_close = out["close"].shift(1)

    tr = pd.concat(
        [
            out["high"] - out["low"],
            (
                out["high"]
                - prev_close
            ).abs(),
            (
                out["low"]
                - prev_close
            ).abs(),
        ],
        axis=1
    ).max(axis=1)

    out["atr"] = (
        tr.rolling(
            ATR_PERIOD
        ).mean()
    )

    # RSI
    up = (
        out["close"]
        .diff()
        .clip(lower=0)
    )

    down = (
        -out["close"]
        .diff()
        .clip(upper=0)
    )

    rs = (
        wilder_smooth(
            up,
            RSI_PERIOD
        )
        /
        wilder_smooth(
            down,
            RSI_PERIOD
        ).replace(
            0,
            np.nan
        )
    )

    out["rsi"] = (
        100
        -
        (
            100
            /
            (1 + rs)
        )
    )

    # ADX
    up_move = out["high"].diff()
    down_move = -out["low"].diff()

    plus_dm = np.where(
        (
            up_move > down_move
        )
        &
        (
            up_move > 0
        ),
        up_move,
        0.0
    )

    minus_dm = np.where(
        (
            down_move > up_move
        )
        &
        (
            down_move > 0
        ),
        down_move,
        0.0
    )

    atr_w = wilder_smooth(
        tr,
        ADX_PERIOD
    )

    plus_di = (
        100
        *
        wilder_smooth(
            pd.Series(
                plus_dm,
                index=out.index
            ),
            ADX_PERIOD
        )
        /
        atr_w.replace(
            0,
            np.nan
        )
    )

    minus_di = (
        100
        *
        wilder_smooth(
            pd.Series(
                minus_dm,
                index=out.index
            ),
            ADX_PERIOD
        )
        /
        atr_w.replace(
            0,
            np.nan
        )
    )

    dx = (
        100
        *
        (
            plus_di
            - minus_di
        ).abs()
        /
        (
            plus_di
            + minus_di
        ).replace(
            0,
            np.nan
        )
    )

    out["adx"] = wilder_smooth(
        dx,
        ADX_PERIOD
    )

    out["plus_di"] = plus_di
    out["minus_di"] = minus_di

    out["vol_sma20"] = (
        out["volume"]
        .rolling(20)
        .mean()
    )

    out["donchian_high"] = (
        out["high"]
        .rolling(
            DONCHIAN_LOOKBACK
        )
        .max()
        .shift(1)
    )

    out["donchian_low"] = (
        out["low"]
        .rolling(
            DONCHIAN_LOOKBACK
        )
        .min()
        .shift(1)
    )

    # Generic candle returns.
    # On 15m:
    # ret1 = 15m
    # ret3 = 45m
    # ret6 = 90m
    out["ret1"] = (
        out["close"]
        .pct_change(1)
    )

    out["ret3"] = (
        out["close"]
        .pct_change(3)
    )

    out["ret6"] = (
        out["close"]
        .pct_change(6)
    )

    return out


# ============================================================
# SESSION
# ============================================================

def session_state():

    hour = now_utc().hour

    if hour < NIGHT_END_UTC:
        return "NIGHT"

    if (
        CORE_START_UTC
        <= hour
        < CORE_END_UTC
    ):
        return "CORE"

    return "SHOULDER"


# ============================================================
# BTC REGIME
# ============================================================

btc_cache = {
    "ts": 0,
    "data": None,
}


def btc_regime():

    now = time.time()

    if (
        btc_cache["data"]
        and
        now - btc_cache["ts"] < 60
    ):
        return btc_cache["data"]

    df15 = add_indicators(
        ohlcv_df(
            BTC_SYMBOL,
            TIMEFRAME_SIGNAL
        )
    )

    df1h = add_indicators(
        ohlcv_df(
            BTC_SYMBOL,
            TIMEFRAME_TREND
        )
    )

    a = df15.iloc[-2]
    t = df1h.iloc[-2]

    data = {
        "bull": bool(
            t["ema20"] > t["ema50"]
        ),

        "bear": bool(
            t["ema20"] < t["ema50"]
        ),

        "stack_long": bool(
            t["ema20"]
            > t["ema50"]
            > t["ema200"]
        ),

        "stack_short": bool(
            t["ema20"]
            < t["ema50"]
            < t["ema200"]
        ),

        "impulse_up": bool(
            a["close"] > a["ema20"]
            and
            a["ema20"] >= a["ema50"]
        ),

        "impulse_down": bool(
            a["close"] < a["ema20"]
            and
            a["ema20"] <= a["ema50"]
        ),

        "ret15": safe_float(
            a["ret1"]
        ),

        "ret45": safe_float(
            a["ret3"]
        ),

        "ret90": safe_float(
            a["ret6"]
        ),

        "adx": safe_float(
            a["adx"]
        ),

        "close": safe_float(
            a["close"]
        ),
    }

    btc_cache["ts"] = now
    btc_cache["data"] = data

    return data


# ============================================================
# LEARNING
# ============================================================

learning = {
    "version": 2,
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "fingerprints": {},
    "symbols": {},
}


def load_learning():

    global learning

    if not os.path.exists(
        LEARNING_FILE
    ):
        return

    try:
        with open(
            LEARNING_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if isinstance(data, dict):
            learning.update(data)

    except Exception as e:
        tg(
            f"⚠️ Learning file ignored: "
            f"{e}"
        )


def save_learning():

    try:
        tmp = LEARNING_FILE + ".tmp"

        with open(
            tmp,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                learning,
                f,
                indent=2
            )

        os.replace(
            tmp,
            LEARNING_FILE
        )

    except Exception:
        pass


def fingerprint(signal):

    adx = safe_float(
        signal.get("adx")
    )

    if adx >= 40:
        adx_bucket = "very_strong"

    elif adx >= 30:
        adx_bucket = "strong"

    elif adx >= 22:
        adx_bucket = "trend"

    else:
        adx_bucket = "weak"

    structure = (
        "sweep"
        if signal.get("sweep")
        else
        "pullback"
        if signal.get(
            "pullback_quality",
            0
        ) >= 2
        else
        "continuation"
    )

    return "|".join(
        [
            str(
                signal.get(
                    "side",
                    "?"
                )
            ),
            adx_bucket,
            structure,
            str(
                signal.get(
                    "btc_regime",
                    "neutral"
                )
            ),
            str(
                signal.get(
                    "session",
                    "?"
                )
            ),
        ]
    )


def learning_adjustment(signal):

    if (
        learning.get("trades", 0)
        < LEARNING_MIN_TRADES
    ):
        return 0.0

    fp = fingerprint(signal)

    rec = (
        learning
        .get(
            "fingerprints",
            {}
        )
        .get(fp)
    )

    if not rec:
        return 0.0

    trades = int(
        rec.get(
            "trades",
            0
        )
    )

    if trades < 5:
        return 0.0

    wins = int(
        rec.get(
            "wins",
            0
        )
    )

    net = safe_float(
        rec.get(
            "net",
            0
        )
    )

    wr = wins / trades

    if wr >= 0.70 and net > 0:
        return min(
            LEARNING_MAX_BONUS,
            2.0
        )

    if wr <= 0.35 and net < 0:
        return max(
            LEARNING_MAX_PENALTY,
            -2.0
        )

    return 0.0


# ============================================================
# ADAPTIVE THRESHOLD
# ============================================================

def adaptive_threshold(signal):

    threshold = BASE_MIN_SCORE

    adx = safe_float(
        signal.get("adx")
    )

    confirm = int(
        signal.get(
            "confirm_components",
            0
        )
    )

    momentum = safe_float(
        signal.get(
            "momentum_quality",
            0
        )
    )

    entry_quality = safe_float(
        signal.get(
            "entry_quality",
            0
        )
    )

    if (
        adx >= ADX_VERY_STRONG
        and confirm >= 2
        and momentum >= 2
        and entry_quality >= 2
    ):
        threshold = min(
            threshold,
            STRONG_REGIME_MIN_SCORE
        )

    elif (
        adx >= ADX_STRONG
        and confirm >= 3
        and momentum >= 2
    ):
        threshold = max(
            HARD_MIN_SCORE,
            threshold - 2
        )

    threshold -= learning_adjustment(
        signal
    )

    return max(
        HARD_MIN_SCORE,
        threshold
    )


# ============================================================
# EVALUATE SYMBOL
# ============================================================

def evaluate_symbol(symbol, btc):

    sess = session_state()

    if (
        sess == "NIGHT"
        and symbol not in MAJORS
    ):
        return {
            "symbol": symbol,
            "side": None,
            "score": 0,
            "tactics": 0,
            "threshold": BASE_MIN_SCORE,
            "skip": "night-alts",
            "price": 0,
            "atr": 0,
            "entry_quality": 0,
            "confirm_components": 0,
            "adx": 0,
            "rel": 0,
        }

    df15 = add_indicators(
        ohlcv_df(
            symbol,
            TIMEFRAME_SIGNAL
        )
    )

    df5 = add_indicators(
        ohlcv_df(
            symbol,
            TIMEFRAME_CONFIRM
        )
    )

    df1h = add_indicators(
        ohlcv_df(
            symbol,
            TIMEFRAME_TREND
        )
    )

    a = df15.iloc[-2]
    a_prev = df15.iloc[-3]

    c = df5.iloc[-2]
    c_prev = df5.iloc[-3]

    t = df1h.iloc[-2]

    price = safe_float(
        a["close"]
    )

    atr = safe_float(
        a["atr"]
    )

    if (
        price <= 0
        or
        not np.isfinite(atr)
        or atr <= 0
    ):
        return None

    atr_pct = atr / price

    rsi = safe_float(
        a["rsi"],
        50
    )

    adx = safe_float(
        a["adx"]
    )

    # --------------------------------------------------------
    # MULTI-HORIZON RELATIVE STRENGTH
    # --------------------------------------------------------

    coin_ret15 = safe_float(
        a["ret1"]
    )

    coin_ret45 = safe_float(
        a["ret3"]
    )

    coin_ret90 = safe_float(
        a["ret6"]
    )

    rel15 = (
        coin_ret15
        - btc["ret15"]
    )

    rel45 = (
        coin_ret45
        - btc["ret45"]
    )

    rel90 = (
        coin_ret90
        - btc["ret90"]
    )

    rel = (
        rel15 * 0.50
        + rel45 * 0.30
        + rel90 * 0.20
    )

    rs_persistent_long = (
        rel15 > 0
        and rel45 > 0
    )

    rs_persistent_short = (
        rel15 < 0
        and rel45 < 0
    )

    # --------------------------------------------------------
    # EMA
    # --------------------------------------------------------

    ema20_15 = safe_float(
        a["ema20"]
    )

    ema50_15 = safe_float(
        a["ema50"]
    )

    ema20_5 = safe_float(
        c["ema20"]
    )

    ema20_slope = safe_float(
        a["ema20_slope"]
    )

    ema20_slope_pct = (
        ema20_slope / price
    )

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    stack_long = (
        t["ema20"]
        > t["ema50"]
        > t["ema200"]
    )

    stack_short = (
        t["ema20"]
        < t["ema50"]
        < t["ema200"]
    )

    fast_long = (
        t["ema20"] > t["ema50"]
    )

    fast_short = (
        t["ema20"] < t["ema50"]
    )

    trend_long = (
        ema20_15 > ema50_15
        and ema20_slope > 0
    )

    trend_short = (
        ema20_15 < ema50_15
        and ema20_slope < 0
    )

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    volume_ok = False

    if pd.notna(
        a["vol_sma20"]
    ):
        volume_ok = (
            a["volume"]
            >
            a["vol_sma20"]
            * VOLUME_MULT
        )

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    dist_ema = (
        price - ema20_15
    ) / price

    abs_dist_ema = abs(
        dist_ema
    )

    near_ema = (
        abs_dist_ema
        <=
        PULLBACK_ATR
        * atr
        / price
    )

    touched_ema_long = (
        min(
            safe_float(a["low"]),
            safe_float(a_prev["low"])
        )
        <=
        ema20_15
        + PULLBACK_ATR * atr
    )

    touched_ema_short = (
        max(
            safe_float(a["high"]),
            safe_float(a_prev["high"])
        )
        >=
        ema20_15
        - PULLBACK_ATR * atr
    )

    pullback_long = (
        near_ema
        or touched_ema_long
    )

    pullback_short = (
        near_ema
        or touched_ema_short
    )

    # --------------------------------------------------------
    # EXTENSION
    # --------------------------------------------------------

    ext_long = (
        dist_ema
        > MAX_EMA_EXT_PCT
    )

    ext_short = (
        dist_ema
        < -MAX_EMA_EXT_PCT
    )

    # --------------------------------------------------------
    # IMPULSE
    # --------------------------------------------------------

    bar15_range = (
        safe_float(a["high"])
        - safe_float(a["low"])
    )

    bar1h_range = (
        safe_float(t["high"])
        - safe_float(t["low"])
    )

    atr1h = safe_float(
        t["atr"],
        atr
    )

    impulse_15 = (
        bar15_range
        >
        IMPULSE_15M_ATR * atr
    )

    impulse_1h = (
        bar1h_range
        >
        IMPULSE_1H_ATR * atr1h
    )

    # --------------------------------------------------------
    # RUN
    # --------------------------------------------------------

    run8 = 0.0

    if len(df15) >= 10:

        old_close = safe_float(
            df15["close"].iloc[-10]
        )

        if old_close > 0:
            run8 = (
                price / old_close
                - 1
            )

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    recent_low = (
        df15["low"]
        .iloc[
            -DONCHIAN_LOOKBACK - 3:
            -3
        ]
        .min()
    )

    recent_high = (
        df15["high"]
        .iloc[
            -DONCHIAN_LOOKBACK - 3:
            -3
        ]
        .max()
    )

    sweep_long = (
        safe_float(a_prev["low"])
        <
        recent_low
        - NEAR_SWEEP_ATR * atr
        and
        price > recent_low
    )

    sweep_short = (
        safe_float(a_prev["high"])
        >
        recent_high
        + NEAR_SWEEP_ATR * atr
        and
        price < recent_high
    )

    structure_long = (
        (
            pullback_long
            and trend_long
        )
        or sweep_long
    )

    structure_short = (
        (
            pullback_short
            and trend_short
        )
        or sweep_short
    )

    # --------------------------------------------------------
    # 5M CONFIRMATION
    # --------------------------------------------------------

    c_atr = safe_float(
        c["atr"],
        atr
    )

    bounce_5m = (
        safe_float(c["low"])
        <=
        ema20_5
        + 0.35 * c_atr
    )

    reject_5m = (
        safe_float(c["high"])
        >=
        ema20_5
        - 0.35 * c_atr
    )

    confirm_long_components = 0
    confirm_short_components = 0

    # EMA alignment
    if c["ema20"] > c["ema50"]:
        confirm_long_components += 1

    if c["ema20"] < c["ema50"]:
        confirm_short_components += 1

    # Price relative to EMA
    if c["close"] > c["ema20"]:
        confirm_long_components += 1

    if c["close"] < c["ema20"]:
        confirm_short_components += 1

    # Immediate momentum
    if c["close"] > c_prev["close"]:
        confirm_long_components += 1

    if c["close"] < c_prev["close"]:
        confirm_short_components += 1

    # Pullback/rejection
    if bounce_5m:
        confirm_long_components += 1

    if reject_5m:
        confirm_short_components += 1

    confirm_long = (
        confirm_long_components >= 3
    )

    confirm_short = (
        confirm_short_components >= 3
    )

    # --------------------------------------------------------
    # QUALITY / REGIME
    # --------------------------------------------------------

    atr_quality = (
        0.0018
        <= atr_pct
        <= 0.035
    )

    trending = (
        adx >= ADX_TREND
    )

    strong_trend = (
        adx >= ADX_STRONG
    )

    very_strong_trend = (
        adx >= ADX_VERY_STRONG
    )

    choppy = (
        adx < ADX_CHOP
    )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    momentum_quality_long = 0
    momentum_quality_short = 0

    if ema20_slope_pct > 0.0003:
        momentum_quality_long += 1

    if ema20_slope_pct < -0.0003:
        momentum_quality_short += 1

    if a["close"] > a_prev["close"]:
        momentum_quality_long += 1

    if a["close"] < a_prev["close"]:
        momentum_quality_short += 1

    if btc["ret15"] > 0:
        momentum_quality_long += 1

    if btc["ret15"] < 0:
        momentum_quality_short += 1

    # --------------------------------------------------------
    # RS
    # --------------------------------------------------------

    rs_long = (
        rel > RS_WEAK
    )

    rs_strong_long = (
        rel > RS_OVERRIDE
    )

    rs_short = (
        rel < -RS_WEAK
    )

    rs_strong_short = (
        rel < -RS_OVERRIDE
    )

    rs_failure_short = (
        rel > 0
        and trend_short
        and confirm_short
        and rsi > RSI_SHORT_MIN
    )

    rs_failure_long = (
        rel < 0
        and trend_long
        and confirm_long
        and rsi < RSI_LONG_MAX
    )

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    long_score = 0.0
    short_score = 0.0

    long_tactics = 0
    short_tactics = 0

    # 1H trend
    if stack_long:
        long_score += 16
        long_tactics += 1

    elif fast_long:
        long_score += 7

    if stack_short:
        short_score += 16
        short_tactics += 1

    elif fast_short:
        short_score += 7

    # 15m directional trend
    if trend_long:
        long_score += 12
        long_tactics += 1

    if trend_short:
        short_score += 12
        short_tactics += 1

    # Volume = QUALITY ONLY
    if volume_ok:
        long_score += 6
        short_score += 6

    # Structure
    if structure_long:

        long_score += (
            17
            if sweep_long
            else 11
        )

        long_tactics += 1

    if structure_short:

        short_score += (
            17
            if sweep_short
            else 11
        )

        short_tactics += 1

    # 5m directional confirmation
    if confirm_long:

        long_score += (
            15
            if confirm_long_components >= 4
            else 11
        )

        long_tactics += 1

    if confirm_short:

        short_score += (
            15
            if confirm_short_components >= 4
            else 11
        )

        short_tactics += 1

    # ATR = QUALITY ONLY
    if atr_quality:
        long_score += 4
        short_score += 4

    # ADX = quality/regime
    if trending:
        long_score += 5
        short_score += 5

    if strong_trend:
        long_score += 3
        short_score += 3

    if very_strong_trend:
        long_score += 3
        short_score += 3

    # Directional momentum
    if momentum_quality_long >= 2:
        long_score += 4
        long_tactics += 1

    if momentum_quality_short >= 2:
        short_score += 4
        short_tactics += 1

    # Persistent relative strength
    if rs_strong_long:
        long_score += 6
        long_tactics += 1

    elif (
        rs_long
        and rs_persistent_long
    ):
        long_score += 3
        long_tactics += 1

    if rs_strong_short:
        short_score += 6
        short_tactics += 1

    elif (
        rs_short
        and rs_persistent_short
    ):
        short_score += 3
        short_tactics += 1

    # RS failure
    if rs_failure_short:
        short_score += 5
        short_tactics += 1

    if rs_failure_long:
        long_score += 5
        long_tactics += 1

    # --------------------------------------------------------
    # SIDE
    # --------------------------------------------------------

    if long_score >= short_score:

        side = "long"
        score = long_score
        tactics = long_tactics

        confirm_components = (
            confirm_long_components
        )

        structure = structure_long

        momentum_quality = (
            momentum_quality_long
        )

        pullback_quality = (
            3
            if near_ema
            else
            2
            if touched_ema_long
            else 0
        )

        sweep = sweep_long

    else:

        side = "short"
        score = short_score
        tactics = short_tactics

        confirm_components = (
            confirm_short_components
        )

        structure = structure_short

        momentum_quality = (
            momentum_quality_short
        )

        pullback_quality = (
            3
            if near_ema
            else
            2
            if touched_ema_short
            else 0
        )

        sweep = sweep_short

    # --------------------------------------------------------
    # ENTRY QUALITY
    # --------------------------------------------------------

    entry_quality = 0

    if pullback_quality >= 3:
        entry_quality += 2

    elif pullback_quality >= 2:
        entry_quality += 1

    side_extended = (
        ext_long
        if side == "long"
        else ext_short
    )

    if not side_extended:
        entry_quality += 1

    if not (
        impulse_15
        or impulse_1h
    ):
        entry_quality += 1

    if abs_dist_ema < 0.008:
        entry_quality += 1

    # --------------------------------------------------------
    # BTC LABEL
    # --------------------------------------------------------

    if btc["stack_long"]:
        btc_regime_label = "BTC_BULL"

    elif btc["stack_short"]:
        btc_regime_label = "BTC_BEAR"

    elif btc["bull"]:
        btc_regime_label = "BTC_UP"

    elif btc["bear"]:
        btc_regime_label = "BTC_DOWN"

    else:
        btc_regime_label = "BTC_NEUTRAL"

    preliminary = {
        "side": side,
        "adx": adx,
        "confirm_components": confirm_components,
        "momentum_quality": momentum_quality,
        "entry_quality": entry_quality,
        "pullback_quality": pullback_quality,
        "sweep": sweep,
        "btc_regime": btc_regime_label,
        "session": sess,
    }

    threshold = adaptive_threshold(
        preliminary
    )

    skip_reasons = []

    # Shoulder = slightly harder
    if sess == "SHOULDER":
        score -= 4

    if choppy:
        skip_reasons.append(
            "adx-chop"
        )

    # Also require the trend threshold.
    if not trending:
        skip_reasons.append(
            f"adx-{adx:.0f}<{ADX_TREND:.0f}"
        )

    # RSI
    if (
        side == "long"
        and rsi >= RSI_LONG_MAX
    ):
        if (
            not very_strong_trend
            or entry_quality < 2
        ):
            skip_reasons.append(
                "rsi-overbought"
            )

    if (
        side == "short"
        and rsi <= RSI_SHORT_MIN
    ):
        if (
            not very_strong_trend
            or entry_quality < 2
        ):
            skip_reasons.append(
                "rsi-oversold"
            )

    # Extension
    if side == "long":

        if (
            ext_long
            and not (
                very_strong_trend
                and
                confirm_long_components >= 4
                and
                pullback_quality >= 2
            )
        ):
            skip_reasons.append(
                "extended-long"
            )

    else:

        if (
            ext_short
            and not (
                very_strong_trend
                and
                confirm_short_components >= 4
                and
                pullback_quality >= 2
            )
        ):
            skip_reasons.append(
                "extended-short"
            )

    # Impulse chase
    if (
        impulse_15
        or impulse_1h
    ):

        if not (
            very_strong_trend
            and
            pullback_quality >= 2
            and
            confirm_components >= 3
        ):
            skip_reasons.append(
                "impulse-chase"
            )

    # Extended run
    if (
        side == "long"
        and run8 > MAX_RUN_PCT
    ):

        if not (
            very_strong_trend
            and pullback_quality >= 2
        ):
            skip_reasons.append(
                "extended-run"
            )

    if (
        side == "short"
        and run8 < -MAX_RUN_PCT
    ):

        if not (
            very_strong_trend
            and pullback_quality >= 2
        ):
            skip_reasons.append(
                "extended-run"
            )

    # BTC direction
    if symbol not in MAJORS:

        if side == "long":

            if btc["impulse_down"]:

                if not rs_failure_long:
                    skip_reasons.append(
                        "btc-against-long"
                    )

            elif btc["bear"]:

                if not rs_strong_long:
                    skip_reasons.append(
                        "btc-bear"
                    )

        else:

            if btc["impulse_up"]:

                if not rs_failure_short:
                    skip_reasons.append(
                        "btc-against-short"
                    )

            elif btc["bull"]:

                if not (
                    rs_strong_short
                    or rs_failure_short
                ):
                    skip_reasons.append(
                        "btc-bull-weak-short"
                    )

    # 5m confirmation
    if confirm_components < 3:

        strong_override = (
            very_strong_trend
            and
            momentum_quality >= 2
            and
            structure
            and
            pullback_quality >= 2
            and
            entry_quality >= 3
        )

        if not strong_override:
            skip_reasons.append(
                f"5m-{confirm_components}/4"
            )

    required_tactics = (
        STRONG_MIN_TACTICS
        if very_strong_trend
        else MIN_TACTICS
    )

    if tactics < required_tactics:
        skip_reasons.append(
            f"tactics-"
            f"{tactics}/"
            f"{required_tactics}"
        )

    if score < threshold:
        skip_reasons.append(
            f"score-"
            f"{score:.0f}<"
            f"{threshold:.0f}"
        )

    if not atr_quality:
        skip_reasons.append(
            "atr-band"
        )

    # --------------------------------------------------------
    # SECOND CHANCE
    # --------------------------------------------------------

    second_chance = False

    if (
        score >= HARD_MIN_SCORE
        and
        score < threshold
        and
        adx >= ADX_STRONG
        and
        momentum_quality >= 2
        and
        structure
        and
        confirm_components >= 3
        and
        entry_quality >= 3
        and
        pullback_quality >= 2
        and
        not impulse_15
    ):
        second_chance = True

        skip_reasons = [
            x for x in skip_reasons
            if not x.startswith(
                "score-"
            )
        ]

    # Valid RS failures can override corresponding BTC conflict.
    if (
        side == "short"
        and rs_failure_short
    ):
        skip_reasons = [
            x for x in skip_reasons
            if x != "btc-bull-weak-short"
        ]

    if (
        side == "long"
        and rs_failure_long
    ):
        skip_reasons = [
            x for x in skip_reasons
            if x != "btc-bear"
        ]

    final_skip = (
        skip_reasons[0]
        if skip_reasons
        else None
    )

    learning_bonus = (
        learning_adjustment(
            preliminary
        )
    )

    return {
        "symbol": symbol,
        "side": side,
        "score": float(score),
        "tactics": tactics,
        "threshold": threshold,
        "second_chance": second_chance,
        "price": price,
        "atr": atr,
        "atr_pct": atr_pct,
        "rsi": rsi,
        "adx": adx,

        "rel": rel,
        "rel15": rel15,
        "rel45": rel45,
        "rel90": rel90,

        "confirm": (
            confirm_components >= 3
        ),

        "confirm_components":
            confirm_components,

        "structure":
            bool(structure),

        "pullback_quality":
            pullback_quality,

        "entry_quality":
            entry_quality,

        "momentum_quality":
            momentum_quality,

        "sweep":
            bool(sweep),

        "rs_failure": (
            rs_failure_short
            if side == "short"
            else rs_failure_long
        ),

        "learning_bonus":
            learning_bonus,

        "skip":
            final_skip,

        "skip_reasons":
            skip_reasons,

        "session":
            sess,

        "btc_regime":
            btc_regime_label,

        "run8":
            run8,

        "volume_ok":
            bool(volume_ok),

        "timestamp":
            time.time(),
    }


# ============================================================
# TRADE LEVELS
# ============================================================

def calculate_trade_levels(
    entry,
    atr,
    side,
    tick
):

    risk_distance = max(
        atr * ATR_SL_MULT,
        entry * MIN_STOP_PCT
    )

    risk_distance = min(
        risk_distance,
        entry * MAX_STOP_PCT
    )

    if side == "long":

        raw_sl = (
            entry - risk_distance
        )

        raw_tp = (
            entry
            + risk_distance * TP_R_MULT
        )

    else:

        raw_sl = (
            entry + risk_distance
        )

        raw_tp = (
            entry
            - risk_distance * TP_R_MULT
        )

    sl = round_trade_price(
        raw_sl,
        tick,
        side,
        "sl"
    )

    tp = round_trade_price(
        raw_tp,
        tick,
        side,
        "tp"
    )

    if side == "long":

        if not (
            sl < entry < tp
        ):
            return None

    else:

        if not (
            tp < entry < sl
        ):
            return None

    actual_risk_distance = abs(
        entry - sl
    )

    return (
        sl,
        tp,
        actual_risk_distance,
    )


# ============================================================
# COST
# ============================================================

def roundtrip_cost(notional):

    return (
        notional
        *
        (
            TAKER_FEE_RATE * 2
            + SLIPPAGE_RATE
        )
    )


# ============================================================
# SIZING
# ============================================================

def calculate_contracts(
    symbol,
    entry,
    sl,
    equity,
    available
):

    meta = get_instrument(symbol)

    contract_value = safe_float(
        meta.get("contractValue")
    )

    min_size = safe_float(
        meta.get("minSize")
    )

    lot_size = safe_float(
        meta.get("lotSize")
    )

    max_market_size = safe_float(
        meta.get(
            "maxMarketSize",
            0
        )
    )

    if (
        contract_value <= 0
        or min_size <= 0
        or lot_size <= 0
    ):
        raise RuntimeError(
            f"Bad contract metadata "
            f"for {symbol}"
        )

    stop_pct = (
        abs(entry - sl)
        / entry
    )

    risk_budget = (
        equity
        * MAX_RISK_EQUITY_PCT
    )

    risk_notional = (
        risk_budget / stop_pct
        if stop_pct > 0
        else 0
    )

    margin_cap = min(
        BASE_MARGIN_USDT,
        MAX_MARGIN_USDT,
        equity
        * MAX_MARGIN_EQUITY_PCT
    )

    target_notional = min(
        risk_notional,
        margin_cap * LEVERAGE
    )

    max_affordable_margin = max(
        0.0,
        available
        * AVAILABLE_MARGIN_BUFFER
    )

    target_notional = min(
        target_notional,
        max_affordable_margin
        * LEVERAGE
    )

    if target_notional <= 0:
        return None

    contracts = floor_step(
        target_notional
        /
        (
            contract_value
            * entry
        ),
        lot_size
    )

    if contracts < min_size:
        contracts = min_size

    if max_market_size > 0:

        contracts = min(
            contracts,
            floor_step(
                max_market_size,
                lot_size
            )
        )

    if contracts < min_size:
        return None

    notional = (
        contracts
        * contract_value
        * entry
    )

    margin = (
        notional
        / LEVERAGE
    )

    if (
        margin
        >
        max_affordable_margin
        + 1e-9
    ):

        contracts = floor_step(
            (
                max_affordable_margin
                * LEVERAGE
            )
            /
            (
                contract_value
                * entry
            ),
            lot_size
        )

        if contracts < min_size:
            return None

        notional = (
            contracts
            * contract_value
            * entry
        )

        margin = (
            notional
            / LEVERAGE
        )

    if (
        margin
        >
        MAX_MARGIN_USDT
        + 1e-9
    ):

        contracts = floor_step(
            (
                MAX_MARGIN_USDT
                * LEVERAGE
            )
            /
            (
                contract_value
                * entry
            ),
            lot_size
        )

        if contracts < min_size:
            return None

        notional = (
            contracts
            * contract_value
            * entry
        )

        margin = (
            notional
            / LEVERAGE
        )

    expected_tp = (
        notional
        * TP_R_MULT
        * stop_pct
    )

    cost = roundtrip_cost(
        notional
    )

    if (
        expected_tp - cost
        <
        MIN_NET_TP_USDT
    ):
        return None

    return {
        "contracts": contracts,
        "contract_value": contract_value,
        "notional": notional,
        "margin": margin,
        "expected_tp_usdt":
            expected_tp,
        "roundtrip_cost": cost,
    }


# ============================================================
# TPSL
# ============================================================

def pending_tpsl(inst_id=None):

    params = {
        "limit": "100"
    }

    if inst_id:
        params["instId"] = inst_id

    resp = native(
        "private_get_trade_orders_tpsl_pending",
        params
    )

    if not native_ok(resp):
        raise RuntimeError(
            f"TPSL lookup failed: "
            f"{response_error(resp)}"
        )

    return (
        resp.get("data")
        or []
    )


def has_live_protection(p):
    """
    V6.8 safety fix:
    a position is protected ONLY if BOTH
    TP and SL are present on the same live/effective TPSL.
    """

    inst = p.get("instId")

    pos_side = str(
        p.get(
            "positionSide",
            "net"
        )
    ).lower()

    pid = position_id(p)

    try:
        orders = pending_tpsl(
            inst
        )
    except Exception:
        return False, None

    for o in orders:

        state = str(
            o.get(
                "state",
                ""
            )
        ).lower()

        if state not in (
            "live",
            "effective",
        ):
            continue

        if o.get("instId") != inst:
            continue

        order_side = str(
            o.get(
                "positionSide",
                "net"
            )
        ).lower()

        if order_side != pos_side:
            continue

        opid = str(
            o.get(
                "positionId",
                ""
            )
            or ""
        )

        if (
            pid
            and opid
            and opid != pid
        ):
            continue

        has_tp = (
            safe_float(
                o.get(
                    "tpTriggerPrice"
                )
            ) > 0
        )

        has_sl = (
            safe_float(
                o.get(
                    "slTriggerPrice"
                )
            ) > 0
        )

        if has_tp and has_sl:
            return True, o

    return False, None


def all_positions_protected():

    try:

        positions = get_positions()

        for p in positions:

            protected, _ = (
                has_live_protection(p)
            )

            if not protected:
                return False

        return True

    except Exception:
        return False


def tpsl_prices_valid(
    side,
    sl,
    tp,
    last
):

    if last <= 0:
        return False

    if side == "long":
        return (
            sl < last < tp
        )

    return (
        tp < last < sl
    )


def place_tpsl_for_position(
    p,
    sl,
    tp,
    client_tag="v68"
):

    inst = p["instId"]

    side = position_side(p)

    pside = str(
        p.get(
            "positionSide",
            "net"
        )
    ).lower()

    if pside not in (
        "net",
        "long",
        "short",
    ):
        pside = "net"

    payload = {
        "instId": inst,

        "marginMode":
            position_margin_mode(p),

        "positionSide":
            pside,

        "side": (
            "sell"
            if side == "long"
            else "buy"
        ),

        "tpTriggerPrice":
            str(tp),

        "tpOrderPrice":
            "-1",

        "tpTriggerPriceType":
            "mark",

        "slTriggerPrice":
            str(sl),

        "slOrderPrice":
            "-1",

        "slTriggerPriceType":
            "mark",

        "size":
            "-1",

        "reduceOnly":
            "true",

        "clientOrderId":
            f"{client_tag}"
            f"{int(time.time() * 1000) % 100000000}",
    }

    pid = position_id(p)

    if pid:
        payload["positionId"] = pid

    if BROKER_ID:
        payload["brokerId"] = BROKER_ID

    resp = native(
        "private_post_trade_order_tpsl",
        payload
    )

    if not native_ok(resp):
        return (
            False,
            response_error(resp)
        )

    time.sleep(0.8)

    ok, found = (
        has_live_protection(p)
    )

    if not ok:
        return (
            False,
            "TPSL accepted but "
            "both TP+SL not visible"
        )

    return True, found


# ============================================================
# REPAIR
# ============================================================

def repair_one_position(p):

    symbol = position_symbol(p)
    side = position_side(p)
    entry = position_entry(p)

    if entry <= 0:
        return False

    protected, _ = (
        has_live_protection(p)
    )

    if protected:
        return True

    df = add_indicators(
        ohlcv_df(
            symbol,
            TIMEFRAME_SIGNAL
        )
    )

    atr = safe_float(
        df.iloc[-2]["atr"]
    )

    tick = safe_float(
        get_instrument(
            symbol
        ).get(
            "tickSize"
        ),
        0.0001
    )

    levels = calculate_trade_levels(
        entry,
        atr,
        side,
        tick
    )

    if not levels:
        return False

    sl, tp, _ = levels

    last = safe_float(
        exchange.fetch_ticker(
            symbol
        ).get("last")
    )

    if not tpsl_prices_valid(
        side,
        sl,
        tp,
        last
    ):
        return False

    ok, _ = place_tpsl_for_position(
        p,
        sl,
        tp,
        client_tag="repair"
    )

    return ok


def repair_existing_positions():

    positions = get_positions()

    if not positions:
        return True

    tg(
        f"🛡️ V6.8 STARTUP\n"
        f"Open positions: "
        f"{len(positions)}\n"
        f"Checking TP + SL..."
    )

    for p in positions:

        symbol = position_symbol(p)
        side = position_side(p)

        try:

            protected, _ = (
                has_live_protection(p)
            )

            if protected:

                tg(
                    f"✅ {symbol} "
                    f"{side.upper()}: "
                    f"TP + SL confirmed."
                )

                continue

            repaired = (
                repair_one_position(p)
            )

            if repaired:

                tg(
                    f"🛡️ REPAIRED "
                    f"{symbol} "
                    f"{side.upper()}"
                )

            else:

                tg(
                    f"🚨 UNPROTECTED "
                    f"{symbol} "
                    f"{side.upper()}"
                )

        except Exception as e:

            tg(
                f"🚨 PROTECTION ERROR "
                f"{symbol}\n"
                f"{e}"
            )

    # Critical:
    # verify ALL positions after repairs.
    return all_positions_protected()


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(
    symbol,
    leverage
):

    payload = {
        "instId":
            blofin_inst_id(symbol),

        "leverage":
            str(leverage),

        "marginMode":
            MARGIN_MODE,
    }

    # In one-way/net mode positionSide is not
    # required for setting leverage.
    resp = native(
        "private_post_account_set_leverage",
        payload
    )

    if not native_ok(resp):
        return (
            False,
            response_error(resp)
        )

    return True, resp


# ============================================================
# POSITION FIND
# ============================================================

def find_new_position(
    symbol,
    before_ids,
    timeout=8
):

    deadline = (
        time.time()
        + timeout
    )

    while time.time() < deadline:

        try:

            for p in get_positions():

                if (
                    position_symbol(p)
                    != symbol
                ):
                    continue

                pid = position_id(p)

                if (
                    pid
                    and pid not in before_ids
                ):
                    return p

                # One-way mode may merge/return
                # an empty positionId.
                if not pid:
                    return p

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


# ============================================================
# DAILY RESET
# ============================================================

def reset_daily_if_needed(
    current_equity=None
):

    today = amsterdam_today()

    if daily["date"] != today:

        daily["date"] = today

        daily["start_equity"] = (
            current_equity
        )

        daily["trades"] = 0
        daily["wins"] = 0
        daily["losses"] = 0
        daily["net"] = 0.0

        daily["long_trades"] = 0
        daily["long_wins"] = 0
        daily["long_net"] = 0.0

        daily["short_trades"] = 0
        daily["short_wins"] = 0
        daily["short_net"] = 0.0

        return True

    if (
        daily["start_equity"] is None
        and current_equity is not None
    ):
        daily["start_equity"] = (
            current_equity
        )

    return False


# ============================================================
# OPEN TRADE
# ============================================================

def open_trade(signal):

    global protection_lock

    symbol = signal["symbol"]
    side = signal["side"]

    account = futures_account()

    equity = account[
        "total_equity"
    ]

    available = account[
        "available"
    ]

    reset_daily_if_needed(
        equity
    )

    if equity < MIN_EQUITY_USDT:

        tg(
            f"⛔ RISK STOP\n"
            f"Equity ${equity:.2f} "
            f"< ${MIN_EQUITY_USDT:.2f}"
        )

        return False

    if available <= 0:
        return False

    # Never open when another live position
    # is known to be unprotected.
    if (
        not PAPER_TRADING
        and protection_lock
    ):
        return False

    tick = safe_float(
        get_instrument(
            symbol
        ).get(
            "tickSize"
        ),
        0.0001
    )

    live = safe_float(
        exchange.fetch_ticker(
            symbol
        ).get("last"),
        signal["price"]
    )

    signal_px = signal["price"]

    if (
        live <= 0
        or signal_px <= 0
    ):
        return False

    slip = (
        live - signal_px
    ) / signal_px

    if (
        side == "long"
        and slip > MAX_FILL_SLIP_PCT
    ):

        tg(
            f"⏭️ {symbol} "
            f"LONG too late "
            f"{slip * 100:.2f}%"
        )

        return False

    if (
        side == "short"
        and slip < -MAX_FILL_SLIP_PCT
    ):

        tg(
            f"⏭️ {symbol} "
            f"SHORT too late "
            f"{slip * 100:.2f}%"
        )

        return False

    entry = live

    levels = calculate_trade_levels(
        entry,
        signal["atr"],
        side,
        tick
    )

    if not levels:
        return False

    sl, tp, _ = levels

    stop_pct = (
        abs(entry - sl)
        / entry
    )

    if stop_pct < MIN_STOP_PCT:
        return False

    sizing = calculate_contracts(
        symbol,
        entry,
        sl,
        equity,
        available
    )

    if not sizing:
        return False

    if (
        sizing["margin"]
        >
        available
        * AVAILABLE_MARGIN_BUFFER
    ):
        return False

    before_ids = {
        position_id(p)
        for p in get_positions()
        if position_id(p)
    }

    # --------------------------------------------------------
    # HARD LEVERAGE CHECK
    # --------------------------------------------------------

    lev_ok, lev_result = (
        set_leverage(
            symbol,
            LEVERAGE
        )
    )

    if not lev_ok:

        tg(
            f"⛔ LEVERAGE FAILED\n"
            f"{symbol}\n"
            f"Requested: {LEVERAGE}x\n"
            f"{lev_result}\n"
            f"Trade skipped."
        )

        return False

    # --------------------------------------------------------
    # ORDER PAYLOAD
    # --------------------------------------------------------
    #
    # IMPORTANT:
    # SyntaxError from V6.7 is fixed here.
    #
    # Correct:
    # f"v68{int(time.time() * 1000) % 1000000000}"
    #
    # ========================================================

    payload = {
        "instId":
            blofin_inst_id(symbol),

        "marginMode":
            MARGIN_MODE,

        "positionSide":
            "net",

        "side": (
            "buy"
            if side == "long"
            else "sell"
        ),

        "orderType":
            "market",

        "size":
            str(
                sizing["contracts"]
            ),

        "tpTriggerPrice":
            str(tp),

        "tpOrderPrice":
            "-1",

        "tpTriggerPriceType":
            "mark",

        "slTriggerPrice":
            str(sl),

        "slOrderPrice":
            "-1",

        "slTriggerPriceType":
            "mark",

        "clientOrderId":
            f"v68{int(time.time() * 1000) % 1000000000}",
    }

    if BROKER_ID:
        payload["brokerId"] = BROKER_ID

    # --------------------------------------------------------
    # PAPER
    # --------------------------------------------------------

    if PAPER_TRADING:

        tg(
            f"🧪 PAPER V6.8\n"
            f"{symbol} "
            f"{side.upper()}\n"
            f"Score "
            f"{signal['score']:.0f}/"
            f"{signal['threshold']:.0f}"
            f"{' ⚡SECOND' if signal.get('second_chance') else ''}\n"
            f"Directional tactics "
            f"{signal['tactics']}\n"
            f"5m "
            f"{signal['confirm_components']}/4\n"
            f"ADX "
            f"{signal['adx']:.0f}\n"
            f"RS composite "
            f"{signal['rel'] * 100:+.2f}%\n"
            f"RS15 "
            f"{signal.get('rel15', 0) * 100:+.2f}%\n"
            f"RS45 "
            f"{signal.get('rel45', 0) * 100:+.2f}%\n"
            f"RS90 "
            f"{signal.get('rel90', 0) * 100:+.2f}%\n"
            f"RSI "
            f"{signal['rsi']:.0f}\n"
            f"Pullback "
            f"{signal['pullback_quality']}/3\n"
            f"EntryQ "
            f"{signal['entry_quality']}\n"
            f"BTC "
            f"{signal['btc_regime']}\n"
            f"Entry "
            f"{entry:.8g}\n"
            f"SL "
            f"{sl:.8g}\n"
            f"TP "
            f"{tp:.8g}\n"
            f"ExpTP "
            f"${sizing['expected_tp_usdt']:.2f}\n"
            f"Est. cost "
            f"${sizing['roundtrip_cost']:.2f}"
        )

        return True

    # --------------------------------------------------------
    # LIVE
    # --------------------------------------------------------

    resp = native(
        "private_post_trade_order",
        payload
    )

    if not native_ok(resp):

        err = response_error(resp)

        if "152012" in err:

            protection_lock = True

            tg(
                f"🛑 152012 LOCK\n"
                f"{err}"
            )

        else:

            tg(
                f"❌ LIVE ORDER FAILED\n"
                f"{symbol} "
                f"{side.upper()}\n"
                f"{err}"
            )

        return False

    tg(
        f"🟢 V6.8 LIVE ORDER\n"
        f"{symbol} "
        f"{side.upper()}\n"
        f"━━━━━━━━━━━━\n"
        f"Score "
        f"{signal['score']:.0f}/"
        f"{signal['threshold']:.0f}"
        f"{' ⚡ SECOND-CHANCE' if signal.get('second_chance') else ''}\n"
        f"Directional tactics "
        f"{signal['tactics']}\n"
        f"5m "
        f"{signal['confirm_components']}/4\n"
        f"ADX "
        f"{signal['adx']:.0f}\n"
        f"RS "
        f"{signal['rel'] * 100:+.2f}%\n"
        f"RSI "
        f"{signal['rsi']:.0f}\n"
        f"Pullback "
        f"{signal['pullback_quality']}/3\n"
        f"EntryQ "
        f"{signal['entry_quality']}\n"
        f"Momentum "
        f"{signal['momentum_quality']}\n"
        f"BTC "
        f"{signal['btc_regime']}\n"
        f"━━━━━━━━━━━━\n"
        f"Entry ~ "
        f"{entry:.8g}\n"
        f"SL "
        f"{sl:.8g}\n"
        f"TP "
        f"{tp:.8g}\n"
        f"Stop "
        f"{stop_pct * 100:.2f}%\n"
        f"ExpTP "
        f"${sizing['expected_tp_usdt']:.2f}\n"
        f"Cost "
        f"${sizing['roundtrip_cost']:.2f}\n"
        f"Contracts "
        f"{sizing['contracts']}\n"
        f"Notional "
        f"${sizing['notional']:.2f}\n"
        f"Margin "
        f"${sizing['margin']:.2f}\n"
        f"{LEVERAGE}x"
    )

    # --------------------------------------------------------
    # POSITION CHECK
    # --------------------------------------------------------

    new_pos = find_new_position(
        symbol,
        before_ids,
        timeout=8
    )

    if new_pos is None:

        tg(
            f"⚠️ {symbol}: "
            f"order accepted, "
            f"position not visible yet."
        )

        # An accepted live order may have created exposure.
        # Do not treat this as an execution failure and
        # immediately try another candidate.
        return True

    # --------------------------------------------------------
    # PROTECTION
    # --------------------------------------------------------

    if has_live_protection(
        new_pos
    )[0]:

        tg(
            f"🛡️ PROTECTION CONFIRMED\n"
            f"{symbol} "
            f"{side.upper()}\n"
            f"TP + SL visible."
        )

        return True

    last = safe_float(
        exchange.fetch_ticker(
            symbol
        ).get("last")
    )

    if tpsl_prices_valid(
        side,
        sl,
        tp,
        last
    ):

        ok, result = (
            place_tpsl_for_position(
                new_pos,
                sl,
                tp,
                client_tag="fallback"
            )
        )

        if ok:

            tg(
                f"🛡️ FALLBACK "
                f"TP + SL CONFIRMED\n"
                f"{symbol}"
            )

            return True

        tg(
            f"🚨 CRITICAL "
            f"PROTECTION FAILURE\n"
            f"{symbol}\n"
            f"{result}\n"
            f"NEW TRADES PAUSED."
        )

        protection_lock = True

        # The position already exists, so this is still
        # considered an opened trade. Returning False here
        # could make main() attempt another candidate.
        return True

    tg(
        f"🚨 {symbol}: "
        f"position exists but fallback "
        f"TP/SL cannot safely be placed.\n"
        f"NEW TRADES PAUSED."
    )

    protection_lock = True

    return True


# ============================================================
# HISTORY
# ============================================================

def closed_positions_history(
    limit=100
):

    merged = []

    for state in (
        "closed",
        "liquidated",
        "adl",
    ):

        try:

            resp = native(
                "private_get_account_positions_history",
                {
                    "state": state,
                    "limit": str(limit),
                }
            )

            if native_ok(resp):

                merged.extend(
                    resp.get("data")
                    or []
                )

        except Exception:
            continue

    return merged


def close_reason(record):

    raw = str(
        record.get(
            "closeType",
            record.get(
                "state",
                ""
            )
        )
    ).lower()

    mapping = {
        "tp": "TAKE PROFIT",
        "take_profit": "TAKE PROFIT",
        "sl": "STOP LOSS",
        "stop_loss": "STOP LOSS",
        "liquidated": "LIQUIDATION",
        "liquidation": "LIQUIDATION",
        "partially_liquidated": "PARTIAL LIQ",
        "adl": "ADL",
        "partially_closed": "PARTIAL CLOSE",
        "manual": "MANUAL",
        "mannual": "MANUAL",
        "close": "CLOSED",
    }

    return mapping.get(
        raw,
        raw.upper()
        if raw
        else "CLOSED"
    )


def fmt_duration(
    start_ms,
    end_ms
):

    start = safe_float(
        start_ms
    )

    end = safe_float(
        end_ms
    )

    if (
        start <= 0
        or end <= 0
        or end < start
    ):
        return "unknown"

    seconds = int(
        (end - start)
        / 1000
    )

    hours, rem = divmod(
        seconds,
        3600
    )

    minutes, secs = divmod(
        rem,
        60
    )

    if hours:
        return (
            f"{hours}h "
            f"{minutes}m"
        )

    if minutes:
        return (
            f"{minutes}m "
            f"{secs}s"
        )

    return f"{secs}s"


def infer_side(record):

    side = str(
        record.get(
            "positionSide",
            "net"
        )
    ).lower()

    if side in (
        "long",
        "short",
    ):
        return side

    entry = safe_float(
        record.get(
            "openAveragePrice"
        )
    )

    exit_p = safe_float(
        record.get(
            "closeAveragePrice"
        )
    )

    pnl = safe_float(
        record.get(
            "realizedPnl"
        )
    )

    if entry and exit_p:

        moved_up = (
            exit_p >= entry
        )

        if (
            (
                moved_up
                and pnl >= 0
            )
            or
            (
                not moved_up
                and pnl < 0
            )
        ):
            return "long"

        return "short"

    return "net"


# ============================================================
# LEARNING UPDATE
# ============================================================

def update_learning(
    record,
    net_pnl
):

    global learning

    side = infer_side(record)

    # Conservative history bucket.
    # Full setup-context persistence can be added
    # after the execution core has proven stable.
    fp = (
        f"{side}|history"
    )

    fingerprints = (
        learning.setdefault(
            "fingerprints",
            {}
        )
    )

    rec = fingerprints.setdefault(
        fp,
        {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "net": 0.0,
        }
    )

    rec["trades"] += 1

    if net_pnl >= 0:
        rec["wins"] += 1

    else:
        rec["losses"] += 1

    rec["net"] += net_pnl

    learning["trades"] = (
        int(
            learning.get(
                "trades",
                0
            )
        )
        + 1
    )

    if net_pnl >= 0:

        learning["wins"] = (
            int(
                learning.get(
                    "wins",
                    0
                )
            )
            + 1
        )

    else:

        learning["losses"] = (
            int(
                learning.get(
                    "losses",
                    0
                )
            )
            + 1
        )

    save_learning()


# ============================================================
# CLOSED TRADE
# ============================================================

def notify_closed_trade(record):

    global consecutive_losses
    global loss_pause_until

    reset_daily_if_needed()

    symbol = str(
        record.get(
            "instId",
            ""
        )
    ).replace(
        "-USDT",
        "/USDT:USDT"
    )

    side = infer_side(record)

    entry = safe_float(
        record.get(
            "openAveragePrice"
        )
    )

    exit_price = safe_float(
        record.get(
            "closeAveragePrice"
        )
    )

    gross_pnl = safe_float(
        record.get(
            "realizedPnl"
        )
    )

    fee = abs(
        safe_float(
            record.get("fee")
        )
    )

    net_pnl = (
        gross_pnl - fee
    )

    pnl_ratio = (
        safe_float(
            record.get(
                "realizedPnlRatio"
            )
        )
        * 100
    )

    duration = fmt_duration(
        record.get("createTime"),
        record.get("updateTime")
    )

    reason = close_reason(
        record
    )

    size = safe_float(
        record.get(
            "closePositions"
        )
    )

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

    symbol_last_trade[
        symbol
    ] = time.time()

    if net_pnl >= 0:

        daily["wins"] += 1
        consecutive_losses = 0

        symbol_last_result[
            symbol
        ] = "win"

        symbol_loss_streak[
            symbol
        ] = 0

    else:

        daily["losses"] += 1
        consecutive_losses += 1

        symbol_last_result[
            symbol
        ] = "loss"

        symbol_loss_streak[
            symbol
        ] = (
            symbol_loss_streak.get(
                symbol,
                0
            )
            + 1
        )

        if (
            consecutive_losses
            >= MAX_CONSECUTIVE_LOSSES
        ):

            loss_pause_until = (
                time.time()
                + LOSS_PAUSE_MIN * 60
            )

    update_learning(
        record,
        net_pnl
    )

    wr_long = (
        daily["long_wins"]
        /
        daily["long_trades"]
        * 100
        if daily["long_trades"]
        else 0
    )

    wr_short = (
        daily["short_wins"]
        /
        daily["short_trades"]
        * 100
        if daily["short_trades"]
        else 0
    )

    pause = ""

    if (
        consecutive_losses
        >= MAX_CONSECUTIVE_LOSSES
    ):

        pause = (
            f"\n⏸ "
            f"{MAX_CONSECUTIVE_LOSSES} "
            f"losses → pauze "
            f"{LOSS_PAUSE_MIN} min"
        )

    icon = (
        "🟢"
        if net_pnl >= 0
        else "🔴"
    )

    tg(
        f"{icon} POSITION CLOSED V6.8\n\n"
        f"{symbol}\n"
        f"{side.upper()}\n"
        f"━━━━━━━━━━━━\n"
        f"Entry: {entry:.8g}\n"
        f"Exit:  {exit_price:.8g}\n"
        f"Size:  {size:g}\n\n"
        f"💰 Realized: "
        f"{'+' if gross_pnl >= 0 else ''}"
        f"${gross_pnl:.2f}\n"
        f"📊 Ratio: "
        f"{pnl_ratio:+.2f}%\n"
        f"💸 Fee: "
        f"-${fee:.2f}\n"
        f"💵 Net: "
        f"{'+' if net_pnl >= 0 else ''}"
        f"${net_pnl:.2f}\n\n"
        f"🎯 {reason}\n"
        f"⏱ {duration}\n\n"
        f"📅 TODAY Amsterdam\n"
        f"All: "
        f"{daily['trades']} | "
        f"{daily['wins']}W "
        f"{daily['losses']}L | "
        f"{'+' if daily['net'] >= 0 else ''}"
        f"${daily['net']:.2f}\n"
        f"LONG: "
        f"{daily['long_trades']} / "
        f"{wr_long:.0f}% / "
        f"{'+' if daily['long_net'] >= 0 else ''}"
        f"${daily['long_net']:.2f}\n"
        f"SHORT: "
        f"{daily['short_trades']} / "
        f"{wr_short:.0f}% / "
        f"{'+' if daily['short_net'] >= 0 else ''}"
        f"${daily['short_net']:.2f}"
        f"{pause}"
    )


# ============================================================
# HISTORY SEED
# ============================================================

def seed_closed_history():

    reset_daily_if_needed()

    try:

        for r in closed_positions_history(
            100
        ):

            hid = str(
                r.get(
                    "historyId",
                    ""
                )
            )

            if hid:
                seen_closed_history_ids.add(
                    hid
                )

        tg(
            f"📚 V6.8 PNL TRACKER READY\n"
            f"Known closes: "
            f"{len(seen_closed_history_ids)}\n"
            f"Learning trades: "
            f"{learning.get('trades', 0)}"
        )

    except Exception as e:

        tg(
            f"⚠️ PNL tracker warning\n"
            f"{e}"
        )


# ============================================================
# CLOSED POSITION CHECK
# ============================================================

def check_for_closed_positions():

    records = closed_positions_history(
        100
    )

    new_records = []

    for r in reversed(records):

        hid = str(
            r.get(
                "historyId",
                ""
            )
        )

        if (
            not hid
            or hid
            in seen_closed_history_ids
        ):
            continue

        seen_closed_history_ids.add(
            hid
        )

        new_records.append(r)

    for r in new_records:
        notify_closed_trade(r)

    if (
        len(
            seen_closed_history_ids
        ) > 2500
    ):

        seen_closed_history_ids.clear()

        for r in records:

            hid = str(
                r.get(
                    "historyId",
                    ""
                )
            )

            if hid:
                seen_closed_history_ids.add(
                    hid
                )


# ============================================================
# DAILY RISK
# ============================================================

def check_daily_risk(equity):

    reset_daily_if_needed(
        equity
    )

    start = daily[
        "start_equity"
    ]

    if (
        not start
        or start <= 0
    ):
        return True, "OK"

    dd = (
        start - equity
    ) / start * 100

    if (
        dd
        >= DAILY_LOSS_LIMIT_PCT
    ):
        return (
            False,
            f"Daily risk stop "
            f"{dd:.2f}% "
            f"vs start "
            f"${start:.2f}"
        )

    return True, "OK"


# ============================================================
# COOLDOWN
# ============================================================

def cooldown_ok(symbol):

    last = symbol_last_trade.get(
        symbol,
        0
    )

    if (
        symbol_loss_streak.get(
            symbol,
            0
        ) >= 2
    ):

        needed = (
            REPEAT_LOSS_COOLDOWN_MIN
            * 60
        )

    elif (
        symbol_last_result.get(
            symbol
        ) == "loss"
    ):

        needed = (
            LOSS_COOLDOWN_MIN
            * 60
        )

    else:

        needed = (
            WIN_COOLDOWN_MIN
            * 60
        )

    return (
        time.time() - last
    ) >= needed


# ============================================================
# ALT COUNT
# ============================================================

def count_alt_sides(positions):

    longs = 0
    shorts = 0

    for p in positions:

        if (
            position_symbol(p)
            in MAJORS
        ):
            continue

        if (
            position_side(p)
            == "short"
        ):
            shorts += 1

        else:
            longs += 1

    return longs, shorts


# ============================================================
# SCORE REPORT
# ============================================================

def maybe_score_report(rows):

    global last_score_report

    if (
        time.time()
        - last_score_report
        <
        SCORE_REPORT_MIN * 60
    ):
        return

    last_score_report = time.time()

    ranked = sorted(
        rows,
        key=lambda x: (
            x.get("score", 0),
            x.get(
                "entry_quality",
                0
            ),
            x.get("adx", 0),
        ),
        reverse=True
    )[:10]

    lines = [
        (
            f"📡 V6.8 "
            f"{session_state()} "
            f"{now_utc().strftime('%H:%M')} UTC"
        )
    ]

    if not ranked:
        lines.append("geen scans")

    for r in ranked:

        skip = r.get("skip")

        mark = (
            "🟢 READY"
            if not skip
            else f"⏭ {skip}"
        )

        second = (
            " ⚡"
            if r.get(
                "second_chance"
            )
            else ""
        )

        lines.append(
            f"{r['symbol'].split('/')[0]} "
            f"{str(r.get('side') or '-').upper()} "
            f"{r.get('score', 0):.0f}/"
            f"{r.get('threshold', BASE_MIN_SCORE):.0f}"
            f"{second} "
            f"t{r.get('tactics', 0)} "
            f"5m{r.get('confirm_components', 0)}/4 "
            f"ADX{r.get('adx', 0):.0f} "
            f"RS{r.get('rel', 0) * 100:+.1f}% "
            f"EQ{r.get('entry_quality', 0)} "
            f"{mark}"
        )

    tg(
        "\n".join(lines)
    )


# ============================================================
# STARTUP
# ============================================================

def startup_diagnostics():

    acc = futures_account()

    reset_daily_if_needed(
        acc["total_equity"]
    )

    tg(
        f"🚀 BLOFIN V6.8 STARTED\n"
        f"Mode: "
        f"{'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"Position mode expected: NET / ONE-WAY\n"
        f"Equity: "
        f"${acc['total_equity']:.2f}\n"
        f"Available: "
        f"${acc['available']:.2f}\n"
        f"Day start: "
        f"${daily['start_equity']:.2f}\n"
        f"Base score: "
        f"{BASE_MIN_SCORE:.0f}\n"
        f"Hard floor: "
        f"{HARD_MIN_SCORE:.0f}\n"
        f"Strong regime: "
        f"{STRONG_REGIME_MIN_SCORE:.0f}\n"
        f"Directional tactics required: "
        f"{MIN_TACTICS}\n"
        f"ADX trend: "
        f"{ADX_TREND:.0f}\n"
        f"ADX strong: "
        f"{ADX_STRONG:.0f}\n"
        f"Alt cap "
        f"L{MAX_ALT_LONGS}/"
        f"S{MAX_ALT_SHORTS}\n"
        f"Max positions: "
        f"{MAX_ACTIVE_POSITIONS}\n"
        f"Max execution attempts/scan: "
        f"{MAX_EXECUTION_ATTEMPTS_PER_SCAN}\n"
        f"Learning trades: "
        f"{learning.get('trades', 0)}\n"
        f"Daily cap: "
        f"{DAILY_LOSS_LIMIT_PCT:.0f}%"
    )


# ============================================================
# PROTECTION WATCH
# ============================================================

def protection_watch():

    global protection_lock

    if PAPER_TRADING:
        protection_lock = False
        return

    positions = get_positions()

    if not positions:
        protection_lock = False
        return

    if REPAIR_EXISTING_POSITIONS:

        for p in positions:

            try:

                protected, _ = (
                    has_live_protection(p)
                )

                if protected:
                    continue

                symbol = position_symbol(p)
                side = position_side(p)

                repaired = (
                    repair_one_position(p)
                )

                if repaired:

                    tg(
                        f"🛡️ REPAIRED "
                        f"{symbol} "
                        f"{side.upper()}"
                    )

            except Exception:
                pass

    # Re-fetch/re-check ALL positions.
    protection_lock = (
        not all_positions_protected()
    )


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

            protection_lock = (
                not
                repair_existing_positions()
            )

        except Exception as e:

            protection_lock = True

            tg(
                f"🚨 STARTUP "
                f"PROTECTION FAILED\n"
                f"{e}"
            )

    while True:

        try:

            # ------------------------------------------------
            # CLOSED TRADES
            # ------------------------------------------------

            if not PAPER_TRADING:

                try:
                    check_for_closed_positions()

                except Exception as e:

                    tg(
                        f"⚠️ PNL TRACKER ERROR\n"
                        f"{e}"
                    )

            # ------------------------------------------------
            # ACCOUNT / DAILY RISK
            # ------------------------------------------------

            acc = futures_account()

            ok, reason = check_daily_risk(
                acc["total_equity"]
            )

            if not ok:

                tg(
                    f"🛑 {reason}"
                )

                time.sleep(120)
                continue

            # ------------------------------------------------
            # LOSS PAUSE
            # ------------------------------------------------

            if (
                time.time()
                < loss_pause_until
            ):

                time.sleep(
                    SCAN_SECONDS
                )

                continue

            # ------------------------------------------------
            # POSITIONS
            # ------------------------------------------------

            positions = get_positions()

            # ------------------------------------------------
            # PROTECTION
            # ------------------------------------------------

            if not PAPER_TRADING:
                protection_watch()

            # ------------------------------------------------
            # POSITION / SAFETY LIMIT
            # ------------------------------------------------

            positions = get_positions()

            if (
                len(positions)
                >= MAX_ACTIVE_POSITIONS
            ):

                time.sleep(
                    SCAN_SECONDS
                )

                continue

            if (
                protection_lock
                and not PAPER_TRADING
            ):

                time.sleep(
                    SCAN_SECONDS
                )

                continue

            # ------------------------------------------------
            # BTC REGIME
            # ------------------------------------------------

            try:
                btc = btc_regime()

            except Exception as e:

                tg(
                    f"⚠️ BTC REGIME ERROR\n"
                    f"{e}"
                )

                time.sleep(
                    SCAN_SECONDS
                )

                continue

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------

            scanned = []
            candidates = []

            for symbol in SYMBOLS:

                if not cooldown_ok(
                    symbol
                ):
                    continue

                try:

                    signal = evaluate_symbol(
                        symbol,
                        btc
                    )

                    if not signal:
                        continue

                    scanned.append(
                        signal
                    )

                    if not signal.get(
                        "skip"
                    ):
                        candidates.append(
                            signal
                        )

                except Exception:
                    continue

            # ------------------------------------------------
            # REPORT
            # ------------------------------------------------

            maybe_score_report(
                scanned
            )

            # ------------------------------------------------
            # RANKING
            # ------------------------------------------------

            candidates.sort(
                key=lambda x: (
                    x["score"],
                    x["entry_quality"],
                    x["confirm_components"],
                    x["adx"],
                    x["tactics"],
                ),
                reverse=True
            )

            # ------------------------------------------------
            # OPEN MAX ONE SUCCESSFUL TRADE PER SCAN
            # ------------------------------------------------

            execution_attempts = 0

            for signal in candidates:

                current = get_positions()

                if (
                    len(current)
                    >= MAX_ACTIVE_POSITIONS
                ):
                    break

                existing_symbols = {
                    position_symbol(p)
                    for p in current
                }

                if (
                    signal["symbol"]
                    in existing_symbols
                ):
                    continue

                alt_l, alt_s = (
                    count_alt_sides(
                        current
                    )
                )

                if (
                    signal["symbol"]
                    not in MAJORS
                ):

                    if (
                        signal["side"]
                        == "long"
                        and
                        alt_l
                        >= MAX_ALT_LONGS
                    ):
                        continue

                    if (
                        signal["side"]
                        == "short"
                        and
                        alt_s
                        >= MAX_ALT_SHORTS
                    ):
                        continue

                if (
                    execution_attempts
                    >=
                    MAX_EXECUTION_ATTEMPTS_PER_SCAN
                ):
                    break

                execution_attempts += 1

                opened = open_trade(
                    signal
                )

                if opened:

                    # Only successful/accepted trade
                    # starts the cooldown.
                    symbol_last_trade[
                        signal["symbol"]
                    ] = time.time()

                    # Max one successful trade per scan.
                    break

                # IMPORTANT:
                # Failed execution does NOT break.
                # Candidate #2/#3 may still be tried.

            # ------------------------------------------------
            # SLEEP
            # ------------------------------------------------

            time.sleep(
                SCAN_SECONDS
            )

        except KeyboardInterrupt:

            tg(
                "🛑 V6.8 stopped."
            )

            raise

        except Exception as e:

            msg = (
                f"{type(e).__name__}: "
                f"{e}"
            )

            if "152002" in msg:

                time.sleep(20)
                continue

            tg(
                f"⚠️ V6.8 LOOP ERROR\n"
                f"{msg}"
            )

            time.sleep(30)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
