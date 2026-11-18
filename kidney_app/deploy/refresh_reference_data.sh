#!/usr/bin/env bash
# Refresh one reference-data source against a staged DuckDB, then atomically promote it.
# Run on the Linux host with Docker Compose. User app data is never touched.
set -Eeuo pipefail

operation="${1:-}"
case "$operation" in
  clinicaltrials|pubmed|directories) ;;
  *) echo "Usage: $0 {clinicaltrials|pubmed|directories}" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$APP_DIR/data"
cd "$APP_DIR"

if [[ ! -f "$DATA_DIR/reference.duckdb" ]]; then
  echo "Reference database not found at $DATA_DIR/reference.duckdb; build it before scheduling refreshes." >&2
  exit 1
fi

exec 9>"$DATA_DIR/.reference-refresh.lock"
if ! flock -n 9; then
  echo "Another reference-data refresh is already running; skipping $operation."
  exit 0
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE_NAME=".reference-refresh-${operation}-${STAMP}-$$.duckdb"
STAGE_DB_HOST="$DATA_DIR/$STAGE_NAME"
STAGE_DB_CONTAINER="/app/data/$STAGE_NAME"
BACKUP_DB="$DATA_DIR/reference.previous.duckdb"
STOPPED=0
PROMOTED=0
RUN_STARTED_EPOCH="$(date -u +%s)"

write_ingestion_status() {
  local outcome="$1" finished_epoch
  finished_epoch="$(date -u +%s)"
  python3 deploy/record_ingestion_status.py "$DATA_DIR/ingestion_status.json" "$operation" "$outcome" "$RUN_STARTED_EPOCH" "$finished_epoch"
}

wait_for_api() {
  local n
  for ((n=1; n<=45; n++)); do
    if docker compose exec -T api curl -fsS http://localhost:8000/healthz >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

finish() {
  local status=$?
  trap - EXIT
  if (( status == 0 )); then write_ingestion_status success || true; else write_ingestion_status failure || true; fi
  if (( STOPPED )); then
    if (( status != 0 && PROMOTED )); then
      echo "Refresh promotion failed; restoring the previous reference database." >&2
      docker compose stop api research-agent mcp-tools >/dev/null 2>&1 || true
      cp -p "$BACKUP_DB" "$DATA_DIR/reference.duckdb"
    fi
    docker compose up -d api research-agent mcp-tools || status=1
    if (( status == 0 )) && ! wait_for_api; then
      echo "API did not become healthy after reference database refresh." >&2
      status=1
    fi
  fi
  rm -f "$STAGE_DB_HOST"
  exit "$status"
}
trap finish EXIT

echo "[$(date -u +%FT%TZ)] Starting reference refresh: $operation"
write_ingestion_status running
cp -p "$DATA_DIR/reference.duckdb" "$STAGE_DB_HOST"

run_ingest() {
  docker compose run --rm --no-deps \
    -e "REFERENCE_DB=$STAGE_DB_CONTAINER" \
    -e "CACHE_DB=/app/data/reference-refresh-http-cache.sqlite" \
    api "$@"
}

case "$operation" in
  clinicaltrials)
    run_ingest python -m ingest.clinicaltrials
    ;;
  pubmed)
    run_ingest python -m ingest.documents \
      data/raw/resources_manifest.csv data/raw/guidelines_manifest.csv \
      --literature data/raw/literature.csv \
      --discover-topics data/raw/literature_searches.yaml \
      --pdf-dir data/raw/pdfs \
      --literature-report data/literature_refresh_report.csv \
      --snapshot data/literature_chunks.parquet
    ;;
  directories)
    if [[ ! -f data/raw/directories.csv || ! -f data/raw/plans.csv ]]; then
      echo "Directory refresh needs data/raw/directories.csv and data/raw/plans.csv." >&2
      exit 1
    fi
    run_ingest python -m ingest.directories \
      --batch data/raw/directories.csv --plans data/raw/plans.csv
    ;;
esac

# Verify the staged database opens and contains the essential searchable tables before promotion.
run_ingest python -c 'import duckdb,os; c=duckdb.connect(os.environ["REFERENCE_DB"],read_only=True); t={r[0] for r in c.execute("show tables").fetchall()}; required={"chunks","trials","trial_sites"}; missing=required-t; assert not missing, f"missing tables: {sorted(missing)}"; assert c.execute("select count(*) from chunks").fetchone()[0] > 0, "empty chunks table"; assert c.execute("select count(*) from trials").fetchone()[0] > 0, "empty trials table"; print("staged reference database verified")'

# Keep one rollback copy. Readers must be stopped only for this brief same-filesystem swap.
docker compose stop api research-agent mcp-tools
STOPPED=1
cp -p "$DATA_DIR/reference.duckdb" "$BACKUP_DB"
mv -f "$STAGE_DB_HOST" "$DATA_DIR/reference.duckdb"
PROMOTED=1
docker compose up -d api research-agent mcp-tools
if ! wait_for_api; then
  echo "API health check failed after refresh promotion." >&2
  exit 1
fi

STOPPED=0
PROMOTED=0
echo "[$(date -u +%FT%TZ)] Completed reference refresh: $operation"
