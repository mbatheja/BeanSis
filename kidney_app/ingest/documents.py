"""
Load the team's documents into the reference DB as searchable chunks.

Input: a manifest CSV, one row per document:
    doc_id, collection, title, url, year, pub_type, tags, path, text
  - collection: research | coverage | resource
  - path: a .pdf or .txt file (relative to the manifest), OR put the content directly in `text`
    (handy for resources: one row per program with a short description)
Optional: a file of PubMed IDs (one per line) from your keyword search -> abstracts are fetched.

Usage:
    python -m ingest.documents data/raw/manifest.csv --pmids data/raw/pmids.txt
Rebuilds the `chunks` table from everything given, then rebuilds the BM25 index.
"""
import argparse
import csv
import re
from pathlib import Path

CHUNK_CHARS = 1200
OVERLAP = 200


def pdf_text(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return "\n\n".join((p.extract_text() or "") for p in pdf.pages)


def clean(text):
    text = re.sub(r"-\n(\w)", r"\1", text)          # re-join hyphenated line breaks
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunk(text, size=CHUNK_CHARS, overlap=OVERLAP):
    """Paragraph-aware chunks of ~size chars with overlap, so sentences aren't cut mid-thought."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:                         # very long paragraph: hard split
            head, p = p[:size], p[size - overlap:]
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(head)
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            cur = (cur[-overlap:] + "\n\n" + p) if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def rows_from_manifest(manifest):
    base = Path(manifest).parent
    out = []
    with open(manifest, newline="", encoding="utf-8") as f:
        for d in csv.DictReader(f):
            text = d.get("text") or ""
            path = (d.get("path") or "").strip()
            if path:
                p = base / path
                text = pdf_text(p) if p.suffix.lower() == ".pdf" else p.read_text(encoding="utf-8", errors="ignore")
            text = clean(text)
            if not text:
                print(f"skip {d.get('doc_id')}: no text")
                continue
            for i, c in enumerate(chunk(text)):
                out.append({
                    "chunk_id": f"{d['doc_id']}#{i}", "doc_id": d["doc_id"], "collection": d["collection"],
                    "title": d.get("title") or d["doc_id"], "url": d.get("url") or None,
                    "year": int(d["year"]) if (d.get("year") or "").isdigit() else None,
                    "pub_type": d.get("pub_type") or None, "tags": d.get("tags") or None,
                    # prepend title so BM25 matches title words in every chunk (cheap contextual retrieval)
                    "text": f"{d.get('title') or ''}\n{c}",
                })
    return out


def rows_from_pmids(pmid_file):
    from app.cache import cached_get_text
    from app.tools.external import EUTILS, _ncbi_params, parse_pubmed_xml
    ids = [l.strip() for l in open(pmid_file) if l.strip().isdigit()]
    out = []
    for i in range(0, len(ids), 150):
        xml = cached_get_text(f"{EUTILS}/efetch.fcgi",
                              _ncbi_params({"db": "pubmed", "id": ",".join(ids[i:i + 150]), "retmode": "xml"}),
                              ttl=30 * 86400)
        for a in parse_pubmed_xml(xml):
            if not a["abstract"]:
                continue
            out.append({"chunk_id": f"PMID{a['pmid']}#0", "doc_id": f"PMID{a['pmid']}", "collection": "research",
                        "title": a["title"], "url": f"https://pubmed.ncbi.nlm.nih.gov/{a['pmid']}/",
                        "year": int(a["year"]) if (a["year"] or "").isdigit() else None,
                        "pub_type": ", ".join(a["pub_types"][:2]), "tags": None,
                        "text": f"{a['title']}\n{a['abstract']}"})
    return out


def load(rows):
    import duckdb
    import pandas as pd
    from app import config
    config.REFERENCE_DB.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows).drop_duplicates("chunk_id")
    con = duckdb.connect(str(config.REFERENCE_DB))
    con.sql("INSTALL fts; LOAD fts;")
    con.register("df", df)
    con.sql("CREATE OR REPLACE TABLE chunks AS SELECT * FROM df")
    con.sql("PRAGMA create_fts_index('chunks', 'chunk_id', 'title', 'text', overwrite=1)")
    df.to_parquet(config.DATA_DIR / "chunks.parquet", index=False)
    n = con.sql("SELECT collection, count(*) FROM chunks GROUP BY 1").fetchall()
    con.close()
    print("chunks by collection:", n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="*", help="one or more manifest CSVs (all are loaded together)")
    ap.add_argument("--pmids")
    ap.add_argument("--literature", help="curated list with title verification (see ingest/literature.py)")
    a = ap.parse_args()
    rows = [r for m in a.manifest for r in rows_from_manifest(m)] + (rows_from_pmids(a.pmids) if a.pmids else [])
    if a.literature:
        from app import config
        from ingest.literature import rows_from_literature
        rows += rows_from_literature(a.literature, report_path=config.DATA_DIR / "literature_report.csv")
    load(rows)
