import ccxt
import time
import os
import requests

# Verbinding maken met BloFin via CCXT
exchange = ccxt.blofin({
    'apiKey': os.environ.get('EXCHANGE_API_KEY', ''),
    'secret': os.environ.get('EXCHANGE_SECRET', ''),
    'password': os.environ.get('EXCHANGE_PASSWORD', ''),
    'enableRateLimit': True,
})

# Telegram configuratie
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

def send_telegram_message(message):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"⚠️ Fout bij versturen Telegram bericht: {e}")

# Lijst van munten waarop de bot actief is
SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT']
TIMEFRAME = '5m'
TRADE_AMOUNT_USD = 5.0  # Inzet per trade
LEVERAGE = 12           # Hefboom van 12x
MIN_BREAKOUT_PCT = 0.05 # Momentum drempelwaarde

def set_leverage_for_symbol(symbol):
    try:
        exchange.set_leverage(LEVERAGE, symbol, {'marginMode': 'isolated'})
    except Exception as e:
        pass

def run_momentum_bot():
    for symbol in SYMBOLS:
        try:
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
            
            # Bereken notional waarde en controleer exchange minimums
            notional_value = TRADE_AMOUNT_USD * LEVERAGE
            raw_quantity = notional_value / current_price
            
            market = exchange.market(symbol)
            min_amount = market['limits']['amount']['min']
            
            # Zorg dat de hoeveelheid minimaal gelijk is aan wat BloFin vereist
            if raw_quantity < min_amount:
                raw_quantity = min_amount

            quantity = exchange.amount_to_precision(symbol, raw_quantity)

            if pct_change >= MIN_BREAKOUT_PCT:
                msg = f"🚀 *[{symbol}]* Bullish momentum ({pct_change:.2f}%).\nLong openen met {LEVERAGE}x hefboom..."
                print(msg)
                
                order = exchange.create_market_buy_order(symbol, quantity)
                success_msg = f"✅ *Long Order geplaatst op {symbol}!*\nHoeveelheid: `{quantity}` | Prijs: `{current_price}`"
                print(success_msg)
                send_telegram_message(success_msg)
                
            elif pct_change <= -MIN_BREAKOUT_PCT:
                msg = f"🔻 *[{symbol}]* Bearish momentum ({pct_change:.2f}%).\nShort openen met {LEVERAGE}x hefboom..."
                print(msg)
                
                order = exchange.create_market_sell_order(symbol, quantity)
                success_msg = f"✅ *Short Order geplaatst op {symbol}!*\nHoeveelheid: `{quantity}` | Prijs: `{current_price}`"
                print(success_msg)
                send_telegram_message(success_msg)
            else:
                print(f"⏳ [{symbol}] Geen sterke beweging ({pct_change:.2f}%). Wachten...")

        except Exception as e:
            err_msg = f"⚠️ Fout bij {symbol}: {e}"
            print(err_msg)

if __name__ == "__main__":
    exchange.load_markets()
    start_msg = "🤖 *Multi-Coin Crypto Momentum Bot gestart*\n(12x Hefboom + Minimale Volume Check)"
    print(start_msg)
    send_telegram_message(start_msg)
    
    while True:
        run_momentum_bot()
        time.sleep(300)
