"""You tab tests: medications, reminders, interaction check, nutrition. Offline (external calls stubbed)."""
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import appstate, grounding, presentation  # noqa: E402
from app.tools import local  # noqa: E402
from app.you import meds, nutrition  # noqa: E402

NORMALIZED = {"prograf": ("tacrolimus", "1"), "diflucan": ("fluconazole", "2"), "zestril": ("lisinopril", "3")}
LABELS = {
    "tacrolimus": {"drug_interactions": "Tacrolimus is metabolized by CYP3A. Azole Antifungal agents such as "
                                        "fluconazole increase tacrolimus concentrations; monitor levels."},
    "fluconazole": {"drug_interactions": "Fluconazole may increase concentrations of several drugs."},
    "lisinopril": {"drug_interactions": "Diuretics may cause excessive blood pressure reduction."},
}


def stub_externals():
    def normalize(name):
        ing, rx = NORMALIZED.get(name.lower(), (None, None))
        return {"results": {"input": name, "rxcui": rx, "ingredients": [ing] if ing else []}, "sources": []}

    def label(ing, sections=None, max_chars=0):
        return {"results": {"drug": ing, "found": True, "sections": LABELS.get(ing, {})},
                "sources": [{"source_id": f"LABEL:{ing}-set", "title": f"FDA label: {ing}", "url": "u"}]}

    meds.external.normalize_drug = normalize
    meds.external.drug_label = label
    meds._classes = lambda rxcui: {"2": ["Azole Antifungal [EPC]"]}.get(rxcui, [])
    meds.faers_co_reported = lambda a, b: 42


def store_with_meds():
    s = appstate.Store("sqlite:///:memory:")
    s.update_profile("u1", {"timezone": "America/Los_Angeles"})
    meds.add(s, "u1", "Prograf", "1 mg capsule", ["20:00", "08:00"])
    meds.add(s, "u1", "Zestril", "10 mg tablet", ["08:00"], days=[0, 2, 4], reminders=False)
    return s


def test_add_meds_normalizes_and_validates():
    stub_externals()
    s = store_with_meds()
    m = s.list_meds("u1")
    assert [x["ingredient"] for x in m] == ["tacrolimus", "lisinopril"] and m[0]["times"] == ["08:00", "20:00"]
    assert m[0]["dose_text"] == "1 mg capsule"            # stored exactly as entered
    unknown = meds.add(s, "u1", "my vitamin gummy", "1 gummy", ["09:00"])
    assert unknown["recognized"] is False
    try:
        meds.add(s, "u1", "Prograf", "1 mg", ["8am"])
        assert False, "bad time accepted"
    except ValueError:
        pass


def test_reminders_once_per_slot_generic_text_and_schedule():
    stub_externals()
    s = store_with_meds()
    tz = ZoneInfo("America/Los_Angeles")
    monday_8 = datetime(2026, 11, 9, 8, 3, tzinfo=tz)       # a Monday
    due = meds.due_reminders(s, monday_8.astimezone(ZoneInfo("UTC")))
    assert len(due) == 1 and due[0]["time"] == "08:00" and len(due[0]["med_ids"]) == 1   # Zestril has reminders off
    assert due[0]["message"] == "Time for your 8:00 AM medications. Open the app to see which ones."
    assert "Prograf" not in due[0]["message"]             # no drug names on lock screens
    assert meds.due_reminders(s, monday_8.astimezone(ZoneInfo("UTC"))) == []          # not sent twice
    assert meds.due_reminders(s, datetime(2026, 11, 9, 8, 30, tzinfo=tz)) == []       # outside window

    sched = meds.todays_schedule(s, "u1", now=datetime(2026, 11, 9, 9, 0, tzinfo=tz))
    assert [(d["time"], d["name"], d["status"]) for d in sched] == [
        ("08:00", "Prograf", "due"), ("08:00", "Zestril", "due"), ("20:00", "Prograf", "upcoming")]
    s.log_dose("u1", sched[0]["med_id"], sched[0]["scheduled_for"], "taken")
    assert meds.todays_schedule(s, "u1", now=datetime(2026, 11, 9, 9, 0, tzinfo=tz))[0]["status"] == "taken"
    tuesday = meds.todays_schedule(s, "u1", now=datetime(2026, 11, 10, 9, 0, tzinfo=tz))
    assert "Zestril" not in [d["name"] for d in tuesday]  # Mon/Wed/Fri only


def test_interaction_check_label_flags_with_citations():
    stub_externals()
    s = store_with_meds()
    res = meds.check_interactions(s, "u1", extra="Diflucan")
    pair = next(p for p in res["pairs"] if set(p["drugs"]) == {"tacrolimus", "fluconazole"})
    assert pair["flagged"] and pair["faers_reported_together_women"] == 42
    flag = pair["label_flags"][0]
    assert flag["label_of"] == "tacrolimus" and "fluconazole" in flag["quote"]
    assert flag["source_id"] == "LABEL:tacrolimus-set"
    other = next(p for p in res["pairs"] if set(p["drugs"]) == {"tacrolimus", "lisinopril"})
    assert not other["flagged"]
    assert meds.ALWAYS_NOTICE in res["notices"]             # "no flag" is never presented as "safe"

    out = local.check_my_interactions({"drug": "Diflucan"}, {"store": s, "user_id": "u1"})
    assert {x["source_id"] for x in out["sources"]} == {"LABEL:tacrolimus-set"}


class ParseLLM:
    def __init__(self, payload):
        self.payload, self.messages = payload, self

    def create(self, **kw):
        return NS(content=[NS(type="text", text=json.dumps(self.payload))])


def test_nutrition_log_and_summary():
    per100 = {"banana, raw": {"potassium_mg": 358, "sodium_mg": 1, "phosphorus_mg": 22, "protein_g": 1.1},
              "white rice, cooked": {"potassium_mg": 35, "sodium_mg": 1, "phosphorus_mg": 43, "protein_g": 2.7}}
    nutrition.lookup = lambda food: ({"fdc_id": 1, "description": food, "per_100g": per100[food],
                                      "source_id": "FDC:1", "url": "u"} if food in per100 else None)
    s = appstate.Store("sqlite:///:memory:")
    llm = ParseLLM({"items": [{"food": "banana, raw", "grams": 120}, {"food": "white rice, cooked", "grams": 200},
                              {"food": "mystery sauce", "grams": 30}], "fluids_ml": 250})
    out = nutrition.log_meal(s, llm, "u1", "2026-11-07", "banana, a cup of rice, sauce, glass of water", "lunch")
    assert out["totals"]["potassium_mg"] == round(358 * 1.2 + 35 * 2, 1) and out["totals"]["fluids_ml"] == 250
    assert out["unmatched"] == ["mystery sauce"]
    summ = nutrition.day_summary(s, "u1", "2026-11-07")
    assert summ["targets_set"] is False and summ["days"][0]["vs_targets"] == {}   # never invents targets
    s.update_profile("u1", {"nutrition_targets": {"potassium_mg": 2000}})
    vs = nutrition.day_summary(s, "u1", "2026-11-07")["days"][0]["vs_targets"]
    assert vs == {"potassium_mg": {"total": 499.6, "target": 2000, "percent": 25}}
    assert "calorie" not in json.dumps(summ).lower()


def test_usda_citations_and_deletion():
    assert grounding.check("Bananas are high in potassium [FDC:1].",
                           [{"source_id": "FDC:1", "title": "Banana", "url": "u"}], "symptoms")[1]["cited"] == ["FDC:1"]
    assert presentation.source_kind({"source_id": "FDC:1"}) == "USDA FoodData Central"
    stub_externals()
    s = store_with_meds()
    s.add_meal("u1", "2026-11-07", None, "toast", [], {})
    s.delete_user_data("u1")
    assert s.list_meds("u1", active_only=False) == [] and s.list_meals("u1", "2026-11-07") == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
