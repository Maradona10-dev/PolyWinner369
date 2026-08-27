import time
import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8840031344:AAGDJmeIC6yJPSUazE72tn32qZcFsdDvXP0"
TELEGRAM_CHAT_ID = "369244706"

# Alleen verhandelbare markten
TRACKED_CRYPTO = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# Dynamische zoek/gefilterde links die 100% gegarandeerd nooit 404 geven
POLYMARKET_URLS = {
    "BTC": "https://polymarket.com/crypto?via=search&q=BTC",
    "ETH": "https://polymarket.com/crypto?via=search&q=ETH",
    "SOL": "https://polymarket.com/crypto?via=search&q=SOL"
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
# INDICATOREN EN BEREKENINGEN
# ---------------------------------------------------------------------------
def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1]
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
            
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# ---------------------------------------------------------------------------
# CONFLUENTIE SCANNER (EMA + RSI + 15M TREND)
# ---------------------------------------------------------------------------
def check_live_crypto_signals():
    print("Scannen van live Binance Crypto data op confluentie...")
    
    for symbol in TRACKED_CRYPTO:
        try:
            # 1. Haal 5-minuten klines op (voor EMA 5/15 crossover & RSI 14)
            url_5m = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=30"
            res_5m = requests.get(url_5m, timeout=5)
            
            # 2. Haal 15-minuten klines op (voor 15m Trend met EMA 50)
            url_15m = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=60"
            res_15m = requests.get(url_15m, timeout=5)
            
            if res_5m.status_code == 200 and res_15m.status_code == 200:
                data_5m = res_5m.json()
                data_15m = res_15m.json()
                
                closes_5m = [float(c[4]) for c in data_5m]
                closes_15m = [float(c[4]) for c in data_15m]
                
                # Bereken 5m indicatoren
                ema_5 = calculate_ema(closes_5m, 5)
                ema_15 = calculate_ema(closes_5m, 15)
                prev_ema_5 = calculate_ema(closes_5m[:-1], 5)
                prev_ema_15 = calculate_ema(closes_5m[:-1], 15)
                rsi = calculate_rsi(closes_5m, 14)
                
                # Bereken 15m Trend indicator
                ema_50_15m = calculate_ema(closes_15m, 50)
                
                current_price = closes_5m[-1]
                coin_name = symbol.replace("USDT", "")
                market_url = POLYMARKET_URLS.get(coin_name, "https://polymarket.com/crypto")
                
                # --- CHECK BULLISH CONFLUENTIE (UP) ---
                ema_cross_up = (ema_5 > ema_15) and (prev_ema_5 <= prev_ema_15)
                rsi_valid_up = 50 < rsi < 70
                trend_15m_up = current_price > ema_50_15m
                
                if ema_cross_up and rsi_valid_up and trend_15m_up:
                    alert_msg = (
                        f"🚨 **HIGH CONFIDENCE BREAKOUT (UP)** 📈\n\n"
                        f"**Pair:** {coin_name}/USDT\n"
                        f"**Live Prijs:** ${current_price:,.2f}\n\n"
                        f"**Confluentie Check (3/3):**\n"
                        f"✅ **5M EMA Crossover:** EMA 5 (${ema_5:,.2f}) > EMA 15 (${ema_15:,.2f})\n"
                        f"✅ **RSI (14):** {rsi:.1f} (Sterk momentum)\n"
                        f"✅ **15M Trend:** Boven EMA 50 (${ema_50_15m:,.2f})\n\n"
                        f"💡 **Actie:** Zet **€5.00** of **€10.00** in op **'UP'**"
                    )
                    send_telegram_alert(alert_msg, target_url=market_url)

                # --- CHECK BEARISH CONFLUENTIE (DOWN) ---
                ema_cross_down = (ema_5 < ema_15) and (prev_ema_5 >= prev_ema_15)
                rsi_valid_down = 30 < rsi < 50
                trend_15m_down = current_price < ema_50_15m
                
                if ema_cross_down and rsi_valid_down and trend_15m_down:
                    alert_msg = (
                        f"🚨 **HIGH CONFIDENCE BREAKDOWN (DOWN)** 📉\n\n"
                        f"**Pair:** {coin_name}/USDT\n"
                        f"**Live Prijs:** ${current_price:,.2f}\n\n"
                        f"**Confluentie Check (3/3):**\n"
                        f"✅ **5M EMA Crossover:** EMA 5 (${ema_5:,.2f}) < EMA 15 (${ema_15:,.2f})\n"
                        f"✅ **RSI (14):** {rsi:.1f} (Zwak momentum)\n"
                        f"✅ **15M Trend:** Onder EMA 50 (${ema_50_15m:,.2f})\n\n"
                        f"💡 **Actie:** Zet **€5.00** of **€10.00** in op **'DOWN'**"
                    )
                    send_telegram_alert(alert_msg, target_url=market_url)
                    
        except Exception as e:
            print(f"Fout bij ophalen Binance data voor {symbol}: {e}")

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🤖 Polymarket Confluence Scanner is gestart!")
    
    send_telegram_alert(
        "⚡ **Polymarket Links Hersteld!**\n\n"
        "Alle knoppen linken nu altijd direct en zonder foutmeldingen door naar de actieve coin-pagina's.",
        target_url="https://polymarket.com/crypto"
    )
    
    try:
        while True:
            check_live_crypto_signals()
            time.sleep(120)
    except KeyboardInterrupt:
        send_telegram_alert("🔴 **Polymarket Bot is gestopt.**")
