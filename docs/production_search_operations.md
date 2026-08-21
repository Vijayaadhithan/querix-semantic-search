# Production Search Operations

This document defines production policy and operational expectations. Executable deployment and maintenance commands are kept separately in [Production commands](production_commands.md).

## Service objectives

The production service should provide:

- tenant-isolated search and usage accounting;
- semantic-first ranking with structured-filter correctness;
- bounded latency and memory on the selected host;
- graceful degradation during vector, BM25, reranker, or query-provider failures;
- reproducible ingestion and rollback;
- API-visible timings and provider diagnostics.

## Recommended 8 GB profile

Use one API worker, a tenant engine cache of one, and one concurrent search per tenant as the safe starting point. Keep pgvector and Redis on private Docker networking and expose the API only through a TLS reverse proxy.

The hosted profile uses Voyage 2.5 first, OpenRouter Nemotron free second, and
Voyage 2.5 Lite last. LangSearch and Jina are not runtime providers. The profile
reranks 20 candidates, caps each candidate document at 300 characters, and uses
a 40-item hybrid recall window. A compatibility adapter may retain the ranked
window first and add eligible filtered continuation results only after it.

Exact catalogue categories with simple stated filters use the deterministic
database path and skip the LLM, embedding, pgvector, BM25, and reranker.
Conversational, offer/wanted, typo, ambiguous, descriptive, and multilingual
requests use a semantic path:
tenant-aware planning, `embeddinggemma:latest`, pgvector HNSW and BM25,
reciprocal-rank fusion, intent shaping, then hosted reranking. Fuzzy or inferred
category language is never promoted to a hard category filter.

Observed production timings are workload and provider dependent. Warm exact
deterministic requests have been about 0.34-0.35 seconds; an uncached exact plan
has been about 0.70-0.72 seconds. Recent semantic stage observations put the
planner around two seconds when uncached, retrieval around 0.2-0.35 seconds, and
reranking around 0.35-1.65 seconds. Treat these as diagnostic examples, not
service guarantees; result and plan cache hits are substantially faster.

The hosted document payload is bounded at 6,000 characters per search before JSON overhead: 20 candidates multiplied by 300 characters. This is 60% lower than a 30-candidate, 500-character profile while retaining a fully reranked 20-result first page. Provider-reported token counts can differ from character estimates, so monitor the usage diagnostics in real traffic.

The service intentionally has no local reranker fallback. If all hosted providers fail, it returns the existing hybrid order with degraded diagnostics and does not cache that degraded result. Vector and BM25 retrieval are also independent: one may serve while the other is degraded, and both failures are required before retrieval itself is unavailable.

## Routine releases

The canonical copy-paste workflow is
[Routine code change](production_commands.md#routine-code-change-use-this-every-time).
An API-only release rebuilds and recreates the API container but does not run
ingestion or modify pgvector/BM25. The production Compose layout uses Docker
Ollama and the `DOCKER_OLLAMA_BASE_URL`, `DOCKER_REDIS_URL`, and
`DOCKER_MYSQL_HOST` variables.

## Release policy

Every search release should pass:

1. unit and API contract tests;
2. strict dependency, database, vector, and BM25 checks;
3. query-plan evaluation;
4. tenant retrieval evaluation;
5. a warm API smoke search;
6. log review for degraded providers, timeouts, and unexpectedly slow stages.

Ranking changes should be approved from a reviewed evaluation set. Generated cases are useful for coverage but are not a substitute for client-approved relevance judgments.

## Index lifecycle

Use incremental ingestion for routine updates. It writes changed BM25 rows and
vectors while skipping completely unchanged content. Embedding text/model
identity and retrieval metadata are compared separately: a BM25/filter-only
change updates BM25 and stored vector metadata while reusing the existing
embedding.

An upstream BM25 source-column change is different from a routine row update.
Run one full ETL content rebuild without `--incremental` so unchanged listings
receive the new lexical contract, publish the complete artifact atomically,
then run backend incremental ingestion. Re-embedding is required only if the
embedding text/hash, model, or vector dimensions changed.

The enabled systemd timer starts a shadow-generation job around 03:00 IST. A
source scan may read all eligible rows, but it embeds only changed/new content
into the inactive tenant slot and reconciles deletions there. The active
pgvector/BM25 generation continues serving throughout the scan. The candidate
must match source/vector/BM25 counts. Candidate HNSW recall is measured against
exact vector search and cannot regress materially from the active generation;
BM25 control-query overlap remains at least 80% by default. It is then
prewarmed and hot-swapped in
the tenant service pool; the API process is not restarted and in-flight
requests finish against the previous generation.

Each tenant owns two bounded physical slots. Only the active generation enters
cache fingerprints, and the previous generation is retained as the next
standby and immediate rollback target. A failed build, validation, warm-up, or
activation leaves the active generation unchanged. The first migration from a
legacy single index performs a one-time copy of the existing pgvector and BM25
indexes before applying incremental changes.

If a promoted generation later proves unsuitable, call
`POST /api/v1/<tenant>/admin/rollback-index` with `X-Admin-Key`. The API opens
and checks the recorded previous generation before atomically switching back;
it does not restart the process or interrupt in-flight searches.

Use deletion reconciliation only after a complete scan. Use forced re-embedding when the embedding model or embedding-text contract changes. Use replacement only for an authoritative tenant rebuild, because it clears that tenant's existing vector source and BM25 index before repopulation.

Back up pgvector and the `storage/` directory before destructive maintenance.

## Monitoring

Monitor:

- readiness and tenant health;
- total request latency and stage timings;
- result-cache hit rate;
- reranker provider, error type, and degraded status;
- database pool wait and query time;
- pgvector and BM25 counts;
- process/container memory and CPU;
- HTTP 429 and 5xx rates;
- bounded-capacity rejections and degraded retrieval counts.

Each tenant's `storage.pgvector.query_mode` controls the vector SQL rollout:

- `legacy` serves only the established SQL path;
- `shadow` serves legacy results and executes the optimized reduced-payload
  query for ID, order, distance, document, and metadata equivalence metrics;
- `optimized` serves the reduced-payload path after shadow validation passes.

Shadow mode fails open to the legacy vector results when the comparison query
fails. Search logs include `vector_query_mode`, `vector_shadow_equal`,
`vector_shadow_error`, legacy/optimized database timings, parallel retrieval,
fusion, authoritative type lookup, eligibility, and hydration durations. Shadow
mode intentionally adds database work and is for bounded validation windows,
not steady-state production traffic.

Planning has three observable execution paths:

- `deterministic_filter` resolves a complete objective category/filter request
  locally and queries the catalogue without an LLM.
- `direct_semantic` accepts a short, locally resolvable marketplace request
  with no numeric, location, duration, or otherwise ambiguous constraint. This
  includes clear offer/wanted language and reviewed tenant translations. It
  skips hosted planning but retains embedding, vector/BM25 fusion, reranking,
  eligibility, and hydration.
- `semantic` uses the hosted planner for every remaining or uncertain query.

Plan logs include `route_reason`. Direct routing is deliberately asymmetric:
uncertainty goes to the hosted planner because an unnecessary LLM call costs
latency, while an incorrect bypass can cost relevance. Set
`QUERY_DIRECT_SEMANTIC_FAST_PATH=false` for an immediate configuration rollback.

Set `storage.pgvector.prewarm_on_startup: true` for a tenant to synchronously
warm its HNSW index before the API reports ready. `prewarm_mode: read` targets
the host filesystem cache; `prewarm_mode: buffer` targets PostgreSQL shared
buffers and requires a buffer pool larger than the HNSW index. Startup prewarm
is fail-open and logs the index, mode, blocks, bytes, and duration; it does not
alter ranking behavior. The periodic local-path warm-up repeats the configured
index prewarm before its representative HNSW queries. The scheduled tenant
warm-up uses a deep unfiltered HNSW window after warming the complete vector
heap and index. Override `WARMUP_CANDIDATES` only after latency and relevance
testing.

For a tenant configured with `prewarm_mode: buffer`, startup, the 30-minute
warm-up, and the post-ingestion warm-up load the vector heap and HNSW index
into PostgreSQL's fixed buffer pool before running representative queries. Do
not change the prewarm mode, `shared_buffers`, or the container memory limit
without comparing filtered semantic latency and peak host/cgroup memory.

`vector_eligible=1001 vector_eligible_capped=True` means the bounded
eligibility probe found more than the 1,000-row exact-ranking threshold; it
does not mean the vector query fetched 1,001 payload rows. The broad-filter
path uses native iterative filtered HNSW and stops after the requested matching
candidate count. A semantic engine log
with `products=0 hydration=deferred` likewise means the compatibility layer
will hydrate the ranked IDs, not that retrieval found no products.

A high reranker time or token count suggests reducing the ranked window or document-character cap only after relevance testing. A high vector time suggests checking HNSW use, metadata predicates, database load, and `ef_search`. A high planner time suggests deterministic fast-path coverage or query-provider latency.

Container logs show lifecycle, access, and provider warnings. Per-stage search
timings are stored in the tenant-safe admin event feed:

```bash
curl -fsS \
  "http://127.0.0.1:8000/api/v1/<company>/admin/search-events?limit=20" \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

The enabled tenant also persists a reduced tenant-facing search history table
and a separate operator-facing provider-call/token table to its configured
MySQL database for now. See
[`search_analytics.md`](search_analytics.md) for the two-table schema,
migration command, privacy boundary, and reporting queries.

Authenticated administrators can also poll a bounded, sanitized application
log feed without server access:

```bash
curl -fsS \
  "https://api.example.com/api/v1/admin/logs?limit=100&level=WARNING" \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

Use `next_after_id` as the next request's `after_id` to retrieve only newer
entries. Events are explicitly ordered `oldest_to_newest` and include a small
`kind` such as `startup`, `search_completed`, `provider_fallback`, `capacity`,
`http_failure`, `warning`, or `error`. At INFO, the feed keeps one end-to-end
tenant completion summary per request rather than every internal search stage.
It also keeps the startup warm-up summary, provider fallback/capacity events,
failed HTTP requests, and all warnings/errors. Successful HTTP access logs and
intermediate plan/retrieval/reranking lines are omitted; use the company admin
`search-events` endpoint for structured stage timings and Docker logs for raw
detail.

The feed retains at most `API_ADMIN_LOG_BUFFER_SIZE` entries per API process,
resets when that process restarts, omits tracebacks, and redacts common
credential formats. It intentionally does not expose Nginx, Docker,
PostgreSQL, Redis, Ollama, arbitrary files, or environment variables. Use the
server's normal operational tooling when those infrastructure logs are needed.

At the normal `info` level, successful searches retain completion summaries
for planning, retrieval, reranking, and the full engine search. Start/attempt,
cache-miss, successful provider, related-tail, and database-map details require
`API_LOG_LEVEL=debug`. API container logs rotate at 10 MB with three files, so
their disk use remains bounded to roughly 30 MB per container.

## Security checklist

- Authentication and rate limiting are enabled.
- Customer and admin keys are distinct.
- CORS contains only approved, exact HTTPS origins. Production and testing
  frontends are listed separately; wildcard origins are not used.
- Source and vector databases use least-privilege credentials.
- Remote database TLS uses certificate verification.
- Redis and pgvector are not publicly reachable.
- Logs do not contain API keys, passwords, or raw sensitive queries.
- Container images and dependencies are rebuilt on a controlled schedule.

## Backup and recovery

Back up the pgvector database with standard PostgreSQL tooling. Back up `storage/` for BM25, usage, and local application state. Verify recovery by restoring into a separate environment, running the doctor, comparing index counts, and executing the retrieval evaluation.

If a release degrades relevance or stability, restore the prior image and configuration first. Restore index data only when the schema, embedding model, or ingestion contract changed.

## Capacity decisions

Do not increase concurrency solely because individual requests are fast. Test simultaneous cold and warm searches while measuring latency, provider quotas, database load, and peak resident memory. Add API workers only after shared caches and rate limits behave correctly across processes.
