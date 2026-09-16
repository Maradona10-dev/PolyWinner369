import ccxt
import pandas as pd
import pandas_ta as ta
import time
import logging
import os
import requests
import json
from datetime import datetime
from typing import Dict, Optional, Tuple
from flask import Thread, Flask

# ====================== RAILWAY & CONFIG ======================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "BloFin Multi-Factor Bot is running active!", 200

API_KEY = os.environ.get("EXCHANGE_API_KEY", "")
API_SECRET = os.environ.get("EXCHANGE_SECRET", "")
API_PASSWORD = os.environ.get("EXCHANGE_PASSWORD", "")  # Verplicht voor BloFin
EXCHANGE_ID = "blofin"

PAPER_TRADING = os.environ.get("PAPER_TRADING", "False").lower() == "true"

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# BloFin Perpetual Symbols
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
    "SUI/USDT:USDT"
]

# Strategie parameters (gebaseerd op je 15M/1H logica)
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
MAX_ACTIVE_ORDERS = 2

TRADE_LOG_FILE = "trade_history.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Telegram error: {e}")


class MultiFactorBot:
    def __init__(self):
        exchange_class = getattr(ccxt, EXCHANGE_ID)
        self.exchange = exchange_class({
            "apiKey": API_KEY,
            "secret": API_SECRET,
            "password": API_PASSWORD,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"}
        })

        try:
            self.exchange.load_markets()
            logger.info(f"Markets geladen voor BloFin. Actief op {len(SYMBOLS)} symbolen.")
        except Exception as e:
            logger.error(f"Kon markets niet laden: {e}")

        self.state: Dict[str, dict] = {s: {"in_position": False, "entry_price": 0.0} for s in SYMBOLS}

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> Optional[pd.DataFrame]:
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=ATR_LEN)
            df["ema20"] = ta.ema(df["close"], length=EMA_FAST)
            df["ema50"] = ta.ema(df["close"], length=EMA_MID)
            df["ema200"] = ta.ema(df["close"], length=EMA_SLOW)
            df["vol_sma"] = ta.sma(df["volume"], length=VOLUME_LEN)
            return df
        except Exception as e:
            logger.error(f"Fetch error {symbol} ({timeframe}): {e}")
            return None

    def analyze_coin(self, symbol: str) -> Optional[dict]:
        df_15m = self.fetch_ohlcv(symbol, SCAN_TF, limit=60)
        df_1h = self.fetch_ohlcv(symbol, TREND_TF, limit=250)
        df_5m = self.fetch_ohlcv(symbol, CONFIRM_TF, limit=30)
        df_btc = self.fetch_ohlcv("BTC/USDT:USDT", SCAN_TF, limit=10)

        if df_15m is None or df_1h is None or df_5m is None or df_btc is None:
            return None

        # 1H Trend Regimes
        h_curr = df_1h.iloc[-1]
        bullish_trend = h_curr["ema20"] > h_curr["ema50"] and h_curr["ema50"] > h_curr["ema200"]
        bearish_trend = h_curr["ema20"] < h_curr["ema50"] and h_curr["ema50"] < h_curr["ema200"]

        # 15M Data & Liquidity Sweeps
        c_curr = df_15m.iloc[-1]
        l_curr = df_15m.iloc[-1]
        
        prev_low = df_15m["low"].iloc[-LOOKBACK:-1].min()
        prev_high = df_15m["high"].iloc[-LOOKBACK:-1].max()

        long_sweep = c_curr["low"] < prev_low and c_curr["close"] > prev_low
        short_sweep = c_curr["high"] > prev_high and c_curr["close"] < prev_high

        volume_ok = c_curr["volume"] > (c_curr["vol_sma"] * VOLUME_MULT)

        # 5M Confirmations
        m5_prev = df_5m.iloc[-2]
        m5_curr = df_5m.iloc[-1]
        long_break = m5_curr["close"] > m5_prev["high"]
        short_break = m5_curr["close"] < m5_prev["low"]

        # Relative Strength vs BTC
        btc_return = (df_btc.iloc[-1]["close"] / df_btc.iloc[-4]["close"]) - 1
        coin_return = (df_15m.iloc[-1]["close"] / df_15m.iloc[-4]["close"]) - 1
        relative = coin_return - btc_return
        strong_relative = relative > 0.002
        weak_relative = relative < -0.002

        # Scoring System (0-100)
        long_score = 0
        long_score += 15 if bullish_trend else 0
        long_score += 10 if c_curr["ema20"] > c_curr["ema50"] else 0
        long_score += 20 if long_sweep else 0
        long_score += 5 if c_curr["close"] > c_curr["ema20"] else 0
        long_score += 10 if volume_ok else 0
        long_score += 10 if strong_relative else 0
        long_score += 10 if long_break else 0
        long_score += 5 if (c_curr["atr"] / c_curr["close"]) > 0.004 else 0

        short_score = 0
        short_score += 15 if bearish_trend else 0
        short_score += 10 if c_curr["ema20"] < c_curr["ema50"] else 0
        short_score += 20 if short_sweep else 0
        short_score += 5 if c_curr["close"] < c_curr["ema20"] else 0
        short_score += 10 if volume_ok else 0
        short_score += 10 if weak_relative else 0
        short_score += 10 if short_break else 0
        short_score += 5 if (c_curr["atr"] / c_curr["close"]) > 0.004 else 0

        if long_score >= MIN_SCORE and long_score > short_score:
            return {"symbol": symbol, "direction": "long", "score": long_score, "price": c_curr["close"], "atr": c_curr["atr"]}
        elif short_score >= MIN_SCORE and short_score > long_score:
            return {"symbol": symbol, "direction": "short", "score": short_score, "price": c_curr["close"], "atr": c_curr["atr"]}

        return None

    def execute_trade(self, setup: dict):
        symbol = setup["symbol"]
        direction = setup["direction"]
        entry = setup["price"]
        atr = setup["atr"]

        if self.state[symbol]["in_position"]:
            return

        # Stop loss & take profit berekening op basis van ATR (Risk/Reward 1:2)
        if direction == "long":
            stop = entry - (atr * 1.5)
            target = entry + ((entry - stop) * 2.0)
            side = "buy"
            side_close = "sell"
        else:
            stop = entry + (atr * 1.5)
            target = entry - ((stop - entry) * 2.0)
            side = "sell"
            side_close = "buy"

        position_value = FIXED_MARGIN_USDT * FIXED_LEVERAGE
        amount = position_value / entry
        amount = float(self.exchange.amount_to_precision(symbol, amount))

        msg = (
            f"🎯 <b>STERK MULTI-FACTOR SIGNAAL ({setup['score']}/100)</b>\n"
            f"Munt: <b>{symbol}</b> ({direction.upper()})\n"
            f"Entry: <code>{entry:.4f}</code>\n"
            f"Stop Loss: <code>{stop:.4f}</code>\n"
            f"Take Profit: <code>{target:.4f}</code>\n"
            f"Marge: {FIXED_MARGIN_USDT} USDT ({FIXED_LEVERAGE}x)"
        )
        send_telegram(msg)

        if PAPER_TRADING:
            logger.info(f"[PAPER] Gesimuleerde trade geopend voor {symbol}")
            self.state[symbol]["in_position"] = True
            self.state[symbol]["entry_price"] = entry
            return

        try:
            self.exchange.set_margin_mode('isolated', symbol)
            self.exchange.set_leverage(FIXED_LEVERAGE, symbol)

            params = {"marginMode": "isolated", "leverage": FIXED_LEVERAGE}
            self.exchange.create_order(symbol=symbol, type="market", side=side, amount=amount, params=params)
            
            # SL & TP instellen
            self.exchange.create_order(symbol=symbol, type="market", side=side_close, amount=amount, params={"stopLossPrice": float(stop), "reduceOnly": True})
            self.exchange.create_order(symbol=symbol, type="limit", side=side_close, amount=amount, price=float(target), params={"takeProfitPrice": float(target), "reduceOnly": True})

            self.state[symbol]["in_position"] = True
            self.state[symbol]["entry_price"] = entry
            send_telegram(f"✅ Order met succes geplaatst op BloFin voor {symbol}")
        except Exception as e:
            logger.error(f"Execution error on {symbol}: {e}")

    def run(self):
        send_telegram("🚀 <b>Multi-Factor Scanner & Bot gestart op BloFin!</b>")
        while True:
            for symbol in SYMBOLS:
                try:
                    setup = self.analyze_coin(symbol)
                    if setup:
                        self.execute_trade(setup)
                except Exception as e:
                    logger.error(f"Loop error for {symbol}: {e}")
                time.sleep(3)
            time.sleep(60) # Scan elke minuut de lijst door


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    # Start Flask web server in een achtergrond-thread (vereist voor Railway)
    web_thread = threading.Thread(target=run_web)
    web_thread.daemon = True
    web_thread.start()

    # Start de bot
    bot = MultiFactorBot()
    bot.run()
