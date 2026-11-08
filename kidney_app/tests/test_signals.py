"""drug_signal_profile tests: synthetic sources, so every classification is checked exactly."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import grounding, presentation  # noqa: E402
from app.tools import external, reviews, signals  # noqa: E402


def row(cond, text, name="tacrolimus"):
    return {"medicine_name": name, "condition": cond, "review_date": "2021-01-01", "rating": 7, "likes": 0,
            "review": text}


TX = "Organ Transplant, Rejection Prophylaxis"
REVIEWS = [
    row(TX, "Hand tremor the first months."), row(TX, "Tremor and headaches."), row(TX, "Shaky hands, mild tremor."),
    row(TX, "My hair is falling out, hair loss is upsetting."), row(TX, "Hair thinning and hair loss since starting."),
    row(TX, "Tremor, and hair loss too."),
    row(TX, "Tremor and hair loss too.", name="prograf"),              # brand/generic duplicate
    row("Atopic Dermatitis", "Burning, rash and itching on my face."),  # other use: filtered out
    row("Atopic Dermatitis", "Rash and itch at first."),
]
LABEL_TEXT = ("Adverse reactions in kidney transplant patients included tremor, headache, diarrhea, nausea and "
              "hyperglycemia. Nephrotoxicity has been reported. Monitor blood glucose.")
FAERS = [{"reaction": "TREMOR", "reports": 900}, {"reaction": "ALOPECIA", "reports": 300},
         {"reaction": "DIARRHOEA", "reports": 500}, {"reaction": "RASH", "reports": 5}]


def stub(label_text=LABEL_TEXT, faers=FAERS, pubmed_hits=12):
    calls = {"pubmed": []}
    external.drug_label = lambda ing, sections=None, max_chars=0: {
        "results": {"sections": {"adverse_reactions": label_text} if label_text else {}},
        "sources": [{"source_id": "LABEL:tac", "title": "FDA label: tacrolimus", "url": "u"}] if label_text else []}
    external.faers_summary = lambda g, female_only=True, consumer_reports_only=False, top_n=15: {
        "results": {"total_reports": 20000, "top_reactions": faers},
        "sources": [{"source_id": "FAERS:tacrolimus:female", "title": "FAERS", "url": "u"}]}

    def fake_get(url, params=None, ttl=0):
        calls["pubmed"].append(params["term"])
        return {"esearchresult": {"count": str(pubmed_hits)}}
    signals.cached_get_json = fake_get
    external.pubmed_search = lambda q, max_results=3, min_year=None: {
        "results": [{"pmid": "111", "title": "Alopecia in transplant recipients on tacrolimus", "year": "2019"}],
        "sources": [{"source_id": "PMID:111", "title": "Alopecia in transplant recipients", "url": "u"}]}
    return calls


def test_classification_across_sources():
    calls = stub()
    prof, sources = signals.build_profile("tacrolimus", REVIEWS, condition="transplant")
    by = {s["theme"]: s for s in prof["signals"]}
    assert prof["sample"]["reviews_in_group"] == 6                         # deduped, eczema reviews excluded
    t = by["tremor or shaking"]
    assert (t["status"], t["reviews_mentioning"], t["faers_reports_women"], t["on_label"]) == \
           ("on_label_and_reported", 4, 900, True)
    assert "tremor" in t["label_sentence"].lower()
    h = by["hair loss"]
    assert (h["status"], h["reviews_mentioning"], h["faers_reports_women"]) == ("reported_not_on_label", 3, 300)
    assert by["diarrhea"]["status"] == "on_label_and_reported"            # FAERS + label, no reviews
    assert by["kidney function concerns"]["status"] == "label_only"
    assert "rash or itching" not in by                                     # 5 FAERS reports, 0 reviews: too weak
    assert prof["summary"]["reported_not_on_label"] == ["hair loss"]
    assert [s["status"] for s in prof["signals"]][:2] == ["on_label_and_reported", "on_label_and_reported"]


def test_research_only_for_unlabeled_signals():
    calls = stub()
    prof, sources = signals.build_profile("tacrolimus", REVIEWS, condition="transplant")
    assert len(calls["pubmed"]) == 1 and "alopecia" in calls["pubmed"][0]  # one targeted query, the gap only
    r = prof["research_on_unlabeled"][0]
    assert r["theme"] == "hair loss" and r["pubmed_hits"] == 12 and r["top_papers"][0]["cite_as"] == "PMID:111"
    assert {s["source_id"] for s in sources} == {"LABEL:tac", "FAERS:tacrolimus:female", "PMID:111"}
    calls = stub()
    signals.build_profile("tacrolimus", REVIEWS, condition="transplant", research=False)
    assert calls["pubmed"] == []


def test_no_label_means_no_gap_claims():
    stub(label_text=None)
    prof, _ = signals.build_profile("tacrolimus", REVIEWS, condition="transplant")
    assert prof["sample"]["label_found"] is False
    assert prof["summary"]["reported_not_on_label"] == []                  # can't call it "not on label" without one


def test_tool_wrapper_citations_and_notice():
    stub()
    reviews.drug_names = lambda d: (["prograf", "tacrolimus"], "tacrolimus")
    reviews.reviews_for = lambda names, con=None: REVIEWS
    out = signals.drug_signal_profile({"drug": "Prograf", "condition": "transplant"}, {})
    ids = {s["source_id"] for s in out["sources"]}
    assert {"LABEL:tac", "FAERS:tacrolimus:female", "PMID:111", "REVIEWS:tacrolimus"} <= ids
    ans = ("Tremor is on the label and commonly reported [LABEL:tac][REVIEWS:tacrolimus]. Some reviewers mention hair "
           "loss, which the label doesn't list; a few studies discuss it [PMID:111].")
    _, rep = grounding.check(ans, out["sources"], "medications")
    assert rep["removed"] == [] and set(rep["cited"]) == {"LABEL:tac", "REVIEWS:tacrolimus", "PMID:111"}
    shown = presentation.build("medications", ans, rep, out["sources"], [{"tool": "drug_signal_profile"}],
                               grounding.CLINICAL_TABS)
    assert presentation.TOOL_NOTICES["drug_signal_profile"] in shown["notices"]


def test_missing_reviews_table_falls_back_to_label_and_faers():
    stub()
    reviews.drug_names = lambda d: (["prograf", "tacrolimus"], "tacrolimus")

    def no_table(names, con=None):
        raise RuntimeError("IO Error: Cannot open file data/reference.duckdb")
    reviews.reviews_for = no_table
    out = signals.drug_signal_profile({"drug": "Prograf"}, {})
    prof = out["results"]
    assert prof["sample"]["reviews_in_group"] == 0 and prof["sample"]["label_found"] is True
    assert {s["theme"] for s in prof["signals"]} >= {"tremor or shaking", "diarrhea"}   # label + FAERS still compared
    assert not any(x["source_id"].startswith("REVIEWS:") for x in out["sources"])      # no review citation invented


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
