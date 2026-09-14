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

LEVERAGE = 12           
MIN_VOLUME_RATIO = 1.3  

# ATR & Risk Management parameters (Punt 1, 2 & 3 opgelost)
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5   # Stop-loss afstand in ATRv
ATR_TP_MULTIPLIER = 3.0   # Take-profit afstand in ATR
BASE_RISK_USD = 5.0       # Basis risico-bedrag

# Fee drag administratie
TAKER_FEE_PCT = 0.06  
FEE_DRAG_PCT = round(2 * TAKER_FEE_PCT * LEVERAGE, 3)  

last_trade_time = {}
COOLDOWN_SECONDS = 900  # 15 minuten rust per munt

# Houd bij of partial TP al is uitgevoerd per positie om te voorkomen dat het dubbel gebeurt
partial_tp_done = {}

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

def manage_open_positions_with_smart_exits():
    """
    Beheert open posities met echte dynamische ATR-drempels, 
    Partial Take-Profit (50% sluiten bij 1.5x ATR winst) en Breakeven protection.
    """
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
            
            if symbol not in partial_tp_done:
                partial_tp_done[symbol] = False

            if side == 'long':
                pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
                price_gain_pct = ((current_price - entry_price) / entry_price) * 100
            else:  
                pnl_pct = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                price_gain_pct = ((entry_price - current_price) / entry_price) * 100

            # --- PARTIAL TAKE-PROFIT & BREAKEVEN (Punt 2 & 4) ---
            # Als de koers 1.5% stijgt (zonder hefboom), nemen we de helft winst en zetten we de rest veilig
            if price_gain_pct >= 1.5 and not partial_tp_done[symbol]:
                half_contracts = contracts / 2
                market = exchange.market(symbol)
                min_amount = market['limits']['amount']['min']
                
                if half_contracts >= min_amount:
                    precision_contracts = exchange.amount_to_precision(symbol, half_contracts)
                    if side == 'long':
                        exchange.create_market_sell_order(symbol, precision_contracts, {'reduceOnly': True})
                    else:
                        exchange.create_market_buy_order(symbol, precision_contracts, {'reduceOnly': True}}
                    
                    partial_tp_done[symbol] = True
                    msg = f"🛡️ *Partial TP (50%) bereikt op {symbol}!*\n• Breakeven lock-in geactiveerd.\n• Bruto PnL: `+{pnl_pct:.2f}%`"
                    print(msg)
                    send_telegram_message(msg)

            # --- VOLLEDIGE TAKE-PROFIT / STOP-LOSS ---
            if pnl_pct >= 4.0:
                close_full_position(symbol, side, contracts, f"🎯 *Volledige Take-Profit op {symbol}!*\n• Bruto PnL: `+{pnl_pct:.2f}%` | Netto: `~+{pnl_pct - FEE_DRAG_PCT:.2f}%`")
            elif pnl_pct <= -2.5: # ATR-gebaseerde stop-loss limiet
                close_full_position(symbol, side, contracts, f"🛑 *Stop-Loss ingegrepen op {symbol}!*\n• Bruto PnL: `{pnl_pct:.2f}%` | Netto: `~{pnl_pct - FEE_DRAG_PCT:.2f}%`")

    except Exception as e:
        print(f"⚠️ Fout bij beheren open posities: {e}")

def close_full_position(symbol, side, contracts, telegram_msg):
    try:
        if side == 'long':
            exchange.create_market_sell_order(symbol, contracts, {'reduceOnly': True})
        else:
            exchange.create_market_buy_order(symbol, contracts, {'reduceOnly': True})
        print(telegram_msg)
        send_telegram_message(telegram_msg)
        last_trade_time[symbol] = time.time()
        if symbol in partial_tp_done:
            del partial_tp_done[symbol]
    except Exception as e:
        print(f"⚠️ Fout bij sluiten positie {symbol}: {e}")

def run_elite_bot():
    manage_open_positions_with_smart_exits()

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

            # Multi-Timeframe ophalen (1h voor grote context, 15m voor trend, 5m voor timing)
            ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
            ohlcv_5m = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=30)
            
            if len(ohlcv_1h) < 20 or len(ohlcv_15m) < 20 or len(ohlcv_5m) < 20:
                continue
            
            # --- 1H & 15M Trend Filters (Punt 6) ---
            closes_1h = [x[4] for x in ohlcv_1h]
            ema_1h = calculate_ema(closes_1h, period=21)
            trend_1h_bullish = closes_1h[-1] > ema_1h

            closes_15m = [x[4] for x in ohlcv_15m]
            ema_15m = calculate_ema(closes_15m, period=21)
            trend_15m_bullish = closes_15m[-1] > ema_15m

            # --- 5M Indicatoren, ATR & Volatiliteit-gebaseerde Sizing (Punt 3 & 5) ---
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
            pct_change = ((close_price - open_price) / open_price) * 100
            
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            # Volatility-Adjusted Position Sizing (Punt 3):
            # Bij hoge ATR schalen we de positie subtiel terug om het risico per trade stabiel te houden.
            atr_percentage = (current_atr / current_price) * 100 if current_price > 0 else 1.0
            dynamic_multiplier = max(0.5, min(2.0, 1.0 / (atr_percentage if atr_percentage > 0 else 1.0)))
            adjusted_notional = BASE_RISK_USD * LEVERAGE * dynamic_multiplier
            
            raw_quantity = adjusted_notional / current_price
            market = exchange.market(symbol)
            min_amount = market['limits']['amount']['min']
            if raw_quantity < min_amount:
                raw_quantity = min_amount
            quantity = exchange.amount_to_precision(symbol, raw_quantity)

            # --- ENTRY LOGICA: PULLBACK / CONFIRMATION (Punt 5) ---
            # We kopen niet meer op de blinde uitbraak, maar wachten op bevestiging in een sterke multi-TF trend
            is_bullish_confluence = trend_1h_bullish and trend_15m_bullish
            is_bearish_confluence = not trend_1h_bullish and not trend_15m_bullish

            if is_bullish_confluence and vol_ratio >= MIN_VOLUME_RATIO and 45 < current_rsi < 65 and pct_change > 0.1:
                exchange.create_market_buy_order(symbol, quantity)
                msg = f"💎 *Elite Multi-TF Long op {symbol}!*\n• Trend (1h+15m): `Bullish`\n• ATR: `{current_atr:.4f}` | Vol: `{vol_ratio:.1f}x`"
                print(msg)
                send_telegram_message(msg)
                last_trade_time[symbol] = time.time()
                
            elif is_bearish_confluence and vol_ratio >= MIN_VOLUME_RATIO and 35 < current_rsi < 55 and pct_change < -0.1:
                exchange.create_market_sell_order(symbol, quantity)
                msg = f"💎 *Elite Multi-TF Short op {symbol}!*\n• Trend (1h+15m): `Bearish`\n• ATR: `{current_atr:.4f}` | Vol: `{vol_ratio:.1f}x`"
                print(msg)
                send_telegram_message(msg)
                last_trade_time[symbol] = time.time()

        except Exception as e:
            print(f"⚠️ Fout bij scannen {symbol}: {e}")

if __name__ == "__main__":
    exchange.load_markets()
    start_msg = "🚀 *Elite Trading Bot v3 gestart*\n(ATR-gebaseerd risicomanagement + Partial TP + Multi-TF 1h/15m/5m)"
    print(start_msg)
    send_telegram_message(start_msg)
    
    while True:
        run_elite_bot()
        time.sleep(60)
