"""
ClinicalTrials.gov tool for the kidney + women's health app.

Pipeline:  CT.gov API v2  ->  flatten  ->  Parquet  ->  DuckDB (+ BM25 index)  ->  search_trials()

Uses the official public API (no scraping):  https://clinicaltrials.gov/api/v2/studies
Run from the project root:  python -m ingest.clinicaltrials
"""

import re
import time
from pathlib import Path

import requests

from app import config

API = "https://clinicaltrials.gov/api/v2/studies"
DATA_DIR = config.DATA_DIR            # shared with the rest of the app
DB_PATH = config.REFERENCE_DB

# Conditions the app cares about; each is queried separately, then de-duplicated.
CONDITION_QUERIES = [
    "chronic kidney disease",
    "lupus nephritis",
    "IgA nephropathy",
    "polycystic kidney disease",
    "kidney transplant",
    "dialysis",
    "preeclampsia",
    "pregnancy AND kidney",
]


# ---------------------------------------------------------------- fetch
def fetch_studies(condition, statuses=("RECRUITING", "NOT_YET_RECRUITING"), page_size=500):
    """Yield raw study JSON objects for one condition query, following nextPageToken."""
    params = {
        "query.cond": condition,
        "filter.overallStatus": ",".join(statuses),
        "pageSize": page_size,
        "format": "json",
    }
    while True:
        r = requests.get(API, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()
        yield from payload.get("studies", [])
        token = payload.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
        time.sleep(0.3)  # be polite to the API


# ---------------------------------------------------------------- flatten
def _excludes_pregnancy(criteria):
    """Heuristic: does the EXCLUSION section mention pregnancy? (Label as heuristic in the UI.)"""
    if not criteria:
        return None
    parts = re.split(r"exclusion criteria", criteria, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) < 2:
        return None
    return bool(re.search(r"pregnan|breast-?feeding|lactat", parts[1], re.IGNORECASE))


def flatten_study(study):
    """Return (trial_row, [site_rows]) from one v2 study object."""
    p = study.get("protocolSection", {})
    ident = p.get("identificationModule", {})
    status = p.get("statusModule", {})
    cond = p.get("conditionsModule", {})
    elig = p.get("eligibilityModule", {})
    desc = p.get("descriptionModule", {})
    design = p.get("designModule", {})
    locs = p.get("contactsLocationsModule", {}).get("locations", [])

    nct = ident.get("nctId")
    criteria = elig.get("eligibilityCriteria", "")
    conditions = cond.get("conditions", [])

    trial = {
        "nct_id": nct,
        "title": ident.get("briefTitle"),
        "status": status.get("overallStatus"),
        "phases": ",".join(design.get("phases", []) or []),
        "conditions_text": "; ".join(conditions),
        "sex": elig.get("sex"),                      # ALL | FEMALE | MALE
        "min_age": elig.get("minimumAge"),
        "max_age": elig.get("maximumAge"),
        "excludes_pregnancy": _excludes_pregnancy(criteria),
        "eligibility_criteria": criteria,
        "brief_summary": desc.get("briefSummary"),
        "last_update": status.get("lastUpdatePostDateStruct", {}).get("date"),
        "url": f"https://clinicaltrials.gov/study/{nct}",
    }
    sites = []
    for loc in locs:
        geo = loc.get("geoPoint") or {}
        sites.append({
            "nct_id": nct,
            "facility": loc.get("facility"),
            "city": loc.get("city"),
            "state": loc.get("state"),
            "zip": loc.get("zip"),
            "country": loc.get("country"),
            "site_status": loc.get("status"),
            "lat": geo.get("lat"),
            "lon": geo.get("lon"),
        })
    return trial, sites


# ---------------------------------------------------------------- build
def build(conditions=CONDITION_QUERIES):
    """Fetch all conditions, write Parquet snapshots, load DuckDB, build BM25 index."""
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    trials, sites, seen = [], [], set()
    for c in conditions:
        for study in fetch_studies(c):
            t, s = flatten_study(study)
            if t["nct_id"] and t["nct_id"] not in seen:
                seen.add(t["nct_id"])
                trials.append(t)
                sites.extend(s)
        print(f"{c!r}: {len(seen)} unique trials so far")

    DATA_DIR.mkdir(exist_ok=True)
    pq.write_table(pa.Table.from_pylist(trials), DATA_DIR / "trials.parquet")
    pq.write_table(pa.Table.from_pylist(sites), DATA_DIR / "trial_sites.parquet")

    con = duckdb.connect(str(DB_PATH))
    con.sql("INSTALL fts; LOAD fts;")
    con.sql(f"CREATE OR REPLACE TABLE trials AS SELECT * FROM '{DATA_DIR / 'trials.parquet'}'")
    con.sql(f"CREATE OR REPLACE TABLE trial_sites AS SELECT * FROM '{DATA_DIR / 'trial_sites.parquet'}'")
    # BM25 index over the text the agent searches; rebuild after every reload.
    con.sql("PRAGMA create_fts_index('trials', 'nct_id', 'title', 'conditions_text', "
            "'brief_summary', overwrite=1)")
    con.close()
    print(f"Loaded {len(trials)} trials, {len(sites)} sites into {DB_PATH}")


# ---------------------------------------------------------------- search (agent tool)
def search_trials(query, lat=None, lon=None, radius_miles=50, women_eligible=True,
                  hide_pregnancy_excluding=False, limit=10, con=None):
    """
    BM25 search over local trials, optionally restricted to sites within radius_miles.
    Returns a list of dicts; nearest site distance included when lat/lon given.
    """
    import duckdb
    con = con or duckdb.connect(str(DB_PATH), read_only=True)
    con.sql("LOAD fts")

    filters = ["score IS NOT NULL"]
    if women_eligible:
        filters.append("sex IN ('ALL', 'FEMALE')")
    if hide_pregnancy_excluding:
        filters.append("coalesce(excludes_pregnancy, false) = false")

    geo_join, geo_select, params = "", "NULL AS nearest_miles", [query]
    if lat is not None and lon is not None:
        geo_join = """
        JOIN (
            SELECT nct_id, min(3958.8 * 2 * asin(sqrt(
                pow(sin(radians(lat - ?) / 2), 2) +
                cos(radians(?)) * cos(radians(lat)) * pow(sin(radians(lon - ?) / 2), 2)
            ))) AS nearest_miles
            FROM trial_sites WHERE lat IS NOT NULL
            GROUP BY nct_id
        ) d USING (nct_id)"""
        geo_select = "d.nearest_miles"
        filters.append("nearest_miles <= ?")
        params = [query, lat, lat, lon, radius_miles]
        # positional order in the SQL text: bm25 query, 3 haversine params, radius

    sql = f"""
        SELECT * FROM (
            SELECT t.nct_id, t.title, t.status, t.phases, t.sex, t.min_age, t.max_age,
                   t.excludes_pregnancy, t.url, {geo_select},
                   fts_main_trials.match_bm25(t.nct_id, ?) AS score
            FROM trials t {geo_join}
        ) sub
        WHERE {' AND '.join(filters)}
        ORDER BY score DESC
        LIMIT {int(limit)}
    """
    rows = con.execute(sql, params).fetchall()
    cols = [c[0] for c in con.description]
    return [dict(zip(cols, r)) for r in rows]


if __name__ == "__main__":
    build()
    for r in search_trials("lupus nephritis", lat=34.0522, lon=-118.2437, radius_miles=100):
        print(r)
