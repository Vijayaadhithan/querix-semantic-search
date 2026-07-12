#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed or not on PATH."
  exit 0
fi

docker compose stop api pgvector redis ollama 2>/dev/null || true

echo "Stopped local Docker services: api, pgvector, redis, ollama."
echo "Data volumes were kept. To remove pgvector data, run explicitly:"
echo "  docker compose rm -f pgvector"
echo "  docker volume rm peronsal_rag_pgvector-data"
