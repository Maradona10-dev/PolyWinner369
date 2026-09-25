def is_a_setup(signal):
    return bool(
        signal.get("entry_route") == "EARLY"
        and int(signal.get("confirm_components", 0)) >= 4
        and bool(signal.get("rejection"))
        and bool(signal.get("micro_break"))
        and int(signal.get("entry_quality", 0)) >= 3
        and int(signal.get("tactics", 0)) >= 4
        and safe_float(signal.get("adx")) >= max(EARLY_MIN_ADX, 22.0)
        and not bool(signal.get("rs_conflict_hard"))
        and not (
            bool(signal.get("recent_impulse"))
            and not bool(signal.get("impulse_reset"))
        )
    )
