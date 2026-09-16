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

# Juiste symbolen voor BloFin perpetual futures
SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "ADA/USDT:USDT",
    "DOGE/USDT:USDT",
    "AVAX/USDT:USDT",
    "ZEC/USDT:USDT",
    "XLM/USDT:USDT",
    "HBAR/USDT:USDT",
    "XMR/USDT:USDT",
    "TRX/USDT:USDT",
    "THETA/USDT:USDT",
    "LTC/USDT:USDT"
]

# Strategie parameters
TIMEFRAME_ENTRY = "1m"
TIMEFRAME_FILTER = "5m"

LOOKBACK = 50
ZONE_ATR_MULT = 0.6
MIN_BREAK_ATR = 1.4
RR_RATIO = 2.0
MAX_RISK_USDT = 1.0             # Richtlijn: max €1 risico per trade
LEVERAGE = 7                    # Hefboom ingesteld op 7x
ATR_LENGTH = 14
EMA_FILTER_LENGTH = 20
SCAN_INTERVAL = 12
MAX_BREAKOUT_AGE_MINUTES = 22
MAX_ACTIVE_ORDERS = 5           # Maximaal aantal gelijktijdige open orders/posities

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
            "options": {"defaultType": "swap"}
        })

        try:
            self.exchange.load_markets()
            logger.info(f"Markets geladen. Aantal beschikbare markten: {len(self.exchange.markets)}")
        except Exception as e:
            logger.error(f"Kon markets niet laden: {e}")

        self.state: Dict[str, dict] = {}
        for symbol in SYMBOLS:
            self.state[symbol] = {
                "direction": None,
                "broke_out": False,
                "zone": None,
                "level": None,
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
        df = self.fetch_ohlcv(symbol, TIMEFRAME_FILTER, limit=50)
        if df is None or len(df) < EMA_FILTER_LENGTH + 5:
            return None

        current = df.iloc[-1]
        prev = df.iloc[-2]

        price_above_ema = float(current["close"]) > float(current["ema"])
        ema_rising = float(current["ema"]) > float(prev["ema"])
        last_bullish = float(current["close"]) > float(current["open"])

        bull_score = sum([price_above_ema, ema_rising, last_bullish])
        bear_score = sum([not price_above_ema, not ema_rising, not last_bullish])

        if bull_score >= 2:
            return "bullish"
        if bear_score >= 2:
            return "bearish"
        return None

    def find_zones(self, df: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
        if len(df) < 30:
            return None, None

        highs = df["high"].values
        lows = df["low"].values
        atr = float(df["atr"].iloc[-1])

        if pd.isna(atr) or atr == 0:
            return None, None

        resistance = None
        for i in range(len(df) - 8, 8, -1):
            if highs[i] == max(highs[i-7:i+8]) and highs[i] > highs[i-3] and highs[i] > highs[i+3]:
                resistance = float(highs[i])
                break

        support = None
        for i in range(len(df) - 8, 8, -1):
            if lows[i] == min(lows[i-7:i+8]) and lows[i] < lows[i-3] and lows[i] < lows[i+3]:
                support = float(lows[i])
                break

        return resistance, support

    def check_setup(self, symbol: str, df_1m: pd.DataFrame) -> Optional[dict]:
        if len(df_1m) < LOOKBACK:
            return None

        current = df_1m.iloc[-1]
        atr = float(current["atr"])
        if pd.isna(atr) or atr == 0:
            return None

        state = self.state[symbol]
        resistance, support = self.find_zones(df_1m)
        htf_bias = self.get_higher_tf_bias(symbol)

        close = float(current["close"])
        open_p = float(current["open"])
        vol = float(current["volume"])

        # Long Setup
        if not state["broke_out"] and resistance is not None and htf_bias == "bullish":
            strong_break = (
                close > resistance and
                (close - resistance) > atr * MIN_BREAK_ATR and
                close > open_p and
                vol > float(df_1m["volume"].iloc[-10:-1].mean()) * 0.8
            )
            if strong_break:
                half = atr * ZONE_ATR_MULT
                zone = (float(resistance - half * 0.4), float(resistance + half * 0.8))
                state.update({
                    "direction": "long",
                    "broke_out": True,
                    "zone": zone,
                    "level": float(resistance),
                    "break_time": datetime.now()
                })
                send_telegram(
                    f"🚀 <b>LONG BREAKOUT</b> {symbol}\n"
                    f"Resistance: <code>{resistance:.4f}</code>\n"
                    f"Zone: {zone[0]:.4f} - {zone[1]:.4f}"
                )

        # Short Setup
        if not state["broke_out"] and support is not None and htf_bias == "bearish":
            strong_break = (
                close < support and
                (support - close) > atr * MIN_BREAK_ATR and
                close < open_p and
                vol > float(df_1m["volume"].iloc[-10:-1].mean()) * 0.8
            )
            if strong_break:
                half = atr * ZONE_ATR_MULT
                zone = (float(support - half * 0.8), float(support + half * 0.4))
                state.update({
                    "direction": "short",
                    "broke_out": True,
                    "zone": zone,
                    "level": float(support),
                    "break_time": datetime.now()
                })
                send_telegram(
                    f"🔻 <b>SHORT BREAKDOWN</b> {symbol}\n"
                    f"Support: <code>{support:.4f}</code>\n"
                    f"Zone: {zone[0]:.4f} - {zone[1]:.4f}"
                )

        # Retest + Entry
        if state["broke_out"] and state["zone"] and not state["in_position"]:
            zone_bottom, zone_top = state["zone"]
            direction = state["direction"]
            low = float(current["low"])
            high = float(current["high"])

            if direction == "long" and htf_bias != "bullish":
                return None
            if direction == "short" and htf_bias != "bearish":
                return None

            if direction == "long":
                touched = low <= zone_top and low >= zone_bottom * 0.998
                confirm = (
                    close > open_p and
                    close > zone_top * 0.998 and
                    low <= zone_top
                )
                if touched and confirm:
                    entry = close
                    stop = float(zone_bottom - (atr * 0.3))
                    risk = entry - stop
                    target = float(entry + (risk * RR_RATIO))
                    return {"symbol": symbol, "direction": "long", "entry": entry, "stop": stop, "target": target, "zone": state["zone"]}

            elif direction == "short":
                touched = high >= zone_bottom and high <= zone_top * 1.002
                confirm = (
                    close < open_p and
                    close < zone_bottom * 1.002 and
                    high >= zone_bottom
                )
                if touched and confirm:
                    entry = close
                    stop = float(zone_top + (atr * 0.3))
                    risk = stop - entry
                    target = float(entry - (risk * RR_RATIO))
                    return {"symbol": symbol, "direction": "short", "entry": entry, "stop": stop, "target": target, "zone": state["zone"]}

        if state["broke_out"] and state["break_time"]:
            age = (datetime.now() - state["break_time"]).total_seconds() / 60
            if age > MAX_BREAKOUT_AGE_MINUTES:
                state.update({
                    "direction": None,
                    "broke_out": False,
                    "zone": None,
                    "level": None,
                    "break_time": None,
                    "in_position": False
                })

        return None

    def calculate_position_size(self, symbol: str, entry: float, stop: float) -> float:
        try:
            risk_per_unit = abs(entry - stop)
            if risk_per_unit <= 0:
                return 0
            
            calculated_amount = MAX_RISK_USDT / risk_per_unit
            market = self.exchange.market(symbol)
            min_amount = float(market['limits']['amount']['min'])
            max_amount = float(market['limits']['amount'].get('max', float('inf')))
            
            if calculated_amount < min_amount:
                calculated_amount = min_amount
            if max_amount and calculated_amount > max_amount:
                calculated_amount = max_amount
            
            # Zorg dat de hoeveelheid netjes voldoet aan de precision regels van BloFin
            precision_amount = market['precision']['amount']
            if precision_amount:
                # Afronden op de juiste decimalen van de beurs
                return float(self.exchange.amount_to_precision(symbol, calculated_amount))
            return float(calculated_amount)
        except Exception as e:
            logger.error(f"Position size error for {symbol}: {e}")
            return 0

    def check_open_positions_and_orders(self) -> int:
        if PAPER_TRADING:
            return sum(1 for s in SYMBOLS if self.state[s]["in_position"])

        try:
            open_orders = self.exchange.fetch_open_orders()
            positions = self.exchange.fetch_positions(SYMBOLS)
            active_positions = [p for p in positions if float(p.get('contracts', 0)) > 0]

            total_active = len(open_orders) + len(active_positions)
            
            active_symbols = {p['symbol'] for p in active_positions}
            for symbol in SYMBOLS:
                if self.state[symbol]["in_position"] and symbol not in active_symbols:
                    self.state[symbol] = {
                        "direction": None,
                        "broke_out": False,
                        "zone": None,
                        "level": None,
                        "break_time": None,
                        "in_position": False
                    }

            return total_active
        except Exception as e:
            logger.error(f"Fout bij controleren open orders/posities: {e}")
            return 0

    def execute_trade(self, setup: dict):
        active_count = self.check_open_positions_and_orders()
        if active_count >= MAX_ACTIVE_ORDERS:
            logger.info(f"Max aantal actieve orders/posities bereikt ({active_count}/{MAX_ACTIVE_ORDERS}). Trade overgeslagen.")
            return

        symbol = setup["symbol"]
        direction = setup["direction"]
        
        entry = float(setup["entry"])
        stop = float(setup["stop"])
        target = float(setup["target"])
        zone = (float(setup["zone"][0]), float(setup["zone"][1]))

        amount = self.calculate_position_size(symbol, entry, stop)
        if amount <= 0:
            return

        side = "buy" if direction == "long" else "sell"
        side_close = "sell" if direction == "long" else "buy"

        emoji = "✅" if direction == "long" else "🔻"
        msg = (
            f"{emoji} <b>{direction.upper()} ENTRY [BloFin]</b> {symbol}\n\n"
            f"Entry:      <code>{entry:.4f}</code>\n"
            f"Stop Loss:  <code>{stop:.4f}</code>\n"
            f"Take Profit:<code>{target:.4f}</code>\n"
            f"Risk:Reward → 1:{RR_RATIO}\n"
            f"Qty: {amount} (Lev: {LEVERAGE}x)"
        )
        send_telegram(msg)

        if PAPER_TRADING:
            logger.info(f"[PAPER] {direction.upper()} trade gesimuleerd → {symbol}")
            self.state[symbol]["in_position"] = True
            return

        try:
            try:
                self.exchange.set_leverage(LEVERAGE, symbol)
            except Exception:
                pass

            # Open market order
            self.exchange.create_order(symbol=symbol, type="market", side=side, amount=amount)
            
            # Stop Loss (met gepaste floats)
            self.exchange.create_order(
                symbol=symbol, type="market", side=side_close, amount=amount,
                params={"stopLossPrice": float(stop), "reduceOnly": True}
            )
            
            # Take Profit (met gepaste floats)
            self.exchange.create_order(
                symbol=symbol, type="limit", side=side_close, amount=amount, price=float(target),
                params={"takeProfitPrice": float(target), "reduceOnly": True}
            )

            self.state[symbol]["in_position"] = True
            send_telegram(f"📌 <b>LIVE ORDERS GEPLAATST [7x Lev]</b> {symbol}")
        except Exception as e:
            logger.error(f"Order error {symbol}: {e}")
            send_telegram(f"⚠️ <b>Order Fout op {symbol}</b>: {e}")

    def run(self):
        logger.info("Break & Retest Bot (Fix JSON + Precision) gestart...")
        send_telegram("🤖 <b>Break & Retest Bot gestart! (Met JSON & Precision fixes)</b>")
        
        while True:
            self.check_open_positions_and_orders()

            for symbol in SYMBOLS:
                try:
                    df_1m = self.fetch_ohlcv(symbol, TIMEFRAME_ENTRY, limit=80)
                    if df_1m is not None:
                        setup = self.check_setup(symbol, df_1m)
                        if setup and not self.state[symbol]["in_position"]:
                            self.execute_trade(setup)
                except Exception as e:
                    logger.error(f"Fout bij scannen van {symbol}: {e}")
                
                time.sleep(2)
            
            time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    bot = BreakRetestBot()
    bot.run()
