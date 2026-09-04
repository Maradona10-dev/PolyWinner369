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

POLYMARKET_URL = "https://polymarket.com/crypto/5M"
INZET_PER_TRADE = 10.0
DATA_FILE = "portfolio.json"

# 🎯 DREMPELWAARDE (THRESHOLD): Verhoogd naar 0.25% voor alleen de sterkste signalen
MIN_EDGE_THRESHOLD = 0.25 

SYMBOLS = {
    "BTC/USDT": "BTCUSDT",
    "ETH/USDT": "ETHUSDT",
    "SOL/USDT": "SOLUSDT"
}

# ---------------------------------------------------------------------------
# PERSISTENTIE: DATA OPSLAAN EN INLADEN
# ---------------------------------------------------------------------------
def load_portfolio():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                return data.get("balance", 1000.0), data.get("total_trades", 0)
        except Exception as e:
            print(f"Fout bij laden portfolio: {e}")
    return 1000.0, 0

def save_portfolio(balance, total_trades):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump({"balance": balance, "total_trades": total_trades}, f)
    except Exception as e:
        print(f"Fout bij opslaan portfolio: {e}")

PAPER_BALANCE, TOTAL_TRADES = load_portfolio()
active_trade = None

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
        
        # EMAs
        ema5 = round(sum(closes[-5:]) / 5, 2)
        ema15 = round(sum(closes[-15:]) / 15, 2)
        ema50 = round(sum(closes[-50:]) / 50, 2)
        
        # RSI
        gains = [max(0, closes[i] - closes[i-1]) for i in range(-14, 0)]
        losses = [max(0, closes[i-1] - closes[i]) for i in range(-14, 0)]
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14 or 1
        rs = avg_gain / avg_loss
        rsi = round(100 - (100 / (1 + rs)), 1)
        
        # Volume Check
        avg_volume = sum(volumes[-10:-1]) / 9
        current_volume = volumes[-1]
        volume_ok = current_volume > (avg_volume * 1.1)
        
        # 📐 BEREKENING VAN DE EDGE (%)
        # Berekend op basis van de afstand van EMA5 boven EMA15 en de RSI-sterkte
        ema_spread_pct = ((ema5 - ema15) / ema15) * 100
        rsi_bonus = max(0, (rsi - 60) * 0.01)
        calculated_edge = round(ema_spread_pct + rsi_bonus, 2)
        
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
            f"• Scant **BTC**, **ETH** en **SOL** met een verhoogde threshold van **{MIN_EDGE_THRESHOLD}%**.\n"
            "• Tijd-filter actief (> 2 min over in ronde).\n"
            "• Stuur `/portfolio` om je tussenstand te bekijken."
        )
        send_telegram_msg(msg)

    elif "/portfolio" in cmd or "/status" in cmd:
        msg = (
            "📊 **HUIDIGE PORTFOLIO STATUS**\n\n"
            f"• **Huidig Saldo:** `${PAPER_BALANCE:.2f}`\n"
            f"• **Totaal Trades:** `{TOTAL_TRADES}`\n"
            f"• **Minimale Edge Threshold:** `{MIN_EDGE_THRESHOLD}%`\n"
            f"• **Status:** Actief aan het scannen (BTC, ETH, SOL)..."
        )
        send_telegram_msg(msg)

# ---------------------------------------------------------------------------
# MAIN TRADING ENGINE
# ---------------------------------------------------------------------------
def main():
    global PAPER_BALANCE, TOTAL_TRADES, active_trade
    print(f"🚀 Quant Bot gestart (Threshold: {MIN_EDGE_THRESHOLD}%)...")
    
    clear_telegram_webhooks()
    time.sleep(1)
    
    send_telegram_msg(f"⚙️ **Poly Bot Live:** Min. Edge Threshold verhoogd naar `{MIN_EDGE_THRESHOLD}%` voor hogere win-rate! Multi-asset & Tijd-filter actief.")

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
            pass

        # 2. Markt Analyse & Scanner (elke 15 sec)
        if time.time() - last_scan_time > 15:
            last_scan_time = time.time()
            remaining_seconds, time_str = get_remaining_5m_time()

            # Tijd-filter (> 2 min over) & Geen actieve trade
            if remaining_seconds >= 120 and not active_trade:
                
                for pair_name, symbol_code in SYMBOLS.items():
                    data, error = get_binance_data(symbol_code)
                    
                    if data:
                        price = data["price"]
                        ema5 = data["ema5"]
                        ema15 = data["ema15"]
                        ema50 = data["ema50"]
                        rsi = data["rsi"]
                        volume_ok = data["volume_ok"]
                        edge = data["edge"]

                        # 🎯 HOOFDVOORWAARDE: Confluentie + THRESHOLD CHECK (Minimaal 0.25%)
                        if ema5 > ema15 and rsi > 60 and price > ema50 and volume_ok and edge >= MIN_EDGE_THRESHOLD:
                            direction = "UP"
                            
                            signal_msg = (
                                f"🚀 **HIGH CONFIDENCE BREAKOUT ({direction})**\n📈\n\n"
                                f"Pair: {pair_name}\n"
                                f"Gemeten Edge: `{edge}%` (Threshold: >{MIN_EDGE_THRESHOLD}%)\n"
                                f"Live Prijs: `${price:,.2f}` (Binance Feed)\n"
                                f"Tijd over in ronde: ~{time_str}\n\n"
                                f"Confluentie Check (3/3):\n"
                                f"✅ 5M EMA Crossover: EMA 5 (${ema5:,.2f}) > EMA 15 (${ema15:,.2f})\n"
                                f"✅ RSI (14): {rsi} (Sterk momentum)\n"
                                f"✅ 15M Trend: Boven EMA 50 (${ema50:,.2f})\n\n"
                                f"💡 **Actie:** Zet €5.00 of €10.00 in op '{direction}'"
                            )
                            send_telegram_msg(signal_msg, button_url=POLYMARKET_URL)

                            # Paper Trade
                            PAPER_BALANCE -= INZET_PER_TRADE
                            TOTAL_TRADES += 1
                            save_portfolio(PAPER_BALANCE, TOTAL_TRADES)
                            
                            active_trade = {
                                "pair": pair_name,
                                "symbol": symbol_code,
                                "entry": price,
                                "direction": direction,
                                "start_time": time.time(),
                                "duration": remaining_seconds
                            }

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
                            break

        # 3. Sluiten van geopende trade
        if active_trade and (time.time() - active_trade["start_time"] >= active_trade["duration"]):
            current_data, _ = get_binance_data(active_trade["symbol"])
            exit_price = current_data["price"] if current_data else active_trade["entry"]
            
            win = (exit_price > active_trade["entry"]) if active_trade["direction"] == "UP" else (exit_price < active_trade["entry"])
            
            if win:
                profit = INZET_PER_TRADE * 1.85
                PAPER_BALANCE += profit
                result_header = "✅ **PAPER TRADE GEWONNEN** 🎉"
            else:
                result_header = "❌ **PAPER TRADE VERLOREN** 📉"

            save_portfolio(PAPER_BALANCE, TOTAL_TRADES)

            result_msg = (
                f"{result_header}\n\n"
                f"Trade #{TOTAL_TRADES} ({active_trade['pair']} - {active_trade['direction']})\n"
                f"Entry: `${active_trade['entry']:,.2f}` ➔ Exit: `${exit_price:,.2f}`\n"
                f"💰 Nieuw Saldo: `${PAPER_BALANCE:.2f}`"
            )
            send_telegram_msg(result_msg)
            active_trade = None

        # 4. Heartbeat
        if time.time() - last_heartbeat > 3600:
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
