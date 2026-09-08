import os
import time
import json
import requests

# —————————————————————————
# CONFIGURATIE & ENVIRONMENT VARIABELEN
# —————————————————————————

RAW_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN.replace('bot', '', 1) if RAW_TOKEN.startswith('bot') else RAW_TOKEN
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()

BINANCE_API_URL = "https://api.binance.com/api/v3"
SCAN_INTERVAL_SEC = 10

# Scannen van alle paren met Multi-Timeframe filter
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ZECUSDT"]

MAX_TRADES_PER_CYCLE = 2

# —————————————————————————
# PAPER TRADING INSTELLINGEN
# —————————————————————————
PAPER_BALANCE = 1000.0
TRADE_AMOUNT = 25.0
active_paper_trades = []
active_signals_sent = {}

# —————————————————————————
# TELEGRAM BERICHTEN
# —————————————————————————

def send_telegram_msg(message, button_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram token of Chat ID ontbreekt.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if button_url:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "🚀 ZET DIRECT IN OP POLYMARKET", "url": button_url}]]}

    try:
        res = requests.post(url, json=payload, timeout=8).json()
        if not res.get("ok"):
            print(f"❌ Telegram API Fout: {res}")
    except Exception as e:
        print(f"❌ Fout bij versturen Telegram: {e}")

# —————————————————————————
# DYNAMISCHE POLYMARKET ODDS
# —————————————————————————

def calculate_dynamic_polymarket_price(rsi, percent_change, strat_type, signal):
    base_price = 0.50
    abs_move = abs(percent_change)
    
    if strat_type == "MOMENTUM":
        rsi_factor = max(0, (rsi - 50) / 50.0) if signal == "UP" else max(0, (50 - rsi) / 50.0)
        move_factor = min(1.0, abs_move / 1.0)
        calculated_price = base_price + (rsi_factor * 0.20) + (move_factor * 0.18)
    else:
        rsi_extreme = abs(rsi - 50) / 50.0
        calculated_price = base_price + (rsi_extreme * 0.22)

    return round(max(0.52, min(0.88, calculated_price)), 2)

# —————————————————————————
# RSI BEREKENING
# —————————————————————————

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0

    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

# —————————————————————————
# 15-MINUTEN TREND CHECK (MULTI-TIMEFRAME)
# —————————————————————————

def fetch_15m_trend(symbol):
    try:
        url = f"{BINANCE_API_URL}/klines?symbol={symbol}&interval=15m&limit=10"
        res = requests.get(url, timeout=3).json()
        
        close_prices = [float(k[4]) for k in res]
        rsi_15m = calculate_rsi(close_prices)
        
        # Trend bepaling op basis van koersstijging en RSI op 15m
        price_change = ((close_prices[-1] - close_prices[-3]) / close_prices[-3]) * 100
        
        if price_change > 0.10 and rsi_15m > 50:
            return "BULLISH"
        elif price_change < -0.10 and rsi_15m < 50:
            return "BEARISH"
        
        return "NEUTRAL"
    except Exception as e:
        print(f"Fout bij 15m trendcheck voor {symbol}: {e}")
        return "NEUTRAL"

# —————————————————————————
# BINANCE DATA OPHALEN (5M GRAFIEK)
# —————————————————————————

def fetch_binance_data(symbol):
    try:
        url = f"{BINANCE_API_URL}/klines?symbol={symbol}&interval=1m&limit=30"
        res = requests.get(url, timeout=5).json()

        close_prices = [float(k[4]) for k in res]
        volumes = [float(k[5]) for k in res]

        current_price = close_prices[-1]
        start_price = close_prices[-5]

        percent_change = round(((current_price - start_price) / start_price) * 100, 2)
        rsi = calculate_rsi(close_prices)
        prev_rsi = calculate_rsi(close_prices[:-1])

        current_volume = volumes[-1]
        avg_volume = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else current_volume
        volume_ratio = round(current_volume / avg_volume, 2) if avg_volume > 0 else 1.0

        return current_price, percent_change, rsi, prev_rsi, volume_ratio
    except Exception as e:
        print(f"Fout bij ophalen data voor {symbol}: {e}")
        return None, None, None, None, None

# —————————————————————————
# STRATEGIE MET 15M MULTI-TIMEFRAME FILTER
# —————————————————————————

def analyze_market_and_get_signal(symbol, rsi, prev_rsi, percent_change, volume_ratio, time_remaining_sec):
    trend_15m = fetch_15m_trend(symbol)

    # -------------------------------------------------------------
    # FASE 1: MOMENTUM (Must Align with 15M Trend!)
    # -------------------------------------------------------------
    if 210 <= time_remaining_sec <= 300:
        min_move = 0.40 if symbol in ["ZECUSDT", "DOGEUSDT"] else 0.30

        # Alleen UP als 15M Trend minstens Neutraal of Bullish is (geen Bearish!)
        if 52 <= rsi <= 68 and percent_change >= min_move and volume_ratio >= 1.3:
            if trend_15m != "BEARISH":
                reason = f"🚀 Momentum UP (+{percent_change}%, Vol {volume_ratio}x) | 15M Trend: {trend_15m}"
                return "UP", "MOMENTUM", reason

        # Alleen DOWN als 15M Trend minstens Neutraal of Bearish is (geen Bullish!)
        elif 32 <= rsi <= 48 and percent_change <= -min_move and volume_ratio >= 1.3:
            if trend_15m != "BULLISH":
                reason = f"🚀 Momentum DOWN ({percent_change}%, Vol {volume_ratio}x) | 15M Trend: {trend_15m}"
                return "DOWN", "MOMENTUM", reason

    # -------------------------------------------------------------
    # FASE 2: PULLBACK / REVERSION (Extreme RSI Omslag)
    # -------------------------------------------------------------
    elif 60 <= time_remaining_sec <= 150:
        if prev_rsi >= 78 and rsi < prev_rsi and percent_change >= 0.45:
            reason = f"🔄 Piek Omslag Bevestigd (Prev RSI {prev_rsi} ➔ {rsi}) | 15M: {trend_15m}"
            return "DOWN", "PULLBACK", reason

        elif prev_rsi <= 22 and rsi > prev_rsi and percent_change <= -0.45:
            reason = f"🔄 Bodem Bounce Bevestigd (Prev RSI {prev_rsi} ➔ {rsi}) | 15M: {trend_15m}"
            return "UP", "PULLBACK", reason

    return None, None, "Geen geschikt signaal"

# —————————————————————————
# PAPER TRADING EVALUATIE
# —————————————————————————

def check_and_evaluate_paper_trades():
    global PAPER_BALANCE, active_paper_trades
    now_ts = time.time()
    remaining_trades = []

    for trade in active_paper_trades:
        if now_ts >= trade["expiry_time"]:
            current_price, _, _, _, _ = fetch_binance_data(trade["symbol"])
            if current_price is None:
                remaining_trades.append(trade)
                continue

            entry_price = trade["entry_price"]
            signal = trade["signal"]
            share_price = trade["share_price"]
            shares_bought = trade["shares_bought"]
            
            price_diff = round(current_price - entry_price, 4)
            pct_diff = round(((current_price - entry_price) / entry_price) * 100, 2)
            
            is_win = False
            if signal == "UP" and current_price > entry_price:
                is_win = True
            elif signal == "DOWN" and current_price < entry_price:
                is_win = True

            if is_win:
                payout = shares_bought * 1.00
                profit = round(payout - TRADE_AMOUNT, 2)
                PAPER_BALANCE += profit
                badge = f"✅ **PAPER TRADE GEWONNEN!** (+${profit:.2f})"
            else:
                PAPER_BALANCE -= TRADE_AMOUNT
                badge = f"❌ **PAPER TRADE VERLOREN** (-${TRADE_AMOUNT:.2f})"

            diff_str = f"+${price_diff} (+{pct_diff}%)" if price_diff > 0 else f"-${abs(price_diff)} ({pct_diff}%)"

            msg = (
                f"{badge}\n\n"
                f"📍 **Pair:** {trade['symbol']} ({signal})\n"
                f"🎯 **Type:** {trade['strat_type']}\n"
                f"🎯 **Instapkoers (Target to Beat):** `${entry_price}`\n"
                f"🏁 **Eindkoers:** `${current_price}` ({diff_str})\n"
                f"📊 **Aangekochte Odds:** `${share_price:.2f}` per aandeel\n"
                f"💰 **Nieuw Paper Saldo:** `${PAPER_BALANCE:.2f}`"
            )
            send_telegram_msg(msg)
        else:
            remaining_trades.append(trade)

    active_paper_trades = remaining_trades

# —————————————————————————
# MAIN LOOP
# —————————————————————————

def main():
    print("🤖 Poly Bot v4.5 (15M Multi-Timeframe Active)...")
    send_telegram_msg(
        f"🤖 **Poly Bot Online (Versie v4.5 - Multi-Timeframe)!**\n"
        f"💰 Start Saldo: `${PAPER_BALANCE:.2f}` | Inzet per Trade: `${TRADE_AMOUNT:.2f}`\n"
        f"📊 **15M + 5M Filter Actief:** Geen trades meer openen tegen de 15-minuten trend in voor álle munten!"
    )

    while True:
        try:
            now_sec = time.time()
            time_in_5m_cycle = now_sec % 300
            time_remaining_sec = 300 - time_in_5m_cycle
            cycle_id = int(now_sec // 300)

            check_and_evaluate_paper_trades()

            current_cycle_trades_count = sum(
                1 for key in active_signals_sent if key.startswith(f"cycle_{cycle_id}_")
            )

            if current_cycle_trades_count < MAX_TRADES_PER_CYCLE:
                for symbol in PAIRS:
                    if current_cycle_trades_count >= MAX_TRADES_PER_CYCLE:
                        break

                    price, pct_change, rsi, prev_rsi, vol_ratio = fetch_binance_data(symbol)

                    if price is None:
                        continue

                    signal, strat_type, reason = analyze_market_and_get_signal(
                        symbol, rsi, prev_rsi, pct_change, vol_ratio, time_remaining_sec
                    )

                    if signal:
                        trade_key = f"cycle_{cycle_id}_{symbol}_{strat_type}"

                        if trade_key not in active_signals_sent:
                            active_signals_sent[trade_key] = True
                            current_cycle_trades_count += 1

                            share_price = calculate_dynamic_polymarket_price(rsi, pct_change, strat_type, signal)
                            shares_bought = round(TRADE_AMOUNT / share_price, 2)
                            potential_profit = round((shares_bought * 1.00) - TRADE_AMOUNT, 2)

                            expiry_time = now_sec + time_remaining_sec
                            active_paper_trades.append({
                                "symbol": symbol,
                                "signal": signal,
                                "strat_type": strat_type,
                                "entry_price": price,
                                "share_price": share_price,
                                "shares_bought": shares_bought,
                                "expiry_time": expiry_time
                            })

                            mins = int(time_remaining_sec // 60)
                            secs = int(time_remaining_sec % 60)
                            
                            direction_icon = "📈" if signal == "UP" else "📉"
                            target_condition = f"Hoger dan ${price}" if signal == "UP" else f"Lager dan ${price}"
                            type_badge = "🔥 MOMENTUM (15M MATCH)" if strat_type == "MOMENTUM" else "🔄 PULLBACK"

                            msg = (
                                f"🚨 **5M EXPIRY {type_badge}!** {direction_icon}\n\n"
                                f"📍 **Pair:** {symbol}\n"
                                f"🎯 **Price to Beat:** `${price}`\n"
                                f"👉 **Inzetten:** **{signal}** ({target_condition})\n\n"
                                f"🏷️ **Odds:** `${share_price:.2f}` per aandeel (`{shares_bought}` aandelen)\n"
                                f"💵 **Mogelijke Winst:** `+${potential_profit:.2f}`\n"
                                f"📊 **RSI 5M:** `{rsi}` | **Vol Ratio:** `{vol_ratio}x` | **Move:** `{pct_change}%`\n"
                                f"⏱️ **Resterend:** `~{mins}m {secs}s`\n\n"
                                f"💡 **Analyse:**\n"
                                f"• {reason}\n\n"
                                f"📝 *[Paper Trade Geopend: Inzet ${TRADE_AMOUNT}]*"
                            )
                            
                            send_telegram_msg(msg, button_url="https://polymarket.com")
                            print(f"🚀 Signaal gestart voor {symbol}: {signal}")

            if len(active_signals_sent) > 100:
                active_signals_sent.clear()

        except Exception as e:
            print(f"Error in main loop: {e}")

        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
