def calculate_size(symbol, entry_price, equity, available, stop_distance):
    meta = instrument(symbol)
    contract_value = fnum(meta.get("contractValue"))
    min_size = fnum(meta.get("minSize"))
    lot_size = fnum(meta.get("lotSize"))
    min_notional = fnum(meta.get("minNotional", 5.0)) # Veiligheidshalve minimaal exchange vereiste

    if min(
        contract_value,
        min_size,
        lot_size,
        entry_price,
        stop_distance,
    ) <= 0:
        return None

    # We willen minimaal 12 USDT marge * hefboom = totale notionele positie
    target_margin = MIN_MARGIN_USDT  # 12.0 USDT
    notional = target_margin * LEVERAGE

    # Check of we wel genoeg beschikbaar saldo hebben (88% van available * hefboom)
    max_notional = available * 0.88 * LEVERAGE
    if notional > max_notional:
        # Als er geen 12 USDT marge gepakt kan worden, slaan we de trade volledig over!
        return None

    risk_amount = notional * (stop_distance / entry_price)

    contracts = floor_step(
        notional / (contract_value * entry_price),
        lot_size,
    )
    
    # Controleer of de berekende contracten wel voldoen aan de minimum eisen van de exchange
    if contracts < min_size:
        return None

    # Dubbelcheck of de uiteindelijke notionele waarde van de contracten niet te laag is
    final_notional = contracts * contract_value * entry_price
    if (final_notional / LEVERAGE) < (MIN_MARGIN_USDT * 0.95): # Kleine marge voor afronding
        return None

    return {
        "contracts": contracts,
        "notional": final_notional,
        "risk_usdt": risk_amount,
    }
