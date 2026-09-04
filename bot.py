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
def get_binance_data(symbol_code):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol_code}&interval=5m&limit=50"
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

        return {
            "price": live_price,
            "ema5": ema5,
            "ema15": ema15,
            "ema50": ema50,
            "rsi": rsi,
            "volume_ok": volume_ok,
            "edge": calculated_edge
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

# ---------------------------------------------------------------------------
# COMMAND HANDLER
# ---------------------------------------------------------------------------
def handle_command(text):
    global PAPER_BALANCE, TOTAL_TRADES

    cmd = text.lower().strip()

    if "/start" in cmd:
        msg = (
            "🤖 **Nobelele's Poly Bot Is Actief!**\n\n"
            f"• Scant **{', '.join(SYMBOLS.keys())}** met een threshold van **{MIN_EDGE_THRESHOLD}%**.\n"
            f"• Tijd-filter actief (> {MIN_TIME_REMAINING_SEC // 60} min over in ronde).\n"
            "• Stuur `/portfolio` om je tussenstand te bekijken."
        )
        send_telegram_msg(msg)

    elif "/portfolio" in cmd or "/status" in cmd:
        open_pairs = ", ".join(active_trades.keys()) if active_trades else "geen"
        msg = (
            "📊 **HUIDIGE PORTFOLIO STATUS**\n\n"
            f"• **Huidig Saldo:** `${PAPER_BALANCE:.2f}`\n"
            f"• **Totaal Trades:** `{TOTAL_TRADES}`\n"
            f"• **Open Trades:** `{len(active_trades)}` ({open_pairs})\n"
            f"• **Minimale Edge Threshold:** `{MIN_EDGE_THRESHOLD}%`\n"
            f"• **Status:** Actief aan het scannen ({', '.join(SYMBOLS.keys())})..."
        )
        send_telegram_msg(msg)

# ---------------------------------------------------------------------------
# MAIN TRADING ENGINE
# ---------------------------------------------------------------------------
def main():
    global PAPER_BALANCE, TOTAL_TRADES, active_trades
    print(f"🚀 Quant Bot gestart (Threshold: {MIN_EDGE_THRESHOLD}%)...")

    clear_telegram_webhooks()
    time.sleep(1)

    send_telegram_msg(
        f"⚙️ **Poly Bot Live:** Min. Edge Threshold `{MIN_EDGE_THRESHOLD}%`. "
        f"Multi-asset & Tijd-filter actief. Edge-logging: {'AAN' if LOG_ALL_SIGNALS else 'UIT'}."
    )

    last_update_id = 0
    last_heartbeat = time.time()
    last_scan_time = 0

    while True:
        # 1. Telegram Listener
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 2}
            res = requests.get(url, params=params, timeout=5).json()

            if res.get("ok") and "result" in res:
                for update in res["result"]:
                    last_update_id = update["update_id"]
                    msg_obj = update.get("message", {})
                    text = msg_obj.get("text", "").strip()

                    if text:
                        handle_command(text)
        except Exception as e:
            print(f"Telegram listener error: {e}")

        # 2. Markt Analyse & Scanner
        if time.time() - last_scan_time > SCAN_INTERVAL_SEC:
            last_scan_time = time.time()
            remaining_seconds, time_str = get_remaining_5m_time()

            if remaining_seconds >= MIN_TIME_REMAINING_SEC:

                for pair_name, symbol_code in SYMBOLS.items():
                    # Elk symbool heeft zijn eigen trade-slot: als dit symbool
                    # al een open trade heeft, sla het over maar scan de rest door.
                    if pair_name in active_trades:
                        continue

                    data, error = get_binance_data(symbol_code)

                    if data:
                        price = data["price"]
                        ema5 = data["ema5"]
                        ema15 = data["ema15"]
                        ema50 = data["ema50"]
                        rsi = data["rsi"]
                        volume_ok = data["volume_ok"]
                        edge = data["edge"]

                        up_confluence = ema5 > ema15 and rsi > RSI_UP_THRESHOLD and price > ema50 and volume_ok
                        down_confluence = ema5 < ema15 and rsi < RSI_DOWN_THRESHOLD and price < ema50 and volume_ok

                        direction = None
                        if up_confluence:
                            direction = "UP"
                        elif down_confluence:
                            direction = "DOWN"

                        confluence_ok = direction is not None
                        passes_threshold = confluence_ok and edge >= MIN_EDGE_THRESHOLD

                        # Log ELKE meting (ook onder-threshold) zodat je later kunt zien
                        # welke edge-range daadwerkelijk winnende trades oplevert.
                        if LOG_ALL_SIGNALS and confluence_ok:
                            log_edge_observation(
                                pair_name, edge, rsi, volume_ok, passes_threshold,
                                event="scan", extra={"direction": direction}
                            )

                        # 🎯 HOOFDVOORWAARDE: Confluentie (UP of DOWN) + THRESHOLD CHECK
                        if passes_threshold:
                            if direction == "UP":
                                emoji = "📈"
                                trend_line = f"✅ 5M EMA Crossover: EMA 5 (${ema5:,.2f}) > EMA 15 (${ema15:,.2f})\n"
                                rsi_line = f"✅ RSI (14): {rsi} (Sterk momentum, > {RSI_UP_THRESHOLD})\n"
                                trend50_line = f"✅ 15M Trend: Boven EMA 50 (${ema50:,.2f})\n\n"
                            else:
                                emoji = "📉"
                                trend_line = f"✅ 5M EMA Crossover: EMA 5 (${ema5:,.2f}) < EMA 15 (${ema15:,.2f})\n"
                                rsi_line = f"✅ RSI (14): {rsi} (Zwak momentum, < {RSI_DOWN_THRESHOLD})\n"
                                trend50_line = f"✅ 15M Trend: Onder EMA 50 (${ema50:,.2f})\n\n"

                            signal_msg = (
                                f"🚀 **HIGH CONFIDENCE BREAKOUT ({direction})**\n{emoji}\n\n"
                                f"Pair: {pair_name}\n"
                                f"Gemeten Edge: `{edge}%` (Threshold: >{MIN_EDGE_THRESHOLD}%)\n"
                                f"Live Prijs: `${price:,.2f}` (Binance Feed)\n"
                                f"Tijd over in ronde: ~{time_str}\n\n"
                                f"Confluentie Check (3/3):\n"
                                f"{trend_line}"
                                f"{rsi_line}"
                                f"{trend50_line}"
                                f"💡 **Actie:** Zet €5.00 of €10.00 in op '{direction}'"
                            )
                            send_telegram_msg(signal_msg, button_url=POLYMARKET_URL)

                            # Paper Trade
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
                                "edge": edge,
                                "rsi": rsi,
                            }

                            log_edge_observation(
                                pair_name, edge, rsi, volume_ok, True,
                                event="trade_open",
                                extra={"entry_price": price, "direction": direction,
                                       "trade_number": TOTAL_TRADES}
                            )

                            open_trade_msg = (
                                f"📝 **PAPER TRADE GEOPEND (SIMULATIE)**\n🤖\n\n"
                                f"Asset: {pair_name} 5M\n"
                                f"🎯 Richting: {direction}\n"
                                f"📊 Gemeten Edge: `{edge}%`\n"
                                f"💰 Virtuele Inzet: `${INZET_PER_TRADE:.2f}` @ Koers: `${price:,.2f}`\n"
                                f"💵 Resterend Saldo: `${PAPER_BALANCE:.2f}`\n"
                                f"⏱️ Verwachte Afloop: over ~{time_str}"
                            )
                            send_telegram_msg(open_trade_msg)

        # 3. Sluiten van geopende trades (elk symbool onafhankelijk)
        finished_pairs = [
            pair for pair, trade in active_trades.items()
            if time.time() - trade["start_time"] >= trade["duration"]
        ]

        for pair_name in finished_pairs:
            trade = active_trades[pair_name]
            current_data, _ = get_binance_data(trade["symbol"])
            exit_price = current_data["price"] if current_data else trade["entry"]

            win = (exit_price > trade["entry"]) if trade["direction"] == "UP" else (exit_price < trade["entry"])

            if win:
                profit = INZET_PER_TRADE * PAYOUT_MULTIPLIER
                PAPER_BALANCE += profit
                result_header = "✅ **PAPER TRADE GEWONNEN** 🎉"
            else:
                result_header = "❌ **PAPER TRADE VERLOREN** 📉"

            save_portfolio(PAPER_BALANCE, TOTAL_TRADES)

            log_edge_observation(
                trade["pair"], trade["edge"], trade["rsi"],
                None, True,
                event="trade_close",
                extra={
                    "win": win,
                    "entry_price": trade["entry"],
                    "exit_price": exit_price,
                    "direction": trade["direction"],
                }
            )

            result_msg = (
                f"{result_header}\n\n"
                f"Trade #{TOTAL_TRADES} ({trade['pair']} - {trade['direction']})\n"
                f"Entry: `${trade['entry']:,.2f}` ➔ Exit: `${exit_price:,.2f}`\n"
                f"📊 Edge was: `{trade['edge']}%`\n"
                f"💰 Nieuw Saldo: `${PAPER_BALANCE:.2f}`"
            )
            send_telegram_msg(result_msg)
            del active_trades[pair_name]

        # 4. Heartbeat
        if time.time() - last_heartbeat > HEARTBEAT_INTERVAL_SEC:
            send_telegram_msg(
                f"💗 **BOT HEARTBEAT CHECK**\n\n"
                f"• Bot is online (Threshold: `{MIN_EDGE_THRESHOLD}%`).\n"
                f"• Totaal Trades: `{TOTAL_TRADES}`\n"
                f"• Huidig Saldo: `${PAPER_BALANCE:.2f}`"
            )
            last_heartbeat = time.time()

        time.sleep(2)

if __name__ == "__main__":
    main()
