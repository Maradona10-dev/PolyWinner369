import ccxt
import time
import os

# Verbinding maken met Bybit Spot via CCXT
exchange = ccxt.bybit({
    'apiKey': os.environ.get('EXCHANGE_API_KEY', ''),
    'secret': os.environ.get('EXCHANGE_SECRET', ''),
    'enableRateLimit': True,
})

SYMBOL = 'BTC/USDT'
TIMEFRAME = '5m'
TRADE_AMOUNT_USD = 5.0  # Ingesteld op 5 USDT per trade
MIN_BREAKOUT_PCT = 0.05

def run_momentum_bot():
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=2)
        if len(ohlcv) < 2:
            return
        
        candle = ohlcv[-1]
        open_price = float(candle[1])
        close_price = float(candle[4])
        
        if open_price == 0:
            return

        pct_change = ((close_price - open_price) / open_price) * 100
        
        ticker = exchange.fetch_ticker(SYMBOL)
        current_price = ticker['last']
        quantity = round(TRADE_AMOUNT_USD / current_price, 6)

        if pct_change >= MIN_BREAKOUT_PCT:
            print(f"🚀 Bullish momentum ({pct_change:.2f}%). Spot BTC kopen...")
            order = exchange.create_market_buy_order(SYMBOL, quantity)
            print(f"Order succesvol geplaatst! ID: {order.get('id')}")
            
        elif pct_change <= -MIN_BREAKOUT_PCT:
            print(f"🔻 Bearish momentum ({pct_change:.2f}%). Spot BTC verkopen...")
            order = exchange.create_market_sell_order(SYMBOL, quantity)
            print(f"Order succesvol geplaatst! ID: {order.get('id')}")
        else:
            print(f"Geen sterke beweging ({pct_change:.2f}%). Wachten...")

    except Exception as e:
        print(f"⚠️ Fout in bot cyclus: {e}")

if __name__ == "__main__":
    print("🤖 Crypto Spot Momentum Bot gestart (Bybit EU Mode)")
    exchange.load_markets()
    
    while True:
        run_momentum_bot()
        time.sleep(300)
