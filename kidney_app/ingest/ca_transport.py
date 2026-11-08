"""
Load California ZIP -> county data for the ride finder.

Download the Census 2020 ZCTA-to-county relationship file (free), e.g. tab20_zcta520_county20_natl.txt:
  https://www.census.gov/geographies/reference-files/time-series/geo/relationship-files.html
Usage:  python -m ingest.ca_transport data/raw/tab20_zcta520_county20_natl.txt

Counties covering under 5% of a ZIP's land are dropped, so a ZIP with a tiny sliver in a neighboring
county doesn't trigger a "which county?" question. The plan list is a separate CSV your team maintains
(data/raw/ca_medi_cal_plans.csv), read directly by app/tools/transport.py.
"""
import argparse

MIN_SHARE = 0.05


def load(path):
    import duckdb
    import pandas as pd
    from app import config
    from ingest.providers import detect_sep
    df = pd.read_csv(path, sep=detect_sep(path), dtype=str, encoding="utf-8-sig")
    col = lambda key: next(c for c in df.columns if c.strip().upper() == key)      # noqa: E731
    zcta, cty, name, part = (col("GEOID_ZCTA5_20"), col("GEOID_COUNTY_20"), col("NAMELSAD_COUNTY_20"),
                             col("AREALAND_PART"))
    df = df[df[cty].str.startswith("06", na=False) & df[zcta].notna()].copy()     # 06 = California
    df["part"] = pd.to_numeric(df[part], errors="coerce").fillna(0)
    df["share"] = df["part"] / df.groupby(zcta)["part"].transform("sum").replace(0, 1)
    df["county"] = df[name].str.replace(r"\s+County$", "", regex=True)
    df = df.sort_values([zcta, "share"], ascending=[True, False])
    keep = df[(df["share"] >= MIN_SHARE) | (df.groupby(zcta).cumcount() == 0)]
    out = keep.rename(columns={zcta: "zip"})[["zip", "county", "share"]].rename(columns={"share": "land_share"})
    con = duckdb.connect(str(config.REFERENCE_DB))
    con.register("zc", out)
    con.sql("CREATE OR REPLACE TABLE zip_county AS SELECT * FROM zc")
    multi = out.groupby("zip").size().gt(1).sum()
    print(f"CA ZIPs: {out['zip'].nunique()}, spanning 2+ counties: {multi}")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    load(ap.parse_args().path)
