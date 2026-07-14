#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPANY_ID="${COMPANY_ID:-gainr}"
LOCK_FILE="${LOCK_FILE:-/tmp/semantic-search-ingest-${COMPANY_ID}.lock}"

cd "$PROJECT_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Scheduled ingestion is already running for ${COMPANY_ID}."
  exit 0
fi

echo "Starting incremental ingestion for ${COMPANY_ID} at $(date --iso-8601=seconds)."
docker compose run --rm api python src/ingest.py \
  --company "$COMPANY_ID" \
  --database \
  --mysql-reconcile-deletions \
  --mysql-batch-size 500 \
  --embed-batch-size 32

# The API keeps tenant indexes and filter catalogues open in memory. Restart
# only after a successful ingestion so the next request sees the new revision.
docker compose restart api

for _attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/api/v1/ready >/dev/null; then
    echo "Incremental ingestion completed and API is ready."
    exit 0
  fi
  sleep 5
done

echo "Ingestion completed, but the API did not become ready within 150 seconds." >&2
exit 1
