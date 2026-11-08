"""Census file parsing: 2026 gazetteer is pipe-separated, older years are tab-separated with padded headers."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.providers import detect_sep, read_zips  # noqa: E402

PIPE = ("GEOID|GEOIDFQ|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG\n"
        "90024|860Z200US90024|5|0|1.0|0.0|34.065645|-118.435284\n"
        "00601|860Z200US00601|1|0|1.0|0.0|18.180621|-66.749931\n")
TAB = "GEOID\tALAND\tINTPTLAT\tINTPTLONG                    \n90024\t5\t34.065645\t-118.435284\n"


def test_reads_2026_pipe_and_older_tab_formats():
    with tempfile.TemporaryDirectory() as d:
        p, t = Path(d) / "pipe.txt", Path(d) / "tab.txt"
        p.write_text(PIPE)
        t.write_text(TAB)
        assert detect_sep(p) == "|" and detect_sep(t) == "\t"
        a, b = read_zips(p), read_zips(t)
    assert a.to_dict("records")[0] == {"zip": "90024", "lat": 34.065645, "lon": -118.435284}
    assert a.zip.tolist() == ["90024", "00601"]                 # leading zeros kept
    assert b.to_dict("records") == [{"zip": "90024", "lat": 34.065645, "lon": -118.435284}]


def test_nppes_header_matching_tolerates_v1_and_v2_styles():
    import csv
    from ingest.providers import NPPES_COLS, resolve_columns
    base = list(NPPES_COLS) + [f"Healthcare Provider Taxonomy Code_{i}" for i in range(1, 16)]
    styles = {"v1": base, "underscore_caps": [c.replace(" ", "_").upper() for c in base],
              "missing_city": [c for c in base if "City" not in c]}
    with tempfile.TemporaryDirectory() as d:
        res = {}
        for name, header in styles.items():
            p = Path(d) / f"{name}.csv"
            with open(p, "w", newline="") as f:
                csv.writer(f, quoting=csv.QUOTE_ALL).writerow(header)
            res[name] = resolve_columns(p)
    m, tax, missing = res["v1"]
    assert not missing and len(tax) == 15 and m["Provider First Name"] == "Provider First Name"
    m, tax, missing = res["underscore_caps"]
    assert not missing and m["Provider First Name"] == "PROVIDER_FIRST_NAME" and len(tax) == 15
    assert res["missing_city"][2] == ["Provider Business Practice Location Address City Name"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
