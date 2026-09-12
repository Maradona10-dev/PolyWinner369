import os
import time
import re
import json
from typing import Optional, Dict, Any
import requests

from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderType

# ============================================================
# CONFIGURATIE & INSTELLINGEN
# ============================================================
CLOB_HOST = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
BINANCE_API_URL = "https://api.binance.com/api/v3"
CHAIN_ID = 137

SCAN_INTERVAL_SEC = float(os.environ.get("SCAN_INTERVAL_SEC", "0.2"))
PRE_CLOSE_WINDOW_START = int(os.environ.get("PRE_CLOSE_WINDOW_START", "290"))
PRE_CLOSE_WINDOW_END = int(os.environ.get("PRE_CLOSE_WINDOW_END", "298"))

MAX_MAKER_PRICE = float(os.environ.get("MAX_MAKER_PRICE", "0.72"))
TARGET_MAKER_PRICE = float(os.environ.get("TARGET_MAKER_PRICE", "0.62"))
LIVE_TRADE_AMOUNT = float(os.environ.get("LIVE_TRADE_AMOUNT", "5.0"))
MIN_BREAKOUT_PCT = float(os.environ.get("MIN_BREAKOUT_PCT", "0.05"))
ORDER_TIMEOUT_SEC = int(os.environ.get("ORDER_TIMEOUT_SEC", "15"))

# TELEGRAM
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN[3:] if RAW_TOKEN.startswith("bot") else RAW_TOKEN
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# WALLET CONFIGURATIE (AUTOMATISCHE SAFE DETECTIE VOOR METAMASK)
RAW_PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER_ADDRESS = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()

# Forceer automatisch SigType 2 als er een funder-adres is ingesteld
if FUNDER_ADDRESS:
    SIG_TYPE = 2
    print(f"🔒 Funder adres gedetecteerd! Signature Type wordt automatisch geforceerd op 2 (POLY_GNOSIS_SAFE).")
else:
    SIG_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))

def clean_private_key(value: str) -> str:
    if not value: return ""
    if value.lower().startswith("0x"): value = value[2:]
    return "".join(re.findall(r"[0-9a-fA-F]", value))

PK = clean_private_key(RAW_PRIVATE_KEY)

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

active_signals = {}
open_orders_tracker = {}
session = requests.Session()
session.headers.update({"User-Agent": "PolyMakerBot/30.0"})

def request_json(method: str, url: str, *, timeout: int = 2, **kwargs):
    try:
        resp = session.request(method, url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"⚠️ Netwerkfout ({url}): {e}")
    return None

def send_telegram_msg(message: str, button_url: Optional[str] = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}
    if button_url:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "🚀 BEKIJK MAKER ORDER OP POLYMARKET", "url": button_url}]]}
    try:
        r = session.post(url, json=payload, timeout=2)
        return r.ok
    except Exception:
        return False

# ============================================================
# INITIALISATIE CLOB CLIENT (METAMASK / SAFE)
# ============================================================
def init_clob_client():
    if not PK: 
        raise SystemExit("❌ FOUT: Geen POLYMARKET_PRIVATE_KEY ingesteld.")

    client_args = {
        "host": CLOB_HOST,
        "key": PK,
        "chain_id": CHAIN_ID,
        "signature_type": SIG_TYPE,
    }
    
    if FUNDER_ADDRESS:
        client_args["funder"] = FUNDER_ADDRESS

    try:
        auth_client = ClobClient(**client_args)

        try:
            creds = auth_client.create_or_derive_api_key()
            print("🔑 API Key succesvol geregistreerd.")
        except Exception:
            creds = auth_client.derive_api_key()
            print("🔑 Bestaande L2 API Key opgehaald.")

        client_args["creds"] = creds
        client = ClobClient(**client_args)

        print(f"✅ MetaMask / Safe Client Verbonden!")
        print(f"🔹 SigType: {SIG_TYPE} (POLY_GNOSIS_SAFE)")
        print(f"🔹 Signer Wallet Adres: {client.get_address()}")
        if FUNDER_ADDRESS:
            print(f"🔹 Funder (Safe) Adres: {FUNDER_ADDRESS}")
        return client
    except Exception as e:
        raise SystemExit(f"❌ CLOB Initialisatie mislukt: {e}")

clob_client = init_clob_client()

# ============================================================
# MOMENTUM & EXECUTION LOGICA
# ============================================================
def analyze_binance_momentum(symbol: str) -> Optional[str]:
    url = f"{BINANCE_API_URL}/klines"
    params = {"symbol": symbol, "interval": "5m", "limit": 1}
    klines = request_json("GET", url, params=params)

    if not klines or len(klines) < 1: return None

    candle = klines[0]
    open_p = float(candle[1])
    curr_p = float(candle[4])
    if open_p == 0: return None

    pct = ((curr_p - open_p) / open_p) * 100
    if pct >= MIN_BREAKOUT_PCT: return "UP"
    if pct <= -MIN_BREAKOUT_PCT: return "DOWN"
    return None

def get_token_info(symbol: str, signal: str):
    aliases = POLY_SYMBOL_ALIASES.get(symbol, [])
    wanted = signal.lower()

    for alias in aliases:
        gamma_url = f"{POLYMARKET_GAMMA_URL}/events?limit=30&active=true&closed=false&q={alias}"
        gamma_data = request_json("GET", gamma_url, timeout=2)
        if not gamma_data or not isinstance(gamma_data, list): continue

        for event in gamma_data:
            slug = str(event.get("slug", "")).lower()
            markets = event.get("markets", [])
            for market in markets:
                if market.get("closed", False) or not market.get("active", True): continue

                outcomes = market.get("outcomes", [])
                token_ids = market.get("clobTokenIds", [])
                if isinstance(outcomes, str):
                    try: outcomes = json.loads(outcomes)
                    except Exception: pass
                if isinstance(token_ids, str):
                    try: token_ids = json.loads(token_ids)
                    except Exception: pass

                if not outcomes or not token_ids: continue

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

def execute_maker_limit_order(token_id: str, amount_usd: float):
    try:
        best_bid = 0.50
        best_ask = 0.52

        try:
            mid_res = clob_client.get_midpoint(token_id)
            if isinstance(mid_res, dict) and "mid" in mid_res:
                mid = float(mid_res["mid"])
                if mid > 0.05:
                    best_bid = round(mid - 0.01, 3)
                    best_ask = round(mid + 0.01, 3)
        except Exception:
            pass

        maker_price = round(min(best_bid + 0.01, TARGET_MAKER_PRICE), 3)
        if maker_price >= best_ask: maker_price = round(best_ask - 0.01, 3)

        if maker_price > MAX_MAKER_PRICE or maker_price < 0.10:
            return False, maker_price, f"Prijs buiten bereik (${maker_price:.3f})"

        shares = round(amount_usd / maker_price, 2)

        order_args = OrderArgs(
            price=float(maker_price),
            size=float(shares),
            side="BUY",
            token_id=str(token_id)
        )

        signed_order = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed_order, OrderType.GTC)

        if resp and isinstance(resp, dict) and resp.get("success"):
            order_id = resp.get("orderID", resp.get("id", "N/A"))
            return True, maker_price, order_id
        else:
            return False, maker_price, str(resp)

    except Exception as e:
        return False, 0.0, str(e)

def auto_cancel_stale_orders():
    now = time.time()
    to_delete = []
    for order_id, created_at in open_orders_tracker.items():
        if now - created_at > ORDER_TIMEOUT_SEC:
            try:
                clob_client.cancel_order(order_id)
                print(f"🧹 Order gecanceld: {order_id}")
            except Exception:
                pass
            to_delete.append(order_id)

    for oid in to_delete:
        del open_orders_tracker[oid]

def main():
    print("🤖 BOT ACTIEF (METAMARK / SAFE MODE)")
    send_telegram_msg("🚀 **Bot gestart in MetaMask / Gnosis Safe Mode!**")

    while True:
        try:
            now_sec = time.time()
            time_elapsed = int(now_sec % 300)
            cycle_id = int(now_sec // 300)

            if PRE_CLOSE_WINDOW_START <= time_elapsed <= PRE_CLOSE_WINDOW_END:
                for symbol in PAIRS:
                    trade_key = f"cycle_{cycle_id}_{symbol}"
                    if trade_key in active_signals: continue

                    signal = analyze_binance_momentum(symbol)
                    if not signal: continue

                    token_info = get_token_info(symbol, signal)
                    if not token_info: continue

                    success, price, info = execute_maker_limit_order(token_info["token_id"], LIVE_TRADE_AMOUNT)
                    active_signals[trade_key] = True

                    if success:
                        open_orders_tracker[info] = time.time()
                        msg = (
                            f"🔷 **PRE-CLOSE MAKER ORDER GEPLAATST**\n"
                            f"Asset: `{symbol}` ({signal})\n"
                            f"Limit Prijs: `${price:.3f}`\n"
                            f"Order ID: `{info}`"
                        )
                        send_telegram_msg(msg, token_info["url"])
                    else:
                        msg = (
                            f"🚨 **MAKER ORDER OVERGESLAGEN**\n"
                            f"Asset: `{symbol}` ({signal})\n"
                            f"Berekende Limit Prijs: `${price:.3f}`\n"
                            f"Reden: `{info}`"
                        )
                        send_telegram_msg(msg, token_info["url"])

            auto_cancel_stale_orders()

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Fout: {e}")

        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
