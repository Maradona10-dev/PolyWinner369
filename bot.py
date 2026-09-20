import os
import time
import json
import math
import uuid
import hmac
import base64
import hashlib
import threading
import traceback
from datetime import datetime, timezone

import requests
import ccxt


# ============================================================
# BLOFIN TACTICAL ENGINE V6.0 — LIVE
# ============================================================

VERSION = "6.0-LIVE"

# ------------------------------------------------------------
# LIVE / SAFETY
# ------------------------------------------------------------

PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"

# Extra bevestiging zodat de bot niet per ongeluk live gaat
LIVE_CONFIRM = os.getenv("LIVE_CONFIRM", "NO")

if not PAPER_TRADING and LIVE_CONFIRM != "YES_I_WANT_LIVE_TRADING":
    raise RuntimeError(
        "LIVE TRADING BLOCKED.\n"
        "Set LIVE_CONFIRM=YES_I_WANT_LIVE_TRADING"
    )


# ------------------------------------------------------------
# BLOFIN
# ------------------------------------------------------------

EXCHANGE_API_KEY = os.getenv("EXCHANGE_API_KEY", "")
EXCHANGE_SECRET = os.getenv("EXCHANGE_SECRET", "")
EXCHANGE_PASSWORD = os.getenv("EXCHANGE_PASSWORD", "")

BLOFIN_URL = "https://openapi.blofin.com"

if not EXCHANGE_API_KEY:
    raise RuntimeError("EXCHANGE_API_KEY ontbreekt")

if not EXCHANGE_SECRET:
    raise RuntimeError("EXCHANGE_SECRET ontbreekt")

if not EXCHANGE_PASSWORD:
    raise RuntimeError("EXCHANGE_PASSWORD ontbreekt")


# ------------------------------------------------------------
# TELEGRAM
# ------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ------------------------------------------------------------
# ACCOUNT / RISK
# ------------------------------------------------------------

LEVERAGE = 10

MAX_ACTIVE_POSITIONS = 2

MIN_MARGIN_USDT = 5.0
BASE_MARGIN_USDT = 7.5
MAX_MARGIN_USDT = 12.0

MAX_MARGIN_EQUITY_PCT = 0.18
MAX_TOTAL_MARGIN_EQUITY_PCT = 0.38

MAX_RISK_EQUITY_PCT = 0.015

MIN_EQUITY_USDT = 30.0

DAILY_LOSS_LIMIT_PCT = 0.07
MAX_CONSECUTIVE_LOSSES = 3

SYMBOL_COOLDOWN_SECONDS = 15 * 60
LOSS_COOLDOWN_SECONDS = 30 * 60


# ------------------------------------------------------------
# TARGETS
# ------------------------------------------------------------

MIN_PROFIT_TARGET_USDT = 2.50
IDEAL_PROFIT_TARGET_USDT = 5.00
MAX_PROFIT_TARGET_USDT = 10.00

STOP_ATR_MULTIPLIER = 1.25
TP_R_MULTIPLIER = 1.80


# ------------------------------------------------------------
# SCANNER
# ------------------------------------------------------------

SCAN_TIMEFRAME = "15m"
CONFIRM_TIMEFRAME = "5m"
TREND_TIMEFRAME = "1h"

SCAN_INTERVAL = 45

MIN_SCORE = 54
STRONG_SCORE = 68

ATR_MIN_PCT = 0.20
ATR_MAX_PCT = 5.00

VOLUME_RATIO_MIN = 1.05

MAX_SPREAD_PCT = 0.30


# ------------------------------------------------------------
# SYMBOLS
# ------------------------------------------------------------

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
# GLOBAL STATE
# ============================================================

lock = threading.RLock()

positions = {}
symbol_cooldowns = {}

daily_start_equity = None
daily_realized_pnl = 0.0
consecutive_losses = 0

learning_file = "bot_learning_v6.json"

learning = {
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "gross_profit": 0.0,
    "gross_loss": 0.0,
    "net": 0.0,
    "strategy": {}
}


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM NOT CONFIGURED:", message)
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=10
        )

        if r.status_code != 200:
            print("Telegram error:", r.text)
            return False

        return True

    except Exception as e:
        print("Telegram exception:", e)
        return False


def telegram_test():
    mode = "PAPER" if PAPER_TRADING else "LIVE"

    telegram(
        f"🟢 BLOFIN TACTICAL {VERSION}\n\n"
        f"Mode: {mode}\n"
        f"Leverage: {LEVERAGE}x\n"
        f"Max positions: {MAX_ACTIVE_POSITIONS}\n"
        f"Scanner: {len(SYMBOLS)} markets\n\n"
        f"Telegram: ✅\n"
        f"Bot gestart."
    )


# ============================================================
# CCXT
# ============================================================

exchange = ccxt.blofin({
    "apiKey": EXCHANGE_API_KEY,
    "secret": EXCHANGE_SECRET,
    "password": EXCHANGE_PASSWORD,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
    },
})

exchange.load_markets()


# ============================================================
# BLOFIN NATIVE REST
# ============================================================

def blofin_request(method, path, body=None, params=None):

    if body is None:
        body = {}

    if params is None:
        params = {}

    timestamp = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())

    if method.upper() == "GET":
        if params:
            query = "&".join(
                f"{k}={params[k]}"
                for k in sorted(params)
            )
            request_path = f"{path}?{query}"
        else:
            request_path = path

        body_string = ""

    else:
        body_string = json.dumps(
            body,
            separators=(",", ":")
        )

        request_path = path

    # BloFin signature:
    # requestPath + method + timestamp + nonce + body
    prehash = (
        request_path
        + method.upper()
        + timestamp
        + nonce
        + body_string
    )

    digest = hmac.new(
        EXCHANGE_SECRET.encode(),
        prehash.encode(),
        hashlib.sha256
    ).hexdigest()

    signature = base64.b64encode(
        digest.encode()
    ).decode()

    headers = {
        "ACCESS-KEY": EXCHANGE_API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": EXCHANGE_PASSWORD,
        "Content-Type": "application/json",
    }

    url = BLOFIN_URL + request_path

    if method.upper() == "GET":
        response = requests.get(
            url,
            headers=headers,
            timeout=15
        )
    else:
        response = requests.post(
            url,
            headers=headers,
            data=body_string,
            timeout=15
        )

    response.raise_for_status()

    data = response.json()

    if str(data.get("code")) != "0":
        raise RuntimeError(
            f"BloFin API error: {data}"
        )

    return data


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for price in values[period:]:
        result = (
            (price - result) * multiplier
            + result
        )

    return result


def sma(values, period):

    if len(values) < period:
        return None

    return sum(values[-period:]) / period


def atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):

        high = candles[i][2]
        low = candles[i][3]
        prev_close = candles[i - 1][4]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        trs.append(tr)

    return sum(trs[-period:]) / period


def rsi(closes, period=14):

    if len(closes) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]

        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# ============================================================
# MARKET DATA
# ============================================================

def candles(symbol, timeframe, limit=100):

    return exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=limit
    )


def ticker(symbol):

    return exchange.fetch_ticker(symbol)


# ============================================================
# REGIME
# ============================================================

def detect_regime(candles_1h):

    closes = [x[4] for x in candles_1h]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)

    if not all([e20, e50, e200]):
        return "TRANSITION"

    price = closes[-1]

    if e20 > e50 > e200 and price > e20:
        return "TREND_UP"

    if e20 < e50 < e200 and price < e20:
        return "TREND_DOWN"

    recent = closes[-20:]

    high = max(recent)
    low = min(recent)

    if high > 0:
        range_pct = ((high - low) / low) * 100
    else:
        range_pct = 0

    if range_pct < 6:
        return "RANGE"

    return "TRANSITION"


# ============================================================
# STRATEGY SCORING
# ============================================================

def score_symbol(symbol):

    try:

        c15 = candles(symbol, "15m", 120)
        c5 = candles(symbol, "5m", 80)
        c1h = candles(symbol, "1h", 120)

        if len(c15) < 60:
            return None

        closes15 = [x[4] for x in c15]
        volumes15 = [x[5] for x in c15]

        close = closes15[-1]

        e20 = ema(closes15, 20)
        e50 = ema(closes15, 50)

        atr_value = atr(c15)

        if not e20 or not e50 or not atr_value:
            return None

        atr_pct = atr_value / close * 100

        if atr_pct < ATR_MIN_PCT:
            return None

        if atr_pct > ATR_MAX_PCT:
            return None

        volume_avg = sma(
            volumes15[:-1],
            20
        )

        volume_ratio = (
            volumes15[-1] / volume_avg
            if volume_avg
            else 0
        )

        regime = detect_regime(c1h)

        score_long = 0
        score_short = 0

        strategy_long = []
        strategy_short = []

        # ----------------------------------------------------
        # TREND
        # ----------------------------------------------------

        if regime == "TREND_UP":
            score_long += 15
            strategy_long.append("TREND")

        if regime == "TREND_DOWN":
            score_short += 15
            strategy_short.append("TREND")

        # ----------------------------------------------------
        # EMA
        # ----------------------------------------------------

        if e20 > e50:
            score_long += 10
            strategy_long.append("EMA")

        if e20 < e50:
            score_short += 10
            strategy_short.append("EMA")

        # ----------------------------------------------------
        # PRICE / EMA
        # ----------------------------------------------------

        if close > e20:
            score_long += 7

        if close < e20:
            score_short += 7

        # ----------------------------------------------------
        # VOLUME
        # ----------------------------------------------------

        if volume_ratio >= VOLUME_RATIO_MIN:

            score_long += 8
            score_short += 8

        # ----------------------------------------------------
        # DONCHIAN BREAKOUT
        # ----------------------------------------------------

        previous_high = max(
            x[2] for x in c15[-21:-1]
        )

        previous_low = min(
            x[3] for x in c15[-21:-1]
        )

        if close > previous_high:
            score_long += 15
            strategy_long.append(
                "MOMENTUM_BREAKOUT"
            )

        if close < previous_low:
            score_short += 15
            strategy_short.append(
                "MOMENTUM_BREAKOUT"
            )

        # ----------------------------------------------------
        # LIQUIDITY SWEEP
        # ----------------------------------------------------

        last = c15[-1]

        high = last[2]
        low = last[3]
        candle_close = last[4]

        previous_range_high = max(
            x[2] for x in c15[-13:-1]
        )

        previous_range_low = min(
            x[3] for x in c15[-13:-1]
        )

        # bullish sweep
        if (
            low < previous_range_low
            and candle_close > previous_range_low
        ):
            score_long += 12
            strategy_long.append(
                "LIQUIDITY_SWEEP"
            )

        # bearish sweep
        if (
            high > previous_range_high
            and candle_close < previous_range_high
        ):
            score_short += 12
            strategy_short.append(
                "LIQUIDITY_SWEEP"
            )

        # ----------------------------------------------------
        # 5M CONFIRMATION
        # ----------------------------------------------------

        closes5 = [x[4] for x in c5]

        e20_5 = ema(closes5, 20)
        e50_5 = ema(closes5, 50)
        rsi5 = rsi(closes5)

        if e20_5 and e50_5 and rsi5:

            if (
                e20_5 > e50_5
                and rsi5 > 52
            ):
                score_long += 10

            if (
                e20_5 < e50_5
                and rsi5 < 48
            ):
                score_short += 10

        # ----------------------------------------------------
        # ATR QUALITY
        # ----------------------------------------------------

        if 0.35 <= atr_pct <= 2.5:
            score_long += 5
            score_short += 5

        # ----------------------------------------------------
        # CHOOSE
        # ----------------------------------------------------

        if score_long >= score_short:

            side = "long"
            score = score_long
            strategies = strategy_long

        else:

            side = "short"
            score = score_short
            strategies = strategy_short

        if score < MIN_SCORE:
            return None

        return {
            "symbol": symbol,
            "side": side,
            "score": score,
            "strategies": strategies,
            "regime": regime,
            "price": close,
            "atr": atr_value,
            "atr_pct": atr_pct,
            "volume_ratio": volume_ratio,
        }

    except Exception as e:

        print(
            f"[SCAN ERROR] {symbol}: {e}"
        )

        return None


# ============================================================
# POSITION SIZE
# ============================================================

def calculate_size(signal, equity):

    price = signal["price"]
    atr_value = signal["atr"]

    stop_distance = (
        atr_value * STOP_ATR_MULTIPLIER
    )

    if stop_distance <= 0:
        return None

    # max allowed loss
    risk_budget = equity * MAX_RISK_EQUITY_PCT

    # contracts/notional approximation
    notional_by_risk = (
        risk_budget
        / (stop_distance / price)
    )

    margin = notional_by_risk / LEVERAGE

    margin = max(
        MIN_MARGIN_USDT,
        min(
            margin,
            MAX_MARGIN_USDT,
            equity * MAX_MARGIN_EQUITY_PCT
        )
    )

    notional = margin * LEVERAGE

    raw_amount = notional / price

    market = exchange.market(
        signal["symbol"]
    )

    contract_size = market.get(
        "contractSize",
        1
    )

    contracts = raw_amount / contract_size

    try:
        contracts = float(
            exchange.amount_to_precision(
                signal["symbol"],
                contracts
            )
        )
    except Exception:
        return None

    limits = market.get("limits", {})

    minimum = (
        limits.get("amount", {})
        .get("min")
    )

    if minimum and contracts < minimum:
        return None

    if contracts <= 0:
        return None

    return contracts, margin


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(symbol):

    try:

        exchange.set_leverage(
            LEVERAGE,
            symbol
        )

        return True

    except Exception as e:

        print(
            f"Leverage warning {symbol}: {e}"
        )

        return False


# ============================================================
# NATIVE BLOFIN LIVE ORDER
# ============================================================

def place_live_order(
    signal,
    contracts,
    stop_price,
    tp_price
):

    market = exchange.market(
        signal["symbol"]
    )

    inst_id = market["id"]

    side = (
        "buy"
        if signal["side"] == "long"
        else "sell"
    )

    size = str(contracts)

    client_id = (
        "BOT"
        + str(int(time.time()))[-8:]
        + uuid.uuid4().hex[:6]
    )

    body = {
        "instId": inst_id,
        "marginMode": "isolated",
        "positionSide": "net",
        "side": side,
        "orderType": "market",
        "size": size,

        # Native BloFin protection
        "tpTriggerPrice": str(tp_price),
        "tpOrderPrice": "-1",
        "tpTriggerPriceType": "mark",

        "slTriggerPrice": str(stop_price),
        "slOrderPrice": "-1",
        "slTriggerPriceType": "mark",

        "clientOrderId": client_id
    }

    result = blofin_request(
        "POST",
        "/api/v1/trade/order",
        body=body
    )

    return result


# ============================================================
# POSITION FETCH
# ============================================================

def fetch_live_positions():

    result = blofin_request(
        "GET",
        "/api/v1/account/positions"
    )

    return result.get("data", [])


def active_position_count():

    try:

        data = fetch_live_positions()

        count = 0

        for p in data:

            try:
                size = float(
                    p.get("availablePositions", 0)
                    or p.get("positions", 0)
                    or p.get("size", 0)
                )

                if abs(size) > 0:
                    count += 1

            except Exception:
                continue

        return count

    except Exception as e:

        print(
            "Position check error:",
            e
        )

        return MAX_ACTIVE_POSITIONS


# ============================================================
# ACCOUNT
# ============================================================

def fetch_equity():

    result = blofin_request(
        "GET",
        "/api/v1/account/balance"
    )

    data = result.get("data")

    if isinstance(data, list):
        data = data[0] if data else {}

    if not data:
        raise RuntimeError(
            "Geen account balance ontvangen"
        )

    candidates = [
        data.get("equity"),
        data.get("totalEquity"),
        data.get("accountEquity"),
        data.get("balance")
    ]

    for value in candidates:

        if value is not None:

            try:
                return float(value)
            except Exception:
                pass

    raise RuntimeError(
        f"Equity niet gevonden: {data}"
    )


# ============================================================
# DAILY RISK
# ============================================================

def risk_allowed(equity):

    global daily_start_equity

    if daily_start_equity is None:
        daily_start_equity = equity

    if equity < MIN_EQUITY_USDT:

        telegram(
            f"🛑 BOT STOP\n"
            f"Equity onder minimum:\n"
            f"${equity:.2f}"
        )

        return False

    daily_drawdown = (
        daily_start_equity - equity
    ) / daily_start_equity

    if daily_drawdown >= DAILY_LOSS_LIMIT_PCT:

        telegram(
            f"🛑 DAILY LOSS LIMIT\n\n"
            f"Start: ${daily_start_equity:.2f}\n"
            f"Now: ${equity:.2f}\n"
            f"Drawdown: {daily_drawdown:.2%}\n\n"
            f"Nieuwe trades geblokkeerd."
        )

        return False

    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:

        telegram(
            f"🛑 LOSS STREAK\n\n"
            f"{consecutive_losses} verliestrades.\n"
            f"Nieuwe trades geblokkeerd."
        )

        return False

    return True


# ============================================================
# COOLDOWN
# ============================================================

def cooldown_active(symbol):

    until = symbol_cooldowns.get(
        symbol,
        0
    )

    return time.time() < until


def set_cooldown(
    symbol,
    seconds=SYMBOL_COOLDOWN_SECONDS
):

    symbol_cooldowns[symbol] = (
        time.time() + seconds
    )


# ============================================================
# TRADE
# ============================================================

def execute_trade(signal, equity):

    symbol = signal["symbol"]

    if cooldown_active(symbol):
        return False

    with lock:

        if active_position_count() >= MAX_ACTIVE_POSITIONS:

            return False

        sizing = calculate_size(
            signal,
            equity
        )

        if not sizing:
            return False

        contracts, margin = sizing

        set_leverage(symbol)

        price = signal["price"]

        stop_distance = (
            signal["atr"]
            * STOP_ATR_MULTIPLIER
        )

        if signal["side"] == "long":

            stop = price - stop_distance

            tp = (
                price
                + stop_distance
                * TP_R_MULTIPLIER
            )

        else:

            stop = price + stop_distance

            tp = (
                price
                - stop_distance
                * TP_R_MULTIPLIER
            )

        stop = float(
            exchange.price_to_precision(
                symbol,
                stop
            )
        )

        tp = float(
            exchange.price_to_precision(
                symbol,
                tp
            )
        )

        if PAPER_TRADING:

            telegram(
                f"📝 PAPER TRADE\n\n"
                f"{symbol}\n"
                f"{signal['side'].upper()}\n"
                f"Score: {signal['score']}\n"
                f"Entry: ${price:.6g}\n"
                f"SL: ${stop:.6g}\n"
                f"TP: ${tp:.6g}\n"
                f"Margin: ${margin:.2f}"
            )

            positions[symbol] = {
                "side": signal["side"],
                "entry": price,
                "stop": stop,
                "tp": tp,
                "contracts": contracts,
                "margin": margin,
                "score": signal["score"],
                "strategy": signal["strategies"],
                "opened": time.time()
            }

            set_cooldown(symbol)

            return True

        # ----------------------------------------------------
        # LIVE
        # ----------------------------------------------------

        telegram(
            f"🟡 LIVE ORDER\n\n"
            f"{symbol}\n"
            f"{signal['side'].upper()}\n"
            f"Score: {signal['score']}\n"
            f"Entry ~ ${price:.6g}\n"
            f"SL: ${stop:.6g}\n"
            f"TP: ${tp:.6g}\n"
            f"Margin: ${margin:.2f}\n"
            f"Leverage: {LEVERAGE}x"
        )

        try:

            result = place_live_order(
                signal,
                contracts,
                stop,
                tp
            )

        except Exception as e:

            telegram(
                f"🔴 ORDER FAILED\n\n"
                f"{symbol}\n"
                f"{e}"
            )

            print(
                traceback.format_exc()
            )

            return False

        telegram(
            f"🟢 LIVE TRADE OPENED\n\n"
            f"{symbol}\n"
            f"{signal['side'].upper()}\n"
            f"Score: {signal['score']}\n"
            f"SL: ${stop:.6g}\n"
            f"TP: ${tp:.6g}\n\n"
            f"BloFin protection: ✅"
        )

        positions[symbol] = {
            "side": signal["side"],
            "entry": price,
            "stop": stop,
            "tp": tp,
            "contracts": contracts,
            "margin": margin,
            "score": signal["score"],
            "strategy": signal["strategies"],
            "order": result,
            "opened": time.time()
        }

        set_cooldown(symbol)

        return True


# ============================================================
# LEARNING
# ============================================================

def load_learning():

    global learning

    try:

        if os.path.exists(learning_file):

            with open(
                learning_file,
                "r",
                encoding="utf-8"
            ) as f:

                learning = json.load(f)

    except Exception as e:

        print(
            "Learning load error:",
            e
        )


def save_learning():

    try:

        with open(
            learning_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                learning,
                f,
                indent=2
            )

    except Exception as e:

        print(
            "Learning save error:",
            e
        )


# ============================================================
# MAIN SCAN
# ============================================================

def scan():

    equity = fetch_equity()

    print(
        f"\n[{datetime.now().strftime('%H:%M:%S')}] "
        f"Equity ${equity:.2f}"
    )

    if not risk_allowed(equity):
        return

    current_positions = active_position_count()

    if current_positions >= MAX_ACTIVE_POSITIONS:

        print(
            "Max positions bereikt."
        )

        return

    candidates = []

    for symbol in SYMBOLS:

        if cooldown_active(symbol):
            continue

        if symbol in positions:
            continue

        signal = score_symbol(symbol)

        if signal:

            candidates.append(signal)

        time.sleep(0.15)

    if not candidates:
        print(
            "Geen geldige setups."
        )
        return

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    print("\nCANDIDATES")

    for candidate in candidates[:10]:

        print(
            candidate["symbol"],
            candidate["side"],
            candidate["score"],
            candidate["strategies"]
        )

    # --------------------------------------------------------
    # Alleen de beste setups uitvoeren
    # --------------------------------------------------------

    slots = (
        MAX_ACTIVE_POSITIONS
        - active_position_count()
    )

    for signal in candidates:

        if slots <= 0:
            break

        if signal["score"] < MIN_SCORE:
            continue

        success = execute_trade(
            signal,
            equity
        )

        if success:
            slots -= 1


# ============================================================
# HEALTH
# ============================================================

def health_check():

    try:

        equity = fetch_equity()

        positions_live = (
            active_position_count()
        )

        print(
            f"HEALTH | "
            f"Equity=${equity:.2f} | "
            f"Positions={positions_live}"
        )

    except Exception as e:

        telegram(
            f"🔴 BLOFIN HEALTH ERROR\n\n{e}"
        )


# ============================================================
# STARTUP
# ============================================================

def startup():

    print("=" * 60)
    print(
        f"BLOFIN TACTICAL ENGINE {VERSION}"
    )
    print("=" * 60)

    print(
        "PAPER_TRADING:",
        PAPER_TRADING
    )

    print(
        "LIVE_CONFIRM:",
        LIVE_CONFIRM
    )

    print(
        "Markets:",
        len(SYMBOLS)
    )

    # API test
    try:

        equity = fetch_equity()

        print(
            f"BloFin API OK — Equity ${equity:.2f}"
        )

    except Exception as e:

        telegram(
            f"🔴 STARTUP FAILED\n\n"
            f"BloFin API:\n{e}"
        )

        raise

    # Telegram
    telegram_test()

    telegram(
        f"🚀 BOT READY\n\n"
        f"Mode: "
        f"{'PAPER' if PAPER_TRADING else 'LIVE'}\n"
        f"Equity: ${equity:.2f}\n"
        f"Markets: {len(SYMBOLS)}\n"
        f"Max positions: {MAX_ACTIVE_POSITIONS}\n"
        f"Leverage: {LEVERAGE}x\n\n"
        f"Scanning..."
    )


# ============================================================
# LOOP
# ============================================================

def main():

    load_learning()

    startup()

    last_health = 0

    while True:

        try:

            now = time.time()

            if now - last_health >= 300:

                health_check()

                last_health = now

            scan()

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:

            telegram(
                "🔴 BLOFIN BOT STOPPED"
            )

            break

        except Exception as e:

            print(
                traceback.format_exc()
            )

            telegram(
                f"🔴 BOT ERROR\n\n"
                f"{type(e).__name__}\n"
                f"{e}"
            )

            time.sleep(30)


if __name__ == "__main__":
    main()
