import os
import time
import math
import requests
import json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType, ApiCreds
from py_clob_client.order_builder.constants import BUY, SELL

# ============================================================
# CONFIGURATIE & OMGEVINGSVARIABELEN
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Binance symbols
SYMBOLS = ["SOLUSDT", "ETHUSDT", "BTCUSDT", "ZECUSDT", "BNBUSDT", "XRPUSDT"]

# Polymarket Aliases voor Crypto markten
POLY_SYMBOL_ALIASES = {
    "SOLUSDT": ["solana", "sol"],
    "ETHUSDT": ["ethereum", "eth"],
    "BTCUSDT": ["bitcoin", "btc"],
    "ZECUSDT": ["zcash", "zec"],
    "BNBUSDT": ["binance coin", "bnb"],
    "XRPUSDT": ["ripple", "xrp"]
}

# Trade instellingen
TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", "5.0"))  # Bedrag in USD per order
MAX_ALLOWABLE_ODDS = float(os.getenv("MAX_ALLOWABLE_ODDS", "0.70"))  # Max prijs/odds (bijv. 0.70 = 70 cent)

# Endpoints
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"

# Polymarket Authenticatie
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")

# Signature type: 0 = EOA, 1 = POLY_PROXY, 2 = POLY_GNOSIS_SAFE
SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))

# Initialiseer CLOB Client
clob_client = None
if POLYMARKET_PRIVATE_KEY and POLYMARKET_API_KEY:
    try:
        creds = ApiCreds(
            api_key=POLYMARKET_API_KEY,
            api_secret=POLYMARKET_API_SECRET,
            api_passphrase=POLYMARKET_API_PASSPHRASE
        )
        clob_client = ClobClient(
            host=CLOB_HOST,
            key=POLYMARKET_PRIVATE_KEY,
            chain_id=CHAIN_ID,
            creds=creds,
            signature_type=SIGNATURE_TYPE,
            funder=POLYMARKET_FUNDER_ADDRESS if POLYMARKET_FUNDER_ADDRESS else None
        )
        print("✅ Polymarket CLOB client succesvol geinitialiseerd.")
    except Exception as e:
        print(f"❌ Fout bij initialiseren Polymarket Client: {e}")

# ============================================================
# HELPER FUNCTIES
# ============================================================
def send_telegram_message(message: str, button_url: str = None, button_text: str = None):
    """Verstuurt een Telegram bericht, optioneel met een inline knop."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram token of chat ID ontbreekt. Bericht overgeslagen.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }

    if button_url and button_text:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": [[
                {"text": button_text, "url": button_url}
            ]]
        })

    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"❌ Fout bij versturen Telegram bericht: {e}")

def safe_json_load(data):
    if isinstance(data, str):
        try:
            return json.loads(data)
        except Exception:
            return []
    return data if isinstance(data, list) else []

def request_json(method: str, url: str, **kwargs):
    try:
        res = requests.request(method, url, **kwargs)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"⚠️ Netwerkfout bij request naar {url}: {e}")
    return None

# ============================================================
# GEECORRIGEERDE GET_TOKEN_INFO (MET EXACTE EVENT URL)
# ============================================================
def get_token_info(symbol: str, signal: str):
    """
    Zoekt het actieve token op Polymarket voor een gegeven crypto symbol (bijv. SOLUSDT)
    en bouwt de exacte URL naar het juiste event.
    """
    aliases = POLY_SYMBOL_ALIASES.get(symbol, [])
    wanted = signal.lower()

    # 1. Haal alle actieve CLOB sampling markten op
    clob_url = f"{CLOB_HOST}/sampling-markets"
    clob_data = request_json("GET", clob_url, timeout=5)

    if clob_data and isinstance(clob_data, list):
        for m in clob_data:
            question = str(m.get("question", "")).lower()
            description = str(m.get("description", "")).lower()
            text_block = f"{question} {description}"

            # Controleer of het symbool (bijv. 'sol' of 'solana') in de markttekst staat
            if any(alias in text_block for alias in aliases) and not m.get("closed", False):
                tokens = m.get("tokens", [])
                for idx, token in enumerate(tokens):
                    outcome = str(token.get("outcome", "")).lower()
                    token_id = str(token.get("token_id", ""))

                    is_up = (wanted == "up" and outcome in {"up", "yes", "above"})
                    is_down = (wanted == "down" and outcome in {"down", "no", "below"})

                    if (is_up or is_down) and token_id:
                        condition_id = m.get("condition_id", "")
                        
                        # Haal de exacte URL slug op via Gamma API op basis van condition_id
                        event_url = "https://polymarket.com"
                        if condition_id:
                            gamma_market = request_json("GET", f"{POLYMARKET_GAMMA_URL}/markets?condition_id={condition_id}", timeout=3)
                            if gamma_market and isinstance(gamma_market, list) and len(gamma_market) > 0:
                                slug = gamma_market[0].get("slug") or (gamma_market[0].get("events", [{}])[0].get("slug") if gamma_market[0].get("events") else None)
                                if slug:
                                    event_url = f"https://polymarket.com/event/{slug}"

                        print(f"✅ CLOB Match voor {symbol} ({signal}): {token_id[:10]}...")
                        return {
                            "token_id": token_id,
                            "outcome": token.get("outcome"),
                            "event_slug": condition_id,
                            "url": event_url
                        }

    # 2. Fallback: Zoek specifiek op de exacte alias via Gamma API
    for alias in aliases:
        gamma_url = f"{POLYMARKET_GAMMA_URL}/events?limit=10&active=true&closed=false&q={alias}"
        gamma_data = request_json("GET", gamma_url, timeout=5)

        if not gamma_data or not isinstance(gamma_data, list):
            continue

        for event in gamma_data:
            title = str(event.get("title", "")).lower()
            slug = event.get("slug", "")

            # Strikte controle: controleer of het event daadwerkelijk over de crypto asset gaat
            if not any(a in title or a in slug for a in aliases):
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

                    if is_up or is_down:
                        if idx < len(token_ids):
                            token_id = str(token_ids[idx])
                            print(f"✅ Gamma Match voor {symbol} ({signal}): {slug}")
                            return {
                                "token_id": token_id,
                                "outcome": str(outcome),
                                "event_slug": slug,
                                "url": f"https://polymarket.com/event/{slug}"
                            }

    print(f"⚠️ Geen matchend actief token gevonden op Polymarket voor {symbol} ({signal})")
    return None

# ============================================================
# TRADING LOGICA
# ============================================================
def execute_polymarket_order(symbol: str, signal: str):
    """Haalt het juiste token op, controleert de prijs en plaatst een order op Polymarket."""
    token_info = get_token_info(symbol, signal)
    if not token_info:
        send_telegram_message(f"⚠️ *SIGNAAL ONTVANGEN MAAR MARKT NIET GEVONDEN*\n\n📍 **Pair:** {symbol} ({signal.upper()})\n🔍 Kan geen actieve Polymarket uitkomst vinden.")
        return

    token_id = token_info["token_id"]
    event_url = token_info["url"]

    if not clob_client:
        send_telegram_message(
            f"🚀 *SIGNAAL ONTVANGEN (GEEN LIVE TRADING)*\n\n📍 **Pair:** {symbol} ({signal.upper()})\nℹ️ Geen API key ingesteld.",
            button_url=event_url,
            button_text="🚀 ZET DIRECT IN OP POLYMARKET"
        )
        return

    try:
        # Haal actuele orderbook op om de beste ask-prijs te bepalen
        book = clob_client.get_order_book(token_id)
        if not book.asks:
            send_telegram_message(f"🚨 *ORDER MISLUKT*\n\n📍 **Pair:** {symbol} ({signal.upper()})\n⚠️ Geen verkopers (asks) in orderbook.", button_url=event_url, button_text="🚀 ZET DIRECT IN OP POLYMARKET")
            return

        best_ask = float(book.asks[0].price)

        # Prijslimiet controle
        if best_ask > MAX_ALLOWABLE_ODDS:
            send_telegram_message(
                f"🚨 *LIVE ORDER GEWEIGERD / MISLUKT*\n\n📍 **Pair:** {symbol} ({signal.upper()})\n⚠️ **Reden:** Prijs te hoog (${best_ask:.3f} > max ${MAX_ALLOWABLE_ODDS:.3f})",
                button_url=event_url,
                button_text="🚀 ZET DIRECT IN OP POLYMARKET"
            )
            return

        # Bereken het aantal te kopen shares
        shares = math.floor(TRADE_AMOUNT_USD / best_ask)
        if shares < 1:
            shares = 1

        print(f"🛒 Order uitvoeren: {shares} shares van {symbol} op {best_ask:.3f} USD")

        # Plaats FOK Market Order
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=shares,
            side=BUY,
            price=best_ask
        )
        signed_order = clob_client.create_market_order(order_args)
        resp = clob_client.post_order(signed_order, OrderType.FOK)

        if resp and resp.get("success"):
            order_id = resp.get("orderID", "N/A")
            send_telegram_message(
                f"✅ *LIVE ORDER GEPLAATST!*\n\n📍 **Pair:** {symbol} ({signal.upper()})\n💵 **Inzet:** ${TRADE_AMOUNT_USD:.2f} (~{shares} shares)\n📊 **Prijs:** ${best_ask:.3f}\n🆔 **Order ID:** `{order_id}`",
                button_url=event_url,
                button_text="🚀 ZET DIRECT IN OP POLYMARKET"
            )
        else:
            error_msg = resp.get("errorMsg") if resp else "Onbekende fout"
            send_telegram_message(
                f"🚨 *LIVE ORDER GEWEIGERD / MISLUKT*\n\n📍 **Pair:** {symbol} ({signal.upper()})\n⚠️ **Reden:** {error_msg}",
                button_url=event_url,
                button_text="🚀 ZET DIRECT IN OP POLYMARKET"
            )

    except Exception as e:
        print(f"❌ Fout tijdens order executie: {e}")
        send_telegram_message(
            f"🚨 *LIVE ORDER GEWEIGERD / MISLUKT*\n\n📍 **Pair:** {symbol} ({signal.upper()})\n⚠️ **Reden:** {e}",
            button_url=event_url,
            button_text="🚀 ZET DIRECT IN OP POLYMARKET"
        )

# ============================================================
# MAIN LOOP / STRATEGIE MONITOR
# ============================================================
def check_signals():
    """
    Placeholder/voorbeeld functie voor het scannen van markt-signalen.
    Hier koppel je jouw eigen Binance indicator- / trend-logica aan.
    """
    print("🔍 Controleren op nieuwe handelssignalen...")

if __name__ == "__main__":
    print("🤖 Nobeulele's Poly Bot is gestart...")
    send_telegram_message("🤖 *Nobeulele's Poly Bot is succesvol opgestart!*")
    
    while True:
        try:
            check_signals()
        except Exception as e:
            print(f"❌ Fout in hoofd-loop: {e}")
        time.sleep(10)
