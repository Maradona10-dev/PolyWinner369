import time
import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8840031344:AAGDJmeIC6yJPSUazE72tn32qZcFsdDvXP0"
TELEGRAM_CHAT_ID = "369244706"

TRACKED_CRYPTO = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
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
            # Binance klines endpoint for 5m interval
            url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=20"
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                closes = [float(candle[4]) for candle in data]
                
                ema_5 = calculate_ema(closes, 5)
                ema_15 = calculate_ema(closes, 15)
                current_price = closes[-1]
                
                # Crossover check (EMA 5 kruist boven EMA 15)
                if ema_5 > ema_15 and closes[-2] <= calculate_ema(closes[:-1], 15):
                    coin_name = symbol.replace("USDT", "")
                    alert_msg = (
                        f"🚨 **LIVE CRYPTO 5M BREAKOUT DETECTED** 📈\n\n"
                        f"**Pair:** {coin_name}/USDT (5-Min Window)\n"
                        f"**Live Prijs:** ${current_price:,.2f}\n"
                        f"**EMA 5:** ${ema_5:,.2f} | **EMA 15:** ${ema_15:,.2f}\n\n"
                        f"💰 **Status:** Bullish EMA Crossover opgetreden!\n"
                        f"💡 **Actie:** Check de actieve {coin_name} markt op Polymarket."
                    )
                    send_telegram_alert(alert_msg, target_url=f"https://polymarket.com/crypto")
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
            # De bot scant hier de live geopende markten
            print(f"Succesvol {len(events)} live Polymarket events opgehaald.")
    except Exception as e:
        print(f"Fout bij ophalen Polymarket API: {e}")

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🤖 Polymarket Live Scanner is gestart!")
    
    send_telegram_alert(
        "⚡ **Polymarket Bot staat nu op LIVE DATA!**\n\n"
        "• Nepberichten zijn uitgeschakeld.\n"
        "• Scant continu live prijzen op Binance & Polymarket.\n"
        "• Meldingen verschijnen alleen bij echte markttriggers met een inzetknop.",
        target_url="https://polymarket.com"
    )
    
    try:
        while True:
            check_live_crypto_signals()
            check_live_polymarket_events()
            time.sleep(120)  # Scant elke 2 minuten
    except KeyboardInterrupt:
        send_telegram_alert("🔴 **Polymarket Bot is gestopt.**")
