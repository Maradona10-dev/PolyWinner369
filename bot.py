import os
import time
import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE
# ---------------------------------------------------------------------------
RAW_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8840031344:AAGDJmeIC6yJPSUazE72tn32qZcFsdDvXP0")
TELEGRAM_BOT_TOKEN = RAW_TOKEN.strip().replace("bot", "") if RAW_TOKEN.strip().startswith("bot") else RAW_TOKEN.strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "369244706").strip()

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
DOME_CHAINLINK_URL = "https://api.domeapi.io/v1/crypto-prices/chainlink"
POLYMARKET_BTC_5MIN_URL = "https://polymarket.com/market/btc-updown-5m"

LAST_SIGNAL = None
LAST_OFFSET = 0

# PAPER TRADING CONFIGURATIE
PAPER_STAKE = 10.0
TEST_DURATION_HOURS = 24
START_TIME = time.time()

PAPER_STATE = {
    "balance": 100.0,
    "start_balance": 100.0,
    "trades": [],
    "total_trades": 0,
    "wins": 0,
    "losses": 0
}

# ---------------------------------------------------------------------------
# TELEGRAM FUNCTIES
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
            "inline_keyboard": [
                [
                    {"text": "🚀 ZET DIRECT IN OP POLYMARKET", "url": target_url}
                ]
            ]
        }
        
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"Telegram Fout: {e}")

# ---------------------------------------------------------------------------
# DATA OPHALEN & METRICS
# ---------------------------------------------------------------------------
def fetch_binance_klines(interval="5m", limit=60):
    params = {"symbol": "BTCUSDT", "interval": interval, "limit": limit}
    try:
        res = requests.get(BINANCE_KLINES_URL, params=params, timeout=5)
        if res.status_code == 200:
            return [float(k[4]) for k in res.json()]
    except Exception as e:
        print(f"Fout bij ophalen Binance data: {e}")
    return []

def fetch_chainlink_price():
    params = {"currency": "btc/usd"}
    try:
        res = requests.get(DOME_CHAINLINK_URL, params=params, timeout=5)
        if res.status_code == 200:
            prices = res.json().get("prices", [])
            if prices:
                return float(prices[0].get("value", 0))
    except Exception:
        pass
    return None

def calculate_ema(prices, period):
    if len(prices) < period:
        return 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(abs(change))
            losses.append(abs(change))
            
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------------------------------------------------------------------
# PAPER TRADING LOGICA (SETTLEMENT & REPORTING)
# ---------------------------------------------------------------------------
def execute_paper_trade(direction, price):
    global PAPER_STATE
    
    if PAPER_STATE["balance"] < PAPER_STAKE:
        send_telegram_alert("⚠️ **Paper Trading Alert:** Onvoldoende virtueel saldo voor een nieuwe positie.")
        return

    PAPER_STATE["balance"] -= PAPER_STAKE
    PAPER_STATE["total_trades"] += 1

    trade_entry = {
        "id": PAPER_STATE["total_trades"],
        "asset": "BTC/USDT",
        "direction": direction,
        "entry_price": price,
        "stake": PAPER_STAKE,
        "status": "OPEN",
        "entry_time": time.time(),
        "entry_time_str": time.strftime("%H:%M:%S")
    }
    
    PAPER_STATE["trades"].append(trade_entry)

    report = (
        f"📝 **PAPER TRADE GEOPEND (SIMULATIE)** 🤖\n\n"
        f"**Asset:** BTC/USDT 5M\n"
        f"🎯 **Richting:** {direction}\n"
        f"💰 **Virtuele Inzet:** ${PAPER_STAKE:.2f} @ Koers: ${price:,.2f}\n"
        f"💵 **Resterend Saldo:** ${PAPER_STATE['balance']:.2f}\n"
        f"⏱ **Verwachte Afloop:** over ~5 minuten"
    )
    send_telegram_alert(report)

def process_open_trades():
    global PAPER_STATE
    current_time = time.time()
    
    chainlink_price = fetch_chainlink_price()
    closes = fetch_binance_klines(interval="5m", limit=5)
    exit_price = chainlink_price if chainlink_price else (closes[-1] if closes else None)

    if not exit_price:
        return

    for trade in PAPER_STATE["trades"]:
        if trade["status"] == "OPEN" and (current_time - trade["entry_time"]) >= 300:
            win = False
            if trade["direction"] == "UP" and exit_price > trade["entry_price"]:
                win = True
            elif trade["direction"] == "DOWN" and exit_price < trade["entry_price"]:
                win = True

            if win:
                profit = trade["stake"] * 1.85
                PAPER_STATE["balance"] += profit
                PAPER_STATE["wins"] += 1
                trade["status"] = "WIN"
                res_msg = f"✅ **PAPER TRADE GEWONNEN!** 🎉\n\nTrade #{trade['id']} ({trade['direction']})\nEntry: ${trade['entry_price']:,.2f} ➔ Exit: ${exit_price:,.2f}\n💰 +${profit - trade['stake']:.2f} Winst | Saldo: ${PAPER_STATE['balance']:.2f}"
            else:
                PAPER_STATE["losses"] += 1
                trade["status"] = "LOSS"
                res_msg = f"❌ **PAPER TRADE VERLOREN** 📉\n\nTrade #{trade['id']} ({trade['direction']})\nEntry: ${trade['entry_price']:,.2f} ➔ Exit: ${exit_price:,.2f}\n💸 -${trade['stake']:.2f} | Saldo: ${PAPER_STATE['balance']:.2f}"
            
            send_telegram_alert(res_msg)

def show_paper_portfolio():
    open_trades = [t for t in PAPER_STATE["trades"] if t["status"] == "OPEN"]
    elapsed_hours = round((time.time() - START_TIME) / 3600, 1)
    winrate = (PAPER_STATE["wins"] / (PAPER_STATE["wins"] + PAPER_STATE["losses"]) * 100) if (PAPER_STATE["wins"] + PAPER_STATE["losses"]) > 0 else 0
    
    msg = (
        f"📊 **24H TEST PORTFOLIO OVERZICHT** ({elapsed_hours}u/24u)\n\n"
        f"💵 **Huidig Saldo:** ${PAPER_STATE['balance']:.2f} (Start: $100.00)\n"
        f"📈 **Netto PnL:** ${PAPER_STATE['balance'] - PAPER_STATE['start_balance']:.2f}\n"
        f"🎯 **Winrate:** {winrate:.1f}% ({PAPER_STATE['wins']}W - {PAPER_STATE['losses']}L)\n"
        f"🔢 **Totaal Trades Executed:** {PAPER_STATE['total_trades']}\n"
        f"📂 **Openstaande Posities:** {len(open_trades)}"
    )
    send_telegram_alert(msg)

# ---------------------------------------------------------------------------
# TELEGRAM COMMANDOS
# ---------------------------------------------------------------------------
def check_telegram_commands():
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
                            "🤖 **PolyWinner 5M 24H Test Bot**\n\n"
                            "• `/portfolio` - Bekijk live PnL, winrate en saldo\n"
                            "• `/status` - Bekijk actieve bot instellingen\n"
                            "• `/scan` - Voer direct een handmatige koerscheck uit"
                        )
                        send_telegram_alert(help_msg)
                    elif text == "/portfolio":
                        show_paper_portfolio()
                    elif text == "/status":
                        status_msg = f"📊 **BOT STATUS**\n\n✅ **Status:** PolyWinner 24H Paper Test Actief\n💰 **Saldo:** ${PAPER_STATE['balance']:.2f}"
                        send_telegram_alert(status_msg)
                    elif text == "/scan":
                        send_telegram_alert("🔍 Handmatige markt-scan wordt uitgevoerd...")
                        check_crypto_confluence()
    except Exception as e:
        print(f"Fout bij Telegram updates: {e}")

# ---------------------------------------------------------------------------
# CRYPTO SCANNER LOGICA
# ---------------------------------------------------------------------------
def check_crypto_confluence():
    global LAST_SIGNAL
    
    closes_5m = fetch_binance_klines(interval="5m", limit=60)
    if not closes_5m:
        return

    binance_price = closes_5m[-1]
    ema_5 = calculate_ema(closes_5m, 5)
    ema_15 = calculate_ema(closes_5m, 15)
    rsi = calculate_rsi(closes_5m, 14)

    closes_15m = fetch_binance_klines(interval="15m", limit=60)
    ema_50_15m = calculate_ema(closes_15m, 50) if closes_15m else binance_price

    chainlink_price = fetch_chainlink_price()
    active_price = chainlink_price if chainlink_price else binance_price
    price_source = "Chainlink Oracle" if chainlink_price else "Binance Feed"

    current_time_sec = time.time()
    seconds_in_5m = 300
    time_remaining_min = round((seconds_in_5m - (current_time_sec % seconds_in_5m)) / 60, 1)

    bullish_confluence = (ema_5 > ema_15) and (rsi > 60) and (active_price > ema_50_15m)
    bearish_confluence = (ema_5 < ema_15) and (rsi < 40) and (active_price < ema_50_15m)

    if bearish_confluence and LAST_SIGNAL != "DOWN":
        LAST_SIGNAL = "DOWN"
        msg = (
            f"🚨 **HIGH CONFIDENCE BREAKDOWN (DOWN)** 📉\n\n"
            f"**Pair:** BTC/USDT\n"
            f"**Live Prijs:** ${active_price:,.2f} (*{price_source}*)\n"
            f"**Tijd over in ronde:** ~{time_remaining_min} minuten\n\n"
            f"**Confluentie Check (3/3):**\n"
            f"✅ **5M EMA Crossover:** EMA 5 (${ema_5:,.2f}) < EMA 15 (${ema_15:,.2f})\n"
            f"✅ **RSI (14):** {rsi:.1f} (Zwak momentum)\n"
            f"✅ **15M Trend:** Onder EMA 50 (${ema_50_15m:,.2f})\n\n"
            f"💡 **Actie:** Zet €5.00 of €10.00 in op **'DOWN'**"
        )
        send_telegram_alert(msg, target_url=POLYMARKET_BTC_5MIN_URL)
        execute_paper_trade("DOWN", active_price)

    elif bullish_confluence and LAST_SIGNAL != "UP":
        LAST_SIGNAL = "UP"
        msg = (
            f"🚀 **HIGH CONFIDENCE BREAKOUT (UP)** 📈\n\n"
            f"**Pair:** BTC/USDT\n"
            f"**Live Prijs:** ${active_price:,.2f} (*{price_source}*)\n"
            f"**Tijd over in ronde:** ~{time_remaining_min} minuten\n\n"
            f"**Confluentie Check (3/3):**\n"
            f"✅ **5M EMA Crossover:** EMA 5 (${ema_5:,.2f}) > EMA 15 (${ema_15:,.2f})\n"
            f"✅ **RSI (14):** {rsi:.1f} (Sterk momentum)\n"
            f"✅ **15M Trend:** Boven EMA 50 (${ema_50_15m:,.2f})\n\n"
            f"💡 **Actie:** Zet €5.00 of €10.00 in op **'UP'**"
        )
        send_telegram_alert(msg, target_url=POLYMARKET_BTC_5MIN_URL)
        execute_paper_trade("UP", active_price)

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🤖 PolyWinner 24-Uurs Paper Trading Bot Gestart!")
    send_telegram_alert(
        "🏆 **POLYWINNER BOT HERSTART MET GEFIXTE LINK!** 🚀\n\n"
        "• **Directe Link:** Direct werkende route naar BTC 5M-markt\n"
        "• **Startsaldo:** $100.00 (Virtueel)\n"
        "• **Inzet per trade:** $10.00\n"
        "💬 *Gebruik `/portfolio` om de tussenstand te bekijken.*"
    )
    
    while True:
        try:
            check_telegram_commands()
            check_crypto_confluence()
            process_open_trades()
            time.sleep(15)
        except Exception as e:
            print(f"Fout in main loop: {e}")
            time.sleep(10)
