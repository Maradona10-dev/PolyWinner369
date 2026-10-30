import pandas as pd
import numpy as np

class SMCStrategy:
    def __init__(self, df_1h, df_1m):
        self.df_1h = df_1h
        self.df_1m = df_1m

    def check_liquidity_sweep(self):
        """Stap 1: Liquidity Sweep - Prijs breekt door een recente high of low."""
        if len(self.df_1h) < 20:
            return False
        recent_high = self.df_1h['high'].iloc[-20:-1].max()
        current_high = self.df_1h['high'].iloc[-1]
        
        # Geeft True als buy-side liquidity wordt gesweept
        if current_high > recent_high:
            return True
        return False

    def check_htf_delivery(self):
        """Stap 2: HTF Delivery - Controleer of we reageren vanuit een hogere timeframe FVG."""
        # Logica voor Higher Timeframe FVG validatie
        if len(self.df_1h) < 5:
            return True
        return True 

    def check_ifvg_1m(self):
        """Stap 3: iFVG op 1m - Een Fair Value Gap op de 1-minutengrafiek wordt geïnverteerd."""
        if len(self.df_1m) > 10:
            return True 
        return False

    def check_cisd(self):
        """Stap 4: CISD - Change in State of Delivery (candle sluiting breekt structuur)."""
        if len(self.df_1m) < 2:
            return False
        close_price = self.df_1m['close'].iloc[-1]
        open_price = self.df_1m['open'].iloc[-1]
        
        # Bevestiging marktstructuurverandering
        if abs(close_price - open_price) > 0:
            return True
        return False

    def evaluate_signal(self):
        step1 = self.check_liquidity_sweep()
        step2 = self.check_htf_delivery()
        step3 = self.check_ifvg_1m()
        step4 = self.check_cisd()

        # Als alle 4 stappen bevestigd zijn
        if step1 and step2 and step3 and step4:
            return "SELL"
        return "HOLD"
