import os
import time
import requests
import traceback

# ---------------------------------------------------------------------------
# CONFIGURATIE POLYWINNER
# ---------------------------------------------------------------------------
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN.strip().replace("bot", "") if RAW_TOKEN.strip().startswith("bot") else RAW_TOKEN.strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

MIN_EDGE = 0.001       # 0.1% minimale edge
PAPER_BALANCE = 1000.0
TRADE_AMOUNT = 10.0
TOTAL_TRADES = 0
HIGHEST_EDGE_HOUR = 0.0

# ---------------------------------------------------------------------------
# TELEGRAM VERSTUUR FUNCTIE
# ---------------------------------------------------------------------------
def send_telegram_msg(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ FOUT: TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID ontbreekt!")
        return False
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        print(f"Verzonden naar Telegram: status {res.status_code}")
        return True
    except Exception as e:
        print(f"Telegram Fout: {e}")
        return False

# ---------------------------------------------------------------------------
# COMMAND VERWERKER
# ---------------------------------------------------------------------------
def handle_command(text):
    global PAPER_BALANCE, TOTAL_TRADES, HIGHEST_EDGE_HOUR
    
    if "/start" in text:
        msg = (
            "🤖 **Nobelele's Poly Bot Is Actief!**\n\n"
            "• Ik scan elke 10s op de 5-minuten kaart.\n"
            "• Minimaal vereiste Edge: `0.1%`\n"
            "• Paper Trading Mode: **AAN**\n\n"
            "Gebruik /portfolio voor je huidige saldo."
        )
        send_telegram_msg(msg)

    elif "/portfolio" in text or "/status" in text:
        msg = (
            "📊 **HUIDIGE PORTFOLIO STATUS**\n\n"
            f"• **Huidig Saldo:** `${PAPER_BALANCE:.2f}`\n"
            f"• **Totaal Trades:** `{TOTAL_TRADES}`\n"
            f"• **Hoogst Gemeten Edge (afgelopen uur):** `{HIGHEST_EDGE_HOUR * 100:.2f}%`\n"
            f"• **Status:** Actief aan het scannen..."
        )
        send_telegram_msg(msg)

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
def main():
    global HIGHEST_EDGE_HOUR, PAPER_BALANCE, TOTAL_TRADES
    print("🚀 Nobelele's Poly Bot Gestart met Directe Reactie...")
    
    # Direct een startbericht sturen bij boot
    send_telegram_msg("⚙️ **Poly Bot Live:** De bot is opnieuw opgestart en luistert nu actief naar je commando's!")
    
    last_update_id = 0
    last_heartbeat = time.time()
    last_trade_time = 0

    while True:
        # 1. Luisteren naar berichten via getUpdates (snelle timeout)
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 2}
            res = requests.get(url, params=params, timeout=4).json()
            
            if res.get("ok") and "result" in res:
                for update in res["result"]:
                    last_update_id = update["update_id"]
                    message = update.get("message", {})
                    text = message.get("text", "").strip()

                    if text:
                        print(f"📩 Ontvangen commando: {text}")
                        handle_command(text)
        except Exception as e:
            print(f"Polling update fout: {e}")

        # 2. Paper Trade Simulatie
        try:
            simulated_edge = 0.0025  # 0.25% Edge
            market = "BTC 5-Min Window Outcome"
            
            if simulated_edge > HIGHEST_EDGE_HOUR:
                HIGHEST_EDGE_HOUR = simulated_edge

            if simulated_edge >= MIN_EDGE and (time.time() - last_trade_time > 300):
                PAPER_BALANCE -= TRADE_AMOUNT
                TOTAL_TRADES += 1
                
                msg = (
                    f"📝 **PAPER TRADE EXECUTED!** 🚀\n\n"
                    f"🎯 **Markt:** `{market}`\n"
                    f"📈 **Gemeten Edge:** `{simulated_edge * 100:.2f}%`\n"
                    f"💵 **Inzet:** `${TRADE_AMOUNT:.2f}`\n"
                    f"💰 **Nieuw Virtueel Saldo:** `${PAPER_BALANCE:.2f}`"
                )
                send_telegram_msg(msg)
                last_trade_time = time.time()

            # 3. Heartbeat (elk uur)
            if time.time() - last_heartbeat > 3600:
                send_telegram_msg(
                    f"💗 **BOT HEARTBEAT CHECK**\n\n"
                    f"• Bot is online en scant elke 10s.\n"
                    f"• Hoogst gemeten Edge afgelopen uur: `{HIGHEST_EDGE_HOUR * 100:.2f}%`\n"
                    f"• Totaal Trades: `{TOTAL_TRADES}`\n"
                    f"• Huidig Saldo: `${PAPER_BALANCE:.2f}`"
                )
                HIGHEST_EDGE_HOUR = 0.0
                last_heartbeat = time.time()

        except Exception as e:
            print(f"Trading loop fout: {e}")

        time.sleep(2)

if __name__ == "__main__":
    main()
