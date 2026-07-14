#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPANY_ID="${COMPANY_ID:-gainr}"
READY_URL="${READY_URL:-http://127.0.0.1:8000/api/v1/ready}"
READINESS_ATTEMPTS="${READINESS_ATTEMPTS:-60}"
READINESS_INTERVAL_SECONDS="${READINESS_INTERVAL_SECONDS:-3}"
LOCK_FILE="${LOCK_FILE:-/tmp/semantic-search-production-deploy.lock}"
RUN_DOCTOR="${RUN_DOCTOR:-true}"

cd "$PROJECT_DIR"

for command_name in git docker curl; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is missing: ${command_name}" >&2
    exit 1
  fi
done

if [[ ! -f .env ]]; then
  echo "Missing production .env in ${PROJECT_DIR}." >&2
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

ready_file="$(mktemp)"
cleanup() {
  rm -f "$ready_file"
}
trap cleanup EXIT

revision="$(git rev-parse --short HEAD)"
echo "Deploying revision ${revision} for company ${COMPANY_ID}."

docker compose config --quiet
docker compose build --pull api
docker compose --profile ollama up -d pgvector redis ollama
docker compose --profile ollama up -d --no-deps --force-recreate api

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

if command -v jq >/dev/null 2>&1; then
  jq . "$ready_file"
else
  printf 'Readiness response: '
  tr -d '\n' < "$ready_file"
  printf '\n'
fi

if [[ "$RUN_DOCTOR" == "true" ]]; then
  docker compose exec -T api python scripts/doctor.py --company "$COMPANY_ID"
fi

docker compose ps
docker compose logs --tail=100 api
echo "Deployment complete: revision ${revision} is ready."
