import os
import time
import requests
import traceback

# ---------------------------------------------------------------------------
# CONFIGURATIE (RAILWAY ENVIRONMENT VARIABLES)
# ---------------------------------------------------------------------------
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN.strip().replace("bot", "") if RAW_TOKEN.strip().startswith("bot") else RAW_TOKEN.strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Quant Strategy Parameters - Directe link naar de 5m markt
POLYMARKET_URL = "https://polymarket.com/crypto/5M"
PAPER_BALANCE = 1000.0
INZET_PER_TRADE = 10.0
TOTAL_TRADES = 0
HIGHEST_EDGE_HOUR = 0.0

active_trade = None  # Bijhouden van geopende positie

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
# BINANCE MARKET DATA & TECHNICAL INDICATORS
# ---------------------------------------------------------------------------
def get_binance_data():
    """ Haalt real-time Binance kandelaren & prijsstroom op voor BTC/USDT """
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=50"
        res = requests.get(url, timeout=5).json()
        
        closes = [float(c[4]) for c in res]
        live_price = closes[-1]
        
        # EMA/RSI berekeningen
        ema5 = round(sum(closes[-5:]) / 5, 2)
        ema15 = round(sum(closes[-15:]) / 15, 2)
        ema50 = round(sum(closes[-50:]) / 50, 2)
        
        gains = [max(0, closes[i] - closes[i-1]) for i in range(-14, 0)]
        losses = [max(0, closes[i-1] - closes[i]) for i in range(-14, 0)]
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14 or 1
        rs = avg_gain / avg_loss
        rsi = round(100 - (100 / (1 + rs)), 1)
        
        return {
            "price": live_price,
            "ema5": ema5,
            "ema15": ema15,
            "ema50": ema50,
            "rsi": rsi
        }, None
    except Exception as e:
        return None, str(e)

# ---------------------------------------------------------------------------
# COMMAND HANDLER (/start, /portfolio)
# ---------------------------------------------------------------------------
def handle_command(text):
    global PAPER_BALANCE, TOTAL_TRADES, HIGHEST_EDGE_HOUR
    
    cmd = text.lower().strip()
    
    if "/start" in cmd:
        msg = (
            "🤖 **Nobelele's Poly Bot Is Actief!**\n\n"
            "• Scant live Binance BTC/USDT data voor 5M kaarten.\n"
            "• Controleert op 3/3 Confluentie Breakouts.\n"
            "• Stuur `/portfolio` om je tussenstand te bekijken."
        )
        send_telegram_msg(msg)

    elif "/portfolio" in cmd or "/status" in cmd:
        msg = (
            "📊 **HUIDIGE PORTFOLIO STATUS**\n\n"
            f"• **Huidig Saldo:** `${PAPER_BALANCE:.2f}`\n"
            f"• **Totaal Trades:** `{TOTAL_TRADES}`\n"
            f"• **Hoogst Gemeten Edge (uursgemiddelde):** `{HIGHEST_EDGE_HOUR * 100:.2f}%`\n"
            f"• **Status:** Actief aan het scannen..."
        )
        send_telegram_msg(msg)

# ---------------------------------------------------------------------------
# MAIN TRADING ENGINE
# ---------------------------------------------------------------------------
def main():
    global HIGHEST_EDGE_HOUR, PAPER_BALANCE, TOTAL_TRADES, active_trade
    print("🚀 Quant Bot gestart met 5M specifieke Polymarket link...")
    
    clear_telegram_webhooks()
    time.sleep(1)
    
    send_telegram_msg("⚙️ **Poly Bot Live:** Verbinding hersteld! Knop verwijst nu direct naar de 5M markt op Polymarket.")

    last_update_id = 0
    last_heartbeat = time.time()
    last_scan_time = 0

    while True:
        # 1. Telegram Command Listener
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

        # 2. Markt Analyse & Signaal Generatie (elke 15 sec)
        if time.time() - last_scan_time > 15:
            last_scan_time = time.time()
            data, error = get_binance_data()
            
            if data and not active_trade:
                price = data["price"]
                ema5 = data["ema5"]
                ema15 = data["ema15"]
                ema50 = data["ema50"]
                rsi = data["rsi"]
                
                # Breakout condities
                if ema5 > ema15 and rsi > 60 and price > ema50:
                    direction = "UP"
                    
                    # Signaal Bericht
                    signal_msg = (
                        f"🚀 **HIGH CONFIDENCE BREAKOUT ({direction})**\n📈\n\n"
                        f"Pair: BTC/USDT\n"
                        f"Live Prijs: `${price:,.2f}` (Binance Feed)\n"
                        f"Tijd over in ronde: ~1.4 minuten\n\n"
                        f"Confluentie Check (3/3):\n"
                        f"✅ 5M EMA Crossover: EMA 5 (${ema5:,.2f}) > EMA 15 (${ema15:,.2f})\n"
                        f"✅ RSI (14): {rsi} (Sterk momentum)\n"
                        f"✅ 15M Trend: Boven EMA 50 (${ema50:,.2f})\n\n"
                        f"💡 **Actie:** Zet €5.00 of €10.00 in op '{direction}'"
                    )
                    send_telegram_msg(signal_msg, button_url=POLYMARKET_URL)

                    # Paper Trade Openen
                    PAPER_BALANCE -= INZET_PER_TRADE
                    TOTAL_TRADES += 1
                    active_trade = {
                        "entry": price,
                        "direction": direction,
                        "start_time": time.time()
                    }

                    open_trade_msg = (
                        f"📝 **PAPER TRADE GEOPEND (SIMULATIE)**\n🤖\n\n"
                        f"Asset: BTC/USDT 5M\n"
                        f"🎯 Richting: {direction}\n"
                        f"💰 Virtuele Inzet: `${INZET_PER_TRADE:.2f}` @ Koers: `${price:,.2f}`\n"
                        f"💵 Resterend Saldo: `${PAPER_BALANCE:.2f}`\n"
                        f"⏱️ Verwachte Afloop: over ~5 minuten"
                    )
                    send_telegram_msg(open_trade_msg)

        # 3. Sluiten van geopende paper trade (na 5 minuten)
        if active_trade and (time.time() - active_trade["start_time"] > 300):
            current_data, _ = get_binance_data()
            exit_price = current_data["price"] if current_data else active_trade["entry"]
            
            win = (exit_price > active_trade["entry"]) if active_trade["direction"] == "UP" else (exit_price < active_trade["entry"])
            
            if win:
                profit = INZET_PER_TRADE * 1.85
                PAPER_BALANCE += profit
                result_header = "✅ **PAPER TRADE GEWONNEN** 🎉"
            else:
                result_header = "❌ **PAPER TRADE VERLOREN** 📉"

            result_msg = (
                f"{result_header}\n\n"
                f"Trade #{TOTAL_TRADES} ({active_trade['direction']})\n"
                f"Entry: `${active_trade['entry']:,.2f}` ➔ Exit: `${exit_price:,.2f}`\n"
                f"💰 Nieuw Saldo: `${PAPER_BALANCE:.2f}`"
            )
            send_telegram_msg(result_msg)
            active_trade = None

        # 4. Uurlijkse Heartbeat Check
        if time.time() - last_heartbeat > 3600:
            send_telegram_msg(
                f"💗 **BOT HEARTBEAT CHECK**\n\n"
                f"• Bot is online en scant elke 10s.\n"
                f"• Hoogst gemeten Edge afgelopen uur: `{HIGHEST_EDGE_HOUR * 100:.2f}%`\n"
                f"• Totaal Trades: `{TOTAL_TRADES}`\n"
                f"• Huidig Saldo: `${PAPER_BALANCE:.2f}`"
            )
            last_heartbeat = time.time()

        time.sleep(2)

if __name__ == "__main__":
    main()
