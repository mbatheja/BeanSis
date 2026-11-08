#!/usr/bin/env bash
# Run on YOUR machine after building the reference DB locally (ingest scripts need internet + big files).
# Usage: bash deploy/push_data.sh ubuntu@<vm-ip>
set -euo pipefail
VM="$1"
rsync -avz --progress data/reference.duckdb "$VM:~/kidney_app/data/reference.duckdb"
echo ">> Uploaded. On the VM: docker compose restart api"
