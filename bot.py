import os
import time
import json
import requests

# —————————————————————————
# CONFIGURATIE & ENVIRONMENT VARIABELEN
# —————————————————————————

RAW_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN.replace('bot', '', 1) if RAW_TOKEN.startswith('bot') else RAW_TOKEN
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()

BINANCE_API_URL = "https://api.binance.com/api/v3"
SCAN_INTERVAL_SEC = 10  # Hoe vaak de bot de markt scant (in seconden)

# Te scannen handelsparen
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ZECUSDT"]

# Status van geopende trades om dubbele signalen te voorkomen
active_trades = {}

# —————————————————————————
# TELEGRAM BERICHTEN STUREN
# —————————————————————————

def send_telegram_msg(message, button_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram token of Chat ID ontbreekt.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if button_url:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "🚀 ZET DIRECT IN OP POLYMARKET", "url": button_url}]]}

    try:
        res = requests.post(url, json=payload, timeout=8).json()
        if not res.get("ok"):
            print(f"❌ Telegram API Fout: {res}")
        else:
            print("✅ Telegram signaal verzonden!")
    except Exception as e:
        print(f"❌ Fout bij versturen Telegram: {e}")

# —————————————————————————
# RSI BEREKENING LOGICA
# —————————————————————————

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

# —————————————————————————
# DUBBELE STRATEGIE MARKTANALYSE
# —————————————————————————

def analyze_market_and_get_signal(symbol, rsi, percent_change, time_remaining_sec):
    """
    Analyseert de markt met de gecombineerde strategie:
    - Minimale tijd op klok: 3 min 30 sec (210s) tot 5 min (300s)
    - Stap 1: Gezond Momentum (RSI 52-68 / 32-48)
    - Stap 2: Pullback / Mean Reversion (RSI >= 75 / <= 25)
    """
    
    # 0. Tijdsfilter: Alleen instappen als er ruim genoeg tijd is (3:30 tot 5:00 min)
    if time_remaining_sec < 210 or time_remaining_sec > 300:
        return None, None, "Tijd niet in scope (enkel 3:30m - 5:00m te gaan)"

    # -------------------------------------------------------------
    # STAP 1: GEZOND MOMENTUM (Vroeg in de trend instappen)
    # -------------------------------------------------------------
    if 52 <= rsi <= 68 and percent_change >= 0.25:
        reason = "🎯 Stap 1: Gezond Bullish Momentum (Beginnende stijging)"
        return "UP", "MOMENTUM", reason

    elif 32 <= rsi <= 48 and percent_change <= -0.25:
        reason = "🎯 Stap 1: Gezond Bearish Momentum (Beginnende daling)"
        return "DOWN", "MOMENTUM", reason

    # -------------------------------------------------------------
    # STAP 2: PULLBACK / REVERSION (Extremen traden op de top/dal)
    # -------------------------------------------------------------
    elif rsi >= 75 and percent_change >= 0.20:
        reason = "🔄 Stap 2: Overbought Piek (RSI >= 75) -> Verwachte Pullback DOWN"
        return "DOWN", "PULLBACK", reason

    elif rsi <= 25 and percent_change <= -0.20:
        reason = "🔄 Stap 2: Oversold Dip (RSI <= 25) -> Verwachte Bounce UP"
        return "UP", "PULLBACK", reason

    return None, None, "Geen sterk signaal gedetecteerd"

# —————————————————————————
# BINANCE DATA OPHALEN
# —————————————————————————

def fetch_binance_data(symbol):
    try:
        # Haal 1m klines op voor RSI en prijsverandering
        url = f"{BINANCE_API_URL}/klines?symbol={symbol}&interval=1m&limit=30"
        res = requests.get(url, timeout=5).json()

        close_prices = [float(k[4]) for k in res]
        current_price = close_prices[-1]
        start_price = close_prices[-5]  # Prijs van 5 minuten geleden

        percent_change = round(((current_price - start_price) / start_price) * 100, 2)
        rsi = calculate_rsi(close_prices)

        return current_price, percent_change, rsi
    except Exception as e:
        print(f"Fout bij ophalen data voor {symbol}: {e}")
        return None, None, None

# —————————————————————————
# BERICHT OPBOUW & TELEGRAM TRIGGER
# —————————————————————————

def format_signal_message(symbol, signal, strat_type, current_price, percent_change, rsi, time_remaining_sec, reason):
    mins = int(time_remaining_sec // 60)
    secs = int(time_remaining_sec % 60)
    time_str = f"~{mins}m {secs}s"

    direction_icon = "📈" if signal == "UP" else "📉"
    type_badge = "🔥 MOMENTUM SIGNAAL" if strat_type == "MOMENTUM" else "🔄 PULLBACK / REVERSION SIGNAAL"

    msg = (
        f"🚨 **5M EXPIRY {type_badge}!** {direction_icon}\n\n"
        f"📍 **Pair:** {symbol}\n"
        f"📊 **RSI:** `{rsi}` | **3-5M Move:** `{percent_change}%`\n"
        f"💵 **Huidige Koers:** `${current_price}`\n"
        f"⏱️ **Resterende Tijd:** `{time_str}`\n\n"
        f"💡 **Strategie Advies:**\n"
        f"• {reason}\n"
        f"👉 **Richting op Polymarket:** **{signal}**"
    )
    return msg

# —————————————————————————
# MAIN BOT LOOP
# —————————————————————————

def main():
    print("🤖 Nobeulele's Poly Bot (Dubbele Strategie: Momentum + Pullback) Gestart...")
    send_telegram_msg("🤖 **Poly Bot Online**\nGestart met Momentum + Pullback strategie op de 3:30-5:00M timeframes!")

    while True:
        try:
            # Bereken waar we zitten in de 5-minuten cyclus
            now_sec = time.time()
            time_in_5m_cycle = now_sec % 300  # 300 sec = 5 min
            time_remaining_sec = 300 - time_in_5m_cycle

            for symbol in PAIRS:
                price, pct_change, rsi = fetch_binance_data(symbol)

                if price is None:
                    continue

                signal, strat_type, reason = analyze_market_and_get_signal(
                    symbol, rsi, pct_change, time_remaining_sec
                )

                if signal:
                    # Voorkom dat hetzelfde signaal dubbel wordt gestuurd binnen dezelfde ronde
                    cycle_id = int(now_sec // 300)
                    trade_key = f"{symbol}_{cycle_id}"

                    if trade_key not in active_trades:
                        active_trades[trade_key] = True

                        polymarket_url = "https://polymarket.com"
                        msg = format_signal_message(
                            symbol, signal, strat_type, price, pct_change, rsi, time_remaining_sec, reason
                        )
                        
                        send_telegram_msg(msg, button_url=polymarket_url)
                        print(f"🚀 Signaal verzonden voor {symbol}: {signal} ({strat_type})")

            # Oude trades opschonen uit geheugen
            if len(active_trades) > 100:
                active_trades.clear()

        except Exception as e:
            print(f"Error in main trading loop: {e}")

        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
