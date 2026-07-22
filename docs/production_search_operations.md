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
a 40-item hybrid recall window. Gainr retains the ranked window first and adds
eligible filtered continuation results only after it.

Exact catalogue categories with simple stated filters use the deterministic
database path and skip the LLM, embedding, pgvector, BM25, and reranker. Typo,
ambiguous, descriptive, and multilingual requests use the semantic path:
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

Use incremental ingestion for routine updates. It writes changed BM25 rows and vectors while skipping content whose hash and embedding model are current.

The enabled systemd timer starts this guarded job around 03:00 IST. A source
scan may read all eligible rows, but it embeds only changed/new content,
reconciles deletions, and restarts the API only after success. Once readiness
passes, the job runs several representative unfiltered vector searches to warm
the shared PostgreSQL HNSW buffer paths before live traffic resumes.

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

A high reranker time or token count suggests reducing the ranked window or document-character cap only after relevance testing. A high vector time suggests checking HNSW use, metadata predicates, database load, and `ef_search`. A high planner time suggests deterministic fast-path coverage or query-provider latency.

Container logs show lifecycle, access, and provider warnings. Per-stage search
timings are stored in the tenant-safe admin event feed:

```bash
curl -fsS \
  "http://127.0.0.1:8000/api/v1/<company>/admin/search-events?limit=20" \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

Authenticated administrators can also poll a bounded, sanitized application
log feed without server access:

```bash
curl -fsS \
  "https://api.example.com/api/v1/admin/logs?limit=100&level=WARNING" \
  -H "X-Admin-Key: $API_ADMIN_KEY" | jq
```

Use `next_after_id` as the next request's `after_id` to retrieve only newer
entries. The feed retains at most `API_ADMIN_LOG_BUFFER_SIZE` entries per API
process, resets when that process restarts, omits tracebacks, and redacts common
credential formats. It includes API, search/provider, compatibility, and
Uvicorn access messages. Successful `/api/v1/live` and `/api/v1/ready` probes
are omitted as routine noise; failed probes remain visible. It intentionally
does not expose Nginx, Docker,
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
- CORS contains only approved origins.
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
