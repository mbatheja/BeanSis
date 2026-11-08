"""Patient-review tool tests (offline; synthetic rows shaped like the cleaned DrugRev table)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import grounding, presentation  # noqa: E402
from app.tools import reviews  # noqa: E402
from ingest.reviews import clean_text  # noqa: E402


def row(cond, text, rating=7, likes=0, date="2020-01-01", name="tacrolimus"):
    return {"medicine_name": name, "condition": cond, "review_date": date, "rating": rating, "likes": likes,
            "review": text}


ROWS = [
    row("Organ Transplant, Rejection Prophylaxis", "Hand tremor for the first months, it got better.", likes=9),
    row("Organ Transplant, Rejection Prophylaxis", "Bad shaking in my hands and some nausea.", likes=3),
    row("Organ Transplant, Rejection Prophylaxis", "I have tremor and my kidney transplant is doing well."),
    row("Organ Transplant, Rejection Prophylaxis", "Tremors, but I gave birth to a healthy baby on it.", likes=5),
    row("Organ Transplant, Rejection Prophylaxis", "Hand tremor for the first months, it got better.", name="prograf"),
    row("Atopic Dermatitis", "The ointment burned and itched at first, rash cleared later.", rating=4),
    row("Atopic Dermatitis", "Itching and burning on my face, a rash, then it worked."),
    row("Atopic Dermatitis", "Rash came back after stopping, itch was bad."),
]


def test_cleaning_strips_scrape_debris():
    raw = ("Cap took 20 minutes to open.'5-HTP User Reviews & Ratings (Page 2)For Anxiety11830-Jun-17Code9n"
           "Taken for less than 1 monthI have Opioid withdrawal")
    assert clean_text(raw) == "Cap took 20 minutes to open."
    assert clean_text('"Works well.\r\nNo issues."') == "Works well. No issues."


def test_summary_splits_by_condition_and_dedupes():
    s = reviews.summarize(ROWS)
    assert s["total_reviews_all_conditions"] == 7                       # brand/generic duplicate removed
    assert [(g["condition"], g["reviews"]) for g in s["by_condition"]] == [
        ("Organ Transplant, Rejection Prophylaxis", 4), ("Atopic Dermatitis", 3)]
    tx = reviews.summarize(ROWS, condition="transplant")
    themes = {t["theme"]: t["reviews_mentioning"] for t in tx["themes"]}
    assert themes == {"tremor or shaking": 4}                             # eczema rash/itch excluded by filter
    assert "kidney function concerns" not in themes                       # "my kidney transplant" isn't a side effect
    assert tx["mentions_pregnancy_or_breastfeeding"] == 1
    assert tx["excerpts"][0]["text"].startswith("Hand tremor")            # most-liked first
    assert reviews.CAVEAT == tx["caveat"]


def test_excerpts_are_short():
    long = row("Lupus", "word " * 200)
    s = reviews.summarize([long] * 1, condition="lupus")
    assert len(s["excerpts"][0]["text"]) <= reviews.EXCERPT_CHARS + 1


def test_tool_output_citation_and_caveats():
    reviews.drug_names = lambda d: (["prograf", "tacrolimus"], "tacrolimus")
    reviews.reviews_for = lambda names, con=None: ROWS
    out = reviews.patient_reviews({"drug": "Prograf", "condition": "transplant"}, {})
    assert out["sources"][0]["source_id"] == "REVIEWS:tacrolimus"
    assert "DrugRev" in out["sources"][0]["title"] and "10.17632" in out["sources"][0]["url"]
    _, rep = grounding.check("Some reviewers mentioned tremor [REVIEWS:tacrolimus].", out["sources"], "medications")
    assert rep["cited"] == ["REVIEWS:tacrolimus"]
    assert presentation.source_kind(out["sources"][0]).startswith("Patient reviews")
    shown = presentation.build("medications", "x [REVIEWS:tacrolimus]", rep, out["sources"],
                               [{"tool": "patient_reviews"}], grounding.CLINICAL_TABS)
    assert presentation.TOOL_NOTICES["patient_reviews"] in shown["notices"]
    reviews.reviews_for = lambda names, con=None: []
    assert reviews.patient_reviews({"drug": "unknowndrug"}, {})["sources"] == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
