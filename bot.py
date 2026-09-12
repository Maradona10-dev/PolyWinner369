import os
import time
import re
from typing import Optional, Dict, Any, List
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
# CONFIGURATIE & INSTELLINGEN (MAKER SNIPER)
# ============================================================
CLOB_HOST = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
BINANCE_API_URL = "https://api.binance.com/api/v3"
CHAIN_ID = 137

SCAN_INTERVAL_SEC = float(os.environ.get("SCAN_INTERVAL_SEC", "0.2"))             # 200ms snelle scanlus
PRE_CLOSE_WINDOW_START = int(os.environ.get("PRE_CLOSE_WINDOW_START", "290"))      # Vanaf sec 290 (T-10s)
PRE_CLOSE_WINDOW_END = int(os.environ.get("PRE_CLOSE_WINDOW_END", "298"))          # Tot sec 298 (T-2s)

# MAKER LIMIT INSTELLINGEN
MAX_MAKER_PRICE = float(os.environ.get("MAX_MAKER_PRICE", "0.72"))                 # Absolute max limit buy prijs ($0.72)
TARGET_MAKER_PRICE = float(os.environ.get("TARGET_MAKER_PRICE", "0.62"))           # Ideale instapprijs ($0.62)
LIVE_TRADE_AMOUNT = float(os.environ.get("LIVE_TRADE_AMOUNT", "5.0"))              # Inzet in USD
MIN_BREAKOUT_PCT = float(os.environ.get("MIN_BREAKOUT_PCT", "0.05"))               # Min % beweging op Binance candle
ORDER_TIMEOUT_SEC = int(os.environ.get("ORDER_TIMEOUT_SEC", "15"))                 # Cancelt onvervulde maker orders na 15s

# TELEGRAM CONFIGURATIE
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN[3:] if RAW_TOKEN.startswith("bot") else RAW_TOKEN
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# WALLET CONFIGURATIE
RAW_PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
POLYMARKET_FUNDER_ADDRESS = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
SIGNATURE_TYPE_ENV = os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1").strip()

def clean_private_key(value: str) -> str:
    if not value: return ""
    if value.lower().startswith("0x"): value = value[2:]
    return "".join(re.findall(r"[0-9a-fA-F]", value))

PK = clean_private_key(RAW_PRIVATE_KEY)

def get_signature_type() -> int:
    try: return int(SIGNATURE_TYPE_ENV)
    except ValueError: return 1

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

active_signals = {}
open_orders_tracker = {}
session = requests.Session()
session.headers.update({"User-Agent": "PolyMakerBot/11.0"})

def request_json(method: str, url: str, *, timeout: int = 2, **kwargs):
    try:
        resp = session.request(method, url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"⚠️ Netwerkfout ({url}): {e}")
    return None

def init_clob_client():
    if not PK: raise SystemExit("❌ FOUT: Geen POLYMARKET_PRIVATE_KEY ingesteld.")
    try:
        kwargs = {"host": CLOB_HOST, "key": PK, "chain_id": CHAIN_ID, "signature_type": SIG_TYPE}
        if POLYMARKET_FUNDER_ADDRESS: kwargs["funder"] = POLYMARKET_FUNDER_ADDRESS
        client = ClobClient(**kwargs)
        try: creds = client.derive_api_key()
        except Exception: creds = client.create_api_key()
        client.set_api_creds(creds)
        print(f"✅ CLOB Verbonden | Wallet: {client.get_address()}")
        return client
    except Exception as e:
        raise SystemExit(f"❌ CLOB Initialisatie mislukt: {e}")

clob_client = init_clob_client()

def send_telegram_msg(message: str, button_url: Optional[str] = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}
    if button_url:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "🚀 BEKIJK MAKER ORDER OP POLYMARKET", "url": button_url}]]}
    try:
        session.post(url, json=payload, timeout=2)
        return True
    except Exception as e:
        print(f"⚠️ Telegram Fout: {e}")
        return False

# ============================================================
# MOMENTUM & TOKEN SEARCH
# ============================================================
def analyze_binance_momentum(symbol: str) -> Optional[str]:
    """ Checkt Binance candle vlak voor kaarswissel (T-10s tot T-2s) """
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
                    import json
                    try: outcomes = json.loads(outcomes)
                    except: pass
                if isinstance(token_ids, str):
                    import json
                    try: token_ids = json.loads(token_ids)
                    except: pass

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

# ============================================================
# PRE-CLOSE MAKER ORDER EXECUTION
# ============================================================
def execute_maker_limit_order(token_id: str, amount_usd: float) -> tuple[bool, float, str]:
    """
    Plaatst een PASSIEVE LIMIT ORDER om Maker te blijven (0% fee + Maker Rebates).
    Order wordt gecalculeerd net boven best_bid maar onder best_ask.
    """
    try:
        orderbook = clob_client.get_order_book(token_id)
        bids = getattr(orderbook, "bids", []) or (orderbook.get("bids", []) if isinstance(orderbook, dict) else [])
        asks = getattr(orderbook, "asks", []) or (orderbook.get("asks", []) if isinstance(orderbook, dict) else [])

        def get_p(entry):
            return float(entry.get("price", 0)) if isinstance(entry, dict) else float(getattr(entry, "price", 0))

        best_bid = get_p(sorted(bids, key=get_p, reverse=True)[0]) if bids else 0.50
        best_ask = get_p(sorted(asks, key=get_p)[0]) if asks else 0.99

        # We plaatsen onze Limit Buy net 1 cent boven de best_bid (passief)
        maker_price = round(min(best_bid + 0.01, TARGET_MAKER_PRICE), 3)

        # Zorg dat we NIET de ask crossen (voorkomt direct Taker execution)
        if maker_price >= best_ask:
            maker_price = round(best_ask - 0.01, 3)

        # Cap op maximale instapprijs
        if maker_price > MAX_MAKER_PRICE:
            return False, maker_price, f"Maker prijs (${maker_price:.3f}) > Max ingesteld (${MAX_MAKER_PRICE:.3f})"

        if maker_price <= 0.05:
            return False, maker_price, "Geen geldige bid/ask diepte gevonden"

        shares = round(amount_usd / maker_price, 2)

        # PLAATSEN VAN GTL (Good-Till-Cancelled) LIMIT MAKER ORDER
        order_args = OrderArgs(price=maker_price, size=shares, side="BUY", token_id=token_id)
        signed_order = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed_order, OrderType.GTC)

        if resp and isinstance(resp, dict) and resp.get("success"):
            order_id = resp.get("orderID", "N/A")
            return True, maker_price, order_id
        else:
            return False, maker_price, str(resp)

    except Exception as e:
        return False, 0.0, str(e)

def auto_cancel_stale_orders():
    """ Verwijdert niet-gevulde limit orders na de ingestelde timeout """
    now = time.time()
    to_delete = []
    for order_id, created_at in open_orders_tracker.items():
        if now - created_at > ORDER_TIMEOUT_SEC:
            try:
                clob_client.cancel_order(order_id)
                print(f"🧹 Stale Maker Limit Order Gecanceld: {order_id}")
            except Exception as e:
                print(f"⚠️ Kon order {order_id} niet cancelen: {e}")
            to_delete.append(order_id)

    for oid in to_delete:
        del open_orders_tracker[oid]

# ============================================================
# MAIN LUS
# ============================================================
def main():
    print("\n" + "=" * 60)
    print("🤖 POLYMARKET PRE-CLOSE MAKER / SOFT SNIPER BOT V11.0")
    print("=" * 60)

    while True:
        try:
            now_sec = time.time()
            time_elapsed = int(now_sec % 300)
            cycle_id = int(now_sec // 300)

            # STAP 1: PRE-CLOSE VENSTER (T-10s TOT T-2s VÓÓR CANDLE CLOSE)
            if PRE_CLOSE_WINDOW_START <= time_elapsed <= PRE_CLOSE_WINDOW_END:
                for symbol in PAIRS:
                    trade_key = f"cycle_{cycle_id}_{symbol}"
                    if trade_key in active_signals:
                        continue

                    # Pre-close momentum check
                    signal = analyze_binance_momentum(symbol)
                    if not signal: continue

                    token_info = get_token_info(symbol, signal)
                    if not token_info: continue

                    print(f"🎯 [{symbol}] PRE-CLOSE MOMENTUM GEVONDEN ({signal}) op T-{300-time_elapsed}s!")

                    # Voer de passieve Limit Order uit
                    success, price, info = execute_maker_limit_order(token_info["token_id"], LIVE_TRADE_AMOUNT)

                    active_signals[trade_key] = True

                    if success:
                        open_orders_tracker[info] = time.time()
                        msg = (
                            f"🔷 **PRE-CLOSE MAKER ORDER GEPLAATST**\n"
                            f"Asset: `{symbol}` ({signal})\n"
                            f"Limit Prijs: `${price:.3f}` (Passief / 0% Fee)\n"
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

            # STAP 2: VERWIJDER OVERDUMPED / STALE ORDERS
            auto_cancel_stale_orders()

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Fout in hoofdloop: {e}")

        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
