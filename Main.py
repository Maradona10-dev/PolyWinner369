import os
import ccxt
import time
import pandas as pd
from strategy import SMCStrategy
from telegram_notifier import send_telegram_message

# Lees configuratie uit Railway Environment Variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Initialiseer BloFin exchange via CCXT
exchange = ccxt.blofin({
    'enableRateLimit': True,
    'apiKey': os.getenv('BLOFIN_API_KEY'),
    'secret': os.getenv('BLOFIN_SECRET'),
    'password': os.getenv('BLOFIN_PASSWORD'),
})

SYMBOL = 'BTC/USDT:USDT'

def fetch_data():
    # Haal 1-uur data op (Stap 2)
    ohlcv_1h = exchange.fetch_ohlcv(SYMBOL, timeframe='1h', limit=50)
    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    # Haal 1-minuut data op (Stap 3 & 4)
    ohlcv_1m = exchange.fetch_ohlcv(SYMBOL, timeframe='1m', limit=50)
    df_1m = pd.DataFrame(ohlcv_1m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    return df_1h, df_1m

def run_bot():
    start_msg = f"🚀 BloFin Trading Bot is gestart voor {SYMBOL}!"
    print(start_msg)
    send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, start_msg)
    
    while True:
        try:
            df_1h, df_1m = fetch_data()
            strategy = SMCStrategy(df_1h, df_1m)
            signal = strategy.evaluate_signal()

            print(f"{pd.Timestamp.now()} - Huidig signaal: {signal}")

            if signal == "SELL":
                alert_msg = f"🚨 *Signaal gedetecteerd!*\nSymbool: {SYMBOL}\nActie: SELL (Confirmation Model bevestigd)"
                print(alert_msg)
                send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, alert_msg)
                
                # Zodra je klaar bent om live orders te plaatsen, kun je dit ontkoppelen:
                # order = exchange.create_market_sell_order(SYMBOL, 0.001)
            
            # Wacht 60 seconden tot de volgende check
            time.sleep(60)

        except Exception as e:
            err_msg = f"❌ Fout opgetreden in Trading Bot: {e}"
            print(err_msg)
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, err_msg)
            time.sleep(10)

if __name__ == "__main__":
    run_bot()
