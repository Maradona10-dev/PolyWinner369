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

# Selectie munten
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT',
    'LINK/USDT:USDT', 'SUI/USDT:USDT', 'NEAR/USDT:USDT', 'PEPE/USDT:USDT'
]

TIMEFRAME = '5m'
TRADE_AMOUNT_USD = 5.0  
LEVERAGE = 12           
MIN_BREAKOUT_PCT = 0.25 
MIN_VOLUME_RATIO = 1.5  # Volume moet minstens 1.5x het gemiddelde zijn

# Fee-gecorrigeerde TP/SL
TAKER_FEE_PCT = 0.06  
FEE_DRAG_PCT = round(2 * TAKER_FEE_PCT * LEVERAGE, 3)  # 1.44%

TAKE_PROFIT_PCT = 3.0   
STOP_LOSS_PCT = 2.0     

last_trade_time = {}
COOLDOWN_SECONDS = 900  # 15 minuten rust

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def enforce_leverage_and_margin(symbol):
    try:
        exchange.set_margin_mode('isolated', symbol)
        exchange.set_leverage(LEVERAGE, symbol)
    except Exception as e:
        pass

def manage_open_positions():
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
            side = pos.get('side')
            if entry_price == 0:
                continue
                
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            if side == 'long':
                pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
            else:  
                pnl_pct = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                
            if pnl_pct >= TAKE_PROFIT_PCT:
                close_position(symbol, side, contracts, f"🎯 *Take-Profit bereikt op {symbol}!*\nPnL (bruto): `+{pnl_pct:.2f}%` | Netto: `~+{pnl_pct - FEE_DRAG_PCT:.2f}%`")
                last_trade_time[symbol] = time.time()
            elif pnl_pct <= -STOP_LOSS_PCT:
                close_position(symbol, side, contracts, f"🛑 *Stop-Loss ingegrepen op {symbol}!*\nPnL (bruto): `{pnl_pct:.2f}%` | Netto: `~{pnl_pct - FEE_DRAG_PCT:.2f}%`")
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
    manage_open_positions()

    for symbol in SYMBOLS:
        try:
            if symbol in last_trade_time:
                if time.time() - last_trade_time[symbol] < COOLDOWN_SECONDS:
                    continue

            enforce_leverage_and_margin(symbol)

            positions = exchange.fetch_positions([symbol])
            has_position = any(float(p.get('contracts', 0)) > 0 for p in positions)
            if has_position:
                continue

            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=30)
            if len(ohlcv) < 20:
                continue
            
            closes = [x[4] for x in ohlcv]
            volumes = [x[5] for x in ohlcv]
            
            current_rsi = calculate_rsi(closes, period=14)
            avg_volume = sum(volumes[-10:-1]) / 9 if len(volumes) >= 10 else 1.0
            current_volume = volumes[-1]
            
            vol_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

            candle = ohlcv[-1]
            open_price = float(candle[1])
            close_price = float(candle[4])
            pct_change = ((close_price - open_price) / open_price) * 100
            
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            notional_value = TRADE_AMOUNT_USD * LEVERAGE
            raw_quantity = notional_value / current_price
            market = exchange.market(symbol)
            min_amount = market['limits']['amount']['min']
            if raw_quantity < min_amount:
                raw_quantity = min_amount
            quantity = exchange.amount_to_precision(symbol, raw_quantity)

            if pct_change >= MIN_BREAKOUT_PCT and vol_ratio >= MIN_VOLUME_RATIO and 40 < current_rsi < 70:
                exchange.create_market_buy_order(symbol, quantity)
                msg = f"🔥 *Gefilterde Long Order op {symbol}!*\n• Momentum: `+{pct_change:.2f}%`\n• RSI: `{current_rsi:.1f}`\n• Vol Ratio: `{vol_ratio:.1f}x` (12x)"
                print(msg)
                send_telegram_message(msg)
                
            elif pct_change <= -MIN_BREAKOUT_PCT and vol_ratio >= MIN_VOLUME_RATIO and 30 < current_rsi < 60:
                exchange.create_market_sell_order(symbol, quantity)
                msg = f"🔥 *Gefilterde Short Order op {symbol}!*\n• Momentum: `{pct_change:.2f}%`\n• RSI: `{current_rsi:.1f}`\n• Vol Ratio: `{vol_ratio:.1f}x` (12x)"
                print(msg)
                send_telegram_message(msg)

        except Exception as e:
            print(f"⚠️ Fout bij scannen {symbol}: {e}")

if __name__ == "__main__":
    exchange.load_markets()
    start_msg = "🤖 *Slimme Indicator-Bot gestart*\n(RSI + Volume Ratio + Fee-gecorrigeerde TP/SL)"
    print(start_msg)
    send_telegram_message(start_msg)
    
    while True:
        run_momentum_bot()
        time.sleep(60)
