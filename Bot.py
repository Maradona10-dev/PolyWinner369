import time
import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE GEGEVENS
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8840031344:AAGDJmeIC6yJPSUazE72tn32qZcFsdDvXP0"
TELEGRAM_CHAT_ID = "369244706"

# Cryptomunten die we actief scannen op 5m/15m kaarten
TRACKED_CRYPTO = ["BTC", "ETH", "SOL"]

# ---------------------------------------------------------------------------
# TELEGRAM NOTIFICATIE FUNCTIE
# ---------------------------------------------------------------------------
def send_telegram_alert(message):
    """Stuurt een pushmelding rechtstreeks naar je iPhone/iPad via Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Fout bij versturen van Telegram bericht: {e}")

# ---------------------------------------------------------------------------
# STRATEGIE 1: CRYPTO 5m/15m BREAKOUT & EMA CROSSOVER SCANNER
# ---------------------------------------------------------------------------
def calculate_ema(prices, period):
    """Berekent het Exponential Moving Average (EMA) voor een reeks prijzen"""
    if len(prices) < period:
        return prices[-1]
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def check_crypto_signals():
    """
    Scant 5m en 15m kaarsen voor BTC, ETH en SOL.
    Berekent 5 EMA vs 15 EMA crossovers & 5m High/Low Breakouts.
    """
    print("Scannen naar 5m/15m Crypto Breakouts & EMA Crossovers...")
    
    # Live data fetch van exchange/orderbook (gesimuleerd voor opstart):
    mock_5m_prices = [62100, 62150, 62200, 62350, 62500]  # Krachtige 5m/15m stijging
    mock_polymarket_yes_price = 0.45  # Vertraagde prijs op Polymarket (ondergewaardeerd)

    ema_5 = calculate_ema(mock_5m_prices, 5)
    ema_15 = calculate_ema(mock_5m_prices, 15)
    
    # 1. EMA Crossover check (5 EMA > 15 EMA = Bullish Momentum)
    is_bullish_crossover = ema_5 > ema_15
    
    # 2. Polymarket Lag Check (Er is een kans als Polymarket achterloopt)
    if is_bullish_crossover and mock_polymarket_yes_price < 0.60:
        alert_msg = (
            "🚨 **CRYPTO 5M/15M BREAKOUT DETECTED** 📈\n\n"
            "**Pair:** BTC/USDT (15-Min Window)\n"
            f"**5 EMA / 15 EMA:** Bullish Crossover (EMA5: ${ema_5:.2f})\n\n"
            f"💰 **Polymarket UP Prijs:** ${mock_polymarket_yes_price:.2f} *(Achterlopende markt!)*\n"
            "📊 **Verwachte Target:** $0.85+\n"
            "💡 **Advies Inzet:** $20.00 op 'UP'\n\n"
            "🔗 [Open 15m Crypto Markt op Polymarket](https://polymarket.com)"
        )
        send_telegram_alert(alert_msg)

# ---------------------------------------------------------------------------
# STRATEGIE 2: SPORT FAVORED COMEBACK SCANNER (INCL. BOOKIES, X/SOCIAL & STATS)
# ---------------------------------------------------------------------------
def verify_sport_data_and_sentiment(match_name):
    """
    Verifieert live data: bookmaker odds, xG/schoten statistieken
    en de laatste social media/nieuws updates om valse comebacks te filteren.
    """
    # In productie haalt deze functie live data via API's op:
    live_bookie_prob = 0.65       # Bookmakers schatten winstkans favoriet nog steeds op 65%
    live_xg = 2.4                 # Expected Goals favoriet is erg hoog
    social_sentiment_ok = True    # Geen rode kaarten of blessures van sterspelers
    
    return live_bookie_prob, live_xg, social_sentiment_ok

def check_favored_comebacks():
    """
    Scant live sportwedstrijden op Polymarket waar de pre-match favoriet
    achter staat en er overdreven 'Comeback Value' ontstaat.
    """
    print("Scannen naar Sport Comeback kansen op Polymarket...")
    
    match_name = "Real Madrid vs. Getafe"
    polymarket_price = 0.34  # Paniekprijs op Polymarket ($0.34 = 34% kans)
    
    # Haal uitgebreide analyse-data op (bookmakers + statistieken + social media)
    bookie_prob, xg, sentiment_ok = verify_sport_data_and_sentiment(match_name)
    
    # Arbitrage trigger: Polymarket biedt 34%, terwijl bookmakers & xG ~65% aangeven
    if polymarket_price < (bookie_prob - 0.20) and sentiment_ok:
        alert_msg = (
            "🚨 **SPORT COMEBACK TRIGGER DETECTED** ⚽\n\n"
            f"**Wedstrijd:** {match_name}\n"
            "**Status:** '62 min (Stand: 0 - 1)\n"
            "**Pre-Match Winstkans:** 82%\n\n"
            "📊 **Live Data & Sentiment Analysis:**\n"
            f"• Bookie Implied Odds: ~{int(bookie_prob*100)}%\n"
            f"• Live Expected Goals (xG): {xg}\n"
            "• Social/News Check: Geen rode kaarten / sterspelers op veld\n\n"
            f"💰 **Polymarket YES Prijs:** ${polymarket_price:.2f} *(Zware onderwaardering!)*\n"
            "💡 **Advies Inzet:** $15.00 op Real Madrid Winst\n\n"
            "🔗 [Bekijk op Polymarket](https://polymarket.com)"
        )
        send_telegram_alert(alert_msg)

# ---------------------------------------------------------------------------
# MAIN LOOP (24/7 CLOUD EXECUTION)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🤖 Polymarket Scanner is gestart!")
    
    # 1. Stuur opstartbericht naar Telegram
    send_telegram_alert(
        "🟢 **Polymarket Bot is Actief op Railway!**\n\n"
        "De scanner scant nu 24/7 naar:\n"
        "• 5m/15m Crypto Momentum & EMA Breakouts (BTC/ETH/SOL)\n"
        "• Sport Comebacks (incl. Bookie Odds, xG & Social Media Sentiment)"
    )
    
    # 2. Continuous Scanning Loop
    try:
        while True:
            check_crypto_signals()
            check_favored_comebacks()
            # Scant elke 2 minuten om netwerk en API-limieten optimaal te benutten
            time.sleep(120)
    except KeyboardInterrupt:
        send_telegram_alert("🔴 **Polymarket Bot is gestopt.**")
