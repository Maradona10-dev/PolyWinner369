import os
import time
import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE (SPORT BOT)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8945207926:AAEe0V3AYAdKqyT732B1TWIYcEBz8Ps9E_8")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "369244706")

# Polymarket Gamma API Endpoint
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com/events"

# ---------------------------------------------------------------------------
# TELEGRAM NOTIFICATIE FUNCTIE
# ---------------------------------------------------------------------------
def send_telegram_alert(message, target_url="https://polymarket.com/sports"):
    """Stuurt een pushmelding inclusief directe actie-knop naar Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "⚽ BEKIJK OP POLYMARKET",
                    "url": target_url
                }
            ]
        ]
    }
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
        "reply_markup": reply_markup
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Fout bij versturen van Telegram bericht: {e}")

# ---------------------------------------------------------------------------
# POLYMARKET SPORT SCANNER
# ---------------------------------------------------------------------------
def scan_polymarket_sports():
    """Scant actieve sportmarkten op Polymarket"""
    print("Scannen van live Polymarket sportmarkten...")
    try:
        params = {
            "closed": "false",
            "limit": 30,
            "tag_slug": "sports"
        }
        res = requests.get(POLYMARKET_GAMMA_API, params=params, timeout=10)
        
        if res.status_code == 200:
            events = res.json()
            print(f"Succesvol {len(events)} sport events opgehaald.")
            
            for event in events:
                title = event.get("title", "Onbekend Event")
                slug = event.get("slug", "")
                event_url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/sports"
                
                markets = event.get("markets", [])
                for market in markets:
                    group_item_title = market.get("groupItemTitle", "")
                    outcome_prices = market.get("outcomePrices", [])
                    
                    if outcome_prices and len(outcome_prices) >= 2:
                        try:
                            # Verwerkt strings of floats veilig
                            price_yes = float(eval(outcome_prices[0]) if isinstance(outcome_prices[0], str) and outcome_prices[0].startswith('"') else outcome_prices[0])
                            price_no = float(eval(outcome_prices[1]) if isinstance(outcome_prices[1], str) and outcome_prices[1].startswith('"') else outcome_prices[1])
                            
                            # Signaleer markten waar een duidelijke favoriet is (bijv. YES tussen 65% en 85%)
                            if 0.65 <= price_yes <= 0.85:
                                alert_msg = (
                                    f"⚽ **SPORT VALUE OPPORTUNITY** 🏆\n\n"
                                    f"**Event:** {title}\n"
                                    f"**Markt:** {group_item_title if group_item_title else 'Winnaar'}\n\n"
                                    f"📊 **Kansen op Polymarket:**\n"
                                    f"• **YES:** ${price_yes:.2f} ({price_yes*100:.0f}%)\n"
                                    f"• **NO:** ${price_no:.2f} ({price_no*100:.0f}%)\n\n"
                                    f"💡 **Actie:** Bekijk de wedstrijd op Polymarket."
                                )
                                send_telegram_alert(alert_msg, target_url=event_url)
                                
                        except (ValueError, TypeError):
                            continue

    except Exception as e:
        print(f"Fout bij ophalen Polymarket Sport API: {e}")

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🤖 Polymarket Live Sport Scanner is gestart!")
    
    send_telegram_alert(
        "⚽ **Polymarket Sport Bot is Actief!**\n\n"
        "De bot scant nu live sportwedstrijden op Polymarket op waarde-kansen.",
        target_url="https://polymarket.com/sports"
    )
    
    try:
        while True:
            scan_polymarket_sports()
            time.sleep(300)
    except KeyboardInterrupt:
        send_telegram_alert("🔴 **Sport Bot is gestopt.**")
