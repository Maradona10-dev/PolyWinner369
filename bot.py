import os
import time
import json
import re
from typing import Optional, Dict, Any, Tuple

import requests

# ============================================================
# FLEXIBELE CLOB CLIENT IMPORTS
# ============================================================

ClobClient = None
OrderArgs = None
OrderType = None

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
except Exception:
    try:
        from py_clob_client import ClobClient, OrderArgs, OrderType
    except Exception:
        try:
            from py_clob_client_v2 import ClobClient, OrderArgs, OrderType
        except Exception:
            pass

if ClobClient is None:
    raise SystemExit(
        "\n❌ py-clob-client ontbreekt of kon niet geïmporteerd worden.\n"
        "Zorg dat 'requirements.txt' in je GitHub repo staat met daarin:\n"
        "py-clob-client\nrequests\n"
    )

# ============================================================
# CONFIGURATIE & INSTELLINGEN
# ============================================================

CLOB_HOST = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
BINANCE_API_URL = "https://api.binance.com/api/v3"

CHAIN_ID = 137  # Polygon Mainnet

SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "3"))
MAX_TRADES_PER_CYCLE = int(os.environ.get("MAX_TRADES_PER_CYCLE", "2"))
MAX_ALLOWABLE_ODDS = float(os.environ.get("MAX_ALLOWABLE_ODDS", "0.85"))
LIVE_TRADE_AMOUNT = float(os.environ.get("LIVE_TRADE_AMOUNT", "5.0"))

VERBOSE_LOG_INTERVAL_SEC = 15

# ============================================================
# TELEGRAM CONFIGURATIE
# ============================================================

RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN[3:] if RAW_TOKEN.startswith("bot") else RAW_TOKEN
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ============================================================
# POLYMARKET WALLET & VALIDATIE
# ============================================================

RAW_PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
POLYMARKET_FUNDER_ADDRESS = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
SIGNATURE_TYPE_ENV = os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1").strip()

def clean_private_key(value: str) -> str:
    if not value:
        return ""
    if value.lower().startswith("0x"):
        value = value[2:]
    cleaned = "".join(re.findall(r"[0-9a-fA-F]", value))
    return cleaned

PK = clean_private_key(RAW_PRIVATE_KEY)

def get_signature_type() -> int:
    value = SIGNATURE_TYPE_ENV.lower()
    if value in ["1", "poly_proxy", "proxy"]:
        return 1
    if value in ["2", "poly_gnosis_safe", "gnosis", "safe"]:
        return 2
    if value in ["3", "poly_1271", "1271"]:
        return 3
    return 0

SIG_TYPE = get_signature_type()

# ============================================================
# MARKETS & PAIR MAPPINGS
# ============================================================

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ZECUSDT"]

POLY_SYMBOL_ALIASES = {
    "BTCUSDT": ["bitcoin", "btc"],
    "ETHUSDT": ["ethereum", "eth"],
    "SOLUSDT": ["solana", "sol"],
    "BNBUSDT": ["binance", "bnb"],
    "XRPUSDT": ["ripple", "xrp"],
    "DOGEUSDT": ["dogecoin", "doge"],
    "ZECUSDT": ["zcash", "zec"],
}

EXCLUDED_KEYWORDS = ["ipo", "election", "presidential", "tweet", "approval", "sec", "court", "fed", "rate", "lawsuit"]

active_signals_sent = {}
last_verbose_log_time = 0
session = requests.Session()
session.headers.update({"User-Agent": "PolyAutoTrader/5.6"})

# ============================================================
# HELPERS & HTTP WRAPPERS
# ============================================================

def safe_json_load(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        return []

def request_json(method: str, url: str, *, timeout: int = 5, **kwargs):
    try:
        response = session.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"⚠️ Netwerkfout ({url}): {e}")
    return None

# ============================================================
# CLOB CLIENT INITIALISATIE
# ============================================================

def init_clob_client():
    if not PK:
        raise SystemExit("❌ FOUT: Geen geldige POLYMARKET_PRIVATE_KEY gevonden.")

    try:
        host_url = "https://clob.polymarket.com"
        
        kwargs = {
            "host": host_url,
            "key": PK,
            "chain_id": CHAIN_ID,
            "signature_type": SIG_TYPE,
        }
        if POLYMARKET_FUNDER_ADDRESS:
            kwargs["funder"] = POLYMARKET_FUNDER_ADDRESS

        client = ClobClient(**kwargs)

        try:
            creds = client.derive_api_key()
        except Exception:
            creds = client.create_api_key()

        client.set_api_creds(creds)
        print(f"✅ CLOB Verbonden | Wallet Adres: {client.get_address()}")
        return client
    except Exception as e:
        raise SystemExit(f"❌ CLOB Initialisatie mislukt: {e}")

clob_client = init_clob_client()

# ============================================================
# TELEGRAM BERICHTEN
# ============================================================

def send_telegram_msg(message: str, button_url: Optional[str] = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "🚀 ZET DIRECT IN OP POLYMARKET", "url": button_url}]]
        }

    try:
        session.post(url, json=payload, timeout=5)
        return True
    except Exception as e:
        print(f"⚠️ Telegram Fout: {e}")
        return False

# ============================================================
# BINANCE DATA & RSI / 15M TREND BEREKENING
# ============================================================

def fetch_klines(symbol: str, interval: str, limit: int):
    return request_json("GET", f"{BINANCE_API_URL}/klines", params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=4)

def calculate_rsi(prices, period: int = 14):
    if len(prices) < period + 1:
        return 50.0
    gains = [max(0.0, prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    losses = [max(0.0, prices[i - 1] - prices[i]) for i in range(1, len(prices))]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return round(100 - (100 / (1 + (avg_gain / avg_loss))), 1)

def fetch_15m_trend(symbol: str) -> Tuple[str, float]:
    res_15m = fetch_klines(symbol, "15m", 15)
    if not res_15m or len(res_15m) < 15:
        return "NEUTRAL", 0.0

    closes = [float(k[4]) for k in res_15m]
    rsi_15m = calculate_rsi(closes)
    change = ((closes[-1] - closes[0]) / closes[0]) * 100

    if rsi_15m > 58 and change > 0.12:
        return "BULLISH", rsi_15m
    elif rsi_15m < 42 and change < -0.12:
        return "BEARISH", rsi_15m
    return "NEUTRAL", rsi_15m

def fetch_binance_data(symbol: str):
    try:
        res_5m = fetch_klines(symbol, "5m", 2)
        res_1m = fetch_klines(symbol, "1m", 30)
        if not res_5m or not res_1m:
            return None, None, None, None, None, None, None, None

        window_strike_price = float(res_5m[-1][1])
        close_prices = [float(k[4]) for k in res_1m]
        volumes = [float(k[5]) for k in res_1m]

        current_price = close_prices[-1]
        window_delta_pct = round(((current_price - window_strike_price) / window_strike_price) * 100, 3)
        rsi = calculate_rsi(close_prices)
        prev_rsi = calculate_rsi(close_prices[:-1])

        current_volume = volumes[-1]
        avg_volume = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else current_volume
        volume_ratio = round(current_volume / avg_volume, 2) if avg_volume > 0 else 1.0

        trend_15m, rsi_15m = fetch_15m_trend(symbol)

        return current_price, window_strike_price, window_delta_pct, rsi, prev_rsi, volume_ratio, trend_15m, rsi_15m
    except Exception as e:
        print(f"⚠️ fetch_binance_data fout voor {symbol}: {e}")
        return None, None, None, None, None, None, None, None

# ============================================================
# STRATEGIE LOGICA
# ============================================================

def analyze_market_and_get_signal(symbol, rsi, prev_rsi, window_delta_pct, volume_ratio, trend_15m, rsi_15m, time_remaining_sec):
    if symbol in ["ZECUSDT", "DOGEUSDT"]:
        min_delta, min_vol_ratio = 0.04, 1.05
    elif symbol in ["SOLUSDT", "BNBUSDT", "XRPUSDT"]:
        min_delta, min_vol_ratio = 0.02, 1.02
    else:
        min_delta, min_vol_ratio = 0.01, 1.00

    now_sec = time.time()
    time_remaining_15m = int(900 - (now_sec % 900))

    if 180 <= time_remaining_sec <= 285:
        if rsi >= 51 and window_delta_pct >= min_delta and volume_ratio >= min_vol_ratio:
            if trend_15m != "BEARISH":
                return "UP", "5M_MOMENTUM", f"🚀 5M Momentum UP (+{window_delta_pct}% t.o.v. Strike, Vol {volume_ratio}x)"

        if rsi <= 49 and window_delta_pct <= -min_delta and volume_ratio >= min_vol_ratio:
            if trend_15m != "BULLISH":
                return "DOWN", "5M_MOMENTUM", f"🚀 5M Momentum DOWN ({window_delta_pct}% t.o.v. Strike, Vol {volume_ratio}x)"

    if time_remaining_15m <= 360 and time_remaining_sec <= 270:
        if trend_15m == "BULLISH" and rsi >= 48 and window_delta_pct >= 0:
            return "UP", "15M_TREND", f"🌊 15M Trend Follow UP (15M RSI {rsi_15m:.1f}, 5M Delta +{window_delta_pct}%)"

        if trend_15m == "BEARISH" and rsi <= 52 and window_delta_pct <= 0:
            return "DOWN", "15M_TREND", f"🌊 15M Trend Follow DOWN (15M RSI {rsi_15m:.1f}, 5M Delta {window_delta_pct}%)"

    if 30 <= time_remaining_sec <= 240:
        if rsi > prev_rsi and window_delta_pct >= 0.005:
            if trend_15m != "BEARISH":
                return "UP", "5M_PULLBACK", f"🔄 5M Pullback Reentry UP (RSI {prev_rsi:.1f} ➔ {rsi:.1f}, Delta +{window_delta_pct}%)"

        if rsi < prev_rsi and window_delta_pct <= -0.005:
            if trend_15m != "BULLISH":
                return "DOWN", "5M_PULLBACK", f"🔄 5M Pullback Reentry DOWN (RSI {prev_rsi:.1f} ➔ {rsi:.1f}, Delta {window_delta_pct}%)"

    return None, None, None

# ============================================================
# GEVERIFIEERDE MARKTMATCHING
# ============================================================

def get_token_info(symbol: str, signal: str):
    aliases = POLY_SYMBOL_ALIASES.get(symbol, [])
    wanted = signal.lower()

    for alias in aliases:
        gamma_url = f"{POLYMARKET_GAMMA_URL}/events?limit=30&active=true&closed=false&q={alias}"
        gamma_data = request_json("GET", gamma_url, timeout=5)

        if not gamma_data or not isinstance(gamma_data, list):
            continue

        for event in gamma_data:
            slug = str(event.get("slug", "")).lower()
            title = str(event.get("title", "")).lower()

            if any(ex in slug or ex in title for ex in EXCLUDED_KEYWORDS):
                continue

            markets = event.get("markets", [])
            for market in markets:
                if market.get("closed", False) or not market.get("active", True):
                    continue

                outcomes = safe_json_load(market.get("outcomes"))
                token_ids = safe_json_load(market.get("clobTokenIds"))

                if not outcomes or not token_ids:
                    continue

                for idx, outcome in enumerate(outcomes):
                    norm = str(outcome).strip().lower()

                    is_up = (wanted == "up" and norm in {"up", "yes", "above"})
                    is_down = (wanted == "down" and norm in {"down", "no", "below"})

                    if (is_up or is_down) and idx < len(token_ids):
                        token_id = str(token_ids[idx])
                        target_url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
                        print(f"✅ Geldige Match voor {symbol} ({signal}): {slug} [Token: {token_id[:8]}...]")
                        return {
                            "token_id": token_id,
                            "outcome": str(outcome),
                            "event_slug": slug,
                            "url": target_url
                        }

    print(f"⚠️ Geen geschikte actieve markt gevonden voor {symbol} ({signal})")
    return None

# ============================================================
# ORDER EXECUTIE
# ============================================================

def execute_live_order(token_id: str, amount_usd: float, max_price: float):
    try:
        orderbook = clob_client.get_order_book(token_id)
        if not orderbook:
            return False, 0.0, "Geen orderbook ontvangen van Polymarket"

        asks = getattr(orderbook, "asks", []) or (orderbook.get("asks", []) if isinstance(orderbook, dict) else [])
        if not asks:
            return False, 0.0, "Geen verkooporders (asks) op dit moment"

        def extract_price(entry):
            if isinstance(entry, dict):
                return float(entry.get("price", 0))
            return float(getattr(entry, "price", 0))

        sorted_asks = sorted(asks, key=extract_price)
        best_ask = extract_price(sorted_asks[0])

        if best_ask <= 0:
            return False, 0.0, "Ongeldige ask prijs"

        if best_ask > max_price:
            return False, best_ask, f"Prijs te hoog (${best_ask:.3f} > max${max_price:.3f})"

        shares = round(amount_usd / best_ask, 2)

        order_args = OrderArgs(
            price=best_ask,
            size=shares,
            side="BUY",
            token_id=token_id,
        )

        signed_order = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed_order, OrderType.FOK)

        if resp and isinstance(resp, dict) and resp.get("success"):
            order_id = resp.get("orderID", "N/A")
            return True, best_ask, order_id
        else:
            error_msg = resp.get("errorMsg", str(resp)) if isinstance(resp, dict) else str(resp)
            return False, best_ask, error_msg

    except Exception as e:
        return False, 0.0, str(e)

# ============================================================
# MAIN BOT LOOP
# ============================================================

def main():
    global last_verbose_log_time

    print("\n" + "=" * 60)
    print("🤖 POLYMARKET LIVE TRADING BOT V5.6")
    print("=" * 60)
    print(f"Live order bedrag: ${LIVE_TRADE_AMOUNT:.2f} USD")
    print(f"Max Ask Limiet: ${MAX_ALLOWABLE_ODDS:.2f}")
    print(f"Signature Type: {SIG_TYPE}")
    print(f"Scan Interval: {SCAN_INTERVAL_SEC}s")
    print("=" * 60 + "\n")

    send_telegram_msg(
        "⚡ **Poly Auto-Trader LIVE Gestart (v5.6)**\n\n"
        "🚨 **Systeemstatus:** VOLAUTOMATISCH TRADEN ACTIEF\n"
        f"💵 **Inzet per trade:** `${LIVE_TRADE_AMOUNT:.2f}`\n"
        f"🛡️ **Max Ask Limiet:** `${MAX_ALLOWABLE_ODDS:.2f}`"
    )

    while True:
        try:
            now_sec = time.time()
            time_remaining_sec = int(300 - (now_sec % 300))
            cycle_id = int(now_sec // 300)

            should_log_status = (now_sec - last_verbose_log_time) >= VERBOSE_LOG_INTERVAL_SEC
            if should_log_status:
                last_verbose_log_time = now_sec

            current_cycle_count = sum(1 for k in active_signals_sent if k.startswith(f"cycle_{cycle_id}_"))

            if current_cycle_count < MAX_TRADES_PER_CYCLE:
                for symbol in PAIRS:
                    if current_cycle_count >= MAX_TRADES_PER_CYCLE:
                        break

                    current_price, strike, delta_pct, rsi, prev_rsi, vol_ratio, trend_15m, rsi_15m = fetch_binance_data(symbol)

                    if current_price is None:
                        continue

                    signal, strat_type, reason = analyze_market_and_get_signal(
                        symbol, rsi, prev_rsi, delta_pct, vol_ratio, trend_15m, rsi_15m, time_remaining_sec
                    )

                    if not signal:
                        if should_log_status:
                            print(f"🔍 {symbol}: geen signaal | RSI={rsi} | delta={delta_pct}% | vol={vol_ratio}
