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

# Instellingen voor de bot
SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT']
TIMEFRAME = '5m'
TRADE_AMOUNT_USD = 5.0  # Inzet per trade
LEVERAGE = 12           # Hefboom van 12x
MIN_BREAKOUT_PCT = 0.05 # Momentum drempelwaarde

# Doelstellingen voor automatische winst en verlies (in percentages)
TAKE_PROFIT_PCT = 0.4   # Winstdoel: 0.4%
STOP_LOSS_PCT = 0.3     # Verlieslimiet: 0.3%

def enforce_leverage_and_margin(symbol):
    try:
        # Forceer Isolated margin en 12x hefboom
        exchange.set_margin_mode('isolated', symbol)
        exchange.set_leverage(LEVERAGE, symbol)
    except Exception as e:
        print(f"⚠️ Kon margin/hefboom niet instellen voor {symbol}: {e}")

def manage_open_positions():
    """Controleert open posities en sluit ze automatisch bij bereiken van TP of SL"""
    try:
        balance = exchange.fetch_balance()
        positions = exchange.fetch_positions()
        
        for pos in positions:
            contracts = float(pos.get('contracts', 0))
            if contracts == 0:
                continue
                
            symbol = pos.get('symbol')
            if symbol not in SYMBOLS:
                continue
                
            entry_price = float(pos.get('entryPrice', 0))
            side = pos.get('side') # 'long' of 'short'
            
            if entry_price == 0:
                continue
                
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            # Bereken percentage winst/verlies inclusief hefboom
            if side == 'long':
                pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
            else:  # short
                pnl_pct = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                
            # Actie ondernemen bij behalen van TP of SL
            if pnl_pct >= TAKE_PROFIT_PCT:
                print(f"🎯 Take-Profit geraakt voor {symbol} ({pnl_pct:.2f}%). Positie sluiten...")
                close_position(symbol, side, contracts, f"🎯 *Take-Profit bereikt op {symbol}!*\nPnL: `+{pnl_pct:.2f}%`\nInstap: `{entry_price}` | Uitstap: `{current_price}`")
                
            elif pnl_pct <= -STOP_LOSS_PCT:
                print(f"🛑 Stop-Loss geraakt voor {symbol} ({pnl_pct:.2f}%). Positie sluiten...")
                close_position(symbol, side, contracts, f"🛑 *Stop-Loss ingegrepen op {symbol}!*\nPnL: `{pnl_pct:.2f}%`\nInstap: `{entry_price}` | Uitstap: `{current_price}`")
                
    except Exception as e:
        print(f"⚠️ Fout bij beheren open posities: {e}")

def close_position(symbol, side, contracts, telegram_msg):
    try:
        if side == 'long':
            exchange.create_market_sell_order(symbol, contracts, {'reduceOnly': True})
        else:
            exchange.create_market_buy_order(symbol, contracts, {'reduceOnly': True})
            
        print(telegram_msg)
        send_telegram_message(telegram_msg)
    except Exception as e:
        print(f"⚠️ Fout bij sluiten positie {symbol}: {e}")

def run_momentum_bot():
    # Stap 1: Beheer eerst de actieve posities (TP/SL checks)
    manage_open_positions()

    # Stap 2: Scan op nieuwe momentums
    for symbol in SYMBOLS:
        try:
            enforce_leverage_and_margin(symbol)

            # Kijk of we al een open positie hebben voor deze munt; zo ja, open geen nieuwe
            positions = exchange.fetch_positions([symbol])
            has_position = any(float(p.get('contracts', 0)) > 0 for p in positions)
            if has_position:
                continue

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
            
            # Bereken correcte volume
            notional_value = TRADE_AMOUNT_USD * LEVERAGE
            raw_quantity = notional_value / current_price
            
            market = exchange.market(symbol)
            min_amount = market['limits']['amount']['min']
            if raw_quantity < min_amount:
                raw_quantity = min_amount

            quantity = exchange.amount_to_precision(symbol, raw_quantity)

            if pct_change >= MIN_BREAKOUT_PCT:
                msg = f"🚀 *[{symbol}]* Bullish momentum ({pct_change:.2f}%).\nLong openen met {LEVERAGE}x hefboom..."
                print(msg)
                
                exchange.create_market_buy_order(symbol, quantity)
                success_msg = f"✅ *Long Order geplaatst op {symbol}!*\nHoeveelheid: `{quantity}` | Prijs: `{current_price}` (12x Isolated)"
                print(success_msg)
                send_telegram_message(success_msg)
                
            elif pct_change <= -MIN_BREAKOUT_PCT:
                msg = f"🔻 *[{symbol}]* Bearish momentum ({pct_change:.2f}%).\nShort openen met {LEVERAGE}x hefboom..."
                print(msg)
                
                exchange.create_market_sell_order(symbol, quantity)
                success_msg = f"✅ *Short Order geplaatst op {symbol}!*\nHoeveelheid: `{quantity}` | Prijs: `{current_price}` (12x Isolated)"
                print(success_msg)
                send_telegram_message(success_msg)

        except Exception as e:
            print(f"⚠️ Fout bij scannen {symbol}: {e}")

if __name__ == "__main__":
    exchange.load_markets()
    start_msg = "🤖 *Slimme Momentum Bot gestart*\n(12x Isolated Hefboom + Auto TP/SL + Telegram Meldingen)"
    print(start_msg)
    send_telegram_message(start_msg)
    
    while True:
        run_momentum_bot()
        time.sleep(60)  # Controleert nu elke minuut de posities en markten
