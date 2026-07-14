# Local pgvector Workflow

This workflow validates a tenant profile, builds local indexes, and runs a search without production-specific hostnames or credentials.

## Prerequisites

- Python environment with `requirements.txt` installed
- PostgreSQL with the `vector` extension
- Redis
- Ollama with the configured embedding model
- A tenant YAML under `configs/tenants/`
- Non-secret values in `.env` and secrets in `.env.keys`

Set a reusable tenant slug:

```bash
export COMPANY_ID=<tenant-slug>
```

## Start dependencies

The repository supports two local API modes. Use only one API process on port
8000 at a time.

### Python API on the Mac

The normal `.env` host addresses are used:

```dotenv
OLLAMA_BASE_URL=http://localhost:11434
REDIS_URL=redis://127.0.0.1:6379/0
MYSQL_HOST=localhost
PGVECTOR_HOST=localhost
PGVECTOR_PORT=15432
```

```bash
docker compose up -d pgvector redis
ollama list
.venv/bin/python src/run_api.py
```

### API in Docker on macOS

Keep the Mac values above for direct Python use and add the Docker-specific
addresses below. Compose injects these only into the API container:

```dotenv
DOCKER_OLLAMA_BASE_URL=http://host.docker.internal:11434
DOCKER_REDIS_URL=redis://redis:6379/0
DOCKER_MYSQL_HOST=host.docker.internal
```

```bash
docker compose build api
docker compose up -d pgvector redis
docker compose up -d --no-deps --force-recreate api
```

Wait for readiness without piping a failed `curl` into `jq`:

```bash
until curl -fsS --max-time 5 -o /tmp/local-search-ready.json \
  http://127.0.0.1:8000/api/v1/ready; do sleep 3; done
jq . /tmp/local-search-ready.json
```

Stop the Docker API before returning to the direct Python API:

```bash
docker compose stop api
.venv/bin/python src/run_api.py
```

Neither mode rebuilds embeddings. Run `ollama pull embeddinggemma:latest`
only when `ollama list` reports that the model is missing.

## Validate source access

This command is read-only and does not generate embeddings:

```bash
.venv/bin/python src/ingest.py \
  --company "$COMPANY_ID" \
  --database \
  --check \
  --limit 10
```

## Build or resume indexes

Incremental ingestion safely skips unchanged vectors:

```bash
.venv/bin/python src/ingest.py \
  --company "$COMPANY_ID" \
  --database
```

After a complete source scan, reconcile source deletions:

```bash
.venv/bin/python src/ingest.py \
  --company "$COMPANY_ID" \
  --database \
  --mysql-reconcile-deletions
```

Do not run either ingestion command merely because API code changed. A normal
code change needs only an API rebuild/restart; existing pgvector and BM25 data
remain valid.

## Verify

```bash
.venv/bin/python src/ingest.py --company "$COMPANY_ID" --list
.venv/bin/python scripts/doctor.py --company "$COMPANY_ID" --strict
.venv/bin/pytest -q
```

Run a one-shot search:

```bash
.venv/bin/python src/chat.py \
  --company "$COMPANY_ID" \
  --query "example product query" \
  --limit 10
```

## Evaluate changes

```bash
.venv/bin/python src/evaluate_queries.py --company "$COMPANY_ID"
.venv/bin/python src/evaluate_retrieval.py --company "$COMPANY_ID"
```

Use a reviewed tenant-specific case file when changing embeddings, candidate windows, reranking models, or ranking policy. Compare pass rate, mean reciprocal rank, wall time, and peak memory before adopting the change.
