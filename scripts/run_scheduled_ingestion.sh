#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPANY_ID="${COMPANY_ID:-gainr}"
LOCK_FILE="${LOCK_FILE:-/tmp/semantic-search-ingest-${COMPANY_ID}.lock}"
WARMUP_LOCK_FILE="${WARMUP_LOCK_FILE:-/tmp/semantic-search-warmup-${COMPANY_ID}.lock}"
CONTAINER_NAME="${INGEST_CONTAINER_NAME:-semantic-search-ingest-${COMPANY_ID}}"
RUN_ANALYTICS_FLUSH="${RUN_ANALYTICS_FLUSH:-true}"
ANALYTICS_BATCH_SIZE="${ANALYTICS_BATCH_SIZE:-500}"
RUN_DAILY_ANALYTICS="${RUN_DAILY_ANALYTICS:-true}"

cd "$PROJECT_DIR"
# Ingestion has priority over the lightweight hourly job. If a warm-up is
# already finishing, wait for it instead of treating the daily ingestion as a
# duplicate and silently skipping the source scan.
exec 8>"$WARMUP_LOCK_FILE"
if ! flock -w 600 8; then
  echo "Timed out waiting for the search-path warm-up lock." >&2
  exit 1
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Scheduled ingestion is already running for ${COMPANY_ID}."
  exit 0
fi

cleanup_container() {
  if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "Stopping scheduled ingestion container ${CONTAINER_NAME}." >&2
    docker stop --time 30 "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
}

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  echo "Scheduled ingestion container ${CONTAINER_NAME} already exists." >&2
  exit 1
fi
trap cleanup_container EXIT TERM INT

if [[ "$RUN_ANALYTICS_FLUSH" == "true" ]]; then
  echo "Uploading pending search analytics for ${COMPANY_ID}."
  if ! docker compose run --rm --no-deps api \
    python scripts/flush_search_analytics.py \
      --company "$COMPANY_ID" \
      --batch-size "$ANALYTICS_BATCH_SIZE"; then
    echo "Search analytics upload failed; local rows retained for retry." >&2
  fi
fi

if [[ "$RUN_DAILY_ANALYTICS" == "true" ]]; then
  echo "Building the daily analytics snapshot for ${COMPANY_ID}."
  if ! docker compose run --rm --no-deps analytics-api \
    python -m analytics_service.refresh --company "$COMPANY_ID"; then
    echo "Daily analytics refresh failed; the previous snapshot remains active." >&2
  fi
  echo "Pruning expired analytics login sessions."
  if ! docker compose run --rm --no-deps analytics-api \
    python -m analytics_service.users prune-sessions; then
    echo "Expired analytics session cleanup failed; continuing ingestion." >&2
  fi
fi

echo "Starting incremental ingestion for ${COMPANY_ID} at $(date --iso-8601=seconds)."
docker compose run --rm --name "$CONTAINER_NAME" api python -m cli.ingest \
  --company "$COMPANY_ID" \
  --database \
  --mysql-reconcile-deletions \
  --mysql-batch-size 500 \
  --embed-batch-size 32

# The API keeps tenant indexes and filter catalogues open in memory. Restart
# only after a successful ingestion so the next request sees the new revision.
docker compose restart api

api_ready=false
for _attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/api/v1/ready >/dev/null; then
    api_ready=true
    break
  fi
  sleep 5
done

if [[ "$api_ready" != "true" ]]; then
  echo "Ingestion completed, but the API did not become ready within 150 seconds." >&2
  exit 1
fi

echo "API is ready; warming vector and BM25 search paths."
docker compose exec -T api python scripts/warm_search_paths.py \
  --company "$COMPANY_ID" \
  --candidates 800
echo "Incremental ingestion and search-path warm-up completed."
