"""Ride-finder flow tests: ZIP/county/plan data stubbed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import grounding  # noqa: E402
from app.tools import transport as T  # noqa: E402

ZIPS = {"90210": ["Los Angeles"], "91311": ["Los Angeles", "Ventura"]}
PLANS = {"Los Angeles": [
    {"county": "Los Angeles", "plan_name": "Plan A", "member_services_phone": "(800) 555-0100",
     "ride_line_phone": "(800) 555-0199", "tty": "711", "url": "", "last_verified": "2026-11-01", "source": "DHCS"},
    {"county": "Los Angeles", "plan_name": "Plan B", "member_services_phone": "(800) 555-0123",
     "ride_line_phone": "", "tty": "", "url": "", "last_verified": "", "source": ""}]}
T.counties_for_zip = lambda z: ZIPS.get(z, [])
T.plans_for_county = lambda c: PLANS.get(c, [])
FULL = {"zip": "90210", "coverage": "medi_cal", "plan": "Plan A", "destination": "dialysis", "mobility": "car"}


def walk(answers, profile=None):
    return T.next_step(answers, profile)


def test_question_order_and_county_only_when_needed():
    assert walk({})["ask"]["field"] == "zip"
    assert walk({"zip": "99999"})["ask"]["field"] == "zip"                     # not a CA ZIP
    assert walk({"zip": "90210"})["ask"]["field"] == "coverage"                # one county: no county question
    q = walk({"zip": "91311"})["ask"]
    assert q["field"] == "county" and [o["value"] for o in q["options"]] == ["Los Angeles", "Ventura"]
    q = walk({"zip": "90210", "coverage": "medi_cal"})["ask"]
    assert q["field"] == "plan" and "ID card" in q["question"]
    assert [o["value"] for o in q["options"]] == ["Plan A", "Plan B", "fee_for_service", "unknown"]
    assert walk({**FULL, "mobility": None})["ask"]["field"] == "mobility"


def test_verified_plan_gets_call_button_and_dialysis_script():
    c = walk(FULL, {"life_stage": "pregnant"})["card"]
    assert c["mode"] == "NMT" and c["contact"]["tel"] == "tel:+18005550199"   # ride line preferred
    assert "ride line" in c["contact"]["label"] and c["contact"]["last_verified"] == "2026-11-01"
    assert any("no other way" in r for r in c["requirements"])
    assert any("standing rides" in r for r in c["requirements"])
    assert "every session" in c["call_script"] and "dialysis" in c["call_script"]
    assert any("pregnant" in n for n in c["notes"])


def test_unverified_plan_never_shows_a_number():
    c = walk({**FULL, "plan": "Plan B"})["card"]
    assert c["contact"]["phone"] is None and c["contact"]["tel"] is None
    assert any("haven't verified" in n for n in c["notes"])


def test_nemt_requires_prescription_and_ffs_uses_dhcs_form():
    c = walk({**FULL, "mobility": "wheelchair", "plan": "fee_for_service", "destination": "prenatal"})["card"]
    assert c["mode"] == "NEMT" and any("prescription" in r for r in c["requirements"])
    assert c["contact"]["email"] == "DHCSNMT@dhcs.ca.gov" and "wheelchair van" in c["call_script"]
    c2 = walk({**FULL, "plan": "unknown"})["card"]
    assert "directory" in c2["contact"]["url"] and c2["contact"].get("tel") is None


def test_medicare_only_and_no_coverage_paths():
    m = walk({"zip": "90210", "coverage": "medicare", "destination": "dialysis", "mobility": "car"})["card"]
    assert m["mode"] is None and any("Medicare Advantage" in n for n in m["notes"]) and m["call_script"] is None
    n = walk({"zip": "90210", "coverage": "neither", "destination": "prenatal", "mobility": "car"},
             {"life_stage": "pregnant"})["card"]
    assert any("PE4PP" in x for x in n["notes"])
    assert {f["name"] for f in n["fallbacks"]} == {"ADA paratransit", "American Kidney Fund", "211"}
    both = walk({**FULL, "coverage": "both"})["card"]
    assert any("through Medi-Cal" in x for x in both["notes"])


def test_chat_tool_uses_profile_zip_and_cites_dhcs():
    out = T.ride_help({"coverage": "medi_cal", "plan": "Plan A", "destination": "dialysis", "mobility": "car"},
                      {"profile": {"zip": "90210"}})
    assert out["results"]["done"] and {s["source_id"] for s in out["sources"]} >= {"DOC:medi-cal-transport"}
    _, rep = grounding.check("Medi-Cal covers rides to dialysis [DOC:medi-cal-transport].", out["sources"],
                             "resources")
    assert rep["cited"] == ["DOC:medi-cal-transport"]
    assert T.ride_help({}, {"profile": {}})["results"]["ask"]["field"] == "zip"


def test_orange_county_real_plan_file():
    """Uses data/raw/ca_medi_cal_plans.csv as committed (Orange County rows from the DHCS directory)."""
    import importlib
    import app.tools.transport as fresh
    importlib.reload(fresh)                                    # undo this module's stubs; read the real CSV
    fresh.counties_for_zip = lambda z: ["Orange"]
    q = fresh.next_step({"zip": "92701", "coverage": "medi_cal"})["ask"]
    assert [o["value"] for o in q["options"]][:2] == ["CalOptima Health", "Kaiser Permanente"]
    base = {"zip": "92701", "coverage": "medi_cal", "destination": "dialysis", "mobility": "car"}
    cal = fresh.next_step({**base, "plan": "CalOptima Health"})["card"]["contact"]
    assert cal["tel"] == "tel:+18885878088" and cal["tty"] == "711" and cal["last_verified"] == "2026-11-07"
    kp = fresh.next_step({**base, "plan": "Kaiser Permanente"})["card"]["contact"]
    assert [(o["label"], o["tel"]) for o in kp["other_phones"]] == [
        ("Spanish", "tel:+18007880616"), ("Chinese dialects", "tel:+18007577585")]
    oc = fresh.next_step({**base, "plan": "Orange County (OC) PACE"})["card"]
    assert oc["contact"]["tel"] is None and "directory" in oc["contact"]["url"]
    assert any("main phone number" in n for n in oc["notes"])


def test_los_angeles_real_plan_file():
    import importlib
    import app.tools.transport as fresh
    importlib.reload(fresh)
    fresh.counties_for_zip = lambda z: ["Los Angeles"]
    opts = fresh.next_step({"zip": "90024", "coverage": "medi_cal"})["ask"]["options"]
    labels = {o["value"]: o["label"] for o in opts}
    assert [o["value"] for o in opts][:3] == ["Health Net Community Solutions Inc.", "Kaiser Permanente",
                                              "L.A. Care Health Plan"]            # main plans first
    assert labels["SCAN Health Plan"].endswith("(age 65+ with Medicare and Medi-Cal)")
    assert labels["High Desert PACE"].endswith("(age 55+)") and labels["Seen Health"].endswith("(specialty plan)")
    base = {"zip": "90024", "coverage": "medi_cal", "destination": "prenatal", "mobility": "car"}
    lac = fresh.next_step({**base, "plan": "L.A. Care Health Plan"})["card"]["contact"]
    assert lac["tel"] == "tel:+18888399909" and lac["tty"] == "1-866-522-2731"
    garbled = fresh.next_step({**base, "plan": "myPlace Greater LA PACE"})["card"]
    assert garbled["contact"]["tel"] is None and any("main phone number" in n for n in garbled["notes"])
    unsure = fresh.next_step({**base, "plan": "unknown"})["card"]["contact"]
    assert unsure["alternate"]["tel"] == "tel:+18004304263" and unsure["alternate"]["tty"] == "1-800-430-7077"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
