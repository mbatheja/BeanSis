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
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

CHUNK_CHARS = 1200
OVERLAP = 200
INGESTION_VERSION = "documents-v3"


def pdf_text(path):
    return "\n\n".join(text for _, text in pdf_pages(path))


def pdf_pages(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return [(i + 1, p.extract_text() or "") for i, p in enumerate(pdf.pages)]


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
    manifest_path = Path(manifest).resolve()
    out = []
    with open(manifest, newline="", encoding="utf-8") as f:
        for d in csv.DictReader(f):
            text = d.get("text") or ""
            path = (d.get("path") or "").strip()
            source_file = path.replace("\\", "/") if path else manifest_path.name
            source_uri = d.get("url") or None
            page_texts = None
            if path:
                p = base / path
                if not p.exists():
                    missing_path = str(p).encode("ascii", "backslashreplace").decode("ascii")
                    print(f"skip {d.get('doc_id')}: source file not found: {missing_path}", flush=True)
                    continue
                raw_bytes = p.read_bytes()
                source_hash = hashlib.sha256(raw_bytes).hexdigest()
                if p.suffix.lower() == ".pdf":
                    page_texts = [(n, clean(t)) for n, t in pdf_pages(p)]
                    text = "\n\n".join(t for _, t in page_texts)
                else:
                    text = raw_bytes.decode("utf-8", errors="ignore")
            else:
                source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            text = clean(text)
            if not text:
                print(f"skip {d.get('doc_id')}: no text")
                continue
            doc_chunks = ([(page, c) for page, page_text in page_texts for c in chunk(page_text)]
                          if page_texts is not None else [(None, c) for c in chunk(text)])
            for i, (page_number, c) in enumerate(doc_chunks):
                out.append({
                    "chunk_id": f"{d['doc_id']}#{i}", "doc_id": d["doc_id"], "collection": d["collection"],
                    "title": d.get("title") or d["doc_id"], "url": d.get("url") or None,
                    "year": int(d["year"]) if (d.get("year") or "").isdigit() else None,
                    "pub_type": d.get("pub_type") or None, "tags": d.get("tags") or None,
                    "source_uri": source_uri, "source_file": source_file, "source_hash": source_hash,
                    "ingest_version": INGESTION_VERSION, "ingested_at": datetime.now(timezone.utc).isoformat(),
                    "page_number": page_number, "chunk_index": i,
                    "source_type": "local_pdf" if path and path.lower().endswith(".pdf") else "manifest",
                    "full_text_status": "full_text_pdf" if page_number is not None and path and path.lower().endswith(".pdf") else None,
                    "topics": d.get("tags") if d.get("collection") == "research" else None,
                    # prepend title so BM25 matches title words in every chunk (cheap contextual retrieval)
                    "text": f"{d.get('title') or ''}\n{c}",
                })
    return out


def _pdf_topics(text):
    text = (text or "").lower()
    rules = {
        "ckd": r"chronic kidney disease|\bckd\b|renal insufficien|kidney disease",
        "dialysis": r"dialysis|hemodialysis|haemodialysis|peritoneal dialysis",
        "transplant": r"kidney transplant|renal transplant|\ballograft\b|transplant recipient",
        "bone_health": r"renal osteodystrophy|ckd.?mineral and bone|bone density|bone mineral",
        "pregnancy": r"pregnan|preeclampsia|pre-eclampsia|gestational|obstetric",
        "womens_health": r"women.s health|menopaus|reproductive health|contraception|gynecol|gynaecol",
    }
    return [name for name, pattern in rules.items() if re.search(pattern, text)]


def rows_from_pdf_dir(directory, literature_path=None, existing_hashes=None):
    """Index every text-readable PDF under a folder, enriching it from PubMed when matchable."""
    import pdfplumber
    from ingest.literature import fetch, resolve, title_match

    root = Path(directory)
    if not root.exists():
        print(f"PDF directory not found: {root}")
        return []
    curated = list(csv.DictReader(open(literature_path, encoding="utf-8"))) if literature_path else []
    paths = sorted(root.rglob("*.pdf"))
    pdf_records = []
    seen_hashes = set(existing_hashes or [])
    text_cache = Path(__file__).resolve().parents[1] / "data" / "pdf_text_cache"
    text_cache.mkdir(parents=True, exist_ok=True)
    for path in paths:
        printable_name = path.name.encode("ascii", "backslashreplace").decode("ascii")
        print(f"reading PDF: {printable_name}", flush=True)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen_hashes:
            print(f"skip duplicate PDF content: {path.name}")
            continue
        seen_hashes.add(digest)
        cache_path = text_cache / f"{digest}.json"
        metadata = {}
        page_texts = None
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                metadata = cached.get("metadata") or {}
                page_texts = [(int(page), text) for page, text in cached.get("pages", [])]
                print(f"  reused extracted pages for {printable_name}", flush=True)
            except (OSError, ValueError, TypeError):
                page_texts = None
        if page_texts is None:
            try:
                with pdfplumber.open(path) as pdf:
                    metadata = pdf.metadata or {}
                    page_texts = [(i + 1, clean(page.extract_text() or "")) for i, page in enumerate(pdf.pages)]
                temp_cache = cache_path.with_suffix(".tmp")
                temp_cache.write_text(json.dumps({"metadata": metadata, "pages": page_texts}, ensure_ascii=False),
                                      encoding="utf-8")
                temp_cache.replace(cache_path)
            except Exception as exc:
                print(f"skip unreadable PDF {printable_name}: {exc}", flush=True)
                continue
        first_text = next((text for _, text in page_texts if text), "")
        if not first_text:
            print(f"skip PDF without a text layer (OCR required): {path.name}")
            continue
        title = " ".join((metadata.get("Title") or "").split())
        if not title:
            title = next((line.strip() for line in first_text.splitlines()
                          if 20 <= len(line.strip()) <= 240 and not line.strip().lower().startswith(("doi:", "http"))),
                         path.stem)
        author_text = " ".join((metadata.get("Author") or "").split()) or None
        doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", "\n".join(t for _, t in page_texts[:2]), re.I)
        doi = doi_match.group(0).rstrip(".,;:)").lower() if doi_match else None

        pmid = None
        try:
            if doi:
                pmid = resolve("doi", doi)
            # PDFs without a DOI can still be linked by an exact-title PubMed search, but the result
            # is accepted only after a title similarity check below.
            if not pmid and title and title != path.stem:
                pmid = resolve("title", title)
            rec = fetch([pmid]).get(pmid) if pmid else None
        except Exception as exc:
            print(f"PubMed metadata unavailable for {path.name}; indexing PDF text only: {exc}")
            pmid, rec = None, None
        if rec and title_match(title, rec["title"]) < 0.78:
            rec = None
            pmid = None
        # Use the curated topic when a PDF matches a seed row; otherwise infer broad tags from text.
        matched_topic = ""
        for item in curated:
            if doi and item.get("id_type") == "doi" and item.get("id", "").lower() == doi:
                matched_topic = item.get("topic", "")
                break
            if pmid and item.get("id_type") == "pmid" and item.get("id", "") == pmid:
                matched_topic = item.get("topic", "")
                break
        full_text = "\n\n".join(t for _, t in page_texts)
        tags = matched_topic or "; ".join(_pdf_topics((rec["title"] + " " + rec["abstract"] + " " + full_text[:5000])
                                                         if rec else (title + " " + full_text[:5000])))
        pdf_records.append({"path": path, "relative_path": path.relative_to(root).as_posix(),
                            "hash": digest, "pages": page_texts, "title": rec["title"] if rec else title,
                            "authors": "; ".join(rec.get("authors", [])) if rec and rec.get("authors") else author_text,
                            "doi": rec.get("doi") if rec and rec.get("doi") else doi,
                            "pmid": pmid, "record": rec, "tags": tags})

    catalog_path = Path(__file__).resolve().parents[1] / "data" / "literature_pdf_catalog.csv"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with catalog_path.open("w", newline="", encoding="utf-8") as f:
        fields = ("doc_id", "pmid", "doi", "title", "authors", "pubmed_author_keywords",
                  "pubmed_mesh_terms", "topic", "source_file", "source_hash", "full_text_status")
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for doc in pdf_records:
            rec = doc["record"] or {}
            pmid, doi = doc["pmid"], doc["doi"]
            doc_id = f"PMID{pmid}" if pmid else (f"DOI{hashlib.sha256(doi.encode()).hexdigest()[:16]}" if doi
                                                   else f"PDF{hashlib.sha256(doc['relative_path'].encode()).hexdigest()[:16]}")
            writer.writerow({"doc_id": doc_id, "pmid": pmid or "", "doi": doi or "", "title": doc["title"],
                             "authors": doc["authors"] or "",
                             "pubmed_author_keywords": "; ".join(rec.get("author_keywords", [])),
                             "pubmed_mesh_terms": "; ".join(rec.get("mesh_terms", [])), "topic": doc["tags"],
                             "source_file": doc["relative_path"], "source_hash": doc["hash"],
                             "full_text_status": "full_text_pdf"})

    out = []
    for doc in pdf_records:
        doi = doc["doi"]
        pmid = doc["pmid"]
        doc_id = f"PMID{pmid}" if pmid else (f"DOI{hashlib.sha256(doi.encode()).hexdigest()[:16]}" if doi
                                               else f"PDF{hashlib.sha256(doc['relative_path'].encode()).hexdigest()[:16]}")
        rec = doc["record"] or {}
        source_uri = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else (f"https://doi.org/{doi}" if doi else None)
        metadata = "\n".join(x for x in (
            f"Topics: {doc['tags']}" if doc["tags"] else "",
            f"Authors: {doc['authors']}" if doc["authors"] else "",
            f"Author keywords: {'; '.join(rec.get('author_keywords', []))}" if rec.get("author_keywords") else "",
            f"MeSH terms: {'; '.join(rec.get('mesh_terms', []))}" if rec.get("mesh_terms") else "") if x)
        chunks = [(page, c) for page, page_text in doc["pages"] for c in chunk(page_text)]
        for i, (page_number, content) in enumerate(chunks):
            out.append({"chunk_id": f"{doc_id}#{i}", "doc_id": doc_id, "collection": "research",
                        "title": doc["title"], "url": source_uri, "year": int(rec["year"]) if (rec.get("year") or "").isdigit() else None,
                        "pub_type": ", ".join(rec.get("pub_types", [])[:2]) or None, "tags": doc["tags"] or None,
                        "source_uri": source_uri or doc["path"].resolve().as_uri(),
                        "source_file": doc["relative_path"], "source_hash": doc["hash"],
                        "ingest_version": INGESTION_VERSION, "ingested_at": datetime.now(timezone.utc).isoformat(),
                        "page_number": page_number, "chunk_index": i,
                        "text": f"{doc['title']}\n{metadata}\n{content}" if metadata else f"{doc['title']}\n{content}",
                        "pmid": pmid, "doi": doi, "topics": doc["tags"] or None,
                        "authors": doc["authors"], "author_keywords": "; ".join(rec.get("author_keywords", [])) or None,
                        "mesh_terms": "; ".join(rec.get("mesh_terms", [])) or None,
                        "abstract": rec.get("abstract") or None,
                        "full_text_status": "full_text_pdf", "source_type": "local_pdf"})
    print(f"PDF ingestion: {len(pdf_records)} readable unique PDFs, {len(out)} full-text chunks")
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
            txt = f"{a['title']}\n{a['abstract']}"
            out.append({"chunk_id": f"PMID{a['pmid']}#0", "doc_id": f"PMID{a['pmid']}", "collection": "research",
                        "title": a["title"], "url": f"https://pubmed.ncbi.nlm.nih.gov/{a['pmid']}/",
                        "year": int(a["year"]) if (a["year"] or "").isdigit() else None,
                        "pub_type": ", ".join(a["pub_types"][:2]), "tags": None,
                        "text": txt, "source_uri": f"https://pubmed.ncbi.nlm.nih.gov/{a['pmid']}/",
                        "source_file": None, "source_hash": hashlib.sha256(txt.encode()).hexdigest(),
                        "ingest_version": INGESTION_VERSION, "ingested_at": datetime.now(timezone.utc).isoformat(),
                        "page_number": None, "chunk_index": 0})
    return out


def load(rows, snapshot_path=None):
    import duckdb
    import pandas as pd
    from app import config
    config.REFERENCE_DB.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        print("no input documents; reference database unchanged")
        return
    df = pd.DataFrame(rows).drop_duplicates("chunk_id")
    column_types = {"source_uri": "VARCHAR", "source_file": "VARCHAR", "source_hash": "VARCHAR",
                    "ingest_version": "VARCHAR", "ingested_at": "VARCHAR", "page_number": "INTEGER",
                    "chunk_index": "INTEGER", "source_type": "VARCHAR", "full_text_status": "VARCHAR",
                    "pmid": "VARCHAR", "doi": "VARCHAR", "authors": "VARCHAR", "author_keywords": "VARCHAR",
                    "mesh_terms": "VARCHAR", "abstract": "VARCHAR", "topics": "VARCHAR"}
    for col in column_types:
        if col not in df:
            df[col] = None
    con = duckdb.connect(str(config.REFERENCE_DB))
    con.execute("INSTALL fts")
    con.execute("LOAD fts")
    vector_rows = []
    if config.ENABLE_SEMANTIC_INDEX:
        from app.semantic import prepare_missing
        vector_rows = prepare_missing(con, df.to_dict("records"))
    con.begin()
    try:
        has_chunks = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='chunks'").fetchall()[0][0]
        if has_chunks:
            # Migrate old local DBs in place before the first incremental load.
            existing = {r[1] for r in con.execute("PRAGMA table_info('chunks')").fetchall()}
            for col, typ in column_types.items():
                if col not in existing:
                    con.execute(f"ALTER TABLE chunks ADD COLUMN {col} {typ}")
            con.register("incoming", df)
            con.execute("DELETE FROM chunks WHERE doc_id IN (SELECT DISTINCT doc_id FROM incoming) "
                        "OR source_file IN (SELECT DISTINCT source_file FROM incoming WHERE source_file IS NOT NULL)")
            con.execute("INSERT INTO chunks BY NAME SELECT * FROM incoming")
        else:
            con.register("incoming", df)
            con.execute("CREATE TABLE chunks AS SELECT * FROM incoming")
        if config.ENABLE_SEMANTIC_INDEX:
            from app.semantic import create_index, write_vectors
            create_index(con)
            write_vectors(con, vector_rows)
            con.execute("DELETE FROM chunk_embeddings WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)")
        con.execute("PRAGMA create_fts_index('chunks', 'chunk_id', 'title', 'text', overwrite=1)")
        con.commit()
    except Exception:
        con.rollback()
        con.close()
        raise
    snapshot = Path(snapshot_path) if snapshot_path else config.DATA_DIR / "chunks.parquet"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(snapshot, index=False)
    n = con.execute("SELECT collection, count(*) FROM chunks GROUP BY 1").fetchall()
    con.close()
    print("chunks by collection:", n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="*", help="one or more manifest CSVs (all are loaded together)")
    ap.add_argument("--pmids")
    ap.add_argument("--literature", help="curated list with title verification (see ingest/literature.py)")
    ap.add_argument("--discover-topics", help="YAML of reviewed PubMed searches for recent literature")
    ap.add_argument("--pdf-dir", help="recursively ingest every readable PDF under this directory")
    ap.add_argument("--literature-report", help="where to write the title-verification report")
    ap.add_argument("--snapshot", help="optional chunk dataset path for DVC/versioned artifacts")
    a = ap.parse_args()
    rows = []
    if a.pdf_dir:
        rows += rows_from_pdf_dir(a.pdf_dir, literature_path=a.literature)
    rows += [r for m in a.manifest for r in rows_from_manifest(m)]
    if a.discover_topics:
        from ingest.literature import rows_from_discovery
        rows += rows_from_discovery(a.discover_topics)
    rows += rows_from_pmids(a.pmids) if a.pmids else []
    if a.literature:
        from app import config
        from ingest.literature import rows_from_literature
        from app import config
        report_path = Path(a.literature_report) if a.literature_report else config.DATA_DIR / "literature_report.csv"
        rows += rows_from_literature(a.literature, report_path=report_path)
    load(rows, snapshot_path=a.snapshot)
