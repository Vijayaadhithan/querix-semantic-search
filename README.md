# Semantic Advertisement Search

Local-first semantic search for advertisement and ecommerce catalogs.

The system is designed for users who know what they need but may not know the
product, service, category, brand, or model name. It searches advertisement
titles, descriptions, keywords, taxonomy, attributes, prices, locations, and
rental metadata, then returns canonical rows from MySQL.

Example:

```text
something portable that can record a wedding clearly from far away
```

This can retrieve cameras, drone cameras, portable recorders, microphones,
gimbals, and related recording equipment without forcing the user to name one
specific product category.

## Current Stack

- MySQL: source and canonical advertisement records
- Ollama: local embeddings only
- Gemini API: hosted structured query extraction
- Chroma: persistent vector index
- SQLite FTS5: persistent BM25 keyword index
- `Alibaba-NLP/gte-reranker-modernbert-base`: local cross-encoder reranking
- Python: ingestion, retrieval, evaluation, and CLI orchestration

Defaults are configured in `config.yaml`:

- Collection: `local_data`
- Embedding model: `embeddinggemma:latest`
- Query models, in fallback order: `gemma-4-26b-a4b-it`,
  `gemma-4-31b-it`, `gemini-3.1-flash-lite`
- Reranker: `Alibaba-NLP/gte-reranker-modernbert-base`
- Vector candidates: 30
- BM25 candidates: 30
- API reranker candidates: 60
- API primary ranked results: 60
- API related tail: up to 140
- API combined result window: 200
- CLI final results: 10

## Search Design

The system separates hard constraints from semantic intent.

### Hard Filters

Hard filters are applied only when they are explicitly requested or safely
derived from a unique database relationship:

- Offer ads versus wanted ads
- State, city, and locality
- Rental duration
- Minimum and maximum rental fee
- Explicit main category or subcategory

Advertisement intent uses the canonical `ads.type` field:

- `type = 1`: offer advertisement
- `type = 2`: wanted/request advertisement

`I need a bike` means the searcher wants an available offer, so results must be
`type = 1`. `Someone looking for bikes` asks for another person's request, so
results must be `type = 2`. The deterministic planner enforces this perspective
after LLM extraction. `ads.status` is a separate lifecycle field and must not
be used to infer offer versus wanted intent.

For example, `Quad Bike in Bangalore per hour` can safely resolve to:

```json
{
  "main_category": "Sports & Toys",
  "subcategory": "Quad Bike",
  "state": "Karnataka",
  "city": "Bengaluru",
  "rental_duration": "Per Hour"
}
```

### Soft Category Hints

When a category is inferred from functionality rather than stated by the user,
it becomes a ranking hint instead of a hard filter.

For example:

```text
something portable that records distant subjects
```

The model may infer `Camera`, but Camera is not used to eliminate recorders,
drone cameras, microphones, or other potentially relevant products.

### DB-Backed Resolution

The persistent BM25 product table is also used to build safe relationship maps:

- Subcategory to main category
- City to state
- Locality to city and state

A parent is derived only when the indexed relationship is unique. Ambiguous
subcategories such as `Technician`, `Consultant`, or `Others` do not force an
incorrect main category.

The planner also handles:

- Singular and plural category names
- Common city aliases such as Bangalore/Bengaluru and Bombay/Mumbai
- Conservative typo recovery such as Coimbtore/Coimbatore
- Hourly, daily, weekly, monthly, and per-ride language
- Price ranges and upper/lower budgets
- Searcher intent versus wanted-ad intent

## End-to-End Search Flow

1. The first available configured Gemini API model rewrites the request into a
   semantic query, keyword query, ad
   intent, and possible structured constraints.
2. Deterministic validation corrects duration, price, ad type, taxonomy,
   aliases, spelling, and hierarchy relationships.
3. Explicit constraints become hard filters. Guessed categories become soft
   hints.
4. Chroma retrieves semantically similar advertisement documents.
5. SQLite FTS5 retrieves exact brands, models, keywords, categories, and
   attributes using BM25.
6. Reciprocal Rank Fusion combines vector and BM25 ranks without comparing
   their incompatible raw scores.
7. API searches over-fetch and fuse a 60-document primary reranker pool.
8. The canonical `ads.type` value removes offer/wanted mismatches before the
   final candidate K is applied.
9. `Alibaba-NLP/gte-reranker-modernbert-base` scores the original user request
   against all primary candidates and retains the strongest 60.
10. Repeated exact titles are diversified in the first result window. Lower
    scoring duplicates remain available later in the primary tier.
11. A stable related tail is selected using whichever validated category,
    location, duration, or price filters are available. It is not necessary
    for every filter field to be present.
12. Primary IDs are excluded from the related tail, and offer/wanted intent is
    enforced in both tiers.
13. Full records are fetched from the canonical `ads` table in this order:
    60 reranked advertisements, then up to 140 related advertisements.

The extraction model never generates product records. Returned data always
comes from MySQL.

## Architecture Boundaries

`ProductSearchEngine` in `src/search_engine.py` exposes the complete:

```text
plan -> retrieve -> filter -> rerank primary -> append filtered tail -> fetch ads
```

flow. The CLI and evaluation tools use this same implementation.

Provider protocols are defined in `src/providers.py`:

- `EmbeddingProvider`
- `StructuredQueryProvider`
- `RerankingProvider`

Ollama provides embeddings and the Google Generative Language API provides
schema-constrained query plans using Gemma or Gemini models. Both use the same
provider boundaries, so filtering, retrieval, fusion, evaluation, and MySQL
mapping remain provider-independent.

## Setup

Create `.env`:

```bash
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_KEEP_ALIVE=-1
GEMINI_API_KEY=your-gemini-api-key
GEMINI_API_BASE_URL=https://generativelanguage.googleapis.com/v1beta

MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DATABASE=rag_ht_test
MYSQL_USER=root
MYSQL_PASSWORD=your-local-password
MYSQL_TABLE=ads_search_ready
MYSQL_CONTENT_COLUMN=embedding_content
MYSQL_BM25_COLUMN=bm25_content
MYSQL_SEARCH_ID_COLUMN=id
MYSQL_RESULT_TABLE=ads
MYSQL_RESULT_ID_COLUMN=id
```

Install dependencies:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

Install the local Ollama embedding model:

```bash
ollama pull embeddinggemma:latest
```

The ModernBERT reranker loads from the local Hugging Face cache. If it is not
cached, Transformers downloads it once. The API then loads those cached weights
into memory during startup and reuses one model instance for all requests in
that process.

## MySQL Data Contract

The search index is built from `ads_search_ready`.

Important fields include:

- `id`
- `embedding_content`
- `bm25_content`
- `main_category_name`
- `subcategory_name`
- `state_name`
- `city_name`
- `locality_name`
- `rental_duration`
- `rental_fee`

`embedding_content` is labelled semantic text containing title, description,
metadata, taxonomy, location, attributes, and selected values.

`bm25_content` contains exact searchable terms such as IDs, brands, models,
keywords, category names, location names, and attribute values.

The final result source is the canonical `ads` table, joined by advertisement
ID after reranking.

## MySQL Ingestion

Validate MySQL configuration without embedding:

```bash
.venv/bin/python src/ingest.py --mysql --check --limit 10
```

Run a small ingestion:

```bash
.venv/bin/python src/ingest.py --mysql --limit 100
```

Run full incremental ingestion:

```bash
.venv/bin/python src/ingest.py --mysql --mysql-batch-size 500 --embed-batch-size 32
```

Rows are streamed from MySQL. Stable document IDs and content hashes allow
unchanged rows to be skipped when ingestion is resumed.

Rebuild only BM25:

```bash
.venv/bin/python src/ingest.py --mysql-bm25-only --mysql-batch-size 5000
```

Replace the MySQL source inside Chroma:

```bash
.venv/bin/python src/ingest.py --mysql --mysql-replace-source
```

Force all rows to be embedded again:

```bash
.venv/bin/python src/ingest.py --mysql --mysql-force-reembed
```

These commands do not update or delete MySQL source rows.

## Local File Ingestion

Files placed in `data/raw_docs/` can also be indexed.

Supported extensions:

- `.pdf`
- `.csv`
- `.tsv`
- `.xlsx`
- `.xlsm`

Validate:

```bash
.venv/bin/python src/ingest.py --check
```

Ingest:

```bash
.venv/bin/python src/ingest.py
```

## Run Search

```bash
.venv/bin/python src/chat.py
```

Example queries:

```text
something portable that can record a wedding clearly from far away in Chennai for a day
A vehicle for recreational driving on rough terrain.
Quad Bike for Hourly Rent in Bangalore
someone looking for bikes in 1000 range per hour
need a bike for a week within 800
```

The reranker is loaded once per process and reused for later queries.

## HTTP API

Start one API process from the repository root:

```bash
.venv/bin/python src/run_api.py
```

Expected startup output:

```text
INFO: Waiting for application startup.
INFO: Preloading reranker Alibaba-NLP/gte-reranker-modernbert-base once for this process...
INFO: Reranker ready in 2797 ms.
INFO: Preloading the Ollama embedding model...
INFO: Ollama embedding model ready in 129 ms.
INFO: Application startup complete.
INFO: Uvicorn running on http://127.0.0.1:8000
```

Each new search prints a correlated flow using an eight-character search ID:

```text
INFO: [search:a1b2c3d4] step=search status=start query_chars=39 limit=60
INFO: [search:a1b2c3d4] step=plan status=start query_chars=39 models=gemma-4-26b-a4b-it -> ...
INFO: step=query_model status=attempt model=gemma-4-26b-a4b-it position=1/3
INFO: step=query_model status=success model=gemma-4-26b-a4b-it duration_ms=1120
INFO: [search:a1b2c3d4] step=plan status=complete model=gemma-4-26b-a4b-it ...
INFO: [search:a1b2c3d4] step=retrieve status=complete vector=120 bm25=120 candidates=60 ...
INFO: [search:a1b2c3d4] step=rerank status=complete results=60 ...
INFO: [search:a1b2c3d4] step=related_tail status=complete primary=60 related=140 ...
INFO: [search:a1b2c3d4] step=mysql_map status=complete rows=200
INFO: [search:a1b2c3d4] step=search status=complete products=200 duration_ms=2840
```

When a model is exhausted or temporarily unavailable, a warning shows the HTTP
status and next model. Logs include model names, stage timings, candidate
counts, and filter field names. They intentionally omit the API key, raw query,
filter values, and product contents. Set `API_LOG_LEVEL=warning` in `.env` for
quiet operation, or `API_LOG_LEVEL=debug` for library diagnostics.

The first reranker run downloads approximately 598 MB of model weights. The
measured first download plus load was 49.7 seconds; subsequent cached startup
was about 2.8-3.1 seconds on the current machine.

The API preloads `embeddinggemma:latest`.
`OLLAMA_KEEP_ALIVE=-1` keeps it resident until Ollama is stopped. Keep one API
process running: requests reuse the loaded reranker and embedding model. Run
one API worker because every additional worker would hold another reranker
copy.

Interactive OpenAPI documentation is available at
`http://127.0.0.1:8000/docs`. Check readiness with:

```bash
curl http://127.0.0.1:8000/api/v1/health
```

Example health output:

```json
{
  "status": "ok",
  "app": "Local Data Assistant",
  "indexed_products": 250117,
  "max_result_window": 200,
  "session_ttl_seconds": 600,
  "reranker_model": "Alibaba-NLP/gte-reranker-modernbert-base",
  "reranker_loaded": true,
  "reranker_load_ms": 2796.68,
  "embedding_warmup": {
    "embedding_model": {
      "model": "embeddinggemma:latest",
      "total_ms": 128.53,
      "load_ms": 83.12
    }
  }
}
```

### First search batch

Send a query and the number of products the UI wants initially:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"bike in Chennai under 1000 per day"}'
```

`page_size` defaults to 20. It can be included explicitly as
`"page_size": 20`; values above 20 are rejected.

The response shape is:

```json
{
  "search_id": "9fdc4b42-0867-442b-92ef-c14678f2c668",
  "query": "bike in Chennai under 1000 per day",
  "cached": false,
  "items": [
    {
      "result_tier": "ranked",
      "id": "231049",
      "title": "Bajaj Pulsar 220 Bike for Daily Rent",
      "rental_duration": "Per Day",
      "rental_fee": "750.00"
    }
  ],
  "interpreted_query": {
    "semantic_query": "bike",
    "keyword_query": "bike",
    "target_ad_type": "offer"
  },
  "applied_filters": {
    "categorical": {
      "main_category_name": "Automobiles",
      "subcategory_name": "Bike",
      "state_name": "Tamil Nadu",
      "city_name": "Chennai",
      "rental_duration": "Per Day"
    },
    "max_rental_fee": 1000
  },
  "unresolved_filters": {},
  "timings_ms": {
    "planning": 10007,
    "vector_search": 1457,
    "bm25_search": 245,
    "related_tail": 75,
    "reranker_load": 0,
    "reranking": 5162,
    "total": 17334,
    "query_model_total": 9843,
    "query_model_load": 228,
    "embedding_model_total": 122,
    "embedding_model_load": 86
  },
  "pagination": {
    "page_size": 20,
    "returned": 20,
    "offset": 0,
    "total_results": 200,
    "has_more": true,
    "next_cursor": "NEXT_CURSOR"
  }
}
```

`items` come from canonical rows in the MySQL `ads` table, but the public API
returns an explicit field allowlist. Internal user IDs, phone numbers, hidden
contact data, keywords, and administrative fields are not serialized. Rows
with a non-null `deleted_at` are excluded. The API does not guess visibility
from `ads.status`, because wanted rows use a different status lifecycle.
`result_tier` is the only synthetic result field: `ranked` identifies the
primary cross-encoder results and `related` identifies the filtered tail.
With the default page size, pages 1–3 contain the 60 primary ranked results;
page 4 onward contains the related tail. If fewer than 60 primary results are
available, the related tier begins immediately after the last primary result.

### Infinite scroll / next batch

When `has_more` is `true`, send the returned cursor instead of the query:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"cursor":"PASTE_NEXT_CURSOR_HERE"}'
```

Abbreviated page-2 output:

```json
{
  "query": "bike in Chennai under 1000 per day",
  "cached": true,
  "items": [
    {
      "result_tier": "ranked",
      "id": "10094",
      "title": "Honda Dream Yuga Bike for Daily Rent",
      "rental_fee": "600.00"
    }
  ],
  "pagination": {
    "page_size": 20,
    "returned": 20,
    "offset": 20,
    "total_results": 200,
    "has_more": true,
    "next_cursor": "NEXT_CURSOR"
  }
}
```

The measured page-2 response time was 1.1 ms because it used the cached ranked
window.

The server constructs the configured combined result window once and keeps it
in memory for 10 minutes by default. Cursor requests return stable slices from
that window, so scrolling does not repeat query extraction, embeddings,
retrieval, reranking, or related-tail selection. Cursor responses have
`"cached": true`. A cursor is intentionally opaque; the frontend should store
and return it unchanged. An expired cursor returns HTTP `410`, and the frontend
must start a new search using `query`.

`K` controls primary candidate-pool depth; it is not the end of the catalog.
The API fuses 60 primary candidates, reranks them, and places those 60 first.
Positions 61–200 are a stable tail matching whichever validated intent
fields are present. For example, a query containing only `city=Chennai` can
produce a Chennai tail; it does not also require a category, state, duration,
and price. A completely unfiltered query does not receive a random catalog
tail. Primary IDs are deduplicated from the tail.

API behavior is configured under `api` in `config.yaml`:

- `default_page_size`: used when the payload omits `page_size`
- `max_page_size`: validation ceiling for one response
- `max_results`: maximum combined ranked-plus-related window available to scroll
- `session_ttl_seconds`: lifetime of an in-memory cursor
- `max_sessions`: memory bound for active searches

Set `API_CORS_ORIGINS` in `.env` when a browser frontend runs on a different
origin. Use a comma-separated allowlist; do not use `*` with private data.

### Hosted query planning

The Google API is used only for structured query extraction. The configured
order is `gemma-4-26b-a4b-it`, `gemma-4-31b-it`,
then `gemini-3.1-flash-lite`. A request moves to the next model only for HTTP
429 (quota/rate limit) or a temporary HTTP 5xx provider failure.
Authentication, permission, and malformed-request errors do not trigger
fallback. Quota numbers are not hardcoded because Google applies them per
project and model and may change them; the provider's HTTP response is the
source of truth. Local embeddings, filtering, BM25, reranking, and canonical
MySQL result retrieval are unchanged. The API key is read from `.env` and sent
in the `X-goog-api-key` header.

`gemini-3.1-flash-live-preview` is intentionally excluded. It is a voice-first
Live API model that exposes bidirectional generation rather than the
`generateContent` endpoint used here, and it does not support structured JSON
outputs. It is appropriate for a separate real-time audio interface, not this
request-response query planner.

This removes the local 12B model's memory and cold-start cost. The tradeoff is
that each raw search query, including any stated location and budget, is sent to
Google. It also adds network dependency, provider rate limits, and usage cost.
If every configured Google model is unavailable, the planner falls back to the
original query with no model-extracted filters.

To release Ollama memory after stopping the API:

```bash
ollama stop embeddinggemma:latest
```

## Evaluation

Run deterministic unit tests:

```bash
.venv/bin/python -m pytest -q
```

Run query-understanding scenarios:

```bash
.venv/bin/python src/evaluate_queries.py
```

Cases are stored in `eval/query_cases.json`.

Run end-to-end labeled retrieval:

```bash
.venv/bin/python src/evaluate_retrieval.py
```

Cases are stored in `eval/retrieval_cases.json`. The evaluator reports passed
cases and Mean Reciprocal Rank.

Latest verified results:

```text
57 unit tests passed
9/9 query-plan cases passed
5/5 end-to-end retrieval cases passed
MRR = 0.900
```

Compile-check:

```bash
python3 -m compileall -q src tests
```

## Index Management

List indexed sources:

```bash
.venv/bin/python src/ingest.py --list
```

Delete one Chroma source:

```bash
.venv/bin/python src/ingest.py --delete "source-name"
```

Clear Chroma:

```bash
.venv/bin/python src/ingest.py --clear
```

These operations affect Chroma only. They do not delete source files, BM25
rows, or MySQL records.

## Project Structure

```text
src/
  chat.py                 Interactive CLI
  api.py                  HTTP API and cursor pagination
  run_api.py              API process entry point
  search_engine.py        Reusable end-to-end search service
  query_planner.py        LLM extraction and deterministic validation
  retrieval.py            Vector, BM25, RRF, and ad-type filtering
  reranker.py             Transformer cross-encoder adapter
  providers.py            Replaceable model-provider protocols
  gemini_client.py        Hosted structured-query provider
  ollama_client.py        Local embedding provider
  bm25_index.py           Persistent SQLite FTS5 index
  mysql_store.py          MySQL reads and canonical record lookup
  ingestion_service.py    Incremental ingestion workflows
  document_processing.py  Source and metadata preparation
  evaluate_queries.py     Query-plan evaluation
  evaluate_retrieval.py   End-to-end retrieval evaluation
eval/
  query_cases.json
  retrieval_cases.json
```

## Current Limitations

- Query extraction and local embedding latency depend on Ollama model size and
  hardware.
- The API pays ModernBERT and Ollama model-loading costs during startup. The
  CLI still loads models lazily on its first query.
- Keeping both Ollama models resident used approximately 8.5 GB of GPU memory
  in the measured environment.
- The labeled retrieval set is intentionally small and must grow before making
  production-quality claims.
- Soft category boosts and candidate counts still require benchmark-driven
  tuning.
- The HTTP API excludes soft-deleted rows but does not interpret `ads.status`.
  Confirm the complete status/visibility policy with the owning team before
  production deployment. The lower-level engine and CLI return canonical rows
  without applying the API presentation rule.
- City aliases are not a complete geographic knowledge base.
- Exact-title diversification can hide multiple legitimate listings with the
  same title; business-specific deduplication should eventually use seller,
  location, price, and availability.
