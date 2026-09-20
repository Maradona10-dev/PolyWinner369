# ============================================================
# BLOFIN TACTICAL ENGINE V5.0
# ============================================================
#
# Multi-regime / multi-strategy / adaptive crypto futures bot
#
# Strategies:
#   1. MOMENTUM_BREAKOUT
#   2. LIQUIDITY_SWEEP
#   3. OI_DIVERGENCE
#   4. TAKER_MOMENTUM
#
# Adaptive learning:
#   - Persists strategy statistics to JSON
#   - Tracks rolling results
#   - Adjusts strategy weights gradually
#   - Never changes aggressively after only a few trades
#
# Risk:
#   - Risk based position sizing
#   - Max active positions
#   - Daily loss limit
#   - Consecutive loss protection
#   - Symbol cooldown
#   - Cluster exposure protection
#
# Execution:
#   - BloFin perpetual swaps through CCXT
#   - Market entries
#   - Separate SL / TP trigger orders
#   - TP1 partial close
#   - Break-even
#   - ATR trailing runner
#
# IMPORTANT:
# Test PAPER_TRADING=true first.
# ============================================================

import os
import json
import time
import math
import traceback
import threading
from datetime import datetime, timezone
from collections import defaultdict, deque

import ccxt
import pandas as pd
import numpy as np


# ============================================================
# CONFIG
# ============================================================

EXCHANGE_ID = "blofin"

API_KEY = os.getenv("EXCHANGE_API_KEY", "")
API_SECRET = os.getenv("EXCHANGE_SECRET", "")
API_PASSWORD = os.getenv("EXCHANGE_PASSWORD", "")

PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"

# ------------------------------------------------------------
# Telegram
# ------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ------------------------------------------------------------
# Symbols
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

BTC_SYMBOL = "BTC/USDT:USDT"

# ------------------------------------------------------------
# Timeframes
# ------------------------------------------------------------

TF_FAST = "5m"
TF_SIGNAL = "15m"
TF_TREND = "1h"

# ------------------------------------------------------------
# Engine timing
# ------------------------------------------------------------

SCAN_INTERVAL_SECONDS = 45
SYMBOL_DELAY_SECONDS = 0.20

POSITION_SYNC_SECONDS = 45
MANAGEMENT_INTERVAL_SECONDS = 20

# ------------------------------------------------------------
# Positions
# ------------------------------------------------------------

MAX_ACTIVE_POSITIONS = 2

# Do NOT artificially limit trades to one per hour.
GLOBAL_ENTRY_COOLDOWN_SECONDS = 0

SYMBOL_COOLDOWN_SECONDS = 15 * 60
LOSS_COOLDOWN_SECONDS = 30 * 60

# ------------------------------------------------------------
# Leverage / sizing
# ------------------------------------------------------------

LEVERAGE = 10

MIN_MARGIN_USDT = 5.0
BASE_MARGIN_USDT = 7.5
MAX_MARGIN_USDT = 12.0

MAX_MARGIN_EQUITY_PCT = 0.18
MAX_TOTAL_MARGIN_EQUITY_PCT = 0.38

# Actual risk to equity per trade.
MAX_RISK_EQUITY_PCT = 0.015

# ------------------------------------------------------------
# Profit objectives
# ------------------------------------------------------------

MIN_PROFIT_TARGET_USDT = 2.50
IDEAL_PROFIT_TARGET_USDT = 5.00
MAX_PROFIT_TARGET_USDT = 10.00

# These are NOT guaranteed targets.
# They are used as an expected-value filter.

# ------------------------------------------------------------
# Stops / targets
# ------------------------------------------------------------

ATR_SL_MULTIPLIER = 1.25

TP1_R = 0.90
TP2_R = 1.80

TP1_CLOSE_PCT = 0.60

BREAK_EVEN_BUFFER_PCT = 0.0015

TRAIL_ATR_MULTIPLIER = 1.10

# ------------------------------------------------------------
# Signal thresholds
# ------------------------------------------------------------

MIN_SCORE = 54
STRONG_SCORE = 68

# ------------------------------------------------------------
# Market filters
# ------------------------------------------------------------

MIN_ATR_PCT = 0.20
MAX_ATR_PCT = 5.00

MAX_SPREAD_PCT = 0.30

MIN_VOLUME_RATIO = 1.05

# ------------------------------------------------------------
# Learning
# ------------------------------------------------------------

LEARNING_FILE = "bot_learning_v5.json"

LEARNING_MIN_TRADES = 15

LEARNING_WINDOW = 100

WEIGHT_MIN = 0.65
WEIGHT_MAX = 1.35

# Learning is intentionally slow.
LEARNING_RATE = 0.08

# ------------------------------------------------------------
# Safety
# ------------------------------------------------------------

DAILY_LOSS_LIMIT_PCT = 0.07

MAX_CONSECUTIVE_LOSSES = 3

MIN_EQUITY_USDT = 30.0

# ------------------------------------------------------------
# Execution assumptions
# ------------------------------------------------------------

ESTIMATED_TAKER_FEE_PCT = 0.0006
ESTIMATED_SLIPPAGE_PCT = 0.0005

ROUND_TRIP_COST_PCT = (
    ESTIMATED_TAKER_FEE_PCT * 2
    + ESTIMATED_SLIPPAGE_PCT * 2
)

# ------------------------------------------------------------
# API caching
# ------------------------------------------------------------

BTC_CACHE_SECONDS = 45

OI_CACHE_SECONDS = 60

FUNDING_CACHE_SECONDS = 120

TRADE_CACHE_SECONDS = 30

# ============================================================
# HELPERS
# ============================================================

def now_ts():
    return time.time()


def utc_now():
    return datetime.now(timezone.utc)


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def pct_change(a, b):
    if not a:
        return 0.0
    return (b - a) / a


def normalize_side(side):
    if side in ("long", "buy"):
        return "long"
    return "short"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        import requests

        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=10,
        )

    except Exception as e:
        print("Telegram error:", e)


# ============================================================
# INDICATORS
# ============================================================

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def atr(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.rolling(period).mean()


def rsi(series, period=14):
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    return 100 - (100 / (1 + rs))


def volume_ratio(df, period=20):
    avg = df["volume"].rolling(period).mean()
    return df["volume"] / avg.replace(0, np.nan)


def donchian(df, period=20):
    high = df["high"].rolling(period).max()
    low = df["low"].rolling(period).min()

    return high, low


def candle_body_ratio(row):
    rng = row["high"] - row["low"]

    if rng <= 0:
        return 0.0

    return abs(row["close"] - row["open"]) / rng


def upper_wick_ratio(row):
    rng = row["high"] - row["low"]

    if rng <= 0:
        return 0.0

    upper = row["high"] - max(
        row["open"],
        row["close"]
    )

    return upper / rng


def lower_wick_ratio(row):
    rng = row["high"] - row["low"]

    if rng <= 0:
        return 0.0

    lower = min(
        row["open"],
        row["close"]
    ) - row["low"]

    return lower / rng


# ============================================================
# MARKET REGIME
# ============================================================

def detect_regime(df_1h, df_15m):

    if len(df_1h) < 220 or len(df_15m) < 80:
        return "UNKNOWN"

    d1 = df_1h.copy()
    d15 = df_15m.copy()

    d1["ema20"] = ema(d1["close"], 20)
    d1["ema50"] = ema(d1["close"], 50)
    d1["ema200"] = ema(d1["close"], 200)

    d15["ema20"] = ema(d15["close"], 20)
    d15["ema50"] = ema(d15["close"], 50)
    d15["atr"] = atr(d15)

    last = d1.iloc[-1]
    prev = d1.iloc[-5]

    atr15 = d15["atr"].iloc[-1]
    price15 = d15["close"].iloc[-1]

    if not np.isfinite(atr15) or price15 <= 0:
        return "UNKNOWN"

    atr_pct = atr15 / price15 * 100

    trend_up = (
        last["ema20"] > last["ema50"]
        > last["ema200"]
    )

    trend_down = (
        last["ema20"] < last["ema50"]
        < last["ema200"]
    )

    slope = pct_change(
        prev["ema20"],
        last["ema20"]
    )

    ema_spread = abs(
        last["ema20"] - last["ema50"]
    ) / last["close"]

    # Extreme volatility.
    if atr_pct > 4.0:
        return "CHAOS"

    # Strong directional environment.
    if trend_up and slope > 0.001:
        return "TREND_UP"

    if trend_down and slope < -0.001:
        return "TREND_DOWN"

    # Tight moving average structure.
    if ema_spread < 0.0025:
        return "RANGE"

    return "TRANSITION"


# ============================================================
# EXCHANGE
# ============================================================

class BlofinEngine:

    def __init__(self):

        self.lock = threading.RLock()

        self.exchange = ccxt.blofin({
            "apiKey": API_KEY,
            "secret": API_SECRET,
            "password": API_PASSWORD,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
            },
        })

        self.exchange.load_markets()

        self.positions = {}

        self.symbol_cooldowns = {}

        self.loss_cooldowns = {}

        self.last_global_entry = 0

        self.last_position_sync = 0

        self.last_management = 0

        self.last_daily_reset = utc_now().date()

        self.day_start_equity = None

        self.daily_realized_pnl = 0.0

        self.consecutive_losses = 0

        self.paper_equity = 64.0

        self.paper_positions = {}

        self.oi_cache = {}

        self.funding_cache = {}

        self.trade_cache = {}

        self.btc_cache = None

        self.learning = self.load_learning()

        self.validate_symbols()

        self.configure_leverage()

    # ========================================================
    # LEARNING
    # ========================================================

    def default_learning(self):

        strategies = [
            "MOMENTUM_BREAKOUT",
            "LIQUIDITY_SWEEP",
            "OI_DIVERGENCE",
            "TAKER_MOMENTUM",
        ]

        return {
            "version": 5,
            "created": utc_now().isoformat(),
            "strategies": {
                s: {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "gross_profit": 0.0,
                    "gross_loss": 0.0,
                    "net_pnl": 0.0,
                    "avg_r": 0.0,
                    "weight": 1.0,
                    "recent": [],
                }
                for s in strategies
            },
        }

    def load_learning(self):

        try:

            if not os.path.exists(LEARNING_FILE):
                return self.default_learning()

            with open(
                LEARNING_FILE,
                "r",
                encoding="utf-8"
            ) as f:
                data = json.load(f)

            default = self.default_learning()

            for strategy in default["strategies"]:

                if strategy not in data.get(
                    "strategies",
                    {}
                ):
                    data.setdefault(
                        "strategies",
                        {}
                    )[strategy] = default[
                        "strategies"
                    ][strategy]

            return data

        except Exception as e:

            print(
                "Learning file error:",
                e
            )

            return self.default_learning()

    def save_learning(self):

        try:

            tmp = LEARNING_FILE + ".tmp"

            with open(
                tmp,
                "w",
                encoding="utf-8"
            ) as f:
                json.dump(
                    self.learning,
                    f,
                    indent=2
                )

            os.replace(
                tmp,
                LEARNING_FILE
            )

        except Exception as e:
            print(
                "Could not save learning:",
                e
            )

    def strategy_weight(self, strategy):

        try:
            return clamp(
                float(
                    self.learning[
                        "strategies"
                    ][strategy]["weight"]
                ),
                WEIGHT_MIN,
                WEIGHT_MAX,
            )

        except Exception:
            return 1.0

    def learn_trade(
        self,
        strategy,
        pnl,
        r_multiple,
    ):

        if strategy not in self.learning[
            "strategies"
        ]:
            return

        s = self.learning[
            "strategies"
        ][strategy]

        s["trades"] += 1

        if pnl > 0:
            s["wins"] += 1
            s["gross_profit"] += pnl
        else:
            s["losses"] += 1
            s["gross_loss"] += abs(pnl)

        s["net_pnl"] += pnl

        recent = s.setdefault(
            "recent",
            []
        )

        recent.append({
            "time": utc_now().isoformat(),
            "pnl": pnl,
            "r": r_multiple,
        })

        if len(recent) > LEARNING_WINDOW:
            del recent[
                :-LEARNING_WINDOW
            ]

        values = [
            x["r"]
            for x in recent
        ]

        if values:
            s["avg_r"] = (
                sum(values) / len(values)
            )

        # ----------------------------------------------------
        # Slow adaptive learning.
        # ----------------------------------------------------

        if s["trades"] >= LEARNING_MIN_TRADES:

            win_rate = (
                s["wins"] / s["trades"]
            )

            avg_r = s["avg_r"]

            # Positive expectancy.
            if avg_r > 0.15 and win_rate >= 0.42:

                s["weight"] += LEARNING_RATE * 0.5

            # Weak expectancy.
            elif avg_r < -0.10:

                s["weight"] -= LEARNING_RATE * 0.5

            s["weight"] = clamp(
                s["weight"],
                WEIGHT_MIN,
                WEIGHT_MAX
            )

        self.save_learning()

    # ========================================================
    # SYMBOLS
    # ========================================================

    def validate_symbols(self):

        available = set(
            self.exchange.symbols
        )

        valid = []

        for symbol in SYMBOLS:

            if symbol in available:
                valid.append(symbol)

        self.symbols = valid

        print(
            f"Loaded {len(self.symbols)} symbols."
        )

    # ========================================================
    # LEVERAGE
    # ========================================================

    def configure_leverage(self):

        if PAPER_TRADING:
            return

        for symbol in self.symbols:

            try:

                if hasattr(
                    self.exchange,
                    "set_leverage"
                ):

                    self.exchange.set_leverage(
                        LEVERAGE,
                        symbol
                    )

            except Exception as e:

                print(
                    f"Leverage {symbol}:",
                    e
                )

    # ========================================================
    # DATA
    # ========================================================

    def fetch_ohlcv(
        self,
        symbol,
        timeframe,
        limit=250
    ):

        raw = self.exchange.fetch_ohlcv(
            symbol,
            timeframe,
            limit=limit
        )

        if not raw:
            return None

        df = pd.DataFrame(
            raw,
            columns=[
                "timestamp",
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

        return df

    # ========================================================
    # BTC CACHE
    # ========================================================

    def fetch_btc_context(self):

        now = now_ts()

        if (
            self.btc_cache
            and now - self.btc_cache["time"]
            < BTC_CACHE_SECONDS
        ):
            return self.btc_cache

        try:

            df15 = self.fetch_ohlcv(
                BTC_SYMBOL,
                TF_SIGNAL,
                120
            )

            df1h = self.fetch_ohlcv(
                BTC_SYMBOL,
                TF_TREND,
                220
            )

            regime = detect_regime(
                df1h,
                df15
            )

            close = df15["close"]

            btc_change = pct_change(
                close.iloc[-12],
                close.iloc[-1]
            )

            result = {
                "time": now,
                "regime": regime,
                "change": btc_change,
                "df15": df15,
                "df1h": df1h,
            }

            self.btc_cache = result

            return result

        except Exception as e:

            print(
                "BTC context error:",
                e
            )

            return None

    # ========================================================
    # OPEN INTEREST
    # ========================================================

    def fetch_oi(self, symbol):

        now = now_ts()

        cached = self.oi_cache.get(symbol)

        if cached:
            if (
                now - cached["time"]
                < OI_CACHE_SECONDS
            ):
                return cached

        try:

            if not hasattr(
                self.exchange,
                "fetch_open_interest"
            ):
                return None

            current = (
                self.exchange.fetch_open_interest(
                    symbol
                )
            )

            oi = safe_float(
                current.get("openInterest")
            )

            if oi <= 0:
                oi = safe_float(
                    current.get("openInterestValue")
                )

            result = {
                "time": now,
                "oi": oi,
            }

            self.oi_cache[symbol] = result

            return result

        except Exception:

            return None

    # ========================================================
    # FUNDING
    # ========================================================

    def fetch_funding(self, symbol):

        now = now_ts()

        cached = self.funding_cache.get(
            symbol
        )

        if cached:

            if (
                now - cached["time"]
                < FUNDING_CACHE_SECONDS
            ):
                return cached

        try:

            if not hasattr(
                self.exchange,
                "fetch_funding_rate"
            ):
                return None

            data = (
                self.exchange.fetch_funding_rate(
                    symbol
                )
            )

            rate = safe_float(
                data.get("fundingRate")
            )

            result = {
                "time": now,
                "rate": rate,
            }

            self.funding_cache[symbol] = result

            return result

        except Exception:

            return None

    # ========================================================
    # TAKER PRESSURE
    # ========================================================

    def fetch_taker_pressure(
        self,
        symbol,
        limit=80
    ):

        now = now_ts()

        cached = self.trade_cache.get(
            symbol
        )

        if cached:

            if (
                now - cached["time"]
                < TRADE_CACHE_SECONDS
            ):
                return cached

        try:

            trades = self.exchange.fetch_trades(
                symbol,
                limit=limit
            )

            if not trades:
                return None

            buy_volume = 0.0
            sell_volume = 0.0
            total = 0.0

            for trade in trades:

                amount = safe_float(
                    trade.get("amount")
                )

                if amount <= 0:
                    continue

                side = trade.get("side")

                total += amount

                if side == "buy":
                    buy_volume += amount

                elif side == "sell":
                    sell_volume += amount

            if total <= 0:
                return None

            buy_ratio = (
                buy_volume / total
            )

            result = {
                "time": now,
                "buy_ratio": buy_ratio,
                "sell_ratio": 1 - buy_ratio,
                "total": total,
            }

            self.trade_cache[symbol] = result

            return result

        except Exception:

            return None

    # ========================================================
    # ORDERBOOK / SPREAD
    # ========================================================

    def spread_pct(self, symbol):

        try:

            book = self.exchange.fetch_order_book(
                symbol,
                limit=5
            )

            bid = safe_float(
                book["bids"][0][0]
            )

            ask = safe_float(
                book["asks"][0][0]
            )

            if bid <= 0 or ask <= 0:
                return 999

            mid = (bid + ask) / 2

            return (
                (ask - bid) / mid * 100
            )

        except Exception:

            return 999

    # ========================================================
    # RELATIVE STRENGTH
    # ========================================================

    def relative_strength(
        self,
        symbol_df,
        btc_df
    ):

        try:

            n = min(
                12,
                len(symbol_df) - 1,
                len(btc_df) - 1
            )

            if n <= 0:
                return 0.0

            asset_move = pct_change(
                symbol_df["close"].iloc[-1-n],
                symbol_df["close"].iloc[-1]
            )

            btc_move = pct_change(
                btc_df["close"].iloc[-1-n],
                btc_df["close"].iloc[-1]
            )

            return asset_move - btc_move

        except Exception:

            return 0.0

    # ========================================================
    # FEATURE ENGINE
    # ========================================================

    def enrich(self, df):

        df = df.copy()

        df["ema20"] = ema(
            df["close"],
            20
        )

        df["ema50"] = ema(
            df["close"],
            50
        )

        df["ema200"] = ema(
            df["close"],
            200
        )

        df["atr"] = atr(
            df,
            14
        )

        df["rsi"] = rsi(
            df["close"],
            14
        )

        df["volume_ratio"] = volume_ratio(
            df,
            20
        )

        high, low = donchian(
            df,
            20
        )

        df["donchian_high"] = high.shift(1)
        df["donchian_low"] = low.shift(1)

        df["body_ratio"] = df.apply(
            candle_body_ratio,
            axis=1
        )

        df["upper_wick"] = df.apply(
            upper_wick_ratio,
            axis=1
        )

        df["lower_wick"] = df.apply(
            lower_wick_ratio,
            axis=1
        )

        return df

    # ========================================================
    # BTC ALIGNMENT
    # ========================================================

    def btc_alignment(
        self,
        side,
        btc_context
    ):

        if not btc_context:
            return 0

        regime = btc_context["regime"]

        if side == "long":

            if regime == "TREND_UP":
                return 10

            if regime == "TREND_DOWN":
                return -10

        else:

            if regime == "TREND_DOWN":
                return 10

            if regime == "TREND_UP":
                return -10

        if regime == "RANGE":
            return 0

        return 2

    # ========================================================
    # STRATEGY 1: MOMENTUM BREAKOUT
    # ========================================================

    def strategy_breakout(
        self,
        symbol,
        df15,
        df5,
        regime,
        btc_context
    ):

        d = df15.iloc[-1]
        prev = df15.iloc[-2]

        long_signal = (
            d["close"] > d["donchian_high"]
            and d["volume_ratio"] >= 1.15
            and d["body_ratio"] >= 0.45
        )

        short_signal = (
            d["close"] < d["donchian_low"]
            and d["volume_ratio"] >= 1.15
            and d["body_ratio"] >= 0.45
        )

        candidates = []

        for side, signal in [
            ("long", long_signal),
            ("short", short_signal),
        ]:

            if not signal:
                continue

            score = 0

            # Base breakout.
            score += 20

            # Regime.
            if side == "long" and regime == "TREND_UP":
                score += 15

            elif side == "short" and regime == "TREND_DOWN":
                score += 15

            elif regime == "RANGE":
                score -= 8

            elif regime == "CHAOS":
                score -= 10

            # EMA.
            if side == "long":

                if d["ema20"] > d["ema50"]:
                    score += 8

                if d["close"] > d["ema20"]:
                    score += 5

            else:

                if d["ema20"] < d["ema50"]:
                    score += 8

                if d["close"] < d["ema20"]:
                    score += 5

            # Volume.
            score += int(
                clamp(
                    (d["volume_ratio"] - 1)
                    * 20,
                    0,
                    10
                )
            )

            score += self.btc_alignment(
                side,
                btc_context
            )

            candidates.append({
                "strategy": "MOMENTUM_BREAKOUT",
                "side": side,
                "score": score,
                "reason": "Donchian breakout",
            })

        return candidates

    # ========================================================
    # STRATEGY 2: LIQUIDITY SWEEP
    # ========================================================

    def strategy_sweep(
        self,
        symbol,
        df15,
        df5,
        regime,
        btc_context
    ):

        d = df15.iloc[-1]

        recent_high = df15[
            "high"
        ].iloc[-8:-1].max()

        recent_low = df15[
            "low"
        ].iloc[-8:-1].min()

        candidates = []

        # Bearish liquidity sweep:
        # high taken, candle closes back below.
        bearish_sweep = (
            d["high"] > recent_high
            and d["close"] < recent_high
            and d["upper_wick"] > 0.30
        )

        # Bullish liquidity sweep.
        bullish_sweep = (
            d["low"] < recent_low
            and d["close"] > recent_low
            and d["lower_wick"] > 0.30
        )

        if bullish_sweep:

            score = 22

            if regime in (
                "RANGE",
                "TRANSITION"
            ):
                score += 12

            if d["rsi"] < 45:
                score += 7

            score += self.btc_alignment(
                "long",
                btc_context
            )

            candidates.append({
                "strategy": "LIQUIDITY_SWEEP",
                "side": "long",
                "score": score,
                "reason": "Sell-side liquidity sweep",
            })

        if bearish_sweep:

            score = 22

            if regime in (
                "RANGE",
                "TRANSITION"
            ):
                score += 12

            if d["rsi"] > 55:
                score += 7

            score += self.btc_alignment(
                "short",
                btc_context
            )

            candidates.append({
                "strategy": "LIQUIDITY_SWEEP",
                "side": "short",
                "score": score,
                "reason": "Buy-side liquidity sweep",
            })

        return candidates

    # ========================================================
    # STRATEGY 3: OI + PRICE
    # ========================================================

    def strategy_oi(
        self,
        symbol,
        df15,
        oi_data,
        regime,
        btc_context
    ):

        if not oi_data:
            return []

        if len(df15) < 10:
            return []

        current_oi = safe_float(
            oi_data.get("oi")
        )

        # We need an approximate historical OI proxy.
        #
        # Since some exchanges do not expose historical OI
        # consistently through the unified API, we compare the
        # current value with a locally stored previous value.
        #
        # The local history is maintained below.
        #
        if not hasattr(
            self,
            "oi_history"
        ):
            self.oi_history = defaultdict(
                lambda: deque(
                    maxlen=20
                )
            )

        history = self.oi_history[
            symbol
        ]

        if current_oi > 0:
            history.append(
                (
                    now_ts(),
                    current_oi
                )
            )

        if len(history) < 3:
            return []

        old_oi = history[0][1]

        if old_oi <= 0:
            return []

        oi_change = (
            current_oi - old_oi
        ) / old_oi

        price_change = pct_change(
            df15["close"].iloc[-6],
            df15["close"].iloc[-1]
        )

        candidates = []

        # Price up + OI up = continuation long.
        if (
            price_change > 0.002
            and oi_change > 0.003
        ):

            score = 22

            if regime == "TREND_UP":
                score += 15

            elif regime == "TREND_DOWN":
                score -= 8

            score += self.btc_alignment(
                "long",
                btc_context
            )

            candidates.append({
                "strategy": "OI_DIVERGENCE",
                "side": "long",
                "score": score,
                "reason": (
                    "Price up + OI up "
                    "continuation"
                ),
            })

        # Price down + OI up = continuation short.
        if (
            price_change < -0.002
            and oi_change > 0.003
        ):

            score = 22

            if regime == "TREND_DOWN":
                score += 15

            elif regime == "TREND_UP":
                score -= 8

            score += self.btc_alignment(
                "short",
                btc_context
            )

            candidates.append({
                "strategy": "OI_DIVERGENCE",
                "side": "short",
                "score": score,
                "reason": (
                    "Price down + OI up "
                    "continuation"
                ),
            })

        # Price up + OI down = potential long
        # liquidation/profit-taking reversal.
        if (
            price_change > 0.003
            and oi_change < -0.003
            and regime in (
                "RANGE",
                "TRANSITION"
            )
        ):

            score = 20

            score += self.btc_alignment(
                "short",
                btc_context
            )

            candidates.append({
                "strategy": "OI_DIVERGENCE",
                "side": "short",
                "score": score,
                "reason": (
                    "Price up + OI down "
                    "reversal"
                ),
            })

        # Price down + OI down = shorts covering.
        if (
            price_change < -0.003
            and oi_change < -0.003
            and regime in (
                "RANGE",
                "TRANSITION"
            )
        ):

            score = 20

            score += self.btc_alignment(
                "long",
                btc_context
            )

            candidates.append({
                "strategy": "OI_DIVERGENCE",
                "side": "long",
                "score": score,
                "reason": (
                    "Price down + OI down "
                    "reversal"
                ),
            })

        return candidates

    # ========================================================
    # STRATEGY 4: TAKER MOMENTUM
    # ========================================================

    def strategy_taker(
        self,
        symbol,
        df5,
        taker,
        regime,
        btc_context
    ):

        if not taker:
            return []

        buy_ratio = taker[
            "buy_ratio"
        ]

        d = df5.iloc[-1]

        candidates = []

        # Strong buy pressure.
        if buy_ratio >= 0.61:

            score = 20

            if d["close"] > d["ema20"]:
                score += 10

            if d["ema20"] > d["ema50"]:
                score += 8

            if d["volume_ratio"] > 1.10:
                score += 7

            if regime == "TREND_UP":
                score += 10

            score += self.btc_alignment(
                "long",
                btc_context
            )

            candidates.append({
                "strategy": "TAKER_MOMENTUM",
                "side": "long",
                "score": score,
                "reason": (
                    f"Strong taker buy "
                    f"pressure {buy_ratio:.2f}"
                ),
            })

        # Strong sell pressure.
        if buy_ratio <= 0.39:

            score = 20

            if d["close"] < d["ema20"]:
                score += 10

            if d["ema20"] < d["ema50"]:
                score += 8

            if d["volume_ratio"] > 1.10:
                score += 7

            if regime == "TREND_DOWN":
                score += 10

            score += self.btc_alignment(
                "short",
                btc_context
            )

            candidates.append({
                "strategy": "TAKER_MOMENTUM",
                "side": "short",
                "score": score,
                "reason": (
                    f"Strong taker sell "
                    f"pressure {buy_ratio:.2f}"
                ),
            })

        return candidates

    # ========================================================
    # MICROSTRUCTURE CONFIRMATION
    # ========================================================

    def confirm_candidate(
        self,
        candidate,
        df5,
        oi,
        taker,
        funding
    ):

        side = candidate["side"]

        score = candidate["score"]

        d = df5.iloc[-1]

        # 5m directional confirmation.
        if side == "long":

            if (
                d["close"] > d["ema20"]
                and d["ema20"] > d["ema50"]
            ):
                score += 8

            if d["rsi"] > 50:
                score += 4

            if taker:

                if taker["buy_ratio"] > 0.56:
                    score += 8

                elif taker["buy_ratio"] < 0.44:
                    score -= 8

        else:

            if (
                d["close"] < d["ema20"]
                and d["ema20"] < d["ema50"]
            ):
                score += 8

            if d["rsi"] < 50:
                score += 4

            if taker:

                if taker["sell_ratio"] > 0.56:
                    score += 8

                elif taker["sell_ratio"] < 0.44:
                    score -= 8

        # OI confirmation.
        if oi:

            if candidate["strategy"] == "OI_DIVERGENCE":
                score += 6

        # Funding as a crowding warning.
        if funding:

            rate = funding["rate"]

            if side == "long":

                if rate > 0.0008:
                    score -= 5

            else:

                if rate < -0.0008:
                    score -= 5

        candidate = dict(candidate)

        candidate["score"] = score

        return candidate

    # ========================================================
    # CANDIDATE EVALUATION
    # ========================================================

    def evaluate_symbol(
        self,
        symbol,
        btc_context
    ):

        try:

            df15 = self.fetch_ohlcv(
                symbol,
                TF_SIGNAL,
                150
            )

            df1h = self.fetch_ohlcv(
                symbol,
                TF_TREND,
                220
            )

            if (
                df15 is None
                or df1h is None
            ):
                return []

            if (
                len(df15) < 80
                or len(df1h) < 210
            ):
                return []

            df15 = self.enrich(df15)
            df1h = self.enrich(df1h)

            regime = detect_regime(
                df1h,
                df15
            )

            d = df15.iloc[-1]

            price = safe_float(
                d["close"]
            )

            atr_value = safe_float(
                d["atr"]
            )

            if price <= 0 or atr_value <= 0:
                return []

            atr_pct = (
                atr_value / price * 100
            )

            if (
                atr_pct < MIN_ATR_PCT
                or atr_pct > MAX_ATR_PCT
            ):
                return []

            # ------------------------------------------------
            # Cheap filter before 5m/API-heavy calls.
            # ------------------------------------------------

            spread = self.spread_pct(
                symbol
            )

            if spread > MAX_SPREAD_PCT:
                return []

            # ------------------------------------------------
            # First strategy layer.
            # ------------------------------------------------

            prelim = []

            df5 = self.fetch_ohlcv(
                symbol,
                TF_FAST,
                120
            )

            if df5 is None or len(df5) < 70:
                return []

            df5 = self.enrich(df5)

            prelim.extend(
                self.strategy_breakout(
                    symbol,
                    df15,
                    df5,
                    regime,
                    btc_context
                )
            )

            prelim.extend(
                self.strategy_sweep(
                    symbol,
                    df15,
                    df5,
                    regime,
                    btc_context
                )
            )

            # OI is only fetched for symbols
            # which already have a technical candidate.
            oi = self.fetch_oi(symbol)

            prelim.extend(
                self.strategy_oi(
                    symbol,
                    df15,
                    oi,
                    regime,
                    btc_context
                )
            )

            taker = self.fetch_taker_pressure(
                symbol
            )

            prelim.extend(
                self.strategy_taker(
                    symbol,
                    df5,
                    taker,
                    regime,
                    btc_context
                )
            )

            if not prelim:
                return []

            funding = self.fetch_funding(
                symbol
            )

            final = []

            rel = self.relative_strength(
                df15,
                btc_context["df15"]
                if btc_context
                else df15
            )

            for candidate in prelim:

                candidate = self.confirm_candidate(
                    candidate,
                    df5,
                    oi,
                    taker,
                    funding
                )

                side = candidate["side"]

                # Relative strength.
                if side == "long":

                    if rel > 0.003:
                        candidate["score"] += 6

                    elif rel < -0.006:
                        candidate["score"] -= 6

                else:

                    if rel < -0.003:
                        candidate["score"] += 6

                    elif rel > 0.006:
                        candidate["score"] -= 6

                # Volume.
                vr = safe_float(
                    d["volume_ratio"]
                )

                if vr >= MIN_VOLUME_RATIO:
                    candidate["score"] += 3

                # ATR quality.
                if 0.30 <= atr_pct <= 3.0:
                    candidate["score"] += 4

                # Strategy learning weight.
                weight = self.strategy_weight(
                    candidate["strategy"]
                )

                raw_score = candidate[
                    "score"
                ]

                candidate["raw_score"] = raw_score

                candidate["score"] = (
                    raw_score * weight
                )

                candidate["symbol"] = symbol

                candidate["regime"] = regime

                candidate["atr"] = atr_value

                candidate["atr_pct"] = atr_pct

                candidate["price"] = price

                candidate["spread"] = spread

                candidate["relative_strength"] = rel

                candidate["oi"] = oi

                candidate["taker"] = taker

                candidate["funding"] = funding

                final.append(candidate)

            return final

        except Exception as e:

            print(
                f"Evaluation error {symbol}:",
                e
            )

            return []

    # ========================================================
    # CLUSTER CONTROL
    # ========================================================

    def symbol_cluster(self, symbol):

        major = {
            "BTC",
            "ETH",
            "BNB",
            "SOL",
            "XRP",
            "ADA",
            "TRX",
            "TON",
            "LTC",
            "BCH",
            "AVAX",
            "LINK",
            "DOT",
        }

        meme = {
            "DOGE",
            "PEPE",
            "WIF",
            "BONK",
            "FLOKI",
            "SHIB",
            "JUP",
        }

        base = symbol.split("/")[0]

        if base in major:
            return "MAJOR"

        if base in meme:
            return "MEME"

        return "ALT"

    def cluster_exposure_ok(
        self,
        candidate
    ):

        cluster = self.symbol_cluster(
            candidate["symbol"]
        )

        count = 0

        for p in self.positions.values():

            if self.symbol_cluster(
                p["symbol"]
            ) == cluster:
                count += 1

        # Never put both slots into a meme cluster.
        if cluster == "MEME" and count >= 1:
            return False

        # Maximum 1 position per cluster.
        if count >= 1:
            return False

        return True

    # ========================================================
    # EQUITY
    # ========================================================

    def get_equity(self):

        if PAPER_TRADING:
            return self.paper_equity

        try:

            balance = self.exchange.fetch_balance()

            total = safe_float(
                balance.get("total", {}).get(
                    "USDT"
                )
            )

            if total <= 0:

                total = safe_float(
                    balance.get(
                        "USDT",
                        {}
                    ).get("total")
                )

            if total <= 0:
                return 0.0

            return total

        except Exception as e:

            print(
                "Equity error:",
                e
            )

            return 0.0

    # ========================================================
    # DAILY SAFETY
    # ========================================================

    def reset_daily_state(self):

        today = utc_now().date()

        if today != self.last_daily_reset:

            self.last_daily_reset = today

            self.daily_realized_pnl = 0.0

            self.consecutive_losses = 0

            self.day_start_equity = (
                self.get_equity()
            )

    def trading_allowed(self):

        self.reset_daily_state()

        equity = self.get_equity()

        if equity <= 0:
            return False

        if equity < MIN_EQUITY_USDT:
            print(
                "SAFETY: equity below minimum."
            )
            return False

        if (
            self.day_start_equity
            and self.day_start_equity > 0
        ):

            daily_drawdown = (
                self.day_start_equity
                - equity
            ) / self.day_start_equity

            if daily_drawdown >= DAILY_LOSS_LIMIT_PCT:

                print(
                    "SAFETY: daily loss limit."
                )

                return False

        if (
            self.consecutive_losses
            >= MAX_CONSECUTIVE_LOSSES
        ):

            print(
                "SAFETY: consecutive loss lock."
            )

            return False

        return True

    # ========================================================
    # POSITION COUNT
    # ========================================================

    def active_position_count(self):

        with self.lock:

            return len(
                self.positions
            )

    # ========================================================
    # COOLDOWNS
    # ========================================================

    def symbol_available(self, symbol):

        now = now_ts()

        if now < self.symbol_cooldowns.get(
            symbol,
            0
        ):
            return False

        if now < self.loss_cooldowns.get(
            symbol,
            0
        ):
            return False

        return True

    # ========================================================
    # POSITION SIZE
    # ========================================================

    def calculate_position_size(
        self,
        candidate
    ):

        equity = self.get_equity()

        if equity <= 0:
            return None

        entry = candidate["price"]

        atr_value = candidate["atr"]

        side = candidate["side"]

        # ----------------------------------------------------
        # Stop.
        # ----------------------------------------------------

        stop_distance = (
            atr_value
            * ATR_SL_MULTIPLIER
        )

        if side == "long":
            stop = entry - stop_distance
        else:
            stop = entry + stop_distance

        stop_pct = (
            abs(entry - stop)
            / entry
        )

        if stop_pct <= 0:
            return None

        # ----------------------------------------------------
        # Risk budget.
        # ----------------------------------------------------

        risk_budget = (
            equity
            * MAX_RISK_EQUITY_PCT
        )

        # Stronger signals may use a little more
        # of the allowed risk, but never above cap.
        score_factor = clamp(
            candidate["score"] / 80,
            0.70,
            1.0
        )

        risk_budget *= score_factor

        # ----------------------------------------------------
        # Required notional.
        # ----------------------------------------------------

        notional = (
            risk_budget / stop_pct
        )

        # ----------------------------------------------------
        # Margin.
        # ----------------------------------------------------

        margin = (
            notional / LEVERAGE
        )

        margin = clamp(
            margin,
            MIN_MARGIN_USDT,
            MAX_MARGIN_USDT
        )

        max_by_equity = (
            equity
            * MAX_MARGIN_EQUITY_PCT
        )

        margin = min(
            margin,
            max_by_equity
        )

        # Total margin cap.
        total_margin = sum(
            p.get(
                "margin",
                0.0
            )
            for p in self.positions.values()
        )

        remaining_margin = (
            equity
            * MAX_TOTAL_MARGIN_EQUITY_PCT
            - total_margin
        )

        if remaining_margin <= 0:
            return None

        margin = min(
            margin,
            remaining_margin
        )

        if margin < MIN_MARGIN_USDT:
            return None

        notional = (
            margin * LEVERAGE
        )

        market = self.exchange.market(
            candidate["symbol"]
        )

        contract_size = safe_float(
            market.get("contractSize"),
            1.0
        )

        raw_contracts = (
            notional
            / entry
            / contract_size
        )

        try:

            contracts = float(
                self.exchange.amount_to_precision(
                    candidate["symbol"],
                    raw_contracts
                )
            )

        except Exception:

            contracts = raw_contracts

        limits = market.get(
            "limits",
            {}
        )

        min_amount = safe_float(
            limits.get(
                "amount",
                {}
            ).get("min")
        )

        max_amount = safe_float(
            limits.get(
                "amount",
                {}
            ).get("max")
        )

        if min_amount and contracts < min_amount:
            return None

        if max_amount and contracts > max_amount:
            contracts = max_amount

        if contracts <= 0:
            return None

        actual_notional = (
            contracts
            * contract_size
            * entry
        )

        actual_margin = (
            actual_notional
            / LEVERAGE
        )

        # Expected TP2.
        tp2 = (
            entry
            + stop_distance * TP2_R
            if side == "long"
            else entry
            - stop_distance * TP2_R
        )

        expected_gross = (
            actual_notional
            * abs(tp2 - entry)
            / entry
        )

        estimated_cost = (
            actual_notional
            * ROUND_TRIP_COST_PCT
        )

        expected_net = (
            expected_gross
            - estimated_cost
        )

        # Do not trade tiny setups where the expected
        # move cannot reasonably pay the costs.
        if expected_net < MIN_PROFIT_TARGET_USDT:

            # Strong setups may still pass if expected
            # gross is meaningful.
            if (
                candidate["score"]
                < STRONG_SCORE
                or expected_gross
                < IDEAL_PROFIT_TARGET_USDT
            ):
                return None

        return {
            "contracts": contracts,
            "margin": actual_margin,
            "notional": actual_notional,
            "entry": entry,
            "stop": stop,
            "stop_distance": stop_distance,
            "stop_pct": stop_pct,
            "tp1": (
                entry
                + stop_distance * TP1_R
                if side == "long"
                else entry
                - stop_distance * TP1_R
            ),
            "tp2": tp2,
            "expected_gross": expected_gross,
            "expected_net": expected_net,
        }

    # ========================================================
    # ORDER HELPERS
    # ========================================================

    def amount_precision(
        self,
        symbol,
        amount
    ):

        try:

            return float(
                self.exchange.amount_to_precision(
                    symbol,
                    amount
                )
            )

        except Exception:

            return amount

    def price_precision(
        self,
        symbol,
        price
    ):

        try:

            return float(
                self.exchange.price_to_precision(
                    symbol,
                    price
                )
            )

        except Exception:

            return price

    # ========================================================
    # MARKET ENTRY
    # ========================================================

    def open_market_position(
        self,
        candidate,
        sizing
    ):

        symbol = candidate["symbol"]

        side = (
            "buy"
            if candidate["side"] == "long"
            else "sell"
        )

        amount = sizing["contracts"]

        if PAPER_TRADING:

            entry = sizing["entry"]

            position = {
                "symbol": symbol,
                "side": candidate["side"],
                "contracts": amount,
                "original_contracts": amount,
                "entry": entry,
                "stop": sizing["stop"],
                "original_stop": sizing["stop"],
                "tp1": sizing["tp1"],
                "tp2": sizing["tp2"],
                "margin": sizing["margin"],
                "notional": sizing["notional"],
                "strategy": candidate["strategy"],
                "score": candidate["score"],
                "regime": candidate["regime"],
                "tp1_done": False,
                "breakeven": False,
                "trail_stop": None,
                "opened_at": now_ts(),
                "reason": candidate["reason"],
            }

            self.positions[symbol] = position

            return position

        try:

            order = self.exchange.create_order(
                symbol,
                "market",
                side,
                amount,
                None,
                {
                    "reduceOnly": False,
                }
            )

            time.sleep(0.5)

            actual_entry = (
                safe_float(
                    order.get("average")
                )
                or safe_float(
                    order.get("price")
                )
                or sizing["entry"]
            )

            position = {
                "symbol": symbol,
                "side": candidate["side"],
                "contracts": amount,
                "original_contracts": amount,
                "entry": actual_entry,
                "stop": sizing["stop"],
                "original_stop": sizing["stop"],
                "tp1": sizing["tp1"],
                "tp2": sizing["tp2"],
                "margin": sizing["margin"],
                "notional": sizing["notional"],
                "strategy": candidate["strategy"],
                "score": candidate["score"],
                "regime": candidate["regime"],
                "tp1_done": False,
                "breakeven": False,
                "trail_stop": None,
                "opened_at": now_ts(),
                "entry_order_id": order.get("id"),
                "reason": candidate["reason"],
            }

            self.positions[symbol] = position

            return position

        except Exception as e:

            print(
                "ENTRY ERROR:",
                symbol,
                e
            )

            return None

    # ========================================================
    # BLOFIN PROTECTION
    # ========================================================

    def create_protection(
        self,
        position
    ):

        symbol = position["symbol"]

        side = position["side"]

        amount = position["contracts"]

        close_side = (
            "sell"
            if side == "long"
            else "buy"
        )

        stop = self.price_precision(
            symbol,
            position["stop"]
        )

        tp = self.price_precision(
            symbol,
            position["tp2"]
        )

        amount = self.amount_precision(
            symbol,
            amount
        )

        if PAPER_TRADING:
            return True

        # ----------------------------------------------------
        # IMPORTANT:
        # BloFin can handle standalone TP/SL algo orders.
        # We deliberately create them separately rather than
        # relying on a combined CCXT TPSL request.
        # ----------------------------------------------------

        try:

            # STOP LOSS
            sl_params = {
                "stopLossPrice": stop,
                "reduceOnly": True,
            }

            self.exchange.create_order(
                symbol,
                "market",
                close_side,
                amount,
                None,
                sl_params
            )

            # TAKE PROFIT
            tp_params = {
                "takeProfitPrice": tp,
                "reduceOnly": True,
            }

            self.exchange.create_order(
                symbol,
                "market",
                close_side,
                amount,
                None,
                tp_params
            )

            return True

        except Exception as e:

            print(
                "Protection error:",
                symbol,
                e
            )

            return False

    # ========================================================
    # CLOSE POSITION
    # ========================================================

    def close_market(
        self,
        position,
        amount=None
    ):

        symbol = position["symbol"]

        side = position["side"]

        amount = (
            amount
            if amount is not None
            else position["contracts"]
        )

        amount = self.amount_precision(
            symbol,
            amount
        )

        if amount <= 0:
            return False

        close_side = (
            "sell"
            if side == "long"
            else "buy"
        )

        if PAPER_TRADING:

            return True

        try:

            self.exchange.create_order(
                symbol,
                "market",
                close_side,
                amount,
                None,
                {
                    "reduceOnly": True,
                }
            )

            return True

        except Exception as e:

            print(
                "Close error:",
                symbol,
                e
            )

            return False

    # ========================================================
    # PAPER PNL
    # ========================================================

    def paper_pnl(
        self,
        position,
        price,
        amount=None
    ):

        if amount is None:
            amount = position["contracts"]

        market = self.exchange.market(
            position["symbol"]
        )

        contract_size = safe_float(
            market.get("contractSize"),
            1.0
        )

        price_move = (
            price
            - position["entry"]
        )

        if position["side"] == "short":
            price_move *= -1

        return (
            price_move
            * amount
            * contract_size
        )

    # ========================================================
    # REGISTER TRADE RESULT
    # ========================================================

    def register_trade_result(
        self,
        position,
        pnl
    ):

        strategy = position[
            "strategy"
        ]

        risk = (
            position["margin"]
            * LEVERAGE
            * (
                abs(
                    position["entry"]
                    - position["original_stop"]
                )
                / position["entry"]
            )
        )

        if risk <= 0:
            risk = 1.0

        r_multiple = (
            pnl / risk
        )

        self.daily_realized_pnl += pnl

        if pnl > 0:

            self.consecutive_losses = 0

        else:

            self.consecutive_losses += 1

            self.loss_cooldowns[
                position["symbol"]
            ] = (
                now_ts()
                + LOSS_COOLDOWN_SECONDS
            )

        self.symbol_cooldowns[
            position["symbol"]
        ] = (
            now_ts()
            + SYMBOL_COOLDOWN_SECONDS
        )

        self.learn_trade(
            strategy,
            pnl,
            r_multiple
        )

        print(
            f"TRADE CLOSED | "
            f"{position['symbol']} | "
            f"{strategy} | "
            f"PnL ${pnl:.2f} | "
            f"R {r_multiple:.2f}"
        )

        send_telegram(
            f"🏁 TRADE CLOSED\n"
            f"{position['symbol']}\n"
            f"{strategy}\n"
            f"PnL: ${pnl:.2f}\n"
            f"R: {r_multiple:.2f}"
        )

    # ========================================================
    # POSITION MANAGEMENT
    # ========================================================

    def manage_paper_position(
        self,
        position
    ):

        symbol = position["symbol"]

        try:

            ticker = self.exchange.fetch_ticker(
                symbol
            )

            price = safe_float(
                ticker.get("last")
            )

            if price <= 0:
                return

        except Exception:
            return

        side = position["side"]

        # ----------------------------------------------------
        # STOP
        # ----------------------------------------------------

        stop_hit = (
            price <= position["stop"]
            if side == "long"
            else price >= position["stop"]
        )

        if stop_hit:

            pnl = self.paper_pnl(
                position,
                price
            )

            self.paper_equity += pnl

            self.register_trade_result(
                position,
                pnl
            )

            del self.positions[
                symbol
            ]

            return

        # ----------------------------------------------------
        # TP1
        # ----------------------------------------------------

        tp1_hit = (
            price >= position["tp1"]
            if side == "long"
            else price <= position["tp1"]
        )

        if (
            tp1_hit
            and not position["tp1_done"]
        ):

            partial = (
                position["original_contracts"]
                * TP1_CLOSE_PCT
            )

            partial = min(
                partial,
                position["contracts"]
            )

            if partial > 0:

                pnl = self.paper_pnl(
                    position,
                    price,
                    partial
                )

                self.paper_equity += pnl

                position["contracts"] -= (
                    partial
                )

                position["tp1_done"] = True

                # Break-even.
                if side == "long":

                    position["stop"] = (
                        position["entry"]
                        * (
                            1
                            + BREAK_EVEN_BUFFER_PCT
                        )
                    )

                else:

                    position["stop"] = (
                        position["entry"]
                        * (
                            1
                            - BREAK_EVEN_BUFFER_PCT
                        )
                    )

                position["breakeven"] = True

                print(
                    f"TP1 {symbol} | "
                    f"partial pnl ${pnl:.2f}"
                )

        # ----------------------------------------------------
        # TP2
        # ----------------------------------------------------

        tp2_hit = (
            price >= position["tp2"]
            if side == "long"
            else price <= position["tp2"]
        )

        if (
            tp2_hit
            and position["contracts"] > 0
        ):

            pnl = self.paper_pnl(
                position,
                price
            )

            self.paper_equity += pnl

            self.register_trade_result(
                position,
                pnl
            )

            del self.positions[
                symbol
            ]

            return

        # ----------------------------------------------------
        # TRAILING RUNNER
        # ----------------------------------------------------

        if position["tp1_done"]:

            atr_estimate = abs(
                position["entry"]
                - position["original_stop"]
            ) / ATR_SL_MULTIPLIER

            if side == "long":

                new_stop = (
                    price
                    - atr_estimate
                    * TRAIL_ATR_MULTIPLIER
                )

                position["stop"] = max(
                    position["stop"],
                    new_stop
                )

            else:

                new_stop = (
                    price
                    + atr_estimate
                    * TRAIL_ATR_MULTIPLIER
                )

                position["stop"] = min(
                    position["stop"],
                    new_stop
                )

    # ========================================================
    # MANAGE LIVE POSITION
    # ========================================================

    def manage_live_position(
        self,
        position
    ):

        # For live mode, BloFin's server-side
        # protection remains the primary safety layer.
        #
        # We still monitor positions and can move the
        # stop to break-even after TP1 conditions.

        symbol = position["symbol"]

        try:

            ticker = self.exchange.fetch_ticker(
                symbol
            )

            price = safe_float(
                ticker.get("last")
            )

            if price <= 0:
                return

            side = position["side"]

            tp1_hit = (
                price >= position["tp1"]
                if side == "long"
                else price <= position["tp1"]
            )

            if (
                tp1_hit
                and not position["tp1_done"]
            ):

                partial = (
                    position["original_contracts"]
                    * TP1_CLOSE_PCT
                )

                partial = min(
                    partial,
                    position["contracts"]
                )

                if partial > 0:

                    self.close_market(
                        position,
                        partial
                    )

                    position["contracts"] -= (
                        partial
                    )

                    position["tp1_done"] = True

                    # Move local state to BE.
                    if side == "long":

                        position["stop"] = (
                            position["entry"]
                            * (
                                1
                                + BREAK_EVEN_BUFFER_PCT
                            )
                        )

                    else:

                        position["stop"] = (
                            position["entry"]
                            * (
                                1
                                - BREAK_EVEN_BUFFER_PCT
                            )
                        )

                    position["breakeven"] = True

                    # Recreate protection.
                    self.create_protection(
                        position
                    )

        except Exception as e:

            print(
                "Live management error:",
                symbol,
                e
            )

    # ========================================================
    # MANAGE ALL
    # ========================================================

    def manage_positions(self):

        if (
            now_ts()
            - self.last_management
            < MANAGEMENT_INTERVAL_SECONDS
        ):
            return

        self.last_management = now_ts()

        for symbol in list(
            self.positions.keys()
        ):

            try:

                position = self.positions[
                    symbol
                ]

                if PAPER_TRADING:

                    self.manage_paper_position(
                        position
                    )

                else:

                    self.manage_live_position(
                        position
                    )

            except Exception as e:

                print(
                    "Position manager error:",
                    e
                )

    # ========================================================
    # EXECUTE CANDIDATE
    # ========================================================

    def execute_candidate(
        self,
        candidate
    ):

        with self.lock:

            if not self.trading_allowed():
                return False

            if (
                self.active_position_count()
                >= MAX_ACTIVE_POSITIONS
            ):
                return False

            symbol = candidate[
                "symbol"
            ]

            if symbol in self.positions:
                return False

            if not self.symbol_available(
                symbol
            ):
                return False

            if not self.cluster_exposure_ok(
                candidate
            ):
                return False

            # No artificial global hourly cooldown.
            if (
                GLOBAL_ENTRY_COOLDOWN_SECONDS
                > 0
            ):

                if (
                    now_ts()
                    - self.last_global_entry
                    < GLOBAL_ENTRY_COOLDOWN_SECONDS
                ):
                    return False

            if candidate["score"] < MIN_SCORE:
                return False

            sizing = (
                self.calculate_position_size(
                    candidate
                )
            )

            if not sizing:
                return False

            print(
                "\n=============================="
            )

            print(
                "NEW CANDIDATE"
            )

            print(
                "Symbol:",
                symbol
            )

            print(
                "Side:",
                candidate["side"]
            )

            print(
                "Strategy:",
                candidate["strategy"]
            )

            print(
                "Score:",
                round(
                    candidate["score"],
                    1
                )
            )

            print(
                "Regime:",
                candidate["regime"]
            )

            print(
                "Margin:",
                round(
                    sizing["margin"],
                    2
                )
            )

            print(
                "Expected net:",
                round(
                    sizing["expected_net"],
                    2
                )
            )

            print(
                "Reason:",
                candidate["reason"]
            )

            print(
                "=============================="
            )

            position = (
                self.open_market_position(
                    candidate,
                    sizing
                )
            )

            if not position:
                return False

            # ------------------------------------------------
            # Protection is mandatory in live mode.
            # ------------------------------------------------

            protection_ok = (
                self.create_protection(
                    position
                )
            )

            if not protection_ok:

                print(
                    "CRITICAL: protection failed."
                )

                if not PAPER_TRADING:

                    self.close_market(
                        position
                    )

                self.positions.pop(
                    symbol,
                    None
                )

                return False

            self.last_global_entry = (
                now_ts()
            )

            send_telegram(
                f"🚀 NEW TRADE\n"
                f"{symbol}\n"
                f"{candidate['side'].upper()}\n"
                f"Strategy: "
                f"{candidate['strategy']}\n"
                f"Score: "
                f"{candidate['score']:.1f}\n"
                f"Regime: "
                f"{candidate['regime']}\n"
                f"Margin: "
                f"${sizing['margin']:.2f}\n"
                f"Expected: "
                f"${sizing['expected_net']:.2f}"
            )

            return True

    # ========================================================
    # SCAN
    # ========================================================

    def scan(self):

        if not self.trading_allowed():
            return

        if (
            self.active_position_count()
            >= MAX_ACTIVE_POSITIONS
        ):
            return

        btc = self.fetch_btc_context()

        if not btc:
            return

        all_candidates = []

        # ----------------------------------------------------
        # Scan every symbol.
        # ----------------------------------------------------

        for symbol in self.symbols:

            if (
                self.active_position_count()
                >= MAX_ACTIVE_POSITIONS
            ):
                break

            if symbol in self.positions:
                continue

            if not self.symbol_available(
                symbol
            ):
                continue

            try:

                candidates = (
                    self.evaluate_symbol(
                        symbol,
                        btc
                    )
                )

                all_candidates.extend(
                    candidates
                )

            except Exception as e:

                print(
                    "Scan error:",
                    symbol,
                    e
                )

            time.sleep(
                SYMBOL_DELAY_SECONDS
            )

        if not all_candidates:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                "No valid setups."
            )
            return

        # ----------------------------------------------------
        # Rank.
        # ----------------------------------------------------

        all_candidates.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        # ----------------------------------------------------
        # Avoid taking multiple versions of the same symbol.
        # ----------------------------------------------------

        selected = []

        used_symbols = set()

        for c in all_candidates:

            if c["symbol"] in used_symbols:
                continue

            if c["score"] < MIN_SCORE:
                continue

            selected.append(c)

            used_symbols.add(
                c["symbol"]
            )

            if len(selected) >= 3:
                break

        if not selected:
            print(
                "No candidate passed score."
            )
            return

        # ----------------------------------------------------
        # Execute strongest opportunities.
        # ----------------------------------------------------

        for candidate in selected:

            if (
                self.active_position_count()
                >= MAX_ACTIVE_POSITIONS
            ):
                break

            self.execute_candidate(
                candidate
            )

    # ========================================================
    # LIVE POSITION SYNC
    # ========================================================

    def sync_live_positions(self):

        if PAPER_TRADING:
            return

        if (
            now_ts()
            - self.last_position_sync
            < POSITION_SYNC_SECONDS
        ):
            return

        self.last_position_sync = now_ts()

        try:

            positions = (
                self.exchange.fetch_positions(
                    self.symbols
                )
            )

            live_symbols = set()

            for p in positions:

                contracts = abs(
                    safe_float(
                        p.get("contracts")
                    )
                )

                if contracts <= 0:
                    continue

                symbol = p.get(
                    "symbol"
                )

                if not symbol:
                    continue

                live_symbols.add(
                    symbol
                )

                if symbol not in self.positions:

                    side = normalize_side(
                        p.get("side")
                    )

                    entry = safe_float(
                        p.get("entryPrice")
                    )

                    if entry <= 0:
                        continue

                    self.positions[
                        symbol
                    ] = {
                        "symbol": symbol,
                        "side": side,
                        "contracts": contracts,
                        "original_contracts": contracts,
                        "entry": entry,
                        "stop": entry,
                        "original_stop": entry,
                        "tp1": entry,
                        "tp2": entry,
                        "margin": safe_float(
                            p.get("initialMargin")
                        ),
                        "notional": safe_float(
                            p.get("notional")
                        ),
                        "strategy": "UNKNOWN",
                        "score": 0,
                        "regime": "UNKNOWN",
                        "tp1_done": False,
                        "breakeven": False,
                        "opened_at": now_ts(),
                    }

            # Remove local positions no longer open.
            for symbol in list(
                self.positions.keys()
            ):

                if symbol not in live_symbols:

                    # We cannot reliably reconstruct
                    # exact realized PnL from position
                    # disappearance alone, so do not train
                    # the learning engine here.
                    #
                    # This is intentional: bad labels are
                    # worse than missing labels.

                    del self.positions[
                        symbol
                    ]

        except Exception as e:

            print(
                "Position sync error:",
                e
            )

    # ========================================================
    # STATUS
    # ========================================================

    def status(self):

        equity = self.get_equity()

        print(
            "\n"
            "============================================\n"
            " BLOFIN TACTICAL ENGINE V5\n"
            "============================================"
        )

        print(
            "Mode:",
            "PAPER" if PAPER_TRADING
            else "LIVE"
        )

        print(
            "Equity:",
            f"${equity:.2f}"
        )

        print(
            "Positions:",
            self.active_position_count(),
            "/",
            MAX_ACTIVE_POSITIONS
        )

        print(
            "Daily PnL:",
            f"${self.daily_realized_pnl:.2f}"
        )

        print(
            "Consecutive losses:",
            self.consecutive_losses
        )

        print(
            "\nStrategy learning:"
        )

        for name, data in (
            self.learning[
                "strategies"
            ].items()
        ):

            trades = data["trades"]

            winrate = (
                data["wins"] / trades
                if trades
                else 0
            )

            print(
                f"  {name:<20} "
                f"trades={trades:<4} "
                f"WR={winrate:.1%} "
                f"R={data['avg_r']:.2f} "
                f"W={data['weight']:.2f}"
            )

        print(
            "============================================\n"
        )

    # ========================================================
    # MAIN LOOP
    # ========================================================

    def run(self):

        print(
            "\n"
            "==================================================\n"
            " BLOFIN TACTICAL ENGINE V5.0 STARTED\n"
            "=================================================="
        )

        print(
            "Mode:",
            "PAPER TRADING"
            if PAPER_TRADING
            else "LIVE TRADING"
        )

        print(
            f"Symbols: {len(self.symbols)}"
        )

        print(
            f"Max positions: "
            f"{MAX_ACTIVE_POSITIONS}"
        )

        print(
            f"Leverage: {LEVERAGE}x"
        )

        print(
            f"Min score: {MIN_SCORE}"
        )

        print(
            "Self-learning: ENABLED"
        )

        print(
            "==================================================\n"
        )

        self.day_start_equity = (
            self.get_equity()
        )

        last_status = 0

        while True:

            cycle_start = now_ts()

            try:

                # --------------------------------------------
                # Reset daily safety.
                # --------------------------------------------

                self.reset_daily_state()

                # --------------------------------------------
                # Sync live positions.
                # --------------------------------------------

                self.sync_live_positions()

                # --------------------------------------------
                # Manage existing positions.
                # --------------------------------------------

                self.manage_positions()

                # --------------------------------------------
                # Scan for new opportunities.
                # --------------------------------------------

                if self.trading_allowed():

                    if (
                        self.active_position_count()
                        < MAX_ACTIVE_POSITIONS
                    ):

                        self.scan()

                # --------------------------------------------
                # Status.
                # --------------------------------------------

                if (
                    now_ts()
                    - last_status
                    > 15 * 60
                ):

                    self.status()

                    last_status = now_ts()

            except KeyboardInterrupt:

                print(
                    "Bot stopped manually."
                )

                break

            except Exception as e:

                print(
                    "\nMAIN LOOP ERROR:",
                    e
                )

                traceback.print_exc()

            # ----------------------------------------------
            # Cycle timing.
            # ----------------------------------------------

            elapsed = (
                now_ts()
                - cycle_start
            )

            sleep_for = max(
                2,
                SCAN_INTERVAL_SECONDS
                - elapsed
            )

            print(
                f"Cycle finished in "
                f"{elapsed:.1f}s | "
                f"next cycle in "
                f"{sleep_for:.1f}s"
            )

            time.sleep(
                sleep_for
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    if not API_KEY and not PAPER_TRADING:

        raise RuntimeError(
            "EXCHANGE_API_KEY ontbreekt."
        )

    if not API_SECRET and not PAPER_TRADING:

        raise RuntimeError(
            "EXCHANGE_SECRET ontbreekt."
        )

    if not API_PASSWORD and not PAPER_TRADING:

        raise RuntimeError(
            "EXCHANGE_PASSWORD ontbreekt."
        )

    engine = BlofinEngine()

    engine.run()
