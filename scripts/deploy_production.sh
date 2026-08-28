#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPANY_ID="${COMPANY_ID:?COMPANY_ID must be set explicitly for the tenant}"
READY_URL="${READY_URL:-http://127.0.0.1:8000/api/v1/ready}"
READINESS_ATTEMPTS="${READINESS_ATTEMPTS:-60}"
READINESS_INTERVAL_SECONDS="${READINESS_INTERVAL_SECONDS:-3}"
LOCK_FILE="${LOCK_FILE:-/tmp/semantic-search-production-deploy.lock}"
RUN_DOCTOR="${RUN_DOCTOR:-true}"
RUN_ANALYTICS_MIGRATION="${RUN_ANALYTICS_MIGRATION:-true}"
RUN_ANALYTICS_INITIAL_REFRESH="${RUN_ANALYTICS_INITIAL_REFRESH:-true}"
RUN_DATABASE_ROLE_PROVISIONING="${RUN_DATABASE_ROLE_PROVISIONING:-true}"
ANALYTICS_READY_URL="${ANALYTICS_READY_URL:-http://127.0.0.1:8010/api/v1/ready}"

cd "$PROJECT_DIR"

for command_name in git docker curl python3; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is missing: ${command_name}" >&2
    exit 1
  fi
done

if [[ ! -f .env ]]; then
  echo "Missing production .env in ${PROJECT_DIR}." >&2
  exit 1
fi
if [[ ! -f .env.keys ]]; then
  echo "Missing production .env.keys in ${PROJECT_DIR}." >&2
  exit 1
fi

working_changes="$(git status --porcelain --untracked-files=normal)"
if [[ -n "$working_changes" ]]; then
  echo "Production checkout has uncommitted files; review them before deployment:" >&2
  printf '%s\n' "$working_changes" >&2
  exit 1
fi

exec 9>"$LOCK_FILE"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
  echo "Another production deployment is already running." >&2
  exit 1
fi
exec 8>"/tmp/semantic-search-ingest-${COMPANY_ID}.lock"
if ! flock -n 8; then
  echo "Tenant ingestion is running; retry deployment after it completes." >&2
  exit 1
fi
exec 7>"/tmp/semantic-search-analytics-${COMPANY_ID}.lock"
if ! flock -n 7; then
  echo "Tenant analytics is running; retry deployment after it completes." >&2
  exit 1
fi

ready_file="$(mktemp)"
cleanup() {
  rm -f "$ready_file"
}
trap cleanup EXIT

revision="$(git rev-parse --short HEAD)"
echo "Deploying revision ${revision} for company ${COMPANY_ID}."

python3 scripts/ensure_service_credentials.py
python3 scripts/render_service_env.py --production
docker compose config --quiet
docker compose build --pull api analytics-api
if [[ "$RUN_ANALYTICS_MIGRATION" == "true" ]]; then
  docker compose run --rm --no-deps database-admin \
    python scripts/migrate_search_analytics.py --company "$COMPANY_ID"
fi
if [[ "$RUN_DATABASE_ROLE_PROVISIONING" == "true" ]]; then
  docker compose run --rm --no-deps database-admin \
    python scripts/provision_database_roles.py
fi
docker compose run --rm --no-deps ingestion \
  python -m cli.ingest \
    --company "$COMPANY_ID" \
    --database \
    --check \
    --limit 1
docker compose run --rm --no-deps ingestion \
  python -m cli.ingest --company "$COMPANY_ID" --list
docker compose run --rm --no-deps telemetry-uploader \
  python scripts/check_search_analytics_schema.py \
    --company "$COMPANY_ID"
python3 scripts/migrate_runtime_storage.py --preflight

# The API is the only writer of its local SQLite state. Stop it before moving
# the databases into the search-only bind mount, including any WAL sidecars.
docker compose stop api
python3 scripts/migrate_runtime_storage.py
docker compose run --rm --no-deps telemetry-uploader \
  python scripts/flush_search_analytics.py \
    --company "$COMPANY_ID"
if [[ "$RUN_ANALYTICS_INITIAL_REFRESH" == "true" ]]; then
  docker compose run --rm --no-deps analytics-api \
    python -m analytics_service.refresh \
      --company "$COMPANY_ID" \
      --if-missing
fi
docker compose --profile ollama up -d pgvector redis ollama
docker compose --profile ollama up -d --no-deps --force-recreate \
  api analytics-api

ready=false
for ((attempt = 1; attempt <= READINESS_ATTEMPTS; attempt++)); do
  if curl -fsS --max-time 5 -o "$ready_file" "$READY_URL" 2>/dev/null; then
    ready=true
    break
  fi
  echo "Waiting for API readiness (${attempt}/${READINESS_ATTEMPTS})..."
  sleep "$READINESS_INTERVAL_SECONDS"
done

if [[ "$ready" != "true" ]]; then
  echo "API did not become ready at ${READY_URL}." >&2
  docker compose ps >&2 || true
  docker compose logs --tail=200 api >&2 || true
  exit 1
fi

if ! curl -fsS --max-time 5 "$ANALYTICS_READY_URL" >/dev/null; then
  echo "Analytics API is not ready at ${ANALYTICS_READY_URL}." >&2
  docker compose logs --tail=200 analytics-api >&2 || true
  exit 1
fi

if command -v jq >/dev/null 2>&1; then
  jq . "$ready_file"
else
  printf 'Readiness response: '
  tr -d '\n' < "$ready_file"
  printf '\n'
fi

if [[ "$RUN_DOCTOR" == "true" ]]; then
  docker compose exec -T api python scripts/doctor.py \
    --company "$COMPANY_ID" \
    --strict \
    --production
fi

docker compose exec -T api python scripts/warm_search_paths.py \
  --company "$COMPANY_ID" \
  --candidates 800

docker compose ps
docker compose logs --tail=100 api analytics-api
echo "Deployment complete: revision ${revision} is ready."
