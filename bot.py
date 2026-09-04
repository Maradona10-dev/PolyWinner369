import os
import time
import json
import requests
import datetime

# ---------------------------------------------------------------------------
# CONFIGURATIE (RAILWAY ENVIRONMENT VARIABLES)
# ---------------------------------------------------------------------------
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN.strip().replace("bot", "") if RAW_TOKEN.strip().startswith("bot") else RAW_TOKEN.strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

POLYMARKET_URL = os.environ.get("POLYMARKET_URL", "https://polymarket.com/crypto/5M")

# --- Trading parameters (allemaal instelbaar via env vars) ---
INZET_PER_TRADE = float(os.environ.get("INZET_PER_TRADE", "10.0"))
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", "1000.0"))
MIN_EDGE_THRESHOLD = float(os.environ.get("MIN_EDGE_THRESHOLD", "0.25"))
RSI_UP_THRESHOLD = float(os.environ.get("RSI_UP_THRESHOLD", os.environ.get("RSI_THRESHOLD", "60")))
RSI_DOWN_THRESHOLD = float(os.environ.get("RSI_DOWN_THRESHOLD", "40"))
MIN_TIME_REMAINING_SEC = int(os.environ.get("MIN_TIME_REMAINING_SEC", "120"))
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "15"))
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("HEARTBEAT_INTERVAL_SEC", "3600"))
PAYOUT_MULTIPLIER = float(os.environ.get("PAYOUT_MULTIPLIER", "1.85"))
VOLUME_MULTIPLIER = float(os.environ.get("VOLUME_MULTIPLIER", "1.1"))

DATA_FILE = os.environ.get("DATA_FILE", "portfolio.json")
EDGE_LOG_FILE = os.environ.get("EDGE_LOG_FILE", "edge_log.jsonl")

# --- MACD instellingen (fast-settings voor 5m timeframe, i.p.v. standaard 12-26-9) ---
MACD_FAST = int(os.environ.get("MACD_FAST", "6"))
MACD_SLOW = int(os.environ.get("MACD_SLOW", "13"))
MACD_SIGNAL = int(os.environ.get("MACD_SIGNAL", "4"))

# --- Bollinger Bands instellingen ---
BB_PERIOD = int(os.environ.get("BB_PERIOD", "20"))
BB_STD_DEV = float(os.environ.get("BB_STD_DEV", "2.0"))

# Config voor SYMBOLS via env var: "BTC/USDT:BTCUSDT,ETH/USDT:ETHUSDT,SOL/USDT:SOLUSDT"
SYMBOLS_RAW = os.environ.get(
    "SYMBOLS",
    "BTC/USDT:BTCUSDT,ETH/USDT:ETHUSDT,SOL/USDT:SOLUSDT"
)
SYMBOLS = dict(pair.split(":") for pair in SYMBOLS_RAW.split(","))

# LOG_ALL_SIGNALS: als True, loggen we ook edges die ONDER de threshold zitten
# (nodig om te bepalen of een lagere threshold zinvol zou zijn)
LOG_ALL_SIGNALS = os.environ.get("LOG_ALL_SIGNALS", "true").lower() == "true"

# ---------------------------------------------------------------------------
# PERSISTENTIE: DATA OPSLAAN EN INLADEN
# ---------------------------------------------------------------------------
def load_portfolio():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                return data.get("balance", STARTING_BALANCE), data.get("total_trades", 0)
        except Exception as e:
            print(f"Fout bij laden portfolio: {e}")
    return STARTING_BALANCE, 0

def save_portfolio(balance, total_trades):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump({"balance": balance, "total_trades": total_trades}, f)
    except Exception as e:
        print(f"Fout bij opslaan portfolio: {e}")

PAPER_BALANCE, TOTAL_TRADES = load_portfolio()
# Eén actieve trade per symbool i.p.v. één gedeelde slot voor alle assets
active_trades = {}

# ---------------------------------------------------------------------------
# EDGE LOGGING (voor threshold-analyse)
# ---------------------------------------------------------------------------
def log_edge_observation(pair, edge, rsi, volume_ok, passed_threshold, event, extra=None):
    """
    Schrijft één regel JSON per observatie naar EDGE_LOG_FILE (JSON Lines formaat).
    event kan zijn: 'scan' (elke keer we een edge meten, ook als die niet triggert),
                    'trade_open', 'trade_close'
    Dit bestand kun je later inladen (bv. met pandas) om te zien bij welke edge-range
    de win-rate het hoogst is, i.p.v. de threshold op gevoel te kiezen.
    """
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "pair": pair,
        "edge": edge,
        "rsi": rsi,
        "volume_ok": volume_ok,
        "passed_threshold": passed_threshold,
        "threshold_used": MIN_EDGE_THRESHOLD,
        "event": event,
    }
    if extra:
        entry.update(extra)
    try:
        with open(EDGE_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"Fout bij loggen edge observatie: {e}")

# ---------------------------------------------------------------------------
# TELEGRAM UTILITIES
# ---------------------------------------------------------------------------
def clear_telegram_webhooks():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook?drop_pending_updates=True"
    try:
        requests.get(url, timeout=5)
    except Exception as e:
        print(f"Webhook error: {e}")

def send_telegram_msg(message, button_url=None, button_text="🚀 ZET DIRECT IN OP POLYMARKET"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram credentials missing")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }

    if button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": button_text, "url": button_url}
            ]]
        }

    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print(f"Telegram send error: {e}")

# ---------------------------------------------------------------------------
# BINANCE MARKET DATA & EDGE BEREKENING
# ---------------------------------------------------------------------------
def calculate_ema_series(values, period):
    """Berekent een echte exponentiële moving average reeks (niet SMA)."""
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    ema_series = [sum(values[:period]) / period]  # SMA als seed
    for price in values[period:]:
        ema_series.append((price - ema_series[-1]) * multiplier + ema_series[-1])
    return ema_series

def get_binance_data(symbol_code):
    try:
        # Meer candles opgehaald (100 i.p.v. 50) zodat MACD (met EMA26-achtige
        # opbouw) voldoende historie heeft om betrouwbaar te smoothen.
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol_code}&interval=5m&limit=100"
        res = requests.get(url, timeout=5).json()

        closes = [float(c[4]) for c in res]
        volumes = [float(c[5]) for c in res]
        live_price = closes[-1]

        # EMAs (let op: dit zijn simpele moving averages, geen echte EMA-smoothing)
        ema5 = round(sum(closes[-5:]) / 5, 2)
        ema15 = round(sum(closes[-15:]) / 15, 2)
        ema50 = round(sum(closes[-50:]) / 50, 2)

        # RSI met Wilder's smoothing (matcht TradingView/Binance)
        # Wilder gebruikt een exponentieel gewogen gemiddelde i.p.v. een simpel
        # gemiddelde: eerst een SMA-seed over de eerste 14 periodes, daarna
        # recursief smoothen over de rest van de candle-reeks.
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

        avg_loss = avg_loss or 1e-9  # voorkom deling door 0
        rs = avg_gain / avg_loss
        rsi = round(100 - (100 / (1 + rs)), 1)

        # Volume Check
        avg_volume = sum(volumes[-10:-1]) / 9
        current_volume = volumes[-1]
        volume_ok = current_volume > (avg_volume * VOLUME_MULTIPLIER)

        # 📐 BEREKENING VAN DE EDGE (%)
        # Symmetrisch gemaakt zodat zowel bullish (UP) als bearish (DOWN)
        # signalen een positieve edge-waarde krijgen wanneer ze sterk zijn.
        ema_spread_pct = ((ema5 - ema15) / ema15) * 100  # positief = bullish, negatief = bearish
        if ema_spread_pct >= 0:
            rsi_bonus = max(0, (rsi - 60) * 0.01)
        else:
            rsi_bonus = max(0, (40 - rsi) * 0.01)  # lage RSI versterkt een bearish edge
        calculated_edge = round(abs(ema_spread_pct) + rsi_bonus, 2)

        # --- MACD (fast settings voor 5m: standaard 6/13/4) ---
        # MACD-lijn = EMA_fast - EMA_slow; signaallijn = EMA van de MACD-lijn.
        # Een crossover (MACD kruist signaallijn) duidt op een momentum-shift,
        # vaak eerder zichtbaar dan de EMA5/EMA15-spread die de bot al gebruikt.
        macd_signal_cross = None  # "UP", "DOWN", of None
        macd_histogram = 0.0
        ema_fast_series = calculate_ema_series(closes, MACD_FAST)
        ema_slow_series = calculate_ema_series(closes, MACD_SLOW)

        if len(ema_fast_series) > 1 and len(ema_slow_series) > 1:
            # Lijn ze uit op dezelfde lengte (de kortste reeks bepaalt de overlap)
            offset = len(ema_fast_series) - len(ema_slow_series)
            aligned_fast = ema_fast_series[offset:] if offset > 0 else ema_fast_series
            aligned_slow = ema_slow_series[-offset:] if offset < 0 else ema_slow_series
            macd_line = [f - s for f, s in zip(aligned_fast, aligned_slow)]

            signal_line_series = calculate_ema_series(macd_line, MACD_SIGNAL)
            if len(signal_line_series) >= 2 and len(macd_line) >= len(signal_line_series):
                # Laatste twee punten vergelijken om een verse crossover te detecteren
                macd_now = macd_line[-1]
                macd_prev = macd_line[-2]
                sig_now = signal_line_series[-1]
                sig_prev = signal_line_series[-2] if len(signal_line_series) >= 2 else signal_line_series[-1]

                macd_histogram = round(macd_now - sig_now, 4)

                crossed_up = macd_prev <= sig_prev and macd_now > sig_now
                crossed_down = macd_prev >= sig_prev and macd_now < sig_now
                if crossed_up:
                    macd_signal_cross = "UP"
                elif crossed_down:
                    macd_signal_cross = "DOWN"

        # --- Bollinger Bands ---
        # Uitbraak boven de bovenband (met volume) = bullish momentum-signaal;
        # doorbraak onder de onderband = bearish. Onafhankelijk van RSI/EMA,
        # dus dit is een echt aparte bron van kansen.
        bb_signal = None  # "UP", "DOWN", of None
        if len(closes) >= BB_PERIOD:
            bb_window = closes[-BB_PERIOD:]
            bb_mid = sum(bb_window) / BB_PERIOD
            variance = sum((c - bb_mid) ** 2 for c in bb_window) / BB_PERIOD
            bb_std = variance ** 0.5
            bb_upper = bb_mid + (BB_STD_DEV * bb_std)
            bb_lower = bb_mid - (BB_STD_DEV * bb_std)

            if live_price > bb_upper and volume_ok:
                bb_signal = "UP"
            elif live_price < bb_lower and volume_ok:
                bb_signal = "DOWN"
        else:
            bb_upper = bb_lower = None

        return {
            "price": live_price,
            "ema5": ema5,
            "ema15": ema15,
            "ema50": ema50,
            "rsi": rsi,
            "volume_ok": volume_ok,
            "edge": calculated_edge,
            "macd_signal_cross": macd_signal_cross,
            "macd_histogram": macd_histogram,
            "bb_signal": bb_signal,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
        }, None
    except Exception as e:
        return None, str(e)

def get_remaining_5m_time():
    now = datetime.datetime.utcnow()
    seconds_past_5m = (now.minute % 5) * 60 + now.second
    remaining_seconds = 300 - seconds_past_5m
    minutes = remaining_seconds // 60
    secs = remaining_seconds % 60
    return remaining_seconds, f"{minutes}m {secs}s"
