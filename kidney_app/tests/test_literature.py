"""Literature loader tests: PubMed stubbed; checks resolution paths and the title verification."""
import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest import literature  # noqa: E402

PUBMED = {
    "21940842": ("Pregnancy outcomes in women with chronic kidney disease: a systematic review.", "Adverse outcomes..."),
    "40439632": ("Women and kidney health: conclusions from a Kidney Disease: Improving Global Outcomes (KDIGO) "
                 "Controversies Conference.", "Participants emphasized..."),
    "99999999": ("Effects of coffee on sleep in adolescents.", "Unrelated abstract."),   # a typo'd PMID's real paper
}


def xml_for(ids):
    arts = "".join(
        f"<PubmedArticle><MedlineCitation><PMID>{i}</PMID><Article><Journal><JournalIssue><PubDate><Year>2020"
        f"</Year></PubDate></JournalIssue></Journal><ArticleTitle>{PUBMED[i][0]}</ArticleTitle><Abstract>"
        f"<AbstractText>{PUBMED[i][1]}</AbstractText></Abstract><PublicationTypeList><PublicationType>Review"
        f"</PublicationType></PublicationTypeList></Article></MedlineCitation></PubmedArticle>"
        for i in ids if i in PUBMED)
    return f"<PubmedArticleSet>{arts}</PubmedArticleSet>"


def stub():
    def get_json(url, params=None, ttl=0):
        term = params["term"]
        ids = ["40439632"] if "kint.2025.02.021" in term else []
        return {"esearchresult": {"idlist": ids}}
    literature.cached_get_json = get_json
    literature.cached_get_text = lambda url, params=None, ttl=0: xml_for(params["id"].split(","))


def test_verification_loads_matches_and_skips_mismatches():
    stub()
    with tempfile.TemporaryDirectory() as d:
        src, rep = Path(d) / "lit.csv", Path(d) / "report.csv"
        src.write_text(
            "id_type,id,expected_title,topic\n"
            "pmid,21940842,Pregnancy Outcomes in Women with Chronic Kidney Disease: A Systematic Review,pregnancy\n"
            "doi,10.1016/j.kint.2025.02.021,Women and kidney health: conclusions from a KDIGO Controversies "
            "Conference,overview\n"
            "pmid,99999999,Sex and Cardiovascular Outcomes with Chronic Kidney Disease,cardiometabolic\n"
            "title,Kidney disease and reproductive health,Kidney disease and reproductive health,reproductive\n")
        rows = literature.rows_from_literature(src, report_path=rep)
        status = {r["id"]: r["status"] for r in csv.DictReader(open(rep))}
    assert [r["doc_id"] for r in rows] == ["PMID21940842", "PMID40439632"]
    assert status == {"21940842": "loaded", "10.1016/j.kint.2025.02.021": "loaded",
                      "99999999": "TITLE_MISMATCH", "Kidney disease and reproductive health": "not_found"}
    assert rows[0]["tags"] == "pregnancy" and rows[0]["collection"] == "research"
    assert rows[1]["url"] == "https://pubmed.ncbi.nlm.nih.gov/40439632/"


def test_title_match_tolerates_formatting():
    assert literature.title_match("Chronic Kidney Disease Gender and Access to Care: A Global Perspective",
                                  "Chronic Kidney Disease, Gender, and Access to Care: A Global Perspective.") == 1.0
    assert literature.title_match("Sex and Cardiovascular Outcomes with CKD", "Coffee and sleep") == 0.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
