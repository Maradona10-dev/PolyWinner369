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

TRADE_AMOUNT_USD = 5.0  
LEVERAGE = 12           
MIN_VOLUME_RATIO = 1.4  

# ATR & Risk Management parameters
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5   # Stop-loss op 1.5x ATR afstand
ATR_TP_MULTIPLIER = 3.0   # Take-profit op 3.0x ATR afstand

# Fee drag berekening (voor administratie in logs)
TAKER_FEE_PCT = 0.06  
FEE_DRAG_PCT = round(2 * TAKER_FEE_PCT * LEVERAGE, 3)  

last_trade_time = {}
COOLDOWN_SECONDS = 900  # 15 minuten rust per munt

# Houd bij of we posities al naar breakeven hebben verplaatst
breakeven_triggered = {}

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

def calculate_atr(ohlcv, period=14):
    if len(ohlcv) < period + 1:
        return 0.0
    tr_list = []
    for i in range(1, len(ohlcv)):
        high = ohlcv[i][2]
        low = ohlcv[i][3]
        prev_close = ohlcv[i-1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    
    # Eenvoudige EMA/SMA van True Range
    recent_tr = tr_list[-period:]
    return sum(recent_tr) / len(recent_tr)

def calculate_ema(closes, period=21):
    if len(closes) < period:
        return closes[-1]
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

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
                
            # Slim smart exit / breakeven systeem
            if symbol not in breakeven_triggered:
                breakeven_triggered[symbol] = False

            # Als winst boven de 1.5% komt, zetten we notificatie of beschermen we de trade
            if pnl_pct >= 2.5:
                close_position(symbol, side, contracts, f"🎯 *Take-Profit bereikt op {symbol}!*\nPnL (bruto): `+{pnl_pct:.2f}%` | Netto: `~+{pnl_pct - FEE_DRAG_PCT:.2f}%`")
                last_trade_time[symbol] = time.time()
                if symbol in breakeven_triggered:
                    del breakeven_triggered[symbol]
            elif pnl_pct <= -2.0:
                close_position(symbol, side, contracts, f"🛑 *Stop-Loss / ATR-limiet bereikt op {symbol}!*\nPnL (bruto): `{pnl_pct:.2f}%` | Netto: `~{pnl_pct - FEE_DRAG_PCT:.2f}%`")
                last_trade_time[symbol] = time.time()
                if symbol in breakeven_triggered:
                    del breakeven_triggered[symbol]
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

def run_pro_bot():
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

            # Data ophalen voor Multi-Timeframe (5m en 15m)
            ohlcv_5m = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=30)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
            
            if len(ohlcv_5m) < 20 or len(ohlcv_15m) < 20:
                continue
            
            # --- 15M Trend Filter (Multi-TF Confluence) ---
            closes_15m = [x[4] for x in ohlcv_15m]
            ema_15m = calculate_ema(closes_15m, period=21)
            current_15m_price = closes_15m[-1]
            trend_15m_bullish = current_15m_price > ema_15m

            # --- 5M Indicatoren & ATR ---
            closes_5m = [x[4] for x in ohlcv_5m]
            volumes_5m = [x[5] for x in ohlcv_5m]
            
            current_rsi = calculate_rsi(closes_5m, period=14)
            current_atr = calculate_atr(ohlcv_5m, period=ATR_PERIOD)
            
            avg_volume = sum(volumes_5m[-10:-1]) / 9 if len(volumes_5m) >= 10 else 1.0
            current_volume = volumes_5m[-1]
            vol_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

            candle = ohlcv_5m[-1]
            open_price = float(candle[1])
            close_price = float(candle[4])
            high_price = float(candle[2])
            low_price = float(candle[3])
            
            # Kwaliteitscheck kaars: lichaam moet minstens 50% van de total range zijn (geen vage wicks)
            candle_range = high_price - low_price
            body_size = abs(close_price - open_price)
            solid_body = body_size >= (candle_range * 0.5) if candle_range > 0 else True

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

            # --- LOGICA: CONFLUENCE + ATR VALIDATIE ---
            # Long Signaal: 15m trend is bullish + 5m volume piek + solide kaars + RSI in gezonde zone
            if trend_15m_bullish and pct_change >= 0.3 and vol_ratio >= MIN_VOLUME_RATIO and solid_body and 42 < current_rsi < 68:
                exchange.create_market_buy_order(symbol, quantity)
                msg = f"🚀 *Pro Multi-TF Long op {symbol}!*\n• 15m Trend: `Bullish`\n• 5m Move: `+{pct_change:.2f}%`\n• RSI: `{current_rsi:.1f}` | Vol: `{vol_ratio:.1f}x` (ATR: `{current_atr:.4f}`)"
                print(msg)
                send_telegram_message(msg)
                last_trade_time[symbol] = time.time()
                
            # Short Signaal: 15m trend is bearish + 5m volume piek + solide kaars + RSI in gezonde zone
            elif not trend_15m_bullish and pct_change <= -0.3 and vol_ratio >= MIN_VOLUME_RATIO and solid_body and 32 < current_rsi < 58:
                exchange.create_market_sell_order(symbol, quantity)
                msg = f"📉 *Pro Multi-TF Short op {symbol}!*\n• 15m Trend: `Bearish`\n• 5m Move: `{pct_change:.2f}%`\n• RSI: `{current_rsi:.1f}` | Vol: `{vol_ratio:.1f}x` (ATR: `{current_atr:.4f}`)"
                print(msg)
                send_telegram_message(msg)
                last_trade_time[symbol] = time.time()

        except Exception as e:
            print(f"⚠️ Fout bij scannen {symbol}: {e}")

if __name__ == "__main__":
    exchange.load_markets()
    start_msg = "💎 *Pro-Grade Bot gestart*\n(Multi-TF Confluence + ATR Volatility + Smart Volume filters)"
    print(start_msg)
    send_telegram_message(start_msg)
    
    while True:
        run_pro_bot()
        time.sleep(60)
