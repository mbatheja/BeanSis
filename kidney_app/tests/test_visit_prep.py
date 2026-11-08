"""Visit prep tests (offline: externals stubbed, fake model)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import appstate  # noqa: E402
from app.you import meds, visit_prep  # noqa: E402
from tests.test_you import stub_externals  # noqa: E402


class JsonLLM:
    def __init__(self, payload):
        self.payload, self.messages, self.prompts = payload, self, []

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return NS(content=[NS(type="text", text=text)])


def setup_user(life_stage="pregnant"):
    stub_externals()
    s = appstate.Store("sqlite:///:memory:")
    s.update_profile("u1", {"life_stage": life_stage, "ckd_stage": "3a", "transplant": True,
                            "timezone": "America/Los_Angeles"})
    m1 = meds.add(s, "u1", "Prograf", "1 mg capsule", ["08:00", "20:00"])["med_id"]
    meds.add(s, "u1", "Diflucan", "150 mg tablet", ["09:00"])
    s.log_dose("u1", m1, "2026-11-06T08:00", "taken")
    s.log_dose("u1", m1, "2026-11-06T20:00", "reminded")
    s.log_dose("u1", m1, "2026-11-05T20:00", "skipped")
    t = s.create_thread("u1", "symptoms")
    s.add_message(t, "user", "Why do my ankles swell at night?")
    mid = s.add_message(t, "assistant", "Swelling can come from fluid retention [1].",
                        meta={"answer_display": "Swelling can come from fluid retention [1].",
                              "citations": [{"n": 1, "source_id": "DOC:kdigo", "title": "KDIGO CKD guideline",
                                             "url": "u", "kind": "Research / guideline"}]})
    s.pin_message("u1", mid)
    return s


def test_facts_are_from_her_data_only():
    s = setup_user()
    ctx = visit_prep.gather(s, "u1", days=30)
    f = ctx["facts"]
    assert [m["dose_as_entered"] for m in f["medications"]] == ["1 mg capsule", "150 mg tablet"]
    assert f["doses_last_n_days"] == {"taken": 1, "skipped": 1, "reminded_not_marked": 1,
                                      "note": "Based only on doses marked in the app."}
    assert f["interaction_flags"][0]["drugs"] == ["tacrolimus", "fluconazole"]
    assert f["nutrition_last_n_days"] == {"days_logged": 0}
    assert ctx["recent_questions"] == ["Why do my ankles swell at night?"]
    assert {x["source_id"] for x in ctx["sources"]} == {"DOC:kdigo", "LABEL:tacrolimus-set"}


def test_standard_questions_depend_on_visit_and_life_stage():
    preg = visit_prep.standard_questions("obgyn", "pregnant")
    notp = visit_prep.standard_questions("obgyn", "not_pregnant")
    assert any("aspirin" in q for q in preg) and not any("aspirin" in q for q in notp)
    assert any("birth control" in q for q in notp)
    assert any("pregnancy" in q for q in visit_prep.standard_questions("nephrology", "planning_pregnancy"))
    assert not any("pregnancy" in q for q in visit_prep.standard_questions("nephrology", "not_pregnant"))


def test_generate_validates_and_guarantees_interaction_question():
    s = setup_user()
    llm = JsonLLM({"mention": ["My ankles swell at night.", "Take 2 mg extra if needed"],
                   "questions": [
                       {"question": "Why do my ankles swell at night, and should we check it?",
                        "why": "I asked about this recently.", "based_on": ["MY_QUESTION", "DOC:kdigo"]},
                       {"question": "Should I take low-dose aspirin for preeclampsia?", "why": "Standard.",
                        "based_on": ["STANDARD", "PMID:999"]},
                       {"question": "Should I take 5 mg of tacrolimus twice daily instead?", "why": "x",
                        "based_on": ["MY_MEDS"]}]})
    sheet = visit_prep.generate(s, llm, "u1", "obgyn", concerns="ankle swelling")
    qs = [q["question"] for q in sheet["questions"]]
    assert qs[0].startswith("The FDA label for tacrolimus")                 # added by code, first
    assert not any("5 mg" in q for q in qs)                                 # dosing question dropped
    assert "PMID:999" in sheet["validation"]["dropped"]                      # invented reference dropped
    assert sheet["mention"] == ["My ankles swell at night."]               # dosing statement dropped
    assert {c["source_id"] for c in sheet["citations"]} == {"LABEL:tacrolimus-set", "DOC:kdigo"}
    assert "ankle swelling" in llm.prompts[0] and "aspirin" in llm.prompts[0]
    md = sheet["markdown"]
    assert "**Prograf** (tacrolimus): 1 mg capsule, 08:00, 20:00" in md and "## My questions" in md
    assert "1 taken, 1 skipped, 1 reminders not marked" in md


def test_model_failure_falls_back_to_standard_list_and_rejects_bad_type():
    s = setup_user(life_stage="not_pregnant")
    sheet = visit_prep.generate(s, JsonLLM("sorry, something went wrong"), "u1", "pcp")
    assert sheet["validation"]["fell_back_to_standard"] is True
    assert any("vaccines" in q["question"] for q in sheet["questions"])
    try:
        visit_prep.generate(s, JsonLLM({}), "u1", "dentist")
        assert False
    except ValueError:
        pass


def test_save_to_board():
    s = setup_user()
    sheet = visit_prep.generate(s, JsonLLM({"questions": [], "mention": []}), "u1", "nephrology")
    pid = s.pin_snapshot("u1", "you", "Visit prep: Nephrology", {"kind": "visit_prep",
                                                                 "answer_display": sheet["markdown"]})
    pins = s.board("u1")["pinned_answers"]
    assert any(p["pin_id"] == pid and p["snapshot"]["kind"] == "visit_prep" for p in pins)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
