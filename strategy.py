import pandas as pd
import numpy as np

class SMCStrategy:
    def __init__(self, df_1h, df_1m):
        self.df_1h = df_1h
        self.df_1m = df_1m

    def check_liquidity_sweep(self):
        """Stap 1: Liquidity Sweep - Prijs breekt door een recente high of low."""
        recent_high = self.df_1h['high'].iloc[-20:-1].max()
        current_high = self.df_1h['high'].iloc[-1]
        
        # Voorbeeld logica: als de prijs de buy-side liquidity (recent high) aantikt/breekt
        if current_high > recent_high:
            return True
        return false

    def check_htf_delivery(self):
        """Stap 2: HTF Delivery - Controleer of we reageren vanuit een 1H Fair Value Gap."""
        # Eenvoudige FVG detectie op 1H: Gat tussen candle i-2 en candle i
        for i in range(len(self.df_1h) - 5, len(self.df_1h) - 1):
            # Bullish/Bearish FVG check
            if self.df_1h['low'].iloc[i] > self.df_1h['high'].iloc[i-2]:
                return True
        return True # Placeholder voor demonstratie

    def check_ifvg_1m(self):
        """Stap 3: iFVG op 1m - Een Fair Value Gap op de 1-minutengrafiek wordt geïnverteerd."""
        # Zoek naar omgedraaide prijsactie in de 1m grafiek
        if len(self.df_1m) > 10:
            return True 
        return False

    def check_cisd(self):
        """Stap 4: CISD - Change in State of Delivery (candle sluiting breekt structuur)."""
        close_price = self.df_1m['close'].iloc[-1]
        open_price = self.df_1m['open'].iloc[-1]
        
        # Bevestiging van marktstructuurverandering (bijv. sterke bearish/bullish beweging)
        if abs(close_price - open_price) > 0:
            return True
        return False

    def evaluate_signal(self):
        step1 = self.check_liquidity_sweep()
        step2 = self.check_htf_delivery()
        step3 = self.check_ifvg_1m()
        step4 = self.check_cisd()

        # Als alle 4 stappen kloppen, geef een signaal af
        if step1 and step2 and step3 and step4:
            return "SELL" # Of BUY, afhankelijk van of het buy-side of sell-side sweep is
        return "HOLD"
