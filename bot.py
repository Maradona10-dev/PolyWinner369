import ccxt
import time
import os
import requests
from datetime import datetime

# ====================== CONFIGURATIE ======================
exchange = ccxt.blofin({
    'apiKey': os.environ.get('EXCHANGE_API_KEY', ''),
    'secret': os.environ.get('EXCHANGE_SECRET', ''),
    'password': os.environ.get('EXCHANGE_PASSWORD', ''),
    'enableRateLimit': True,
})

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT',
    'LINK/USDT:USDT', 'SUI/USDT:USDT', 'NEAR/USDT:USDT', 'PEPE/USDT:USDT'
]

LEVERAGE = 12
BASE_RISK_USD = 5.0                  # Basis risico per trade
MIN_SCORE = 3                        # Minimum score om te traden (3 of 4)
MAX_OPEN_POSITIONS = 2               # Maximum aantal open posities
COOLDOWN_SECONDS = 900               # 15 minuten cooldown per coin

ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.6
ATR_PARTIAL_MULTIPLIER = 1.3
ATR_TP_MULTIPLIER = 2.8

TAKER_FEE_PCT = 0.06
FEE_DRAG_PCT = round(2 * TAKER_FEE_PCT * LEVERAGE, 3)

# State
last_trade_time = {}
trade_info = {}          # symbol -> {side, entry, atr, partial_done}
# ==========================================================


def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"⚠️ Telegram fout: {e}")


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
    except Exception:
        pass


def manage_positions():
    try:
        positions = exchange.fetch_positions()
        active_symbols = []
        
        for pos in positions:
            contracts = float(pos.get('contracts', 0))
            if contracts == 0:
                continue
            symbol = pos.get('symbol')
            if symbol not in SYMBOLS:
                continue
            
            active_symbols.append(symbol)
            entry_price = float(pos.get('entryPrice', 0))
            side = pos.get('side')
            if entry_price == 0:
                continue

            if symbol not in trade_info:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=20)
                fallback_atr = calculate_atr(ohlcv, ATR_PERIOD) if len(ohlcv) >= 20 else (entry_price * 0.01)
                trade_info[symbol] = {
                    'side': side,
                    'entry': entry_price,
                    'atr': fallback_atr,
                    'partial_done': False
                }

            info = trade_info[symbol]
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            sl_distance = info['atr'] * ATR_SL_MULTIPLIER
            partial_distance = info['atr'] * ATR_PARTIAL_MULTIPLIER
            tp_distance = info['atr'] * ATR_TP_MULTIPLIER

            if info['side'] == 'long':
                pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
                move = current_price - entry_price
                hit_partial = move >= partial_distance
                hit_tp = move >= tp_distance
                hit_sl = current_price <= (entry_price - sl_distance)
            else:
                pnl_pct = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                move = entry_price - current_price
                hit_partial = move >= partial_distance
                hit_tp = move >= tp_distance
                hit_sl = current_price >= (entry_price + sl_distance)

            # Partial TP (50%)
            if hit_partial and not info['partial_done']:
                half_contracts = contracts / 2
                market = exchange.market(symbol)
                min_amt = market['limits']['amount']['min']
                if half_contracts >= min_amt:
                    prec_contracts = exchange.amount_to_precision(symbol, half_contracts)
                    if info['side'] == 'long':
                        exchange.create_market_sell_order(symbol, prec_contracts, {'reduceOnly': True})
                    else:
                        exchange.create_market_buy_order(symbol, prec_contracts, {'reduceOnly': True})
                    info['partial_done'] = True
                    send_telegram_message(f"🛡️ *Inverted Partial TP (50%) bereikt op {symbol}* | PnL: `+{pnl_pct:.2f}%`")

            # Full TP or SL
            if hit_tp:
                close_position(symbol, info['side'], contracts, f"🎯 *Inverted Take-Profit bereikt op {symbol}* | PnL: `+{pnl_pct:.2f}%`")
            elif hit_sl:
                close_position(symbol, info['side'], contracts, f"🛑 *Inverted Stop-Loss bereikt op {symbol}* | PnL: `{pnl_pct:.2f}%`")

        # Cleanup closed positions from state
        for s in list(trade_info.keys()):
            if s not in active_symbols:
                del trade_info[s]

    except Exception as e:
        print(f"⚠️ Fout bij beheren posities: {e}")


def close_position(symbol, side, contracts, msg):
    try:
        if side == 'long':
            exchange.create_market_sell_order(symbol, contracts, {'reduceOnly': True})
        else:
            exchange.create_market_buy_order(symbol, contracts, {'reduceOnly': True})
        print(msg)
        send_telegram_message(msg)
        last_trade_time[symbol] = time.time()
        if symbol in trade_info:
            del trade_info[symbol]
    except Exception as e:
        print(f"⚠️ Sluiten mislukt voor {symbol}: {e}")


def run_bot():
    manage_positions()

    if len(trade_info) >= MAX_OPEN_POSITIONS:
        return

    for symbol in SYMBOLS:
        try:
            if len(trade_info) >= MAX_OPEN_POSITIONS:
                break
            if symbol in trade_info:
                continue
            if symbol in last_trade_time and time.time() - last_trade_time[symbol] < COOLDOWN_SECONDS:
                continue

            enforce_leverage_and_margin(symbol)

            ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
            ohlcv_5m = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=30)

            if len(ohlcv_1h) < 20 or len(ohlcv_15m) < 20 or len(ohlcv_5m) < 20:
                continue

            closes_1h = [x[4] for x in ohlcv_1h]
            closes_15m = [x[4] for x in ohlcv_15m]
            closes_5m = [x[4] for x in ohlcv_5m]
            volumes_5m = [x[5] for x in ohlcv_5m]

            ema_1h = calculate_ema(closes_1h, 21)
            ema_15m = calculate_ema(closes_15m, 21)

            bullish_1h = closes_1h[-1] > ema_1h
            bullish_15m = closes_15m[-1] > ema_15m

            current_rsi = calculate_rsi(closes_5m, 14)
            current_atr = calculate_atr(ohlcv_5m, ATR_PERIOD)

            avg_vol = sum(volumes_5m[-10:-1]) / 9 if len(volumes_5m) >= 10 else 1.0
            vol_ratio = volumes_5m[-1] / avg_vol if avg_vol > 0 else 1.0

            bullish_score = 0
            bearish_score = 0

            if bullish_1h: 
                bullish_score += 1
            else: 
                bearish_score += 1

            if bullish_15m: 
                bullish_score += 1
            else: 
                bearish_score += 1

            if vol_ratio >= 1.3:
                bullish_score += 1
                bearish_score += 1

            if 45 < current_rsi < 62: 
                bullish_score += 1
            if 38 < current_rsi < 55: 
                bearish_score += 1

            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']

            atr_pct = (current_atr / current_price) * 100 if current_price > 0 else 1.0
            dyn_mult = max(0.5, min(2.0, 1.0 / (atr_pct if atr_pct > 0 else 1.0)))
            notional = BASE_RISK_USD * LEVERAGE * dyn_mult
            raw_qty = notional / current_price
            market = exchange.market(symbol)
            min_amt = market['limits']['amount']['min']
            if raw_qty < min_amt:
                raw_qty = min_amt
            qty = exchange.amount_to_precision(symbol, raw_qty)

            # --- OPGEDRAAIDE LOGICA (INVERTEREN) ---
            # Als de markt score aangeeft voor LONG, gaan we nu SHORT
            if bullish_score >= MIN_SCORE:
                order = exchange.create_market_sell_order(symbol, qty)
                entry = float(order.get('average', current_price) or current_price)
                trade_info[symbol] = {'side': 'short', 'entry': entry, 'atr': current_atr, 'partial_done': False}
                send_telegram_message(f"🔄 *Inverted Short [{bullish_score}/{MIN_SCORE}] op {symbol}* | Entry: `{entry}`")
                last_trade_time[symbol] = time.time()

            # Als de markt score aangeeft voor SHORT, gaan we nu LONG
            elif bearish_score >= MIN_SCORE:
                order = exchange.create_market_buy_order(symbol, qty)
                entry = float(order.get('average', current_price) or current_price)
                trade_info[symbol] = {'side': 'long', 'entry': entry, 'atr': current_atr, 'partial_done': False}
                send_telegram_message(f"🔄 *Inverted Long [{bearish_score}/{MIN_SCORE}] op {symbol}* | Entry: `{entry}`")
                last_trade_time[symbol] = time.time()

        except Exception as e:
            print(f"⚠️ Fout bij {symbol}: {e}")


if __name__ == "__main__":
    exchange.load_markets()
    send_telegram_message("🤖 *Inverted Trading Bot gestart (Longs/Shorts omgedraaid)*")
    while True:
        run_bot()
        time.sleep(60)
