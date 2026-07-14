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

```bash
docker compose up -d pgvector redis
ollama pull embeddinggemma:latest
```

If Ollama should also run in Docker:

```bash
docker compose --profile ollama up -d ollama
docker compose exec ollama ollama pull embeddinggemma:latest
```

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
