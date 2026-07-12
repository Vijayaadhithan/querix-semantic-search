# Multi-Company Search API: Operations and Command Runbook

This document is the command-focused source of truth for installing, configuring,
ingesting, refreshing, running, testing, and troubleshooting the search API.
It describes the currently implemented Gainr deployment and the tenant-isolation
contract used when additional companies are onboarded.

Use this document as the command source of truth. Run repository commands from
the repository root unless a section explicitly changes directory. Replace all
angle-bracket placeholders; never paste a real secret into a committed file.

Quick navigation:

- installation and dependencies: Sections 5--6
- environment and tenant database safety: Sections 7--8
- source publish and index ingestion: Sections 9--10
- API keys, startup, health, search, and cURL: Sections 11--13
- load, regression, evaluation, and LaTeX build: Sections 14--15
- additional companies and scheduled refresh: Sections 16--17
- troubleshooting and production checklist: Sections 18--20

## 1. The most important data-flow distinction

The deterministic path does **not** execute its category/location/price filtering
query directly against the company's live relational database.

For each authenticated company:

| Stage | Deterministic search | Semantic search |
|---|---|---|
| Interpret query | Local deterministic rules | Rules, then hosted query planner when required |
| Candidate lookup | Company's local BM25/SQLite index | Company's vector backend and BM25 index in parallel |
| Offer/wanted validation | Current type values fetched from company's configured DB | Current type values fetched from company's configured DB |
| Ranking | Stable filtered ordering | Reciprocal-rank fusion, local reranking, then related tail |
| Final returned rows | Fetched from company's configured result table | Fetched from company's configured result table |

Therefore:

- pgvector holds searchable vectors and metadata. BM25 holds the separate
  SQLite lexical index. Chroma remains supported for older tenants, but Gainr
  is configured for pgvector.
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
                 +-- vector search in company pgvector table
                 +-- keyword search in company BM25 index
                 +-- reciprocal-rank fusion
                 +-- current offer/wanted validation from company DB
                 +-- local transformer reranking
                 +-- related filtered tail
                 +-- fetch final canonical rows from company DB
```

### Current Gainr storage paths

```text
configs/tenants/gainr.yaml
storage/companies/gainr/chroma/
storage/companies/gainr/bm25.sqlite3
storage/usage.sqlite3
```

Counts are deployment state, not documentation constants. Inspect the current
environment with:

```bash
.venv/bin/python src/ingest.py --company gainr --list
.venv/bin/python scripts/doctor.py --company gainr --strict
```

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
4. Runs pgvector retrieval and BM25 lexical retrieval concurrently.
5. Fuses the two ranked lists using reciprocal-rank fusion.
6. Validates offer/wanted intent using current values from the company DB.
7. Sends the bounded candidate text to the local transformer reranker.
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
- its own planner prompt context
- its own filter schema

Startup fails if two profiles share an endpoint slug, API key, Chroma
collection, pgvector table, or BM25 file.

For a new company, copy `configs/tenants/example-company.yaml.example` to
`configs/tenants/<company>.yaml`. The profile controls
`/api/v1/<endpoint>/...`, frontend request field names, public response fields,
DB table/column names, allowed filters, storage backend, rate limits, and the
company-specific planner prompt. Secrets referenced by `api.key_envs` and
`database.*_env` belong in `.env.keys` or the deployment secret manager.

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

To install Docker, start the bundled pgvector/Redis services, and create the
pgvector extension from the same script, set `.env.keys` first and run:

```bash
START_DOCKER_STACK=1 ./scripts/bootstrap_ubuntu.sh
```

Only when you are using native PostgreSQL instead of the bundled Docker
pgvector service:

```bash
INSTALL_PGVECTOR=1 ./scripts/bootstrap_ubuntu.sh
```

### 6.2 macOS installation

```bash
cd /path/to/Peronsal_rag
chmod +x scripts/bootstrap_macos.sh
./scripts/bootstrap_macos.sh
```

To start the bundled Docker pgvector/Redis services from macOS, install and
start Docker Desktop first, set `.env.keys`, then run:

```bash
START_DOCKER_STACK=1 ./scripts/bootstrap_macos.sh
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

### 6.5 Docker deployment

The repository includes a production-oriented `Dockerfile` and
`docker-compose.yml`. The API container runs as a non-root user, keeps one
Uvicorn worker, mounts `storage/`, and uses Redis as a separate service.

```bash
cd /path/to/Peronsal_rag
cp .env.example .env
docker compose up --build -d redis api
```

If Ollama should run inside Compose:

```bash
docker compose --profile ollama up --build -d
docker compose exec ollama ollama pull embeddinggemma:latest
```

For HTTPS, run Nginx or Caddy in front of the API and keep the application
bound to the private Docker or loopback network.

## 7. Environment configuration

Create the local file only if it does not already exist:

```bash
cp .env.example .env
cp .env.keys.example .env.keys
```

Keep non-secret runtime settings in `.env`:

```env
# Company database host and runtime settings
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DATABASE=<database>

API_AUTH_ENABLED=true
API_CORS_ORIGINS=https://gainr.in,https://www.gainr.in

# Shared state
REDIS_ENABLED=true
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_RESULT_CACHE_ENABLED=true
REDIS_RESULT_CACHE_TTL_SECONDS=300
```

Keep real secrets in `.env.keys`, which is loaded after `.env` and ignored by
Git:

```env
GEMINI_API_KEY=<secret>
JINA_API_KEY=<secret>
VOYAGE_API_KEY=<secret>
MYSQL_USER=<read-only-user>
MYSQL_PASSWORD=<secret>
GAINR_API_KEY=<generated-company-key>
API_ADMIN_KEY=<separate-monitoring-key>
```

8 GB one-company testing limits:

```env
# 8 GB one-company testing limits
API_TENANT_ENGINE_CACHE_SIZE=1
API_TENANT_MAX_CONCURRENT_SEARCHES=2
```

Never commit `.env`. Rotate credentials that have appeared in chat, terminal
history, screenshots, or logs before public deployment.
Never commit `.env.keys`; use a production secret manager when available.

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
- fetches one bounded page and closes its database connection before embedding
- upserts BM25 rows
- compares stable content hashes
- embeds only changed/new documents
- writes only Gainr's pgvector table and BM25 file
- never updates or deletes company DB rows

This paging boundary is important on CPU-only hosts: Ollama may spend minutes
on a page, but no MySQL/PostgreSQL cursor remains open during that work.

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

Production gate:

```bash
.venv/bin/python scripts/doctor.py --company gainr --strict --production
```

`--production` also checks API auth, the admin key, CORS, Redis, database TLS,
and the 8 GB tenant/concurrency guardrails.

The list operation is read-only and uses a metadata-database aggregate. It does
not materialize all vector metadata or load the HNSW index, which keeps it safe
on low-memory production hosts.

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

The frontend call sequence is not three new requests after every keystroke:

1. Selecting the city calls `filter-data` once and stores the `city_id`; call
   it again only when the city changes.
2. Typing calls `search-suggestions` after a 250–300 ms debounce, normally
   from two characters onward. Cancel or ignore stale in-flight responses.
3. Enter/search submit or clicking a suggestion calls `filter-result` using
   the stored city and filters. Do not call it merely because suggestions
   returned—the browser cannot reliably infer a complete free-text word.
4. Scrolling repeats `filter-result` with `page` incremented.

The recent-search response is exactly `status` plus up to ten newest-first
items containing `id`, `value`, and `is_prosper`. Successful first-page
`filter-result` requests record the search. Repeated values are moved to the
front, and Prosper IDs such as `AA5160` use `is_prosper: 1`.

The `filter-result` response keeps Gainr's `status`, `message`, `data`,
`current_page`, `last_page`, and `image_path` fields and returns 20 cards per
page. Internal route, model, filter-resolution, result-window, and usage
details remain in server logs instead of changing the frontend payload.

Gainr cards preserve the source `ads.status` value without adding an API-side
status restriction. Each card's compact `user` object is hydrated from the
configured compatibility `users_table`. Verified users also populate the legacy
`is_aadhar_gst_verified` object; ordinary users keep that field `null`.
Passwords and remember tokens are never selected.

The frontend implements infinite scrolling by resending the same search term
and filters with `page` incremented to `2`, `3`, and so on. It stops when
`current_page` equals `last_page`.

For semantic `filter-result` searches, Gainr reranks at most two frontend pages
(`compatibility.semantic_ranked_window: 40`). Its tenant retrieval policy then
removes scores below the configured absolute/relative relevance floor and does
not add an unscored catalogue tail unless an explicit category or subcategory
was resolved. When one was resolved, later pages are filled only from that
same category constraint. Otherwise the result can have fewer than 40 ads.
Vector retrieval uses a bounded unfiltered HNSW window followed by the same
metadata checks in memory; this avoids Chroma's slow metadata-filtered scan
while never returning a row that violates the selected filters. Exact repeated
query/filter combinations use the IDs-only Redis result cache and rehydrate
current MySQL rows.

### 13.7 Protected live admin status

Generate a separate secret and place it in the production `.env`:

```bash
openssl rand -hex 32
```

```env
API_ADMIN_KEY=<generated-value>
```

Restart the service, then query the general monitor:

```bash
set -a
source .env
curl -sS https://api.querix.co/api/v1/admin/status \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

For full Gainr health, usage, and search detail:

```bash
curl -sS https://api.querix.co/api/v1/gainr/admin/status \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

For the latest structured search execution timelines:

```bash
curl -sS \
  "https://api.querix.co/api/v1/gainr/admin/search-events?limit=20" \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

Show only failed searches:

```bash
curl -sS \
  "https://api.querix.co/api/v1/gainr/admin/search-events?status=failed" \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

Each completed event includes a trace ID where available and a `timeline`
covering planning, hybrid retrieval, embedding timing, reranking, related-tail
selection, database mapping, and total search time. Deterministic
`filter-result` requests show planning, database filtering/relation hydration,
response mapping, and total request time. Events store query length, counts,
model/provider names, timings, and error types—but never the raw query, filters,
result content, credentials, or exception messages.

For a simple live view:

```bash
while true; do
  clear
  curl -sS https://api.querix.co/api/v1/admin/status \
    -H "X-Admin-Key: $API_ADMIN_KEY" | jq
  sleep 5
done
```

The general endpoint shows process CPU/load/RSS and loaded-company health. The
company endpoint additionally shows usage, active searches, and the latest 20
timing/failure summaries. Search text, filter values, product content, customer
API keys, and provider keys are never returned. Both endpoints return 404 when
`API_ADMIN_KEY` is unset. Up to 100 structured events are retained in process
memory and reset on service restart; use journald for durable historical
incidents.

`X-User-ID` is unrelated to admin authentication. It is optional on
`filter-result` and is used only to associate a successful first-page search
with a verified signed-in user's recent-search history. Omit it for anonymous
users. Never send the literal `local-test-user` in production.

Rules:

- explicit checkbox/range filters are hard constraints and override matching
  query-derived filters;
- automatic query filters fill only missing fields;
- values inside one multi-select use OR, while different filter groups use AND;
- deterministic catalogue queries use complete page-number pagination;
- semantic queries use the configured bounded ranked window and tenant
  relevance floor;
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

### 13.8 Compatibility and regression verification

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

Repository regression baseline verified on 2026-07-03:

```text
tests: 137 passed
```

Database and index counts are environment-specific. Use `src/ingest.py --list`
and the strict doctor instead of copying a historical snapshot into an
operational decision.

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

### 14.1 Rerank-window benchmark

Benchmark 30, 40, and 60 candidate windows before changing production defaults.
Set these values in `.env`, restart the API, then run the query/retrieval evals
and mixed load test:

```env
RERANK_CANDIDATE_K=40
PRIMARY_RANKED_K=40
HYBRID_CANDIDATE_K=40
```

Only keep a smaller window if p95 latency improves without lowering labelled
retrieval quality.

### 14.2 Local reranker benchmark

Local reranking is the default. Hosted Jina/Voyage can be kept as fallback by
adding them after `local` in `RERANK_PROVIDER_ORDER`.

Default local reranker:

```env
RERANK_PROVIDER_ORDER=local
RERANK_LOCAL_MODEL=Alibaba-NLP/gte-reranker-modernbert-base
RERANK_LOCAL_ADAPTER=cross-encoder
RERANK_LOCAL_TRUST_REMOTE_CODE=false
```

Fastest lower-quality fallback if CPU latency is still too high:

```env
RERANK_PROVIDER_ORDER=local
RERANK_LOCAL_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2
RERANK_LOCAL_ADAPTER=cross-encoder
RERANK_LOCAL_TRUST_REMOTE_CODE=false
```

Multilingual quality benchmark:

```env
RERANK_PROVIDER_ORDER=local
RERANK_LOCAL_MODEL=BAAI/bge-reranker-v2-m3
RERANK_LOCAL_ADAPTER=cross-encoder
RERANK_LOCAL_TRUST_REMOTE_CODE=false
```

Hosted fallback mode:

```env
RERANK_PROVIDER_ORDER=local,jina,voyage-2.5,voyage-2.5-lite
```

Jina v3 listwise benchmark, only after reviewing the HF code/license:

```env
RERANK_PROVIDER_ORDER=local,jina,voyage-2.5,voyage-2.5-lite
RERANK_LOCAL_MODEL=jinaai/jina-reranker-v3
RERANK_LOCAL_ADAPTER=jina-listwise
RERANK_LOCAL_TRUST_REMOTE_CODE=true
```

If local reranking increases p95 latency or pushes the host into swap, switch
temporarily to the hosted chain or to the MiniLM local model.

### 14.3 pgvector and ANN

Gainr is configured for pgvector. The pgvector table uses an HNSW approximate
nearest-neighbor index:

```yaml
storage:
  vector_backend: pgvector
  pgvector:
    table: gainr_search_vectors
    hnsw:
      m: 16
      ef_construction: 64
      ef_search: 100
```

`m` and `ef_construction` affect index size/build quality. `ef_search` affects
query recall and CPU cost; raise it when relevance drops, lower it when CPU
latency is too high. Compare:

- unfiltered semantic query
- city-filtered semantic query
- price-filtered semantic query
- mixed text plus filters
- repeated cached query

The pgvector adapter supports SQL-side metadata filtering for Chroma-style
`where` filters. BM25 remains the local SQLite lexical index and is not stored
inside pgvector.

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

Generate DB-backed semantic cases from current Gainr rows:

```bash
.venv/bin/python scripts/generate_semantic_cases.py --company gainr
.venv/bin/python src/evaluate_retrieval.py \
  --company gainr \
  --cases eval/gainr_semantic_cases.generated.json
```

Generated cases validate returned IDs against `ads_search_ready` filters such
as category, city, duration, and ad type. They are useful for broad regression
coverage. Hand-labelled cases in `eval/retrieval_cases.json` remain stricter
exact-ID relevance checks.

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

### 15.7 Complete project CLI index

Use `--help` for the authoritative option set:

```bash
.venv/bin/python src/ingest.py --help
.venv/bin/python src/chat.py --help
.venv/bin/python src/evaluate_queries.py --help
.venv/bin/python src/evaluate_retrieval.py --help
.venv/bin/python scripts/doctor.py --help
.venv/bin/python scripts/load_test.py --help
.venv/bin/python scripts/generate_company_api_key.py --help
```

Common commands:

| Goal | Command |
|---|---|
| Validate company source, no index writes | `.venv/bin/python src/ingest.py --company gainr --mysql --check --limit 10` |
| Incremental DB ingestion | `.venv/bin/python src/ingest.py --company gainr --mysql` |
| Incremental ingestion plus stale-ID removal | `.venv/bin/python src/ingest.py --company gainr --mysql --mysql-reconcile-deletions` |
| BM25-only rebuild | `.venv/bin/python src/ingest.py --company gainr --mysql-bm25-only` |
| Force re-embedding | `.venv/bin/python src/ingest.py --company gainr --mysql --mysql-force-reembed` |
| Authoritative tenant index rebuild | `.venv/bin/python src/ingest.py --company gainr --mysql --mysql-replace-source` |
| List indexed sources/counts | `.venv/bin/python src/ingest.py --company gainr --list` |
| One CLI search | `.venv/bin/python src/chat.py --company gainr --query "portable camera" --limit 10` |
| Interactive CLI search | `.venv/bin/python src/chat.py --company gainr` |
| Start HTTP API | `.venv/bin/python src/run_api.py` |
| Generate one company key | `.venv/bin/python scripts/generate_company_api_key.py --company gainr` |
| Strict dependency/index check | `.venv/bin/python scripts/doctor.py --company gainr --strict` |
| Run tests | `.venv/bin/python -m pytest -q` |
| Compile/import check | `.venv/bin/python -m compileall -q src scripts tests` |
| Query-plan evaluation | `.venv/bin/python src/evaluate_queries.py --company gainr` |
| Retrieval evaluation | `.venv/bin/python src/evaluate_retrieval.py --company gainr` |

Legacy local-file mode uses `data/raw_docs`:

```bash
.venv/bin/python src/ingest.py --check
.venv/bin/python src/ingest.py
.venv/bin/python src/ingest.py --list
.venv/bin/python src/ingest.py --delete '<source-name>'
.venv/bin/python src/ingest.py --clear
```

`--delete` and `--clear` are destructive to the selected local retrieval index
and request confirmation unless `--yes` is explicitly supplied.

### 15.8 HTTP endpoint and authentication index

| Method and path | Header | Purpose |
|---|---|---|
| `GET /api/v1/ready` | none | Process readiness and configured-company count |
| `GET /api/v1/health` | `X-API-Key` when auth is enabled | Generic/legacy health |
| `POST /api/v1/search` | `X-API-Key` when auth is enabled | Generic/legacy search |
| `GET /api/v1/{company}/auth/verify` | `X-API-Key` | Verify endpoint/key binding |
| `GET /api/v1/{company}/health` | `X-API-Key` | Company dependency/index health |
| `POST /api/v1/{company}/search` | `X-API-Key` | Company search and cursor paging |
| `GET /api/v1/{company}/usage` | `X-API-Key` | Monthly company model usage |
| `GET /api/v1/admin/status` | `X-Admin-Key` | Protected process/company overview |
| `GET /api/v1/{company}/admin/status` | `X-Admin-Key` | Protected detailed company status |
| `GET /api/v1/{company}/admin/search-events` | `X-Admin-Key` | Recent privacy-safe timelines |
| `POST /api/v1/gainr/search-suggestions` | `X-API-Key` | Gainr autocomplete |
| `POST /api/v1/gainr/filter-data` | `X-API-Key` | Gainr filter choices |
| `POST /api/v1/gainr/filter-result` | `X-API-Key`; optional trusted `X-User-ID` | Gainr cards and page pagination |
| `GET /api/v1/gainr/recent-search` | `X-API-Key`; trusted `X-User-ID` | Gainr user's recent searches |

Interactive OpenAPI documentation is available from the running FastAPI
service at `/docs`; the machine-readable schema is `/openapi.json`. Disable or
protect those routes at the reverse proxy if the deployment policy requires it.

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

### PyMySQL cleanup fails after slow embedding or `Ctrl+C`

Older ingestion code used one unbuffered cursor for the full source table and
left it open while Ollama embedded each batch. A timeout or interruption could
then produce `_finish_unbuffered_query` or
`AttributeError: 'NoneType' object has no attribute 'settimeout'`.

The current implementation fetches a bounded page, closes the cursor and
connection, and then embeds. Deploy the current `mysql_store.py`,
`postgres_store.py`, `database_store.py`, and `ingestion_service.py`, then
resume with the same command. Completed vectors are skipped:

```bash
.venv/bin/python src/ingest.py \
  --company gainr \
  --mysql \
  --mysql-batch-size 100 \
  --embed-batch-size 4
```

Do not use `--mysql-replace-source` when resuming.

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

For the production systemd service, inspect one failing request without
printing API keys:

```bash
sudo systemctl status personal-rag --no-pager -l
sudo journalctl -u personal-rag -n 200 --no-pager -o short-iso
sudo journalctl -u personal-rag --since "5 minutes ago" --no-pager -o cat
```

For an incident spanning 11 PM through 1 AM, first confirm the server timezone,
then use a range that crosses midnight:

```bash
timedatectl
sudo journalctl -u personal-rag \
  --since "2026-07-02 23:00:00" \
  --until "2026-07-03 01:00:00" \
  --no-pager -o short-iso
```

Replace the dates with the incident dates. `journalctl` interprets them in the
server timezone shown by `timedatectl`, which may differ from IST.

To reproduce while watching logs, keep this running in one terminal:

```bash
sudo journalctl -u personal-rag -f -n 0 -o cat
```

Then send the request from another terminal. Search using the eight-character
trace suffix shown in the logs, for example:

```bash
sudo journalctl -u personal-rag --since today --no-pager -o cat \
  | grep -C 30 '9054b35d'
```

The retrieval completion line reports both the complete vector-stage duration
and Ollama's internal `embed_total_ms` / `embed_load_ms`. A large
`embed_load_ms` means the model was cold; a small embed time with a large
`vector_ms` points to Chroma filtering/search instead.

After deploying code or environment changes:

```bash
sudo systemctl restart personal-rag
sudo systemctl status personal-rag --no-pager -l
sudo journalctl -u personal-rag --since "2 minutes ago" --no-pager -o cat
```

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
