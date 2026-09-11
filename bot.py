import os
import time
import json
import re
from typing import Optional, Dict, Any, Tuple

import requests

try:
    from py_clob_client_v2 import (
        ClobClient,
        SignatureTypeV2,
        OrderArgs,
        OrderType,
    )
    from py_clob_client_v2.clob_types import ApiCreds
except ImportError:
    try:
        from py_clob_client import (
            ClobClient,
            OrderArgs,
            OrderType,
        )
        from py_clob_client.clob_types import ApiCreds, SignatureType as SignatureTypeV2
    except ImportError:
        raise SystemExit(
            "\n❌ py-clob-client ontbreekt.\n"
            "Installeer via requirements.txt:\n"
            "py-clob-client\nrequests\n"
        )

# ============================================================
# CONFIGURATIE (PAPER TRADE INSTELLINGEN + PULLBACK)
# ============================================================

CLOB_HOST = "[https://clob.polymarket.com](https://clob.polymarket.com)"
POLYMARKET_GAMMA_URL = "[https://gamma-api.polymarket.com](https://gamma-api.polymarket.com)"
BINANCE_API_URL = "[https://api.binance.com/api/v3](https://api.binance.com/api/v3)"

CHAIN_ID = 137  # Polygon Mainnet

SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "3"))
MAX_TRADES_PER_CYCLE = int(os.environ.get("MAX_TRADES_PER_CYCLE", "2"))
MAX_ALLOWABLE_ODDS = float(os.environ.get("MAX_ALLOWABLE_ODDS", "0.70"))
LIVE_TRADE_AMOUNT = float(os.environ.get("LIVE_TRADE_AMOUNT", "5.0"))

# Maximaal toegestaan verschil tussen orderbook-prijs en get_price()-prijs
MAX_PRICE_DISCREPANCY = float(os.environ.get("MAX_PRICE_DISCREPANCY", "0.05"))

# Log-frequentie voor status-updates als er geen signaal is (in seconden)
VERBOSE_LOG_INTERVAL_SEC = 15

# DEBUG_TOKEN_MATCHING
DEBUG_TOKEN_MATCHING = os.environ.get("DEBUG_TOKEN_MATCHING", "1").strip() == "1"

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
SIGNATURE_TYPE_ENV = os.environ.get("POLYMARKET_SIGNATURE_TYPE", "").strip()

def clean_private_key(value: str) -> str:
    if not value:
        return ""
    if value.lower().startswith("0x"):
        value = value[2:]
    cleaned = "".join(re.findall(r"[0-9a-fA-F]", value))
    return cleaned

PK = clean_private_key(RAW_PRIVATE_KEY)

def get_signature_type():
    if not POLYMARKET_FUNDER_ADDRESS:
        return getattr(SignatureTypeV2, "EOA", 0)

    value = SIGNATURE_TYPE_ENV.lower()
    mapping = {
        "0": getattr(SignatureTypeV2, "EOA", 0),
        "eoa": getattr(SignatureTypeV2, "EOA", 0),
        "1": getattr(SignatureTypeV2, "POLY_PROXY", 1),
        "proxy": getattr(SignatureTypeV2, "POLY_PROXY", 1),
        "poly_proxy": getattr(SignatureTypeV2, "POLY_PROXY", 1),
        "2": getattr(SignatureTypeV2, "POLY_GNOSIS_SAFE", 2),
        "gnosis": getattr(SignatureTypeV2, "POLY_GNOSIS_SAFE", 2),
        "safe": getattr(SignatureTypeV2, "POLY_GNOSIS_SAFE", 2),
        "3": getattr(SignatureTypeV2, "POLY_1271", 3),
        "1271": getattr(SignatureTypeV2, "POLY_1271", 3),
    }
    return mapping.get(value, getattr(SignatureTypeV2, "EOA", 0))

SIG_TYPE = get_signature_type()

# ============================================================
# MARKETS & PAIR MAPPINGS
# ============================================================

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ZECUSDT"]

POLY_SYMBOL_ALIASES = {
    "BTCUSDT": ["btc", "bitcoin"],
    "ETHUSDT": ["eth", "ethereum"],
    "SOLUSDT": ["sol", "solana"],
    "BNBUSDT": ["bnb", "binance"],
    "XRPUSDT": ["xrp", "ripple"],
    "DOGEUSDT": ["doge", "dogecoin"],
    "ZECUSDT": ["zec", "zcash"],
}

active_signals_sent = {}
last_verbose_log_time = 0
session = requests.Session()
session.headers.update({"User-Agent": "PolyAutoTrader/5.3-debug"})

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
        kwargs = {
            "host": CLOB_HOST,
            "chain_id": CHAIN_ID,
            "key": PK,
            "use_server_time": True,
            "signature_type": SIG_TYPE
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

    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendMessage"
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
    avg_loss =
