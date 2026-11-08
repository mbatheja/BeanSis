"""
Provider test set, built from YOUR providers table (loaded from NPPES), not from third-party sites.

1. Sample:  python -m eval.sample_providers sample
   -> eval/datasets/provider_testset.csv: 50 Los Angeles nephrologists + 50 California OB/GYNs (fixed seed,
      so the set is reproducible)
2. Verify:  python -m eval.sample_providers verify
   Looks each NPI up in the official NPI Registry API (one request per provider, cached) and checks that
   the loader copied it correctly: active, same name, specialty code present, same practice city and ZIP.
   Writes the result columns into the same CSV.
3. Search check: python -m eval.components providers
   For each provider, searching their own ZIP + specialty must return them (tests find_providers).
"""
import argparse
import csv
from pathlib import Path

from app.cache import cached_get_json

OUT = Path(__file__).resolve().parent / "datasets" / "provider_testset.csv"
REGISTRY = "https://npiregistry.cms.hhs.gov/api/"
SAMPLES = [  # (specialty, city or None for statewide, n)
    ("nephrology", "LOS ANGELES", 50),
    ("obgyn", None, 50),
]
FIELDS = ["npi", "name", "specialty", "taxonomy", "city", "zip",
          "registry_status", "name_match", "taxonomy_match", "location_match", "verify_notes"]


def sample(seed=42):
    from app import refdb
    cur = refdb.cursor()
    cur.execute(f"SELECT setseed({seed / 100})")
    rows = []
    for specialty, city, n in SAMPLES:
        where, params = "specialty = ?", [specialty]
        if city:
            where += " AND upper(city) = ?"
            params.append(city)
        cur.execute(f"SELECT npi, name, specialty, taxonomy, city, zip FROM providers WHERE {where} "
                    f"ORDER BY random() LIMIT {int(n)}", params)
        got = cur.fetchall()
        print(f"{specialty}{' in ' + city.title() if city else ' statewide'}: {len(got)} of {n} requested")
        rows += [dict(zip(["npi", "name", "specialty", "taxonomy", "city", "zip"], r)) for r in got]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({**r, **{k: "" for k in FIELDS if k not in r}})
    print(f"wrote {len(rows)} providers -> {OUT}")


def _norm(s):
    return " ".join("".join(ch for ch in (s or "").upper() if ch.isalnum() or ch == " ").split())


def check_one(row, record):
    """Compare our row with the registry record. Returns the result columns."""
    if not record:
        return {"registry_status": "not_found", "name_match": "", "taxonomy_match": "", "location_match": "",
                "verify_notes": "NPI not found in the registry"}
    basic = record.get("basic", {})
    status = basic.get("status", "")
    reg_name = _norm(f"{basic.get('first_name', '')} {basic.get('last_name', '')}")
    ours = _norm(row["name"])
    name_ok = reg_name == ours or (reg_name.split()[-1:] == ours.split()[-1:] and reg_name[:1] == ours[:1])
    tax_ok = any(t.get("code") == row["taxonomy"] for t in record.get("taxonomies", []))
    loc = next((a for a in record.get("addresses", []) if a.get("address_purpose") == "LOCATION"), {})
    loc_ok = (_norm(loc.get("city")) == _norm(row["city"]) and (loc.get("postal_code") or "")[:5] == row["zip"])
    notes = []
    if not loc_ok:
        notes.append(f"registry location: {loc.get('city')} {(loc.get('postal_code') or '')[:5]}")
    if not name_ok:
        notes.append(f"registry name: {reg_name.title()}")
    return {"registry_status": {"A": "active"}.get(status, status or "unknown"),
            "name_match": int(name_ok), "taxonomy_match": int(tax_ok), "location_match": int(loc_ok),
            "verify_notes": "; ".join(notes)}


def verify():
    rows = list(csv.DictReader(open(OUT, encoding="utf-8")))
    for r in rows:
        data = cached_get_json(REGISTRY, {"version": "2.1", "number": r["npi"]}, ttl=7 * 86400)
        res = (data.get("results") or [None])[0]
        r.update(check_one(r, res))
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    n = len(rows)
    for col in ("name_match", "taxonomy_match", "location_match"):
        ok = sum(1 for r in rows if str(r[col]) == "1")
        print(f"{col}: {ok}/{n}")
    print("not active:", sum(1 for r in rows if r["registry_status"] != "active"))
    print(f"details in {OUT} (verify_notes column)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["sample", "verify"])
    sample() if ap.parse_args().step == "sample" else verify()
