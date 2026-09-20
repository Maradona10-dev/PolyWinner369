import os
import time
import json
import uuid
import traceback
import threading
from datetime import datetime

import requests
import ccxt


# ============================================================
# BLOFIN TACTICAL ENGINE V6.1
# REAL LIVE TRADING
# ============================================================

VERSION = "6.1-LIVE"


# ============================================================
# ENVIRONMENT
# ============================================================

PAPER_TRADING = (
    os.getenv("PAPER_TRADING", "false").lower() == "true"
)

LIVE_CONFIRM = os.getenv(
    "LIVE_CONFIRM",
    ""
)

if not PAPER_TRADING:
    if LIVE_CONFIRM != "YES_I_WANT_LIVE_TRADING":
        raise RuntimeError(
            "\n"
            "LIVE TRADING BLOCKED.\n"
            "Set:\n"
            "LIVE_CONFIRM=YES_I_WANT_LIVE_TRADING\n"
        )


# ============================================================
# API
# ============================================================

API_KEY = os.getenv("EXCHANGE_API_KEY", "")
API_SECRET = os.getenv("EXCHANGE_SECRET", "")
API_PASSWORD = os.getenv("EXCHANGE_PASSWORD", "")

if not API_KEY:
    raise RuntimeError("EXCHANGE_API_KEY ontbreekt")

if not API_SECRET:
    raise RuntimeError("EXCHANGE_SECRET ontbreekt")

if not API_PASSWORD:
    raise RuntimeError("EXCHANGE_PASSWORD ontbreekt")


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
)


# ============================================================
# ACCOUNT / RISK
# ============================================================

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


# ============================================================
# TRADE MANAGEMENT
# ============================================================

STOP_ATR_MULTIPLIER = 1.25

TP_R_MULTIPLIER = 1.80

BREAKEVEN_BUFFER_PCT = 0.0015

TRAILING_ATR_MULTIPLIER = 1.10

USE_BREAKEVEN = True
USE_TRAILING = True


# ============================================================
# SCANNER
# ============================================================

SCAN_INTERVAL = 45

SCAN_TIMEFRAME = "15m"
CONFIRM_TIMEFRAME = "5m"
TREND_TIMEFRAME = "1h"

MIN_SCORE = 54
STRONG_SCORE = 68

ATR_MIN_PCT = 0.20
ATR_MAX_PCT = 5.00

VOLUME_RATIO_MIN = 1.05


# ============================================================
# COOLDOWNS
# ============================================================

SYMBOL_COOLDOWN_SECONDS = 15 * 60
LOSS_COOLDOWN_SECONDS = 30 * 60


# ============================================================
# SYMBOLS
# ============================================================

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

local_positions = {}

symbol_cooldowns = {}

daily_start_equity = None

consecutive_losses = 0

last_health_message = 0

learning_file = "bot_learning_v6_1.json"

learning = {
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "net": 0.0,
    "strategies": {}
}


# ============================================================
# EXCHANGE
# ============================================================

exchange = ccxt.blofin({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "password": API_PASSWORD,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
    }
})


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        print(
            "Telegram niet geconfigureerd:",
            message
        )

        return False

    try:

        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/sendMessage"
        )

        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=10
        )

        if response.status_code != 200:

            print(
                "Telegram error:",
                response.text
            )

            return False

        return True

    except Exception as e:

        print(
            "Telegram exception:",
            e
        )

        return False


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
            (price - result)
            * multiplier
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

        previous_close = candles[i - 1][4]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        trs.append(tr)

    return sum(
        trs[-period:]
    ) / period


def rsi(closes, period=14):

    if len(closes) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(closes)):

        difference = (
            closes[i]
            - closes[i - 1]
        )

        gains.append(
            max(difference, 0)
        )

        losses.append(
            max(-difference, 0)
        )

    avg_gain = (
        sum(gains[-period:])
        / period
    )

    avg_loss = (
        sum(losses[-period:])
        / period
    )

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


# ============================================================
# MARKET DATA
# ============================================================

def get_candles(
    symbol,
    timeframe,
    limit=120
):

    return exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=limit
    )


def get_ticker(symbol):

    return exchange.fetch_ticker(
        symbol
    )


# ============================================================
# REGIME
# ============================================================

def detect_regime(candles_1h):

    closes = [
        x[4]
        for x in candles_1h
    ]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)

    if not e20 or not e50 or not e200:
        return "TRANSITION"

    price = closes[-1]

    if (
        e20 > e50 > e200
        and price > e20
    ):

        return "TREND_UP"

    if (
        e20 < e50 < e200
        and price < e20
    ):

        return "TREND_DOWN"

    recent = closes[-20:]

    high = max(recent)
    low = min(recent)

    if low > 0:

        range_pct = (
            (high - low)
            / low
            * 100
        )

    else:

        range_pct = 0

    if range_pct < 6:

        return "RANGE"

    return "TRANSITION"


# ============================================================
# SCORE
# ============================================================

def score_symbol(symbol):

    try:

        c15 = get_candles(
            symbol,
            SCAN_TIMEFRAME,
            120
        )

        c5 = get_candles(
            symbol,
            CONFIRM_TIMEFRAME,
            80
        )

        c1h = get_candles(
            symbol,
            TREND_TIMEFRAME,
            220
        )

        if len(c15) < 60:
            return None

        closes15 = [
            x[4]
            for x in c15
        ]

        volumes15 = [
            x[5]
            for x in c15
        ]

        price = closes15[-1]

        e20 = ema(
            closes15,
            20
        )

        e50 = ema(
            closes15,
            50
        )

        atr_value = atr(
            c15,
            14
        )

        if not e20 or not e50 or not atr_value:
            return None

        atr_pct = (
            atr_value
            / price
            * 100
        )

        if atr_pct < ATR_MIN_PCT:
            return None

        if atr_pct > ATR_MAX_PCT:
            return None

        volume_average = sma(
            volumes15[:-1],
            20
        )

        if not volume_average:
            return None

        volume_ratio = (
            volumes15[-1]
            / volume_average
        )

        regime = detect_regime(
            c1h
        )

        long_score = 0
        short_score = 0

        long_strategies = []
        short_strategies = []

        # ----------------------------------------------------
        # TREND
        # ----------------------------------------------------

        if regime == "TREND_UP":

            long_score += 15

            long_strategies.append(
                "TREND"
            )

        elif regime == "TREND_DOWN":

            short_score += 15

            short_strategies.append(
                "TREND"
            )

        # ----------------------------------------------------
        # EMA
        # ----------------------------------------------------

        if e20 > e50:

            long_score += 10

            long_strategies.append(
                "EMA"
            )

        elif e20 < e50:

            short_score += 10

            short_strategies.append(
                "EMA"
            )

        # ----------------------------------------------------
        # PRICE / EMA
        # ----------------------------------------------------

        if price > e20:

            long_score += 7

        elif price < e20:

            short_score += 7

        # ----------------------------------------------------
        # VOLUME
        # ----------------------------------------------------

        if volume_ratio >= VOLUME_RATIO_MIN:

            long_score += 8
            short_score += 8

        # ----------------------------------------------------
        # DONCHIAN BREAKOUT
        # ----------------------------------------------------

        previous_high = max(
            x[2]
            for x in c15[-21:-1]
        )

        previous_low = min(
            x[3]
            for x in c15[-21:-1]
        )

        if price > previous_high:

            long_score += 15

            long_strategies.append(
                "MOMENTUM_BREAKOUT"
            )

        if price < previous_low:

            short_score += 15

            short_strategies.append(
                "MOMENTUM_BREAKOUT"
            )

        # ----------------------------------------------------
        # LIQUIDITY SWEEP
        # ----------------------------------------------------

        last_candle = c15[-1]

        high = last_candle[2]
        low = last_candle[3]
        close = last_candle[4]

        range_high = max(
            x[2]
            for x in c15[-13:-1]
        )

        range_low = min(
            x[3]
            for x in c15[-13:-1]
        )

        if (
            low < range_low
            and close > range_low
        ):

            long_score += 12

            long_strategies.append(
                "LIQUIDITY_SWEEP"
            )

        if (
            high > range_high
            and close < range_high
        ):

            short_score += 12

            short_strategies.append(
                "LIQUIDITY_SWEEP"
            )

        # ----------------------------------------------------
        # 5M CONFIRMATION
        # ----------------------------------------------------

        closes5 = [
            x[4]
            for x in c5
        ]

        e20_5 = ema(
            closes5,
            20
        )

        e50_5 = ema(
            closes5,
            50
        )

        rsi5 = rsi(
            closes5
        )

        if (
            e20_5
            and e50_5
            and rsi5
        ):

            if (
                e20_5 > e50_5
                and rsi5 > 52
            ):

                long_score += 10

            if (
                e20_5 < e50_5
                and rsi5 < 48
            ):

                short_score += 10

        # ----------------------------------------------------
        # ATR QUALITY
        # ----------------------------------------------------

        if 0.35 <= atr_pct <= 2.5:

            long_score += 5
            short_score += 5

        # ----------------------------------------------------
        # FINAL DIRECTION
        # ----------------------------------------------------

        if long_score >= short_score:

            side = "long"
            score = long_score
            strategies = long_strategies

        else:

            side = "short"
            score = short_score
            strategies = short_strategies

        if score < MIN_SCORE:
            return None

        return {
            "symbol": symbol,
            "side": side,
            "score": score,
            "strategies": strategies,
            "regime": regime,
            "price": price,
            "atr": atr_value,
            "atr_pct": atr_pct,
            "volume_ratio": volume_ratio
        }

    except Exception as e:

        print(
            f"[SCAN ERROR] "
            f"{symbol}: {e}"
        )

        return None


# ============================================================
# BALANCE
# ============================================================

def fetch_equity():

    balance = exchange.fetch_balance({
        "type": "swap"
    })

    total = balance.get(
        "total",
        {}
    )

    free = balance.get(
        "free",
        {}
    )

    value = (
        total.get("USDT")
        or free.get("USDT")
    )

    if value is None:

        # Sommige BloFin responses
        # gebruiken USD
        value = (
            total.get("USD")
            or free.get("USD")
        )

    if value is None:

        raise RuntimeError(
            "USDT/USD equity niet gevonden"
        )

    return float(value)


# ============================================================
# POSITIONS
# ============================================================

def fetch_positions():

    return exchange.fetch_positions(
        None,
        {
            "type": "swap"
        }
    )


def real_positions():

    result = []

    try:

        data = fetch_positions()

        for position in data:

            contracts = (
                position.get("contracts")
                or 0
            )

            try:
                contracts = float(
                    contracts
                )
            except Exception:
                contracts = 0

            if abs(contracts) <= 0:
                continue

            result.append(
                position
            )

    except Exception as e:

        print(
            "Position fetch error:",
            e
        )

        raise

    return result


def active_position_count():

    return len(
        real_positions()
    )


def position_for_symbol(symbol):

    for position in real_positions():

        if (
            position.get("symbol")
            == symbol
        ):

            contracts = (
                position.get(
                    "contracts"
                )
                or 0
            )

            try:

                if abs(
                    float(contracts)
                ) > 0:

                    return position

            except Exception:
                pass

    return None


# ============================================================
# COOLDOWNS
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

    symbol_cooldowns[
        symbol
    ] = (
        time.time()
        + seconds
    )


# ============================================================
# RISK CHECK
# ============================================================

def risk_allowed(equity):

    global daily_start_equity

    if daily_start_equity is None:

        daily_start_equity = equity

    if equity < MIN_EQUITY_USDT:

        telegram(
            "🛑 BOT STOP\n\n"
            f"Equity ${equity:.2f}\n"
            f"Onder minimum ${MIN_EQUITY_USDT:.2f}"
        )

        return False

    drawdown = (
        daily_start_equity
        - equity
    ) / daily_start_equity

    if drawdown >= DAILY_LOSS_LIMIT_PCT:

        telegram(
            "🛑 DAILY LOSS LIMIT\n\n"
            f"Start: ${daily_start_equity:.2f}\n"
            f"Now: ${equity:.2f}\n"
            f"DD: {drawdown:.2%}\n\n"
            "Nieuwe trades geblokkeerd."
        )

        return False

    if (
        consecutive_losses
        >= MAX_CONSECUTIVE_LOSSES
    ):

        telegram(
            "🛑 LOSS STREAK\n\n"
            f"{consecutive_losses} "
            "verliestrades.\n\n"
            "Nieuwe trades geblokkeerd."
        )

        return False

    return True


# ============================================================
# POSITION SIZE
# ============================================================

def calculate_size(
    signal,
    equity
):

    symbol = signal["symbol"]

    price = signal["price"]

    atr_value = signal["atr"]

    stop_distance = (
        atr_value
        * STOP_ATR_MULTIPLIER
    )

    if stop_distance <= 0:
        return None

    risk_budget = (
        equity
        * MAX_RISK_EQUITY_PCT
    )

    notional_by_risk = (
        risk_budget
        / (stop_distance / price)
    )

    margin = (
        notional_by_risk
        / LEVERAGE
    )

    margin = max(
        MIN_MARGIN_USDT,
        min(
            margin,
            MAX_MARGIN_USDT,
            equity
            * MAX_MARGIN_EQUITY_PCT
        )
    )

    notional = (
        margin
        * LEVERAGE
    )

    market = exchange.market(
        symbol
    )

    contract_size = float(
        market.get(
            "contractSize",
            1
        )
        or 1
    )

    raw_contracts = (
        notional
        / price
        / contract_size
    )

    try:

        contracts = float(
            exchange.amount_to_precision(
                symbol,
                raw_contracts
            )
        )

    except Exception:

        return None

    minimum = (
        market.get("limits", {})
        .get("amount", {})
        .get("min")
    )

    if minimum:

        if contracts < float(minimum):

            return None

    if contracts <= 0:
        return None

    return contracts, margin


# ============================================================
# LEVERAGE
# ============================================================

def ensure_leverage(symbol):

    try:

        exchange.set_leverage(
            LEVERAGE,
            symbol
        )

        return True

    except Exception as e:

        print(
            f"Leverage warning "
            f"{symbol}: {e}"
        )

        # Niet automatisch doorgaan
        return False


# ============================================================
# BLOFIN NATIVE TPSL
# ============================================================

def place_tpsl(
    signal,
    contracts,
    stop_price,
    tp_price
):

    market = exchange.market(
        signal["symbol"]
    )

    inst_id = market["id"]

    # Long -> SELL om positie te sluiten
    # Short -> BUY om positie te sluiten

    close_side = (
        "sell"
        if signal["side"] == "long"
        else "buy"
    )

    body = {
        "instId": inst_id,
        "marginMode": "isolated",
        "positionSide": "net",
        "side": close_side,

        "tpTriggerPrice": str(
            tp_price
        ),

        "tpOrderPrice": "-1",

        "tpTriggerPriceType": "last",

        "slTriggerPrice": str(
            stop_price
        ),

        "slOrderPrice": "-1",

        "slTriggerPriceType": "last",

        "size": str(
            contracts
        ),

        "reduceOnly": "true",

        "clientOrderId": (
            "V61"
            + uuid.uuid4().hex[:20]
        )
    }

    return exchange.private_post_trade_order_tpsl(
        body
    )


# ============================================================
# VERIFY TPSL
# ============================================================

def verify_tpsl(symbol):

    market = exchange.market(
        symbol
    )

    inst_id = market["id"]

    response = (
        exchange.private_get_trade_orders_tpsl_pending(
            {
                "instId": inst_id
            }
        )
    )

    data = response.get(
        "data",
        []
    )

    if isinstance(data, dict):

        data = [data]

    for order in data:

        state = str(
            order.get(
                "state",
                ""
            )
        ).lower()

        if state == "live":

            return order

    return None


# ============================================================
# EMERGENCY CLOSE
# ============================================================

def emergency_close(
    symbol
):

    try:

        position = position_for_symbol(
            symbol
        )

        if not position:

            return True

        market = exchange.market(
            symbol
        )

        inst_id = market["id"]

        response = (
            exchange.private_post_trade_close_position(
                {
                    "instId": inst_id,
                    "marginMode": "isolated",
                    "positionSide": "net",
                    "clientOrderId": (
                        "KILL"
                        + uuid.uuid4().hex[:20]
                    )
                }
            )
        )

        telegram(
            "🚨 EMERGENCY CLOSE\n\n"
            f"{symbol}\n"
            "Protection kon niet worden "
            "bevestigd.\n"
            "Positie gesloten."
        )

        return True

    except Exception as e:

        telegram(
            "🔥 CRITICAL ERROR\n\n"
            f"{symbol}\n"
            "Positie kon NIET automatisch "
            "worden gesloten.\n\n"
            f"{e}"
        )

        print(
            traceback.format_exc()
        )

        return False


# ============================================================
# OPEN LIVE TRADE
# ============================================================

def execute_trade(
    signal,
    equity
):

    symbol = signal["symbol"]

    with lock:

        # --------------------------------------------
        # Cooldown
        # --------------------------------------------

        if cooldown_active(symbol):
            return False

        # --------------------------------------------
        # Max positions
        # --------------------------------------------

        if (
            active_position_count()
            >= MAX_ACTIVE_POSITIONS
        ):

            return False

        # --------------------------------------------
        # Existing symbol position
        # --------------------------------------------

        if position_for_symbol(
            symbol
        ):

            return False

        # --------------------------------------------
        # Leverage
        # --------------------------------------------

        if not ensure_leverage(
            symbol
        ):

            telegram(
                "🟠 TRADE BLOCKED\n\n"
                f"{symbol}\n"
                "Leverage kon niet worden "
                "bevestigd."
            )

            return False

        # --------------------------------------------
        # Position sizing
        # --------------------------------------------

        sizing = calculate_size(
            signal,
            equity
        )

        if not sizing:

            print(
                "Sizing rejected:",
                symbol
            )

            return False

        contracts, margin = sizing

        price = signal["price"]

        stop_distance = (
            signal["atr"]
            * STOP_ATR_MULTIPLIER
        )

        if signal["side"] == "long":

            stop_price = (
                price
                - stop_distance
            )

            tp_price = (
                price
                + stop_distance
                * TP_R_MULTIPLIER
            )

            order_side = "buy"

        else:

            stop_price = (
                price
                + stop_distance
            )

            tp_price = (
                price
                - stop_distance
                * TP_R_MULTIPLIER
            )

            order_side = "sell"

        stop_price = float(
            exchange.price_to_precision(
                symbol,
                stop_price
            )
        )

        tp_price = float(
            exchange.price_to_precision(
                symbol,
                tp_price
            )
        )

        # --------------------------------------------
        # Telegram BEFORE order
        # --------------------------------------------

        telegram(
            "🟡 LIVE ORDER\n\n"
            f"{symbol}\n"
            f"{signal['side'].upper()}\n"
            f"Score: {signal['score']}\n"
            f"Regime: {signal['regime']}\n\n"
            f"Entry ~ ${price:.8g}\n"
            f"SL: ${stop_price:.8g}\n"
            f"TP: ${tp_price:.8g}\n\n"
            f"Margin: ${margin:.2f}\n"
            f"Leverage: {LEVERAGE}x\n"
            f"Strategy: "
            f"{', '.join(signal['strategies'])}"
        )

        # --------------------------------------------
        # LIVE ENTRY
        # --------------------------------------------

        try:

            order = exchange.create_order(
                symbol,
                "market",
                order_side,
                contracts,
                None,
                {
                    "marginMode": "isolated",
                    "positionSide": "net",
                    "clientOrderId": (
                        "V61"
                        + uuid.uuid4().hex[:20]
                    )
                }
            )

        except Exception as e:

            telegram(
                "🔴 ENTRY FAILED\n\n"
                f"{symbol}\n"
                f"{e}"
            )

            return False

        # --------------------------------------------
        # VERIFY ENTRY
        # --------------------------------------------

        position = None

        for _ in range(10):

            time.sleep(0.5)

            try:

                position = position_for_symbol(
                    symbol
                )

            except Exception:
                position = None

            if position:
                break

        if not position:

            telegram(
                "🔴 ENTRY UNCERTAIN\n\n"
                f"{symbol}\n"
                "Order geplaatst maar positie "
                "niet bevestigd.\n\n"
                "Bot gaat NIET verder."
            )

            return False

        # --------------------------------------------
        # Actual entry price
        # --------------------------------------------

        actual_entry = (
            position.get(
                "entryPrice"
            )
            or position.get(
                "average"
            )
            or price
        )

        try:

            actual_entry = float(
                actual_entry
            )

        except Exception:

            actual_entry = price

        # Recalculate protection
        if signal["side"] == "long":

            stop_price = (
                actual_entry
                - stop_distance
            )

            tp_price = (
                actual_entry
                + stop_distance
                * TP_R_MULTIPLIER
            )

        else:

            stop_price = (
                actual_entry
                + stop_distance
            )

            tp_price = (
                actual_entry
                - stop_distance
                * TP_R_MULTIPLIER
            )

        stop_price = float(
            exchange.price_to_precision(
                symbol,
                stop_price
            )
        )

        tp_price = float(
            exchange.price_to_precision(
                symbol,
                tp_price
            )
        )

        # --------------------------------------------
        # PLACE NATIVE TPSL
        # --------------------------------------------

        try:

            tpsl = place_tpsl(
                signal,
                contracts,
                stop_price,
                tp_price
            )

        except Exception as e:

            telegram(
                "🚨 PROTECTION FAILED\n\n"
                f"{symbol}\n\n"
                f"{e}\n\n"
                "EMERGENCY CLOSE."
            )

            emergency_close(
                symbol
            )

            return False

        # --------------------------------------------
        # VERIFY PROTECTION
        # --------------------------------------------

        protection = None

        for _ in range(10):

            time.sleep(0.5)

            try:

                protection = verify_tpsl(
                    symbol
                )

            except Exception:
                protection = None

            if protection:
                break

        if not protection:

            telegram(
                "🚨 TPSL NOT VERIFIED\n\n"
                f"{symbol}\n\n"
                "Positie wordt gesloten."
            )

            emergency_close(
                symbol
            )

            return False

        # --------------------------------------------
        # Store local state
        # --------------------------------------------

        local_positions[
            symbol
        ] = {
            "symbol": symbol,
            "side": signal["side"],
            "entry": actual_entry,
            "stop": stop_price,
            "tp": tp_price,
            "contracts": contracts,
            "margin": margin,
            "score": signal["score"],
            "strategies": signal[
                "strategies"
            ],
            "opened": time.time(),
            "tpsl": protection,
            "order": order
        }

        set_cooldown(
            symbol
        )

        telegram(
            "🟢 LIVE TRADE OPENED\n\n"
            f"{symbol}\n"
            f"{signal['side'].upper()}\n\n"
            f"Entry: ${actual_entry:.8g}\n"
            f"SL: ${stop_price:.8g}\n"
            f"TP: ${tp_price:.8g}\n\n"
            f"Score: {signal['score']}\n"
            f"Margin: ${margin:.2f}\n"
            f"Leverage: {LEVERAGE}x\n\n"
            "BloFin TPSL: ✅ VERIFIED"
        )

        return True


# ============================================================
# POSITION MONITOR
# ============================================================

def monitor_positions():

    try:

        live = {}

        for position in real_positions():

            symbol = position.get(
                "symbol"
            )

            if not symbol:
                continue

            live[symbol] = position

        # --------------------------------------------
        # Detect closed positions
        # --------------------------------------------

        for symbol in list(
            local_positions.keys()
        ):

            if symbol not in live:

                info = local_positions.pop(
                    symbol,
                    {}
                )

                telegram(
                    "🔵 POSITION CLOSED\n\n"
                    f"{symbol}\n"
                    f"Side: "
                    f"{str(info.get('side', '')).upper()}\n"
                    f"Score: "
                    f"{info.get('score', '-')}\n\n"
                    "BloFin meldt positie gesloten."
                )

        # --------------------------------------------
        # Check protection still exists
        # --------------------------------------------

        for symbol in live:

            if symbol not in local_positions:
                continue

            try:

                protection = verify_tpsl(
                    symbol
                )

                if not protection:

                    telegram(
                        "🚨 PROTECTION LOST\n\n"
                        f"{symbol}\n\n"
                        "TPSL is niet meer actief.\n"
                        "Emergency close."
                    )

                    emergency_close(
                        symbol
                    )

            except Exception as e:

                telegram(
                    "🟠 PROTECTION CHECK ERROR\n\n"
                    f"{symbol}\n"
                    f"{e}"
                )

    except Exception as e:

        print(
            "Monitor error:",
            e
        )


# ============================================================
# STARTUP
# ============================================================

def startup():

    global daily_start_equity

    print("=" * 65)
    print(
        f"BLOFIN TACTICAL ENGINE "
        f"{VERSION}"
    )
    print("=" * 65)

    print(
        "PAPER_TRADING:",
        PAPER_TRADING
    )

    print(
        "LIVE_CONFIRM:",
        LIVE_CONFIRM
    )

    print(
        "MAX POSITIONS:",
        MAX_ACTIVE_POSITIONS
    )

    print(
        "LEVERAGE:",
        LEVERAGE
    )

    print(
        "MARKETS:",
        len(SYMBOLS)
    )

    # --------------------------------------------
    # Load markets
    # --------------------------------------------

    exchange.load_markets()

    print(
        "Markets loaded:",
        len(exchange.markets)
    )

    # --------------------------------------------
    # API test
    # --------------------------------------------

    try:

        equity = fetch_equity()

        daily_start_equity = equity

        print(
            f"BloFin API OK "
            f"| Equity ${equity:.2f}"
        )

    except Exception as e:

        telegram(
            "🔴 STARTUP FAILED\n\n"
            f"BloFin API:\n{e}"
        )

        raise

    # --------------------------------------------
    # Position test
    # --------------------------------------------

    try:

        current = active_position_count()

        print(
            "Current positions:",
            current
        )

        if current > 0:

            telegram(
                "⚠️ EXISTING POSITIONS\n\n"
                f"{current} positie(s) "
                "gevonden op BloFin.\n\n"
                "Bot neemt bestaande posities "
                "niet over."
            )

    except Exception as e:

        telegram(
            "🔴 POSITION API FAILED\n\n"
            f"{e}"
        )

        raise

    # --------------------------------------------
    # Telegram
    # --------------------------------------------

    mode = (
        "PAPER"
        if PAPER_TRADING
        else "🔴 LIVE"
    )

    telegram(
        f"🟢 BLOFIN TACTICAL {VERSION}\n\n"
        f"Mode: {mode}\n"
        f"Equity: ${equity:.2f}\n"
        f"Leverage: {LEVERAGE}x\n"
        f"Max positions: "
        f"{MAX_ACTIVE_POSITIONS}\n"
        f"Markets: {len(SYMBOLS)}\n\n"
        "BloFin API: ✅\n"
        "Position API: ✅\n"
        "Telegram: ✅\n"
        "Protection: native BloFin\n\n"
        "SCANNER ONLINE"
    )


# ============================================================
# SCANNER
# ============================================================

def scan():

    equity = fetch_equity()

    print(
        f"\n"
        f"[{datetime.now().strftime('%H:%M:%S')}] "
        f"Equity ${equity:.2f}"
    )

    if not risk_allowed(
        equity
    ):

        return

    current_positions = (
        active_position_count()
    )

    if (
        current_positions
        >= MAX_ACTIVE_POSITIONS
    ):

        print(
            "Max positions bereikt."
        )

        return

    candidates = []

    # --------------------------------------------------------
    # Scan
    # --------------------------------------------------------

    for symbol in SYMBOLS:

        if cooldown_active(symbol):
            continue

        if symbol in local_positions:
            continue

        signal = score_symbol(
            symbol
        )

        if signal:

            candidates.append(
                signal
            )

        time.sleep(0.12)

    # --------------------------------------------------------
    # Nothing found
    # --------------------------------------------------------

    if not candidates:

        print(
            "Geen geldige setups."
        )

        return

    # --------------------------------------------------------
    # Rank
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    print(
        "\nTOP CANDIDATES"
    )

    for candidate in candidates[:10]:

        print(
            f"{candidate['symbol']} "
            f"{candidate['side']} "
            f"score={candidate['score']} "
            f"{candidate['strategies']}"
        )

    # --------------------------------------------------------
    # Execute best available
    # --------------------------------------------------------

    slots = (
        MAX_ACTIVE_POSITIONS
        - active_position_count()
    )

    for signal in candidates:

        if slots <= 0:
            break

        if (
            signal["score"]
            < MIN_SCORE
        ):

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

def health():

    try:

        equity = fetch_equity()

        positions = (
            active_position_count()
        )

        print(
            f"HEALTH | "
            f"Equity=${equity:.2f} | "
            f"Positions={positions}"
        )

    except Exception as e:

        telegram(
            "🔴 HEALTH ERROR\n\n"
            f"{e}"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    startup()

    last_health = 0
    last_monitor = 0

    while True:

        try:

            now = time.time()

            # ----------------------------------------
            # Position monitor every 15 sec
            # ----------------------------------------

            if now - last_monitor >= 15:

                monitor_positions()

                last_monitor = now

            # ----------------------------------------
            # Health every 5 min
            # ----------------------------------------

            if now - last_health >= 300:

                health()

                last_health = now

            # ----------------------------------------
            # Scan
            # ----------------------------------------

            scan()

            time.sleep(
                SCAN_INTERVAL
            )

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
                "🔴 BOT ERROR\n\n"
                f"{type(e).__name__}\n"
                f"{e}\n\n"
                "Bot blijft proberen."
            )

            time.sleep(30)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
