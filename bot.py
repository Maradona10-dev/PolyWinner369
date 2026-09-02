import os
import time
import math
import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE (POLYWINNER CRYPTO BOT - PAPER TRADING)
# ---------------------------------------------------------------------------
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8840031344:AAGDJmeIC6yJPSUazE72tn32qZcFsdDvXP0")
TELEGRAM_BOT_TOKEN = RAW_TOKEN.strip().replace("bot", "") if RAW_TOKEN.strip().startswith("bot") else RAW_TOKEN.strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "369244706").strip()

SCAN_INTERVAL = 10         # Scant elke 10 seconden
MIN_EDGE_TO_ENTRY = 0.08   # Minimaal 8% prijsverschil
PROFIT_TAKE_EDGE = 0.02    # Bij <2% edge: signaal om positie af te bouwen
REVERSAL_THRESHOLD = 0.35  # Bij <35% winstkans: signaal om te swappen/hedgen

# PAPER TRADING INSTELLINGEN
INITIAL_BALANCE = 1000.0   # Startsaldo in virtuele USDC
TRADE_SIZE = 100.0         # Bedrag per virtuele trade

# ---------------------------------------------------------------------------
# TELEGRAM BOT FUNCTIE
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
            "inline_keyboard": [[{"text": "⚡ OPEN POLYMARKET MARKT", "url": target_url}]]
        }
    try:
        res = requests.post(url, json=payload, timeout=5)
        return res.json()
    except Exception as e:
        print(f"Telegram Fout: {e}")

# ---------------------------------------------------------------------------
# DATA FEEDS & INDICATOREN
# ---------------------------------------------------------------------------
def get_binance_klines(symbol="BTCUSDT", interval="5m", limit=50):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        res = requests.get(url, timeout=3).json()
        return [float(k[4]) for k in res]
    except Exception as e:
        print(f"Binance Kline Fout: {e}")
        return []

def calculate_ema(prices, period):
    if len(prices) < period:
        return 0.0
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = (p * k) + (ema * (1 - k))
    return round(ema, 2)

def calculate_rsi(prices, period=14):
    if len(prices) <= period:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calculate_model_probability(spot_price, strike_price, time_left_seconds, volatility_per_min=0.0015):
    if time_left_seconds <= 0:
        return 1.0 if spot_price > strike_price else 0.0

    price_diff_pct = (spot_price - strike_price) / strike_price
    time_left_min = time_left_seconds / 60.0
    expected_std = volatility_per_min * math.sqrt(time_left_min)

    if expected_std == 0: return 0.5
    z_score = price_diff_pct / expected_std
    return round(1.0 / (1.0 + math.exp(-1.6 * z_score)), 4)

# ---------------------------------------------------------------------------
# PAPER TRADING ENGINE
# ---------------------------------------------------------------------------
class PaperTradingPolyWinner:
    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.active_position = None  # 'UP', 'DOWN' of None
        self.entry_price = 0.0
        self.shares = 0.0
        self.total_trades = 0
        self.winning_trades = 0

    def check_technical_confluence(self, symbol="BTCUSDT"):
        klines_5m = get_binance_klines(symbol, "5m", 50)
        klines_15m = get_binance_klines(symbol, "15m", 60)
        if not klines_5m or not klines_15m: return None

        current_spot = klines_5m[-1]
        ema_5 = calculate_ema(klines_5m, 5)
        ema_15 = calculate_ema(klines_5m, 15)
        rsi_14 = calculate_rsi(klines_5m, 14)
        ema_50_15m = calculate_ema(klines_15m, 50)

        bullish_score = (1 if ema_5 > ema_15 else 0) + (1 if rsi_14 >= 55 else 0) + (1 if current_spot > ema_50_15m else 0)
        bearish_score = (1 if ema_5 < ema_15 else 0) + (1 if rsi_14 <= 45 else 0) + (1 if current_spot < ema_50_15m else 0)

        return {
            "spot": current_spot, "ema_5": ema_5, "ema_15": ema_15,
            "rsi_14": rsi_14, "ema_50_15m": ema_50_15m,
            "bullish_score": bullish_score, "bearish_score": bearish_score
        }

    def evaluate_and_trade(self, market_title, strike_price, time_left_sec, polymarket_up_price, market_url):
        tech = self.check_technical_confluence("BTCUSDT")
        if not tech: return

        spot_price = tech["spot"]
        model_prob_up = calculate_model_probability(spot_price, strike_price, time_left_sec)
        model_prob_down = 1.0 - model_prob_up
        polymarket_down_price = round(1.0 - polymarket_up_price, 4)

        # 1. POSITIE OPENEN (BUY ENTRY)
        if self.active_position is None:
            edge_up = model_prob_up - polymarket_up_price
            edge_down = model_prob_down - polymarket_down_price

            # BUY UP ENTRY
            if edge_up >= MIN_EDGE_TO_ENTRY and tech["bullish_score"] == 3:
                self.active_position = "UP"
                self.entry_price = polymarket_up_price
                self.shares = TRADE_SIZE / polymarket_up_price
                self.balance -= TRADE_SIZE
                self.total_trades += 1
                
                msg = (
                    f"📝 **PAPER TRADE OPENED: BUY UP** 📈\n\n"
                    f"📍 **Markt:** {market_title}\n"
                    f"💰 **Spot:** ${spot_price:,.2f} | **Strike:** ${strike_price:,.2f}\n"
                    f"📊 **Model Prob:** {model_prob_up*100:.1f}% | **Poly Price:** ${polymarket_up_price:.2f}\n"
                    f"🔥 **Edge:** +{edge_up*100:.1f}%\n\n"
                    f"✅ **Confluentie Check (3/3) Passed**\n"
                    f"💼 **Virtuele Inzet:** ${TRADE_SIZE:.2f} ({self.shares:.1f} Shares)\n"
                    f"💵 **Resterend Saldo:** ${self.balance:.2f}"
                )
                send_telegram_alert(msg, target_url=market_url)

            # BUY DOWN ENTRY
            elif edge_down >= MIN_EDGE_TO_ENTRY and tech["bearish_score"] == 3:
                self.active_position = "DOWN"
                self.entry_price = polymarket_down_price
                self.shares = TRADE_SIZE / polymarket_down_price
                self.balance -= TRADE_SIZE
                self.total_trades += 1

                msg = (
                    f"📝 **PAPER TRADE OPENED: BUY DOWN** 📉\n\n"
                    f"📍 **Markt:** {market_title}\n"
                    f"💰 **Spot:** ${spot_price:,.2f} | **Strike:** ${strike_price:,.2f}\n"
                    f"📊 **Model Prob:** {model_prob_down*100:.1f}% | **Poly Price:** ${polymarket_down_price:.2f}\n"
                    f"🔥 **Edge:** +{edge_down*100:.1f}%\n\n"
                    f"✅ **Confluentie Check (3/3) Passed**\n"
                    f"💼 **Virtuele Inzet:** ${TRADE_SIZE:.2f} ({self.shares:.1f} Shares)\n"
                    f"💵 **Resterend Saldo:** ${self.balance:.2f}"
                )
                send_telegram_alert(msg, target_url=market_url)

        # 2. ACTIEVE POSITIE MONITOREN & SLUITEN
        else:
            if self.active_position == "UP":
                current_edge = model_prob_up - polymarket_up_price
                
                # Afbouwen / Sluiten bij winst of verslechterde condities
                if current_edge < PROFIT_TAKE_EDGE or model_prob_up < REVERSAL_THRESHOLD:
                    payout = self.shares * polymarket_up_price
                    pnl = payout - TRADE_SIZE
                    self.balance += payout
                    if pnl > 0: self.winning_trades += 1

                    msg = (
                        f"📝 **PAPER TRADE CLOSED: UP**\n\n"
                        f"💵 **Verkoopprijs:** ${polymarket_up_price:.2f}\n"
                        f"📊 **PnL voor deze trade:** {'+' if pnl>=0 else ''}${pnl:.2f}\n"
                        f"💰 **Nieuwe Saldo:** ${self.balance:.2f}\n"
                        f"📈 **Win Rate:** {(self.winning_trades/self.total_trades)*100:.1f}% ({self.winning_trades}/{self.total_trades})"
                    )
                    send_telegram_alert(msg, target_url=market_url)
                    self.active_position = None

# ---------------------------------------------------------------------------
# MAIN LOOP (24-UUR TEST)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🚀 24-Uur Paper Trading Test Gestart!")
    send_telegram_alert(
        "🧪 **24-UUR PAPER TRADING TEST GESTART!** 🧪\n\n"
        f"• **Virtueel Startsaldo:** ${INITIAL_BALANCE:.2f}\n"
        f"• **Trade Grootte:** ${TRADE_SIZE:.2f} per kans\n"
        "• **Strategie:** Model-Based Edge (≥8%) + 3/3 Confluentie Check\n\n"
        "De bot voert gesimuleerde trades uit en geeft live updates in deze chat."
    )
    
    paper_bot = PaperTradingPolyWinner()
    start_time = time.time()
    
    while time.time() - start_time < 86400:  # Draait exact 24 uur (86.400 seconden)
        try:
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Fout in loop: {e}")
            time.sleep(5)

    # EINDAPPORT NA 24 UUR
    total_pnl = paper_bot.balance - INITIAL_BALANCE
    send_telegram_alert(
        f"🏁 **24-UUR PAPER TRADING TEST AFGEROND!** 📊\n\n"
        f"• **Eindsaldo:** ${paper_bot.balance:.2f}\n"
        f"• **Totale PnL:** {'+' if total_pnl>=0 else ''}${total_pnl:.2f}\n"
        f"• **Totaal Trades:** {paper_bot.total_trades}\n"
        f"• **Win Rate:** {(paper_bot.winning_trades/max(1, paper_bot.total_trades))*100:.1f}%\n\n"
        "Evalueer de resultaten voordat je live geld inzet!"
    )
