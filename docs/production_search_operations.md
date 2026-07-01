# Multi-Company Search API: Operations and Command Runbook

This document is the command-focused source of truth for installing, configuring,
ingesting, refreshing, running, testing, and troubleshooting the search API.
It describes the currently implemented Gainr deployment and the tenant-isolation
contract used when additional companies are onboarded.

## 1. The most important data-flow distinction

The deterministic path does **not** execute its category/location/price filtering
query directly against the company's live relational database.

For each authenticated company:

| Stage | Deterministic search | Semantic search |
|---|---|---|
| Interpret query | Local deterministic rules | Rules, then hosted query planner when required |
| Candidate lookup | Company's local BM25/SQLite index | Company's Chroma vector collection and BM25 index in parallel |
| Offer/wanted validation | Current type values fetched from company's configured DB | Current type values fetched from company's configured DB |
| Ranking | Stable filtered ordering | Reciprocal-rank fusion, Jina/Voyage reranking, then related tail |
| Final returned rows | Fetched from company's configured result table | Fetched from company's configured result table |

Therefore:

- Chroma and BM25 hold searchable text, metadata, IDs, and embeddings.
- The company database remains the canonical source for the returned product
  rows and current offer/wanted type.
- In local testing, the configured company database happens to be the local
  MySQL database.
- In production, the same tenant profile can point to the company's remote
  database or to a read-only serving replica owned for that company.
- A result-cache hit stores only ordered IDs and interpretation metadata; it
  still fetches current rows from the company database.

This design avoids returning stale descriptions/photos after a display-only
database update, while keeping expensive retrieval work out of the relational
database.

## 2. Implemented architecture

```text
Company client
    |
    |  /api/v1/{company}/search + X-API-Key
    v
FastAPI tenant gateway
    |
    +-- resolve API key -> company profile
    +-- verify endpoint slug belongs to the same company
    +-- normalize company-specific request fields
    +-- apply company rate limit
    |
    v
Redis result-ID cache
    |
    +-- hit  -> fetch current canonical rows from company DB -> page response
    |
    +-- miss
          |
          v
      Query routing
          |
          +-- deterministic_filter
          |      |
          |      +-- company BM25 structured browse/filter
          |      +-- fetch current type and final rows from company DB
          |
          +-- semantic
                 |
                 +-- vector search in company Chroma collection
                 +-- keyword search in company BM25 index
                 +-- reciprocal-rank fusion
                 +-- current offer/wanted validation from company DB
                 +-- Jina -> Voyage 2.5 -> Voyage 2.5 Lite reranking
                 +-- related filtered tail
                 +-- fetch final canonical rows from company DB
```

### Current Gainr storage

```text
configs/tenants/gainr.yaml
storage/companies/gainr/chroma/
storage/companies/gainr/bm25.sqlite3
storage/usage.sqlite3
```

The completed Gainr indexes contain 250,117 Chroma vectors and 250,117 BM25
products.

## 3. Query-routing behavior

### 3.1 Deterministic fast path

The deterministic path is selected only when every meaningful part of a query
can be safely translated into supported indexed filters. Examples:

```text
bike
bike in Chennai
bike in Chennai under 1000
car for rent per day
people looking for generators
```

Supported canonical concepts currently include:

- main category
- subcategory
- state
- city
- locality
- rental duration
- minimum/maximum rental fee
- offer versus wanted intent
- supported price ordering

The path:

1. Uses deterministic rules and the company's BM25 filter-value catalogue.
2. Does not call the hosted query LLM.
3. Does not generate a query embedding.
4. Does not call Chroma vector search.
5. Does not call Jina or Voyage.
6. Finds candidate product IDs in the company's BM25 SQLite index.
7. Reads current `type` values and final product rows from the company's
   configured database connection.

The expected response contains:

```json
{
  "interpreted_query": {
    "execution_path": "deterministic_filter",
    "result_cache_hit": false,
    "reranker_provider": "none"
  },
  "usage": {
    "model_requests": 0,
    "total_tokens": 0
  }
}
```

### 3.2 Semantic path

The semantic path is used when descriptive or functional meaning remains after
safe deterministic extraction. Examples:

```text
portable equipment for recording a distant wedding
something to keep food cold during an outdoor event
equipment for lifting material to a second floor
```

The path:

1. Checks the normalized query-plan cache.
2. Runs the hosted Google query-planner fallback chain when needed.
3. Runs local Ollama `embeddinggemma:latest` query embedding.
4. Runs Chroma vector retrieval and BM25 lexical retrieval concurrently.
5. Fuses the two ranked lists using reciprocal-rank fusion.
6. Validates offer/wanted intent using current values from the company DB.
7. Sends the bounded candidate text to:
   `Jina -> Voyage rerank-2.5 -> Voyage rerank-2.5-lite`.
8. Appends a stable related filtered tail when applicable.
9. Fetches current canonical rows from the company's result table.
10. Stores ordered IDs, tiers, and interpretation metadata in Redis for five
    minutes.

### 3.3 Result-cache path

For an identical normalized query after a successful non-fallback search:

1. Redis returns ordered product IDs and the interpreted plan.
2. Planning, embedding, vector search, BM25 search, fusion, and reranking are
   skipped.
3. Current rows are fetched again from the company database.

The cache key includes:

- company ID
- normalized query
- BM25 revision and count
- result-window limit
- reranker model and ranking-window settings

Any ingestion change increments the BM25 revision and invalidates old ranking
keys automatically.

## 4. Multi-company isolation

Every tenant profile owns:

- a unique company ID
- a unique endpoint slug
- one or more unique API keys
- its own MySQL/PostgreSQL connection
- its own Chroma directory and collection, or pgvector table
- its own BM25 SQLite file
- its own Redis namespaces
- its own cursor sessions
- its own rate policy
- its own public response-field mapping
- its own request-field mapping
- its own usage accounting

Startup fails if two profiles share an endpoint slug, API key, Chroma
collection, pgvector table, or BM25 file.

Use a separate Chroma directory per company:

```yaml
storage:
  vector_backend: chroma
  chroma_dir: storage/companies/acme/chroma
  collection_name: company_acme
  bm25_path: storage/companies/acme/bm25.sqlite3
```

## 5. Requirements

### 5.1 Testing server

For one company and one API worker:

```text
2 vCPU
8 GB RAM
100 GB NVMe
Ubuntu
```

Recommended test settings:

```env
API_TENANT_ENGINE_CACHE_SIZE=1
API_TENANT_MAX_CONCURRENT_SEARCHES=2
```

### 5.2 Required services

- Python
- MySQL or PostgreSQL company database
- Redis
- Ollama
- `embeddinggemma:latest`
- system build libraries installed by the bootstrap script

OpenSearch is not required.

## 6. Installation

### 6.1 Ubuntu one-shot installation

```bash
cd /path/to/Peronsal_rag
chmod +x scripts/bootstrap_ubuntu.sh
./scripts/bootstrap_ubuntu.sh
```

Only when a tenant uses PostgreSQL/pgvector:

```bash
INSTALL_PGVECTOR=1 ./scripts/bootstrap_ubuntu.sh
```

### 6.2 macOS installation

```bash
cd /path/to/Peronsal_rag
chmod +x scripts/bootstrap_macos.sh
./scripts/bootstrap_macos.sh
```

### 6.3 Manual Python installation

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-dev.txt
```

### 6.4 Verify local dependencies

```bash
redis-cli ping
ollama list
ollama pull embeddinggemma:latest
```

Expected Redis response:

```text
PONG
```

## 7. Environment configuration

Create the local file only if it does not already exist:

```bash
cp .env.example .env
```

Required groups:

```env
# Company database
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DATABASE=<database>
MYSQL_USER=<read-only-user>
MYSQL_PASSWORD=<secret>

# Company API authentication
API_AUTH_ENABLED=true
GAINR_API_KEY=<generated-company-key>

# Shared state
REDIS_ENABLED=true
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_RESULT_CACHE_ENABLED=true
REDIS_RESULT_CACHE_TTL_SECONDS=300

# Hosted query planning and reranking
GEMINI_API_KEY=<secret>
JINA_API_KEY=<secret>
VOYAGE_API_KEY=<secret>

# 8 GB one-company testing limits
API_TENANT_ENGINE_CACHE_SIZE=1
API_TENANT_MAX_CONCURRENT_SEARCHES=2
```

Never commit `.env`. Rotate credentials that have appeared in chat, terminal
history, screenshots, or logs before public deployment.

## 8. Per-company database safety configuration

Gainr local testing:

```yaml
database:
  backend: mysql
  timeouts:
    connect_seconds: 10
    read_seconds: 300
    write_seconds: 300
    statement_timeout_ms: 0
  pool:
    min_size: 0
    max_size: 2
    timeout_seconds: 5
  tls:
    mode: disable
    ca_file_env: GAINR_DB_TLS_CA_FILE
    cert_file_env: GAINR_DB_TLS_CERT_FILE
    key_file_env: GAINR_DB_TLS_KEY_FILE
```

Remote production PostgreSQL example:

```yaml
database:
  backend: postgres
  timeouts:
    connect_seconds: 10
    read_seconds: 30
    write_seconds: 30
    statement_timeout_ms: 15000
  pool:
    min_size: 1
    max_size: 4
    timeout_seconds: 5
  tls:
    mode: verify-full
    ca_file_env: ACME_POSTGRES_TLS_CA_FILE
    cert_file_env: ACME_POSTGRES_TLS_CERT_FILE
    key_file_env: ACME_POSTGRES_TLS_KEY_FILE
```

Certificate paths are provided through environment variables. Certificate
contents must be mounted from the server's secret-management mechanism.

Pool limits are per active company. Do not configure a pool larger than the
company's useful API concurrency.

## 9. Gainr source preparation

RAG_HT reads and preprocesses the source company data, validates it, and
atomically publishes `ads_search_ready`.

### 9.1 First RAG_HT publish

```bash
cd /path/to/RAG_HT
./scripts/setup.sh
./scripts/run_scheduled_etl.sh gainr --publish
```

### 9.2 Routine RAG_HT refresh

```bash
cd /path/to/RAG_HT
./scripts/run_scheduled_etl.sh gainr --publish
```

The scheduled wrapper prevents overlapping Gainr ETL runs and selects
incremental processing when a valid baseline exists.

## 10. Validate and ingest Gainr

Return to this repository:

```bash
cd /path/to/Peronsal_rag
```

### 10.1 Read-only source validation

```bash
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql \
  --check \
  --limit 10
```

This checks the configured connection, table, content columns, primary key, and
planned row count. It does not write to the company database or local indexes.

### 10.2 First/full incremental ingestion

```bash
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql \
  --mysql-batch-size 500 \
  --embed-batch-size 32
```

The command:

- reads the company's configured `search_ready` table
- upserts BM25 rows
- compares stable content hashes
- embeds only changed/new documents
- writes only Gainr's Chroma collection and BM25 file
- never updates or deletes company DB rows

### 10.3 Routine refresh with deletion reconciliation

```bash
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql \
  --mysql-reconcile-deletions \
  --mysql-batch-size 500 \
  --embed-batch-size 32
```

Deletion reconciliation:

- is permitted only after a successful full source scan
- is rejected when `--limit` is supplied
- removes vector/BM25 IDs no longer present in the source
- leaves unchanged vectors untouched
- invalidates old result-cache ranking keys through the BM25 revision

### 10.4 BM25-only rebuild

```bash
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql-bm25-only \
  --mysql-batch-size 5000
```

### 10.5 Force re-embedding

Use only after changing the embedding model or embedding document contract:

```bash
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql \
  --mysql-force-reembed
```

### 10.6 Authoritative replacement

Use during a maintenance window:

```bash
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql \
  --mysql-replace-source
```

This clears and rebuilds only Gainr's configured vector/BM25 source. It does
not delete company database rows. Replacement is not yet an atomic generation
swap, so normal refreshes should prefer incremental reconciliation.

### 10.7 List and validate indexes

```bash
.venv/bin/python src/ingest.py --company gainr --list
.venv/bin/python scripts/doctor.py --company gainr --strict
```

The doctor verifies:

- company API key
- Gemini key
- Ollama model
- Redis
- reranker chain
- company database
- vector index
- BM25 index

## 11. API key generation

```bash
.venv/bin/python scripts/generate_company_api_key.py --company gainr
```

Store the generated value in a secret manager and set it as `GAINR_API_KEY`.
Do not use the Gemini, Jina, or Voyage provider keys as customer credentials.

Restart the API after adding or rotating a key.

## 12. Start and verify the API

### 12.1 Start

```bash
.venv/bin/python src/run_api.py
```

The project intentionally starts one Uvicorn worker for the single-host
Chroma deployment and shared provider-rate limits.

### 12.2 Process readiness

```bash
curl http://127.0.0.1:8000/api/v1/ready
```

### 12.3 Authentication verification

Load local environment values without printing the key:

```bash
set -a
source .env
set +a
```

```bash
curl http://127.0.0.1:8000/api/v1/gainr/auth/verify \
  -H "X-API-Key: $GAINR_API_KEY"
```

### 12.4 Company health

```bash
curl http://127.0.0.1:8000/api/v1/gainr/health \
  -H "X-API-Key: $GAINR_API_KEY"
```

## 13. Search tests

### 13.1 Deterministic search

```bash
curl -X POST http://127.0.0.1:8000/api/v1/gainr/search \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $GAINR_API_KEY" \
  -d '{"query":"bike in Chennai under 1000","page_size":20}'
```

Check:

```text
interpreted_query.execution_path = deterministic_filter
usage.model_requests = 0
usage.total_tokens = 0
interpreted_query.reranker_provider = none
```

### 13.2 Semantic search

```bash
curl -X POST http://127.0.0.1:8000/api/v1/gainr/search \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $GAINR_API_KEY" \
  -d '{"query":"portable equipment for recording a distant wedding","page_size":20}'
```

Check:

```text
interpreted_query.execution_path = semantic
timings_ms.vector_search > 0
usage contains provider/model attempts when not cached
```

### 13.3 Result-cache test

Run the exact semantic request twice within five minutes.

First response:

```text
interpreted_query.result_cache_hit = false
```

Second response:

```text
cached = true
interpreted_query.result_cache_hit = true
```

The server log contains:

```text
step=result_cache status=hit
```

### 13.4 Pagination

Copy `pagination.next_cursor` from the first response:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/gainr/search \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $GAINR_API_KEY" \
  -d '{"cursor":"<NEXT_CURSOR>","page_size":20}'
```

The cursor is bound to the company that created it.

### 13.5 Monthly usage

```bash
MONTH=$(date -u +%Y-%m)
curl "http://127.0.0.1:8000/api/v1/gainr/usage?month=${MONTH}" \
  -H "X-API-Key: $GAINR_API_KEY"
```

Usage is grouped by company, month, provider, model, operation, and status.

### 13.6 Gainr frontend-compatible endpoints

Gainr can keep its four existing frontend service functions by configuring:

```env
VITE_SEARCH_API_BASE_URL=https://your-api-domain.com/api/v1/gainr
```

Test all four contracts:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/gainr/search-suggestions \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $GAINR_API_KEY" \
  -d '{"term":"bike"}'

curl -X POST http://127.0.0.1:8000/api/v1/gainr/filter-data \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $GAINR_API_KEY" \
  -d '{"city_id":456}'

curl -X POST http://127.0.0.1:8000/api/v1/gainr/filter-result \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $GAINR_API_KEY" \
  -H 'X-User-ID: local-test-user' \
  -d '{
    "searchTerm":"Bike",
    "filter":{
      "city_id":456,
      "subcategory_id":"",
      "locality_id":[],
      "rental_duration":[],
      "ad_type":[1],
      "fee":[],
      "min_fee":null,
      "max_fee":null
    },
    "page":1
  }'

curl http://127.0.0.1:8000/api/v1/gainr/recent-search \
  -H "X-API-Key: $GAINR_API_KEY" \
  -H 'X-User-ID: local-test-user'
```

The `filter-result` response keeps Gainr's `status`, `message`, `data`,
`current_page`, and `last_page` fields. `search_meta` additionally reports the
chosen route, automatic filters, explicit filters, effective filters, ignored
automatic filters, result-window status, and usage.

Rules:

- explicit checkbox/range filters are hard constraints and override matching
  query-derived filters;
- automatic query filters fill only missing fields;
- values inside one multi-select use OR, while different filter groups use AND;
- deterministic catalogue queries use complete page-number pagination;
- semantic queries use the configured bounded ranked window;
- recent searches are isolated by company and verified Gainr user ID;
- `type` and `is_rent_negotiable` are present in `ads_search_ready` and in the
  rebuilt Gainr BM25 index;
- final card hydration checks the current `ads` row, so a changed ad type or
  negotiability value does not wait for the next vector refresh.

Existing installations must rebuild only the Gainr BM25 index once so its new
numeric ID columns are populated:

```bash
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql-bm25-only \
  --mysql-batch-size 5000
```

This command does not call the embedding model and does not modify Chroma.

### 13.7 Compatibility and regression verification

Verify that enabling the frontend-compatible adapter did not change Gainr's
existing generic company endpoint:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/gainr/search \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $GAINR_API_KEY" \
  -d '{"query":"bike in Mumbai under 1000","page_size":5}'
```

Run the complete local verification:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/doctor.py --company gainr --strict
git diff --check
```

Current verified local state:

```text
ads_search_ready rows:   250117
ads rows:                250117
Chroma vectors:          250117
BM25 products:           250117
tests:                   123 passed
strict doctor:           8 passed, 0 failed
```

The compatibility routes are created only when a tenant profile explicitly
sets:

```yaml
compatibility:
  adapter: gainr_legacy
```

Profiles without that setting retain their existing configured endpoint,
request mapping, response fields, database, vector collection, BM25 file,
API keys and rate limits.

## 14. Concurrent load testing

Repeated semantic/cache test:

```bash
.venv/bin/python scripts/load_test.py \
  --company gainr \
  --requests 20 \
  --concurrency 2 \
  --query "portable camera"
```

Mixed traffic:

```bash
.venv/bin/python scripts/load_test.py \
  --company gainr \
  --requests 20 \
  --concurrency 2 \
  --query "bike in Chennai under 1000" \
  --query "portable equipment for recording a distant wedding"
```

Different API port:

```bash
.venv/bin/python scripts/load_test.py \
  --company gainr \
  --base-url http://127.0.0.1:8001 \
  --requests 20 \
  --concurrency 2
```

The current verified local test reported:

```text
requests: 20
concurrency: 2
statuses: 20 x HTTP 200
result-cache hits: 18
cached latency: approximately 190-210 ms
initial uncached semantic latency: approximately 3.6-4.0 seconds
```

Do not interpret a cached-query test as full semantic throughput. Production
load testing should use a representative set of unique deterministic, semantic,
cached, and provider-fallback queries.

## 15. Automated tests

### 15.1 Complete unit/integration-style suite

```bash
.venv/bin/python -m pytest -q
```

### 15.2 Compile/import check

```bash
python3 -m compileall -q src scripts tests
```

### 15.3 Query-plan evaluation

```bash
.venv/bin/python src/evaluate_queries.py --company gainr
```

### 15.4 Retrieval evaluation

```bash
.venv/bin/python src/evaluate_retrieval.py --company gainr
```

### 15.5 Strict environment doctor

```bash
.venv/bin/python scripts/doctor.py --company gainr --strict
```

### 15.6 Build the LaTeX hackathon technical guide

On a machine with a TeX distribution:

```bash
mkdir -p build/docs
pdflatex -interaction=nonstopmode -halt-on-error \
  -output-directory build/docs \
  docs/hackathon_technical_guide.tex
pdflatex -interaction=nonstopmode -halt-on-error \
  -output-directory build/docs \
  docs/hackathon_technical_guide.tex
```

The second pass resolves the table of contents and internal references.

## 16. Add another company

### 16.1 Create the profile

```bash
cp configs/tenants/example-company.yaml.example \
  configs/tenants/acme.yaml
```

Make the filename and `company.id` identical.

Configure:

- database backend and credential environment-variable names
- TLS mode and certificate environment-variable names
- timeouts and pool size
- canonical search-ready table/columns
- final result table/columns
- unique Chroma directory/collection or pgvector table
- unique BM25 path
- unique endpoint slug
- unique API-key environment-variable name
- request-field mapping
- public response fields
- filter schema
- rate policy
- planner prompt context

### 16.2 Same-domain company

An equipment/rental marketplace can use:

```yaml
company:
  planner_adapter: gainr
```

Its RAG_HT adapter must map physical source fields into the canonical Gainr
filter contract.

### 16.3 Different-domain company

Before onboarding a materially different domain:

1. Define its search intents.
2. Define its allowed structured filters and field types.
3. Implement a new planner adapter.
4. Add deterministic query cases.
5. Add retrieval relevance cases.
6. Only then enable the company endpoint.

Do not rely on prompt text alone to make arbitrary database fields safe.

## 17. Operational refresh schedule

Example routine:

```bash
cd /path/to/RAG_HT
./scripts/run_scheduled_etl.sh gainr --publish

cd /path/to/Peronsal_rag
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql \
  --mysql-reconcile-deletions

.venv/bin/python scripts/doctor.py --company gainr --strict
```

Restart the API after a refresh when new category/location filter values were
introduced, because the in-memory filter catalogue is built when the tenant
engine opens.

## 18. Troubleshooting

### Ollama `/api/embed` returns HTTP 400 while the CLI works

`ollama run embeddinggemma "startup test"` and the REST request are different:
the application also sends `keep_alive`. Dotenv reads
`OLLAMA_KEEP_ALIVE=-1` as a string, while Ollama expects numeric `-1` or a
duration string such as `-1m`. The current client normalizes integer-looking
environment values before constructing JSON.

For an older deployed checkout, either deploy the current client or use:

```env
OLLAMA_KEEP_ALIVE=-1m
```

Then restart the API/ingestion process and test:

```bash
.venv/bin/python -c '
import sys
sys.path.insert(0, "src")
from ollama_client import embed_texts
result = embed_texts(["startup test"])
print("embeddings:", len(result), "dimensions:", len(result[0]))
'
```

### `401 Missing API key`

Use a named header:

```bash
-H "X-API-Key: $GAINR_API_KEY"
```

### `403 API key does not match the company endpoint`

The API key belongs to a different tenant. Never reuse one key for two company
profiles.

### `404 Unknown company endpoint`

Check:

```yaml
api:
  endpoint_slug: gainr
```

Restart the API after adding a profile.

### `422 Unexpected request fields`

Gainr expects:

```json
{"query":"portable camera","page_size":20}
```

The Acme example maps those fields to:

```json
{"search_text":"portable camera","limit":20}
```

Request mappings are company-specific.

### `503` missing vector or BM25 index

```bash
.venv/bin/python src/ingest.py --company gainr --mysql
.venv/bin/python scripts/doctor.py --company gainr --strict
```

### Repeated semantic query is not hitting the result cache

Check:

- Redis is connected.
- `REDIS_RESULT_CACHE_ENABLED=true`.
- The first search completed without provider fallback.
- The request is within the 300-second TTL.
- Ingestion is not changing the BM25 revision.
- Query text normalizes to the same value.

### Database pool timeout

The company reached its configured `database.pool.max_size`.

For the 8 GB one-company test machine:

```yaml
pool:
  max_size: 2
```

Do not raise the pool independently of useful search concurrency.

### Vector search suddenly takes tens of seconds

Check the server log:

```text
step=retrieve ... vector_ms=...
```

Each company already owns an isolated Chroma collection. Do not add
`company_id` or `source_file` as unconditional Chroma `where` predicates:
doing so can turn a normal ANN lookup into an expensive metadata-filtered
scan. The current implementation adds a Chroma `where` clause only for actual
category, location, duration or price constraints and still validates
tenant/source metadata after retrieval.

Reference measurements on the 250,117-vector local Gainr collection:

```text
unfiltered, 120 candidates: approximately 592 ms
city_id=456, 120 candidates: approximately 937 ms
```

After changing retrieval code, restart the API process; no ingestion or vector
rebuild is needed for this specific optimization.

### Google query-planner timeout/503

The configured model chain advances only for retryable provider failures.
Search can fall back to a conservative semantic plan. Fallback results are not
stored in the result-order cache.

### Reranker failure

The order is:

```text
Jina -> Voyage rerank-2.5 -> Voyage rerank-2.5-lite
```

Inspect the stage logs for provider, HTTP status, fallback reason, and timing.

## 19. Why this architecture

### Advantages

- Hard company-level data boundaries.
- Cheap single-host deployment without OpenSearch.
- Fast structured browse/filter behavior for deterministic queries.
- Semantic understanding when exact terms are insufficient.
- Hybrid retrieval protects against both vocabulary mismatch and exact-keyword
  misses.
- Hosted reranking avoids loading a large local cross-encoder on an 8 GB host.
- Current canonical DB hydration keeps returned rows fresh.
- Incremental hashing prevents unnecessary re-embedding.
- Deletion reconciliation removes stale local search IDs safely after a full
  scan.
- Redis reduces repeated model/API cost while retaining fresh DB rows.

### Tradeoffs

- The final response depends on company DB availability.
- Chroma is a single-host embedded vector store.
- BM25 is SQLite and should not be shared by multiple writer processes.
- Full replacement is not yet an atomic generation swap.
- Cursor sessions and usage SQLite are local to one API host.
- The result window is bounded to 200, not full-catalog keyset pagination.
- Two simultaneous identical uncached queries can duplicate semantic work;
  request coalescing is a future optimization.
- A materially different business domain requires a new planner adapter.

## 20. Production checklist

- [ ] Rotate all exposed API/provider credentials.
- [ ] Use a read-only company DB user for search and ingestion.
- [ ] Enable `verify-full` TLS for remote databases.
- [ ] Mount CA/client certificates through secret management.
- [ ] Configure conservative per-company pool and concurrency limits.
- [ ] Run strict doctor.
- [ ] Run deterministic, semantic, cached, and mixed load tests.
- [ ] Confirm offer/wanted and visibility rules with the company.
- [ ] Schedule RAG_HT publish followed by reconciled index refresh.
- [ ] Monitor Redis, company DB, provider errors, token usage, disk, and RAM.
- [ ] Put the API behind a TLS reverse proxy and system service.
- [ ] Back up company Chroma/BM25 directories and usage data.
- [ ] Keep one API worker for the current embedded Chroma deployment.
- [ ] Implement a new planner adapter before onboarding a different domain.
