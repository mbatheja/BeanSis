"""
Insurer directory PDFs -> provider_network table.

Every insurer formats directories differently, so instead of one brittle parser:
  1. pdfplumber extracts text; only pages mentioning our specialties are kept (saves cost)
  2. the fast model turns each page into JSON rows
  3. rows are matched to NPPES providers by NPI, else name + ZIP
Unmatched rows go to directory_unmatched for manual review.

Usage (run once per directory PDF; needs the configured LLM provider key and providers table loaded):
    python -m ingest.directories data/raw/example_plan_directory_2026.pdf --plan-id medi-cal-la-example --date 2026-10-01
"""
import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

KEYWORDS = re.compile(r"nephrolog|obstet|gynec|ob/gyn|obgyn|maternal.?fetal|perinatolog", re.IGNORECASE)
EXTRACTOR_VERSION = "directory-sections-v2"
OTHER_SPECIALTY_HEADER = re.compile(
    r"\b(allergy(?:/immunolog\w*)?|cardiolog\w*|dermatolog\w*|endocrinolog\w*|"
    r"family (?:medicine|practice)|general practice|internal medicine|primary care|"
    r"emergency medicine|gastroenterolog\w*|hematolog\w*|"
    r"infectious disease|neurolog\w*|oncolog\w*|ophthalmolog\w*|orthop\w*|otolaryngolog\w*|"
    r"pediatr\w*|psychiatr\w*|pulmonolog\w*|rheumatolog\w*|urolog\w*|"
    r"surgery|neurosurgery|vascular surgery|pain medicine)\b", re.I)
HEADERISH = re.compile(r"^[A-Z][A-Za-z &/(),.'-]{1,64}$")

EXTRACT_PROMPT = """Extract every individual clinician on this insurance directory page whose specialty is
nephrology, obstetrics/gynecology, or maternal-fetal medicine. Ignore other specialties and facilities.
Return ONLY a JSON array; each item:
{"first_name": str, "last_name": str, "specialty": "nephrology"|"obgyn"|"maternal_fetal_medicine",
 "npi": str|null, "address": str|null, "city": str|null, "state": str|null, "zip": str|null,
 "phone": str|null, "accepting_new": true|false|null, "page": integer|null}
The input can include multiple consecutive pages from the same specialty section. Continue records
across page breaks until the next specialty heading; do not treat a page boundary as a section boundary.
Use the page marker nearest each clinician for page. Return [] if none. Page text:
"""


def candidate_pages(pdf_path):
    """Yield target-specialty sections across page breaks until another specialty heading appears."""
    import pdfplumber
    sections, current, current_page, active = [], [], None, False
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            for line in (x.strip() for x in text.splitlines() if x.strip()):
                is_header = bool(HEADERISH.match(line))
                is_target = is_header and bool(KEYWORDS.search(line))
                is_other = is_header and bool(OTHER_SPECIALTY_HEADER.search(line))
                if active and is_other and not is_target:
                    if current:
                        start_page = int(re.search(r"PAGE (\d+)", current[0]).group(1)) - 1
                        sections.append((start_page, "\n".join(current)))
                    current, current_page, active = [], None, False
                if is_target:
                    active = True
                if active:
                    if current_page != i:
                        current.append(f"--- PAGE {i + 1} ---")
                        current_page = i
                    current.append(line)
    if current:
        sections.append((int(re.search(r"PAGE (\d+)", current[0]).group(1)) - 1, "\n".join(current)))
    # A PDF may contain several target sections separated by another specialty heading.
    for i, text in sections:
        yield i, text


def parse_json_array(raw):
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(raw[start:end + 1])
        return [d for d in data if isinstance(d, dict)]
    except json.JSONDecodeError:
        return []


def extract(pdf_path, llm, model, cache_dir):
    """Extract complete target sections across page breaks; cache each bounded page window."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, text in candidate_pages(pdf_path):
        blocks = re.split(r"(?=--- PAGE \d+ ---)", text)
        windows, window, length = [], [], 0
        for block in blocks:
            if not block.strip():
                continue
            if window and length + len(block) > 9000:
                windows.append("\n".join(window))
                # A little overlap keeps records split at a page/window boundary recoverable.
                last_block = window[-1] if window else ""
                page_match = re.search(r"--- PAGE (\d+) ---", last_block)
                overlap = (f"--- PAGE {page_match.group(1)} ---\n" + last_block[-1200:]) if page_match else ""
                window, length = ([overlap] if overlap else []), len(overlap)
            window.append(block)
            length += len(block)
        if window:
            windows.append("\n".join(window))
        for window_text in windows:
            h = hashlib.sha256((EXTRACTOR_VERSION + model + window_text).encode()).hexdigest()[:20]
            cf = cache_dir / f"{h}.json"
            if cf.exists():
                items = json.loads(cf.read_text(encoding="utf-8"))
            else:
                resp = llm.messages.create(model=model, max_tokens=4000,
                                           messages=[{"role": "user", "content": EXTRACT_PROMPT + window_text}])
                items = parse_json_array("".join(b.text for b in resp.content if b.type == "text"))
                cf.write_text(json.dumps(items), encoding="utf-8")
            pages = [int(x) for x in re.findall(r"--- PAGE (\d+) ---", window_text)]
            for it in items:
                candidate_page = int(it.get("page") or (pages[0] if pages else i + 1))
                it["page"] = candidate_page if candidate_page in pages else (pages[0] if pages else i + 1)
                it["page_start"] = pages[0] if pages else i + 1
                it["page_end"] = pages[-1] if pages else i + 1
            rows.extend(items)
            print(f"pages {pages[0] if pages else i + 1}-{pages[-1] if pages else i + 1}: {len(items)} clinicians")
    # Adjacent-window overlap can repeat a record. Keep the first record with the most specific location.
    unique = {}
    for row in rows:
        key = tuple(str(row.get(k) or "").strip().lower() for k in
                    ("first_name", "last_name", "specialty", "zip", "address"))
        if not any(key):
            key = (*key, str(len(unique)))
        if key not in unique or (not unique[key].get("page") and row.get("page")):
            unique[key] = row
    return list(unique.values())


MATCH_SQL = """
WITH d AS (SELECT * FROM dir),
by_npi AS (
    SELECT d.row_id, p.npi, 'npi' AS method FROM d JOIN providers p ON p.npi = regexp_replace(d.npi, '\\D', '', 'g')
),
by_name AS (
    SELECT d.row_id, any_value(p.npi) AS npi, 'name_zip' AS method
    FROM d JOIN providers p
      ON upper(p.name) LIKE '%' || upper(trim(d.last_name)) || '%'
     AND upper(p.name) LIKE upper(left(trim(d.first_name), 1)) || '%'
     AND p.zip = left(d.zip, 5)
    WHERE d.row_id NOT IN (SELECT row_id FROM by_npi)
    GROUP BY d.row_id HAVING count(DISTINCT p.npi) = 1      -- ambiguous matches are left unmatched
)
SELECT * FROM by_npi UNION ALL SELECT * FROM by_name
"""


def load(rows, plan_id, source_file, directory_date, source_sha256=None, extractor_model=None):
    import duckdb
    import pandas as pd
    from app import config
    df = pd.DataFrame(rows)
    for col in ["first_name", "last_name", "npi", "zip", "accepting_new"]:
        if col not in df:
            df[col] = None
    df["npi"] = df["npi"].fillna("").astype(str)
    df["zip"] = df["zip"].fillna("").astype(str)
    df["row_id"] = range(len(df))
    df["page"] = pd.to_numeric(df.get("page", pd.Series([None] * len(df))), errors="coerce")
    df["page_start"] = pd.to_numeric(df.get("page_start", df["page"]), errors="coerce")
    df["page_end"] = pd.to_numeric(df.get("page_end", df["page"]), errors="coerce")
    con = duckdb.connect(str(config.REFERENCE_DB))
    con.register("dir", df)
    matches = con.sql(MATCH_SQL).df()
    m = df.merge(matches, on="row_id", how="left", suffixes=("_dir", ""))
    matched = m[m["npi"].notna() & (m["npi"] != "")]
    net = pd.DataFrame({"npi": matched["npi"], "plan_id": plan_id, "accepting_new": matched["accepting_new"],
                        "source_file": source_file, "directory_date": directory_date,
                        "match_method": matched["method"]}).drop_duplicates("npi")
    con.register("net", net)
    con.sql("CREATE TABLE IF NOT EXISTS provider_network (npi VARCHAR, plan_id VARCHAR, accepting_new BOOLEAN, "
            "source_file VARCHAR, directory_date VARCHAR, match_method VARCHAR)")
    con.execute("DELETE FROM provider_network WHERE plan_id = ? AND source_file = ?", [plan_id, source_file])
    con.sql("INSERT INTO provider_network SELECT npi, plan_id, accepting_new, source_file, directory_date, "
            "match_method FROM net")
    un = m[m["npi"].isna() | (m["npi"] == "")]
    un = pd.DataFrame({"plan_id": plan_id, "source_file": source_file,
                       **{c: un[c].astype(str) if c in un else "" for c in
                          ("first_name", "last_name", "specialty", "address", "city", "zip", "phone", "page")}})
    con.register("un", un)
    con.sql("CREATE TABLE IF NOT EXISTS directory_unmatched (plan_id VARCHAR, source_file VARCHAR, "
            "first_name VARCHAR, last_name VARCHAR, specialty VARCHAR, address VARCHAR, city VARCHAR, "
            "zip VARCHAR, phone VARCHAR, page VARCHAR)")
    con.execute("DELETE FROM directory_unmatched WHERE plan_id = ? AND source_file = ?", [plan_id, source_file])
    con.sql("INSERT INTO directory_unmatched SELECT * FROM un")
    # Preserve the original directory record and exact source location for audit and reprocessing.
    con.sql("CREATE TABLE IF NOT EXISTS provider_directory_provenance ("
            "plan_id VARCHAR, npi VARCHAR, source_file VARCHAR, source_sha256 VARCHAR, directory_date VARCHAR, "
            "page INTEGER, page_start INTEGER, page_end INTEGER, source_row INTEGER, match_method VARCHAR, "
            "extractor_model VARCHAR, extracted_at VARCHAR, raw_record_json VARCHAR)")
    con.execute("DELETE FROM provider_directory_provenance WHERE plan_id = ? AND source_file = ?", [plan_id, source_file])
    provenance = matched.copy()
    provenance["source_sha256"] = source_sha256
    provenance["directory_date"] = directory_date
    provenance["extractor_model"] = extractor_model
    provenance["extracted_at"] = datetime.now(timezone.utc).isoformat()
    provenance["raw_record_json"] = provenance.apply(lambda r: json.dumps({k: (None if pd.isna(v) else str(v))
        for k, v in r.items() if k not in {"raw_record_json"}}, default=str), axis=1)
    provenance = provenance.rename(columns={"method": "match_method"})
    con.register("provenance", provenance)
    con.execute("INSERT INTO provider_directory_provenance BY NAME "
                "SELECT ? AS plan_id, npi, ? AS source_file, source_sha256, directory_date, "
                "CAST(page AS INTEGER) AS page, CAST(page_start AS INTEGER) AS page_start, "
                "CAST(page_end AS INTEGER) AS page_end, row_id AS source_row, match_method, extractor_model, "
                "extracted_at, raw_record_json FROM provenance", [plan_id, source_file])
    con.close()
    print(f"{len(df)} extracted, {len(net)} matched to NPPES, {len(un)} unmatched (see directory_unmatched)")


def preflight(jobs, plans_csv):
    """Check every job before spending money on extraction. Returns a list of problems (empty = go)."""
    import re as _re
    problems = []
    known = set()
    if plans_csv and Path(plans_csv).exists():
        known = {r["plan_id"].strip() for r in csv.DictReader(open(plans_csv, encoding="utf-8"))}
    for j in jobs:
        f = Path(j["file"])
        if not f.exists():
            problems.append(f"{f}: file not found")
            continue
        if not _re.match(r"^\d{4}-\d{2}-\d{2}$", j.get("directory_date", "")):
            problems.append(f"{f.name}: directory_date must be YYYY-MM-DD (got {j.get('directory_date')!r})")
        if known and j.get("plan_id", "").strip() not in known:
            problems.append(f"{f.name}: plan_id {j.get('plan_id')!r} isn't in {Path(plans_csv).name}; add it there "
                            "first or the doctor tab can't show this plan")
        try:
            import pdfplumber
            with pdfplumber.open(f) as pdf:
                sample = pdf.pages[: min(5, len(pdf.pages))]
                if not any((pg.extract_text() or "").strip() for pg in sample):
                    problems.append(f"{f.name}: no text layer on the first pages (scanned PDF?). Run OCR first, "
                                    "e.g. `ocrmypdf in.pdf out.pdf`, and list the OCR'd file instead")
        except Exception as e:
            problems.append(f"{f.name}: can't open as PDF ({e})")
    return problems


def _client():
    from app.llm import get_client
    return get_client()


if __name__ == "__main__":
    import csv as _csv
    from app import config
    ap = argparse.ArgumentParser(description="Load insurer directory PDFs into provider_network.")
    ap.add_argument("pdf", nargs="?", help="one PDF (or use --batch)")
    ap.add_argument("--plan-id")
    ap.add_argument("--date", help="directory publication date YYYY-MM-DD, shown to users")
    ap.add_argument("--batch", help="CSV with columns file,plan_id,directory_date (paths relative to the CSV)")
    ap.add_argument("--plans", default=str(config.DATA_DIR / "raw" / "plans.csv"),
                    help="plans.csv used to check plan_ids exist")
    ap.add_argument("--check", action="store_true", help="only run the pre-flight checks")
    a = ap.parse_args()
    if a.batch:
        base = Path(a.batch).parent
        jobs = [{**r, "file": str((base / r["file"]).resolve())} for r in _csv.DictReader(open(a.batch, encoding="utf-8"))]
    elif a.pdf and a.plan_id and a.date:
        jobs = [{"file": a.pdf, "plan_id": a.plan_id, "directory_date": a.date}]
    else:
        ap.error("give a PDF with --plan-id and --date, or --batch directories.csv")
    problems = preflight(jobs, a.plans)
    for p in problems:
        print("PROBLEM:", p)
    if problems:
        raise SystemExit(f"{len(problems)} problem(s); nothing was loaded. Fix them and rerun.")
    print(f"pre-flight OK: {len(jobs)} directory file(s)")
    if a.check:
        raise SystemExit(0)
    client = _client()
    for j in jobs:
        print(f"\n== {Path(j['file']).name} -> {j['plan_id']} ({j['directory_date']})")
        rows = extract(j["file"], client, config.MODEL_FAST, config.DATA_DIR / "directory_cache")
        digest = hashlib.sha256(Path(j["file"]).read_bytes()).hexdigest()
        load(rows, j["plan_id"].strip(), Path(j["file"]).name, j["directory_date"], digest, config.MODEL_FAST)
