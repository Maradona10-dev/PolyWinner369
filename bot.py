import os
import time
import requests
import numpy as np
import pandas as pd
from py_clob_client.client import ClobClient, ApiCreds
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs

# ==========================================
# 1. CONFIGURATIE EN OMGEVINGSVARIABELEN
# ==========================================
POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137")) # Polygon Mainnet
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1")) # 1 = Deposit Wallet Proxy

# Strategie instellingen
MAX_ENTRY_WINDOW_SECONDS = int(os.getenv("MAX_ENTRY_WINDOW_SECONDS", "80")) # Max 80 sec in nieuwe markt
MAX_ALLOWABLE_ODDS = float(os.getenv("MAX_ALLOWABLE_ODDS", "0.93"))         # Max koopprijs (bijv. $0.93)
TRADE_AMOUNT_USDC = float(os.getenv("TRADE_AMOUNT_USDC", "5.0"))           # Positiegrootte per trade in USDC

# Telegram meldingen (optioneel)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message})
        except Exception as e:
            print(f"Fout bij versturen Telegram bericht: {e}")

# ==========================================
# 2. CLOB CLIENT INITIALISATIE
# ==========================================
def init_clob_client():
    try:
        creds = ApiCreds(
            api_key=os.getenv("POLYMARKET_API_KEY"),
            api_secret=os.getenv("POLYMARKET_API_SECRET"),
            api_passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
        )
        client = ClobClient(
            host=POLYMARKET_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            creds=creds,
            funder=FUNDER_ADDRESS if FUNDER_ADDRESS else None,
            signature_type=SIGNATURE_TYPE
        )
        return client
    except Exception as e:
        print(f"❌ CLOB Initialisatie mislukt: {e}")
        return None

clob_client = init_clob_client()

# ==========================================
# 3. TECHNISCHE INDICATOREN (1M TIJDSFRAME)
# ==========================================
def calculate_indicators(df):
    """
    Berekent EMA 20, RSI 14 en MACD (12, 26, 9) op 1-minuut dataframe.
    """
    # 1. Exponential Moving Average (EMA 20)
    df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()

    # 2. Relative Strength Index (RSI 14)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # 3. MACD (12, 26, 9)
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()

    return df

def get_1m_strategy_signal(df_1m):
    """
    PUNT 1 & 3: Multi-indicator convergentie op 1-minuut candles.
    """
    if len(df_1m) < 30:
        return None

    df = calculate_indicators(df_1m)
    latest = df.iloc[-1]

    # Voorwaarden UP
    ema_up = latest['close'] > latest['ema_20']
    rsi_up = latest['rsi'] > 52
    macd_up = latest['macd'] > latest['macd_signal']

    # Voorwaarden DOWN
    ema_down = latest['close'] < latest['ema_20']
    rsi_down = latest['rsi'] < 48
    macd_down = latest['macd'] < latest['macd_signal']

    if ema_up and rsi_up and macd_up:
        return "UP"
    elif ema_down and rsi_down and macd_down:
        return "DOWN"

    return None

# ==========================================
# 4. MARKT- & ORDERLOGICA
# ==========================================
def evaluate_and_trade(symbol, market_data, df_1m):
    """
    Verwerkt een markt met het 80-seconden venster en plaatst orders bij convergentie.
    """
    # --- CHECK 1: EERSTE 80 SECONDEN VENSTER ---
    market_start_time = market_data.get('start_time_epoch', time.time())
    seconds_elapsed = time.time() - market_start_time

    if seconds_elapsed > MAX_ENTRY_WINDOW_SECONDS:
        print(f"⏳ [{symbol}]: Tijd voorbij ({int(seconds_elapsed)}s > {MAX_ENTRY_WINDOW_SECONDS}s). Slaat markt over.")
        return

    # --- CHECK 2: 1-MINUUT STRATEGIE SIGNAAL ---
    signal = get_1m_strategy_signal(df_1m)
    if not signal:
        print(f"⚪ [{symbol}]: Geen volledige indicator-convergentie op 1m.")
        return

    print(f"🚀 [{symbol}]: Sterk {signal}-signaal gevonden binnen {int(seconds_elapsed)}s!")

    # Haal de token ID en best beschikbare prijs op op Polymarket
    token_id = market_data.get(f'token_{signal.lower()}') # 'token_up' of 'token_down'
    if not token_id:
        print(f"⚠️ [{symbol}]: Geen token ID gevonden voor {signal}.")
        return

    try:
        # Haal orderboek op
        orderbook = clob_client.get_order_book(token_id)
        asks = orderbook.asks if hasattr(orderbook, 'asks') else orderbook.get('asks', [])

        if not asks:
            print(f"⚠️ [{symbol}]: Geen verkopers in het orderboek.")
            return

        best_price = float(asks[0].price if hasattr(asks[0], 'price') else asks[0]['price'])

        # --- CHECK 3: MAX_ALLOWABLE_ODDS PRIJSCONTROLE ---
        if best_price > MAX_ALLOWABLE_ODDS:
            msg = f"⛔ ORDER GEWEIGERD: {symbol} ({signal})\nReden: Prijs te hoog (${best_price:.3f} > max ${MAX_ALLOWABLE_ODDS:.3f})"
            print(msg)
            send_telegram(msg)
            return

        # --- EXECUTIE: ORDER PLAATSEN ---
        print(f"🛒 Order uitvoeren op Polymarket for {symbol} ({signal}) op prijs ${best_price:.3f}...")
        
        # Bereken aantal shares op basis van budget
        shares = TRADE_AMOUNT_USDC / best_price

        order_args = OrderArgs(
            price=best_price,
            size=shares,
            side="BUY",
            token_id=token_id
        )
        signed_order = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed_order)

        success_msg = f"✅ LIVE ORDER GEPLAATST!\nPair: {symbol} ({signal})\nPrijs: ${best_price:.3f}\nBedrag: ${TRADE_AMOUNT_USDC} USDC"
        print(success_msg)
        send_telegram(success_msg)

    except Exception as e:
        err_msg = f"🚨 Fout bij plaatsen order {symbol}: {e}"
        print(err_msg)
        send_telegram(err_msg)

# ==========================================
# 5. HOOFDLUS / BOT HOOFDFUNCTIE
# ==========================================
if __name__ == "__main__":
    print("🤖 Bot succesvol opgestart. Wachten op markten...")
    # Hier komt de loop die periodiek markten en 1m kaarsen ophaalt en evaluate_and_trade() aanroept.
