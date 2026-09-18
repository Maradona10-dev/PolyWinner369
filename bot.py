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
    return "BloFin Multi-Factor Bot v2.4 is running!", 200


API_KEY = os.environ.get("EXCHANGE_API_KEY", "")
API_SECRET = os.environ.get("EXCHANGE_SECRET", "")
API_PASSWORD = os.environ.get("EXCHANGE_PASSWORD", "")

EXCHANGE_ID = "blofin"

PAPER_TRADING = (
    os.environ.get("PAPER_TRADING", "False").lower() == "true"
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ====================== SYMBOLS ======================

SYMBOLS = [
    # Majors
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
    # L2 / L1
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
    # DeFi
    "UNI/USDT:USDT",
    "AAVE/USDT:USDT",
    "LDO/USDT:USDT",
    "ONDO/USDT:USDT",
    "ENA/USDT:USDT",
    "CRV/USDT:USDT",
    "MKR/USDT:USDT",
    "RUNE/USDT:USDT",
    # AI / narrative
    "RENDER/USDT:USDT",
    "FET/USDT:USDT",
    "TAO/USDT:USDT",
    "WLD/USDT:USDT",
    "JUP/USDT:USDT",
    # Memes / high vol
    "PEPE/USDT:USDT",
    "WIF/USDT:USDT",
    "BONK/USDT:USDT",
    "FLOKI/USDT:USDT",
    "SHIB/USDT:USDT",
    "ORDI/USDT:USDT",
    "GALA/USDT:USDT",
    # Extra
    "ETC/USDT:USDT",
    "XLM/USDT:USDT",
    "ALGO/USDT:USDT",
    "SAND/USDT:USDT",
    "APE/USDT:USDT",
]


# ====================== TIMEFRAMES ======================

SCAN_TF = "15m"
CONFIRM_TF = "5m"
TREND_TF = "1h"


# ====================== INDICATORS ======================

LOOKBACK = 12

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200

ATR_LEN = 14
VOLUME_LEN = 20

# 10% extra volume is genoeg; 20% blokkeerde te veel 15m-bars.
VOLUME_MULT = 1.10

# Near-sweep: low/high mag tot 0.15 ATR voorbij lookback liggen.
NEAR_SWEEP_ATR = 0.15


# ====================== ENTRY ======================

# Score >= 39 + 5m break  OF  score >= 55 zonder break.
MIN_SCORE = 39
STRONG_SCORE = 55
MAX_POSSIBLE_SCORE = 85


# ====================== POSITION SIZE ======================

FIXED_MARGIN_USDT = 5.0
FIXED_LEVERAGE = 10

MAX_ACTIVE_POSITIONS = 2


# ====================== SL / TP ======================

STOP_ATR_MULT = 1.5
RISK_REWARD = 2.0


# ====================== BOT BEHAVIOUR ======================

USE_CLOSED_CANDLES = True

SCAN_SLEEP_SECONDS = 60
SYMBOL_SLEEP_SECONDS = 0.35
POSITION_SYNC_SECONDS = 60
WATCH_COOLDOWN_SECONDS = 15 * 60
# Twee score-updates per uur, ook bij lage scores (10/20/25).
SCORE_REPORT_SECONDS = 30 * 60

PROTECTION_RETRIES = 3
PROTECTION_RETRY_SLEEP = 1.2


# ====================== LOGGING ======================

TRADE_LOG_FILE = "trade_history.json"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()],
)

logger = logging.getLogger(__name__)


# ====================== TELEGRAM ======================

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }

        response = requests.post(
            url,
            json=payload,
            timeout=5,
        )

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
                "EXCHANGE_API_KEY, EXCHANGE_SECRET and "
                "EXCHANGE_PASSWORD must be configured."
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

        available = [
            symbol
            for symbol in SYMBOLS
            if symbol in self.exchange.markets
        ]
        missing = [
            symbol
            for symbol in SYMBOLS
            if symbol not in self.exchange.markets
        ]

        if not available:
            raise RuntimeError(
                "Geen enkel symbool beschikbaar op BloFin."
            )

        if missing:
            logger.warning(
                "Niet beschikbaar op BloFin, overgeslagen: %s",
                ", ".join(missing),
            )

        SYMBOLS[:] = available

        logger.info(
            "Markets geladen. %d symbolen beschikbaar.",
            len(SYMBOLS),
        )

        self.state: Dict[str, dict] = {
            symbol: {
                "in_position": False,
                "entry_price": 0.0,
                "side": None,
                "contracts": 0.0,
                "last_sync": None,
            }
            for symbol in SYMBOLS
        }

        self.last_position_sync = 0.0
        self.last_watch: Dict[str, float] = {}
        self.last_score_report = 0.0
        self.lock = threading.Lock()


    # ====================== MARKET DATA ======================

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

            for column in [
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]:
                df[column] = pd.to_numeric(
                    df[column],
                    errors="coerce",
                )

            df["atr"] = ta.atr(
                df["high"],
                df["low"],
                df["close"],
                length=ATR_LEN,
            )

            df["ema20"] = ta.ema(
                df["close"],
                length=EMA_FAST,
            )

            df["ema50"] = ta.ema(
                df["close"],
                length=EMA_MID,
            )

            # EMA200 alleen als er genoeg candles zijn.
            # Anders gooit dropna() de hele 15m/5m-tabel weg.
            if len(df) >= EMA_SLOW + 5:
                df["ema200"] = ta.ema(
                    df["close"],
                    length=EMA_SLOW,
                )
            else:
                df["ema200"] = float("nan")

            df["vol_sma"] = ta.sma(
                df["volume"],
                length=VOLUME_LEN,
            )

            # Geen dropna op indicatoren: anders verdwijnen korte
            # 5m/15m-series en faalt de hele scan.
            df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

            if len(df) < 5:
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


    def last_closed(
        self,
        df: pd.DataFrame,
    ) -> pd.Series:

        if USE_CLOSED_CANDLES and len(df) >= 2:
            return df.iloc[-2]

        return df.iloc[-1]


    # ====================== POSITIONS ======================

    def fetch_open_positions(
        self,
    ) -> Optional[Dict[str, dict]]:

        try:

            positions = self.exchange.fetch_positions(
                SYMBOLS
            )

        except Exception as exc:

            logger.error(
                "Position sync failed: %s",
                exc,
            )

            return None

        result: Dict[str, dict] = {}

        for position in positions:

            symbol = position.get("symbol")

            if symbol not in SYMBOLS:
                continue

            contracts_raw = position.get("contracts")

            try:
                contracts = abs(
                    float(contracts_raw or 0)
                )
            except (TypeError, ValueError):
                contracts = 0.0

            if contracts <= 0:
                continue

            side = position.get("side")

            try:
                entry_price = float(
                    position.get("entryPrice") or 0
                )
            except (TypeError, ValueError):
                entry_price = 0.0

            result[symbol] = {
                "in_position": True,
                "entry_price": entry_price,
                "side": side,
                "contracts": contracts,
                "last_sync": utc_now(),
                "raw": position,
            }

        return result


    def sync_positions(self) -> bool:

        open_positions = self.fetch_open_positions()

        if open_positions is None:
            return False

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

                    was_in_position = self.state[symbol]["in_position"]

                    self.state[symbol].update(
                        {
                            "in_position": False,
                            "entry_price": 0.0,
                            "side": None,
                            "contracts": 0.0,
                            "last_sync": utc_now(),
                        }
                    )

                    if was_in_position:

                        logger.info(
                            "Position closed detected: %s",
                            symbol,
                        )

                        send_telegram(
                            f"📕 <b>POSITION CLOSED</b>\n"
                            f"{symbol}"
                        )

        self.last_position_sync = time.time()

        return True


    def active_position_count(self) -> int:

        with self.lock:

            return sum(
                1
                for state in self.state.values()
                if state["in_position"]
            )


    def emergency_close(self, symbol: str, amount: float, close_side: str) -> None:
        """Sluit een onbeschermde positie market reduce-only."""

        try:
            self.exchange.create_order(
                symbol=symbol,
                type="market",
                side=close_side,
                amount=amount,
                params={
                    "marginMode": "isolated",
                    "reduceOnly": True,
                },
            )
            logger.warning("Emergency close submitted for %s", symbol)
            send_telegram(
                f"🚨 <b>EMERGENCY CLOSE</b>\n"
                f"{symbol}\n"
                f"SL/TP kon niet geverifieerd worden."
            )
        except Exception as exc:
            logger.error("Emergency close failed for %s: %s", symbol, exc)
            send_telegram(
                f"🚨 <b>UNPROTECTED POSITION</b>\n"
                f"{symbol}\n"
                f"Kon niet automatisch sluiten: {str(exc)[:300]}"
            )


    # ====================== STRATEGY ======================

    def analyze_coin(
        self,
        symbol: str,
    ) -> Optional[dict]:

        df_15m = self.fetch_ohlcv(symbol, SCAN_TF, limit=120)
        df_1h = self.fetch_ohlcv(symbol, TREND_TF, limit=250)
        df_5m = self.fetch_ohlcv(symbol, CONFIRM_TF, limit=80)
        df_btc = self.fetch_ohlcv("BTC/USDT:USDT", SCAN_TF, limit=80)

        if df_15m is None or df_1h is None or df_5m is None:
            return None

        if len(df_15m) < LOOKBACK + 5 or len(df_5m) < 5:
            return None

        h_curr = self.last_closed(df_1h)
        c_curr = self.last_closed(df_15m)
        m5_curr = self.last_closed(df_5m)

        if USE_CLOSED_CANDLES:
            m5_prev = df_5m.iloc[-3] if len(df_5m) >= 3 else df_5m.iloc[-2]
        else:
            m5_prev = df_5m.iloc[-2]

        btc_curr = (
            self.last_closed(df_btc)
            if df_btc is not None and len(df_btc) >= 5
            else None
        )

        h_stack_bull = (
            h_curr["ema20"] > h_curr["ema50"]
            and h_curr["ema50"] > h_curr["ema200"]
        )
        h_stack_bear = (
            h_curr["ema20"] < h_curr["ema50"]
            and h_curr["ema50"] < h_curr["ema200"]
        )
        h_fast_bull = h_curr["ema20"] > h_curr["ema50"]
        h_fast_bear = h_curr["ema20"] < h_curr["ema50"]
        h_slow_bull = h_curr["ema50"] > h_curr["ema200"]
        h_slow_bear = h_curr["ema50"] < h_curr["ema200"]

        current_index = (
            len(df_15m) - 2
            if USE_CLOSED_CANDLES
            else len(df_15m) - 1
        )

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

        atr_val = float(c_curr["atr"] or 0.0)

        long_near_sweep = (
            not long_sweep
            and atr_val > 0
            and c_curr["low"] <= prev_low + (atr_val * NEAR_SWEEP_ATR)
            and c_curr["close"] > prev_low
        )
        short_near_sweep = (
            not short_sweep
            and atr_val > 0
            and c_curr["high"] >= prev_high - (atr_val * NEAR_SWEEP_ATR)
            and c_curr["close"] < prev_high
        )

        try:
            vol_sma = float(c_curr["vol_sma"] or 0.0)
        except (TypeError, ValueError):
            vol_sma = 0.0

        volume_ok = bool(vol_sma > 0 and c_curr["volume"] > vol_sma * VOLUME_MULT)

        long_break = m5_curr["close"] > m5_prev["high"]
        short_break = m5_curr["close"] < m5_prev["low"]

        relative = 0.0
        if btc_curr is not None and df_btc is not None and len(df_btc) >= 5 and len(df_15m) >= 5:
            btc_base = float(df_btc.iloc[-5]["close"] or 0.0)
            coin_base = float(df_15m.iloc[-5]["close"] or 0.0)
            if btc_base > 0 and coin_base > 0:
                btc_return = (float(btc_curr["close"]) / btc_base) - 1
                coin_return = (float(c_curr["close"]) / coin_base) - 1
                relative = coin_return - btc_return

        try:
            atr_now = float(c_curr["atr"] or 0.0)
        except (TypeError, ValueError):
            atr_now = 0.0
        atr_ratio = atr_now / float(c_curr["close"]) if c_curr["close"] else 0.0

        # =================================================
        # LONG SCORE  (max 85)
        # =================================================

        long_score = 0

        if h_stack_bull:
            long_score += 15
        elif h_fast_bull:
            long_score += 10
        elif h_slow_bull:
            long_score += 6

        if c_curr["ema20"] > c_curr["ema50"]:
            long_score += 10

        if long_sweep:
            long_score += 20
        elif long_near_sweep:
            long_score += 8

        if c_curr["close"] > c_curr["ema20"]:
            long_score += 5

        if volume_ok:
            long_score += 10

        if relative > 0.002:
            long_score += 10
        elif relative > 0.0005:
            long_score += 5

        if long_break:
            long_score += 10

        if atr_ratio > 0.003:
            long_score += 5

        # =================================================
        # SHORT SCORE  (max 85)
        # =================================================

        short_score = 0

        if h_stack_bear:
            short_score += 15
        elif h_fast_bear:
            short_score += 10
        elif h_slow_bear:
            short_score += 6

        if c_curr["ema20"] < c_curr["ema50"]:
            short_score += 10

        if short_sweep:
            short_score += 20
        elif short_near_sweep:
            short_score += 8

        if c_curr["close"] < c_curr["ema20"]:
            short_score += 5

        if volume_ok:
            short_score += 10

        if relative < -0.002:
            short_score += 10
        elif relative < -0.0005:
            short_score += 5

        if short_break:
            short_score += 10

        if atr_ratio > 0.003:
            short_score += 5

        logger.info(
            "%s | L=%s S=%s | sweepL=%s nearL=%s | sweepS=%s nearS=%s | "
            "5mL=%s 5mS=%s | vol=%s | rel=%.4f | atr%%=%.3f",
            symbol,
            long_score,
            short_score,
            long_sweep,
            long_near_sweep,
            short_sweep,
            short_near_sweep,
            long_break,
            short_break,
            volume_ok,
            relative,
            atr_ratio * 100,
        )

        def pack(direction: str, score: int, confirmed: bool) -> dict:
            return {
                "symbol": symbol,
                "direction": direction,
                "score": score,
                "price": float(c_curr["close"]),
                "atr": float(c_curr["atr"]),
                "timestamp": c_curr["timestamp"].isoformat(),
                "confirmation": confirmed,
            }

        long_entry = (
            long_score >= MIN_SCORE
            and long_score > short_score
            and (long_break or long_score >= STRONG_SCORE)
        )
        short_entry = (
            short_score >= MIN_SCORE
            and short_score > long_score
            and (short_break or short_score >= STRONG_SCORE)
        )

        setup = None

        if long_entry:
            setup = pack("long", long_score, True)
        elif short_entry:
            setup = pack("short", short_score, True)
        elif long_score >= MIN_SCORE and long_score > short_score:
            setup = pack("long", long_score, False)
        elif short_score >= MIN_SCORE and short_score > long_score:
            setup = pack("short", short_score, False)

        return {
            "symbol": symbol,
            "long_score": long_score,
            "short_score": short_score,
            "setup": setup,
        }


    # ====================== ORDER SIZING ======================

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
            FIXED_MARGIN_USDT
            * FIXED_LEVERAGE
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


    # ====================== WATCH MESSAGE ======================

    def send_watch(
        self,
        setup: dict,
    ) -> None:

        symbol = setup["symbol"]
        now = time.time()
        last = self.last_watch.get(symbol, 0)

        if now - last < WATCH_COOLDOWN_SECONDS:
            return

        self.last_watch[symbol] = now

        send_telegram(
            f"👀 <b>WATCH</b>\n"
            f"{setup['symbol']}\n"
            f"Direction: <b>{setup['direction'].upper()}</b>\n"
            f"Score: <b>{setup['score']}/{MAX_POSSIBLE_SCORE}</b>\n"
            f"Waiting for 5m confirmation."
        )


    # ====================== EXECUTION ======================

    def execute_trade(
        self,
        setup: dict,
    ) -> None:

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
            stop = reference_price - atr * STOP_ATR_MULT
            target = reference_price + ((reference_price - stop) * RISK_REWARD)
            side = "buy"
        else:
            stop = reference_price + atr * STOP_ATR_MULT
            target = reference_price - ((stop - reference_price) * RISK_REWARD)
            side = "sell"

        stop = self.price_to_precision(symbol, stop)
        target = self.price_to_precision(symbol, target)

        try:
            amount = self.calculate_contracts(symbol, reference_price)
        except Exception as exc:
            logger.error("Sizing error on %s: %s", symbol, exc)
            return

        send_telegram(
            f"🎯 <b>ENTRY TRIGGERED</b>\n"
            f"{symbol}\n"
            f"Direction: <b>{direction.upper()}</b>\n"
            f"Score: <b>{setup['score']}/{MAX_POSSIBLE_SCORE}</b>\n"
            f"Reference: <code>{reference_price:.8f}</code>\n"
            f"Initial SL: <code>{stop:.8f}</code>\n"
            f"Initial TP: <code>{target:.8f}</code>\n"
            f"Contracts: <code>{amount}</code>\n"
            f"Margin: {FIXED_MARGIN_USDT} USDT\n"
            f"Leverage: {FIXED_LEVERAGE}x"
        )

        # =================================================
        # PAPER TRADING
        # =================================================

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

            send_telegram(
                f"📝 <b>PAPER ENTRY</b>\n"
                f"{symbol} {direction.upper()}\n"
                f"Entry: <code>{reference_price:.8f}</code>\n"
                f"SL: <code>{stop:.8f}</code>\n"
                f"TP: <code>{target:.8f}</code>"
            )

            return

        # =================================================
        # LIVE TRADING
        # =================================================

        try:

            try:
                self.exchange.set_margin_mode("isolated", symbol)
            except Exception as exc:
                raise RuntimeError(
                    f"Could not set isolated margin for {symbol}: {exc}"
                )

            try:
                self.exchange.set_leverage(FIXED_LEVERAGE, symbol)
            except Exception as exc:
                raise RuntimeError(
                    f"Could not set leverage for {symbol}: {exc}"
                )

            entry_order = self.exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=amount,
                params={
                    "marginMode": "isolated",
                    "reduceOnly": False,
                },
            )

            logger.info(
                "Entry order submitted: %s",
                entry_order.get("id"),
            )

            live_position = None

            for attempt in range(5):
                time.sleep(1)
                positions = self.fetch_open_positions()
                if positions is None:
                    continue
                live_position = positions.get(symbol)
                if live_position:
                    break

            if not live_position:
                raise RuntimeError(
                    "Entry order returned, but no open position was found."
                )

            actual_entry = float(live_position["entry_price"])
            actual_contracts = float(live_position["contracts"])

            if direction == "long":
                actual_stop = actual_entry - atr * STOP_ATR_MULT
                actual_target = actual_entry + (
                    (actual_entry - actual_stop) * RISK_REWARD
                )
            else:
                actual_stop = actual_entry + atr * STOP_ATR_MULT
                actual_target = actual_entry - (
                    (actual_stop - actual_entry) * RISK_REWARD
                )

            actual_stop = self.price_to_precision(symbol, actual_stop)
            actual_target = self.price_to_precision(symbol, actual_target)

            close_side = "sell" if direction == "long" else "buy"

            protection_ok = False

            for attempt in range(1, PROTECTION_RETRIES + 1):
                protection_ok = self.place_and_verify_protection(
                    symbol=symbol,
                    direction=direction,
                    amount=actual_contracts,
                    stop=actual_stop,
                    target=actual_target,
                    close_side=close_side,
                )
                if protection_ok:
                    break
                logger.warning(
                    "Protection retry %s/%s for %s",
                    attempt,
                    PROTECTION_RETRIES,
                    symbol,
                )
                time.sleep(PROTECTION_RETRY_SLEEP)

            if not protection_ok:
                logger.error(
                    "Protection could not be verified for %s.",
                    symbol,
                )
                send_telegram(
                    f"🚨 <b>PROTECTION ERROR</b>\n"
                    f"{symbol}\n"
                    f"SL/TP could not be verified.\n"
                    f"Closing position to avoid naked risk."
                )
                self.emergency_close(symbol, actual_contracts, close_side)
                raise RuntimeError(
                    "SL/TP protection could not be verified."
                )

            with self.lock:
                self.state[symbol].update(
                    {
                        "in_position": True,
                        "entry_price": actual_entry,
                        "side": live_position["side"],
                        "contracts": actual_contracts,
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
                    "entry_actual": actual_entry,
                    "stop": actual_stop,
                    "target": actual_target,
                    "contracts": actual_contracts,
                    "order_id": entry_order.get("id"),
                }
            )

            send_telegram(
                f"✅ <b>ENTRY VERIFIED</b>\n"
                f"{symbol} {direction.upper()}\n"
                f"Score: <b>{setup['score']}/{MAX_POSSIBLE_SCORE}</b>\n"
                f"Entry: <code>{actual_entry:.8f}</code>\n"
                f"SL: <code>{actual_stop:.8f}</code>\n"
                f"TP: <code>{actual_target:.8f}</code>\n"
                f"Contracts: <code>{actual_contracts}</code>"
            )

        except Exception as exc:

            logger.exception("Execution error on %s: %s", symbol, exc)

            sync_ok = self.sync_positions()

            if not sync_ok:
                logger.error(
                    "Could not verify position state after execution error on %s.",
                    symbol,
                )

            send_telegram(
                f"❌ <b>TRADE ERROR</b>\n"
                f"{symbol}\n"
                f"{str(exc)[:500]}"
            )


    # ==========================================================
    # PROTECTION
    # ==========================================================

    def place_and_verify_protection(
        self,
        symbol: str,
        direction: str,
        amount: float,
        stop: float,
        target: float,
        close_side: str,
    ) -> bool:

        try:

            sl_supported = False
            tp_supported = False

            try:
                sl_supported = bool(
                    self.exchange.feature_value(
                        symbol,
                        "createOrder",
                        "stopLoss",
                    )
                )
                tp_supported = bool(
                    self.exchange.feature_value(
                        symbol,
                        "createOrder",
                        "takeProfit",
                    )
                )
            except Exception:
                sl_supported = False
                tp_supported = False

            logger.info(
                "Protection support %s | SL=%s TP=%s amount=%s",
                symbol,
                sl_supported,
                tp_supported,
                amount,
            )

            # Altijd echte contract size gebruiken. Nooit amount=0.

            try:
                sl_order = self.exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=close_side,
                    amount=amount,
                    params={
                        "reduceOnly": True,
                        "stopLoss": {
                            "triggerPrice": stop,
                        },
                    },
                )
                logger.info(
                    "SL protection submitted for %s: %s",
                    symbol,
                    sl_order.get("id"),
                )
            except Exception as sl_exc:
                logger.warning(
                    "Attached SL failed on %s (%s). Trying stop order.",
                    symbol,
                    sl_exc,
                )
                sl_order = self.exchange.create_order(
                    symbol=symbol,
                    type="stop",
                    side=close_side,
                    amount=amount,
                    price=None,
                    params={
                        "triggerPrice": stop,
                        "reduceOnly": True,
                    },
                )
                logger.info(
                    "Separate SL submitted for %s: %s",
                    symbol,
                    sl_order.get("id"),
                )

            try:
                tp_order = self.exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=close_side,
                    amount=amount,
                    params={
                        "reduceOnly": True,
                        "takeProfit": {
                            "triggerPrice": target,
                        },
                    },
                )
                logger.info(
                    "TP protection submitted for %s: %s",
                    symbol,
                    tp_order.get("id"),
                )
            except Exception as tp_exc:
                logger.warning(
                    "Attached TP failed on %s (%s). Trying limit TP.",
                    symbol,
                    tp_exc,
                )
                tp_order = self.exchange.create_order(
                    symbol=symbol,
                    type="limit",
                    side=close_side,
                    amount=amount,
                    price=target,
                    params={
                        "reduceOnly": True,
                    },
                )
                logger.info(
                    "Separate TP submitted for %s: %s",
                    symbol,
                    tp_order.get("id"),
                )

            time.sleep(1.2)
            return self.verify_protection_orders(symbol)

        except Exception as exc:

            logger.error("Protection error on %s: %s", symbol, exc)
            return False


    def verify_protection_orders(
        self,
        symbol: str,
    ) -> bool:

        try:
            open_orders = self.exchange.fetch_open_orders(symbol)
        except Exception as exc:
            logger.error(
                "Could not fetch protection orders for %s: %s",
                symbol,
                exc,
            )
            return False

        has_sl = False
        has_tp = False

        for order in open_orders:

            order_type = str(order.get("type") or "").lower()
            info = order.get("info") or {}
            info_text = json.dumps(info).lower()
            reduce_only = bool(
                order.get("reduceOnly")
                or info.get("reduceOnly")
                or info.get("reduce_only")
            )

            if (
                order_type in [
                    "stop",
                    "stop_market",
                    "stop_loss",
                    "loss",
                    "trigger",
                ]
                or "stoploss" in info_text
                or '"sl"' in info_text
                or "stop_loss" in info_text
            ):
                has_sl = True

            if (
                order_type in [
                    "take_profit",
                    "take_profit_market",
                ]
                or "takeprofit" in info_text
                or '"tp"' in info_text
                or "take_profit" in info_text
                or (order_type == "limit" and reduce_only)
            ):
                has_tp = True

        logger.info(
            "Protection verification %s: SL=%s TP=%s orders=%s",
            symbol,
            has_sl,
            has_tp,
            len(open_orders),
        )

        return has_sl and has_tp


    # ====================== LOGGING ======================

    def log_trade(
        self,
        trade: dict,
    ) -> None:

        try:

            history = []

            if os.path.exists(TRADE_LOG_FILE):
                with open(TRADE_LOG_FILE, "r", encoding="utf-8") as file:
                    history = json.load(file)

            if not isinstance(history, list):
                history = []

            history.append(trade)
            history = history[-1000:]

            with open(TRADE_LOG_FILE, "w", encoding="utf-8") as file:
                json.dump(
                    history,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )

        except Exception as exc:
            logger.error("Could not write trade log: %s", exc)


    def publish_score_summary(self, cycle_scores: list) -> None:
        """Log top scores every cycle; Telegram every few minutes."""

        now = time.time()
        due = now - self.last_score_report >= SCORE_REPORT_SECONDS

        if not cycle_scores:
            logger.info("Score summary: geen geldige scans deze ronde.")
            if due:
                self.last_score_report = now
                send_telegram(
                    "📊 <b>Score scan</b>\n"
                    "Geen geldige candles deze ronde.\n"
                    "Check Railway op Fetch error."
                )
            return

        top_long = sorted(
            cycle_scores,
            key=lambda x: x["long_score"],
            reverse=True,
        )[:5]
        top_short = sorted(
            cycle_scores,
            key=lambda x: x["short_score"],
            reverse=True,
        )[:5]

        def short_name(symbol: str) -> str:
            return symbol.split("/")[0]

        long_txt = ", ".join(
            f"{short_name(x['symbol'])} {x['long_score']}"
            for x in top_long
        )
        short_txt = ", ".join(
            f"{short_name(x['symbol'])} {x['short_score']}"
            for x in top_short
        )

        logger.info(
            "TOP L: %s | TOP S: %s | drempel=%s",
            long_txt,
            short_txt,
            MIN_SCORE,
        )

        if not due:
            return

        self.last_score_report = now

        best_long = top_long[0]
        best_short = top_short[0]

        send_telegram(
            "📊 Score scan\n"
            f"Drempel: {MIN_SCORE}/{MAX_POSSIBLE_SCORE}\n"
            f"Scans: {len(cycle_scores)}\n"
            f"Beste long: {short_name(best_long['symbol'])} "
            f"{best_long['long_score']}\n"
            f"Beste short: {short_name(best_short['symbol'])} "
            f"{best_short['short_score']}\n"
            f"Top 5 long: {long_txt}\n"
            f"Top 5 short: {short_txt}"
        )

    # ====================== MAIN LOOP ======================

    def run(self) -> None:

        send_telegram(
            "🚀 <b>Multi-Factor Bot v2.4.2 gestart</b>\n"
            f"{len(SYMBOLS)} symbolen\n"
            f"Min score: {MIN_SCORE}/{MAX_POSSIBLE_SCORE}\n"
            f"Strong score (geen 5m nodig): {STRONG_SCORE}\n"
            f"Max posities: {MAX_ACTIVE_POSITIONS}\n"
            f"Paper trading: {PAPER_TRADING}\n"
            f"Scan: iedere ~{SCAN_SLEEP_SECONDS}s"
        )

        initial_sync = self.sync_positions()

        if not initial_sync:
            send_telegram(
                "🚨 <b>STARTUP ERROR</b>\n"
                "Initial position sync failed.\n"
                "Bot will not trade until the exchange "
                "position state can be read."
            )

        while True:

            cycle_start = time.time()

            try:

                sync_ok = self.sync_positions()

                if not sync_ok:
                    logger.error(
                        "Position sync failed. Skipping this entire scan cycle."
                    )
                    time.sleep(SCAN_SLEEP_SECONDS)
                    continue

                active_count = self.active_position_count()

                if active_count >= MAX_ACTIVE_POSITIONS:
                    logger.info(
                        "Maximum open positions reached: %d",
                        active_count,
                    )

                else:

                    cycle_scores = []

                    for symbol in SYMBOLS:

                        try:

                            if self.active_position_count() >= MAX_ACTIVE_POSITIONS:
                                logger.info(
                                    "Max positions reached. Stopping symbol scan."
                                )
                                break

                            with self.lock:
                                in_pos = self.state[symbol]["in_position"]

                            report = self.analyze_coin(symbol)

                            if report is None:
                                time.sleep(SYMBOL_SLEEP_SECONDS)
                                continue

                            cycle_scores.append(report)

                            if in_pos:
                                time.sleep(SYMBOL_SLEEP_SECONDS)
                                continue

                            setup = report.get("setup")

                            if setup is None:
                                time.sleep(SYMBOL_SLEEP_SECONDS)
                                continue

                            logger.info(
                                "Signal: %s %s score=%s/%s confirmation=%s",
                                symbol,
                                setup["direction"],
                                setup["score"],
                                MAX_POSSIBLE_SCORE,
                                setup["confirmation"],
                            )

                            if setup["score"] >= MIN_SCORE:

                                if setup["confirmation"]:
                                    logger.info(
                                        "ENTRY READY: %s %s score=%s",
                                        symbol,
                                        setup["direction"],
                                        setup["score"],
                                    )
                                    self.execute_trade(setup)
                                else:
                                    logger.info(
                                        "WATCH: %s %s score=%s - waiting for 5m",
                                        symbol,
                                        setup["direction"],
                                        setup["score"],
                                    )
                                    self.send_watch(setup)

                        except Exception as exc:
                            logger.exception(
                                "Loop error for %s: %s",
                                symbol,
                                exc,
                            )

                        time.sleep(SYMBOL_SLEEP_SECONDS)

                    self.publish_score_summary(cycle_scores)

            except Exception as exc:
                logger.exception("Main loop error: %s", exc)

            elapsed = time.time() - cycle_start
            sleep_for = max(1, SCAN_SLEEP_SECONDS - elapsed)

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


# ====================== START ======================

if __name__ == "__main__":

    web_thread = threading.Thread(target=run_web)
    web_thread.daemon = True
    web_thread.start()

    bot = MultiFactorBot()
    bot.run()
