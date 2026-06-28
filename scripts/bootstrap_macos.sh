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
  echo "Created .env from .env.example; add GEMINI_API_KEY and MySQL settings."
fi

ollama pull embeddinggemma:latest

if [[ "${SKIP_MODEL_PREFETCH:-0}" != "1" ]]; then
  .venv/bin/python scripts/prefetch_models.py
fi

.venv/bin/python scripts/doctor.py

echo
echo "Bootstrap complete."
echo "Next: configure .env, load the required MySQL tables, then run:"
echo "  .venv/bin/python src/ingest.py --mysql"
echo "  .venv/bin/python src/run_api.py"
