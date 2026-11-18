"""
Read-only queries over the reference DuckDB built by ingest/*.

Tables (see ingest/ for loaders):
  chunks(chunk_id, doc_id, collection, title, text, url, year, pub_type, tags)   + BM25 index
      collection in: research | coverage | resource
  trials, trial_sites                         (ingest/clinicaltrials.py)
  providers(npi, name, credential, specialty, taxonomy, address, city, state, zip, phone)
  provider_network(npi, plan_id, accepting_new, source_file, directory_date, match_method)
  plans(plan_id, insurer, plan_name, plan_type, state, find_doctor_url, coverage_doc_url)
  zip_centroids(zip, lat, lon)
"""
import threading
import csv
import time

from . import config

_con = None
_lock = threading.Lock()

HAVERSINE = ("3958.8 * 2 * asin(sqrt(pow(sin(radians({lat2} - {lat1}) / 2), 2) + "
             "cos(radians({lat1})) * cos(radians({lat2})) * pow(sin(radians({lon2} - {lon1}) / 2), 2)))")


def cursor():
    """One shared read-only connection; each call gets its own cursor (thread-safe in DuckDB)."""
    global _con
    with _lock:
        if _con is None:
            import duckdb
            _con = duckdb.connect(str(config.REFERENCE_DB), read_only=True)
            _con.sql("LOAD fts")
    return _con.cursor()


def _rows(cur):
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _plans_csv():
    """Read payer metadata directly when the reference DB has not been rebuilt yet."""
    path = config.DATA_DIR / "raw" / "plans.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def table_exists(name, con=None):
    cur = con or cursor()
    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name])
    return bool(cur.fetchone()[0])


def search_documents(query, collection=None, min_year=None, limit=8, con=None):
    """BM25 search over chunked documents, optionally within one collection."""
    cur = con or cursor()
    available = {r[1] for r in cur.execute("PRAGMA table_info('chunks')").fetchall()}
    metadata_fields = ("pmid", "doi", "authors", "author_keywords", "mesh_terms", "abstract", "topics",
                       "full_text_status", "source_type", "source_uri", "source_file", "source_hash",
                       "ingest_version", "ingested_at", "page_number", "chunk_index")
    extra = ", " + ", ".join(field for field in metadata_fields if field in available) if available else ""
    where, params = ["score IS NOT NULL"], [query]
    if collection:
        where.append("collection = ?")
        params.append(collection)
    if min_year:
        where.append("(year IS NULL OR year >= ?)")
        params.append(int(min_year))
    cur.execute(f"""
        SELECT * FROM (
            SELECT *, row_number() OVER (PARTITION BY doc_id ORDER BY score DESC) AS document_rank
            FROM (
                SELECT chunk_id, doc_id, collection, title, url, year, pub_type, tags{extra},
                       left(text, 1200) AS text,
                       fts_main_chunks.match_bm25(chunk_id, ?) AS score
                FROM chunks)
            WHERE {' AND '.join(where)})
        WHERE document_rank = 1
        ORDER BY score DESC LIMIT {int(limit)}""", params)
    return _rows(cur)


def search_documents_semantic(query_vector, collection=None, min_year=None, limit=8, con=None):
    """Exact cosine search over persisted chunk embeddings, returning one best chunk per document."""
    from . import semantic
    cur = con or cursor()
    if not semantic.has_index(cur):
        return []
    where, params = [], [query_vector]
    if collection:
        where.append("c.collection = ?")
        params.append(collection)
    if min_year:
        where.append("(c.year IS NULL OR c.year >= ?)")
        params.append(int(min_year))
    filters = ("WHERE " + " AND ".join(where)) if where else ""
    cur.execute(f"""
        SELECT * EXCLUDE(document_rank) FROM (
            SELECT c.*, array_cosine_similarity(e.embedding, ?::FLOAT[{semantic.config.EMBEDDING_DIMENSIONS}]) AS score,
                   row_number() OVER (PARTITION BY c.doc_id ORDER BY score DESC) AS document_rank
            FROM chunks c JOIN chunk_embeddings e USING (chunk_id)
            {filters}
        ) WHERE document_rank = 1
        ORDER BY score DESC LIMIT {int(limit)}
    """, params)
    return _rows(cur)


def search_documents_hybrid(query, collection=None, min_year=None, limit=8, con=None,
                            query_vector=None, timings=None):
    """BM25 + cosine candidates fused by reciprocal-rank fusion (RRF, k=60)."""
    from .semantic import embed_query
    timings = timings if timings is not None else {}
    t0 = time.perf_counter()
    if query_vector is None:
        query_vector = embed_query(query)
    timings["embedding_seconds"] = time.perf_counter() - t0
    candidates = max(int(limit) * 4, 20)
    t0 = time.perf_counter()
    lexical = search_documents(query, collection=collection, min_year=min_year, limit=candidates, con=con)
    timings["bm25_seconds"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    semantic = search_documents_semantic(query_vector, collection=collection, min_year=min_year,
                                         limit=candidates, con=con)
    timings["semantic_seconds"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    fused = {}
    for rank, row in enumerate(lexical, 1):
        doc_id = row["doc_id"]
        fused.setdefault(doc_id, {"row": row, "bm25_rank": rank, "semantic_rank": None})
        fused[doc_id]["bm25_rank"] = rank
    for rank, row in enumerate(semantic, 1):
        doc_id = row["doc_id"]
        entry = fused.setdefault(doc_id, {"row": row, "bm25_rank": None, "semantic_rank": None})
        entry["semantic_rank"] = rank
        if entry["bm25_rank"] is None or rank < entry["bm25_rank"]:
            entry["row"] = row
    out = []
    for entry in fused.values():
        score = sum(1 / (60 + rank) for rank in (entry["bm25_rank"], entry["semantic_rank"]) if rank is not None)
        row = dict(entry["row"])
        row.update({"bm25_rank": entry["bm25_rank"], "semantic_rank": entry["semantic_rank"],
                    "hybrid_score": score, "retrieval_method": "bm25+semantic"})
        out.append(row)
    out.sort(key=lambda row: row["hybrid_score"], reverse=True)
    timings["fusion_seconds"] = time.perf_counter() - t0
    return out[:int(limit)]


def search_resources(query, zip_code=None, state=None, limit=8, con=None):
    """Search the curated resource collection, preferring explicit ZIP/state-tagged entries.

    Most current manifest entries are statewide/national and have no ZIP coverage field; those
    remain eligible and are clearly distinguishable from ZIP-specific rows.
    """
    rows = search_documents(query, collection="resource", limit=max(limit * 3, limit), con=con)
    zip5 = str(zip_code or "")[:5]
    state = (state or "").upper()
    def tags(row):
        return {x.strip().upper() for x in str(row.get("tags") or "").replace(",", ";").split(";") if x.strip()}
    specific = [r for r in rows if zip5 and (zip5 in tags(r) or (state and state in tags(r)))]
    general = [r for r in rows if not (zip5 and zip5 in tags(r)) and not (state and state in tags(r))]
    ordered = specific + general
    for row in ordered:
        row["location_match"] = "zip_or_state" if row in specific else "general_or_location_unspecified"
        row["cite_as"] = f"DOC:{row['doc_id']}"
    return ordered[:limit]


def list_plans(state=None, con=None):
    cur = con or cursor()
    if table_exists("plans", cur):
        if state:
            cur.execute("SELECT * FROM plans WHERE state = ? OR state IS NULL ORDER BY insurer", [state])
        else:
            cur.execute("SELECT * FROM plans ORDER BY insurer")
        return _rows(cur)
    rows = _plans_csv()
    return [r for r in rows if not state or not r.get("state") or r["state"] == state]


def zip_to_point(zip_code, con=None):
    cur = con or cursor()
    cur.execute("SELECT lat, lon FROM zip_centroids WHERE zip = ?", [str(zip_code)[:5]])
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def find_providers(specialty, zip_code, plan_id=None, radius_miles=25, limit=10, con=None):
    """
    Deterministic provider search. Distance uses ZIP centroids (approximate, fine for ranking).
    If plan_id is given, only providers found in that plan's directory are returned.
    """
    cur = con or cursor()
    if plan_id and not table_exists("provider_network", cur):
        return {"error": "Provider directories have not been loaded into the reference database.",
                "directory_loaded": False, "providers": []}
    point = zip_to_point(zip_code, con=cur)
    if point is None:
        return {"error": f"Unknown ZIP code {zip_code}"}
    lat, lon = point
    dist = HAVERSINE.format(lat1="?", lon1="?", lat2="z.lat", lon2="z.lon")
    net_join = "JOIN" if plan_id else "LEFT JOIN"
    net_on = "AND n.plan_id = ?" if plan_id else ""
    provenance_join = ("LEFT JOIN (SELECT * FROM provider_directory_provenance "
                       "QUALIFY row_number() OVER (PARTITION BY npi, plan_id "
                       "ORDER BY directory_date DESC, page DESC, source_row DESC) = 1) d "
                       "ON d.npi = p.npi AND d.plan_id = n.plan_id"
                       if table_exists("provider_directory_provenance", cur)
                       else "LEFT JOIN (SELECT NULL::VARCHAR AS npi, NULL::VARCHAR AS plan_id, "
                            "NULL::VARCHAR AS source_sha256, NULL::INTEGER AS page) d ON FALSE")
    sql = f"""
        SELECT * FROM (
            SELECT p.npi, p.name, p.credential, p.specialty, p.address, p.city, p.state, p.zip, p.phone,
                   n.plan_id, n.accepting_new, n.directory_date, n.source_file, n.match_method,
                   d.source_sha256 AS directory_source_sha256, d.page AS directory_source_page,
                   {dist} AS miles
            FROM providers p
            JOIN zip_centroids z ON z.zip = p.zip
            {net_join} provider_network n ON n.npi = p.npi {net_on}
            {provenance_join}
            WHERE p.specialty = ?)
        WHERE miles <= ?
        ORDER BY miles LIMIT {int(limit)}"""
    # positional order in the SQL text: haversine (lat, lat, lon), plan_id?, specialty, radius
    params = [lat, lat, lon] + ([plan_id] if plan_id else []) + [specialty, radius_miles]
    cur.execute(sql, params)
    return _rows(cur)


def plan_by_id(plan_id, con=None):
    cur = con or cursor()
    if table_exists("plans", cur):
        cur.execute("SELECT * FROM plans WHERE plan_id = ?", [plan_id])
        rows = _rows(cur)
    else:
        rows = [r for r in _plans_csv() if r.get("plan_id") == plan_id]
    return rows[0] if rows else None
