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
    raise SystemExit(
        "\n❌ py-clob-client-v2 ontbreekt.\n"
        "Installeer via requirements.txt:\n"
        "py-clob-client-v2\nrequests\n"
    )


# ============================================================
# CONFIGURATIE (PAPER TRADE INSTELLINGEN + PULLBACK)
# ============================================================

CLOB_HOST = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
BINANCE_API_URL = "https://api.binance.com/api/v3"

CHAIN_ID = 137  # Polygon Mainnet

SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "3"))
MAX_TRADES_PER_CYCLE = int(os.environ.get("MAX_TRADES_PER_CYCLE", "2"))
MAX_ALLOWABLE_ODDS = float(os.environ.get("MAX_ALLOWABLE_ODDS", "0.70"))
LIVE_TRADE_AMOUNT = float(os.environ.get("LIVE_TRADE_AMOUNT", "5.0"))

# Maximaal toegestaan verschil tussen orderbook-prijs en get_price()-prijs
MAX_PRICE_DISCREPANCY = float(os.environ.get("MAX_PRICE_DISCREPANCY", "0.05"))


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
        return SignatureTypeV2.EOA

    value = SIGNATURE_TYPE_ENV.lower()
    mapping = {
        "0": SignatureTypeV2.EOA,
        "eoa": SignatureTypeV2.EOA,
        "1": SignatureTypeV2.POLY_PROXY,
        "proxy": SignatureTypeV2.POLY_PROXY,
        "poly_proxy": SignatureTypeV2.POLY_PROXY,
        "2": SignatureTypeV2.POLY_GNOSIS_SAFE,
        "gnosis": SignatureTypeV2.POLY_GNOSIS_SAFE,
        "safe": SignatureTypeV2.POLY_GNOSIS_SAFE,
        "3": SignatureTypeV2.POLY_1271,
        "1271": SignatureTypeV2.POLY_1271,
    }
    return mapping.get(value, SignatureTypeV2.EOA)


SIG_TYPE = get_signature_type()


# ============================================================
# MARKETS & PAIR MAPPINGS
# ============================================================

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ZECUSDT"]

POLY_SYMBOL_MAP = {
    "BTCUSDT": "btc",
    "ETHUSDT": "eth",
    "SOLUSDT": "sol",
    "BNBUSDT": "bnb",
    "XRPUSDT": "xrp",
    "DOGEUSDT": "doge",
    "ZECUSDT": "zec",
}

active_signals_sent = {}
session = requests.Session()
session.headers.update({"User-Agent": "PolyAutoTrader/3.7"})


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
        return None, None, None, None, None, None, None, None


# ============================================================
# STRATEGIE LOGICA (5M MOMENTUM, 15M TREND & 5M PULLBACK)
# ============================================================

def analyze_market_and_get_signal(symbol, rsi, prev_rsi, window_delta_pct, volume_ratio, trend_15m, rsi_15m, time_remaining_sec):
    if symbol in ["ZECUSDT", "DOGEUSDT"]:
        min_delta, min_vol_ratio = 0.08, 1.2
    elif symbol in ["SOLUSDT", "BNBUSDT", "XRPUSDT"]:
        min_delta, min_vol_ratio = 0.04, 1.1
    else:
        min_delta, min_vol_ratio = 0.02, 1.05

    now_sec = time.time()
    time_remaining_15m = int(900 - (now_sec % 900))

    # STRATEGIE 1: 5M Expiry Momentum (Eerste minuut van de candle)
    if 220 <= time_remaining_sec <= 280:
        if 53 <= rsi <= 68 and window_delta_pct >= min_delta and volume_ratio >= min_vol_ratio:
            if trend_15m != "BEARISH":
                return "UP", "5M_MOMENTUM", f"🚀 5M Momentum UP (+{window_delta_pct}% t.o.v. Strike, Vol {volume_ratio}x)"

        if 32 <= rsi <= 47 and window_delta_pct <= -min_delta and volume_ratio >= min_vol_ratio:
            if trend_15m != "BULLISH":
                return "DOWN", "5M_MOMENTUM", f"🚀 5M Momentum DOWN ({window_delta_pct}% t.o.v. Strike, Vol {volume_ratio}x)"

    # STRATEGIE 2: 15M Expiry Trend
    if time_remaining_15m <= 300 and time_remaining_sec <= 270:
        if trend_15m == "BULLISH" and rsi >= 50 and window_delta_pct > 0:
            return "UP", "15M_TREND", f"🌊 15M Trend Follow UP (15M RSI {rsi_15m:.1f}, 5M Delta +{window_delta_pct}%)"

        if trend_15m == "BEARISH" and rsi <= 50 and window_delta_pct < 0:
            return "DOWN", "15M_TREND", f"🌊 15M Trend Follow DOWN (15M RSI {rsi_15m:.1f}, 5M Delta {window_delta_pct}%)"

    # STRATEGIE 3: 5M Pullback / Reentry (Midden-tot-eind van de candle)
    if 30 <= time_remaining_sec <= 220:
        # Bullish Pullback: RSI veert op uit oversold (prev_rsi < 45, rsi > prev_rsi) én prijs staat boven strike
        if prev_rsi < 45 and rsi > prev_rsi and window_delta_pct > 0.01:
            if trend_15m != "BEARISH":
                return "UP", "5M_PULLBACK", f"🔄 5M Pullback Reentry UP (RSI {prev_rsi:.1f} ➔ {rsi:.1f}, Delta +{window_delta_pct}%)"

        # Bearish Pullback: RSI koelt af uit overbought (prev_rsi > 55, rsi < prev_rsi) én prijs staat onder strike
        if prev_rsi > 55 and rsi < prev_rsi and window_delta_pct < -0.01:
            if trend_15m != "BULLISH":
                return "DOWN", "5M_PULLBACK", f"🔄 5M Pullback Reentry DOWN (RSI {prev_rsi:.1f} ➔ {rsi:.1f}, Delta {window_delta_pct}%)"

    return None, None, None


# ============================================================
# POLYMARKET DYNAMISCHE EVENT-ZOEKER
# ============================================================

def find_current_polymarket_event(symbol: str):
    prefix = POLY_SYMBOL_MAP.get(symbol)
    if not prefix:
        return None

    url = f"{POLYMARKET_GAMMA_URL}/events?limit=10&active=true&closed=false&q={prefix}-updown-5m"
    data = request_json("GET", url, timeout=5)

    if isinstance(data, list) and len(data) > 0:
        for event in data:
            slug = event.get("slug", "")
            if f"{prefix}-updown-5m-" in slug and not event.get("closed", False):
                return event

    return None


def get_token_info(symbol: str, signal: str):
    event = find_current_polymarket_event(symbol)
    if not event:
        return None

    wanted = signal.lower()
    for market in event.get("markets", []):
        if market.get("closed", False) or not market.get("active", True):
            continue

        outcomes = safe_json_load(market.get("outcomes"))
        token_ids = safe_json_load(market.get("clobTokenIds"))

        for idx, outcome in enumerate(outcomes):
            norm = str(outcome).strip().lower()
            if (wanted == "up" and norm in {"up", "yes"}) or (wanted == "down" and norm in {"down", "no"}):
                return {
                    "token_id": str(token_ids[idx]),
                    "outcome": str(outcome),
                    "event_slug": event.get("slug", ""),
                    "url": f"https://polymarket.com/event/{event.get('slug', '')}?outcomeIndex={idx}"
                }
    return None


def execute_live_order(token_id: str, amount_usd: float, max_price: float):
    try:
        orderbook = clob_client.get_order_book(token_id)
        if not orderbook:
            return False, 0.0, "Geen orderbook ontvangen"

        asks = []
        if isinstance(orderbook, dict):
            asks = orderbook.get("asks", [])
        elif hasattr(orderbook, "asks"):
            asks = orderbook.asks

        if not asks:
            return False, 0.0, "Geen asks in orderbook"

        # Beste (laagste) ask is de laatste entry in de lijst
        best_ask_entry = asks[-1]
        if isinstance(best_ask_entry, dict):
            orderbook_ask = float(best_ask_entry.get("price", 0))
        else:
            orderbook_ask = float(getattr(best_ask_entry, "price", 0))

        if orderbook_ask <= 0:
            return False, 0.0, "Ongeldige ask prijs (orderbook)"

        # Kruiscontrole via get_price()
        live_ask = None
        try:
            live_price = clob_client.get_price(token_id, side="SELL")
            live_ask = float(live_price) if live_price is not None else None
        except Exception as e:
            print(f"⚠️ get_price() kruiscontrole mislukt: {e}")

        if live_ask is not None:
            price_diff = abs(orderbook_ask - live_ask)
            if price_diff > MAX_PRICE_DISCREPANCY:
                return False, orderbook_ask, (
                    f"Prijs-mismatch gedetecteerd: orderbook=${orderbook_ask:.3f} "
                    f"vs live_price=${live_ask:.3f} (verschil {price_diff:.3f}) "
                    f"-- order geweigerd uit voorzorg"
                )
            best_ask = live_ask
        else:
            best_ask = orderbook_ask

        if best_ask > max_price:
            return False, best_ask, f"Prijs te hoog (${best_ask:.3f} > max ${max_price:.3f})"

        # Aantal shares expliciet afronden op 2 decimalen
        shares = round(amount_usd / best_ask, 2)

        order_args = OrderArgs(
            price=best_ask,
            size=shares,
            side="BUY",
            token_id=token_id,
        )

        signed_order = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed_order, OrderType.FOK)

        if resp and resp.get("success"):
            order_id = resp.get("orderID", "N/A")
            return True, best_ask, order_id
        else:
            error_msg = resp.get("errorMsg", "Order afgewezen door CLOB")
            return False, best_ask, error_msg

    except Exception as e:
        return False, 0.0, str(e)


# ============================================================
# MAIN BOT LOOP
# ============================================================

def main():
    print("\n" + "=" * 60)
    print("🤖 POLYMARKET LIVE TRADING BOT (5M MOMENTUM, 15M TREND & PULLBACK)")
    print("=" * 60)
    print(f"Live order bedrag: ${LIVE_TRADE_AMOUNT:.2f} USD")
    print(f"Max Ask Limiet: ${MAX_ALLOWABLE_ODDS:.2f}")
    print(f"Scan Interval: {SCAN_INTERVAL_SEC}s")
    print(f"Max prijs-discrepantie (orderbook vs live): ${MAX_PRICE_DISCREPANCY:.2f}")
    print("=" * 60 + "\n")

    send_telegram_msg(
        "⚡ **Poly Auto-Trader LIVE Gestart (Inclusief Pullback Strategie)**\n\n"
        "🚨 **Systeemstatus:** VOLAUTOMATISCH TRADEN ACTIEF\n"
        f"💵 **Inzet per trade:** `${LIVE_TRADE_AMOUNT:.2f}`\n"
        f"🛡️ **Max Ask Limiet:** `${MAX_ALLOWABLE_ODDS:.2f}`\n"
        f"⏱️ **Scan Frequentie:** `{SCAN_INTERVAL_SEC}s`\n"
        "🎯 **Strategieën:** 5M Momentum + 15M Trend + 5M Pullback Reentry"
    )

    while True:
        try:
            now_sec = time.time()
            time_remaining_sec = int(300 - (now_sec % 300))
            cycle_id = int(now_sec // 300)

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
                        continue

                    trade_key = f"cycle_{cycle_id}_{symbol}_{strat_type}"
                    if trade_key in active_signals_sent:
                        continue

                    token_info = get_token_info(symbol, signal)
                    if not token_info:
                        continue

                    print(f"⚡ Automatische live order starten voor {symbol} ({signal} - {strat_type})...")
                    success, filled_price, info = execute_live_order(
                        token_info["token_id"],
                        LIVE_TRADE_AMOUNT,
                        MAX_ALLOWABLE_ODDS
                    )

                    active_signals_sent[trade_key] = True
                    current_cycle_count += 1

                    mins, secs = divmod(time_remaining_sec, 60)
                    time_str = f"~{mins}m {secs}s"

                    if success:
                        shares = round(LIVE_TRADE_AMOUNT / filled_price, 2)
                        msg = (
                            f"🚨 **SIGNALIERING: {strat_type}!**\n\n"
                            f"📍 **Pair:** {symbol} ({signal})\n"
                            f"🎯 **Target to Beat (5M Open):** `${strike}`\n"
                            f"📌 **Huidige Koers:** `${current_price}` ({delta_pct}%)\n"
                            f"📊 **Uitvoeringsprijs:** `${filled_price:.3f}` ({shares} aandelen)\n"
                            f"💵 **Inzetten op Polymarket:** `${LIVE_TRADE_AMOUNT:.2f}` ({signal})\n\n"
                            f"📊 **RSI:** {rsi} | **15M RSI:** {rsi_15m:.1f} | **Vol Ratio:** {vol_ratio}x\n"
                            f"⏳ **Resterend:** `{time_str}`\n\n"
                            f"💡 **Analyse:** {reason}\n\n"
                            f"🟢 **LIVE ORDER GEPLAATST** | Order ID: `{info}`"
                        )
                    else:
                        msg = (
                            f"🚨 **LIVE ORDER GEWEIGERD / MISLUKT**\n\n"
                            f"📍 **Pair:** {symbol} ({signal})\n"
                            f"⚠️ **Reden:** {info}"
                        )

                    send_telegram_msg(msg, token_info["url"])

        except KeyboardInterrupt:
            print("\n🛑 Bot handmatig gestopt.")
            break
        except Exception as e:
            print(f"\n❌ Fout in hoofdloop: {e}")

        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
