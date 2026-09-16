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

# Strategie parameters (Vol Risico Modus)
TIMEFRAME_ENTRY = "1m"
TIMEFRAME_FILTER = "5m"

LOOKBACK = 50
ZONE_ATR_MULT = 0.6
MIN_BREAK_ATR = 1.4
RR_RATIO = 2.0
MAX_RISK_USDT = 8.0           # Vol risico per trade om positie-omvang op te schroeven
MIN_RISK_PERCENT = 0.002      # Voldoende afstand om extreme hoeveelheden te voorkomen
LEVERAGE = 7                    
ATR_LENGTH = 14
EMA_FILTER_LENGTH = 20
SCAN_INTERVAL = 12
MAX_BREAKOUT_AGE_MINUTES = 22
MAX_ACTIVE_ORDERS = 2           # Strikt maximaal 2 posities tegelijk!

TRADE_LOG_FILE = "trade_history.json"

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
                "in_position": False,
                "entry_price": 0.0
            }
            
        self.last_report_date = datetime.now().date()

    def log_completed_trade(self, symbol: str, direction: str, entry: float, exit_price: float, pnl: float):
        trade_data = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "exit": exit_price,
            "pnl": pnl,
            "win": pnl > 0
        }
        
        trades = []
        if os.path.exists(TRADE_LOG_FILE):
            try:
                with open(TRADE_LOG_FILE, "r") as f:
                    trades = json.load(f)
            except Exception:
                trades = []
                
        trades.append(trade_data)
        try:
            with open(TRADE_LOG_FILE, "w") as f:
                json.dump(trades, f, indent=4)
        except Exception as e:
            logger.error(f"Kon trade log niet wegschrijven: {e}")

    def send_performance_report(self):
        if not os.path.exists(TRADE_LOG_FILE):
            return
            
        try:
            with open(TRADE_LOG_FILE, "r") as f:
                trades = json.load(f)
        except Exception:
            return
            
        if not trades:
            return
            
        total_trades = len(trades)
        wins = [t for t in trades if t["win"]]
        winrate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
        total_pnl = sum(t["pnl"] for t in trades)
        
        coin_pnl = {}
        for t in trades:
            s = t["symbol"]
            coin_pnl[s] = coin_pnl.get(s, 0.0) + t["pnl"]
            
        best_coin = max(coin_pnl, key=coin_pnl.get) if coin_pnl else "Nvt"
        worst_coin = min(coin_pnl, key=coin_pnl.get) if coin_pnl else "Nvt"

        report = (
            f"📊 <b>DAGELIJKSE BOT RAPPORTAGE</b> 📊\n\n"
            f"Totaal aantal trades: <b>{total_trades}</b>\n"
            f"Winrate: <b>{winrate:.1f}%</b>\n"
            f"Geschatte PnL: <b>{total_pnl:.2f} USDT</b>\n"
            f"🏆 Beste munt: <b>{best_coin}</b> ({coin_pnl.get(best_coin, 0):.2f} USDT)\n"
            f"⚠️ Slechtste munt: <b>{worst_coin}</b> ({coin_pnl.get(worst_coin, 0):.2f} USDT)\n\n"
            f"<i>Op weg naar de 100 trades voor adaptieve analyse!</i>"
        )
        send_telegram(report)

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

        # Retest + Entry met robuuste stop-loss controle
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
                    stop = float(zone_bottom - (atr * 0.4))
                    
                    min_stop_distance = entry * MIN_RISK_PERCENT
                    if (entry - stop) < min_stop_distance:
                        stop = entry - min_stop_distance

                    risk = entry - stop
                    target = float(entry + (risk * RR_RATIO))
                    return {"symbol": symbol, "direction": "long", "entry": entry, "stop": stop, "target": target, "zone": state["zone"]}

            elif direction == "short":
                touched = high >= zone_bottom and high <= zone_top * 1.002
                confirm = (
                    close < open_p and
                    close < zone_top * 1.002 and
                    high >= zone_bottom
                )
                if touched and confirm:
                    entry = close
                    stop = float(zone_top + (atr * 0.4))
                    
                    min_stop_distance = entry * MIN_RISK_PERCENT
                    if (stop - entry) < min_stop_distance:
                        stop = entry + min_stop_distance

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
            balance = 32.0 
            try:
                bal_info = self.exchange.fetch_balance()
                balance = float(bal_info['USDT']['free'])
            except Exception:
                pass

            risk_per_unit = abs(entry - stop)
            if risk_per_unit <= 0:
                return 0
            
            calculated_amount = MAX_RISK_USDT / risk_per_unit
            # Maximaal toestaan tot 90% van de totale hefboomcapaciteit voor maximale inzet
            max_allowed_value = (balance * LEVERAGE) * 0.90
            max_qty_by_balance = max_allowed_value / entry
            
            if calculated_amount > max_qty_by_balance:
                calculated_amount = max_qty_by_balance

            market = self.exchange.market(symbol)
            min_amount = float(market['limits']['amount']['min'])
            max_amount = float(market['limits']['amount'].get('max', float('inf')))
            
            if calculated_amount < min_amount:
                calculated_amount = min_amount
            if max_amount and calculated_amount > max_amount:
                calculated_amount = max_amount
            
            precision_amount = market['precision']['amount']
            if precision_amount:
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
                    closed_state = self.state[symbol]
                    entry_p = closed_state.get("entry_price", 0)
                    direction = closed_state.get("direction", "long")
                    
                    exit_p = entry_p
                    for p in positions:
                        if p['symbol'] == symbol:
                            exit_p = float(p.get('markPrice', entry_p))
                            
                    pnl_est = (exit_p - entry_p) if direction == "long" else (entry_p - exit_p)
                    pnl_est *= 5.0 
                    
                    self.log_completed_trade(symbol, direction, entry_p, exit_p, pnl_est)

                    self.state[symbol] = {
                        "direction": None,
                        "broke_out": False,
                        "zone": None,
                        "level": None,
                        "break_time": None,
                        "in_position": False,
                        "entry_price": 0.0
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
            f"{emoji} <b>{direction.upper()} ENTRY [VOL RISICO MODUS]</b> {symbol}\n\n"
            f"Entry:      <code>{entry:.4f}</code>\n"
            f"Stop Loss:  <code>{stop:.4f}</code>\n"
            f"Take Profit:<code>{target:.4f}</code>\n"
            f"Risk:Reward → 1:{RR_RATIO}\n"
            f"Qty: {amount} (Lev: {LEVERAGE}x - Risico: {MAX_RISK_USDT} USDT)"
        )
        send_telegram(msg)

        if PAPER_TRADING:
            logger.info(f"[PAPER] {direction.upper()} trade gesimuleerd → {symbol}")
            self.state[symbol]["in_position"] = True
            self.state[symbol]["entry_price"] = entry
            return

        try:
            try:
                self.exchange.set_margin_mode('isolated', symbol)
                self.exchange.set_leverage(LEVERAGE, symbol)
            except Exception as ex:
                logger.warning(f"Kon margin mode of leverage niet instellen voor {symbol}: {ex}")

            order_params = {
                "marginMode": "isolated",
                "leverage": LEVERAGE
            }

            self.exchange.create_order(symbol=symbol, type="market", side=side, amount=amount, params=order_params)
            
            self.exchange.create_order(
                symbol=symbol, type="market", side=side_close, amount=amount,
                params={"stopLossPrice": float(stop), "reduceOnly": True, "marginMode": "isolated"}
            )
            
            self.exchange.create_order(
                symbol=symbol, type="limit", side=side_close, amount=amount, price=float(target),
                params={"takeProfitPrice": float(target), "reduceOnly": True, "marginMode": "isolated"}
            )

            self.state[symbol]["in_position"] = True
            self.state[symbol]["entry_price"] = entry
            send_telegram(f"📌 <b>VOLLE RISICO POSITIE GEPLAATST [Max 2 tegelijk]</b> {symbol}")
        except Exception as e:
            logger.error(f"Order error {symbol}: {e}")
            send_telegram(f"⚠️ <b>Order Fout op {symbol}</b>: {e}")

    def run(self):
        logger.info("Break & Retest Bot (Vol Risico Modus - Max 2 posities) gestart...")
        send_telegram("🤖 <b>Break & Retest Bot gestart! (Vol Risico Modus, max 2 posities actief)</b>")
        
        while True:
            self.check_open_positions_and_orders()

            current_date = datetime.now().date()
            if current_date != self.last_report_date:
                self.send_performance_report()
                self.last_report_date = current_date

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
