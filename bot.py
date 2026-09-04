import os
import time
import requests
import traceback
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIGURATIE NOBELELE'S POLY BOT
# ---------------------------------------------------------------------------
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = RAW_TOKEN.strip().replace("bot", "") if RAW_TOKEN.strip().startswith("bot") else RAW_TOKEN.strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

MIN_EDGE = 0.001       # 0.1% minimale edge (zeer laag om direct te testen)
PAPER_BALANCE = 1000.0
TRADE_AMOUNT = 10.0
TOTAL_TRADES = 0
HIGHEST_EDGE_HOUR = 0.0

# ---------------------------------------------------------------------------
# TELEGRAM VERSTUUR FUNCTIE
# ---------------------------------------------------------------------------
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print(f"Telegram Fout: {e}")

# ---------------------------------------------------------------------------
# COMMAND VERWERKER (/start, /portfolio, /status)
# ---------------------------------------------------------------------------
def handle_command(text):
    global PAPER_BALANCE, TOTAL_TRADES, HIGHEST_EDGE_HOUR
    
    if text.startswith("/start"):
        msg = (
            "🤖 **Nobelele's Poly Bot Is Actief!**\n\n"
            "• Ik scan elke 10s op de 5-minuten kaart.\n"
            "• Minimaal vereiste Edge: `0.1%`\n"
            "• Paper Trading Mode: **AAN**\n\n"
            "Gebruik /portfolio voor je huidige saldo."
        )
        send_telegram_msg(msg)

    elif text.startswith("/portfolio") or text.startswith("/status"):
        msg = (
            "📊 **HUIDIGE PORTFOLIO STATUS**\n\n"
            f"• **Huidig Saldo:** `${PAPER_BALANCE:.2f}`\n"
            f"• **Totaal Trades:** `{TOTAL_TRADES}`\n"
            f"• **Hoogst Gemeten Edge (afgelopen uur):** `{HIGHEST_EDGE_HOUR * 100:.2f}%`\n"
            f"• **Status:** Actief aan het scannen..."
        )
        send_telegram_msg(msg)

# ---------------------------------------------------------------------------
# PAPER TRADE EXECUTION
# ---------------------------------------------------------------------------
def execute_paper_trade(market, edge):
    global PAPER_BALANCE, TOTAL_TRADES
    
    PAPER_BALANCE -= TRADE_AMOUNT
    TOTAL_TRADES += 1
    
    msg = (
        f"📝 **PAPER TRADE EXECUTED!** 🚀\n\n"
        f"🎯 **Markt:** `{market}`\n"
        f"📈 **Gemeten Edge:** `{edge * 100:.2f}%`\n"
        f"💵 **Inzet:** `${TRADE_AMOUNT:.2f}`\n"
        f"💰 **Nieuw Virtueel Saldo:** `${PAPER_BALANCE:.2f}`\n\n"
        f"⏱️ Positie geopend op de 5m kaart."
    )
    send_telegram_msg(msg)

# ---------------------------------------------------------------------------
# MAIN LOOP (MET TELEGRAM LISTEN ENGINE)
# ---------------------------------------------------------------------------
def main():
    global HIGHEST_EDGE_HOUR
    print("🚀 Nobelele's Poly Bot Gestart met Listener...")
    
    send_telegram_msg("⚙️ **Poly Bot Herstart:** Luistert nu live naar jouw Telegram commando's (/start, /portfolio)!")
    
    last_update_id = 0
    last_heartbeat = time.time()
    last_trade_time = 0

    while True:
        try:
            # 1. Luisteren naar inkomende Telegram berichten
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=5"
            res = requests.get(url, timeout=8).json()
            
            if "result" in res:
                for update in res["result"]:
                    last_update_id = update["update_id"]
                    message = update.get("message", {})
                    text = message.get("text", "").strip()

                    if text:
                        handle_command(text)

            # 2. Markt-scan simulatie (elke 10 seconden)
            simulated_edge = 0.0025  # 0.25% Edge
            market = "BTC 5-Min Window Outcome"
            
            if simulated_edge > HIGHEST_EDGE_HOUR:
                HIGHEST_EDGE_HOUR = simulated_edge

            # Uitvoeren als drempel is bereikt (max 1 trade per 5 minuten om spam te voorkomen)
            if simulated_edge >= MIN_EDGE and (time.time() - last_trade_time > 300):
                execute_paper_trade(market, simulated_edge)
                last_trade_time = time.time()

            # 3. Heartbeat elk uur
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

            time.sleep(2)

        except Exception as e:
            print(f"Loop fout: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
