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
    )
except ImportError:
    raise SystemExit(
        "\n❌ py-clob-client-v2 ontbreekt.\n"
        "Installeer eerst:\n\n"
        "pip install -U py-clob-client-v2 requests\n"
    )


# ============================================================
# CONFIGURATIE
# ============================================================

CLOB_HOST = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
BINANCE_API_URL = "https://api.binance.com/api/v3"

CHAIN_ID = 137

# Aangepast naar 12s om Binance & Polymarket API rate limits te beschermen
SCAN_INTERVAL_SEC = int(
    os.environ.get("SCAN_INTERVAL_SEC", "12")
)

MAX_TRADES_PER_CYCLE = int(
    os.environ.get("MAX_TRADES_PER_CYCLE", "2")
)

MAX_ALLOWABLE_ODDS = float(
    os.environ.get("MAX_ALLOWABLE_ODDS", "0.70")
)

LIVE_TRADE_AMOUNT = float(
    os.environ.get("LIVE_TRADE_AMOUNT", "5.0")
)

PAPER_TRADE_AMOUNT = float(
    os.environ.get("PAPER_TRADE_AMOUNT", "25.0")
)

PAPER_BALANCE = float(
    os.environ.get("PAPER_BALANCE", "1000.0")
)

ENV_LIVE = (
    os.environ
    .get("LIVE_TRADING", "False")
    .strip()
    .lower()
)

REQUESTED_LIVE_MODE = ENV_LIVE in {
    "true",
    "1",
    "yes",
    "on",
}


# ============================================================
# TELEGRAM CONFIGURATIE
# ============================================================

RAW_TOKEN = (
    os.environ
    .get("TELEGRAM_BOT_TOKEN", "")
    .strip()
)

if RAW_TOKEN.startswith("bot"):
    TELEGRAM_BOT_TOKEN = RAW_TOKEN[3:]
else:
    TELEGRAM_BOT_TOKEN = RAW_TOKEN

TELEGRAM_CHAT_ID = (
    os.environ
    .get("TELEGRAM_CHAT_ID", "")
    .strip()
)


# ============================================================
# POLYMARKET WALLET & VALIDATIE
# ============================================================

RAW_PRIVATE_KEY = (
    os.environ
    .get("POLYMARKET_PRIVATE_KEY", "")
    .strip()
)

POLYMARKET_FUNDER_ADDRESS = (
    os.environ
    .get("POLYMARKET_FUNDER_ADDRESS", "")
    .strip()
)

SIGNATURE_TYPE_ENV = (
    os.environ
    .get("POLYMARKET_SIGNATURE_TYPE", "")
    .strip()
)


def clean_private_key(value: str) -> str:
    """
    Valideert de private key strikt op 64 hex-tekens in plaats van
    ongeldige karakters stilzwijgend te strippen.
    """
    if not value:
        return ""

    value = "".join(value.split())

    if value.lower().startswith("0x"):
        value = value[2:]

    if len(value) != 64:
        print(f"⚠️ Private key heeft {len(value)} tekens in plaats van 64.")
        return ""

    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        print("⚠️ Private key bevat ongeldige tekens.")
        return ""

    return value


PK = clean_private_key(RAW_PRIVATE_KEY)


# ============================================================
# SIGNATURE TYPE MAPPING
# ============================================================

def get_signature_type():
    """
    V2 Mapping:
    0 = EOA
    1 = POLY_PROXY
    2 = POLY_GNOSIS_SAFE
    3 = POLY_1271 / Deposit Wallet
    """
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
        "deposit": SignatureTypeV2.POLY_1271,
        "deposit_wallet": SignatureTypeV2.POLY_1271,
        "poly_1271": SignatureTypeV2.POLY_1271,
    }

    return mapping.get(value, None)


SIG_TYPE = get_signature_type()


# ============================================================
# MARKETS & PAIR MAPPINGS
# ============================================================

PAIRS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ZECUSDT",
]

POLY_SYMBOL_MAP = {
    "BTCUSDT": "btc",
    "ETHUSDT": "eth",
    "SOLUSDT": "sol",
    "BNBUSDT": "bnb",
    "XRPUSDT": "xrp",
    "DOGEUSDT": "doge",
    "ZECUSDT": "zec",
}

COIN_KEYWORDS = {
    "BTCUSDT": ["bitcoin", "btc"],
    "ETHUSDT": ["ethereum", "eth"],
    "SOLUSDT": ["solana", "sol"],
    "BNBUSDT": ["bnb", "binance"],
    "XRPUSDT": ["xrp", "ripple"],
    "DOGEUSDT": ["doge", "dogecoin"],
    "ZECUSDT": ["zec", "zcash"],
}


# ============================================================
# GLOBAL STATE & HTTP SESSION
# ============================================================

active_paper_trades = []
active_signals_sent = {}

# Gebruik van requests.Session voor betere performance en verbindinghergebruik
session = requests.Session()
session.headers.update({
    "User-Agent": "PolySignalBot/2.0"
})


# ============================================================
# HELPERS & HTTP WRAPPERS
# ============================================================

def safe_json_load(value):
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        return []


def request_json(method: str, url: str, *, timeout: int = 10, **kwargs):
    """
    Centrale netwerk-wrapper met robuuste error-handling.
    """
    try:
        response = session.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        print(f"⚠️ HTTP {status}: {url}")
    except requests.RequestException as e:
        print(f"⚠️ Netwerkfout: {url} | {e}")
    except ValueError:
        print(f"⚠️ Geen geldige JSON ontvangen van: {url}")
    return None


# ============================================================
# POLYMARKET CLOB CLIENT INITIALISATIE
# ============================================================

def init_clob_client():
    if not PK:
        print("ℹ️ Geen geldige Polymarket private key ingesteld. Publieke marktdata blijft actief.")
        return None

    if POLYMARKET_FUNDER_ADDRESS and SIG_TYPE is None:
        print(
            "\n❌ POLYMARKET_SIGNATURE_TYPE ontbreekt of is ongeldig.\n"
            "Opties: 1 (Proxy), 2 (Gnosis Safe), 3 (Deposit Wallet / 1271)\n"
        )
        return None

    try:
        kwargs = {
            "host": CLOB_HOST,
            "chain_id": CHAIN_ID,
            "key": PK,
            "use_server_time": True,
        }

        if SIG_TYPE is not None:
            kwargs["signature_type"] = SIG_TYPE

        if POLYMARKET_FUNDER_ADDRESS:
            kwargs["funder"] = POLYMARKET_FUNDER_ADDRESS

        client = ClobClient(**kwargs)
        print(f"🔐 Wallet signer: {client.get_address()}")

        creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)

        server_time = client.get_server_time()
        print("✅ Polymarket CLOB V2 authenticatie succesvol.")
        print(f"⏱️ CLOB Server Time: {server_time}")
        print(f"🔏 Signature Type: {SIG_TYPE}")
        if POLYMARKET_FUNDER_ADDRESS:
            print(f"💼 Funder: {POLYMARKET_FUNDER_ADDRESS}")

        return client

    except Exception as e:
        print("\n❌ Polymarket CLOB initialisatie mislukt")
        print(f"Type: {type(e).__name__} | Error: {e}\n")
        return None


clob_client = init_clob_client()


# ============================================================
# TELEGRAM BERICHTEN
# ============================================================

def send_telegram_msg(message: str, button_url: Optional[str] = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials ontbreken.")
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
            "inline_keyboard": [[{
                "text": "🌐 OPEN EN HANDEL OP POLYMARKET",
                "url": button_url
            }]]
        }

    for attempt in range(2):
        try:
            response = session.post(url, json=payload, timeout=6)
            data = response.json()
            if data.get("ok"):
                return True
            print(f"⚠️ Telegram error: {data}")
        except Exception as e:
            print(f"⚠️ Telegram poging {attempt + 1} mislukt: {e}")
        time.sleep(1)

    return False


# ============================================================
# BINANCE DATA & RSI BEREKENING
# ============================================================

def fetch_klines(symbol: str, interval: str, limit: int):
    data = request_json(
        "GET",
        f"{BINANCE_API_URL}/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=5
    )
    return data if isinstance(data, list) else None


def calculate_rsi(prices, period: int = 14):
    if len(prices) < period + 1:
        return 50.0

    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def fetch_15m_trend(symbol: str):
    try:
        # Voldoende candles ophalen voor een accurate RSI14
        candles = fetch_klines(symbol, "15m", 30)
        if not candles:
            return "NEUTRAL"

        closes = [float(k[4]) for k in candles]
        rsi_15m = calculate_rsi(closes, 14)
        price_change = ((closes[-1] - closes[-3]) / closes[-3]) * 100

        if price_change > 0.10 and rsi_15m > 50:
            return "BULLISH"
        if price_change < -0.10 and rsi_15m < 50:
            return "BEARISH"

        return "NEUTRAL"
    except Exception as e:
        print(f"⚠️ 15m trend {symbol}: {e}")
        return "NEUTRAL"


def fetch_binance_data(symbol: str):
    try:
        res_5m = fetch_klines(symbol, "5m", 2)
        if not res_5m:
            raise ValueError("Geen 5m Binance-data")

        window_strike_price = float(res_5m[-1][1])

        res_1m = fetch_klines(symbol, "1m", 30)
        if not res_1m:
            raise ValueError("Geen 1m Binance-data")

        close_prices = [float(k[4]) for k in res_1m]
        volumes = [float(k[5]) for k in res_1m]

        current_price = close_prices[-1]
        window_delta_pct = round(((current_price - window_strike_price) / window_strike_price) * 100, 3)

        rsi = calculate_rsi(close_prices)
        prev_rsi = calculate_rsi(close_prices[:-1])

        current_volume = volumes[-1]
        avg_volume = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else current_volume
        volume_ratio = round(current_volume / avg_volume, 2) if avg_volume > 0 else 1.0

        return current_price, window_strike_price, window_delta_pct, rsi, prev_rsi, volume_ratio

    except Exception as e:
        print(f"⚠️ Binance {symbol}: {e}")
        return None, None, None, None, None, None


# ============================================================
# STRATEGIE & SIGNAALLOGICA
# ============================================================

def analyze_market_and_get_signal(symbol, rsi, prev_rsi, window_delta_pct, volume_ratio, time_remaining_sec):
    trend_15m = fetch_15m_trend(symbol)

    if symbol in ["ZECUSDT", "DOGEUSDT"]:
        min_delta, min_vol_ratio, pullback_delta = 0.14, 1.6, 0.20
    elif symbol in ["SOLUSDT", "BNBUSDT", "XRPUSDT"]:
        min_delta, min_vol_ratio, pullback_delta = 0.08, 1.3, 0.15
    else:
        min_delta, min_vol_ratio, pullback_delta = 0.05, 1.2, 0.12

    # Momentum Venster: 2:00 tot 4:15 resterend
    if 120 <= time_remaining_sec <= 255:
        if 53 <= rsi <= 68 and window_delta_pct >= min_delta and volume_ratio >= min_vol_ratio and trend_15m != "BEARISH":
            reason = f"🚀 Momentum UP (+{window_delta_pct}% vs open, Vol {volume_ratio}x) | 15M: {trend_15m}"
            return "UP", "MOMENTUM", reason

        if 32 <= rsi <= 47 and window_delta_pct <= -min_delta and volume_ratio >= min_vol_ratio and trend_15m != "BULLISH":
            reason = f"🚀 Momentum DOWN ({window_delta_pct}% vs open, Vol {volume_ratio}x) | 15M: {trend_15m}"
            return "DOWN", "MOMENTUM", reason

    # Pullback Venster: 1:00 tot 2:00 resterend (Geen overlap met Momentum)
    elif 60 <= time_remaining_sec < 120:
        if prev_rsi >= 78 and rsi < prev_rsi and window_delta_pct >= pullback_delta:
            reason = f"🔄 Piek omslag (RSI {prev_rsi} ➜ {rsi}) | 15M: {trend_15m}"
            return "DOWN", "PULLBACK", reason

        if prev_rsi <= 22 and rsi > prev_rsi and window_delta_pct <= -pullback_delta:
            reason = f"🔄 Bodem bounce (RSI {prev_rsi} ➜ {rsi}) | 15M: {trend_15m}"
            return "UP", "PULLBACK", reason

    return None, None, "Geen geschikt signaal"


def calculate_model_price(rsi, window_delta_pct, strat_type, signal):
    base_price = 0.50
    abs_move = abs(window_delta_pct)

    if strat_type == "MOMENTUM":
        rsi_factor = max(0, (rsi - 50) / 50) if signal == "UP" else max(0, (50 - rsi) / 50)
        move_factor = min(1.0, abs_move / 0.8)
        calculated = base_price + (rsi_factor * 0.18) + (move_factor * 0.20)
    else:
        rsi_extreme = abs(rsi - 50) / 50
        calculated = base_price + (rsi_extreme * 0.22)

    return round(max(0.52, min(0.88, calculated)), 2)


# ============================================================
# POLYMARKET EVENT & ORDERBOOK MATCHING
# ============================================================

def get_current_cycle_start():
    return int(time.time() // 300) * 300


def find_current_polymarket_event(symbol: str):
    prefix = POLY_SYMBOL_MAP.get(symbol)
    if not prefix:
        return None

    cycle_start = get_current_cycle_start()
    slug = f"{prefix}-updown-5m-{cycle_start}"

    event = request_json("GET", f"{POLYMARKET_GAMMA_URL}/events/slug/{slug}", timeout=8)
    if isinstance(event, dict) and event.get("markets"):
        return event

    return fallback_find_polymarket_event(symbol)


def fallback_find_polymarket_event(symbol: str):
    data = request_json(
        "GET",
        f"{POLYMARKET_GAMMA_URL}/events",
        params={"closed": "false", "limit": 100, "order": "startDate", "ascending": "false"},
        timeout=10
    )
    if not isinstance(data, list):
        return None

    keywords = COIN_KEYWORDS.get(symbol, [])
    for event in data:
        text = (str(event.get("title", "")) + " " + str(event.get("slug", ""))).lower()
        correct_coin = any(kw in text for kw in keywords)
        five_min = any(term in text for term in ["5m", "5-min", "5 min", "5-minute", "updown-5m"])

        if correct_coin and five_min:
            return event
    return None


def get_outcome_market_info(symbol: str, signal: str):
    event = find_current_polymarket_event(symbol)
    if not event:
        print(f"⚠️ Geen actieve Polymarket 5m-event gevonden voor {symbol}")
        return None

    event_slug = event.get("slug", "")
    markets = event.get("markets", [])
    wanted = signal.lower()

    for market in markets:
        if market.get("closed", False) or not market.get("active", True):
            continue

        outcomes = safe_json_load(market.get("outcomes"))
        token_ids = safe_json_load(market.get("clobTokenIds"))
        outcome_prices = safe_json_load(market.get("outcomePrices"))

        if not outcomes or not token_ids or len(outcomes) != len(token_ids):
            continue

        for index, outcome in enumerate(outcomes):
            normalized = str(outcome).strip().lower()
            match = (wanted == "up" and normalized in {"up", "yes"}) or \
                    (wanted == "down" and normalized in {"down", "no"})

            if not match:
                continue

            gamma_price = None
            if index < len(outcome_prices):
                try:
                    gamma_price = float(outcome_prices[index])
                except Exception:
                    pass

            return {
                "token_id": str(token_ids[index]),
                "outcome": str(outcome),
                "outcome_index": index,
                "gamma_price": gamma_price,
                "event_slug": event_slug,
                "url": f"https://polymarket.com/event/{event_slug}?outcomeIndex={index}",
            }
    return None


# ============================================================
# REAL-TIME CLOB ORDERBOEK KOERSEN
# ============================================================

def fetch_clob_price(token_id: str, side: str):
    data = request_json("GET", f"{CLOB_HOST}/price", params={"token_id": token_id, "side": side}, timeout=6)
    if isinstance(data, dict):
        try:
            return float(data["price"])
        except Exception:
            pass
    return None


def create_live_trade_preview(symbol: str, signal: str, amount_usd: float):
    market = get_outcome_market_info(symbol, signal)
    if not market:
        return None

    token_id = market["token_id"]
    best_bid = fetch_clob_price(token_id, "BUY")
    best_ask = fetch_clob_price(token_id, "SELL")

    if best_ask is None:
        best_ask = market.get("gamma_price")

    if best_ask is None or best_ask <= 0 or best_ask >= 1:
        print(f"⚠️ Geen geldige Polymarket ask-prijs voor {symbol} {signal}")
        return None

    shares = round(amount_usd / best_ask, 2)
    potential_profit = round(shares - amount_usd, 2)

    return {
        **market,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "amount": amount_usd,
        "shares": shares,
        "potential_profit": potential_profit,
    }


# ============================================================
# PAPER TRADE EVALUATIE
# ============================================================

def check_and_evaluate_paper_trades():
    global PAPER_BALANCE, active_paper_trades
    now_ts = time.time()
    remaining = []

    for trade in active_paper_trades:
        if now_ts < trade["expiry_time"]:
            remaining.append(trade)
            continue

        current_price, _, _, _, _, _ = fetch_binance_data(trade["symbol"])
        if current_price is None:
            remaining.append(trade)
            continue

        entry_strike = trade["entry_strike"]
        signal = trade["signal"]
        market_price = trade["market_price"]
        shares = trade["shares"]
        trade_amount = trade["trade_amount"]

        price_diff = round(current_price - entry_strike, 4)
        pct_diff = round((price_diff / entry_strike) * 100, 3)

        is_win = (signal == "UP" and current_price >= entry_strike) or \
                 (signal == "DOWN" and current_price < entry_strike)

        if is_win:
            profit = round(shares - trade_amount, 2)
            PAPER_BALANCE += profit
            badge = f"✅ **PAPER TRADE GEWONNEN** (+${profit:.2f})"
        else:
            PAPER_BALANCE -= trade_amount
            badge = f"❌ **PAPER TRADE VERLOREN** (-${trade_amount:.2f})"

        diff_str = f"+${price_diff} (+{pct_diff}%)" if price_diff > 0 else f"-${abs(price_diff)} ({pct_diff}%)"

        msg = (
            f"{badge}\n\n"
            f"📍 **Pair:** {trade['symbol']} ({signal})\n"
            f"🎯 **Type:** {trade['strat_type']}\n"
            f"🎯 **Binance 5M Open:** `${entry_strike}`\n"
            f"🏁 **Binance Eindkoers:** `${current_price}` ({diff_str})\n"
            f"📊 **Polymarket Instapkoers:** `${market_price:.3f}`\n"
            f"💵 **Simulatie Inzet:** `${trade_amount:.2f}`\n"
            f"💰 **Nieuw Paper Saldo:** `${PAPER_BALANCE:.2f}`\n\n"
            f"ℹ️ *Paper-resultaat gebruikt Binance als afwikkelingsproxy.*"
        )
        send_telegram_msg(msg, trade.get("url"))

    active_paper_trades = remaining


def cleanup_old_signals(current_cycle: int):
    old_keys = [
        k for k in active_signals_sent 
        if int(k.split("_")[1]) < current_cycle - 3
    ]
    for k in old_keys:
        active_signals_sent.pop(k, None)


# ============================================================
# MAIN BOT LOOP
# ============================================================

def main():
    print("\n" + "=" * 60)
    print("🤖 POLYMARKET 5M SIGNAL & PAPER TRADING BOT V2")
    print("=" * 60)
    print(f"Pairs: {', '.join(PAIRS)}")
    print(f"Scan Interval: {SCAN_INTERVAL_SEC}s")
    print(f"Max Signals/Cycle: {MAX_TRADES_PER_CYCLE}")
    print(f"Max Allowable Odds (Ask): {MAX_ALLOWABLE_ODDS}")
    print(f"CLOB Status: {'✅ Geauthenticeerd' if clob_client else '❌ Publieke Modus'}")
    print("=" * 60 + "\n")

    mode_msg = "🟠 **LIVE SIGNAAL + HANDMATIGE ORDER MODUS**" if REQUESTED_LIVE_MODE else "📝 **PAPER TRADING MODUS**"

    send_telegram_msg(
        "🤖 **Poly Bot V2 Online**\n\n"
        f"⚙️ Status: {mode_msg}\n"
        f"💰 Paper Saldo: `${PAPER_BALANCE:.2f}`\n"
        f"📊 Max Ask Limiet: `${MAX_ALLOWABLE_ODDS:.2f}`"
    )

    while True:
        try:
            now_sec = time.time()
            time_remaining_sec = 300 - (now_sec % 300)
            cycle_id = int(now_sec // 300)

            cleanup_old_signals(cycle_id)
            check_and_evaluate_paper_trades()

            current_cycle_count = sum(1 for k in active_signals_sent if k.startswith(f"cycle_{cycle_id}_"))

            if current_cycle_count < MAX_TRADES_PER_CYCLE:
                for symbol in PAIRS:
                    if current_cycle_count >= MAX_TRADES_PER_CYCLE:
                        break

                    current_price, strike, delta_pct, rsi, prev_rsi, vol_ratio = fetch_binance_data(symbol)
                    if current_price is None:
                        continue

                    signal, strat_type, reason = analyze_market_and_get_signal(
                        symbol, rsi, prev_rsi, delta_pct, vol_ratio, time_remaining_sec
                    )
                    if not signal:
                        continue

                    trade_key = f"cycle_{cycle_id}_{symbol}_{strat_type}"
                    if trade_key in active_signals_sent:
                        continue

                    model_price = calculate_model_price(rsi, delta_pct, strat_type, signal)
                    amount = LIVE_TRADE_AMOUNT if REQUESTED_LIVE_MODE else PAPER_TRADE_AMOUNT

                    preview = create_live_trade_preview(symbol, signal, amount)
                    if not preview:
                        continue

                    actual_ask = preview["best_ask"]
                    if actual_ask > MAX_ALLOWABLE_ODDS:
                        print(f"⛔ {symbol} {signal} geweigerd | Ask {actual_ask:.3f} > Max Limiet {MAX_ALLOWABLE_ODDS:.3f}")
                        continue

                    active_signals_sent[trade_key] = True
                    current_cycle_count += 1

                    expiry_time = now_sec + time_remaining_sec
                    active_paper_trades.append({
                        "symbol": symbol,
                        "signal": signal,
                        "strat_type": strat_type,
                        "entry_price": current_price,
                        "entry_strike": strike,
                        "market_price": actual_ask,
                        "shares": preview["shares"],
                        "trade_amount": amount,
                        "expiry_time": expiry_time,
                        "url": preview["url"],
                    })

                    mins, secs = int(time_remaining_sec // 60), int(time_remaining_sec % 60)
                    dir_icon = "📈" if signal == "UP" else "📉"
                    target = f"Hoger/gelijk dan ${strike}" if signal == "UP" else f"Lager dan ${strike}"
                    type_badge = "🔥 MOMENTUM" if strat_type == "MOMENTUM" else "🔄 PULLBACK"
                    status = "🟠 **LIVE SIGNAAL (KLIK OM TE TRADEN)**" if REQUESTED_LIVE_MODE else "📝 **PAPER TRADE**"
                    
                    best_bid = preview.get("best_bid")
                    spread_text = f"`{best_bid:.3f}`/`{actual_ask:.3f}`" if best_bid else f"?/`{actual_ask:.3f}`"

                    msg = (
                        f"🚨 **5M {type_badge}** {dir_icon}\n"
                        f"{status}\n\n"
                        f"📍 **Pair:** {symbol}\n"
                        f"🎯 **Binance 5M Open:** `${strike}`\n"
                        f"📍 **Huidige Koers:** `${current_price}` ({delta_pct:+.3f}%)\n"
                        f"👉 **Richting:** **{signal}** ({target})\n\n"
                        f"🎲 **Outcome:** `{preview['outcome']}`\n"
                        f"📊 **Bid/Ask Spread:** {spread_text}\n"
                        f"🤖 **Model Prob:** `{model_price:.2f}`\n"
                        f"💵 **Inzet:** `${amount:.2f}`\n"
                        f"🪙 **Indicatieve Shares:** `{preview['shares']:.2f}`\n"
                        f"💵 **Potentiële Winst:** `+${preview['potential_profit']:.2f}`\n\n"
                        f"📊 **RSI:** `{rsi}` | **Vol Ratio:** `{vol_ratio}x` | **Delta:** `{delta_pct:+.3f}%`\n"
                        f"⏱️ **Resterende Tijd:** `{mins}m {secs}s`\n\n"
                        f"💡 **Analyse:**\n{reason}\n\n"
                        f"🔑 Token ID:\n`{preview['token_id']}`"
                    )

                    send_telegram_msg(msg, preview["url"])
                    print(f"🚨 {symbol} {signal} | Ask={actual_ask:.3f} | Model={model_price:.2f} | Delta={delta_pct:+.3f}%")

        except KeyboardInterrupt:
            print("\n🛑 Bot handmatig gestopt.")
            break
        except Exception as e:
            print(f"\n❌ Fout in hoofd-loop: {type(e).__name__}: {e}")

        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
