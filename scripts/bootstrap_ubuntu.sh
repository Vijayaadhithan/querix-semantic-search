#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f /etc/os-release ]]; then
  echo "Cannot identify this Linux distribution."
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  echo "This installer supports Ubuntu. Detected: ${ID:-unknown}"
  exit 1
fi

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_OLLAMA="${INSTALL_OLLAMA:-1}"
INSTALL_DOCKER="${INSTALL_DOCKER:-1}"
START_DOCKER_STACK="${START_DOCKER_STACK:-0}"
INSTALL_PGVECTOR="${INSTALL_PGVECTOR:-0}"
SKIP_LOCAL_RERANKER="${SKIP_LOCAL_RERANKER:-0}"
SKIP_TESTS="${SKIP_TESTS:-0}"

"${SUDO[@]}" apt-get update
"${SUDO[@]}" apt-get install -y \
  build-essential \
  ca-certificates \
  curl \
  git \
  gnupg \
  libgomp1 \
  libpq-dev \
  pkg-config \
  python3 \
  python3-dev \
  python3-pip \
  python3-venv \
  redis-server

if [[ "$INSTALL_DOCKER" == "1" ]] && ! command -v docker >/dev/null 2>&1; then
  "${SUDO[@]}" install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | "${SUDO[@]}" gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  "${SUDO[@]}" chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | "${SUDO[@]}" tee /etc/apt/sources.list.d/docker.list >/dev/null
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y \
    containerd.io \
    docker-buildx-plugin \
    docker-ce \
    docker-ce-cli \
    docker-compose-plugin
fi

if command -v docker >/dev/null 2>&1; then
  "${SUDO[@]}" systemctl enable --now docker
  if [[ "$(id -u)" -ne 0 ]] && ! id -nG "$USER" | grep -qw docker; then
    "${SUDO[@]}" usermod -aG docker "$USER"
    echo "Added $USER to the docker group. Log out and back in for non-sudo docker."
  fi
fi

"${SUDO[@]}" systemctl enable --now redis-server

if [[ "$INSTALL_OLLAMA" == "1" ]] && ! command -v ollama >/dev/null 2>&1; then
  OLLAMA_INSTALLER="$(mktemp)"
  curl -fsSL https://ollama.com/install.sh -o "$OLLAMA_INSTALLER"
  sh "$OLLAMA_INSTALLER"
  rm -f "$OLLAMA_INSTALLER"
fi

if command -v systemctl >/dev/null 2>&1 && command -v ollama >/dev/null 2>&1; then
  "${SUDO[@]}" systemctl enable --now ollama
fi

if [[ "$INSTALL_PGVECTOR" == "1" ]]; then
  if ! command -v pg_config >/dev/null 2>&1; then
    echo "pg_config is missing. Install PostgreSQL server development files first."
    exit 1
  fi
  PG_MAJOR="$(pg_config --version | awk '{print $2}' | cut -d. -f1)"
  if ! "${SUDO[@]}" apt-get install -y "postgresql-${PG_MAJOR}-pgvector"; then
    "${SUDO[@]}" apt-get install -y "postgresql-server-dev-${PG_MAJOR}"
    PGVECTOR_DIR="$(mktemp -d)"
    git clone --depth 1 --branch v0.8.4 \
      https://github.com/pgvector/pgvector.git "$PGVECTOR_DIR"
    make -C "$PGVECTOR_DIR"
    "${SUDO[@]}" make -C "$PGVECTOR_DIR" install
    rm -rf "$PGVECTOR_DIR"
  fi
fi

if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip setuptools wheel
if [[ "$SKIP_TESTS" == "1" ]]; then
  .venv/bin/python -m pip install -r requirements.txt
else
  .venv/bin/python -m pip install -r requirements-dev.txt
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env. Configure database, CORS, and runtime settings before startup."
fi

if [[ ! -f .env.keys ]]; then
  cp .env.keys.example .env.keys
  chmod 600 .env.keys
  echo "Created .env.keys. Put API keys and DB passwords there; do not commit it."
fi

if command -v ollama >/dev/null 2>&1; then
  ollama pull embeddinggemma:latest
fi

if [[ "$SKIP_LOCAL_RERANKER" != "1" ]]; then
  .venv/bin/python scripts/prefetch_models.py
fi

if [[ "$START_DOCKER_STACK" == "1" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "START_DOCKER_STACK=1 requires Docker."
    exit 1
  fi
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

if [[ "$SKIP_TESTS" != "1" ]]; then
  .venv/bin/python -m pytest -q
fi

echo
echo "Ubuntu bootstrap complete."
echo "Required external configuration:"
echo "  - company MySQL/PostgreSQL credentials"
echo "  - GEMINI_API_KEY"
echo "  - POSTGRES_USER/POSTGRES_PASSWORD for Docker pgvector"
echo "  - PGVECTOR_USER/PGVECTOR_PASSWORD for the API's vector backend"
echo "  - company API keys"
echo
echo "Docker pgvector/Redis stack:"
echo "  START_DOCKER_STACK=1 ./scripts/bootstrap_ubuntu.sh"
echo "  docker compose up -d pgvector redis api"
echo
echo "Local smoke test:"
echo "  .venv/bin/python src/chat.py --query 'bike in Chennai under 1000' --limit 5"
