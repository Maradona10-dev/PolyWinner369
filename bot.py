import os
import time
import requests
import traceback

# ---------------------------------------------------------------------------
# CONFIGURATIE (HAALT GEGEVENS UIT RAILWAY VARIABLES)
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
# TELEGRAM HELPERS
# ---------------------------------------------------------------------------
def clear_telegram_webhooks():
    """ Verwijdert eventuele webhooks en oude wachtende berichten """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook?drop_pending_updates=True"
    try:
        res = requests.get(url, timeout=5).json()
        print(f"🔄 Webhook Reset Status: {res}")
    except Exception as e:
        print(f"❌ Webhook Reset Fout: {e}")

def send_telegram_msg(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ FOUT: TOKEN of CHAT_ID ontbreekt in Railway Variables!")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=8)
        data = res.json()
        if not data.get("ok"):
            print(f"❌ Telegram API Fout: {data}")
            return False
        print("✅ Telegram bericht verzonden")
        return True
    except Exception as e:
        print(f"❌ Verzendfout: {e}")
        return False

# ---------------------------------------------------------------------------
# COMMAND HANDLER
# ---------------------------------------------------------------------------
def handle_command(text):
    global PAPER_BALANCE, TOTAL_TRADES, HIGHEST_EDGE_HOUR
    
    cmd = text.lower().strip()
    
    if "/start" in cmd:
        msg = (
            "🤖 **Nobelele's Poly Bot Is Actief!**\n\n"
            "• Scanning interval: Elke 10s op de 5m kaart.\n"
            "• Min. Edge Threshold: `0.1%`\n"
            "• Paper Trading: **ACTIEF**\n\n"
            "Stuur /portfolio om je saldo en statistieken in te zien."
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
# MAIN SCAN & LISTEN LOOP
# ---------------------------------------------------------------------------
def main():
    global HIGHEST_EDGE_HOUR, PAPER_BALANCE, TOTAL_TRADES
    print("🚀 Nobelele's Poly Bot start op...")
    
    # 1. Reset Telegram verbinding
    clear_telegram_webhooks()
    time.sleep(1)

    # 2. Verstuur opstartbericht
    send_telegram_msg("⚙️ **Poly Bot Herstart:** De bot luistert nu live naar commando's (/start, /portfolio)!")

    last_update_id = 0
    last_heartbeat = time.time()
    last_trade_time = 0

    while True:
        # A. Luisteren naar inkomende commando's
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
                        print(f"📩 Commando ontvangen: {text}")
                        handle_command(text)
        except Exception as e:
            print(f"⚠️ Polling fout: {e}")

        # B. Paper Trading Simulatie
        try:
            simulated_edge = 0.0025  # 0.25% Edge
            market = "BTC 5-Min Window Outcome"

            if simulated_edge > HIGHEST_EDGE_HOUR:
                HIGHEST_EDGE_HOUR = simulated_edge

            # Voer 1 trade per 5 minuten uit om spam te voorkomen
            if simulated_edge >= MIN_EDGE and (time.time() - last_trade_time > 300):
                PAPER_BALANCE -= TRADE_AMOUNT
                TOTAL_TRADES += 1

                trade_msg = (
                    f"📝 **PAPER TRADE EXECUTED!** 🚀\n\n"
                    f"🎯 **Markt:** `{market}`\n"
                    f"📈 **Gemeten Edge:** `{simulated_edge * 100:.2f}%`\n"
                    f"💵 **Inzet:** `${TRADE_AMOUNT:.2f}`\n"
                    f"💰 **Nieuw Virtueel Saldo:** `${PAPER_BALANCE:.2f}`"
                )
                send_telegram_msg(trade_msg)
                last_trade_time = time.time()

            # C. Elk uur Heartbeat
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
            print(f"⚠️ Trading loop fout: {e}")

        time.sleep(2)

if __name__ == "__main__":
    main()
