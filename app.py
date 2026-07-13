"""
CropSense AI — Flask USSD Backend
===================================
Receives POST requests from Africa's Talking USSD gateway.
Manages session state via the 'text' field (cumulative user inputs).
Responds with CON (continue session) or END (close session) strings.

Deploy free on Render.com or Railway.app.
Africa's Talking will POST to: https://your-app.onrender.com/ussd

Session flow:
  Step 0 : Language select
  Step 1 : Main menu
  Step 2 : District entry
  Step 3 : Crop select
  Step 4 : Area entry
  Step 5 : Purpose select
  Step 6 : Planting date entry
  Step 7 : Advisory output (END)

Parallel flows for menu options 2, 3, 4, 5.
Option 5 is new: "What should I plant?" — ranks all crops for the
farmer's district instead of requiring them to already know which
crop to ask about.

CHANGES FROM V1:
  - District input is now validated (unknown district -> re-prompt,
    not a silent fallback to Mbarara buried three steps later).
  - Crop selection re-prompts on invalid input instead of dead-ending.
  - Purpose selection re-prompts on invalid input instead of silently
    defaulting.
  - Planting date re-prompts on unparseable input instead of silently
    substituting today's date (which previously could produce a
    confidently wrong advisory with no indication anything was off).
  - Advisory output now includes: yield confidence range, an
    explainable factor breakdown, disease/pest risk flags, and a
    data-source honesty note (live vs fallback weather/soil data).
"""

from flask import Flask, request, Response, jsonify
from datetime import datetime, timedelta
from crop_engine import (
    get_coords, fetch_weather, fetch_soil, is_known_district,
    predict_yield, score_suitability, rank_crops, assess_risks,
    get_growth_stage, get_weather_summary, merge_sensor_reading,
    CROP_PARAMS, CROP_MENU_MAP,
)
from languages import t

app = Flask(__name__)

# ── ESP32 SENSOR STORE ─────────────────────────────────────────────────────────
# In-memory store of the latest ground-sensor reading per phone number.
# NOTE: this resets on every server restart/redeploy, and won't work
# across multiple server instances (e.g. Render's free tier can spin up
# fresh dynos). Fine for a competition demo; a real deployment needs a
# small persistent store (SQLite/Postgres/Redis) keyed the same way.
DEVICE_READINGS = {}
SENSOR_READING_MAX_AGE_HOURS = 72  # a reading older than this is treated
                                     # as stale and ignored (soil conditions
                                     # can genuinely shift after rain, etc.)


@app.route("/sensor", methods=["POST"])
def sensor_ingest():
    """
    Ingestion endpoint for ESP32 field devices. A farmer's ESP32 (soil pH
    probe + soil moisture + nitrogen sensor + DS18B20 temp probe, wired to
    an ESP32 dev board) POSTs a JSON reading here whenever it has a fix.

    Expected JSON body:
        {
          "phone": "+256700000000",   // links reading to the farmer's USSD sessions
          "ph": 6.1,                   // optional, from analog pH probe
          "nitrogen": 1.4,             // optional, g/kg, from NPK sensor module
          "moisture_pct": 38.2,        // optional, from capacitive soil moisture sensor
          "temperature_c": 24.5,       // optional, from DS18B20 probe
          "device_id": "esp32-001"     // optional, for multi-device farms
        }
    Only 'phone' is required — send whatever the attached sensors support.
    Returns 400 on malformed input rather than silently accepting garbage,
    since a bad soil reading could skew a yield prediction the farmer
    relies on.
    """
    data = request.get_json(silent=True)
    if not data or "phone" not in data:
        return jsonify({"error": "JSON body with 'phone' is required"}), 400

    phone = str(data["phone"]).strip()
    if not phone:
        return jsonify({"error": "'phone' cannot be empty"}), 400

    reading = {}
    for field in ("ph", "nitrogen", "moisture_pct", "temperature_c"):
        val = data.get(field)
        if val is None:
            continue
        try:
            reading[field] = float(val)
        except (TypeError, ValueError):
            return jsonify({"error": f"'{field}' must be numeric"}), 400

    # Basic plausibility bounds — reject physically impossible sensor
    # noise rather than feeding it into a yield prediction.
    bounds = {"ph": (3.0, 10.0), "nitrogen": (0.0, 10.0),
              "moisture_pct": (0.0, 100.0), "temperature_c": (-5.0, 55.0)}
    for field, (lo, hi) in bounds.items():
        if field in reading and not (lo <= reading[field] <= hi):
            return jsonify({"error": f"'{field}' out of plausible range ({lo}-{hi})"}), 400

    if not reading:
        return jsonify({"error": "no recognised sensor fields in body"}), 400

    DEVICE_READINGS[phone] = {
        **reading,
        "device_id": data.get("device_id", "unknown"),
        "received_at": datetime.utcnow(),
    }
    return jsonify({"status": "ok", "stored_fields": list(reading.keys())}), 200


def get_recent_sensor_reading(phone: str):
    """Return the farmer's latest sensor reading if it exists and isn't
    stale, else None (falls back to API/default soil data)."""
    entry = DEVICE_READINGS.get(phone)
    if not entry:
        return None
    age = datetime.utcnow() - entry["received_at"]
    if age > timedelta(hours=SENSOR_READING_MAX_AGE_HOURS):
        return None
    return entry


# ── SESSION PARSER ────────────────────────────────────────────────────────────
def parse_steps(text: str) -> list:
    """Split Africa's Talking cumulative text into individual steps."""
    return [s.strip() for s in text.split("*") if s.strip()] if text else []


def get_lang(steps: list) -> str:
    """Derive language code from step 0 selection."""
    if not steps:
        return "en"
    return {"1": "en", "2": "lg", "3": "rk"}.get(steps[0], "en")


def parse_date_safe(date_raw: str):
    """
    Parse a dd/mm/yyyy planting date. Returns (date_or_none, ok_flag).
    '0' means "planted today". Anything unparseable returns ok=False
    so the caller can re-prompt instead of silently guessing.
    """
    date_raw = date_raw.strip()
    if date_raw == "0":
        return datetime.today(), True
    try:
        parsed = datetime.strptime(date_raw, "%d/%m/%Y")
        if parsed > datetime.today():
            return None, False  # future planting dates aren't supported yet
        return parsed, True
    except ValueError:
        return None, False


# ── MAIN USSD ROUTE ───────────────────────────────────────────────────────────
@app.route("/ussd", methods=["POST"])
def ussd():
    session_id   = request.form.get("sessionId", "")
    phone        = request.form.get("phoneNumber", "")
    text         = request.form.get("text", "")

    steps = parse_steps(text)
    lang  = get_lang(steps)
    depth = len(steps)

    # ── STEP 0: Language selection ──
    if depth == 0:
        response = t("en", "welcome")  # always show language menu in English first

    # ── STEP 1: Main menu ──
    elif depth == 1:
        response = t(lang, "menu_main")

    # ── BRANCH: Option 1 — Full crop advisory ──────────────────────────────
    elif depth == 2 and steps[1] == "1":
        response = t(lang, "ask_district")

    elif depth == 3 and steps[1] == "1":
        if not is_known_district(steps[2]):
            response = t(lang, "error_district") + t(lang, "ask_district")
        else:
            response = t(lang, "ask_crop")

    elif depth == 4 and steps[1] == "1":
        if steps[3] not in CROP_MENU_MAP:
            response = t(lang, "error_input") + t(lang, "ask_crop")
        else:
            response = t(lang, "ask_area")

    elif depth == 5 and steps[1] == "1":
        try:
            if float(steps[4]) <= 0:
                raise ValueError
            response = t(lang, "ask_purpose")
        except ValueError:
            response = t(lang, "error_input") + t(lang, "ask_area")

    elif depth == 6 and steps[1] == "1":
        if steps[5] not in ("1", "2", "3"):
            response = t(lang, "error_input") + t(lang, "ask_purpose")
        else:
            response = t(lang, "ask_planting_date")

    elif depth == 7 and steps[1] == "1":
        # — All inputs collected — run the engine —
        district   = steps[2]
        crop_key   = CROP_MENU_MAP.get(steps[3], "maize")
        purpose_map = {"1": "food", "2": "feed", "3": "both"}
        purpose    = purpose_map.get(steps[5], "food")

        try:
            area_acres = float(steps[4])
        except ValueError:
            area_acres = 1.0

        planting_date, date_ok = parse_date_safe(steps[6])
        if not date_ok:
            response = t(lang, "error_date") + t(lang, "ask_planting_date")
        else:
            # Fetch data + run model
            lat, lon = get_coords(district)
            weather  = fetch_weather(lat, lon)
            soil     = fetch_soil(lat, lon)

            # If this farmer's phone has a recent ESP32 ground-sensor
            # reading, prefer it over the regional API/fallback estimate
            # for the fields it covers (pH, nitrogen, moisture) — a
            # direct in-field reading beats a ~250m-resolution API value.
            sensor = get_recent_sensor_reading(phone)
            used_sensor = False
            if sensor:
                soil = merge_sensor_reading(soil, sensor)
                used_sensor = True

            result   = predict_yield(crop_key, weather, soil,
                                     area_acres, planting_date)
            suit     = score_suitability(crop_key, weather, soil)
            stage    = get_growth_stage(crop_key, planting_date)
            risks    = assess_risks(crop_key, weather, planting_date)
            crop_p   = CROP_PARAMS[crop_key]

            # Harvest tip based on purpose
            if purpose == "feed":
                harvest_tip = crop_p["feed_harvest_tip"]
            else:
                harvest_tip = crop_p["food_harvest_tip"]

            # Build advisory response
            response = t(lang, "advisory_header")
            response += t(lang, "suitability_line",
                          score=suit, crop=crop_p["display"])
            response += t(lang, "yield_line",
                          yield_kg=f"{result['yield_per_acre']:,.0f}",
                          low=f"{result['yield_range'][0]:,.0f}",
                          high=f"{result['yield_range'][1]:,.0f}",
                          conf=result["confidence_pct"])

            # Explainability: show the top limiting factor, not the full
            # breakdown (USSD screens are ~160 chars — keep it scannable)
            limiting = [(k, v) for k, v in result["explanation"].items()
                        if v["impact"] == "limiting"]
            if limiting:
                limiting.sort(key=lambda kv: kv[1]["share_of_penalty_pct"],
                               reverse=True)
                top_factor_name, top_factor_info = limiting[0]
                response += t(lang, "limiting_factor_line",
                              factor=top_factor_name,
                              pct=top_factor_info["share_of_penalty_pct"])

            response += t(lang, "harvest_line",
                          days=result["days_to_harvest"])
            response += t(lang, "action_line",  action=stage["advice"][:60])

            # Risk flag (top 1 for USSD brevity)
            if risks:
                response += t(lang, "risk_line",
                              name=risks[0]["name"], pct=risks[0]["risk_pct"])

            response += t(lang, "tip_line",     tip=harvest_tip[:60])

            # Honesty note: tell the farmer what underpinned this reading —
            # sensor-boosted is a positive signal worth surfacing, and
            # fallback data is a caveat worth surfacing.
            if used_sensor:
                response += t(lang, "sensor_note")
            elif not result["data_sources_live"]["weather"] or \
                 not result["data_sources_live"]["soil"]:
                response += t(lang, "fallback_note")

            response += t(lang, "reply_more")

    # ── BRANCH: Option 2 — Check crop status ──────────────────────────────
    elif depth == 2 and steps[1] == "2":
        response = t(lang, "status_prompt")

    elif depth == 3 and steps[1] == "2":
        raw = steps[2].strip()
        parts = raw.rsplit(" ", 1)
        if len(parts) != 2:
            response = t(lang, "error_input")
        else:
            crop_name_raw, date_raw = parts
            # Match crop name to key; no silent maize default — re-prompt
            # instead, since mislabeling a farmer's actual crop is worse
            # than asking again.
            crop_key = next(
                (k for k, v in CROP_PARAMS.items()
                 if k in crop_name_raw.lower()), None
            )
            planting_date, date_ok = parse_date_safe(date_raw)

            if crop_key is None or not date_ok:
                response = t(lang, "error_input")
            else:
                stage = get_growth_stage(crop_key, planting_date)
                response = t(lang, "status_result",
                             crop=CROP_PARAMS[crop_key]["display"],
                             stage=stage["stage"],
                             days=stage["days"],
                             advice=stage["advice"][:70])

    # ── BRANCH: Option 3 — Weekly weather forecast ─────────────────────────
    elif depth == 2 and steps[1] == "3":
        response = t(lang, "ask_district")

    elif depth == 3 and steps[1] == "3":
        district = steps[2]
        if not is_known_district(district):
            response = t(lang, "error_district")
        else:
            lat, lon = get_coords(district)
            weather  = fetch_weather(lat, lon)
            summary  = get_weather_summary(weather, num_days=5)

            response = t(lang, "weather_header", district=district.title())
            for day_num, desc, rain in summary:
                response += t(lang, "weather_line",
                              d=day_num, desc=desc, rain=rain)

    # ── BRANCH: Option 4 — Extension worker contact ────────────────────────
    elif depth == 2 and steps[1] == "4":
        response = t(lang, "extension_msg")

    # ── BRANCH: Option 5 — What should I plant? (multi-crop ranking) ───────
    elif depth == 2 and steps[1] == "5":
        response = t(lang, "ask_district")

    elif depth == 3 and steps[1] == "5":
        district = steps[2]
        if not is_known_district(district):
            response = t(lang, "error_district")
        else:
            lat, lon = get_coords(district)
            weather  = fetch_weather(lat, lon)
            soil     = fetch_soil(lat, lon)
            top_crops = rank_crops(weather, soil, top_n=3)

            response = t(lang, "rank_header", district=district.title())
            for rank_pos, c in enumerate(top_crops, start=1):
                response += t(lang, "rank_line",
                              pos=rank_pos, crop=c["display"],
                              score=c["suitability"])

    # ── FALLBACK ────────────────────────────────────────────────────────────
    else:
        response = t(lang, "error_input")

    return Response(response, mimetype="text/plain")


# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Local testing: run with `python app.py`
    # Then simulate USSD with curl:
    # curl -X POST http://localhost:5000/ussd \
    #   -d "sessionId=test001&phoneNumber=%2B256700000000&text=1*1*Mbarara*1*2*1*01%2F06%2F2026"
    #
    # Try the new "what should I plant" flow:
    # curl -X POST http://localhost:5000/ussd \
    #   -d "sessionId=test002&phoneNumber=%2B256700000000&text=1*5*Mbarara"
    app.run(debug=True, port=5000)
