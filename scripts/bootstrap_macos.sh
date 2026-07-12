#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh"
  exit 1
fi

install_formula() {
  local formula="$1"
  if ! brew list --formula "$formula" >/dev/null 2>&1; then
    brew install "$formula"
  fi
}

install_formula ollama
install_formula redis
brew services start ollama
brew services start redis

START_DOCKER_STACK="${START_DOCKER_STACK:-0}"
SKIP_LOCAL_RERANKER="${SKIP_LOCAL_RERANKER:-0}"

if [[ "$START_DOCKER_STACK" == "1" ]] && ! command -v docker >/dev/null 2>&1; then
  echo "Docker Desktop is required for START_DOCKER_STACK=1."
  echo "Install it first: https://www.docker.com/products/docker-desktop/"
  exit 1
fi

if [[ "${INSTALL_MYSQL:-0}" == "1" ]]; then
  install_formula mysql
  brew services start mysql
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  install_formula python
  PYTHON_BIN=python3
fi

if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example; configure database and runtime settings."
fi

if [[ ! -f .env.keys ]]; then
  cp .env.keys.example .env.keys
  chmod 600 .env.keys
  echo "Created .env.keys. Put API keys and DB passwords there; do not commit it."
fi

ollama pull embeddinggemma:latest

if [[ "$SKIP_LOCAL_RERANKER" != "1" ]]; then
  .venv/bin/python scripts/prefetch_models.py
fi

if [[ "$START_DOCKER_STACK" == "1" ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  # shellcheck disable=SC1091
  source .env.keys
  set +a
  if [[ -z "${POSTGRES_USER:-}" || -z "${POSTGRES_PASSWORD:-}" ]]; then
    echo "Set POSTGRES_USER and POSTGRES_PASSWORD in .env.keys before starting pgvector."
    exit 1
  fi
  if [[ -z "${PGVECTOR_USER:-}" || -z "${PGVECTOR_PASSWORD:-}" ]]; then
    echo "Set PGVECTOR_USER and PGVECTOR_PASSWORD in .env.keys before starting pgvector."
    exit 1
  fi
  if [[ -z "${PGVECTOR_DATABASE:-}" ]]; then
    echo "Set PGVECTOR_DATABASE in .env before starting pgvector."
    exit 1
  fi
  docker compose up -d pgvector redis
  docker compose exec -T pgvector \
    psql -U "$POSTGRES_USER" -d "$PGVECTOR_DATABASE" \
    -c "CREATE EXTENSION IF NOT EXISTS vector;"
fi

.venv/bin/python scripts/doctor.py

echo
echo "Bootstrap complete."
echo "Next: configure .env and .env.keys, load the required MySQL tables, then run:"
echo "  .venv/bin/python src/ingest.py --company gainr --mysql --mysql-replace-source"
echo "  .venv/bin/python src/run_api.py"
