import os
import time
import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE
# ---------------------------------------------------------------------------
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8945207926:AAEe0V3AYAdKqyT732B1TWIYcEBz8Ps9E_8")
TELEGRAM_BOT_TOKEN = RAW_TOKEN.strip().replace("bot", "") if RAW_TOKEN.strip().startswith("bot") else RAW_TOKEN.strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "369244706").strip()

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
DOME_CHAINLINK_URL = "https://api.domeapi.io/v1/crypto-prices/chainlink"
POLYMARKET_BTC_URL = "https://polymarket.com/market/bitcoin-5-min"

LAST_SIGNAL = None

# ---------------------------------------------------------------------------
# TELEGRAM FUNCTIES
# ---------------------------------------------------------------------------
def send_telegram_alert(message, target_url=POLYMARKET_BTC_URL):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "🚀 ZET DIRECT IN OP POLYMARKET",
                    "url": target_url
                }
            ]
        ]
    }
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"Telegram Fout: {e}")

# ---------------------------------------------------------------------------
# DATA OPHALEN: BINANCE & CHAINLINK
# ---------------------------------------------------------------------------
def fetch_binance_klines(interval="5m", limit=60):
    """Haalt snelle candles op van Binance voor technische indicatoren"""
    params = {"symbol": "BTCUSDT", "interval": interval, "limit": limit}
    try:
        res = requests.get(BINANCE_KLINES_URL, params=params, timeout=5)
        if res.status_code == 200:
            data = res.json()
            closes = [float(k[4]) for k in data]
            return closes
    except Exception as e:
        print(f"Fout bij ophalen Binance data: {e}")
    return []

def fetch_chainlink_price():
    """Haalt de Chainlink Oracle prijs op (het officiële Polymarket settlement-commando)"""
    params = {"currency": "btc/usd"}
    try:
        res = requests.get(DOME_CHAINLINK_URL, params=params, timeout=5)
        if res.status_code == 200:
            prices = res.json().get("prices", [])
            if prices:
                return float(prices[0].get("value", 0))
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# TECHNISCHE INDICATOREN
# ---------------------------------------------------------------------------
def calculate_ema(prices, period):
    if len(prices) < period:
        return 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(abs(change))
            losses.append(abs(change))
            
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------------------------------------------------------------------
# CRYPTO SCANNER LOGICA
# ---------------------------------------------------------------------------
def check_crypto_confluence():
    global LAST_SIGNAL
    
    # 1. Binance Technische Data (EMA & RSI)
    closes_5m = fetch_binance_klines(interval="5m", limit=60)
    if not closes_5m:
        return

    binance_price = closes_5m[-1]
    ema_5 = calculate_ema(closes_5m, 5)
    ema_15 = calculate_ema(closes_5m, 15)
    rsi = calculate_rsi(closes_5m, 14)

    closes_15m = fetch_binance_klines(interval="15m", limit=60)
    ema_50_15m = calculate_ema(closes_15m, 50) if closes_15m else binance_price

    # 2. Chainlink Oracle Prijs (Polymarket Validatie)
    chainlink_price = fetch_chainlink_price()
    active_price = chainlink_price if chainlink_price else binance_price
    price_source = "Chainlink Oracle" if chainlink_price else "Binance Feed"

    # Tijd in de huidige 5-minuten cyclus berekenen
    current_time_sec = time.time()
    seconds_in_5m = 300
    time_remaining_min = round((seconds_in_5m - (current_time_sec % seconds_in_5m)) / 60, 1)

    # Signal Condities
    bullish_confluence = (ema_5 > ema_15) and (rsi > 60) and (active_price > ema_50_15m)
    bearish_confluence = (ema_5 < ema_15) and (rsi < 40) and (active_price < ema_50_15m)

    if bearish_confluence and LAST_SIGNAL != "DOWN":
        LAST_SIGNAL = "DOWN"
        msg = (
            f"🚨 **HIGH CONFIDENCE BREAKDOWN (DOWN)** 📉\n\n"
            f"**Pair:** BTC/USDT\n"
            f"**Live Prijs:** ${active_price:,.2f} (*{price_source}*)\n"
            f"**Tijd over in ronde:** ~{time_remaining_min} minuten\n\n"
            f"**Confluentie Check (3/3):**\n"
            f"✅ **5M EMA Crossover:** EMA 5 (${ema_5:,.2f}) < EMA 15 (${ema_15:,.2f})\n"
            f"✅ **RSI (14):** {rsi:.1f} (Zwak momentum)\n"
            f"✅ **15M Trend:** Onder EMA 50 (${ema_50_15m:,.2f})\n\n"
            f"💡 **Actie:** Zet €5.00 of €10.00 in op **'DOWN'**"
        )
        send_telegram_alert(msg)

    elif bullish_confluence and LAST_SIGNAL != "UP":
        LAST_SIGNAL = "UP"
        msg = (
            f"🚀 **HIGH CONFIDENCE BREAKOUT (UP)** 📈\n\n"
            f"**Pair:** BTC/USDT\n"
            f"**Live Prijs:** ${active_price:,.2f} (*{price_source}*)\n"
            f"**Tijd over in ronde:** ~{time_remaining_min} minuten\n\n"
            f"**Confluentie Check (3/3):**\n"
            f"✅ **5M EMA Crossover:** EMA 5 (${ema_5:,.2f}) > EMA 15 (${ema_15:,.2f})\n"
            f"✅ **RSI (14):** {rsi:.1f} (Sterk momentum)\n"
            f"✅ **15M Trend:** Boven EMA 50 (${ema_50_15m:,.2f})\n\n"
            f"💡 **Actie:** Zet €5.00 of €10.00 in op **'UP'**"
        )
        send_telegram_alert(msg)

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🤖 Dual-Source (Binance + Chainlink) Crypto Bot gestart!")
    send_telegram_alert("🤖 **Crypto 5M Confluentie Bot is actief (Binance + Chainlink)...**")
    
    while True:
        try:
            check_crypto_confluence()
            time.sleep(20)  # Scant elke 20 seconden voor snelle updates
        except Exception as e:
            print(f"Fout in main loop: {e}")
            time.sleep(10)
