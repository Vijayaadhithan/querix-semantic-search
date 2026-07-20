# Company API Integration

This guide describes the tenant-neutral HTTP contract. Examples use placeholders so the same integration can be applied to any configured company.

## Base URL and authentication

```text
https://<api-domain>/api/v1/<company-slug>
```

Send the tenant API key on every company request:

```http
X-API-Key: <company-api-key>
```

Keep this key on a trusted backend. Browser applications should call their own backend proxy rather than embedding the production key in frontend code.

## Verify credentials

```http
GET /api/v1/<company-slug>/auth/verify
X-API-Key: <company-api-key>
```

Successful response:

```json
{
  "authorized": true,
  "company_id": "<company-id>",
  "endpoint_slug": "<company-slug>"
}
```

## Health

Public serving-path readiness:

```http
GET /api/v1/ready
```

Successful deep readiness results are cached for five minutes by default to
avoid repeatedly querying pgvector, the source database, and Ollama when an
external monitor polls frequently. `cached` in the response indicates whether
the component results were reused. Failed readiness results are never cached.
For a cheap process-only liveness probe, use `GET /api/v1/live`.

This endpoint returns `200` only when the configured tenant indexes, source
database, and Ollama embedding model are available. It returns `503` with
component status when a critical dependency is unavailable. Redis and hosted
rerank providers are not readiness blockers because search has local/fusion
fallbacks for those dependencies.

Authenticated tenant health:

```http
GET /api/v1/<company-slug>/health
X-API-Key: <company-api-key>
```

Tenant health reports index counts, reranker state, embedding warmup, Redis connectivity, and cache configuration. Treat a non-2xx response as unavailable.

## Start a search

```http
POST /api/v1/<company-slug>/search
Content-Type: application/json
X-API-Key: <company-api-key>

{
  "query": "portable projector in the city centre under 2000",
  "page_size": 10
}
```

The canonical request fields are:

| Field | Type | Rules |
|---|---|---|
| `query` | string | Required for the first page; must not be blank |
| `cursor` | string | Required for a subsequent page |
| `page_size` | integer | Optional bounded page size |

Supply exactly one of `query` or `cursor`. A tenant profile may map different external field names to this internal contract.

## How a query is executed

Routing changes internal work, not the request or response contract:

- An exact catalogue category with simple user-stated filters uses the
  deterministic indexed-database path. It does not call an LLM, embedding
  model, vector search, BM25, or a reranker.
- Descriptive, ambiguous, misspelled, colloquial, or multilingual wording uses
  semantic search: tenant-aware planning, query embedding, pgvector and BM25,
  fusion, intent shaping, and hosted reranking.
- Explicit request filters are authoritative. Model-inferred categories and
  tenant aliases are soft relevance signals and never fuzzy hard filters.

The planner has one common base prompt. A tenant profile may add its own
`planner.prompt_context` and `planner.query_aliases`. Those settings and both
plan/result caches are tenant-scoped, so one company's vocabulary cannot alter
another company's search behavior.

## Search response

```json
{
  "company_id": "<company-id>",
  "search_id": "<opaque-id>",
  "query": "portable projector in the city centre under 2000",
  "cached": false,
  "items": [
    {
      "id": "123",
      "title": "Portable projector"
    }
  ],
  "interpreted_query": {
    "execution_path": "semantic"
  },
  "applied_filters": {},
  "unresolved_filters": {},
  "timings_ms": {},
  "usage": {},
  "pagination": {
    "page_size": 10,
    "returned": 1,
    "offset": 0,
    "total_results": 1,
    "has_more": false,
    "next_cursor": null
  }
}
```

Only fields approved in the tenant payload configuration appear in `items`. Clients must tolerate additional diagnostic fields but should not depend on undocumented internal ranking values.

## Pagination

When `pagination.has_more` is true, send the returned cursor without repeating the query:

```http
POST /api/v1/<company-slug>/search
Content-Type: application/json
X-API-Key: <company-api-key>

{
  "cursor": "<next-cursor>"
}
```

Cursors are opaque, query-bound, tenant-bound, and time-limited. Do not modify or persist them as permanent catalogue links.

## Usage

```http
GET /api/v1/<company-slug>/usage?month=2026-07
X-API-Key: <company-api-key>
```

The optional month uses `YYYY-MM`. Usage is tenant-scoped and intended for account reporting, not per-request billing decisions in a browser.

## Errors

| Status | Meaning | Client action |
|---|---|---|
| `400` | Invalid request or cursor | Correct the request; do not retry unchanged |
| `401` or `403` | Missing, invalid, or wrong-tenant key | Fix backend credentials |
| `410` | Cursor expired | Restart from the original query |
| `422` | Request validation failed | Show a validation message or correct the payload |
| `429` | Tenant rate limit reached | Retry with exponential backoff and jitter |
| `503` | Dependency unavailable or bounded search capacity is busy | Honor `Retry-After`, retry briefly, and alert if sustained |

Do not automatically retry validation or authentication failures. For `429` and transient `503`, use bounded exponential backoff and preserve the original request ID in application logs.

## Compatibility endpoints

The service can enable an optional tenant adapter for an existing frontend contract. When enabled by tenant configuration, it may expose:

- `POST /api/v1/<company-slug>/search-suggestions`
- `POST /api/v1/<company-slug>/filter-data`
- `POST /api/v1/<company-slug>/filter-result`
- `GET /api/v1/<company-slug>/recent-search`

These routes are adapter-specific and are not part of the canonical integration contract. New integrations should use `/search`, `/health`, and `/usage` unless compatibility is explicitly required.

Gainr's `filter-result` adapter uses page-number pagination rather than the
canonical cursor contract. Resend the same `searchTerm` and `filter` object and
change only `page`. Exact deterministic requests use the direct catalogue path.
Semantic requests keep the ranked vector/BM25 results first, then eligible
filtered continuation inventory satisfying the same predefined city, locality,
price, duration, and ad-type constraints. The final page may contain fewer than
20 rows when eligible inventory is exhausted.

These routing and relevance changes do not alter Gainr's legacy input or output
payload. The adapter still emits only its configured public fields and keeps
internal search metadata out of the frontend response unless explicitly
enabled in tenant configuration.

## Backend proxy example

```python
import os

import requests


def search_catalog(payload: dict) -> dict:
    response = requests.post(
        f"{os.environ['SEARCH_API_URL']}/search",
        headers={"X-API-Key": os.environ["SEARCH_API_KEY"]},
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()
```

Set `SEARCH_API_URL` to the tenant base URL. Store `SEARCH_API_KEY` only in the backend secret store.

## Integration checklist

- Use the assigned company slug and key.
- Proxy browser calls through a trusted backend.
- Implement query-or-cursor pagination exactly.
- Handle expired cursors by restarting the query.
- Back off on rate limits and transient service errors.
- Render only documented public product fields.
- Test against a non-production tenant before launch.
