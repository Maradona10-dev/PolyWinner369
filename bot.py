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

# Slimme instellingen om overtrading tegen te gaan
SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT']
TIMEFRAME = '5m'
TRADE_AMOUNT_USD = 5.0  # Inzet per trade
LEVERAGE = 12           # Hefboom van 12x
MIN_BREAKOUT_PCT = 0.25 # Pas handelen bij een koerssprong van minstens 0.25%

# Winst en verlies doelen (in % rendement)
TAKE_PROFIT_PCT = 0.8   # Winstdoel
STOP_LOSS_PCT = 0.5     # Verlieslimiet

# Cooldown bijhouden per munt (zodat hij niet direct weer instapt)
last_trade_time = {}
COOLDOWN_SECONDS = 900  # 15 minuten rust na sluiten van een trade

def enforce_leverage_and_margin(symbol):
    try:
        exchange.set_margin_mode('isolated', symbol)
        exchange.set_leverage(LEVERAGE, symbol)
    except Exception as e:
        pass

def manage_open_positions():
    """Controleert open posities en sluit ze automatisch bij bereiken van TP of SL"""
    try:
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
            
            # Bereken rendement inclusief hefboom
            if side == 'long':
                pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
            else:  
                pnl_pct = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                
            # Actie bij behalen doel
            if pnl_pct >= TAKE_PROFIT_PCT:
                close_position(symbol, side, contracts, f"🎯 *Take-Profit bereikt op {symbol}!*\nPnL: `+{pnl_pct:.2f}%`\nInstap: `{entry_price}` | Uitstap: `{current_price}`")
                last_trade_time[symbol] = time.time()
                
            elif pnl_pct <= -STOP_LOSS_PCT:
                close_position(symbol, side, contracts, f"🛑 *Stop-Loss ingegrepen op {symbol}!*\nPnL: `{pnl_pct:.2f}%`\nInstap: `{entry_price}` | Uitstap: `{current_price}`")
                last_trade_time[symbol] = time.time()
                
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
    # Stap 1: Beheer actieve posities
    manage_open_positions()

    # Stap 2: Zoek naar nieuwe kansen met strengere filters
    for symbol in SYMBOLS:
        try:
            # Check of de munt in cooldown zit
            if symbol in last_trade_time:
                if time.time() - last_trade_time[symbol] < COOLDOWN_SECONDS:
                    continue

            enforce_leverage_and_margin(symbol)

            # Heeft deze munt al een open positie? Sla dan over.
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
                exchange.create_market_buy_order(symbol, quantity)
                success_msg = f"✅ *Sterke Long Order op {symbol}!*\nMomentum: `+{pct_change:.2f}%` | Prijs: `{current_price}` (12x Isolated)"
                print(success_msg)
                send_telegram_message(success_msg)
                
            elif pct_change <= -MIN_BREAKOUT_PCT:
                exchange.create_market_sell_order(symbol, quantity)
                success_msg = f"✅ *Sterke Short Order op {symbol}!*\nMomentum: `{pct_change:.2f}%` | Prijs: `{current_price}` (12x Isolated)"
                print(success_msg)
                send_telegram_message(success_msg)

        except Exception as e:
            print(f"⚠️ Fout bij scannen {symbol}: {e}")

if __name__ == "__main__":
    exchange.load_markets()
    start_msg = "🤖 *Geoptimaliseerde Momentum Bot gestart*\n(Filter: 0.25% breuk + 15m Cooldown + Auto TP/SL)"
    print(start_msg)
    send_telegram_message(start_msg)
    
    while True:
        run_momentum_bot()
        time.sleep(60)
