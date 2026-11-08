"""Provider test-set verification logic (registry responses stubbed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.sample_providers import check_one  # noqa: E402

ROW = {"npi": "1234567890", "name": "JANE A DOE", "specialty": "nephrology", "taxonomy": "207RN0300X",
       "city": "LOS ANGELES", "zip": "90024"}


def rec(first="JANE", last="DOE", status="A", code="207RN0300X", city="LOS ANGELES", zip_="900241234"):
    return {"basic": {"first_name": first, "last_name": last, "status": status},
            "taxonomies": [{"code": code, "primary": True}],
            "addresses": [{"address_purpose": "MAILING", "city": "ELSEWHERE", "postal_code": "99999"},
                          {"address_purpose": "LOCATION", "city": city, "postal_code": zip_}]}


def test_all_match_uses_practice_location_not_mailing():
    r = check_one(ROW, rec())
    assert (r["registry_status"], r["name_match"], r["taxonomy_match"], r["location_match"]) == ("active", 1, 1, 1)


def test_mismatches_are_reported():
    r = check_one(ROW, rec(code="207V00000X", city="PASADENA", zip_="91101"))
    assert r["taxonomy_match"] == 0 and r["location_match"] == 0 and "PASADENA" in r["verify_notes"].upper()
    assert check_one(ROW, None)["registry_status"] == "not_found"
    assert check_one(ROW, rec(first="JOHN", last="SMITH"))["name_match"] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
