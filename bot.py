import ccxt
import time
import os

# Verbinding maken met BloFin via CCXT
exchange = ccxt.blofin({
    'apiKey': os.environ.get('EXCHANGE_API_KEY', ''),
    'secret': os.environ.get('EXCHANGE_SECRET', ''),
    'password': os.environ.get('EXCHANGE_PASSWORD', ''),
    'enableRateLimit': True,
})

# Lijst van munten waarop de bot actief is
SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT']
TIMEFRAME = '5m'
TRADE_AMOUNT_USD = 5.0  # Inzet per trade
LEVERAGE = 12           # Hefboom van 12x
MIN_BREAKOUT_PCT = 0.05 # Momentum drempelwaarde

# Winst en verlies percentages ten opzichte van instapprijs
TAKE_PROFIT_PCT = 0.4   # Bij 0.4% winst direct sluiten en winst pakken
STOP_LOSS_PCT = 0.3     # Bij 0.3% verlies direct afbreken voor de veiligheid

def set_leverage_for_symbol(symbol):
    try:
        # Zet de hefboom op 12x voor zowel long als short
        exchange.set_leverage(LEVERAGE, symbol, {'marginMode': 'isolated'})
    except Exception as e:
        # Soms staat de hefboom al goed, dan negeren we de melding
        pass

def run_momentum_bot():
    for symbol in SYMBOLS:  # Hier stond per ongeluk SYMBOL (enkelvoud)
        try:
            # Hefboom controleren/instellen voor deze munt
            set_leverage_for_symbol(symbol)

            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=2)
            if len(ohlcv) < 2:
                continue
            
            candle = ohlcv[-1]
            open_price = float(candle[1])
            close_price = float(candle[4])
            
            if open_price == 0:
                continue

            pct_change = ((close_price - open_price) / open_price) * 100
            
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            # Bereken de hoeveelheid op basis van 5 USDT en 12x hefboom
            notional_value = TRADE_AMOUNT_USD * LEVERAGE
            quantity = round(notional_value / current_price, 4)

            if pct_change >= MIN_BREAKOUT_PCT:
                print(f"🚀 [{symbol}] Bullish momentum ({pct_change:.2f}%). Long openen met {LEVERAGE}x hefboom...")
                
                # Bereken Take-Profit en Stop-Loss prijzen voor een BUY (Long) order
                tp_price = current_price * (1 + (TAKE_PROFIT_PCT / 100))
                sl_price = current_price * (1 - (STOP_LOSS_PCT / 100))
                
                params = {
                    'takeProfitPrice': exchange.price_to_precision(symbol, tp_price),
                    'stopLossPrice': exchange.price_to_precision(symbol, sl_price),
                }
                
                order = exchange.create_market_buy_order(symbol, quantity, params)
                print(f"✅ Long Order geplaatst op {symbol}! ID: {order.get('id')}")
                
            elif pct_change <= -MIN_BREAKOUT_PCT:
                print(f"🔻 [{symbol}] Bearish momentum ({pct_change:.2f}%). Short openen met {LEVERAGE}x hefboom...")
                
                # Bereken Take-Profit en Stop-Loss prijzen voor een SELL (Short) order
                tp_price = current_price * (1 - (TAKE_PROFIT_PCT / 100))
                sl_price = current_price * (1 + (STOP_LOSS_PCT / 100))
                
                params = {
                    'takeProfitPrice': exchange.price_to_precision(symbol, tp_price),
                    'stopLossPrice': exchange.price_to_precision(symbol, sl_price),
                }
                
                order = exchange.create_market_sell_order(symbol, quantity, params)
                print(f"✅ Short Order geplaatst op {symbol}! ID: {order.get('id')}")
            else:
                print(f"⏳ [{symbol}] Geen sterke beweging ({pct_change:.2f}%). Wachten...")

        except Exception as e:
            print(f"⚠️ Fout bij {symbol}: {e}")

if __name__ == "__main__":
    print("🤖 Multi-Coin Crypto Momentum Bot gestart (12x Hefboom + TP/SL)")
    exchange.load_markets()
    
    while True:
        run_momentum_bot()
        time.sleep(300)  # Wacht 5 minuten voor de volgende ronde
