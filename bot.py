import os
import time
import re
from typing import Optional, Dict, Any, Tuple
import requests

# ============================================================
# CLOB CLIENT IMPORTS
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
    raise SystemExit("\n❌ py-clob-client ontbreekt in requirements.txt.\n")

# ============================================================
# CONFIGURATIE & INSTELLINGEN
# ============================================================
CLOB_HOST = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
BINANCE_API_URL = "https://api.binance.com/api/v3"
CHAIN_ID = 137  # Polygon Mainnet

SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "3"))
MAX_ENTRY_WINDOW_SECONDS = int(os.environ.get("MAX_ENTRY_WINDOW_SECONDS", "80"))
MAX_ALLOWABLE_ODDS = float(os.environ.get("MAX_ALLOWABLE_ODDS", "0.93"))
LIVE_TRADE_AMOUNT = float(os.environ.get("LIVE_TRADE_AMOUNT", "5.0"))

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
    return "".join(re.findall(r"[0-9a-fA-F]", value))

PK = clean_private_key(RAW_PRIVATE_KEY)

def get_signature_type() -> int:
    try:
        return int(SIGNATURE_TYPE_ENV)
    except ValueError:
        return 1

SIG_TYPE = get_signature_type()

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

active_signals_sent = {}
session = requests.Session()
session.headers.update({"User-Agent": "PolyAutoTrader/6.0"})

def request_json(method: str, url: str, *, timeout: int = 5, **kwargs):
    try:
        response = session.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"⚠️ Netwerkfout ({url}): {e}")
    return None

def init_clob_client():
    if not PK:
        raise SystemExit("❌ FOUT: Geen geldige POLYMARKET_PRIVATE_KEY gevonden.")
    try:
        kwargs = {
            "host": CLOB_HOST,
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
        print(f"✅ CLOB Verbonden | Wallet: {client.get_address()} | SigType: {SIG_TYPE}")
        return client
    except Exception as e:
        raise SystemExit(f"❌ CLOB Initialisatie mislukt: {e}")

clob_client = init_clob_client()

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
# 1-MINUUT INDICATOR BEREKENINGEN (EMA, RSI, MACD)
# ============================================================
def calculate_ema(prices, period):
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def calculate_rsi(prices, period=14):
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

def calculate_macd(prices):
    ema12 = calculate_ema(prices, 12)
    ema26 = calculate_ema(prices, 26)
    macd_line = ema12 - ema26
    # Eenvoudige benadering van de signaallijn over de laatste kaarsen
    macd_signal = macd_line * 0.9 
    return macd_line, macd_signal

def get_1m_strategy_signal(symbol: str):
    """
    1-minuut strategie met 3 indicatoren:
    1. EMA 20
    2. RSI 14
    3. MACD
    """
    klines = request_json("GET", f"{BINANCE_API_URL}/klines", params={"symbol": symbol, "interval": "1m", "limit": 40})
    if not klines or len(klines) < 35:
        return None

    close_prices = [float(k[4]) for k in klines]
    current_price = close_prices[-1]

    ema_20 = calculate_ema(close_prices, 20)
    rsi = calculate_rsi(close_prices, 14)
    macd_line, macd_signal = calculate_macd(close_prices)

    ema_up = current_price > ema_20
    rsi_up = rsi > 52
    macd_up = macd_line > macd_signal

    ema_down = current_price < ema_20
    rsi_down = rsi < 48
    macd_down = macd_line < macd_signal

    if ema_up and rsi_up and macd_up:
        return "UP"
    elif ema_down and rsi_down and macd_down:
        return "DOWN"

    return None

# ============================================================
# MARKT MATCHING & ORDER EXECUTIE
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
            markets = event.get("markets", [])
            for market in markets:
                if market.get("closed", False) or not market.get("active", True):
                    continue

                outcomes = market.get("outcomes", [])
                token_ids = market.get("clobTokenIds", [])
                if isinstance(outcomes, str):
                    import json
                    try: outcomes = json.loads(outcomes)
                    except: pass
                if isinstance(token_ids, str):
                    import json
                    try: token_ids = json.loads(token_ids)
                    except: pass

                if not outcomes or not token_ids:
                    continue

                for idx, outcome in enumerate(outcomes):
                    norm = str(outcome).strip().lower()
                    is_up = (wanted == "up" and norm in {"up", "yes", "above"})
                    is_down = (wanted == "down" and norm in {"down", "no", "below"})

                    if (is_up or is_down) and idx < len(token_ids):
                        return {
                            "token_id": str(token_ids[idx]),
                            "url": f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
                        }
    return None

def execute_live_order(token_id: str, amount_usd: float, max_price: float):
    try:
        orderbook = clob_client.get_order_book(token_id)
        asks = getattr(orderbook, "asks", []) or (orderbook.get("asks", []) if isinstance(orderbook, dict) else [])
        if not asks:
            return False, 0.0, "Geen verkopers in het orderboek"

        def extract_price(entry):
            if isinstance(entry, dict): return float(entry.get("price", 0))
            return float(getattr(entry, "price", 0))

        sorted_asks = sorted(asks, key=extract_price)
        best_ask = extract_price(sorted_asks[0])

        if best_ask > max_price:
            return False, best_ask, f"Prijs te hoog (${best_ask:.3f} > max ${max_price:.3f})"

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
            return True, best_ask, resp.get("orderID", "N/A")
        else:
            return False, best_ask, str(resp)

    except Exception as e:
        return False, 0.0, str(e)

# ============================================================
# HOOFDLOOP
# ============================================================
def main():
    print("\n" + "=" * 60)
    print("🤖 POLYMARKET 1M STRATEGIE BOT V6.0")
    print("=" * 60)

    while True:
        try:
            now_sec = time.time()
            time_elapsed_in_window = int(now_sec % 300) # Verstreken seconden in de 5m interval
            cycle_id = int(now_sec // 300)

            # --- REGEL 1: STRICHTE ENTRY BINNEN DE EERSTE 80 SECONDEN ---
            if time_elapsed_in_window > MAX_ENTRY_WINDOW_SECONDS:
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            for symbol in PAIRS:
                trade_key = f"cycle_{cycle_id}_{symbol}"
                if trade_key in active_signals_sent:
                    continue

                # --- REGEL 2: 1-MINUUT MULTI-INDICATOR CONVERGENTIE ---
                signal = get_1m_strategy_signal(symbol)
                if not signal:
                    continue

                token_info = get_token_info(symbol, signal)
                if not token_info:
                    continue

                print(f"🚀 [{symbol}] Sterk {signal}-signaal in eerste {time_elapsed_in_window}s. Order uitvoeren...")

                success, filled_price, info = execute_live_order(
                    token_info["token_id"],
                    LIVE_TRADE_AMOUNT,
                    MAX_ALLOWABLE_ODDS
                )

                active_signals_sent[trade_key] = True

                if success:
                    send_telegram_msg(f"✅ **LIVE ORDER GEPLAATST:** {symbol} ({signal})\nPrijs: `${filled_price:.3f}`\nOrder ID: `{info}`", token_info["url"])
                else:
                    send_telegram_msg(f"🚨 **ORDER GEWEIGERD:** {symbol} ({signal})\nReden: `{info}`", token_info["url"])

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Fout in loop: {e}")

        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
