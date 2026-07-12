#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .env ]]; then
  echo "Missing .env. Run: cp .env.example .env"
  exit 1
fi

if [[ ! -f .env.keys ]]; then
  echo "Missing .env.keys. Run: cp .env.keys.example .env.keys"
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
# shellcheck disable=SC1091
source .env.keys
set +a

if [[ -z "${POSTGRES_USER:-}" || -z "${POSTGRES_PASSWORD:-}" ]]; then
  echo "Set POSTGRES_USER and POSTGRES_PASSWORD in .env.keys."
  exit 1
fi

if [[ -z "${PGVECTOR_USER:-}" || -z "${PGVECTOR_PASSWORD:-}" ]]; then
  echo "Set PGVECTOR_USER and PGVECTOR_PASSWORD in .env.keys."
  exit 1
fi

if [[ -z "${PGVECTOR_DATABASE:-}" ]]; then
  echo "Set PGVECTOR_DATABASE in .env."
  exit 1
fi

docker compose up -d pgvector redis
docker compose exec -T pgvector \
  psql -U "$POSTGRES_USER" -d "$PGVECTOR_DATABASE" \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"

.venv/bin/python - <<'PY'
import os

import psycopg
from dotenv import load_dotenv

load_dotenv(".env")
load_dotenv(".env.keys", override=True)
conn = psycopg.connect(
    host=os.getenv("PGVECTOR_HOST"),
    port=os.getenv("PGVECTOR_PORT"),
    dbname=os.getenv("PGVECTOR_DATABASE"),
    user=os.getenv("PGVECTOR_USER"),
    password=os.getenv("PGVECTOR_PASSWORD"),
)
cur = conn.cursor()
cur.execute("SELECT current_user, current_database()")
print("pgvector:", cur.fetchone())
conn.close()
PY

redis-cli ping >/dev/null
echo "redis: PONG"

ollama pull embeddinggemma:latest
.venv/bin/python scripts/prefetch_models.py

echo "Local pgvector, Redis, embedding model, and reranker are ready."
