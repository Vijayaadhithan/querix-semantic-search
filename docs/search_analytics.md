# Durable search analytics

MySQL tenants can opt into durable, per-request search analytics with the
tenant profile `analytics` section. Gainr enables this feature and stores two
tables in its configured company database:

- `semantic_search_history` stores one row per search request: UTC timestamp,
  company and optional user ID, normalized query, route, filters, page,
  result counts, cache state, total latency, aggregate provider calls, and
  aggregate token counts.
- `semantic_search_api_usage` stores the planner, embedding, and reranker
  attempts associated with that request, including provider, model, operation,
  status, duration, API-call count, and provider-reported tokens.

The tables deliberately do not store API keys, authorization headers, IP
addresses, or product payloads. Query text and user IDs can contain personal
information, so database access and backups should follow the company's data
retention and access policy.

Create or verify the tables with:

```bash
python scripts/migrate_search_analytics.py --company gainr
```

The migration is idempotent. Production deployment runs it after building the
API image and before restarting services.

Searches enqueue one small in-memory event and do not wait for a MySQL insert.
A single bounded worker preserves insert order and writes the parent and child
rows in one transaction. If MySQL is unavailable, searches continue normally;
the failure is logged without query text. Graceful API shutdown waits briefly
for queued events. Queue counters are process-local operational diagnostics,
not a replacement for database monitoring.

Example daily totals:

```sql
SELECT
    DATE(created_at) AS search_date,
    execution_path,
    COUNT(*) AS searches,
    SUM(api_call_count) AS external_api_calls,
    SUM(total_tokens) AS total_tokens,
    ROUND(AVG(duration_ms), 1) AS average_latency_ms
FROM semantic_search_history
GROUP BY DATE(created_at), execution_path
ORDER BY search_date DESC, execution_path;
```

Example provider usage:

```sql
SELECT
    DATE(created_at) AS usage_date,
    provider,
    model,
    operation,
    status,
    SUM(api_calls) AS api_calls,
    SUM(total_tokens) AS total_tokens
FROM semantic_search_api_usage
GROUP BY DATE(created_at), provider, model, operation, status
ORDER BY usage_date DESC, provider, operation;
```

