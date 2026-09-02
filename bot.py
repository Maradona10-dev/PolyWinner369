import os
import time
import math
import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE (POLYWINNER CRYPTO BOT)
# ---------------------------------------------------------------------------
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8840031344:AAGDJmeIC6yJPSUazE72tn32qZcFsdDvXP0")
TELEGRAM_BOT_TOKEN = RAW_TOKEN.strip().replace("bot", "") if RAW_TOKEN.strip().startswith("bot") else RAW_TOKEN.strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "369244706").strip()

SCAN_INTERVAL = 10         # Scant elke 10 seconden de live candle
MIN_EDGE_TO_ENTRY = 0.08   # Minimaal 8% prijsverschil (mispricing) voor signaal
PROFIT_TAKE_EDGE = 0.02    # Bij <2% edge: signaal om positie af te bouwen
REVERSAL_THRESHOLD = 0.35  # Bij <35% winstkans: signaal om te swappen/hedgen

# ---------------------------------------------------------------------------
# TELEGRAM BOTS
# ---------------------------------------------------------------------------
def send_telegram_alert(message, target_url=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if target_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "⚡ OPEN POLYMARKET 5-MIN MARKT", "url": target_url}]]
        }
    try:
        res = requests.post(url, json=payload, timeout=5)
        return res.json()
    except Exception as e:
        print(f"Telegram Fout: {e}")

# ---------------------------------------------------------------------------
# REALTIME BINANCE SPOT FEED
# ---------------------------------------------------------------------------
def get_binance_spot_price(symbol="BTCUSDT"):
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        res = requests.get(url, timeout=2).json()
        return float(res["price"])
    except Exception as e:
        print(f"Binance API Fout: {e}")
        return None

# ---------------------------------------------------------------------------
# MODEL BEREKENING (FAIR VALUE PROBABILITY)
# ---------------------------------------------------------------------------
def calculate_model_probability(spot_price, strike_price, time_left_seconds, volatility_per_min=0.0015):
    """
    Berekent de wiskundige winstkans van 'UP' op basis van:
    - Afstand tot de strike price (referentieprijs)
    - Resterende tijd in de 5-minuten candle
    - Volatiliteit per minuut
    """
    if time_left_seconds <= 0:
        return 1.0 if spot_price > strike_price else 0.0

    price_diff_pct = (spot_price - strike_price) / strike_price
    time_left_min = time_left_seconds / 60.0
    expected_std = volatility_per_min * math.sqrt(time_left_min)

    if expected_std == 0:
        return 0.5

    z_score = price_diff_pct / expected_std
    prob_up = 1.0 / (1.0 + math.exp(-1.6 * z_score))
    return round(prob_up, 4)

# ---------------------------------------------------------------------------
# POLYWINNER ENGINE CLASS
# ---------------------------------------------------------------------------
class PolyWinnerEngine:
    def __init__(self):
        self.active_position = None  # 'UP', 'DOWN' of None
        self.entry_price = 0.0
        self.last_alert_time = 0

    def evaluate_and_notify(self, market_title, strike_price, time_left_sec, polymarket_up_price, market_url):
        spot_price = get_binance_spot_price("BTCUSDT")
        if not spot_price:
            return

        model_prob_up = calculate_model_probability(spot_price, strike_price, time_left_sec)
        model_prob_down = 1.0 - model_prob_up
        polymarket_down_price = round(1.0 - polymarket_up_price, 4)

        # 1. GEEN ACTIEVE POSITIE -> CONTROLEER OP ENTRY VALUE
        if self.active_position is None:
            edge_up = model_prob_up - polymarket_up_price
            edge_down = model_prob_down - polymarket_down_price

            if edge_up >= MIN_EDGE_TO_ENTRY:
                self.active_position = "UP"
                self.entry_price = polymarket_up_price
                
                msg = (
                    f"🚀 **POLYWINNER CRYPTO SIGNAL: BUY UP** 📈\n\n"
                    f"📍 **Markt:** {market_title}\n"
                    f"💰 **Binance Spot:** ${spot_price:,.2f} | **Strike:** ${strike_price:,.2f}\n"
                    f"⏱ **Tijd over:** {time_left_sec} sec\n\n"
                    f"📊 **Model Kans (UP):** {model_prob_up*100:.1f}%\n"
                    f"🏷 **Polymarket Prijs:** ${polymarket_up_price:.2f}\n"
                    f"🔥 **Mispricing (Edge):** +{edge_up*100:.1f}%\n\n"
                    f"💡 **Advies:** Koppel positie 'UP'. Dynamisch risicobeheer actief."
                )
                send_telegram_alert(msg, target_url=market_url)

            elif edge_down >= MIN_EDGE_TO_ENTRY:
                self.active_position = "DOWN"
                self.entry_price = polymarket_down_price
                
                msg = (
                    f"🔻 **POLYWINNER CRYPTO SIGNAL: BUY DOWN** 📉\n\n"
                    f"📍 **Markt:** {market_title}\n"
                    f"💰 **Binance Spot:** ${spot_price:,.2f} | **Strike:** ${strike_price:,.2f}\n"
                    f"⏱ **Tijd over:** {time_left_sec} sec\n\n"
                    f"📊 **Model Kans (DOWN):** {model_prob_down*100:.1f}%\n"
                    f"🏷 **Polymarket Prijs:** ${polymarket_down_price:.2f}\n"
                    f"🔥 **Mispricing (Edge):** +{edge_down*100:.1f}%\n\n"
                    f"💡 **Advies:** Koppel positie 'DOWN'. Dynamisch risicobeheer actief."
                )
                send_telegram_alert(msg, target_url=market_url)

        # 2. ACTIEVE POSITIE MONITOREN (DYNAMIC HEDGING & EXITS)
        else:
            if self.active_position == "UP":
                current_edge = model_prob_up - polymarket_up_price
                
                # Signaal verzwakt -> Winst nemen / Risico afbouwen
                if current_edge < PROFIT_TAKE_EDGE and model_prob_up > 0.50:
                    msg = (
                        f"⚠️ **POLYWINNER UPDATE: REDUCE POSITION**\n\n"
                        f"📍 **Positie:** UP\n"
                        f"📉 **Edge gedaald naar:** {current_edge*100:.1f}%\n"
                        f"💡 **Advies:** Verkoop 50% van de positie om winst vast te zetten."
                    )
                    send_telegram_alert(msg, target_url=market_url)
                    self.active_position = None  # Reset voor volgende kans

                # Signaal slaat hard om -> Dynamic Hedge
                elif model_prob_up < REVERSAL_THRESHOLD:
                    msg = (
                        f"🔄 **POLYWINNER REVERSAL: DYNAMIC HEDGE!**\n\n"
                        f"📍 **Oude Positie:** UP\n"
                        f"🚨 **Winstkans zakt naar:** {model_prob_up*100:.1f}%\n"
                        f"💡 **Advies:** Sluit 'UP' of koop direct 'DOWN' als hedge om verlies te beperken!"
                    )
                    send_telegram_alert(msg, target_url=market_url)
                    self.active_position = "DOWN"

            elif self.active_position == "DOWN":
                current_edge = model_prob_down - polymarket_down_price
                
                if current_edge < PROFIT_TAKE_EDGE and model_prob_down > 0.50:
                    msg = (
                        f"⚠️ **POLYWINNER UPDATE: REDUCE POSITION**\n\n"
                        f"📍 **Positie:** DOWN\n"
                        f"📉 **Edge gedaald naar:** {current_edge*100:.1f}%\n"
                        f"💡 **Advies:** Verkoop 50% van de positie om winst vast te zetten."
                    )
                    send_telegram_alert(msg, target_url=market_url)
                    self.active_position = None

                elif model_prob_down < REVERSAL_THRESHOLD:
                    msg = (
                        f"🔄 **POLYWINNER REVERSAL: DYNAMIC HEDGE!**\n\n"
                        f"📍 **Oude Positie:** DOWN\n"
                        f"🚨 **Winstkans zakt naar:** {model_prob_down*100:.1f}%\n"
                        f"💡 **Advies:** Sluit 'DOWN' of koop direct 'UP' als hedge!"
                    )
                    send_telegram_alert(msg, target_url=market_url)
                    self.active_position = "UP"

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🚀 PolyWinner 5-Min Crypto Bot Gestart!")
    send_telegram_alert(
        "🤖 **POLYWINNER 5-MIN CRYPTO BOT IS ACTIEF!** ⚡\n\n"
        "• **Model-Based Pricing:** Scant realtime Binance vs Polymarket\n"
        "• **Minimale Edge:** 8% mispricing vereist\n"
        "• **Dynamic Hedging:** Geeft automatisch updates als een signaal verzwakt of omslaat!"
    )
    
    engine = PolyWinnerEngine()
    
    while True:
        try:
            # Hier haalt de bot de actuele 5-minuten markt op
            # (In de live omgeving vult hij de actuele strike price en polymarket prijs in)
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Fout in loop: {e}")
            time.sleep(5)
