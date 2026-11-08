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


def search_documents(query, collection=None, min_year=None, limit=8, con=None):
    """BM25 search over chunked documents, optionally within one collection."""
    cur = con or cursor()
    where, params = ["score IS NOT NULL"], [query]
    if collection:
        where.append("collection = ?")
        params.append(collection)
    if min_year:
        where.append("(year IS NULL OR year >= ?)")
        params.append(int(min_year))
    cur.execute(f"""
        SELECT * FROM (
            SELECT chunk_id, doc_id, collection, title, url, year, pub_type, tags,
                   left(text, 1200) AS text,
                   fts_main_chunks.match_bm25(chunk_id, ?) AS score
            FROM chunks)
        WHERE {' AND '.join(where)}
        ORDER BY score DESC LIMIT {int(limit)}""", params)
    return _rows(cur)


def list_plans(state=None, con=None):
    cur = con or cursor()
    if state:
        cur.execute("SELECT * FROM plans WHERE state = ? OR state IS NULL ORDER BY insurer", [state])
    else:
        cur.execute("SELECT * FROM plans ORDER BY insurer")
    return _rows(cur)


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
    point = zip_to_point(zip_code, con=cur)
    if point is None:
        return {"error": f"Unknown ZIP code {zip_code}"}
    lat, lon = point
    dist = HAVERSINE.format(lat1="?", lon1="?", lat2="z.lat", lon2="z.lon")
    net_join = "JOIN" if plan_id else "LEFT JOIN"
    net_on = "AND n.plan_id = ?" if plan_id else ""
    sql = f"""
        SELECT * FROM (
            SELECT p.npi, p.name, p.credential, p.specialty, p.address, p.city, p.state, p.zip, p.phone,
                   n.plan_id, n.accepting_new, n.directory_date, n.source_file,
                   {dist} AS miles
            FROM providers p
            JOIN zip_centroids z ON z.zip = p.zip
            {net_join} provider_network n ON n.npi = p.npi {net_on}
            WHERE p.specialty = ?)
        WHERE miles <= ?
        ORDER BY miles LIMIT {int(limit)}"""
    # positional order in the SQL text: haversine (lat, lat, lon), plan_id?, specialty, radius
    params = [lat, lat, lon] + ([plan_id] if plan_id else []) + [specialty, radius_miles]
    cur.execute(sql, params)
    return _rows(cur)


def plan_by_id(plan_id, con=None):
    cur = con or cursor()
    cur.execute("SELECT * FROM plans WHERE plan_id = ?", [plan_id])
    rows = _rows(cur)
    return rows[0] if rows else None
