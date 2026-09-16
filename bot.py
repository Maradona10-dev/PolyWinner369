import os
import time
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

import ccxt
import pandas as pd
import pandas_ta as ta
import requests
from flask import Flask


# ====================== WEB / CONFIG ======================

app = Flask(__name__)


@app.route("/")
def health_check():
    return "BloFin Multi-Factor Bot v2 is running!", 200


API_KEY = os.environ.get("EXCHANGE_API_KEY", "")
API_SECRET = os.environ.get("EXCHANGE_SECRET", "")
API_PASSWORD = os.environ.get("EXCHANGE_PASSWORD", "")

EXCHANGE_ID = "blofin"

PAPER_TRADING = os.environ.get("PAPER_TRADING", "False").lower() == "true"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "DOGE/USDT:USDT",
    "ADA/USDT:USDT",
    "AVAX/USDT:USDT",
    "LINK/USDT:USDT",
    "SUI/USDT:USDT",
    "NEAR/USDT:USDT",
    "APT/USDT:USDT",
    "ARB/USDT:USDT",
    "OP/USDT:USDT",
    "INJ/USDT:USDT",
    "PEPE/USDT:USDT",
    "RENDER/USDT:USDT",
    "WIF/USDT:USDT",
    "ENA/USDT:USDT",
    "TON/USDT:USDT",
]

SCAN_TF = "15m"
CONFIRM_TF = "5m"
TREND_TF = "1h"

LOOKBACK = 12
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
ATR_LEN = 14
VOLUME_LEN = 20
VOLUME_MULT = 1.20

MIN_SCORE = 75

FIXED_MARGIN_USDT = 5.0
FIXED_LEVERAGE = 10

MAX_ACTIVE_POSITIONS = 2

# SL = 1.5 ATR, TP = 2R.
STOP_ATR_MULT = 1.5
RISK_REWARD = 2.0

# Use closed candles only. This prevents signals changing while a candle
# is still forming.
USE_CLOSED_CANDLES = True

TRADE_LOG_FILE = "trade_history.json"

SCAN_SLEEP_SECONDS = 60
SYMBOL_SLEEP_SECONDS = 3
POSITION_SYNC_SECONDS = 60


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ====================== HELPERS ======================

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
    except Exception as exc:
        logger.error("Telegram error: %s", exc)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ====================== BOT ======================

class MultiFactorBot:
    def __init__(self):
        if not API_KEY or not API_SECRET or not API_PASSWORD:
            raise RuntimeError(
                "EXCHANGE_API_KEY, EXCHANGE_SECRET and EXCHANGE_PASSWORD "
                "must be configured."
            )

        exchange_class = getattr(ccxt, EXCHANGE_ID)

        self.exchange = exchange_class(
            {
                "apiKey": API_KEY,
                "secret": API_SECRET,
                "password": API_PASSWORD,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "swap",
                },
            }
        )

        self.exchange.load_markets()

        missing = [s for s in SYMBOLS if s not in self.exchange.markets]
        if missing:
            raise RuntimeError(
                f"Symbols not available on BloFin: {', '.join(missing)}"
            )

        logger.info(
            "Markets geladen. %d/%d symbolen beschikbaar.",
            len(SYMBOLS),
            len(SYMBOLS),
        )

        # Local state is only a cache. The exchange is the source of truth.
        self.state: Dict[str, dict] = {
            s: {
                "in_position": False,
                "entry_price": 0.0,
                "side": None,
                "contracts": 0.0,
                "last_sync": None,
            }
            for s in SYMBOLS
        }

        self.last_position_sync = 0.0
        self.lock = threading.Lock()

    # ---------------------- MARKET DATA ----------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 250,
    ) -> Optional[pd.DataFrame]:
        try:
            ohlcv = self.exchange.fetch_ohlcv(
                symbol,
                timeframe,
                limit=limit,
            )

            if not ohlcv or len(ohlcv) < 5:
                return None

            df = pd.DataFrame(
                ohlcv,
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ],
            )

            df["timestamp"] = pd.to_datetime(
                df["timestamp"],
                unit="ms",
                utc=True,
            )

            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df["atr"] = ta.atr(
                df["high"],
                df["low"],
                df["close"],
                length=ATR_LEN,
            )
            df["ema20"] = ta.ema(df["close"], length=EMA_FAST)
            df["ema50"] = ta.ema(df["close"], length=EMA_MID)
            df["ema200"] = ta.ema(df["close"], length=EMA_SLOW)
            df["vol_sma"] = ta.sma(
                df["volume"],
                length=VOLUME_LEN,
            )

            df = df.dropna().reset_index(drop=True)

            if len(df) < 10:
                return None

            return df

        except Exception as exc:
            logger.error(
                "Fetch error %s (%s): %s",
                symbol,
                timeframe,
                exc,
            )
            return None

    def last_closed(self, df: pd.DataFrame) -> pd.Series:
        if USE_CLOSED_CANDLES and len(df) >= 2:
            return df.iloc[-2]

        return df.iloc[-1]

    # ---------------------- POSITIONS ----------------------

    def fetch_open_positions(self) -> Dict[str, dict]:
        try:
            positions = self.exchange.fetch_positions(SYMBOLS)
        except Exception as exc:
            logger.error("Position sync failed: %s", exc)
            return {}

        result: Dict[str, dict] = {}

        for position in positions:
            symbol = position.get("symbol")
            if symbol not in SYMBOLS:
                continue

            contracts_raw = position.get("contracts")
            if contracts_raw is None:
                contracts_raw = position.get("contractSize")

            try:
                contracts = abs(float(contracts_raw or 0))
            except (TypeError, ValueError):
                contracts = 0.0

            if contracts <= 0:
                continue

            side = position.get("side")
            entry_price = float(position.get("entryPrice") or 0)

            result[symbol] = {
                "in_position": True,
                "entry_price": entry_price,
                "side": side,
                "contracts": contracts,
                "last_sync": utc_now(),
                "raw": position,
            }

        return result

    def sync_positions(self) -> Dict[str, dict]:
        open_positions = self.fetch_open_positions()

        with self.lock:
            for symbol in SYMBOLS:
                position = open_positions.get(symbol)

                if position:
                    self.state[symbol].update(
                        {
                            "in_position": True,
                            "entry_price": position["entry_price"],
                            "side": position["side"],
                            "contracts": position["contracts"],
                            "last_sync": position["last_sync"],
                        }
                    )
                else:
                    self.state[symbol].update(
                        {
                            "in_position": False,
                            "entry_price": 0.0,
                            "side": None,
                            "contracts": 0.0,
                            "last_sync": utc_now(),
                        }
                    )

        return open_positions

    def active_position_count(self) -> int:
        return sum(
            1
            for state in self.state.values()
            if state["in_position"]
        )

    # ---------------------- STRATEGY ----------------------

    def analyze_coin(self, symbol: str) -> Optional[dict]:
        df_15m = self.fetch_ohlcv(symbol, SCAN_TF, limit=80)
        df_1h = self.fetch_ohlcv(symbol, TREND_TF, limit=250)
        df_5m = self.fetch_ohlcv(symbol, CONFIRM_TF, limit=40)
        df_btc = self.fetch_ohlcv(
            "BTC/USDT:USDT",
            SCAN_TF,
            limit=20,
        )

        if any(
            df is None
            for df in [df_15m, df_1h, df_5m, df_btc]
        ):
            return None

        if (
            len(df_15m) < LOOKBACK + 5
            or len(df_5m) < 5
            or len(df_btc) < 5
        ):
            return None

        h_curr = self.last_closed(df_1h)
        c_curr = self.last_closed(df_15m)
        m5_curr = self.last_closed(df_5m)
        m5_prev = df_5m.iloc[-3] if USE_CLOSED_CANDLES else df_5m.iloc[-2]
        btc_curr = self.last_closed(df_btc)

        bullish_trend = (
            h_curr["ema20"] > h_curr["ema50"]
            and h_curr["ema50"] > h_curr["ema200"]
        )

        bearish_trend = (
            h_curr["ema20"] < h_curr["ema50"]
            and h_curr["ema50"] < h_curr["ema200"]
        )

        current_index = len(df_15m) - 2 if USE_CLOSED_CANDLES else len(df_15m) - 1
        start_index = current_index - LOOKBACK

        if start_index < 0:
            return None

        previous_window = df_15m.iloc[start_index:current_index]

        prev_low = previous_window["low"].min()
        prev_high = previous_window["high"].max()

        long_sweep = (
            c_curr["low"] < prev_low
            and c_curr["close"] > prev_low
        )

        short_sweep = (
            c_curr["high"] > prev_high
            and c_curr["close"] < prev_high
        )

        volume_ok = (
            c_curr["volume"]
            > c_curr["vol_sma"] * VOLUME_MULT
        )

        long_break = m5_curr["close"] > m5_prev["high"]
        short_break = m5_curr["close"] < m5_prev["low"]

        btc_base = df_btc.iloc[-5]["close"]
        coin_base = df_15m.iloc[-5]["close"]

        if btc_base <= 0 or coin_base <= 0:
            return None

        btc_return = (
            btc_curr["close"] / btc_base
        ) - 1

        coin_return = (
            c_curr["close"] / coin_base
        ) - 1

        relative = coin_return - btc_return

        strong_relative = relative > 0.002
        weak_relative = relative < -0.002

        atr_ratio = c_curr["atr"] / c_curr["close"]

        long_score = 0
        long_score += 15 if bullish_trend else 0
        long_score += 10 if c_curr["ema20"] > c_curr["ema50"] else 0
        long_score += 20 if long_sweep else 0
        long_score += 5 if c_curr["close"] > c_curr["ema20"] else 0
        long_score += 10 if volume_ok else 0
        long_score += 10 if strong_relative else 0
        long_score += 10 if long_break else 0
        long_score += 5 if atr_ratio > 0.004 else 0

        short_score = 0
        short_score += 15 if bearish_trend else 0
        short_score += 10 if c_curr["ema20"] < c_curr["ema50"] else 0
        short_score += 20 if short_sweep else 0
        short_score += 5 if c_curr["close"] < c_curr["ema20"] else 0
        short_score += 10 if volume_ok else 0
        short_score += 10 if weak_relative else 0
        short_score += 10 if short_break else 0
        short_score += 5 if atr_ratio > 0.004 else 0

        if (
            long_score >= MIN_SCORE
            and long_score > short_score
        ):
            return {
                "symbol": symbol,
                "direction": "long",
                "score": long_score,
                "price": float(c_curr["close"]),
                "atr": float(c_curr["atr"]),
                "timestamp": c_curr["timestamp"].isoformat(),
            }

        if (
            short_score >= MIN_SCORE
            and short_score > long_score
        ):
            return {
                "symbol": symbol,
                "direction": "short",
                "score": short_score,
                "price": float(c_curr["close"]),
                "atr": float(c_curr["atr"]),
                "timestamp": c_curr["timestamp"].isoformat(),
            }

        return None

    # ---------------------- ORDER SIZING ----------------------

    def calculate_contracts(
        self,
        symbol: str,
        entry: float,
    ) -> float:
        market = self.exchange.market(symbol)

        contract_size = float(
            market.get("contractSize") or 1
        )

        position_value = (
            FIXED_MARGIN_USDT * FIXED_LEVERAGE
        )

        base_amount = position_value / entry

        contracts = base_amount / contract_size

        contracts = float(
            self.exchange.amount_to_precision(
                symbol,
                contracts,
            )
        )

        limits = market.get("limits", {})
        amount_limits = limits.get("amount", {})

        min_amount = amount_limits.get("min")
        max_amount = amount_limits.get("max")

        if min_amount is not None and contracts < float(min_amount):
            raise ValueError(
                f"{symbol}: calculated contracts {contracts} "
                f"below exchange minimum {min_amount}"
            )

        if max_amount is not None and contracts > float(max_amount):
            raise ValueError(
                f"{symbol}: calculated contracts {contracts} "
                f"above exchange maximum {max_amount}"
            )

        if contracts <= 0:
            raise ValueError(
                f"{symbol}: calculated contract amount is <= 0"
            )

        return contracts

    def price_to_precision(
        self,
        symbol: str,
        price: float,
    ) -> float:
        return float(
            self.exchange.price_to_precision(
                symbol,
                price,
            )
        )

    # ---------------------- EXECUTION ----------------------

    def execute_trade(self, setup: dict) -> None:
        symbol = setup["symbol"]
        direction = setup["direction"]
        reference_price = setup["price"]
        atr = setup["atr"]

        with self.lock:
            if self.state[symbol]["in_position"]:
                return

            if self.active_position_count() >= MAX_ACTIVE_POSITIONS:
                logger.info(
                    "Max active positions reached (%d). Skip %s.",
                    MAX_ACTIVE_POSITIONS,
                    symbol,
                )
                return

        if direction == "long":
            stop = reference_price - (
                atr * STOP_ATR_MULT
            )
            target = reference_price + (
                (reference_price - stop)
                * RISK_REWARD
            )
            side = "buy"
        else:
            stop = reference_price + (
                atr * STOP_ATR_MULT
            )
            target = reference_price - (
                (stop - reference_price)
                * RISK_REWARD
            )
            side = "sell"

        stop = self.price_to_precision(symbol, stop)
        target = self.price_to_precision(symbol, target)

        try:
            amount = self.calculate_contracts(
                symbol,
                reference_price,
            )
        except Exception as exc:
            logger.error(
                "Sizing error on %s: %s",
                symbol,
                exc,
            )
            return

        msg = (
            f"🎯 <b>MULTI-FACTOR SIGNAL "
            f"({setup['score']}/100)</b>\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Direction: <b>{direction.upper()}</b>\n"
            f"Reference: <code>{reference_price:.8f}</code>\n"
            f"SL: <code>{stop:.8f}</code>\n"
            f"TP: <code>{target:.8f}</code>\n"
            f"Contracts: <code>{amount}</code>\n"
            f"Margin: {FIXED_MARGIN_USDT} USDT\n"
            f"Leverage: {FIXED_LEVERAGE}x"
        )

        send_telegram(msg)

        if PAPER_TRADING:
            logger.info(
                "[PAPER] %s %s amount=%s entry=%s SL=%s TP=%s",
                direction.upper(),
                symbol,
                amount,
                reference_price,
                stop,
                target,
            )

            with self.lock:
                self.state[symbol].update(
                    {
                        "in_position": True,
                        "entry_price": reference_price,
                        "side": direction,
                        "contracts": amount,
                        "last_sync": utc_now(),
                    }
                )

            self.log_trade(
                {
                    "time": utc_now(),
                    "mode": "paper",
                    "symbol": symbol,
                    "direction": direction,
                    "score": setup["score"],
                    "entry": reference_price,
                    "stop": stop,
                    "target": target,
                    "contracts": amount,
                }
            )
            return

        try:
            self.exchange.set_margin_mode(
                "isolated",
                symbol,
            )
        except Exception as exc:
            logger.warning(
                "set_margin_mode failed for %s: %s",
                symbol,
                exc,
            )

        try:
            self.exchange.set_leverage(
                FIXED_LEVERAGE,
                symbol,
            )
        except Exception as exc:
            logger.warning(
                "set_leverage failed for %s: %s",
                symbol,
                exc,
            )

        entry_order = None

        try:
            params = {
                "marginMode": "isolated",
                "reduceOnly": False,
                "stopLoss": {
                    "triggerPrice": stop,
                },
                "takeProfit": {
                    "triggerPrice": target,
                },
            }

            entry_order = self.exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=amount,
                params=params,
            )

            logger.info(
                "Entry order submitted: %s",
                entry_order.get("id"),
            )

            time.sleep(1)

            positions = self.fetch_open_positions()
            live_position = positions.get(symbol)

            if not live_position:
                raise RuntimeError(
                    "Entry order returned, but no open position "
                    "was found during verification."
                )

            with self.lock:
                self.state[symbol].update(
                    {
                        "in_position": True,
                        "entry_price": live_position["entry_price"],
                        "side": live_position["side"],
                        "contracts": live_position["contracts"],
                        "last_sync": utc_now(),
                    }
                )

            self.log_trade(
                {
                    "time": utc_now(),
                    "mode": "live",
                    "symbol": symbol,
                    "direction": direction,
                    "score": setup["score"],
                    "entry_reference": reference_price,
                    "entry_actual": live_position["entry_price"],
                    "stop": stop,
                    "target": target,
                    "contracts": live_position["contracts"],
                    "order_id": entry_order.get("id"),
                }
            )

            send_telegram(
                f"✅ <b>ENTRY VERIFIED</b>\n"
                f"{symbol} {direction.upper()}\n"
                f"Entry: <code>"
                f"{live_position['entry_price']:.8f}"
                f"</code>\n"
                f"SL: <code>{stop:.8f}</code>\n"
                f"TP: <code>{target:.8f}</code>"
            )

        except Exception as exc:
            logger.exception(
                "Execution error on %s: %s",
                symbol,
                exc,
            )

            self.sync_positions()

            send_telegram(
                f"❌ <b>TRADE ERROR</b>\n"
                f"{symbol}\n"
                f"{str(exc)[:500]}"
            )

    # ---------------------- LOGGING ----------------------

    def log_trade(self, trade: dict) -> None:
        try:
            history = []

            if os.path.exists(TRADE_LOG_FILE):
                with open(
                    TRADE_LOG_FILE,
                    "r",
                    encoding="utf-8",
                ) as file:
                    history = json.load(file)

            if not isinstance(history, list):
                history = []

            history.append(trade)
            history = history[-1000:]

            with open(
                TRADE_LOG_FILE,
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    history,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )

        except Exception as exc:
            logger.error(
                "Could not write trade log: %s",
                exc,
            )

    # ---------------------- MAIN LOOP ----------------------

    def run(self) -> None:
        send_telegram(
            "🚀 <b>Multi-Factor Bot v2 gestart</b>\n"
            f"{len(SYMBOLS)} symbolen\n"
            f"Max posities: {MAX_ACTIVE_POSITIONS}\n"
            f"Paper trading: {PAPER_TRADING}"
        )

        self.sync_positions()

        while True:
            cycle_start = time.time()

            try:
                if (
                    time.time() - self.last_position_sync
                    >= POSITION_SYNC_SECONDS
                ):
                    self.sync_positions()
                    self.last_position_sync = time.time()

                if (
                    self.active_position_count()
                    >= MAX_ACTIVE_POSITIONS
                ):
                    logger.info(
                        "Maximum open positions reached: %d",
                        MAX_ACTIVE_POSITIONS,
                    )
                else:
                    for symbol in SYMBOLS:
                        try:
                            self.sync_positions()

                            if (
                                self.active_position_count()
                                >= MAX_ACTIVE_POSITIONS
                            ):
                                break

                            if self.state[symbol]["in_position"]:
                                continue

                            setup = self.analyze_coin(symbol)

                            if setup:
                                logger.info(
                                    "Signal: %s %s score=%s",
                                    symbol,
                                    setup["direction"],
                                    setup["score"],
                                )
                                self.execute_trade(setup)

                        except Exception as exc:
                            logger.exception(
                                "Loop error for %s: %s",
                                symbol,
                                exc,
                            )

                        time.sleep(SYMBOL_SLEEP_SECONDS)

            except Exception as exc:
                logger.exception(
                    "Main loop error: %s",
                    exc,
                )

            elapsed = time.time() - cycle_start
            sleep_for = max(
                1,
                SCAN_SLEEP_SECONDS - elapsed,
            )

            logger.info(
                "Scan cycle complete in %.1fs. Sleeping %.1fs.",
                elapsed,
                sleep_for,
            )

            time.sleep(sleep_for)


# ====================== FLASK ======================

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(
        host="0.0.0.0",
        port=port,
    )


if __name__ == "__main__":
    # Start de Flask webserver in een achtergrond-thread (voor Railway healthchecks)
    web_thread = threading.Thread(target=run_web)
    web_thread.daemon = True
    web_thread.start()

    # Start de Multi-Factor Bot
    bot = MultiFactorBot()
    bot.run()
