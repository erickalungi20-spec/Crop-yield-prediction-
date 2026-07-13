"""
CropSense AI — Core ML & Advisory Engine
=========================================
Handles:
  - Weather data fetch (Open-Meteo API)
  - Soil data fetch (ISRIC SoilGrids API)
  - GDD (Growing Degree Days) calculation
  - Yield prediction (rule-informed regression) + confidence range
  - Crop suitability scoring + explainable factor breakdown
  - Multi-crop ranking (top N crops for a location)
  - Basic disease & pest risk flagging (rule-based, weather-driven)
  - Stage-specific advisory generation

Called by app.py (Flask USSD handler).
Can also be run standalone for testing.

NOTE ON HONESTY: yield/suitability figures here are a transparent
rule-based model (documented FAO/CIMMYT agronomic constants), not a
trained ML model. Confidence scores reflect how well current
conditions match known optimal ranges for the crop — not a
statistical prediction interval from historical data. This is stated
plainly so it can be pitched accurately: "explainable rule-based
model, ML-upgrade path documented" rather than overclaiming AI.
"""

import requests
import math
from datetime import datetime, timedelta

# ── CROP PARAMETERS ──────────────────────────────────────────────────────────
# All agronomic constants sourced from FAO, CIMMYT, CIAT published data

CROP_PARAMS = {
    "maize": {
        "display":        "Maize (Kasooli)",
        "base_temp":      10.0,    # °C — GDD base temperature
        "gdd_maturity":   1500,    # GDD units to full maturity
        "opt_temp_min":   18.0,
        "opt_temp_max":   32.0,
        "stress_temp":    34.0,    # heat stress above this at silking
        "rain_min_mm":    500,     # seasonal minimum rainfall
        "rain_max_mm":    800,
        "ph_min":         5.5,
        "ph_max":         7.0,
        "n_fixing":       False,   # does NOT fix nitrogen
        "base_yield_kg_acre": 1800,  # kg/acre under good conditions Uganda
        "growth_days":    105,     # approximate
        "stages": {
            0:   "Germination (days 0–7)",
            7:   "Seedling establishment (days 7–21)",
            21:  "Vegetative growth (days 21–55)",
            55:  "Tasseling & silking — critical! (days 55–70)",
            70:  "Grain filling (days 70–95)",
            95:  "Maturation & drying (days 95–105)",
        },
        "stage_advice": {
            0:   "Ensure soil moisture at 60–70%. Plant seeds 5cm deep.",
            7:   "Thin to one plant per station if double-planted.",
            21:  "Apply top-dress nitrogen fertiliser (CAN/Urea) now.",
            55:  "Critical stage — do NOT allow drought stress. Irrigate if dry.",
            70:  "Reduce irrigation. Monitor for stalk borers.",
            95:  "Allow cobs to dry on stalk. Harvest when husks are brown.",
        },
        "food_harvest_tip":  "Harvest at full dry kernel stage. Store at <13% moisture.",
        "feed_harvest_tip":  "Harvest at silage stage (day ~70, 30–35% dry matter) for feed.",
        # ── Disease/pest risk rules (rule-based, weather-triggered) ──
        "risks": [
            {
                "name": "Fall Armyworm",
                "stage_range": (7, 55),   # days after planting when relevant
                "condition": lambda w: w["avg_tmax"] >= 25 and w["dry_spell"],
                "base_risk": 35,
                "advice": "Scout leaf whorls weekly. Early sign: pinhole leaf damage.",
            },
            {
                "name": "Maize Leaf Blight",
                "stage_range": (21, 95),
                "condition": lambda w: w["humid_wet"],
                "base_risk": 30,
                "advice": "High humidity favours blight. Improve field airflow/spacing.",
            },
            {
                "name": "Stalk Borer",
                "stage_range": (55, 95),
                "condition": lambda w: w["avg_tmax"] >= 27,
                "base_risk": 20,
                "advice": "Check stalks for boreholes. Remove and destroy affected plants.",
            },
        ],
    },
    "beans": {
        "display":        "Common Beans (Obwanzi/Ebijanjaalo)",
        "base_temp":      8.0,
        "gdd_maturity":   900,
        "opt_temp_min":   16.0,
        "opt_temp_max":   28.0,
        "stress_temp":    30.0,
        "rain_min_mm":    300,
        "rain_max_mm":    500,
        "ph_min":         6.0,
        "ph_max":         7.0,
        "n_fixing":       True,    # fixes nitrogen via root nodules
        "base_yield_kg_acre": 600,
        "growth_days":    80,
        "stages": {
            0:  "Germination (days 0–5)",
            5:  "Seedling (days 5–15)",
            15: "Vegetative growth (days 15–35)",
            35: "Flowering — drought-sensitive! (days 35–50)",
            50: "Pod filling (days 50–65)",
            65: "Maturation (days 65–80)",
        },
        "stage_advice": {
            0:  "Plant 4cm deep. Ensure good drainage — waterlogging kills nodules.",
            5:  "Check for damping off. Avoid excessive nitrogen fertiliser.",
            15: "Weed thoroughly. Beans fix own nitrogen — no top-dress needed.",
            35: "Critical: ensure adequate moisture during flowering. Drought = pod drop.",
            50: "Monitor for bean fly and aphids. Reduce watering.",
            65: "Harvest when 90% of pods are dry and brown.",
        },
        "food_harvest_tip":  "Harvest dry pods. Sun-dry beans for 2–3 days before storage.",
        "feed_harvest_tip":  "Harvest green biomass at pod-fill stage for silage/hay.",
        "risks": [
            {
                "name": "Bean Rust",
                "stage_range": (15, 65),
                "condition": lambda w: w["humid_wet"],
                "base_risk": 30,
                "advice": "Orange-brown pustules on leaves. Remove infected leaves early.",
            },
            {
                "name": "Bean Fly",
                "stage_range": (0, 15),
                "condition": lambda w: w["dry_spell"],
                "base_risk": 25,
                "advice": "Most damaging at seedling stage. Inspect stem base for larvae.",
            },
            {
                "name": "Aphids",
                "stage_range": (15, 50),
                "condition": lambda w: w["avg_tmax"] >= 24,
                "base_risk": 20,
                "advice": "Check underside of leaves. Aphids spread bean viruses.",
            },
        ],
    },
    "cassava": {
        "display":        "Cassava (Muwogo)",
        "base_temp":      12.0,
        "gdd_maturity":   3000,
        "opt_temp_min":   20.0,
        "opt_temp_max":   30.0,
        "stress_temp":    35.0,
        "rain_min_mm":    600,
        "rain_max_mm":    1500,
        "ph_min":         5.5,
        "ph_max":         7.5,
        "n_fixing":       False,
        "base_yield_kg_acre": 4000,
        "growth_days":    270,
        "stages": {
            0:   "Planting stakes (days 0–14)",
            14:  "Sprouting & establishment (days 14–60)",
            60:  "Rapid vegetative growth (days 60–150)",
            150: "Tuber bulking (days 150–240)",
            240: "Maturation (days 240–270+)",
        },
        "stage_advice": {
            0:   "Plant stakes at 45° angle, 25–30cm long, in well-drained soil.",
            14:  "Replace missing stakes. Weed early to reduce competition.",
            60:  "Apply potassium fertiliser. Cassava is drought-tolerant here.",
            150: "Monitor for cassava mosaic disease. Remove infected plants.",
            240: "Harvest as needed — cassava can stay in ground up to 24 months.",
        },
        "food_harvest_tip":  "Harvest fresh roots for cooking or processing within 24 months.",
        "feed_harvest_tip":  "Cassava leaves and peels are nutritious animal feed.",
        "risks": [
            {
                "name": "Cassava Mosaic Disease",
                "stage_range": (14, 270),
                "condition": lambda w: w["avg_tmax"] >= 22,
                "base_risk": 30,
                "advice": "Look for yellow/green leaf mottling. Uproot & burn infected plants.",
            },
            {
                "name": "Cassava Brown Streak",
                "stage_range": (60, 270),
                "condition": lambda w: w["humid_wet"],
                "base_risk": 20,
                "advice": "Causes root rot — often invisible above ground. Use certified stakes.",
            },
        ],
    },
    "sorghum": {
        "display":        "Sorghum (Obulo/Ente)",
        "base_temp":      8.0,
        "gdd_maturity":   1200,
        "opt_temp_min":   20.0,
        "opt_temp_max":   35.0,
        "stress_temp":    38.0,
        "rain_min_mm":    300,
        "rain_max_mm":    600,
        "ph_min":         5.5,
        "ph_max":         7.5,
        "n_fixing":       False,
        "base_yield_kg_acre": 800,
        "growth_days":    110,
        "stages": {
            0:  "Germination (days 0–7)",
            7:  "Seedling (days 7–25)",
            25: "Vegetative (days 25–60)",
            60: "Heading (days 60–80)",
            80: "Grain filling (days 80–100)",
            100:"Maturation (days 100–110)",
        },
        "stage_advice": {
            0:  "Sorghum tolerates poor soils. Plant 3–4cm deep.",
            7:  "Thin to 15–20cm spacing.",
            25: "Apply modest nitrogen. Sorghum is drought-hardy.",
            60: "Monitor for head smut and striga weed.",
            80: "Birds are the main threat — use netting or scare tactics.",
            100:"Harvest when grain hard and moisture <20%.",
        },
        "food_harvest_tip":  "Thresh and dry grain. Used for flour, porridge, local brew.",
        "feed_harvest_tip":  "Cut green sorghum at heading stage for silage.",
        "risks": [
            {
                "name": "Head Smut",
                "stage_range": (25, 80),
                "condition": lambda w: w["humid_wet"],
                "base_risk": 25,
                "advice": "Black powdery masses replace grain heads. Use resistant seed next season.",
            },
            {
                "name": "Striga Weed",
                "stage_range": (25, 60),
                "condition": lambda w: w["dry_spell"],
                "base_risk": 20,
                "advice": "Parasitic weed worsened by low soil fertility. Rotate with legumes.",
            },
        ],
    },
    "groundnuts": {
        "display":        "Groundnuts (Ebinyebwa/Ebinyeebwa)",
        "base_temp":      10.0,
        "gdd_maturity":   1100,
        "opt_temp_min":   20.0,
        "opt_temp_max":   30.0,
        "stress_temp":    33.0,
        "rain_min_mm":    400,
        "rain_max_mm":    650,
        "ph_min":         5.8,
        "ph_max":         7.0,
        "n_fixing":       True,
        "base_yield_kg_acre": 500,
        "growth_days":    100,
        "stages": {
            0:  "Germination (days 0–10)",
            10: "Seedling (days 10–25)",
            25: "Flowering & pegging (days 25–50)",
            50: "Pod development (days 50–80)",
            80: "Maturation (days 80–100)",
        },
        "stage_advice": {
            0:  "Plant shelled pods 5cm deep in well-drained, sandy-loam soil.",
            10: "Thin if overcrowded. Avoid waterlogging.",
            25: "Calcium (gypsum) applied at pegging improves pod fill.",
            50: "Reduce watering. Monitor for leaf spot and rosette virus.",
            80: "Test maturity: scrape pod — inside should be dark-coloured.",
        },
        "food_harvest_tip":  "Harvest before heavy rains to prevent aflatoxin contamination.",
        "feed_harvest_tip":  "Groundnut haulms (vines) are excellent high-protein livestock feed.",
        "risks": [
            {
                "name": "Groundnut Rosette Virus",
                "stage_range": (10, 50),
                "condition": lambda w: w["dry_spell"],
                "base_risk": 30,
                "advice": "Spread by aphids in dry spells. Rogue out stunted/mottled plants.",
            },
            {
                "name": "Leaf Spot",
                "stage_range": (25, 80),
                "condition": lambda w: w["humid_wet"],
                "base_risk": 25,
                "advice": "Dark circular spots on leaves. Avoid overhead irrigation late in day.",
            },
            {
                "name": "Aflatoxin Risk",
                "stage_range": (80, 100),
                "condition": lambda w: w["humid_wet"],
                "base_risk": 20,
                "advice": "Harvest promptly and dry well before heavy rains arrive.",
            },
        ],
    },
}

CROP_MENU_MAP = {
    "1": "maize",
    "2": "beans",
    "3": "cassava",
    "4": "sorghum",
    "5": "groundnuts",
}

# ── DISTRICT → COORDINATES ───────────────────────────────────────────────────
# Common Uganda districts. Extend as needed.
DISTRICT_COORDS = {
    "mbarara":   (-0.6072,  30.6545),
    "kampala":   ( 0.3476,  32.5825),
    "gulu":      ( 2.7748,  32.2990),
    "jinja":     ( 0.4244,  33.2041),
    "mbale":     ( 1.0840,  34.1754),
    "fort portal":(-0.6710, 30.2750),
    "lira":      ( 2.2499,  32.8998),
    "soroti":    ( 1.7148,  33.6108),
    "masaka":    (-0.3360,  31.7331),
    "arua":      ( 3.0200,  30.9100),
    "kasese":    ( 0.1833,  30.0833),
    "kabale":    (-1.2492,  29.9939),
    "hoima":     ( 1.4339,  31.3526),
    "tororo":    ( 0.6921,  34.1816),
    "entebbe":   ( 0.0512,  32.4637),
}


def get_coords(district: str):
    """Return (lat, lon) for district, defaulting to Mbarara."""
    return DISTRICT_COORDS.get(district.lower().strip(),
                               DISTRICT_COORDS["mbarara"])


def is_known_district(district: str) -> bool:
    """True if district is recognised (used for input validation upstream)."""
    return district.lower().strip() in DISTRICT_COORDS


# ── WEATHER FETCH ─────────────────────────────────────────────────────────────
def fetch_weather(lat: float, lon: float, days: int = 14) -> dict:
    """
    Fetch daily weather forecast + recent history from Open-Meteo.
    Returns dict with lists: dates, temp_max, temp_min, rainfall, humidity,
    and an 'is_live' flag so downstream code/users know if this is real
    data or a safe fallback (honesty matters more than looking polished).

    Humidity is now fetched directly (hourly relative_humidity_2m,
    collapsed to a daily mean) rather than inferred from rainfall
    frequency — previously disease-risk logic proxied "humid" purely
    from rain patterns, which is a reasonable approximation but not the
    same as measured humidity.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":  lat,
        "longitude": lon,
        "daily":     "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "hourly":    "relative_humidity_2m",
        "past_days": 7,
        "forecast_days": days,
        "timezone": "Africa/Nairobi",
    }
    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        j = r.json()
        d = j["daily"]
        humidity_daily = _daily_mean_from_hourly(
            j.get("hourly", {}).get("time", []),
            j.get("hourly", {}).get("relative_humidity_2m", []),
            d["time"],
        )
        return {
            "dates":    d["time"],
            "temp_max": d["temperature_2m_max"],
            "temp_min": d["temperature_2m_min"],
            "rain":     d["precipitation_sum"],
            "humidity": humidity_daily,
            "is_live":  True,
        }
    except Exception as e:
        print(f"[WARN] Weather API error: {e} — using fallback defaults")
        today = datetime.today()
        dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(days + 7)]
        return {
            "dates":    dates,
            "temp_max": [27.0] * len(dates),
            "temp_min": [17.0] * len(dates),
            "rain":     [3.0] * len(dates),
            "humidity": [65.0] * len(dates),  # typical Uganda mid-range fallback
            "is_live":  False,
        }


def _daily_mean_from_hourly(hourly_times, hourly_vals, daily_dates):
    """Collapse an hourly series into a per-day mean, aligned to daily_dates.
    Falls back to 65.0 for any day with no matching hourly readings —
    keeps callers simple (always get a value per date) without silently
    mixing None into downstream averages."""
    if not hourly_times or not hourly_vals:
        return [65.0] * len(daily_dates)
    buckets = {}
    for t, v in zip(hourly_times, hourly_vals):
        if v is None:
            continue
        day = t[:10]  # "YYYY-MM-DDTHH:MM" -> "YYYY-MM-DD"
        buckets.setdefault(day, []).append(v)
    out = []
    for day in daily_dates:
        vals = buckets.get(day)
        out.append(round(sum(vals) / len(vals), 1) if vals else 65.0)
    return out


# ── SOIL FETCH ────────────────────────────────────────────────────────────────
def fetch_soil(lat: float, lon: float) -> dict:
    """
    Fetch soil properties from ISRIC SoilGrids API for given coordinates.
    Returns pH, sand%, clay%, nitrogen, organic carbon (soc), and CEC
    (cation exchange capacity — a broader fertility/nutrient-holding
    indicator than pH or nitrogen alone), plus an 'is_live' flag.
    Falls back to typical Uganda loam defaults on error.
    """
    url = "https://rest.isric.org/soilgrids/v2.0/properties/query"
    params = {
        "lon": lon,
        "lat": lat,
        "property": ["phh2o", "sand", "clay", "nitrogen", "soc", "cec"],
        "depth":    ["0-5cm"],
        "value":    ["mean"],
    }
    defaults = {"ph": 6.2, "sand_pct": 40, "clay_pct": 25, "nitrogen": 1.2,
                "soc": 15.0, "cec": 12.0, "is_live": False}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        layers = r.json().get("properties", {}).get("layers", [])
        result = {}
        for layer in layers:
            name = layer["name"]
            val = layer["depths"][0]["values"]["mean"]
            if val is None:
                continue
            if name == "phh2o":
                result["ph"] = round(val / 10, 2)      # SoilGrids returns pH×10
            elif name == "sand":
                result["sand_pct"] = round(val / 10, 1)
            elif name == "clay":
                result["clay_pct"] = round(val / 10, 1)
            elif name == "nitrogen":
                result["nitrogen"] = round(val / 100, 2)
            elif name == "soc":
                result["soc"] = round(val / 10, 1)      # dg/kg -> g/kg
            elif name == "cec":
                result["cec"] = round(val / 10, 1)      # mmol(c)/kg -> cmol(c)/kg
        merged = {**defaults, **result}
        merged["is_live"] = bool(result)  # only true if we actually got values
        merged["source"] = "soilgrids_api"
        return merged
    except Exception as e:
        print(f"[WARN] SoilGrids API error: {e} — using defaults")
        defaults["source"] = "fallback_defaults"
        return defaults


def merge_sensor_reading(soil: dict, sensor: dict) -> dict:
    """
    Merge a farmer-submitted ESP32 ground-sensor reading on top of
    API/fallback soil data. Sensor fields present (ph, nitrogen,
    moisture_pct, temperature_c) override the corresponding API values,
    since a direct in-field reading is more trustworthy for that specific
    plot than a ~250m-resolution regional API estimate. Fields the
    sensor doesn't report (e.g. this ESP32 has no CEC probe) are kept
    from the API/fallback as before.

    'source' is updated to reflect the mix, and 'is_live' is forced True
    since a physical reading was just taken — this feeds into the yield
    confidence calculation.
    """
    merged = dict(soil)
    overridden = []
    for key in ("ph", "nitrogen"):
        if sensor.get(key) is not None:
            merged[key] = sensor[key]
            overridden.append(key)
    if sensor.get("moisture_pct") is not None:
        merged["moisture_pct"] = sensor["moisture_pct"]
        overridden.append("moisture_pct")
    if overridden:
        merged["source"] = f"esp32_sensor+{soil.get('source', 'api')}"
        merged["is_live"] = True
        merged["sensor_fields"] = overridden
    return merged


# ── GDD CALCULATION ───────────────────────────────────────────────────────────
def calculate_gdd(temp_max_list, temp_min_list, base_temp: float) -> list:
    """Return cumulative GDD list from daily max/min temperatures."""
    gdd_daily = []
    cumulative = 0.0
    for tmax, tmin in zip(temp_max_list, temp_min_list):
        if tmax is None or tmin is None:
            gdd_daily.append(cumulative)
            continue
        mean_temp = (tmax + tmin) / 2
        gdd = max(0.0, mean_temp - base_temp)
        cumulative += gdd
        gdd_daily.append(round(cumulative, 1))
    return gdd_daily


# ── SHARED WEATHER AGGREGATES ─────────────────────────────────────────────────
def _weather_aggregates(weather: dict) -> dict:
    """
    Single source of truth for avg_tmax / total_rain / humidity, used
    consistently by predict_yield, score_suitability, and risk flagging
    (previously these were computed slightly differently in different
    functions, which could silently disagree — e.g. filtering None vs
    falsy dropped real 0.0mm rain days from rainfall totals).

    humid_wet is now based on directly-fetched relative humidity rather
    than proxied from rainfall frequency alone — more accurate for
    disease risk, since fungal/bacterial risk tracks humidity even on
    days without active rain.
    """
    tmax_vals = [w for w in weather["temp_max"] if w is not None]
    rain_vals = [r for r in weather["rain"] if r is not None]
    humidity_vals = [h for h in weather.get("humidity", []) if h is not None]

    avg_tmax = sum(tmax_vals) / max(len(tmax_vals), 1)
    total_rain = sum(rain_vals)
    n_days = max(len(rain_vals), 1)
    avg_daily_rain = total_rain / n_days
    avg_humidity = sum(humidity_vals) / len(humidity_vals) if humidity_vals else None

    if avg_humidity is not None:
        humid_wet = avg_humidity >= 70.0
    else:
        humid_wet = avg_daily_rain >= 4.0  # fallback proxy if no humidity data

    return {
        "avg_tmax": avg_tmax,
        "total_rain": total_rain,
        "n_days": n_days,
        "avg_humidity": avg_humidity,
        "humid_wet": humid_wet,
        "dry_spell": avg_daily_rain < 1.0,    # low rain → drought/pest-favouring
    }


# ── YIELD PREDICTION ──────────────────────────────────────────────────────────
def predict_yield(crop_key: str, weather: dict, soil: dict,
                  area_acres: float, planting_date: datetime) -> dict:
    """
    Predict crop yield (kg/acre and total kg) using:
      - Crop base yield under good conditions
      - Temperature stress penalty
      - Rainfall adequacy factor
      - Soil pH suitability factor
      - Soil N penalty (for non-N-fixing crops only)

    Returns dict with yield_per_acre, total_yield, confidence range,
    and an explainable breakdown of which factors drove the result.
    """
    p = CROP_PARAMS[crop_key]
    base = p["base_yield_kg_acre"]
    agg = _weather_aggregates(weather)
    avg_tmax = agg["avg_tmax"]

    # — Temperature stress factor —
    if avg_tmax > p["stress_temp"]:
        temp_factor = max(0.5, 1 - (avg_tmax - p["stress_temp"]) * 0.04)
    elif avg_tmax < p["opt_temp_min"]:
        temp_factor = max(0.6, 1 - (p["opt_temp_min"] - avg_tmax) * 0.03)
    else:
        temp_factor = 1.0

    # — Seasonal rainfall factor (forecast period as proxy) —
    season_rain_est = agg["total_rain"] * (p["growth_days"] / agg["n_days"])
    if season_rain_est < p["rain_min_mm"]:
        rain_factor = max(0.4, season_rain_est / p["rain_min_mm"])
    elif season_rain_est > p["rain_max_mm"] * 1.5:
        rain_factor = 0.75  # waterlogging penalty
    else:
        rain_factor = min(1.0, season_rain_est / p["rain_min_mm"])

    # — Soil pH factor —
    ph = soil.get("ph", 6.5)
    if p["ph_min"] <= ph <= p["ph_max"]:
        ph_factor = 1.0
    elif ph < p["ph_min"]:
        ph_factor = max(0.6, 1 - (p["ph_min"] - ph) * 0.15)
    else:
        ph_factor = max(0.7, 1 - (ph - p["ph_max"]) * 0.15)

    # — Nitrogen + organic carbon factor (fertility, non-N-fixing crops only) —
    # Legumes fix their own N so this factor doesn't apply to them.
    # Organic carbon (soc) is a broader fertility indicator than N alone —
    # it reflects the soil's overall nutrient-holding and biological
    # activity, so it's blended in rather than relying on N in isolation.
    if not p["n_fixing"]:
        n_val = soil.get("nitrogen", 1.2)
        n_component = min(1.0, n_val / 1.5)          # 1.5 g/kg = good N level
        soc_val = soil.get("soc", 15.0)
        soc_component = min(1.0, soc_val / 20.0)      # 20 g/kg = good organic carbon
        n_factor = round((n_component + soc_component) / 2, 3)
    else:
        n_factor = 1.0  # legumes fix own N

    # — Combined yield —
    combined = temp_factor * rain_factor * ph_factor * n_factor
    yield_per_acre = round(base * combined, 0)
    total_yield    = round(yield_per_acre * area_acres, 0)

    # — Days to harvest from today (clamped so future planting dates
    #   never produce a negative "days planted" / impossible countdown) —
    days_planted = max(0, (datetime.today() - planting_date).days)
    days_remaining = max(0, p["growth_days"] - days_planted)

    # — Explainable breakdown: % contribution of each factor to the —
    # — combined penalty (how much each factor pulled yield down from 100%) —
    factors_raw = {
        "temperature": temp_factor,
        "rainfall":    rain_factor,
        "soil_ph":     ph_factor,
        "fertility":   n_factor,  # blend of nitrogen + organic carbon
    }
    total_shortfall = sum(1 - v for v in factors_raw.values()) or 1e-9
    explanation = {}
    for name, val in factors_raw.items():
        shortfall = 1 - val
        pct_of_penalty = round((shortfall / total_shortfall) * 100, 0) if total_shortfall > 1e-9 else 0
        explanation[name] = {
            "factor": round(val, 2),
            "impact": "limiting" if val < 0.95 else "optimal",
            "share_of_penalty_pct": pct_of_penalty,
        }

    # — Confidence & range: how much live vs fallback data underpins this,
    # — plus how far conditions sit from the crop's optimal band. This is
    # — an honesty/uncertainty signal, not a statistical ML prediction interval.
    # — A farmer-submitted ESP32 sensor reading (soil["source"] contains
    # — "esp32_sensor") gets an extra confidence boost, since a direct
    # — in-field reading beats a ~250m-resolution regional API estimate. —
    data_confidence = 100
    if not weather.get("is_live", True):
        data_confidence -= 30
    if not soil.get("is_live", True):
        data_confidence -= 20
    if "esp32_sensor" in soil.get("source", ""):
        data_confidence = min(100, data_confidence + 15)
    condition_confidence = round(combined * 100)
    confidence_pct = max(35, round((data_confidence + condition_confidence) / 2))

    spread = 1 - (confidence_pct / 100)  # wider range when less confident
    low_est  = round(yield_per_acre * (1 - spread * 0.5))
    high_est = round(yield_per_acre * (1 + spread * 0.3))

    return {
        "yield_per_acre": yield_per_acre,
        "total_yield":    total_yield,
        "yield_range":    (max(0, low_est), high_est),
        "days_to_harvest": days_remaining,
        "factors": {
            "temp":      round(temp_factor, 2),
            "rain":      round(rain_factor, 2),
            "ph":        round(ph_factor, 2),
            "fertility": round(n_factor, 2),
        },
        "explanation": explanation,
        "combined_score": round(combined * 100, 1),
        "confidence_pct": confidence_pct,
        "soil_source": soil.get("source", "unknown"),
        "data_sources_live": {
            "weather": weather.get("is_live", True),
            "soil":    soil.get("is_live", True),
        },
    }


# ── CROP SUITABILITY SCORING ──────────────────────────────────────────────────
def score_suitability(crop_key: str, weather: dict, soil: dict) -> int:
    """Return 0–100 suitability score for a crop given local conditions."""
    p = CROP_PARAMS[crop_key]
    agg = _weather_aggregates(weather)
    score = 100

    if not (p["opt_temp_min"] <= agg["avg_tmax"] <= p["opt_temp_max"]):
        score -= 20

    season_est = agg["total_rain"] * (p["growth_days"] / agg["n_days"])
    if season_est < p["rain_min_mm"]:
        score -= 25
    elif season_est > p["rain_max_mm"] * 1.5:
        score -= 15

    ph = soil.get("ph", 6.5)
    if not (p["ph_min"] <= ph <= p["ph_max"]):
        score -= 20

    return max(0, min(100, score))


# ── MULTI-CROP RANKING ────────────────────────────────────────────────────────
def rank_crops(weather: dict, soil: dict, top_n: int = 3) -> list:
    """
    Rank all crops in CROP_PARAMS by suitability for the given location's
    current weather/soil, best first. Returns list of dicts:
    {crop_key, display, suitability, top_risk_name or None}.
    Used to answer "what should I plant here?" rather than requiring the
    farmer to already know which crop to ask about.
    """
    ranked = []
    for key, p in CROP_PARAMS.items():
        suit = score_suitability(key, weather, soil)
        ranked.append({
            "crop_key": key,
            "display": p["display"],
            "suitability": suit,
        })
    ranked.sort(key=lambda x: x["suitability"], reverse=True)
    return ranked[:top_n]


# ── DISEASE & PEST RISK FLAGGING (rule-based) ─────────────────────────────────
def assess_risks(crop_key: str, weather: dict, planting_date: datetime,
                  top_n: int = 2) -> list:
    """
    Flag likely disease/pest risks for a crop given current weather and
    growth stage, using documented agronomic trigger conditions (humidity,
    dry spells, temperature) rather than a trained classifier.

    Returns up to top_n risks as:
    {name, risk_pct, advice}
    sorted by risk_pct descending. Empty list if nothing is currently
    elevated for this crop/stage.

    HONESTY NOTE: 'risk_pct' is a rule-based heuristic (base_risk plus a
    condition match), not a calibrated probability from historical
    outbreak data. It's presented to farmers as "elevated risk" framing,
    not a precise percentage claim, to avoid overstating certainty.
    """
    p = CROP_PARAMS[crop_key]
    agg = _weather_aggregates(weather)
    days_elapsed = max(0, (datetime.today() - planting_date).days)

    hits = []
    for rule in p.get("risks", []):
        lo, hi = rule["stage_range"]
        if not (lo <= days_elapsed <= hi):
            continue
        if not rule["condition"](agg):
            continue
        hits.append({
            "name": rule["name"],
            "risk_pct": rule["base_risk"],
            "advice": rule["advice"],
        })

    hits.sort(key=lambda x: x["risk_pct"], reverse=True)
    return hits[:top_n]


# ── GROWTH STAGE DETECTION ────────────────────────────────────────────────────
def get_growth_stage(crop_key: str, planting_date: datetime):
    """Return stage label/advice/days-to-harvest based on days since planting."""
    p = CROP_PARAMS[crop_key]
    days_elapsed = max(0, (datetime.today() - planting_date).days)

    stage_label  = list(p["stages"].values())[-1]
    stage_advice = list(p["stage_advice"].values())[-1]

    for day_threshold in sorted(p["stages"].keys(), reverse=True):
        if days_elapsed >= day_threshold:
            stage_label  = p["stages"][day_threshold]
            stage_advice = p["stage_advice"][day_threshold]
            break

    days_to_harvest = max(0, p["growth_days"] - days_elapsed)

    return {
        "stage":   stage_label,
        "advice":  stage_advice,
        "days":    days_to_harvest,
        "days_elapsed": days_elapsed,
    }


# ── WEEKLY FORECAST SUMMARY ───────────────────────────────────────────────────
def get_weather_summary(weather: dict, num_days: int = 5) -> list:
    """Return list of (day_num, description, rain_mm) for USSD display."""
    summary = []
    forecast_start = 7  # skip past 7 days
    for i in range(num_days):
        idx = forecast_start + i
        if idx >= len(weather["dates"]):
            break
        rain = weather["rain"][idx] or 0
        tmax = weather["temp_max"][idx] or 25
        if rain > 10:
            desc = "Heavy rain"
        elif rain > 2:
            desc = "Light rain"
        elif tmax > 32:
            desc = "Hot & dry"
        else:
            desc = "Mostly dry"
        summary.append((i + 1, desc, round(rain, 1)))
    return summary


# ── STANDALONE TEST ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== CropSense AI Engine — standalone test ===\n")
    lat, lon = get_coords("mbarara")
    print(f"Location: Mbarara ({lat}, {lon})")

    weather = fetch_weather(lat, lon)
    print(f"Weather fetched: {len(weather['dates'])} days (live={weather['is_live']})")

    soil = fetch_soil(lat, lon)
    print(f"Soil data: pH={soil['ph']}, sand={soil['sand_pct']}%, "
          f"clay={soil['clay_pct']}%, N={soil['nitrogen']}g/kg (live={soil['is_live']})")

    print("\n--- Top crops for this location ---")
    for r in rank_crops(weather, soil, top_n=5):
        print(f"  {r['display']:<30} suitability={r['suitability']}%")

    for crop in ["maize", "beans"]:
        plant_date = datetime.today() - timedelta(days=30)
        result = predict_yield(crop, weather, soil, 1.0, plant_date)
        suit   = score_suitability(crop, weather, soil)
        stage  = get_growth_stage(crop, plant_date)
        risks  = assess_risks(crop, weather, plant_date)

        print(f"\n--- {CROP_PARAMS[crop]['display']} ---")
        print(f"  Suitability:    {suit}%")
        print(f"  Yield estimate: {result['yield_per_acre']} kg/acre "
              f"(range {result['yield_range'][0]}-{result['yield_range'][1]}, "
              f"confidence {result['confidence_pct']}%)")
        print(f"  Total (1 acre): {result['total_yield']} kg")
        print(f"  Days to harvest:{result['days_to_harvest']}")
        print(f"  Stage:          {stage['stage']}")
        print(f"  Advice:         {stage['advice']}")
        print(f"  Explanation:")
        for name, info in result["explanation"].items():
            print(f"    - {name}: factor={info['factor']} "
                  f"({info['impact']}), share_of_penalty={info['share_of_penalty_pct']}%")
        if risks:
            print(f"  Risk flags:")
            for r in risks:
                print(f"    - {r['name']} (~{r['risk_pct']}% elevated risk): {r['advice']}")
        else:
            print("  Risk flags:     none elevated currently")
