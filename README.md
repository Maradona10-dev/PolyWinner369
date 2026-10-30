# Smart Money Concepts (SMC) Trading Bot

Een geautomatiseerde trading bot gebaseerd op het **Confirmation Model** met 4 stappen[span_1](start_span)[span_1](end_span):
1. **Liquidity Sweep**: Detectie van buy-side of sell-side sweeps op belangrijke highs/lows[span_2](start_span)[span_2](end_span).
2. **HTF Delivery**: Validatie via een Higher Timeframe (1H/15m) Fair Value Gap[span_3](start_span)[span_3](end_span).
3. **iFVG op 1m**: Zoeken naar geïnverteerde Fair Value Gaps op de 1-minutengrafiek[span_4](start_span)[span_4](end_span).
4. **CISD**: Bevestiging via Change in State of Delivery voor de daadwerkelijke entry[span_5](start_span)[span_5](end_span).

## Installatie
1. Kloon de repository:
   ```bash
   git clone [https://github.com/jouw-gebruikersnaam/my-trading-bot.git](https://github.com/jouw-gebruikersnaam/my-trading-bot.git)
