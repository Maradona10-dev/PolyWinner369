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
POLYMARKET_API_URL = "https://clob.polymarket.com"
SCAN_INTERVAL_SEC = 10  # Scanningsinterval in seconden

# Te scannen handelsparen
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ZECUSDT"]

# Maximale toegestane trades per 5-minuten cyclus
MAX_TRADES_PER_CYCLE = 2

# —————————————————————————
# PAPER TRADING INSTELLINGEN
# —————————————————————————
PAPER_BALANCE = 1000.0  # Startkapitaal in USD
TRADE_AMOUNT = 25.0     # Inzet per virtuele trade in USD
active_paper_trades = []
active_signals_sent = {}

# —————————————————————————
# TELEGRAM BERICHTEN STUREN
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
        else:
            print("✅ Telegram bericht verzonden!")
    except Exception as e:
        print(f"❌ Fout bij versturen Telegram: {e}")

# —————————————————————————
# POLYMARKET LIVE PRIJS OPHALEN
# —————————————————————————

def get_polymarket_share_price(symbol, signal):
    """
    Haalt de actuele orderboekprijs/odds op van Polymarket CLOB.
    Valt terug op een reële schatting als de API niet direct reageert.
    """
    try:
        clean_symbol = symbol.replace("USDT", "")
        url = f"{POLYMARKET_API_URL}/markets"
        res = requests.get(url, params={"query": clean_symbol}, timeout=3).json()
        
        if res and isinstance(res, list) and len(res) > 0:
            tokens = res[0].get("tokens", [])
            for token in tokens:
                if token.get("outcome", "").upper() == signal.upper():
                    price = float(token.get("price", 0.55))
                    return max(0.10, min(0.90, price))
    except Exception as e:
        print(f"Ophalen Polymarket prijs mislukt voor {symbol}, standaardinschatting gebruikt: {e}")
    
    return 0.55

# —————————————————————————
# RSI BEREKENING LOGICA
# —————————————————————————

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0

    gains = []
    losses = []

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
# ORDERBOOK LIQUIDITEIT & DRUK CHECK
# —————————————————————————

def check_orderbook_imbalance(symbol):
    try:
        url = f"{BINANCE_API_URL}/depth?symbol={symbol}&limit=20"
        res = requests.get(url, timeout=3).json()

        bid_volume = sum([float(b[1]) for b in res.get('bids', [])])
        ask_volume = sum([float(a[1]) for a in res.get('asks', [])])

        if ask_volume == 0 or bid_volume == 0:
            return "NEUTRAL"

        ratio = bid_volume / ask_volume

        if ratio >= 1.8:
            return "BULLISH"
        elif ratio <= 0.55:
            return "BEARISH"

        return "NEUTRAL"
    except Exception as e:
        print(f"Fout bij orderboek check voor {symbol}: {e}")
        return "NEUTRAL"

# —————————————————————————
# GEAVANCEERDE STRATEGIE MARKTANALYSE
# —————————————————————————

def analyze_market_and_get_signal(symbol, rsi, percent_change, time_remaining_sec):
    ob_status = check_orderbook_imbalance(symbol)

    # FASE 1: VROEG MOMENTUM (3:30m tot 5:00m te gaan / 210s - 300s)
    if 210 <= time_remaining_sec <= 300:
        min_move = 0.40 if symbol == "ZECUSDT" else 0.30

        if 52 <= rsi <= 68 and percent_change >= min_move and ob_status == "BULLISH":
            reason = f"🚀 Fase 1: Vroeg Bullish Momentum (+{percent_change}%) + Koopmuur!"
            return "UP", "MOMENTUM", reason

        elif 32 <= rsi <= 48 and percent_change <= -min_move and ob_status == "BEARISH":
            reason = f"🚀 Fase 1: Vroeg Bearish Momentum ({percent_change}%) + Verkoopmuur!"
            return "DOWN", "MOMENTUM", reason

    # FASE 2: EXTREME PULLBACK (1:00m tot 2:30m te gaan / 60s - 150s)
    elif 60 <= time_remaining_sec <= 150:
        if rsi >= 78 and percent_change >= 0.50:
            reason = f"🔄 Fase 2: Extreme Piek (RSI {rsi} >= 78 & Move {percent_change}% >= 0.50%) -> Trade Pullback DOWN!"
            return "DOWN", "PULLBACK", reason

        elif rsi <= 22 and percent_change <= -0.50:
            reason = f"🔄 Fase 2: Extreme Dip (RSI {rsi} <= 22 & Move {percent_change}% <= -0.50%) -> Trade Bounce UP!"
            return "UP", "PULLBACK", reason

    return None, None, "Geen geschikt signaal"

# —————————————————————————
# BINANCE DATA OPHALEN
# —————————————————————————

def fetch_binance_data(symbol):
    try:
        url = f"{BINANCE_API_URL}/klines?symbol={symbol}&interval=1m&limit=30"
        res = requests.get(url, timeout=5).json()

        close_prices = [float(k[4]) for k in res]
        current_price = close_prices[-1]
        start_price = close_prices[-5]

        percent_change = round(((current_price - start_price) / start_price) * 100, 2)
        rsi = calculate_rsi(close_prices)

        return current_price, percent_change, rsi
    except Exception as e:
        print(f"Fout bij ophalen data voor {symbol}: {e}")
        return None, None, None

# —————————————————————————
# PAPER TRADING ENGINE (INCL. KOERS-DIF BEREKENING)
# —————————————————————————

def check_and_evaluate_paper_trades():
    global PAPER_BALANCE, active_paper_trades
    now_ts = time.time()
    remaining_trades = []

    for trade in active_paper_trades:
        if now_ts >= trade["expiry_time"]:
            current_price, _, _ = fetch_binance_data(trade["symbol"])
            if current_price is None:
                remaining_trades.append(trade)
                continue

            entry_price = trade["entry_price"]
            signal = trade["signal"]
            share_price = trade["share_price"]
            shares_bought = trade["shares_bought"]
            
            # Bereken het exacte prijsverschil
            price_diff = round(current_price - entry_price, 4)
            pct_diff = round(((current_price - entry_price) / entry_price) * 100, 2)
            
            is_win = False
            if signal == "UP" and current_price > entry_price:
                is_win = True
            elif signal == "DOWN" and current_price < entry_price:
                is_win = True

            if is_win:
                payout = shares_bought * 1.00
                profit = payout - TRADE_AMOUNT
                PAPER_BALANCE += profit
                badge = f"✅ **PAPER TRADE GEWONNEN!** (+${profit:.2f})"
            else:
                PAPER_BALANCE -= TRADE_AMOUNT
                profit = -TRADE_AMOUNT
                badge = f"❌ **PAPER TRADE VERLOREN** (-${TRADE_AMOUNT:.2f})"

            diff_str = f"+${price_diff} (+{pct_diff}%)" if price_diff > 0 else f"-${abs(price_diff)} ({pct_diff}%)"

            msg = (
                f"{badge}\n\n"
                f"📍 **Pair:** {trade['symbol']} ({signal})\n"
                f"🎯 **Type:** {trade['strat_type']}\n"
                f"🎯 **Instapkoers (Target to Beat):** `${entry_price}`\n"
                f"🏁 **Eindkoers:** `${current_price}` ({diff_str})\n"
                f"📊 **Gekochte Odds:** `${share_price:.2f}` per aandeel\n"
                f"💰 **Nieuw Paper Saldo:** `${PAPER_BALANCE:.2f}`"
            )
            send_telegram_msg(msg)
            print(f"Paper trade afgerond voor {trade['symbol']}: {'WIN' if is_win else 'LOSS'}")
        else:
            remaining_trades.append(trade)

    active_paper_trades = remaining_trades

# —————————————————————————
# MAIN BOT LOOP
# —————————————————————————

def main():
    print("🤖 Poly Bot Gestart met duidelijke prijsnotering...")
    send_telegram_msg(
        f"🤖 **Poly Bot Online (Versie v3.1)!**\n"
        f"💰 Start Saldo: `${PAPER_BALANCE:.2f}` | Inzet per Trade: `${TRADE_AMOUNT:.2f}`\n"
        f"🎯 **Prijsnotering:** Toont nu exact de 'price to beat' en de Polymarket odds bij de geopende trade."
    )

    while True:
        try:
            now_sec = time.time()
            time_in_5m_cycle = now_sec % 300
            time_remaining_sec = 300 - time_in_5m_cycle
            cycle_id = int(now_sec // 300)

            # 1. Evalueer openstaande trades
            check_and_evaluate_paper_trades()

            current_cycle_trades_count = sum(
                1 for key in active_signals_sent if key.startswith(f"cycle_{cycle_id}_")
            )

            # 2. Scannen voor nieuwe trades
            if current_cycle_trades_count < MAX_TRADES_PER_CYCLE:
                for symbol in PAIRS:
                    if current_cycle_trades_count >= MAX_TRADES_PER_CYCLE:
                        break

                    price, pct_change, rsi = fetch_binance_data(symbol)

                    if price is None:
                        continue

                    signal, strat_type, reason = analyze_market_and_get_signal(
                        symbol, rsi, pct_change, time_remaining_sec
                    )

                    if signal:
                        trade_key = f"cycle_{cycle_id}_{symbol}_{strat_type}"

                        if trade_key not in active_signals_sent:
                            active_signals_sent[trade_key] = True
                            current_cycle_trades_count += 1

                            share_price = get_polymarket_share_price(symbol, signal)
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
                            type_badge = "🔥 MOMENTUM SIGNAAL" if strat_type == "MOMENTUM" else "🔄 PULLBACK / REVERSION SIGNAAL"

                            msg = (
                                f"🚨 **5M EXPIRY {type_badge}!** {direction_icon}\n\n"
                                f"📍 **Pair:** {symbol}\n"
                                f"🎯 **Price to Beat (Instapkoers):** `${price}`\n"
                                f"👉 **Inzetten op Polymarket:** **{signal}** ({target_condition})\n\n"
                                f"🏷️ **Polymarket Odds:** `${share_price:.2f}` per aandeel (`{shares_bought}` aandelen)\n"
                                f"💵 **Mogelijke Winst bij Win:** `+${potential_profit:.2f}`\n"
                                f"📊 **RSI:** `{rsi}` | **3-5M Move:** `{pct_change}%`\n"
                                f"⏱️ **Tijd op Klok:** `~{mins}m {secs}s`\n\n"
                                f"💡 **Strategie Advies:**\n"
                                f"• {reason}\n\n"
                                f"📝 *[Paper Trade Geopend: Inzet ${TRADE_AMOUNT}]*"
                            )
                            
                            polymarket_url = "https://polymarket.com"
                            send_telegram_msg(msg, button_url=polymarket_url)
                            print(f"🚀 Signaal gestart voor {symbol}: {signal} (Target: ${price})")

            if len(active_signals_sent) > 100:
                active_signals_sent.clear()

        except Exception as e:
            print(f"Error in main loop: {e}")

        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
