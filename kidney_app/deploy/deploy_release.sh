#!/usr/bin/env bash
# Executed remotely by GitHub Actions over SSH. Arguments: app directory, release commit SHA.
set -Eeuo pipefail

APP_DIR="${1:?usage: deploy_release.sh APP_DIR RELEASE_SHA}"
RELEASE_SHA="${2:?usage: deploy_release.sh APP_DIR RELEASE_SHA}"
if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Release SHA must be a full 40-character commit hash." >&2
  exit 2
fi
cd "$APP_DIR"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Refusing deployment: the server checkout has tracked local changes." >&2
  exit 1
fi

git fetch --prune origin main
git cat-file -e "$RELEASE_SHA^{commit}"
if ! git merge-base --is-ancestor "$RELEASE_SHA" origin/main; then
  echo "Release commit is not reachable from origin/main." >&2
  exit 1
fi

PREVIOUS_SHA="$(git rev-parse HEAD)"
if [[ "$PREVIOUS_SHA" == "$RELEASE_SHA" ]]; then
  echo "Release $RELEASE_SHA is already deployed."
  exit 0
fi

DEPLOY_STARTED=0
rollback_on_failure() {
  local status=$?
  trap - EXIT
  if (( status != 0 && DEPLOY_STARTED )); then
    echo "Deployment failed; restoring code at $PREVIOUS_SHA." >&2
    if git checkout --detach "$PREVIOUS_SHA"; then
      docker compose up -d --build --remove-orphans || status=1
      wait_for_ready || status=1
    else
      status=1
    fi
  fi
  exit "$status"
}

wait_for_ready() {
  local n
  for ((n=1; n<=60; n++)); do
    if docker compose exec -T api curl -fsS http://localhost:8000/healthz >/dev/null 2>&1; then
      if docker compose exec -T api python -c 'from app import refdb; c=refdb.cursor(); required={"chunks","trials","trial_sites"}; tables={r[0] for r in c.execute("show tables").fetchall()}; missing=required-tables; assert not missing, f"missing reference tables: {sorted(missing)}"; assert c.execute("select count(*) from chunks").fetchone()[0] > 0, "chunks table is empty"; assert c.execute("select count(*) from trials").fetchone()[0] > 0, "trials table is empty"' >/dev/null 2>&1; then
        return 0
      fi
    fi
    sleep 2
  done
  echo "Deployment readiness check failed." >&2
  return 1
}

trap rollback_on_failure EXIT
DEPLOY_STARTED=1
git checkout --detach "$RELEASE_SHA"
docker compose up -d --build --remove-orphans
wait_for_ready
DEPLOY_STARTED=0
echo "Successfully deployed $RELEASE_SHA"
