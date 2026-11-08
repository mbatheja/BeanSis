"""
Load the DrugRev patient-review dataset into the reference DB.

Source: Ahmed, Mia, Mazrur, Rana, Ekfa, Rahman (2023). "DrugRev: A comprehensive customers reviews on drugs
purchasing and satisfaction." Mendeley Data, V1. https://doi.org/10.17632/64cc5w5dxy.1  (CC BY 4.0)
Reviews were collected from Drugs.com (2008-2023). Credit the dataset wherever review data is shown.

Cleaning:
  - UserName is dropped (not needed, and the app shouldn't hold reviewer handles)
  - scraping debris is cut: ~1% of reviews have another page's header and review glued on
  - very short reviews are dropped; dates normalized to ISO; "For X" -> "X"

Usage (DrugReviews.csv is ~170 MB, so it isn't bundled; put it in data/raw/):
    python -m ingest.reviews data/raw/DrugReviews.csv
"""
import argparse
import re

DEBRIS = re.compile(r"'?[^'\n]{0,120}User Reviews & Ratings \(Page \d+\).*", re.DOTALL)
SOURCE_URL = "https://doi.org/10.17632/64cc5w5dxy.1"
MIN_CHARS = 20


def clean_text(text):
    text = DEBRIS.sub("", str(text or ""))
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip().strip('"').strip()
    return text


def clean(path):
    import pandas as pd
    df = pd.read_csv(path, encoding="utf-8-sig")
    df = df.drop(columns=["UserName"], errors="ignore")
    df["review"] = df["Reviews"].map(clean_text)
    had_debris = (df["review"].str.len() < df["Reviews"].astype(str).str.len() - 40).sum()
    df = df[df["review"].str.len() >= MIN_CHARS].copy()
    out = pd.DataFrame({
        "review_id": range(1, len(df) + 1),
        "medicine_name": df["MedicineName"].astype(str).str.strip(),
        "name_key": df["MedicineName"].astype(str).str.strip().str.lower(),
        # "Prograf (tacrolimus) for Organ Transplant" -> "Organ Transplant", so one condition = one group
        "condition": df["MedicineFor"].astype(str).str.replace(r"^For\s+", "", regex=True)
                     .str.replace(r"^.{1,80}?\([^)]*\)\s+for\s+", "", regex=True).str.strip(),
        "review_date": pd.to_datetime(df["ReviewDate"], format="%d-%b-%y", errors="coerce").dt.date.astype(str),
        "intake_time": df["IntakeTime"].astype(str),
        "rating": df["Rating"].astype(int),
        "likes": df["NumberOfLikes"].astype(int),
        "review": df["review"],
    })
    print(f"{len(out)} reviews kept; debris trimmed from ~{had_debris}; usernames dropped")
    return out


def load(df):
    import duckdb
    from app import config
    config.REFERENCE_DB.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(config.DATA_DIR / "reviews.parquet", index=False)
    con = duckdb.connect(str(config.REFERENCE_DB))
    con.sql("INSTALL fts; LOAD fts;")
    con.register("rdf", df)
    con.sql("CREATE OR REPLACE TABLE reviews AS SELECT * FROM rdf")
    con.sql("CREATE INDEX IF NOT EXISTS ix_reviews_name ON reviews (name_key)")
    con.sql("PRAGMA create_fts_index('reviews', 'review_id', 'review', overwrite=1)")
    print("drugs:", con.sql("SELECT count(DISTINCT name_key) FROM reviews").fetchone()[0])
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    load(clean(ap.parse_args().csv))
