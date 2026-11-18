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
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

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


def _row_from_record(rec, topic=None):
    """Convert one verified PubMed record into a searchable, provenance-rich abstract row."""
    pmid = rec["pmid"]
    authors = "; ".join(rec.get("authors", []))
    author_keywords = "; ".join(rec.get("author_keywords", []))
    mesh_terms = "; ".join(rec.get("mesh_terms", []))
    metadata = "\n".join(x for x in (f"Topics: {topic}" if topic else "", f"Authors: {authors}" if authors else "",
                                      f"Author keywords: {author_keywords}" if author_keywords else "",
                                      f"MeSH terms: {mesh_terms}" if mesh_terms else "") if x)
    text = f"{rec['title']}\n{metadata}\n{rec['abstract']}" if metadata else f"{rec['title']}\n{rec['abstract']}"
    return {"chunk_id": f"PMID{pmid}#0", "doc_id": f"PMID{pmid}", "collection": "research",
            "title": rec["title"], "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "year": int(rec["year"]) if (rec["year"] or "").isdigit() else None,
            "pub_type": ", ".join(rec["pub_types"][:2]), "tags": topic,
            "text": text, "source_uri": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "source_file": None,
            "source_hash": hashlib.sha256(text.encode()).hexdigest(), "ingest_version": "documents-v3",
            "ingested_at": datetime.now(timezone.utc).isoformat(), "page_number": None, "chunk_index": 0,
            "pmid": pmid, "doi": rec.get("doi"), "authors": authors or None,
            "author_keywords": author_keywords or None, "mesh_terms": mesh_terms or None,
            "abstract": rec["abstract"] or None, "full_text_status": "abstract_only", "source_type": "pubmed",
            "topics": topic}


def rows_from_discovery(path):
    """Fetch recent PubMed candidates from reviewed topic queries in YAML."""
    import yaml
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    topics = config.get("topics", [])
    reldate = int(config.get("reldate_days", 730))
    retmax = int(config.get("retmax_per_topic", 25))
    topic_pmids = {}
    for topic in topics:
        name, query = topic["name"], topic["query"]
        try:
            result = cached_get_json(f"{EUTILS}/esearch.fcgi", _ncbi_params({
                "db": "pubmed", "term": query, "retmax": retmax, "sort": "pub_date",
                "datetype": "pdat", "reldate": reldate, "retmode": "json"}), ttl=6 * 3600
            ).get("esearchresult", {})
        except Exception as exc:
            print(f"PubMed discovery failed for topic {name}: {exc}")
            continue
        for pmid in result.get("idlist", []):
            topic_pmids.setdefault(pmid, set()).add(name)

    try:
        records = fetch(list(topic_pmids))
    except Exception as exc:
        print(f"PubMed metadata fetch failed; continuing with curated/PDF sources: {exc}")
        records = {}
    rows = []
    catalog = []
    for pmid, rec in records.items():
        tags = "; ".join(sorted(topic_pmids[pmid]))
        if not rec.get("abstract"):
            continue
        rows.append(_row_from_record(rec, tags))
        catalog.append({"pmid": pmid, "doi": rec.get("doi") or "", "title": rec["title"],
                        "year": rec.get("year") or "", "topic": tags,
                        "pubmed_mesh_terms": "; ".join(rec.get("mesh_terms", [])),
                        "pubmed_author_keywords": "; ".join(rec.get("author_keywords", [])),
                        "pubmed_authors": "; ".join(rec.get("authors", [])),
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"})
    target = Path(__file__).resolve().parents[1] / "data" / "literature_discovered.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = ("pmid", "doi", "title", "year", "topic", "pubmed_mesh_terms",
              "pubmed_author_keywords", "pubmed_authors", "url")
    with target.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(catalog)
    print(f"PubMed discovery: {len(rows)} recent articles across {len(topics)} topics")
    return rows


def rows_from_literature(path, report_path=None):
    entries = list(csv.DictReader(open(path, encoding="utf-8")))
    for e in entries:
        e["pmid"] = resolve(e["id_type"], e["id"])
    records = fetch([e["pmid"] for e in entries if e["pmid"]])
    rows, report = [], []
    for e in entries:
        rec = records.get(e["pmid"]) if e["pmid"] else None
        e.setdefault("pubmed_mesh_terms", "")
        e.setdefault("pubmed_author_keywords", "")
        e.setdefault("pubmed_authors", "")
        score = title_match(e["expected_title"], rec["title"]) if rec else 0.0
        if rec and score >= MATCH_THRESHOLD:
            # Keep PubMed's controlled vocabulary separate from author supplied keywords.
            e["pubmed_mesh_terms"] = "; ".join(rec.get("mesh_terms", []))
            e["pubmed_author_keywords"] = "; ".join(rec.get("author_keywords", []))
            e["pubmed_authors"] = "; ".join(rec.get("authors", []))
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
        rows.append(_row_from_record(rec, e.get("topic")))
    # Keep fetched metadata in a generated catalog so ingestion never rewrites the curated seed file.
    source_fields = list(entries[0]) if entries else []
    for col in ("pubmed_mesh_terms", "pubmed_author_keywords", "pubmed_authors"):
        if col not in source_fields:
            source_fields.append(col)
    if entries:
        enriched_path = Path(__file__).resolve().parents[1] / "data" / "literature_enriched.csv"
        enriched_path.parent.mkdir(parents=True, exist_ok=True)
        with open(enriched_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=source_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(entries)
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
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
