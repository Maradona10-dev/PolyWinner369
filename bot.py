import time
import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8840031344:AAGDJmeIC6yJPSUazE72tn32qZcFsdDvXP0"
TELEGRAM_CHAT_ID = "369244706"

# Nu inclusief SOL, BNB en TRX
TRACKED_CRYPTO = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "TRXUSDT"]

# Directe Polymarket URLs per coin
POLYMARKET_URLS = {
    "BTC": "https://polymarket.com/event/bitcoin-price-5-min",
    "ETH": "https://polymarket.com/event/ethereum-price-5-min",
    "SOL": "https://polymarket.com/event/solana-price-5-min",
    "BNB": "https://polymarket.com/event/binance-coin-price-5-min",
    "TRX": "https://polymarket.com/event/tron-price-5-min"
}

# ---------------------------------------------------------------------------
# TELEGRAM NOTIFICATIE FUNCTIE
# ---------------------------------------------------------------------------
def send_telegram_alert(message, target_url="https://polymarket.com"):
    """Stuurt een pushmelding inclusief directe actie-knop naar Telegram"""
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
        "disable_web_page_preview": False,
        "reply_markup": reply_markup
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Fout bij versturen van Telegram bericht: {e}")

# ---------------------------------------------------------------------------
# LIVE CRYPTO METRICS VIA BINANCE API
# ---------------------------------------------------------------------------
def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1]
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def check_live_crypto_signals():
    """Haalt live 5-minuten kline data op van Binance API"""
    print("Scannen van live Binance Crypto API data...")
    
    for symbol in TRACKED_CRYPTO:
        try:
            url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=20"
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                closes = [float(candle[4]) for candle in data]
                
                ema_5 = calculate_ema(closes, 5)
                ema_15 = calculate_ema(closes, 15)
                
                prev_closes = closes[:-1]
                prev_ema_5 = calculate_ema(prev_closes, 5)
                prev_ema_15 = calculate_ema(prev_closes, 15)
                
                current_price = closes[-1]
                coin_name = symbol.replace("USDT", "")
                
                # Bepaal de specifieke link voor deze specifieke coin
                market_url = POLYMARKET_URLS.get(coin_name, "https://polymarket.com/crypto")
                
                # 1. Bullish Crossover (UP signal)
                if ema_5 > ema_15 and prev_ema_5 <= prev_ema_15:
                    alert_msg = (
                        f"🚨 **CRYPTO 5M BREAKOUT (UP)** 📈\n\n"
                        f"**Pair:** {coin_name}/USDT (5-Min Window)\n"
                        f"**Live Prijs:** ${current_price:,.4f}\n"
                        f"**EMA 5:** ${ema_5:,.4f} | **EMA 15:** ${ema_15:,.4f}\n\n"
                        f"💡 **Actie:** Zet **€5.00** of **€10.00** in op **'UP'** (5 min)"
                    )
                    send_telegram_alert(alert_msg, target_url=market_url)
                
                # 2. Bearish Crossover (DOWN signal)
                elif ema_5 < ema_15 and prev_ema_5 >= prev_ema_15:
                    alert_msg = (
                        f"🚨 **CRYPTO 5M BREAKDOWN (DOWN)** 📉\n\n"
                        f"**Pair:** {coin_name}/USDT (5-Min Window)\n"
                        f"**Live Prijs:** ${current_price:,.4f}\n"
                        f"**EMA 5:** ${ema_5:,.4f} | **EMA 15:** ${ema_15:,.4f}\n\n"
                        f"💡 **Actie:** Zet **€5.00** of **€10.00** in op **'DOWN'** (5 min)"
                    )
                    send_telegram_alert(alert_msg, target_url=market_url)
                    
        except Exception as e:
            print(f"Fout bij ophalen Binance data voor {symbol}: {e}")

# ---------------------------------------------------------------------------
# LIVE POLYMARKET MARKTSCANNER
# ---------------------------------------------------------------------------
def check_live_polymarket_events():
    """Haalt actieve live markten op via de openbare Polymarket Gamma API"""
    print("Scannen van live Polymarket markten...")
    try:
        url = "https://gamma-api.polymarket.com/events?closed=false&limit=10"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            events = res.json()
            print(f"Succesvol {len(events)} live Polymarket events opgehaald.")
    except Exception as e:
        print(f"Fout bij ophalen Polymarket API: {e}")

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🤖 Polymarket Live Scanner is gestart!")
    
    send_telegram_alert(
        "⚡ **Polymarket Bot is Geüpdatet!**\n\n"
        "Gemonitorde coins: **BTC, ETH, SOL, BNB en TRX**.\n"
        "Elke melding linkt nu direct naar de specifieke coin-pagina.",
        target_url="https://polymarket.com/crypto"
    )
    
    try:
        while True:
            check_live_crypto_signals()
            check_live_polymarket_events()
            time.sleep(120)
    except KeyboardInterrupt:
        send_telegram_alert("🔴 **Polymarket Bot is gestopt.**")
