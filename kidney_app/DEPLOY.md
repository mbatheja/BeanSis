# Deploying: Base44 (frontend + login) + Nebius (API)

## Step 0: Compare with your existing folder BEFORE changing anything
Your working folder may already contain some of these changes, or newer versions of the same files
(for example a Nebius model adapter). Never copy this package over it. Diff first, then make targeted edits.

**0a. Snapshot your current state**, so every change below shows up as a reviewable diff:
```
cd ~/path/to/your/kidney_app
git status                      # commit or stash anything pending
git add -A && git commit -m "before deployment changes"
```

**0b. Unpack this package somewhere else** and get the overview:
```
unzip kidney_app.zip -d ~/kidney_app_new
diff -rq --exclude=data --exclude=__pycache__ --exclude=.env --exclude=results \
  ~/path/to/your/kidney_app ~/kidney_app_new/kidney_app
```
`Only in ~/kidney_app_new/...` = a file you don't have. `Files ... differ` = compare it (0c/0d).

**0c. New files from this round.** Add the ones you don't have. If you already have a file with the same
name, diff it (`diff -u yours theirs`) and merge by hand instead of replacing it.

| File | Purpose |
|---|---|
| `app/security.py` | shared-secret check + per-user rate limiting |
| `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `deploy/Caddyfile` | container + HTTPS |
| `deploy/setup_vm.sh`, `deploy/push_data.sh`, `deploy/backup.sh` | VM setup, data upload, backups |
| `base44/functions/kidney-api/entry.ts` | Base44 proxy (auth, route allowlist, user_id injection) |
| `base44/functions/reminder-relay/entry.ts` | reminder emails via Base44 SendEmail |
| `base44/FRONTEND_SPEC.md` | spec to paste into the Base44 builder |
| `tests/test_security.py` | tests secret + rate limit, and that the proxy allows every API route |
| `.gitignore` | keeps `.env`, `data/`, eval results out of git (merge with yours if you have one) |

**0d. Edits to existing files: `deploy/deployment_changes.patch`.** Six hunks in four files. Check
whether they apply to YOUR versions before applying anything:
```
cd ~/path/to/your/kidney_app
git apply --check -v ~/kidney_app_new/kidney_app/deploy/deployment_changes.patch
```
- Applies cleanly: `git apply ~/kidney_app_new/kidney_app/deploy/deployment_changes.patch`, then
  review with `git diff`.
- Fails (your file changed nearby, e.g. the `llm = anthropic.Anthropic()` line in `api.py` was replaced
  by a Nebius client, which is context for hunk 2): run
  `git apply --reject ~/kidney_app_new/kidney_app/deploy/deployment_changes.patch`.
  It applies every hunk that fits, leaves your lines alone, and writes the ones that don't fit to
  `<file>.rej` (e.g. `app/api.py.rej`). Apply those by hand using the table below, then delete the
  `.rej` files. (Don't use `--3way`: it needs git history from this package that your repo won't have.)

| File | Hunk | Change |
|---|---|---|
| `app/api.py` | 1 | `import os` |
| `app/api.py` | 2 | import `security`; replace the open `CORSMiddleware(allow_origins=["*"])` with: CORS only if `ALLOWED_ORIGINS` is set; `guard` middleware (401 without `x-app-secret`, 429 over the per-user limit on costly POSTs); `GET /healthz` |
| `app/api.py` | 3 | `log_dose`: 404 unless the medication belongs to that user (bug fix) |
| `app/you/meds.py` | 1 | reminder webhook POST sends header `x-relay-secret` |
| `app/config.py` | 1 | `REMINDER_WEBHOOK_SECRET` setting |
| `.env.example` | 1 | deployment variables block (`DOMAIN`, secrets, webhook URL, rate limits, `ALLOWED_ORIGINS`) |

**0e. If you already built an equivalent, keep yours** and only confirm it meets these requirements:
- [ ] every API route except `/healthz` rejects requests without the shared secret
- [ ] `user_id` comes only from the logged-in Base44 user (set by the proxy), never from the browser
- [ ] the proxy only allows listed routes; `{me}` paths can only reach the caller's own data
- [ ] model-calling POSTs are rate-limited per user
- [ ] CORS closed in production
- [ ] reminder webhook is authenticated (`x-relay-secret`) and reminder text names no drugs
- [ ] marking a dose checks that the medication belongs to the user
- [ ] container runs as non-root, ONE worker, with `data/` on a persistent mount
- [ ] `.env` is git-ignored

**0f. Verify after merging.** Run every test suite, including `python tests/test_security.py`. Its
allowlist test fails if your `api.py` has a route the proxy doesn't allow (or the reverse), which is the
most likely thing to drift when two versions are merged. Then `git diff` to review the result.

**Using Claude Code for this?** Tell it: "Follow Step 0 of DEPLOY.md. Do not overwrite existing files.
For each file, show me the diff against my version and what you'll change, make targeted edits only,
and report which parts of my code you kept."

## 0. Prepare secrets (on your machine)
Generate two long random strings, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`:
- `APP_SHARED_SECRET`: Base44 proxy -> API
- `REMINDER_WEBHOOK_SECRET`: API -> Base44 reminder relay
Never commit them. Keep `.env` out of git.

## 1. Build the reference data locally
The ingest scripts need internet and large downloads, so run them on your machine:
```
python -m ingest.documents data/raw/manifest.csv --pmids data/raw/pmids.txt
python -m ingest.clinicaltrials
# NPPES: download 'Monthly NPPES Downloadable File Version 2 (V.2)' from download.cms.gov/nppes/NPI_Files.html,
# unzip, keep the big npidata_pfile_*.csv as data/raw/npidata_pfile.csv (don't open it in Excel). Check it first:
python -m ingest.providers --nppes data/raw/npidata_pfile.csv --check-nppes
python -m ingest.providers --nppes data/raw/npidata_pfile.csv --zips data/raw/2026_Gaz_zcta_national.txt --plans data/raw/plans.csv --states CA
python -m ingest.directories data/raw/<directory>.pdf --plan-id <plan> --date <YYYY-MM-DD>
python -m ingest.reviews data/raw/DrugReviews.csv
```
Result: `data/reference.duckdb`. Run the tests and a small eval before deploying.

## 2. Create the Nebius VM
In the Nebius console, create a Compute VM: CPU-only (2-4 vCPU, 8 GB RAM is plenty; no GPU), Ubuntu,
a public IP, your SSH key, and a boot disk of 50 GB+ (reference DB + user data + backups).
Allow inbound 22, 80, 443 in the VM's network/security settings.

## 3. Domain
Point a DNS A record (e.g. `api.yourdomain.com`) at the VM's public IP. Caddy gets the HTTPS certificate
automatically once DNS resolves. (Without a domain, `<ip-with-dashes>.sslip.io` style hostnames often
work for a demo, but use a real domain for anything long-lived.)

## 4. Set up the VM and start the API
```
ssh ubuntu@<vm-ip>
git clone <your-repo> ~/kidney_app && cd ~/kidney_app
bash deploy/setup_vm.sh           # then log out and back in once
cp .env.example .env && nano .env # ANTHROPIC_API_KEY (or Nebius vars), FDC_API_KEY, DOMAIN,
                                  # APP_SHARED_SECRET, REMINDER_WEBHOOK_SECRET; leave ALLOWED_ORIGINS empty
```
From your machine: `bash deploy/push_data.sh ubuntu@<vm-ip>`
Back on the VM:
```
sudo chown -R 1000:1000 data       # the container runs as uid 1000
docker compose up -d --build
curl https://api.yourdomain.com/healthz                       # {"ok": true}
curl -i https://api.yourdomain.com/board?user_id=x            # 401: the secret check works
```

## 5. Base44
1. Create the app; enable backend functions (requires a plan that includes them).
2. Add the two functions from `base44/functions/` (`kidney-api`, `reminder-relay`) and deploy them.
3. App secrets: `KIDNEY_API_URL=https://api.yourdomain.com`, `KIDNEY_API_SECRET=<APP_SHARED_SECRET>`,
   `RELAY_SECRET=<REMINDER_WEBHOOK_SECRET>`.
4. Put the reminder function's URL in the VM's `.env` as `REMINDER_WEBHOOK_URL`, then
   `docker compose up -d` to reload.
5. Paste `base44/FRONTEND_SPEC.md` into the Base44 builder to generate the UI.
6. Test the `reminder-relay` user lookup (`entities.User.get`) in your app; adjust if your app's user
   model differs.

## 6. Smoke test (with a synthetic test account)
Log in, set a profile, ask one question per tab, pin an answer, add two medications and run the
interaction check, log a meal, generate a visit sheet, add a medication with a reminder 2 minutes out
and confirm the email arrives, then use "Delete all my data" and confirm the You tab is empty.

## Day 2
- **Update**: `git pull && docker compose up -d --build` (user data on the mounted disk survives).
- **New reference data**: rebuild locally, `push_data.sh`, `docker compose restart api`.
- **Backups**: `crontab -e` -> `0 3 * * * bash ~/kidney_app/deploy/backup.sh` (14 days kept on the VM;
  also copy them off the VM, e.g. to Nebius Object Storage).
- **Logs**: `docker compose logs -f api`. Don't add logging of prompts or answers: they contain health data.
- **Scaling note**: one API container by design (SQLite + in-process reminder loop). If you need more,
  move to Postgres (`DATABASE_URL`) and run reminders as a separate single worker first.

## Go-live checklist (before real patients, not just the demo)
- [ ] Clinician review: safety patterns and messages, golden set, visit-prep questions, tab prompts
- [ ] Eval baseline with judge on: 0 hard failures, 0 violations; safety urgent recall with `--llm` reviewed
- [ ] Privacy notice and terms published; consent at signup; data-deletion flow tested
- [ ] Breach-response plan (the FTC Health Breach Notification Rule covers apps like this)
- [ ] Backups copied off the VM and a restore tested once
- [ ] Secrets only in `.env` / Base44 settings; `.env` not in git
- [ ] DrugRev credit visible wherever review data appears
Until these are done, call it a beta and use synthetic personas in demos.
