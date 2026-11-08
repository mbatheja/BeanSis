"""
Build providers, zip_centroids, and plans tables.

Downloads (free):
  NPPES full file, Version 2 (V.2) (unzip; use the big npidata_pfile_*.csv). Check the header first with
  --check-nppes; column names are matched ignoring spaces/underscores/case, so V.1 and V.2 both work:
      https://download.cms.gov/nppes/NPI_Files.html
  Census ZCTA gazetteer (ZIP centroids), e.g. 2026_Gaz_zcta_national.txt:
      https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html
  plans.csv (your team writes it): plan_id, insurer, plan_name, plan_type, state, find_doctor_url, coverage_doc_url

Usage:
    python -m ingest.providers --nppes data/raw/npidata_pfile.csv --zips data/raw/2026_Gaz_zcta_national.txt \
        --plans data/raw/plans.csv [--states CA]
"""
import argparse

# NUCC taxonomy codes -> app specialty labels
TAXONOMY = {
    "207RN0300X": "nephrology",
    "207V00000X": "obgyn",
    "207VX0000X": "obgyn",            # obstetrics
    "207VG0400X": "obgyn",            # gynecology
    "207VM0101X": "maternal_fetal_medicine",
}

NPPES_COLS = {
    "NPI": "npi",
    "Entity Type Code": "entity_type",
    "Provider Last Name (Legal Name)": "last_name",
    "Provider First Name": "first_name",
    "Provider Credential Text": "credential",
    "Provider First Line Business Practice Location Address": "address",
    "Provider Business Practice Location Address City Name": "city",
    "Provider Business Practice Location Address State Name": "state",
    "Provider Business Practice Location Address Postal Code": "postal",
    "Provider Business Practice Location Address Telephone Number": "phone",
}


def _norm(name):
    """'Provider First Name' == 'Provider_First_Name' == 'provider first name' (V.1 vs V.2 header styles)."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def resolve_columns(path):
    """Read only the header line; map each needed column to the file's actual header text.
    Returns (mapping {wanted_name: actual_header}, taxonomy_headers, missing)."""
    import csv as _csv
    with open(path, encoding="utf-8-sig", errors="ignore", newline="") as f:
        header = next(_csv.reader(f))
    by_norm = {_norm(h): h for h in header}
    mapping = {k: by_norm.get(_norm(k)) for k in NPPES_COLS}
    tax = [by_norm[_norm(f"Healthcare Provider Taxonomy Code_{i}")] for i in range(1, 16)
           if _norm(f"Healthcare Provider Taxonomy Code_{i}") in by_norm]
    missing = [k for k, v in mapping.items() if v is None] + ([] if tax else ["Healthcare Provider Taxonomy Code_1..15"])
    return mapping, tax, missing


def load_nppes(con, path, states=None):
    """Stream the ~10 GB NPPES CSV in DuckDB, keeping only individual clinicians in our specialties."""
    mapping, tax_headers, missing = resolve_columns(path)
    if missing:
        raise SystemExit(f"NPPES file is missing expected columns: {missing}. Run with --check-nppes to see "
                         "what the header contains.")
    tax_cols = [f'"{h}"' for h in tax_headers]
    codes = ", ".join(f"'{c}'" for c in TAXONOMY)
    # first matching taxonomy among the 15 slots
    first_match = "coalesce(" + ", ".join(f"CASE WHEN {c} IN ({codes}) THEN {c} END" for c in tax_cols) + ")"
    sel = ", ".join(f'"{mapping[k]}" AS {v}' for k, v in NPPES_COLS.items())
    state_filter = ""
    if states:
        state_filter = "AND state IN (" + ", ".join(f"'{s.strip().upper()}'" for s in states) + ")"
    case_map = "CASE " + " ".join(f"WHEN taxonomy = '{k}' THEN '{v}'" for k, v in TAXONOMY.items()) + " END"
    con.sql(f"""
        CREATE OR REPLACE TABLE providers AS
        WITH raw AS (
            SELECT {sel}, {first_match} AS taxonomy
            FROM read_csv('{path}', all_varchar = true, header = true)
        )
        SELECT npi,
               trim(concat_ws(' ', first_name, last_name)) AS name,
               credential, taxonomy, {case_map} AS specialty,
               address, city, state, left(postal, 5) AS zip, phone
        FROM raw
        WHERE entity_type = '1' AND taxonomy IS NOT NULL {state_filter}
    """)
    print(con.sql("SELECT specialty, count(*) FROM providers GROUP BY 1").fetchall())


def detect_sep(path):
    """Census files: older years are tab-separated, 2020+ relationship and 2026 gazetteer files use '|'."""
    with open(path, encoding="utf-8-sig", errors="ignore") as f:
        header = f.readline()
    return "|" if "|" in header else "\t" if "\t" in header else ","


def read_zips(path):
    """ZCTA gazetteer -> zip, lat, lon. Older years are tab-separated with padded headers; 2026 uses '|'."""
    import pandas as pd
    df = pd.read_csv(path, sep=detect_sep(path), dtype=str, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"GEOID": "zip", "INTPTLAT": "lat", "INTPTLONG": "lon"})[["zip", "lat", "lon"]]
    df["zip"] = df["zip"].str.strip().str.zfill(5)
    df["lat"], df["lon"] = pd.to_numeric(df["lat"]), pd.to_numeric(df["lon"])
    return df


def load_zips(con, path):
    df = read_zips(path)
    con.register("zdf", df)
    con.sql("CREATE OR REPLACE TABLE zip_centroids AS SELECT * FROM zdf")
    print("zip centroids:", len(df))


def load_plans(con, path):
    con.sql(f"CREATE OR REPLACE TABLE plans AS SELECT * FROM read_csv('{path}', all_varchar = true, header = true)")
    print("plans:", con.sql("SELECT count(*) FROM plans").fetchone()[0])


if __name__ == "__main__":
    from app import config
    ap = argparse.ArgumentParser()
    ap.add_argument("--nppes")
    ap.add_argument("--zips")
    ap.add_argument("--plans")
    ap.add_argument("--states", help="comma-separated state codes to keep (smaller DB for the demo)")
    ap.add_argument("--check-nppes", action="store_true",
                    help="only read the NPPES header and report whether every needed column is found")
    a = ap.parse_args()
    if a.check_nppes:
        mapping, tax, missing = resolve_columns(a.nppes)
        for k, v in mapping.items():
            print(f"  {'OK ' if v else 'MISSING'}  {k}" + (f"  ->  {v}" if v and v != k else ""))
        print(f"  {'OK ' if tax else 'MISSING'}  taxonomy columns found: {len(tax)} of 15")
        raise SystemExit(1 if missing else 0)
    import duckdb
    config.REFERENCE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(config.REFERENCE_DB))
    if a.nppes:
        load_nppes(con, a.nppes, a.states.split(",") if a.states else None)
    if a.zips:
        load_zips(con, a.zips)
    if a.plans:
        load_plans(con, a.plans)
    con.close()
