import os
import time
import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE
# ---------------------------------------------------------------------------
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8945207926:AAEe0V3AYAdKqyT732B1TWIYcEBz8Ps9E_8")
TELEGRAM_BOT_TOKEN = RAW_TOKEN.strip().replace("bot", "") if RAW_TOKEN.strip().startswith("bot") else RAW_TOKEN.strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "369244706").strip()

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com/events"

FOOTBALL_TAGS = [
    "soccer", "premier-league", "la-liga", "serie-a", 
    "bundesliga", "ligue-1", "eredivisie", "jupiler-pro-league", 
    "primeira-liga", "mls"
]

LEAGUE_IDS = [88, 144, 39, 140, 135, 78, 61, 94, 253] # NL, BE, UK, ES, IT, DE, FR, PT, USA

LAST_OFFSET = 0
ALERTED_MATCHES = set()

# ---------------------------------------------------------------------------
# TELEGRAM FUNCTIES MET DIRECTE LINK
# ---------------------------------------------------------------------------
def send_telegram_alert(message, target_url="https://polymarket.com/sports"):
    """Stuurt een pushmelding met een DIRECTE link naar het specifieke event/markt"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "⚡ DIRECT INZETTEN OP POLYMARKET",
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
        res = requests.post(url, json=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"Telegram Fout: {e}")

def check_telegram_commands():
    """Luistert naar inkomende Telegram commando's (/scan, /status, /help)"""
    global LAST_OFFSET
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": LAST_OFFSET + 1, "timeout": 2}
    
    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            updates = res.json().get("result", [])
            for update in updates:
                LAST_OFFSET = update["update_id"]
                message = update.get("message", {})
                text = message.get("text", "").strip()
                chat_id = str(message.get("chat", {}).get("id", ""))
                
                if chat_id == TELEGRAM_CHAT_ID:
                    if text in ["/start", "/help"]:
                        help_msg = (
                            "🤖 **Polymarket Fast-Action Bot**\n\n"
                            "• `/scan` - Voer direct een handmatige scan uit\n"
                            "• `/status` - Bekijk actieve instellingen\n"
                            "• `/help` - Toon dit menu"
                        )
                        send_telegram_alert(help_msg)
                    elif text == "/status":
                        status_msg = (
                            "📊 **BOT STATUS**\n\n"
                            "✅ **Status:** Actief & Direct-Link Enabled\n"
                            "🎯 **Filters:** 65%-85% Value, 35%-60% Comeback, <98% Arbitrage\n"
                            "⚽ **Competities:** NL, BE, DE, FR, UK, ES, PT, IT, USA"
                        )
                        send_telegram_alert(status_msg)
                    elif text == "/scan":
                        send_telegram_alert("🔍 Handmatige scan gestart...")
                        scan_polymarket_sports()
    except Exception as e:
        print(f"Fout bij ophalen Telegram updates: {e}")

# ---------------------------------------------------------------------------
# POLYMARKET SCANNER MET EXACTE URL OPBOUW
# ---------------------------------------------------------------------------
def scan_polymarket_sports():
    print("Scannen van Polymarket markten met directe deep-links...")
    scanned_event_ids = set()
    found_matches = 0

    for tag in FOOTBALL_TAGS:
        try:
            params = {
                "closed": "false",
                "limit": 25,
                "tag_slug": tag
            }
            res = requests.get(POLYMARKET_GAMMA_API, params=params, timeout=10)
            
            if res.status_code == 200:
                events = res.json()
                for event in events:
                    event_id = event.get("id")
                    if event_id in scanned_event_ids:
                        continue
                    scanned_event_ids.add(event_id)
                    
                    title = event.get("title", "Onbekend Event")
                    slug = event.get("slug", "")
                    
                    # Directe URL opbouw naar het specifieke event
                    if slug:
                        event_url = f"https://polymarket.com/event/{slug}"
                    else:
                        event_url = "https://polymarket.com/sports"
                    
                    markets = event.get("markets", [])
                    for market in markets:
                        group_item_title = market.get("groupItemTitle", "")
                        question = market.get("question", "")
                        outcome_prices = market.get("outcomePrices", [])
                        market_slug = market.get("slug", "")
                        
                        # Als de individuele markt een eigen slug heeft, sturen we direct daarheen
                        if market_slug:
                            direct_market_url = f"https://polymarket.com/market/{market_slug}"
                        else:
                            direct_market_url = event_url

                        market_type = "Winnaar / 1X2"
                        if "over" in question.lower() or "under" in question.lower() or "goals" in question.lower():
                            market_type = "Over/Under Doelpunten"

                        if outcome_prices and len(outcome_prices) >= 2:
                            try:
                                price_yes = float(eval(outcome_prices[0]) if isinstance(outcome_prices[0], str) and outcome_prices[0].startswith('"') else outcome_prices[0])
                                price_no = float(eval(outcome_prices[1]) if isinstance(outcome_prices[1], str) and outcome_prices[1].startswith('"') else outcome_prices[1])
                                
                                odds_yes = round(1 / price_yes, 2) if price_yes > 0 else 0
                                odds_no = round(1 / price_no, 2) if price_no > 0 else 0
                                
                                # 1. ARBITRAGE
                                total_implied = price_yes + price_no
                                if 0 < total_implied < 0.98:
                                    found_matches += 1
                                    arb_profit = round((1 - total_implied) * 100, 2)
                                    arb_msg = (
                                        f"🚨 **ARBITRAGE KANS!** 🚨\n\n"
                                        f"**Event:** {title}\n"
                                        f"**Markt:** {group_item_title if group_item_title else question}\n"
                                        f"💰 **Winst:** +{arb_profit}%\n\n"
                                        f"• YES: ${price_yes:.2f} (Odds: {odds_yes})\n"
                                        f"• NO: ${price_no:.2f} (Odds: {odds_no})"
                                    )
                                    send_telegram_alert(arb_msg, target_url=direct_market_url)

                                # 2. VALUE (65% - 85%)
                                elif 0.65 <= price_yes <= 0.85:
                                    found_matches += 1
                                    estimated_bookie_odds = round(odds_yes * 0.93, 2)
                                    alert_msg = (
                                        f"⚽ **VALUE OPPORTUNITY** 🏆\n\n"
                                        f"**Event:** {title}\n"
                                        f"**Type:** {market_type}\n"
                                        f"**Markt:** {group_item_title if group_item_title else question}\n\n"
                                        f"📊 **Polymarket:** YES ${price_yes:.2f} ({price_yes*100:.0f}%) ➔ **Quote: {odds_yes}**\n"
                                        f"📈 **Gem. Bookies:** ~{estimated_bookie_odds}"
                                    )
                                    send_telegram_alert(alert_msg, target_url=direct_market_url)

                                # 3. COMEBACK / ACHTERSTAND (35% - 60%)
                                elif 0.35 <= price_yes <= 0.60:
                                    found_matches += 1
                                    estimated_bookie_odds = round(odds_yes * 0.93, 2)
                                    alert_msg = (
                                        f"🔥 **FAVORIET ACHTERSTAND / COMEBACK** 🔄\n\n"
                                        f"**Event:** {title}\n"
                                        f"**Type:** {market_type}\n"
                                        f"**Markt:** {group_item_title if group_item_title else question}\n\n"
                                        f"📊 **Polymarket:** YES ${price_yes:.2f} ({price_yes*100:.0f}%) ➔ **Hoge Quote: {odds_yes}**\n"
                                        f"📈 **Gem. Bookies:** ~{estimated_bookie_odds}"
                                    )
                                    send_telegram_alert(alert_msg, target_url=direct_market_url)
                                    
                            except (ValueError, TypeError, ZeroDivisionError):
                                continue

        except Exception as e:
            print(f"Fout bij ophalen tag '{tag}': {e}")

    print(f"Scan afgerond. {len(scanned_event_ids)} events gescand. {found_matches} kansen gevonden.")

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🤖 Direct-Link Polymarket Bot is gestart!")
    
    last_scan_time = 0
    try:
        while True:
            check_telegram_commands()
            current_time = time.time()
            if current_time - last_scan_time >= 300:
                scan_polymarket_sports()
                last_scan_time = current_time
            time.sleep(2)
    except KeyboardInterrupt:
        print("Bot gestopt.")
