# Local pgvector workflow

This is the local order for Gainr after switching vectors from Chroma to
pgvector. BM25 remains the local SQLite lexical index.

## Stop local Docker services

```bash
bash scripts/local_stop_docker.sh
```

This keeps Docker volumes. It does not delete pgvector data.

## Start and check local dependencies

Make sure `.env` and `.env.keys` are configured first.

```bash
bash scripts/local_start_check.sh
```

This starts `pgvector` and `redis`, creates the `vector` extension, checks
Python connectivity to pgvector, pulls `embeddinggemma:latest`, and preloads
the configured local reranker.

## Continue embedding/indexing

Use this when a previous ingestion was interrupted or when source rows changed:

```bash
bash scripts/local_resume_gainr_index.sh resume
```

Resume mode does not clear existing rows. It uses the stored content hashes and
embeds only changed or missing rows.

## Full rebuild

Use this once after changing vector backend, embedding model, or when you want
to force a clean local index:

```bash
bash scripts/local_resume_gainr_index.sh replace
```

## Test API

```bash
.venv/bin/python scripts/doctor.py --company gainr
.venv/bin/python src/evaluate_queries.py --company gainr
.venv/bin/python src/evaluate_retrieval.py --company gainr
PGVECTOR_PORT=15432 .venv/bin/python src/run_api.py
```

In another terminal:

```bash
set -a
source .env
source .env.keys
set +a

curl -sS -X POST http://127.0.0.1:8000/api/v1/gainr/filter-result \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $GAINR_API_KEY" \
  -d '{"searchTerm":"comfortable car for a day in Chennai","filter":{},"page":1}'
```

## Latency test

```bash
.venv/bin/python scripts/load_test.py \
  --company gainr \
  --requests 20 \
  --concurrency 2 \
  --query "comfortable car for a day in Chennai" \
  --query "teacher for home lessons in Chennai for one hour"
```
