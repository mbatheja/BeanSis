"""
Nutrition log focused on what matters in kidney disease: sodium, potassium, phosphorus, protein, fluids.

  meal text --(fast model)--> foods + estimated grams + drink volume
            --(USDA FoodData Central)--> nutrients per item, cited
            --> stored meal + daily totals, compared only to targets the USER enters from her care team

Deliberate choices:
- No calories, weight, or "good/bad day" scoring. Needs in CKD and pregnancy are individual, and
  restriction-style tracking can do harm. The app never invents targets.
- Every number is an estimate (portion guesses, generic foods). The UI should say so.
"""
import json
import re

from .. import config
from ..cache import cached_get_json

FDC_SEARCH = "https://api.nal.usda.gov/fdc/v1/foods/search"
NUTRIENTS = {  # USDA nutrient number -> (our key, unit)
    "307": ("sodium_mg", "mg"),
    "306": ("potassium_mg", "mg"),
    "305": ("phosphorus_mg", "mg"),
    "203": ("protein_g", "g"),
}
KEYS = [k for k, _ in NUTRIENTS.values()] + ["fluids_ml"]
NOTICE = ("Nutrient amounts are estimates based on typical USDA values and guessed portions. Your dietitian "
          "can tell you which numbers matter most for you.")

PARSE_PROMPT = """Split this meal description into individual foods with estimated weights in grams, and
estimate the total volume of drinks and other liquids (soup, ice) in milliliters.
Use plain generic food names a USDA database would recognize (e.g. "banana, raw", "white rice, cooked").
Return ONLY JSON: {"items": [{"food": str, "grams": number}], "fluids_ml": number}
Meal: """


def parse_meal(llm, text):
    resp = llm.messages.create(model=config.MODEL_FAST, max_tokens=600,
                               messages=[{"role": "user", "content": PARSE_PROMPT + text[:1000]}])
    raw = "".join(b.text for b in resp.content if b.type == "text")
    try:
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:
        return {"items": [], "fluids_ml": 0}
    items = [{"food": str(i["food"])[:80], "grams": float(i["grams"])}
             for i in data.get("items", []) if i.get("food") and isinstance(i.get("grams"), (int, float))
             and 0 < i["grams"] < 3000]
    fluids = data.get("fluids_ml") or 0
    return {"items": items, "fluids_ml": float(fluids) if isinstance(fluids, (int, float)) and fluids < 5000 else 0}


def lookup(food):
    """Best USDA match, nutrients per 100 g (Foundation / SR Legacy data are per 100 g)."""
    data = cached_get_json(FDC_SEARCH, {"query": food, "dataType": ["Foundation", "SR Legacy"], "pageSize": 1,
                                        "api_key": config.FDC_API_KEY}, ttl=config.TTL["fdc"])
    foods = data.get("foods") or []
    if not foods:
        return None
    f = foods[0]
    per100 = {}
    for n in f.get("foodNutrients", []):
        num = str(n.get("nutrientNumber", ""))
        if num in NUTRIENTS and n.get("value") is not None:
            per100[NUTRIENTS[num][0]] = float(n["value"])
    return {"fdc_id": f["fdcId"], "description": f.get("description"), "per_100g": per100,
            "source_id": f"FDC:{f['fdcId']}", "url": f"https://fdc.nal.usda.gov/food-details/{f['fdcId']}/nutrients"}


def log_meal(store, llm, user_id, day, description, meal_type=None):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day or ""):
        raise ValueError("day must be YYYY-MM-DD")
    parsed = parse_meal(llm, description)
    items, totals = [], {k: 0.0 for k in KEYS}
    for it in parsed["items"]:
        match = lookup(it["food"])
        row = {"food": it["food"], "grams": it["grams"], "matched": bool(match)}
        if match:
            nut = {k: round(v * it["grams"] / 100, 1) for k, v in match["per_100g"].items()}
            row.update({"usda_food": match["description"], "nutrients": nut,
                        "source_id": match["source_id"], "url": match["url"]})
            for k, v in nut.items():
                totals[k] += v
        items.append(row)
    totals["fluids_ml"] = parsed["fluids_ml"]
    totals = {k: round(v, 1) for k, v in totals.items()}
    meal_id = store.add_meal(user_id, day, meal_type, description, items, totals)
    return {"meal_id": meal_id, "items": items, "totals": totals,
            "unmatched": [i["food"] for i in items if not i["matched"]], "notice": NOTICE}


def day_summary(store, user_id, day_from, day_to=None):
    meals = store.list_meals(user_id, day_from, day_to)
    days = {}
    for m in meals:
        d = days.setdefault(m["day"], {k: 0.0 for k in KEYS})
        for k in KEYS:
            d[k] += m["totals"].get(k, 0) or 0
    targets = store.get_profile(user_id).get("nutrition_targets") or {}
    out = []
    for day, tot in sorted(days.items()):
        tot = {k: round(v, 1) for k, v in tot.items()}
        compare = {k: {"total": tot[k], "target": targets[k], "percent": round(100 * tot[k] / targets[k])}
                   for k in KEYS if targets.get(k)}
        out.append({"day": day, "totals": tot, "vs_targets": compare,
                    "meals": [{"meal_id": m["meal_id"], "meal_type": m["meal_type"], "description": m["description"],
                               "totals": m["totals"]} for m in meals if m["day"] == day]})
    return {"days": out, "targets_set": bool(targets), "notice": NOTICE,
            "targets_note": None if targets else "Add targets from your dietitian to compare against them."}
