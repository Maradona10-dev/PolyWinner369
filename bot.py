    now_sec = time.time()
    time_remaining_15m = int(900 - (now_sec % 900))

    # STRATEGIE 1: 5M Expiry Momentum (180s - 285s resterend in 5M candle)
    if 180 <= time_remaining_sec <= 285:
        if rsi >= 51 and window_delta_pct >= min_delta and volume_ratio >= min_vol_ratio:
            if trend_15m != "BEARISH":
                return "UP", "5M_MOMENTUM", f"🚀 5M Momentum UP (+{window_delta_pct}% t.o.v. Strike, Vol {volume_ratio}x)"

        if rsi <= 49 and window_delta_pct <= -min_delta and volume_ratio >= min_vol_ratio:
            if trend_15m != "BEARISH":  # Let op: Negeer niet als 15M neutraal/bullish is
                return "DOWN", "5M_MOMENTUM", f"🚀 5M Momentum DOWN ({window_delta_pct}% t.o.v. Strike, Vol {volume_ratio}x)"

    # STRATEGIE 2: 15M Expiry Trend (Laatste 6 min van 15M candle & <= 4.5 min van 5M candle)
    if time_remaining_15m <= 360 and time_remaining_sec <= 270:
        if trend_15m == "BULLISH" and rsi >= 48 and window_delta_pct >= 0:
            return "UP", "15M_TREND", f"🌊 15M Trend Follow UP (15M RSI {rsi_15m:.1f}, 5M Delta +{window_delta_pct}%)"

        if trend_15m == "BEARISH" and rsi <= 52 and window_delta_pct <= 0:
            return "DOWN", "15M_TREND", f"🌊 15M Trend Follow DOWN (15M RSI {rsi_15m:.1f}, 5M Delta {window_delta_pct}%)"
