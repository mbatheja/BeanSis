"""
Load the team's curated literature list (data/raw/literature.csv) into the research collection.

Each row: id_type (pmid | doi | title), id, expected_title, topic.
  1. resolve DOI or title -> PMID with PubMed esearch
  2. fetch the PubMed record (title, abstract, year, publication type)
  3. VERIFY: the fetched title must match expected_title (word overlap >= 0.6). A typo'd PMID would
     otherwise load a different paper's abstract under your title, and every citation of it would be wrong.
     Mismatches and unresolved rows are skipped and listed in data/literature_report.csv.
Only the PubMed abstract is indexed as evidence. Personal notes about a paper are not, since the agents
cite indexed text as if the paper said it. For full text, add open-access PDFs via the manifest.

Use through the documents loader so everything lands in one index:
    python -m ingest.documents data/raw/manifest.csv --literature data/raw/literature.csv
"""
import csv
import re

from app.cache import cached_get_json, cached_get_text
from app.tools.external import EUTILS, _ncbi_params, parse_pubmed_xml

MATCH_THRESHOLD = 0.6
STOP = set("a an and the of in on for with to from by at as is are its their".split())


def _words(t):
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if w not in STOP}


def title_match(expected, actual):
    a, b = _words(expected), _words(actual)
    return len(a & b) / len(a | b) if a and b else 0.0


def resolve(id_type, ident):
    """Return a PMID for a pmid/doi/title, or None."""
    if id_type == "pmid":
        return ident.strip() if ident.strip().isdigit() else None
    term = f"{ident.strip()}[doi]" if id_type == "doi" else f'"{ident.strip()}"[title]'
    ids = cached_get_json(f"{EUTILS}/esearch.fcgi", _ncbi_params(
        {"db": "pubmed", "term": term, "retmax": 3, "retmode": "json"}), ttl=30 * 86400
    ).get("esearchresult", {}).get("idlist", [])
    return ids[0] if ids else None


def fetch(pmids):
    if not pmids:
        return {}
    xml = cached_get_text(f"{EUTILS}/efetch.fcgi", _ncbi_params(
        {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}), ttl=30 * 86400)
    return {a["pmid"]: a for a in parse_pubmed_xml(xml)}


def rows_from_literature(path, report_path=None):
    entries = list(csv.DictReader(open(path, encoding="utf-8")))
    for e in entries:
        e["pmid"] = resolve(e["id_type"], e["id"])
    records = fetch([e["pmid"] for e in entries if e["pmid"]])
    rows, report = [], []
    for e in entries:
        rec = records.get(e["pmid"]) if e["pmid"] else None
        score = title_match(e["expected_title"], rec["title"]) if rec else 0.0
        if not e["pmid"]:
            status = "not_found"
        elif not rec:
            status = "pmid_not_in_pubmed"
        elif score < MATCH_THRESHOLD:
            status = "TITLE_MISMATCH"
        elif not rec["abstract"]:
            status = "no_abstract"
        else:
            status = "loaded"
        report.append({"id_type": e["id_type"], "id": e["id"], "pmid": e["pmid"] or "", "status": status,
                       "title_match": round(score, 2), "expected_title": e["expected_title"],
                       "pubmed_title": rec["title"] if rec else ""})
        if status != "loaded":
            continue
        rows.append({"chunk_id": f"PMID{rec['pmid']}#0", "doc_id": f"PMID{rec['pmid']}", "collection": "research",
                     "title": rec["title"], "url": f"https://pubmed.ncbi.nlm.nih.gov/{rec['pmid']}/",
                     "year": int(rec["year"]) if (rec["year"] or "").isdigit() else None,
                     "pub_type": ", ".join(rec["pub_types"][:2]), "tags": e.get("topic"),
                     "text": f"{rec['title']}\n{rec['abstract']}"})
    if report_path:
        with open(report_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(report[0]))
            w.writeheader()
            w.writerows(report)
    loaded = sum(r["status"] == "loaded" for r in report)
    print(f"literature: {loaded}/{len(report)} loaded")
    for r in report:
        if r["status"] != "loaded":
            print(f"  {r['status']:<20} {r['id_type']}:{r['id']}  expected: {r['expected_title'][:60]}"
                  + (f"\n  {'':<20} pubmed:   {r['pubmed_title'][:60]}" if r["pubmed_title"] else ""))
    return rows
