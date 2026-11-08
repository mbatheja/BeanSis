---
description: Detect insurer provider-directory files in a folder, set them up, and load them into the provider network
argument-hint: "[folder, default data/raw]"
---

# Load insurer provider directories

Folder to scan: `$ARGUMENTS` (if empty, use `data/raw`). Paths may be Windows paths; handle both separators.

Work in the phases below, in order. **Stop at every CHECKPOINT and wait for my reply.** Never overwrite
existing files: show a diff and make targeted edits. At the end, tell me which parts of my existing code you
kept and exactly what you changed.

## Context (read before starting)
- `ingest/directories.py` loads directory PDFs: `candidate_pages()` keeps pages that mention nephrology /
  OB-GYN / maternal-fetal medicine, the fast model extracts clinicians as JSON, and `load()` matches them to
  NPPES providers by NPI, else name + ZIP. Batch mode: `--batch data/raw/directories.csv` (columns
  `file,plan_id,directory_date`) and `--check` (pre-flight: files exist, YYYY-MM-DD dates, plan_ids exist in
  `plans.csv`, PDFs have a text layer). Read the module before changing anything.
- `data/raw/plans.csv` columns: `plan_id,insurer,plan_name,plan_type,state,find_doctor_url,coverage_doc_url`.
  Template: `data/raw/plans_template.csv`. Our state is **California** (Medi-Cal managed care plans and
  Covered California / commercial plans).
- The `providers` table must exist in `data/reference.duckdb` (built by
  `python -m ingest.providers ... --states CA`). Matching depends on it.

## Phase 1: Detect (read-only)
1. List every file in the folder. Skip files that are obviously not directories: `manifest*.csv`,
   `literature.csv`, `resources_manifest.csv`, `plans*.csv`, `directories*.csv`, `ca_medi_cal_plans*`,
   `DrugReviews.csv`, NPPES/Census files, and our templates.
2. For each remaining file, decide whether it's an insurer provider directory by reading only what's needed:
   for PDFs, extract text from the first ~3 pages and a few middle pages with pdfplumber; for Excel/CSV, read
   the header and ~20 rows. Directory signals: "provider directory", "find a doctor", plan or network names,
   lists of physicians with specialties, addresses, phone numbers, "accepting new patients".
3. For each directory, infer: insurer, plan/network name, plan type (Medi-Cal managed care, Covered
   California, commercial, Medicare Advantage), county/region, directory date (cover, footer, or "updated"
   line), format (text PDF, scanned PDF with no text layer, Excel, CSV), page count, and how many pages
   mention our specialties.
4. Propose a `plan_id` for each (lowercase, hyphens, e.g. `la-care-medi-cal`). Reuse an existing plan_id if
   `plans.csv` already has that plan. Never invent URLs: leave `find_doctor_url` empty unless it's printed in
   the directory itself.

**CHECKPOINT 1:** Show one table: file, is-directory (yes/no + 3-word reason), insurer, plan name, proposed
plan_id, plan type, county, directory date (or "NOT FOUND"), format, specialty pages. List anything you're
unsure of. Ask me to confirm or correct. Do not write any file yet.

## Phase 2: Configure (after I confirm)
1. Add the confirmed plans to `data/raw/plans.csv` (create from the template if missing; append only rows
   whose plan_id isn't there; show the diff).
2. Create or update `data/raw/directories.csv` with one row per confirmed directory (`file` relative to the
   CSV, `plan_id`, `directory_date`). Show the diff.
3. Dates I couldn't confirm: leave them out and tell me; don't guess.

## Phase 3: Add code only where the formats need it
Use the existing pipeline unchanged for text PDFs. Add code only for what Phase 1 found:
- **Scanned PDFs:** if `ocrmypdf` is installed, write OCR'd copies as `<name>.ocr.pdf` (never modify
  originals) and point `directories.csv` at them; if not installed, tell me how to install it on Windows and
  skip those files for now.
- **Excel or CSV directories:** add a small reader in `ingest/directories.py` that maps their columns to the
  same row shape `extract()` returns (`first_name, last_name, specialty, npi, address, city, state, zip,
  phone, accepting_new, page`), filtered to our three specialties, so `load()` is reused unchanged. Wire it
  into batch mode by file extension. No model call is needed for structured files.
- **A PDF layout the extractor handles badly** (check by running `candidate_pages()` and one extraction on
  2-3 specialty pages of each insurer's file): adjust `KEYWORDS` or the prompt only if needed, and show me
  before/after on those pages.
For every code change: targeted edit, a test in `tests/` that runs offline (stub the model and DuckDB the
way the existing tests do), and run all test suites
(`for t in core you eval visit_prep reviews signals security literature transport; do python tests/test_$t.py; done`
or the PowerShell equivalent).

**CHECKPOINT 2:** Show the diffs, test results, and the output of
`python -m ingest.directories --batch data/raw/directories.csv --check`. Estimate the extraction cost: number
of specialty pages across all PDFs, each page one fast-model call. Ask before loading.

## Phase 4: Load (after I approve)
1. Confirm the `providers` table exists; if not, stop and tell me to run `ingest.providers` with `--states CA`.
2. Run `python -m ingest.directories --batch data/raw/directories.csv`.
3. Report per file: clinicians extracted, matched to NPPES, unmatched, by specialty. Then summarize
   `directory_unmatched` (top reasons: missing ZIP, ambiguous name, not in NPPES). If a file matches under
   ~50%, show 5 unmatched rows and suggest a cause; don't "fix" matching by loosening it without asking me.

## Rules
- Health-related data stays local: don't upload or paste directory contents anywhere outside this machine.
- Don't delete or rename my files. Don't change `providers`, NPPES, or other loaders.
- Don't commit to git unless I ask.
