import ccxt
import time
import pandas as pd
from strategy import SMCStrategy

# Initialiseer de exchange (bijv. Binance Testnet / Live)
exchange = ccxt.binance({
    'enableRateLimit': True,
    # 'apiKey': 'JOUW_API_KEY',
    # 'secret': 'JOUW_SECRET_KEY',
})

SYMBOL = 'BTC/USDT'

def fetch_data():
    # Haal 1-uur data op (Stap 2)
    ohlcv_1h = exchange.fetch_ohlcv(SYMBOL, timeframe='1h', limit=50)
    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    # Haal 1-minuut data op (Stap 3 & 4)
    ohlcv_1m = exchange.fetch_ohlcv(SYMBOL, timeframe='1m', limit=50)
    df_1m = pd.DataFrame(ohlcv_1m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    return df_1h, df_1m

def run_bot():
       print(f"Start trading bot voor {SYMBOL} op basis van het Confirmation Model...")
       
       while True:
           try:
               df_1h, df_1m = fetch_data()
               strategy = SMCStrategy(df_1h, df_1m)
               signal = strategy.evaluate_signal()

               print( ഉരുformat(pd.Timestamp.now()), f"- Huidig signaal: {signal}")

               if signal == "SELL":
                   print("🚨 Alle 4 stappen bevestigd! Order wordt geplaatst...")
                   # Voorbeeld order logica (pas op met live trading!):
                   # order = exchange.create_market_sell_order(SYMBOL, 0.001)
                   # print(order)
               
               # Wacht 60 seconden voordat de volgende 1m bar wordt gecheckt
               time.sleep(60)

           except Exception as e:
               print(f"Fout opgetreden: {e}")
               time.sleep(10)

if __name__ == "__main__":
       run_bot()
