import os
import time
import json
import requests
import datetime

# —————————————————————————
# CONFIGURATIE (2-MINUTEN EXPIRY MOMENTUM STRATEGIE)
# —————————————————————————

RAW_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN.replace('bot', '', 1) if RAW_TOKEN.startswith('bot') else RAW_TOKEN
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()

POLYMARKET_URL = os.environ.get('POLYMARKET_URL', 'https://polymarket.com/crypto/5M')

INZET_PER_TRADE = float(os.environ.get("INZET_PER_TRADE", "10.0"))
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", "1000.0"))

# Minimale uitslag in % in de huidige candle (0.12% komt bij BTC overeen met ~$95)
MIN_CANDLE_MOVE_PCT = float(os.environ.get("MIN_CANDLE_MOVE_PCT", "0.12"))

# RSI drempels voor momentum bevestiging
RSI_UP_THRESHOLD = 52.0
RSI_DOWN_THRESHOLD = 48.0

# Vaste window: Instappen rond 2 minuten voor expiry (tussen 100s en 135s resterend)
MIN_TIME_REMAINING_SEC = 100
MAX_TIME_REMAINING_SEC = 135

SCAN_INTERVAL_SEC = 10
PAYOUT_MULTIPLIER = 1.85
VOLUME_MULTIPLIER = 1.20

DATA_FILE = os.environ.get("DATA_FILE", "portfolio.json")

# Uitgebreide lijst met handelsparen
DEFAULT_SYMBOLS = (
    "BTC/USDT:BTCUSDT,"
    "ETH/USDT:ETHUSDT,"
    "SOL/USDT:SOLUSDT,"
    "HYPE/USDT:HYPEUSDT,"
    "BNB/USDT:BNBUSDT,"
    "XRP/USDT:XRPUSDT,"
    "ZEC/USDT:ZECUSDT,"
    "DOGE/USDT:DOGEUSDT"
)

SYMBOLS_RAW = os.environ.get("SYMBOLS", DEFAULT_SYMBOLS)
SYMBOLS = dict(pair.split(":") for pair in SYMBOLS_RAW.split(","))

# —————————————————————————
# HELPER UTILITIES
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

def get_binance_candle_data(symbol_code):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol_code}&interval=5m&limit=50"
        res = requests.get(url, timeout=5).json()

        if not isinstance(res, list) or len(res) < 15:
            return None, "Geen geldige data van Binance"

        # Huidige actieve 5M candle
        current_candle = res[-1]
        open_price = float(current_candle[1])
        live_price = float(current_candle[4])
        current_volume = float(current_candle[5])

        # Bereken de koersuitslag in de huidige candle in %
        price_move_pct = ((live_price - open_price) / open_price) * 100
        abs_dollar_move = abs(live_price - open_price)

        # Historische data voor indicatoren
        closes = [float(c[4]) for c in res]
        volumes = [float(c[5]) for c in res]

        # EMAs
        ema5 = sum(closes[-5:]) / 5
        ema15 = sum(closes[-15:]) / 15

        # RSI
        period = 14
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(0, d) for d in deltas[-period:]]
        losses = [max(0, -d) for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period or 1e-9
        rsi = 100 - (100 / (1 + (avg_gain / avg_loss)))

        # Volume
        avg_volume = sum(volumes[-10:-1]) / 9
        volume_ok = current_volume > (avg_volume * VOLUME_MULTIPLIER)

        return {
            "price": live_price,
            "open_price": open_price,
            "dollar_move": abs_dollar_move,
            "move_pct": price_move_pct,
            "ema5": ema5,
            "ema15": ema15,
            "rsi": rsi,
            "volume_ok": volume_ok
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
    print(f"🚀 2-Minuten Expiry Momentum Bot Gestart voor {len(SYMBOLS)} munten...")

    clear_telegram_webhooks()
    time.sleep(1)

    munt_namen = ", ".join(SYMBOLS.keys())
    send_telegram_msg(
        f"⚡ **2-Minuten Expiry Momentum Bot Actief**\n"
        f"• Munten ({len(SYMBOLS)}): `{munt_namen}`\n"
        f"• Entry Window: **~2m voor expiry**\n"
        f"• Min. Interval Move: `{MIN_CANDLE_MOVE_PCT}%`\n"
        f"• Huidig Saldo: `${PAPER_BALANCE:.2f}`"
    )

    last_scan_time = 0

    while True:
        if time.time() - last_scan_time > SCAN_INTERVAL_SEC:
            last_scan_time = time.time()
            remaining_seconds, time_str = get_remaining_5m_time()

            # EXACT WINDOW: Alleen scannen wanneer er rond de 2 minuten over zijn (100s - 135s)
            if MIN_TIME_REMAINING_SEC <= remaining_seconds <= MAX_TIME_REMAINING_SEC:
                for pair_name, symbol_code in SYMBOLS.items():
                    if pair_name in active_trades:
                        continue

                    data, _ = get_binance_candle_data(symbol_code)
                    if data:
                        price = data["price"]
                        move_pct = data["move_pct"]
                        dollar_move = data["dollar_move"]
                        ema5 = data["ema5"]
                        ema15 = data["ema15"]
                        rsi = data["rsi"]
                        volume_ok = data["volume_ok"]

                        # Voorwaarde 1: Voldoende koersuitslag in de huidige 5M ronde
                        has_enough_movement = abs(move_pct) >= MIN_CANDLE_MOVE_PCT

                        # Voorwaarde 2: Momentum in de richting van de uitslag
                        is_up_momentum = (move_pct > 0) and (ema5 > ema15) and (rsi > RSI_UP_THRESHOLD)
                        is_down_momentum = (move_pct < 0) and (ema5 < ema15) and (rsi < RSI_DOWN_THRESHOLD)

                        direction = None
                        if has_enough_movement and is_up_momentum and volume_ok:
                            direction = "UP"
                        elif has_enough_movement and is_down_momentum and volume_ok:
                            direction = "DOWN"

                        if direction:
                            emoji = "📈" if direction == "UP" else "📉"
                            signal_msg = (
                                f"🎯 **2M EXPIRY MOMENTUM SIGNAAL ({direction})**\n{emoji}\n"
                                f"Pair: {pair_name}\n"
                                f"Beweging in ronde: `{move_pct:+.2f}%` (${dollar_move:,.2f})\n"
                                f"Huidige Koers: `${price:,.4f}`\n"
                                f"RSI: `{rsi:.1f}` | Vol: `OK`\n"
                                f"Tijd resterend: ~{time_str}\n\n"
                                f"💡 **Advies:** Volg het momentum in op Polymarket ('{direction}')"
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

        # Afhandelen geopende trades bij expiry
        finished_pairs = [p for p, t in active_trades.items() if time.time() - t["start_time"] >= t["duration"]]
        for pair_name in finished_pairs:
            trade = active_trades[pair_name]
            current_data, _ = get_binance_candle_data(trade["symbol"])
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
                f"Entry: `${trade['entry']:,.4f}` ➔ Exit: `${exit_price:,.4f}`\n"
                f"Nieuw Saldo: `${PAPER_BALANCE:.2f}`"
            )
            del active_trades[pair_name]

        time.sleep(2)

if __name__ == "__main__":
    main()
