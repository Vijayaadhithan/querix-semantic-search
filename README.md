# Semantic Advertisement Search

Production-oriented hybrid search for rental, advertisement, and ecommerce
catalogs. The service combines structured filtering, semantic retrieval,
keyword retrieval, reranking, canonical database hydration, tenant isolation,
usage accounting, and compatibility endpoints for an existing Gainr frontend.

The system is intended for both explicit catalogue searches:

```text
bike in Chennai under 1000 per day
```

and need-based searches where the user does not know the product name:

```text
portable equipment for recording a distant wedding
```

## Documentation

| Document | Purpose |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | System design, data boundaries, algorithms, routing, security, scaling, and tradeoffs |
| [`docs/production_search_operations.md`](docs/production_search_operations.md) | Complete installation, ingestion, command, cURL, deployment, monitoring, and troubleshooting runbook |
| [`docs/hackathon_technical_guide.tex`](docs/hackathon_technical_guide.tex) | Standalone judge-facing LaTeX handbook with demo narrative and technical Q&A |

The operations runbook is the command source of truth. This README provides the
shortest safe path to understand and run the project.

## Capabilities

- Tenant-isolated MySQL or PostgreSQL source and result databases.
- Tenant-isolated Chroma or PostgreSQL/pgvector vector storage.
- Persistent SQLite FTS5/BM25 keyword and structured-filter index.
- Local Ollama embeddings using `embeddinggemma:latest`.
- Hosted schema-constrained query planning with ordered Gemini/Gemma fallback.
- A conservative deterministic path that avoids model calls for fully
  structured catalogue queries.
- Parallel vector and BM25 retrieval followed by Reciprocal Rank Fusion.
- Hosted reranking through Jina, Voyage 2.5, and Voyage 2.5 Lite fallback.
- IDs-only Redis result caching with fresh canonical row hydration.
- Cursor pagination for the generic API and page pagination for Gainr's
  compatibility API.
- Per-company API keys, rate limits, public-field allowlists, and usage
  accounting.
- Privacy-preserving stage logs and protected operational status endpoints.

## Architecture

```text
Client
  |
  |  /api/v1/{company}/search + X-API-Key
  v
FastAPI tenant gateway
  |
  +-- authenticate key and bind endpoint to company
  +-- validate/map request and apply tenant rate limit
  |
  v
IDs-only Redis result cache
  |
  +-- hit --> fetch current canonical rows --> return page
  |
  +-- miss
       |
       v
    Query router
       |
       +-- deterministic_filter
       |     +-- structured BM25 catalogue lookup
       |     +-- current type/visibility and canonical DB hydration
       |
       +-- semantic
             +-- hosted structured query plan
             +-- Ollama query embedding
             +-- vector retrieval || BM25 retrieval
             +-- Reciprocal Rank Fusion
             +-- current offer/wanted validation
             +-- Jina -> Voyage 2.5 -> Voyage 2.5 Lite rerank
             +-- tenant relevance cutoff
             +-- optional related filtered tail
             +-- canonical DB hydration
```

Searchable indexes contain retrieval text, metadata, IDs, and embeddings.
Returned advertisements always come from the configured company database. A
display-only database change therefore appears immediately without waiting for
re-embedding.

## Query Routing

| Query | Path | Expensive model calls |
|---|---|---|
| `bike` | deterministic filter/browse | none |
| `bike in Chennai under 1000` | deterministic filter/browse | none |
| `1000 rent car` | deterministic filter/browse | none |
| `red bike with ABS` | semantic hybrid search | planner, embedding, reranker |
| `vehicle for recreational driving on rough terrain` | semantic hybrid search | planner, embedding, reranker |
| repeated identical query within cache TTL | result cache | none |

Deterministic routing is compositional, not a whitelist of complete queries.
It combines indexed categories and locations with duration, budget, sort, and
offer/wanted rules. Ambiguous, descriptive, or functional language remains on
the semantic path.

Explicit category/location/price/duration values become hard filters. A
category inferred only from the described function is a soft ranking hint, so
the system preserves recall instead of eliminating plausible alternatives.
Each tenant can independently configure reranker score floors and whether
semantic results may include an unscored related catalogue tail. Gainr uses
strict score pruning and permits the tail only when an explicit category or
subcategory constrains every appended result.

## Technology

| Concern | Implementation |
|---|---|
| API | FastAPI + Uvicorn |
| Canonical data | MySQL or PostgreSQL |
| Vector retrieval | Chroma or pgvector |
| Lexical retrieval | SQLite FTS5/BM25 |
| Embeddings | Ollama `embeddinggemma:latest` |
| Query planning | Gemini API fallback chain with JSON schema |
| Fusion | Weighted Reciprocal Rank Fusion |
| Reranking | Jina, Voyage 2.5, Voyage 2.5 Lite; optional local transformer |
| Shared state | Redis |
| Evaluation | Pytest plus labelled query-plan and retrieval cases |

## Repository Layout

```text
config.yaml                         shared runtime defaults
configs/tenants/                    isolated company profiles
docs/                               architecture, operations, and hackathon guide
eval/                               labelled planner and retrieval cases
scripts/                            bootstrap, doctor, API-key, model, load tools
src/api.py                          FastAPI gateway and pagination
src/search_engine.py                route, retrieve, rerank, hydrate, cache
src/query_planner.py                deterministic grammar and plan validation
src/gemini_client.py                hosted structured-query fallback
src/ollama_client.py                local embedding provider
src/retrieval.py                    vector/BM25 retrieval and RRF
src/reranker.py                     hosted/local reranker chain
src/tenant_config.py                tenant validation and isolation
src/ingest.py                       file and database ingestion CLI
tests/                              unit and integration-style tests
```

## Prerequisites

- macOS or Ubuntu
- Python 3 with `venv`
- Redis
- Ollama with `embeddinggemma:latest`
- MySQL or PostgreSQL containing the configured search-ready and result tables
- Gemini API key for semantic query planning
- Jina and/or Voyage API key for hosted reranking

OpenSearch is not required. pgvector is required only for tenants configured
with `storage.vector_backend: pgvector`.

## Quick Start

Run commands from the repository root.

### 1. Install

macOS:

```bash
./scripts/bootstrap_macos.sh
```

Ubuntu:

```bash
./scripts/bootstrap_ubuntu.sh
```

Ubuntu with pgvector support:

```bash
INSTALL_PGVECTOR=1 ./scripts/bootstrap_ubuntu.sh
```

Manual Python setup:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
ollama pull embeddinggemma:latest
```

### 2. Configure

Keep secrets only in `.env` or a production secret manager:

```env
GEMINI_API_KEY=<secret>
JINA_API_KEY=<secret>
VOYAGE_API_KEY=<secret>

MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DATABASE=<database>
MYSQL_USER=<read-only-user>
MYSQL_PASSWORD=<secret>

REDIS_URL=redis://127.0.0.1:6379/0
GAINR_API_KEY=<company-api-key>
API_AUTH_ENABLED=true
API_ADMIN_KEY=<separate-monitoring-key>
```

Review `configs/tenants/gainr.yaml` before ingestion. Company profiles define
the database contract, storage paths, endpoint slug, API-key environment
variables, request mapping, public response fields, and rate policy.

### 3. Validate Dependencies and Source

```bash
redis-cli ping
ollama list
.venv/bin/python scripts/doctor.py --company gainr
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql \
  --check \
  --limit 10
```

The final command is read-only: it validates the configured source table and
columns without writing indexes or changing the company database.

### 4. Build Isolated Indexes

```bash
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql \
  --mysql-batch-size 500 \
  --embed-batch-size 32

.venv/bin/python src/ingest.py --company gainr --list
.venv/bin/python scripts/doctor.py --company gainr --strict
```

Ingestion reads the company database and writes only the configured vector and
BM25 indexes. It never updates or deletes company database rows.
The `--list` command is also read-only and aggregates Chroma's metadata SQLite
tables directly, so it does not load the HNSW index or all vector metadata into
process memory.

### 5. Start the API

```bash
.venv/bin/python src/run_api.py
```

The service binds to `API_HOST:API_PORT` and intentionally starts one Uvicorn
worker for the default single-host Chroma/provider-rate-limit deployment.

### 6. Verify

```bash
curl http://127.0.0.1:8000/api/v1/ready

curl http://127.0.0.1:8000/api/v1/gainr/auth/verify \
  -H 'X-API-Key: <GAINR_API_KEY>'

curl http://127.0.0.1:8000/api/v1/gainr/health \
  -H 'X-API-Key: <GAINR_API_KEY>'
```

### 7. Search

Deterministic:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/gainr/search \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <GAINR_API_KEY>' \
  -d '{"query":"bike in Chennai under 1000","page_size":20}'
```

Semantic:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/gainr/search \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <GAINR_API_KEY>' \
  -d '{"query":"portable equipment for recording a distant wedding","page_size":20}'
```

Next page:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/gainr/search \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <GAINR_API_KEY>' \
  -d '{"cursor":"<NEXT_CURSOR>","page_size":20}'
```

The request must contain exactly one of `query` or `cursor`. The maximum page
size is 20 by default. Cursors are company-bound and expire.

## Response Contract

The generic company search response includes:

```json
{
  "company_id": "gainr",
  "search_id": "<opaque-id>",
  "query": "bike in Chennai under 1000",
  "cached": false,
  "items": [],
  "interpreted_query": {
    "execution_path": "deterministic_filter",
    "result_cache_hit": false,
    "reranker_provider": "none"
  },
  "applied_filters": {},
  "unresolved_filters": {},
  "timings_ms": {},
  "usage": {
    "model_requests": 0,
    "total_tokens": 0
  },
  "pagination": {
    "page_size": 20,
    "returned": 0,
    "offset": 0,
    "total_results": 0,
    "has_more": false,
    "next_cursor": null
  }
}
```

The exact public fields inside `items` are allowlisted per tenant. Internal
embedding text, secret metadata, and non-public database columns are not
returned.

## Gainr Frontend Compatibility

The Gainr tenant also exposes its existing frontend contracts:

- `POST /api/v1/gainr/search-suggestions`
- `POST /api/v1/gainr/filter-data`
- `POST /api/v1/gainr/filter-result`
- `GET /api/v1/gainr/recent-search`

Set:

```env
VITE_SEARCH_API_BASE_URL=https://your-api-domain.com/api/v1/gainr
```

All payloads, cURL examples, pagination behavior, and frontend call sequencing
are documented in
[`docs/production_search_operations.md`](docs/production_search_operations.md).

## Ingestion Modes

| Command option | Effect |
|---|---|
| `--mysql --check --limit 10` | Read-only source validation |
| `--mysql` | Incremental vector and BM25 upsert |
| `--mysql --mysql-reconcile-deletions` | Full scan plus safe stale-index removal |
| `--mysql-bm25-only` | Rebuild BM25 without embeddings |
| `--mysql --mysql-force-reembed` | Re-embed even unchanged content |
| `--mysql --mysql-replace-source` | Clear/rebuild this tenant's indexes |
| `--list` | Show indexed sources/counts |
| `--delete <source>` | Delete one local-file source |
| `--clear` | Clear the selected vector collection |

`--mysql` is retained as a compatibility flag and also dispatches to
PostgreSQL when the selected tenant profile uses PostgreSQL.

## Caching

Two caches have different contracts:

1. Query-plan cache: stores normalized structured plans and avoids repeated
   hosted planning.
2. Result cache: stores only ordered product IDs, result tiers, and interpreted
   filter metadata.

Result-cache hits still fetch current canonical rows. Keys include tenant,
query, ranking configuration, and BM25 revision metadata, so ingestion changes
invalidate stale ranking orders without caching full database records.

## Observability

`API_LOG_LEVEL=info` emits a privacy-preserving timeline under one trace ID:

```text
plan -> retrieve -> rerank -> related_tail -> database_map -> complete
```

Logs include model/provider choices, fallback reasons, counts, and timings but
omit raw query text by default.

Protected monitor endpoints:

```bash
curl http://127.0.0.1:8000/api/v1/admin/status \
  -H 'X-Admin-Key: <API_ADMIN_KEY>'

curl 'http://127.0.0.1:8000/api/v1/gainr/admin/search-events?limit=20' \
  -H 'X-Admin-Key: <API_ADMIN_KEY>'
```

The admin key is separate from company API keys. Admin endpoints are disabled
when `API_ADMIN_KEY` is empty.

## Testing and Evaluation

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src scripts tests
.venv/bin/python src/evaluate_queries.py --company gainr
.venv/bin/python src/evaluate_retrieval.py --company gainr
.venv/bin/python scripts/doctor.py --company gainr --strict
```

The local unit/integration-style suite verified on 2026-07-03 contains 137
passing tests. That count is a code regression baseline, not a production
relevance or latency claim. The labelled relevance sets are intentionally
small and must be expanded with real search traffic and adjudicated examples.

Run a representative concurrent test:

```bash
.venv/bin/python scripts/load_test.py \
  --company gainr \
  --requests 20 \
  --concurrency 2 \
  --query "bike in Chennai under 1000" \
  --query "portable equipment for recording a distant wedding"
```

## Security and Production Boundaries

- Enable `API_AUTH_ENABLED=true` outside local development.
- Use a unique API key, endpoint slug, database, vector namespace, BM25 path,
  Redis namespace, and cursor scope per company.
- Keep provider, company, database, and admin keys in a secret manager.
- Use `tls.mode: verify-full` for remote production databases.
- Use a read-only database account for source and result tables.
- Terminate HTTPS at a reverse proxy; never expose permanent company secrets in
  a public browser bundle.
- Derive `X-User-ID` from a trusted signed-in session, never arbitrary browser
  input.
- Treat explicit UI filters as hard constraints; do not allow the planner to
  override them.
- Validate backup, restore, alerting, provider quotas, and load behavior before
  increasing concurrency.

## Known Limitations

- Full replacement ingestion is not yet an atomic generation swap; use a
  maintenance window.
- Ingestion is operator-run CLI work rather than a durable asynchronous job.
- Geographic aliases and typo rules are conservative, not a complete knowledge
  base.
- Result windows are intentionally bounded; broad catalogue browsing uses
  filters and pagination, not unbounded semantic top-K retrieval.
- PostgreSQL/pgvector adapters require validation against the target production
  version, permissions, backup policy, and embedding dimension.
- Business visibility semantics beyond configured soft-delete behavior must be
  confirmed with each tenant.

## License

See [`LICENSE`](LICENSE).
