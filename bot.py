import ccxt
import pandas as pd
import pandas_ta as ta
import time
import logging
import os
import requests
from datetime import datetime
from typing import Dict, Optional, Tuple

# ====================== CONFIG ======================
API_KEY = os.environ.get("EXCHANGE_API_KEY", "")
API_SECRET = os.environ.get("EXCHANGE_SECRET", "")
API_PASSWORD = os.environ.get("EXCHANGE_PASSWORD", "")  # Verplicht voor BloFin
EXCHANGE_ID = "blofin"

PAPER_TRADING = os.environ.get("PAPER_TRADING", "True").lower() == "true"

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
    "XRP/USDT", "ADA/USDT", "DOGE/USDT", "AVAX/USDT",
    "ZEC/USDT", "XLM/USDT", "HBAR/USDT", "XMR/USDT",
    "TRX/USDT", "THETA/USDT", "LTC/USDT"
]

# Timeframes & Strategie Parameters
TIMEFRAME_ENTRY = "1m"
TIMEFRAME_FILTER = "5m"

LOOKBACK = 50
ZONE_ATR_MULT = 0.6
MIN_BREAK_ATR = 1.4
RR_RATIO = 2.0
RISK_PER_TRADE = 0.005
ATR_LENGTH = 14
EMA_FILTER_LENGTH = 20
SCAN_INTERVAL = 12
MAX_BREAKOUT_AGE_MINUTES = 22

# ====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("scalper_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Telegram error: {e}")


class BreakRetestBot:
    def __init__(self):
        exchange_class = getattr(ccxt, EXCHANGE_ID)
        self.exchange = exchange_class({
            "apiKey": API_KEY,
            "secret": API_SECRET,
            "password": API_PASSWORD,
            "enableRateLimit": True,
            "options": {"defaultType": "future"}
        })

        self.state: Dict[str, dict] = {}
        for symbol in SYMBOLS:
            self.state[symbol] = {
                "direction": None,          # "long" of "short"
                "broke_out": False,
                "zone": None,               # (bottom, top)
                "level": None,              # originele break level
                "break_time": None,
                "in_position": False
            }

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 80) -> Optional[pd.DataFrame]:
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=ATR_LENGTH)
            df["ema"] = ta.ema(df["close"], length=EMA_FILTER_LENGTH)
            return df
        except Exception as e:
            logger.error(f"Fetch error {symbol} {timeframe}: {e}")
            return None

    def get_higher_tf_bias(self, symbol: str) -> Optional[str]:
        """
        Geeft terug: "bullish", "bearish" of None
        """
        df = self.fetch_ohlcv(symbol, TIMEFRAME_FILTER, limit=50)
        if df is None or len(df) < EMA_FILTER_LENGTH + 5:
            return None

        current = df.iloc[-1]
        prev = df.iloc[-2]

        price_above_ema = current["close"] > current["ema"]
        ema_rising = current["ema"] > prev["ema"]
        last_bullish = current["close"] > current["open"]

        bull_score = sum([price_above_ema, ema_rising, last_bullish])
        bear_score = sum([not price_above_ema, not ema_rising, not last_bullish])

        if bull_score >= 2:
            return "bullish"
        if bear_score >= 2:
            return "bearish"
        return None

    def find_zones(self, df: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
        """Geeft (resistance, support) terug"""
        if len(df) < 30:
            return None, None

        highs = df["high"].values
        lows = df["low"].values
        atr = df["atr"].iloc[-1]

        if pd.isna(atr) or atr == 0:
            return None, None

        resistance = None
        for i in range(len(df) - 8, 8, -1):
            window = highs[i-7:i+8]
            if highs[i] == max(window) and highs[i] > highs[i-3] and highs[i] > highs[i+3]:
                resistance = highs[i]
                break

        support = None
        for i in range(len(df) - 8, 8, -1):
            window = lows[i-7:i+8]
            if lows[i] == min(window) and lows[i] < lows[i-3] and lows[i] < lows[i+3]:
                support = lows[i]
                break

        return resistance, support

    def check_setup(self, symbol: str, df_1m: pd.DataFrame) -> Optional[dict]:
        if len(df_1m) < LOOKBACK:
            return None

        current = df_1m.iloc[-1]
        atr = current["atr"]
        if pd.isna(atr) or atr == 0:
            return None

        state = self.state[symbol]
        resistance, support = self.find_zones(df_1m)
        htf_bias = self.get_higher_tf_bias(symbol)

        # ========== LONG SETUP ==========
        if not state["broke_out"] and resistance is not None and htf_bias == "bullish":
            strong_break = (
                current["close"] > resistance and
                (current["close"] - resistance) > atr * MIN_BREAK_ATR and
                current["close"] > current["open"] and
                current["volume"] > df_1m["volume"].iloc[-10:-1].mean() * 0.8
            )
            if strong_break:
                half = atr * ZONE_ATR_MULT
                zone = (resistance - half * 0.4, resistance + half * 0.8)
                state.update({
                    "direction": "long",
                    "broke_out": True,
                    "zone": zone,
                    "level": resistance,
                    "break_time": datetime.now()
                })
                send_telegram(
                    f"🚀 <b>LONG BREAKOUT [BloFin]</b> {symbol}\n"
                    f"Resistance: <code>{resistance:.4f}</code>\n"
                    f"Zone: {zone[0]:.4f} - {zone[1]:.4f}"
                )
                logger.info(f"{symbol} | LONG BREAKOUT @ {resistance:.4f}")

        # ========== SHORT SETUP ==========
        if not state["broke_out"] and support is not None and htf_bias == "bearish":
            strong_break = (
                current["close"] < support and
                (support - current["close"]) > atr * MIN_BREAK_ATR and
                current["close"] < current["open"] and
                current["volume"] > df_1m["volume"].iloc[-10:-1].mean() * 0.8
            )
            if strong_break:
                half = atr * ZONE_ATR_MULT
                zone = (support - half * 0.8, support + half * 0.4)
                state.update({
                    "direction": "short",
                    "broke_out": True,
                    "zone": zone,
                    "level": support,
                    "break_time": datetime.now()
                })
                send_telegram(
                    f"🔻 <b>SHORT BREAKDOWN [BloFin]</b> {symbol}\n"
                    f"Support: <code>{support:.4f}</code>\n"
                    f"Zone: {zone[0]:.4f} - {zone[1]:.4f}"
                )
                logger.info(f"{symbol} | SHORT BREAKDOWN @ {support:.4f}")

        # ========== RETEST + ENTRY ==========
        if state["broke_out"] and state["zone"] and not state["in_position"]:
            zone_bottom, zone_top = state["zone"]
            direction = state["direction"]

            # Extra check: HTF bias moet nog kloppen
            if direction == "long" and htf_bias != "bullish":
                return None
            if direction == "short" and htf_bias != "bearish":
                return None

            if direction == "long":
                touched = current["low"] <= zone_top and current["low"] >= zone_bottom * 0.998
                confirm = (
                    current["close"] > current["open"] and
                    current["close"] > zone_top * 0.998 and
                    current["low"] <= zone_top
                )
                if touched and confirm:
                    entry = current["close"]
                    stop = zone_bottom - (atr * 0.3)
                    risk = entry - stop
                    target = entry + (risk * RR_RATIO)
                    return {
                        "symbol": symbol,
                        "direction": "long",
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                        "zone": state["zone"]
                    }

            elif direction == "short":
                touched = current["high"] >= zone_bottom and current["high"] <= zone_top * 1.002
                confirm = (
                    current["close"] < current["open"] and
                    current["close"] < zone_bottom * 1.002 and
                    current["high"] >= zone_bottom
                )
                if touched and confirm:
                    entry = current["close"]
                    stop = zone_top + (atr * 0.3)
                    risk = stop - entry
                    target = entry - (risk * RR_RATIO)
                    return {
                        "symbol": symbol,
                        "direction": "short",
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                        "zone": state["zone"]
                    }

        # Reset oude setups
        if state["broke_out"] and state["break_time"]:
            age = (datetime.now() - state["break_time"]).total_seconds() / 60
            if age > MAX_BREAKOUT_AGE_MINUTES:
                logger.info(f"{symbol} | Setup verlopen → reset")
                state.update({
                    "direction": None,
                    "broke_out": False,
                    "zone": None,
                    "level": None,
                    "break_time": None,
                    "in_position": False
                })

        return None

    def calculate_position_size(self, entry: float, stop: float) -> float:
        try:
            balance = self.exchange.fetch_balance()
            usdt = balance.get("USDT", {}).get("free", 0)
            risk_amount = usdt * RISK_PER_TRADE
            risk_per_unit = abs(entry - stop)
            if risk_per_unit <= 0:
                return 0
            return round(risk_amount / risk_per_unit, 4)
        except Exception as e:
            logger.error(f"Position size error: {e}")
            return 0

    def execute_trade(self, setup: dict):
        symbol = setup["symbol"]
        direction = setup["direction"]
        entry = setup["entry"]
        stop = setup["stop"]
        target = setup["target"]
        zone = setup["zone"]

        amount = self.calculate_position_size(entry, stop)
        if amount <= 0:
            logger.warning(f"{symbol} | Ongeldige positiegrootte")
            return

        side = "buy" if direction == "long" else "sell"
        side_close = "sell" if direction == "long" else "buy"

        # Telegram bericht
        emoji = "✅" if direction == "long" else "🔻"
        msg = (
            f"{emoji} <b>{direction.upper()} ENTRY [BloFin]</b> {symbol}\n\n"
            f"Entry:      <code>{entry:.4f}</code>\n"
            f"Stop Loss:  <code>{stop:.4f}</code>\n"
            f"Take Profit:<code>{target:.4f}</code>\n"
            f"Risk:Reward → 1:{RR_RATIO}\n"
            f"Zone: {zone[0]:.4f} - {zone[1]:.4f}\n"
            f"Qty: {amount}"
        )
        send_telegram(msg)

        logger.info(f"{direction.upper()} {symbol} | Entry {entry:.4f} | SL {stop:.4f} | TP {target:.4f}")

        if PAPER_TRADING:
            logger.info(f"[PAPER] {direction.upper()} trade gesimuleerd → {symbol}")
            self.state[symbol]["in_position"] = True
            return

        # ========== LIVE ORDERS ==========
        try:
            # 1. Market entry
            entry_order = self.exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=amount
            )
            logger.info(f"Market {side} geplaatst: {entry_order.get('id')}")

            # 2. Stop Loss
            sl_order = self.exchange.create_order(
                symbol=symbol,
                type="STOP_MARKET",
                side=side_close,
                amount=amount,
                params={
                    "stopPrice": stop,
                    "reduceOnly": True
                }
            )
            logger.info(f"Stop Loss geplaatst: {sl_order.get('id')}")

            # 3. Take Profit
            tp_order = self.exchange.create_order(
                symbol=symbol,
                type="LIMIT",
                side=side_close,
                amount=amount,
                price=target,
                params={"reduceOnly": True}
            )
            logger.info(f"Take Profit geplaatst: {tp_order.get('id')}")

            self.state[symbol]["in_position"] = True
            send_telegram(
                f"📌 <b>LIVE ORDERS GEPLAATST [BloFin]</b> {symbol}\n"
                f"Order ID: {entry_order.get('id')}"
            )
        except Exception as e:
            logger.error(f"Order execution error for {symbol}: {e}")
            send_telegram(f"⚠️ <b>Order Fout op {symbol}</b>: {e}")

    def run(self):
        logger.info("Break & Retest Bot (Long & Short) voor BloFin gestart...")
        send_telegram("🤖 <b>Break & Retest Bot (Long & Short) gestart!</b>")
        
        while True:
            for symbol in SYMBOLS:
                try:
                    df_1m = self.fetch_ohlcv(symbol, TIMEFRAME_ENTRY, limit=80)
                    if df_1m is not None:
                        setup = self.check_setup(symbol, df_1m)
                        if setup and not self.state[symbol]["in_position"]:
                            self.execute_trade(setup)
                except Exception as e:
                    logger.error(f"Fout bij scannen van {symbol}: {e}")
                
                time.sleep(2)  # Pauze tussen munten i.v.m. rate limits
            
            time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    bot = BreakRetestBot()
    bot.run()
