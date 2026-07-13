"""
CropSense AI — Language Strings (STUB)
========================================
NOTE: This is a reconstructed stub for testing app.py, since the real
languages.py wasn't shared. Merge the new keys marked "NEW" below into
your actual file — especially the Luganda (lg) and Runyankole (rk)
translations, which are only in English here as placeholders.
"""

STRINGS = {
    "en": {
        "welcome": "CON Welcome to CropSense AI\n1. English\n2. Luganda\n3. Runyankole",
        "menu_main": (
            "CON CropSense AI\n"
            "1. Full crop advisory\n"
            "2. Check crop status\n"
            "3. Weather forecast\n"
            "4. Talk to extension worker\n"
            "5. What should I plant?"
        ),
        "ask_district": "CON Enter your district:",
        "ask_crop": (
            "CON Select crop:\n1. Maize\n2. Beans\n3. Cassava\n"
            "4. Sorghum\n5. Groundnuts"
        ),
        "ask_area": "CON Enter area in acres (e.g. 1.5):",
        "ask_purpose": "CON Purpose:\n1. Food\n2. Animal feed\n3. Both",
        "ask_planting_date": "CON Enter planting date (DD/MM/YYYY) or 0 if today:",
        "advisory_header": "END CropSense Advisory\n",
        "suitability_line": "{crop}: {score}% suitable\n",
        # NEW: yield line now shows a range + confidence, not a bare number
        "yield_line": "Yield: ~{yield_kg}kg/acre ({low}-{high}, {conf}% confidence)\n",
        # NEW: explainability — top limiting factor
        "limiting_factor_line": "Main limit: {factor} ({pct}% of shortfall)\n",
        "harvest_line": "Harvest in ~{days} days\n",
        "action_line": "Now: {action}\n",
        # NEW: disease/pest risk flag
        "risk_line": "Risk: {name} (~{pct}% elevated)\n",
        "tip_line": "Tip: {tip}\n",
        # NEW: honesty note when data is fallback, not live
        "fallback_note": "(Some data estimated - low signal area)\n",
        # NEW: shown when a farmer's ESP32 ground-sensor reading was used
        "sensor_note": "(Used your soil sensor reading - high accuracy)\n",
        "reply_more": "Reply for extension worker contact.",
        "status_prompt": "CON Enter: crop name + planting date\n(e.g. maize 01/06/2026)",
        "status_result": "END {crop}\nStage: {stage}\nHarvest in {days} days\nAdvice: {advice}",
        "weather_header": "END {district} 5-day forecast:\n",
        "weather_line": "Day {d}: {desc}, {rain}mm\n",
        "extension_msg": "END Extension worker: call 0800-100-100 (toll-free) or SMS FARM to 8500.",
        # NEW: input error / re-prompt strings
        "error_input": "Sorry, invalid input. ",
        "error_district": "District not recognised. Try e.g. Mbarara, Kampala, Gulu, Jinja, Mbale. ",
        "error_date": "Date not recognised or is in the future. Use DD/MM/YYYY or 0 for today. ",
        # NEW: multi-crop ranking ("what should I plant")
        "rank_header": "END Best crops for {district}:\n",
        "rank_line": "{pos}. {crop} ({score}%)\n",
    },
    # NOTE: lg (Luganda) and rk (Runyankole) intentionally omitted here —
    # merge your existing translations for the pre-existing keys, and
    # translate the NEW keys above (yield_line, limiting_factor_line,
    # risk_line, fallback_note, error_district, error_date, rank_header,
    # rank_line) to match.
}


def t(lang: str, key: str, **kwargs) -> str:
    """Look up a string by language + key, formatting with kwargs.
    Falls back to English if the language or key is missing."""
    lang_strings = STRINGS.get(lang, STRINGS["en"])
    template = lang_strings.get(key) or STRINGS["en"].get(key, "")
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template
