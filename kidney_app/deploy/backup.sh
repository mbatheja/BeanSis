#!/usr/bin/env bash
# Nightly backup of user data on the VM. Add to cron:  0 3 * * * bash ~/kidney_app/deploy/backup.sh
set -euo pipefail
cd ~/kidney_app
STAMP=$(date +%Y%m%d-%H%M)
# sqlite3 .backup is safe while the app is running
docker compose exec -T api python -c "import sqlite3; s=sqlite3.connect('data/app.db'); d=sqlite3.connect('data/app.backup.db'); s.backup(d); d.close()"
mv data/app.backup.db ~/backups/app-$STAMP.db
find ~/backups -name 'app-*.db' -mtime +14 -delete     # keep two weeks
