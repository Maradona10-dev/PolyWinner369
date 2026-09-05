import os
import time
import json
import requests
import datetime

# —————————————————————————
# CONFIGURATIE (STRENGERE AFSTELLING)
# —————————————————————————

RAW_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN.replace('bot', '', 1) if RAW_TOKEN.startswith('bot') else RAW_TOKEN
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()

POLYMARKET_URL = os.environ.get('POLYMARKET_URL', 'https://polymarket.com/crypto/5M')

INZET_PER_TRADE = float(os.environ.get("INZET_PER_TRADE", "10.0"))
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", "1000.0"))

# Vaste harde drempel: minimaal 0.45% edge vereist
MIN_EDGE_THRESHOLD = float(os.environ.get("MIN_EDGE_THRESHOLD", "0.45"))

# RSI drempels voor momentum
RSI_UP_THRESHOLD = float(os.environ.get("RSI_UP_THRESHOLD", "54.0"))
RSI_DOWN_THRESHOLD = float(os.environ.get("RSI_DOWN_THRESHOLD", "46.0"))

# Alleen traden tussen 120s en 240s resterend in de 5M candle
MIN_TIME_REMAINING_SEC = int(os.environ.get("MIN_TIME_REMAINING_SEC", "120"))
MAX_TIME_REMAINING_SEC = int(os.environ.get("MAX_TIME_REMAINING_SEC", "250"))

SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "15"))
PAYOUT_MULTIPLIER = float(os.environ.get("PAYOUT_MULTIPLIER", "1.85"))
VOLUME_MULTIPLIER = float(os.environ.get("VOLUME_MULTIPLIER", "1.25"))

DATA_FILE = os.environ.get("DATA_FILE", "portfolio.json")

# MACD & BB Instellingen
MACD_FAST = 6
MACD_SLOW = 13
MACD_SIGNAL = 4
BB_PERIOD = 20
BB_STD_DEV = 2.0

SYMBOLS_RAW = os.environ.get("SYMBOLS", "BTC/USDT:BTCUSDT,ETH/USDT:ETHUSDT,SOL/USDT:SOLUSDT")
SYMBOLS = dict(pair.split(":") for pair in SYMBOLS_RAW.split(","))

# —————————————————————————
# PERSISTENTIE & HELPER FUNCTIONS
# —————————————————————————

def load_portfolio():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                return data.get("balance", STARTING_BALANCE), data.get("total_trades", 0)
        except Exception as e:
            print(f"Fout bij laden portfolio: {e}")
            return STARTING_BALANCE, 0
    return STARTING_BALANCE, 0

def save_portfolio(balance, total_trades):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump({"balance": balance, "total_trades": total_trades}, f)
    except Exception as e:
        print(f"Fout bij opslaan portfolio: {e}")

PAPER_BALANCE, TOTAL_TRADES = load_portfolio()
active_trades = {}

def clear_telegram_webhooks():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook?drop_pending_updates=True"
    try:
        requests.get(url, timeout=5)
    except Exception:
        pass

def send_telegram_msg(message, button_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print(f"Telegram error: {e}")

def calculate_ema_series(values, period):
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    ema_series = [sum(values[:period]) / period]
    for price in values[period:]:
        ema_series.append((price - ema_series[-1]) * multiplier + ema_series[-1])
    return ema_series

def get_binance_data(symbol_code):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol_code}&interval=5m&limit=100"
        res = requests.get(url, timeout=5).json()

        closes = [float(c[4]) for c in res]
        volumes = [float(c[5]) for c in res]
        live_price = closes[-1]

        # EMAs
        ema5 = round(sum(closes[-5:]) / 5, 2)
        ema15 = round(sum(closes[-15:]) / 15, 2)
        ema50 = round(sum(closes[-50:]) / 50, 2)

        # RSI (Wilder's Smoothing)
        period = 14
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        seed_gains = [max(0, d) for d in deltas[:period]]
        seed_losses = [max(0, -d) for d in deltas[:period]]
        avg_gain = sum(seed_gains) / period
        avg_loss = sum(seed_losses) / period

        for d in deltas[period:]:
            gain = max(0, d)
            loss = max(0, -d)
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        avg_loss = avg_loss or 1e-9
        rsi = round(100 - (100 / (1 + (avg_gain / avg_loss))), 1)

        # Volume Check
        avg_volume = sum(volumes[-10:-1]) / 9
        current_volume = volumes[-1]
        volume_ok = current_volume > (avg_volume * VOLUME_MULTIPLIER)

        # Berekende Edge
        ema_spread_pct = ((ema5 - ema15) / ema15) * 100
        rsi_bonus = max(0, (rsi - 50) * 0.015) if ema_spread_pct >= 0 else max(0, (50 - rsi) * 0.015)
        calculated_edge = round(abs(ema_spread_pct) + rsi_bonus, 2)

        # MACD
        macd_signal_cross = None
        ema_fast_series = calculate_ema_series(closes, MACD_FAST)
        ema_slow_series = calculate_ema_series(closes, MACD_SLOW)

        if len(ema_fast_series) > 1 and len(ema_slow_series) > 1:
            offset = len(ema_fast_series) - len(ema_slow_series)
            aligned_fast = ema_fast_series[offset:] if offset > 0 else ema_fast_series
            aligned_slow = ema_slow_series[-offset:] if offset < 0 else ema_slow_series
            macd_line = [f - s for f, s in zip(aligned_fast, aligned_slow)]

            signal_line_series = calculate_ema_series(macd_line, MACD_SIGNAL)
            if len(signal_line_series) >= 2 and len(macd_line) >= len(signal_line_series):
                if macd_line[-2] <= signal_line_series[-2] and macd_line[-1] > signal_line_series[-1]:
                    macd_signal_cross = "UP"
                elif macd_line[-2] >= signal_line_series[-2] and macd_line[-1] < signal_line_series[-1]:
                    macd_signal_cross = "DOWN"

        # Bollinger Bands
        bb_signal = None
        if len(closes) >= BB_PERIOD:
            bb_window = closes[-BB_PERIOD:]
            bb_mid = sum(bb_window) / BB_PERIOD
            variance = sum((c - bb_mid) ** 2 for c in bb_window) / BB_PERIOD
            bb_std = variance ** 0.5
            if live_price > (bb_mid + (BB_STD_DEV * bb_std)) and volume_ok:
                bb_signal = "UP"
            elif live_price < (bb_mid - (BB_STD_DEV * bb_std)) and volume_ok:
                bb_signal = "DOWN"

        return {
            "price": live_price,
            "ema5": ema5,
            "ema15": ema15,
            "ema50": ema50,
            "rsi": rsi,
            "volume_ok": volume_ok,
            "edge": calculated_edge,
            "macd_signal_cross": macd_signal_cross,
            "bb_signal": bb_signal,
        }, None
    except Exception as e:
        return None, str(e)

def get_remaining_5m_time():
    now = datetime.datetime.now(datetime.timezone.utc)
    seconds_past_5m = (now.minute % 5) * 60 + now.second
    remaining_seconds = 300 - seconds_past_5m
    minutes = remaining_seconds // 60
    secs = remaining_seconds % 60
    return remaining_seconds, f"{minutes}m {secs}s"

# —————————————————————————
# MAIN LOOP
# —————————————————————————

def main():
    global PAPER_BALANCE, TOTAL_TRADES, active_trades
    print(f"🚀 Quant Bot gestart met verhoogde winst-drempel ({MIN_EDGE_THRESHOLD}%)...")

    clear_telegram_webhooks()
    time.sleep(1)

    send_telegram_msg(
        f"⚙️ **Poly Bot Herstart:**\n"
        f"• Min. Edge Drempel: `{MIN_EDGE_THRESHOLD}%`\n"
        f"• Eisen: **Minimaal 2 Indicatoren tegelijk**\n"
        f"• Huidig Saldo: `${PAPER_BALANCE:.2f}`"
    )

    last_scan_time = 0

    while True:
        if time.time() - last_scan_time > SCAN_INTERVAL_SEC:
            last_scan_time = time.time()
            remaining_seconds, time_str = get_remaining_5m_time()

            # Alleen scannen in het juiste tijdsvenster
            if MIN_TIME_REMAINING_SEC <= remaining_seconds <= MAX_TIME_REMAINING_SEC:
                for pair_name, symbol_code in SYMBOLS.items():
                    if pair_name in active_trades:
                        continue

                    data, _ = get_binance_data(symbol_code)
                    if data:
                        price = data["price"]
                        ema5 = data["ema5"]
                        ema15 = data["ema15"]
                        ema50 = data["ema50"]
                        rsi = data["rsi"]
                        volume_ok = data["volume_ok"]
                        edge = data["edge"]
                        macd_cross = data["macd_signal_cross"]
                        bb_signal = data["bb_signal"]

                        # 1. EMA Trend & RSI Confluentie
                        ema_rsi_up = ema5 > ema15 and rsi > RSI_UP_THRESHOLD and price > ema50 and volume_ok
                        ema_rsi_down = ema5 < ema15 and rsi < RSI_DOWN_THRESHOLD and price < ema50 and volume_ok

                        up_sources = []
                        down_sources = []
                        if ema_rsi_up: up_sources.append("EMA/RSI")
                        if ema_rsi_down: down_sources.append("EMA/RSI")
                        if macd_cross == "UP" and price > ema50: up_sources.append("MACD")
                        elif macd_cross == "DOWN" and price < ema50: down_sources.append("MACD")
                        if bb_signal == "UP" and price > ema50: up_sources.append("Bollinger")
                        elif bb_signal == "DOWN" and price < ema50: down_sources.append("Bollinger")

                        direction = None
                        sources = []
                        if up_sources and not down_sources:
                            direction = "UP"
                            sources = up_sources
                        elif down_sources and not up_sources:
                            direction = "DOWN"
                            sources = down_sources

                        # STRENGERE EIS: Er MOETEN minstens 2 bronnen zijn (bijv. MACD + EMA/RSI)!
                        is_strong_confluence = len(sources) >= 2
                        passes_threshold = is_strong_confluence and (edge >= MIN_EDGE_THRESHOLD)

                        if passes_threshold:
                            sources_str = " + ".join(sources)
                            emoji = "📈" if direction == "UP" else "📉"

                            signal_msg = (
                                f"🚀 **GEVALIDERD HIGH-CONFIDENCE SIGNAAL ({direction})**\n{emoji}\n"
                                f"Pair: {pair_name}\n"
                                f"Bron(nen): `{sources_str}`\n"
                                f"Gemeten Edge: `{edge}%` (Filter: >{MIN_EDGE_THRESHOLD}%)\n"
                                f"Koers: `${price:,.2f}`\n"
                                f"Tijd in ronde: ~{time_str}\n\n"
                                f"💡 **Inzet-advies:** Zet in op Polymarket op '{direction}'"
                            )
                            send_telegram_msg(signal_msg, button_url=POLYMARKET_URL)

                            PAPER_BALANCE -= INZET_PER_TRADE
                            TOTAL_TRADES += 1
                            save_portfolio(PAPER_BALANCE, TOTAL_TRADES)

                            active_trades[pair_name] = {
                                "pair": pair_name,
                                "symbol": symbol_code,
                                "entry": price,
                                "direction": direction,
                                "start_time": time.time(),
                                "duration": remaining_seconds,
                            }

        # Afhandelen geopende trades
        finished_pairs = [p for p, t in active_trades.items() if time.time() - t["start_time"] >= t["duration"]]
        for pair_name in finished_pairs:
            trade = active_trades[pair_name]
            current_data, _ = get_binance_data(trade["symbol"])
            exit_price = current_data["price"] if current_data else trade["entry"]

            win = (exit_price > trade["entry"]) if trade["direction"] == "UP" else (exit_price < trade["entry"])

            if win:
                PAPER_BALANCE += INZET_PER_TRADE * PAYOUT_MULTIPLIER
                res_txt = "✅ **WIN** 🎉"
            else:
                res_txt = "❌ **VERLIES** 📉"

            save_portfolio(PAPER_BALANCE, TOTAL_TRADES)
            send_telegram_msg(
                f"{res_txt}\n\n"
                f"Trade #{TOTAL_TRADES} ({trade['pair']} - {trade['direction']})\n"
                f"Entry: `${trade['entry']:,.2f}` ➔ Exit: `${exit_price:,.2f}`\n"
                f"Nieuw Saldo: `${PAPER_BALANCE:.2f}`"
            )
            del active_trades[pair_name]

        time.sleep(2)

if __name__ == "__main__":
    main()
