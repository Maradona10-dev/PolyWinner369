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

# ATR & Risk Management parameters (Elite v4)
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5   # Stop-loss op 1.5x ATR
ATR_TP_MULTIPLIER = 3.0   # Take-profit op 3.0x ATR
PARTIAL_TP_R = 1.4        # Partial TP op 1.4R
BASE_RISK_USD = 5.0       

# Fee drag administratie
TAKER_FEE_PCT = 0.06  
FEE_DRAG_PCT = round(2 * TAKER_FEE_PCT * LEVERAGE, 3)  

last_trade_time = {}
COOLDOWN_SECONDS = 900  # 15 minuten rust per munt

# State tracking per actieve trade (bevat entry price, entry ATR, side, partial status)
trade_states = {}

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

def manage_open_positions_elite_v4():
    """
    Beheert open posities op basis van opgeslagen entry-states, 
    echte ATR-afstanden, R-multiples, automatische breakeven en schone state-cleanup.
    """
    try:
        positions = exchange.fetch_positions()
        active_exchange_symbols = []

        for pos in positions:
            contracts = float(pos.get('contracts', 0))
            if contracts == 0:
                continue
            symbol = pos.get('symbol')
            if symbol not in SYMBOLS:
                continue
            
            active_exchange_symbols.append(symbol)
            entry_price = float(pos.get('entryPrice', 0))
            side = pos.get('side')
            if entry_price == 0:
                continue

            # Als de trade nog niet in onze state staat (bijv. na bot restart), initialiseer veilig
            if symbol not in trade_states:
                # Vraag snelle ATR op als fallback
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=20)
                fallback_atr = calculate_atr(ohlcv, ATR_PERIOD) if len(ohlcv) >= 20 else (entry_price * 0.01)
                trade_states[symbol] = {
                    'entry_price': entry_price,
                    'entry_atr': fallback_atr,
                    'side': side,
                    'partial_done': False,
                    'breakeven_active': False
                }

            state = trade_states[symbol]
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']

            # Bereken risico-afstand (R) op basis van de ATR bij entry
            r_distance = state['entry_atr'] * ATR_SL_MULTIPLIER
            tp_distance = state['entry_atr'] * ATR_TP_MULTIPLIER

            if state['side'] == 'long':
                pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
                price_move = current_price - entry_price
                current_r = price_move / r_distance if r_distance > 0 else 0
                
                stop_loss_price = entry_price if state['breakeven_active'] else (entry_price - r_distance)
                take_profit_price = entry_price + tp_distance
                
                hit_sl = current_price <= stop_loss_price
                hit_tp = current_price >= take_profit_price

            else: # short
                pnl_pct = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                price_move = entry_price - current_price
                current_r = price_move / r_distance if r_distance > 0 else 0
                
                stop_loss_price = entry_price if state['breakeven_active'] else (entry_price + r_distance)
                take_profit_price = entry_price - tp_distance
                
                hit_sl = current_price >= stop_loss_price
                hit_tp = current_price <= take_profit_price

            # 1. Partial TP op 1.4R (50% sluiten + Breakeven activeren)
            if current_r >= PARTIAL_TP_R and not state['partial_done']:
                half_contracts = contracts / 2
                market = exchange.market(symbol)
                min_amount = market['limits']['amount']['min']
                
                if half_contracts >= min_amount:
                    precision_contracts = exchange.amount_to_precision(symbol, half_contracts)
                    if state['side'] == 'long':
                        exchange.create_market_sell_order(symbol, precision_contracts, {'reduceOnly': True})
                    else:
                        exchange.create_market_buy_order(symbol, precision_contracts, {'reduceOnly': True})
                    
                    state['partial_done'] = True
                    state['breakeven_active'] = True
                    msg = f"🛡️ *Partial TP (50%) op {symbol} bereikt ({current_r:.2f}R)*\n• Stop-loss verplaatst naar Breakeven."
                    print(msg)
                    send_telegram_message(msg)

            # 2. Volledige Take-Profit of Stop-Loss bereikt
            if hit_tp:
                close_full_position(symbol, state['side'], contracts, f"🎯 *ATR Take-Profit bereikt op {symbol}!*\n• Bruto PnL: `+{pnl_pct:.2f}%` | Netto: `~+{pnl_pct - FEE_DRAG_PCT:.2f}%`")
            elif hit_sl:
                sl_type = "Breakeven" if state['breakeven_active'] else "ATR Stop-Loss"
                close_full_position(symbol, state['side'], contracts, f"🛑 *{sl_type} getriggerd op {symbol}!*\n• Bruto PnL: `{pnl_pct:.2f}%` | Netto: `~{pnl_pct - FEE_DRAG_PCT:.2f}%`")

        # Ruim state op als posities extern/handmatig zijn gesloten op de exchange
        closed_symbols = [s for s in trade_states if s not in active_exchange_symbols]
        for s in closed_symbols:
            del trade_states[s]

    except Exception as e:
        print(f"⚠️ Fout bij beheren open posities (v4): {e}")

def close_full_position(symbol, side, contracts, telegram_msg):
    try:
        if side == 'long':
            exchange.create_market_sell_order(symbol, contracts, {'reduceOnly': True})
        else:
            exchange.create_market_buy_order(symbol, contracts, {'reduceOnly': True})
        print(telegram_msg)
        send_telegram_message(telegram_msg)
        last_trade_time[symbol] = time.time()
        if symbol in trade_states:
            del trade_states[symbol]
    except Exception as e:
        print(f"⚠️ Fout bij sluiten positie {symbol}: {e}")

def run_elite_v4_bot():
    manage_open_positions_elite_v4()

    for symbol in SYMBOLS:
        try:
            if symbol in last_trade_time:
                if time.time() - last_trade_time[symbol] < COOLDOWN_SECONDS:
                    continue

            enforce_leverage_and_margin(symbol)

            if symbol in trade_states:
                continue

            # Data ophalen: 1h, 15m, 5m voor robuuste multi-timeframe context
            ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
            ohlcv_5m = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=30)
            
            if len(ohlcv_1h) < 20 or len(ohlcv_15m) < 20 or len(ohlcv_20 := ohlcv_5m) < 20:
                continue
            
            closes_1h = [x[4] for x in ohlcv_1h]
            ema_1h = calculate_ema(closes_1h, period=21)
            trend_1h_bullish = closes_1h[-1] > ema_1h

            closes_15m = [x[4] for x in ohlcv_15m]
            ema_15m = calculate_ema(closes_15m, period=21)
            trend_15m_bullish = closes_15m[-1] > ema_15m

            closes_5m = [x[4] for x in ohlcv_5m]
            volumes_5m = [x[5] for x in ohlcv_5m]
            
            current_rsi = calculate_rsi(closes_5m, period=14)
            current_atr = calculate_atr(ohlcv_5m, period=ATR_PERIOD)
            
            avg_volume = sum(volumes_5m[-10:-1]) / 9 if len(volumes_5m) >= 10 else 1.0
            current_volume = volumes_5m[-1]
            vol_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

            # Lichte pullback / stabiliteitsfilter in plaats van blinde breakout chase
            prev_candle_close = ohlcv_5m[-2][4]
            prev_candle_open = ohlcv_5m[-2][1]
            current_candle_open = ohlcv_5m[-1][1]
            current_candle_close = ohlcv_5m[-1][4]
            
            pct_change = ((current_candle_close - current_candle_open) / current_candle_open) * 100
            
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            # Volatility-Adjusted Sizing
            atr_percentage = (current_atr / current_price) * 100 if current_price > 0 else 1.0
            dynamic_multiplier = max(0.5, min(2.0, 1.0 / (atr_percentage if atr_percentage > 0 else 1.0)))
            adjusted_notional = BASE_RISK_USD * LEVERAGE * dynamic_multiplier
            
            raw_quantity = adjusted_notional / current_price
            market = exchange.market(symbol)
            min_amount = market['limits']['amount']['min']
            if raw_quantity < min_amount:
                raw_quantity = min_amount
            quantity = exchange.amount_to_precision(symbol, raw_quantity)

            is_bullish_confluence = trend_1h_bullish and trend_15m_bullish
            is_bearish_confluence = not trend_1h_bullish and not trend_15m_bullish

            # Entry met bevestiging (geen extreme uitschieters die al weg zijn, maar sterke trend + volume)
            if is_bullish_confluence and vol_ratio >= MIN_VOLUME_RATIO and 45 < current_rsi < 62 and -0.2 < pct_change < 0.8:
                order = exchange.create_market_buy_order(symbol, quantity)
                actual_entry = float(order.get('average', current_price) or current_price)
                
                trade_states[symbol] = {
                    'entry_price': actual_entry,
                    'entry_atr': current_atr,
                    'side': 'long',
                    'partial_done': False,
                    'breakeven_active': False
                }
                
                msg = f"💎 *Elite v4 Long op {symbol}!*\n• Entry: `{actual_entry}`\n• ATR: `{current_atr:.4f}` | Vol: `{vol_ratio:.1f}x`"
                print(msg)
                send_telegram_message(msg)
                last_trade_time[symbol] = time.time()
                
            elif is_bearish_confluence and vol_ratio >= MIN_VOLUME_RATIO and 38 < current_rsi < 55 and -0.8 < pct_change < 0.2:
                order = exchange.create_market_sell_order(symbol, quantity)
                actual_entry = float(order.get('average', current_price) or current_price)
                
                trade_states[symbol] = {
                    'entry_price': actual_entry,
                    'entry_atr': current_atr,
                    'side': 'short',
                    'partial_done': False,
                    'breakeven_active': False
                }
                
                msg = f"💎 *Elite v4 Short op {symbol}!*\n• Entry: `{actual_entry}`\n• ATR: `{current_atr:.4f}` | Vol: `{vol_ratio:.1f}x`"
                print(msg)
                send_telegram_message(msg)
                last_trade_time[symbol] = time.time()

        except Exception as e:
            print(f"⚠️ Fout bij scannen {symbol}: {e}")

if __name__ == "__main__":
    exchange.load_markets()
    start_msg = "🚀 *Elite Trading Bot v4 gestart*\n(Echte ATR-state tracking + 1.4R Partial TP + Breakeven + Stricter filters)"
    print(start_msg)
    send_telegram_message(start_msg)
    
    while True:
        run_elite_v4_bot()
        time.sleep(60)
