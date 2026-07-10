# Company API Integration Guide

This guide is for companies integrating their website, mobile app, or backend
with the search API. It covers authentication, endpoint URLs, request and
response contracts, pagination, usage reporting, diagnostics, and the Gainr
legacy-compatible frontend flow.

## 1. Base URL

Production deployments should expose the API behind HTTPS:

```text
https://<api-domain>/api/v1
```

Local development examples use:

```text
http://127.0.0.1:8000/api/v1
```

Each company also receives an endpoint slug. For example, Gainr uses:

```text
/api/v1/gainr
```

The generic company search endpoint is:

```text
POST /api/v1/{company_endpoint}/search
```

## 2. Authentication

Every company request must include the issued API key:

```http
X-API-Key: <company-api-key>
```

Do not put the API key in query parameters, browser-visible config, or client
source code unless the deployment is intentionally exposing a public frontend
key through a backend proxy or other controlled boundary.

Before integrating search, verify the key and endpoint binding:

```bash
curl https://<api-domain>/api/v1/<company_endpoint>/auth/verify \
  -H 'X-API-Key: <company-api-key>'
```

Successful response:

```json
{
  "authorized": true,
  "company_id": "gainr",
  "endpoint_slug": "gainr"
}
```

Authentication failures:

| Status | Meaning |
|---|---|
| `401` | Missing or invalid API key |
| `403` | API key is valid but belongs to a different company endpoint |
| `404` | Unknown company endpoint or endpoint not enabled |

## 3. Readiness and Health

Readiness confirms the process is accepting requests:

```bash
curl https://<api-domain>/api/v1/ready
```

Company health confirms the company index and runtime dependencies:

```bash
curl https://<api-domain>/api/v1/<company_endpoint>/health \
  -H 'X-API-Key: <company-api-key>'
```

Example response:

```json
{
  "status": "ok",
  "app": "Local Data Assistant",
  "indexed_products": 123456,
  "max_result_window": 200,
  "session_ttl_seconds": 900,
  "reranker_model": "jina-reranker-v2-base-multilingual",
  "reranker_loaded": true,
  "reranker_load_ms": 1200.0,
  "embedding_warmup": {},
  "redis_enabled": true,
  "redis_connected": true,
  "query_plan_cache_backend": "redis",
  "result_cache_enabled": true,
  "result_cache_ttl_seconds": 300,
  "company_id": "gainr"
}
```

## 4. Generic Search API

Use this API for new company integrations.

```http
POST /api/v1/{company_endpoint}/search
Content-Type: application/json
X-API-Key: <company-api-key>
```

### First Page Request

```bash
curl -X POST https://<api-domain>/api/v1/<company_endpoint>/search \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <company-api-key>' \
  -d '{
    "query": "bike in Chennai under 1000",
    "page_size": 20
  }'
```

Request fields:

| Field | Type | Required | Notes |
|---|---:|---|---|
| `query` | string | yes for first page | Natural-language query, maximum 1000 characters |
| `cursor` | string | yes for next page | Opaque cursor from the previous response |
| `page_size` | integer | no | Defaults to 20; bounded by server configuration |

Send exactly one of `query` or `cursor`.

### Next Page Request

```bash
curl -X POST https://<api-domain>/api/v1/<company_endpoint>/search \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <company-api-key>' \
  -d '{
    "cursor": "<next_cursor>",
    "page_size": 20
  }'
```

Cursors are opaque, company-bound, and expire after the configured session TTL.
Do not decode, edit, or store them long-term.

### Search Response

```json
{
  "company_id": "gainr",
  "search_id": "2f80f54e-8bf3-4df9-b4e5-45b6dcd899f7",
  "query": "bike in Chennai under 1000",
  "cached": false,
  "items": [
    {
      "id": 235255,
      "title": "Mountain bike",
      "rental_duration": "Per Day",
      "rental_fee": 750,
      "city_id": 456,
      "locality_id": 167889,
      "photos": "bike.jpg"
    }
  ],
  "interpreted_query": {
    "semantic_query": "bike",
    "keyword_query": "bike Chennai 1000",
    "target_ad_type": "offer",
    "sort_order": "price_asc",
    "execution_path": "semantic",
    "plan_cache_hit": false,
    "result_cache_hit": false,
    "query_corrections": [],
    "reranker_provider": "jina"
  },
  "applied_filters": {
    "categorical": {
      "city_name": "Chennai"
    },
    "max_rental_fee": 1000
  },
  "unresolved_filters": {},
  "timings_ms": {
    "planning": 12.5,
    "vector_search": 35.1,
    "bm25_search": 7.2,
    "related_tail": 2.0,
    "reranker_load": 0.0,
    "reranking": 48.4,
    "result_cache": 0.0,
    "total": 118.9
  },
  "usage": {
    "tracked": true,
    "model_requests": 1,
    "input_tokens": 120,
    "output_tokens": 35,
    "total_tokens": 155,
    "breakdown": []
  },
  "pagination": {
    "page_size": 20,
    "returned": 1,
    "offset": 0,
    "total_results": 1,
    "has_more": false,
    "next_cursor": null
  }
}
```

The `items` fields are allowlisted per company. Internal embedding text,
private database fields, API keys, and raw provider details are not returned.

## 5. Company-Specific Field Names

A company can configure request field aliases. For example, one tenant may map:

```yaml
payload:
  request_mapping:
    query: search_text
    cursor: continuation_token
    page_size: limit
```

That company would call:

```json
{
  "search_text": "camera in Mumbai",
  "limit": 20
}
```

Unexpected request fields are rejected with `422`, so frontend payloads should
match the configured contract exactly.

## 6. Error Handling

| Status | Common cause | Client action |
|---|---|---|
| `400` | Invalid cursor or invalid cursor offset | Restart search with `query` |
| `401` | Missing or invalid `X-API-Key` | Check the configured API key |
| `403` | API key does not match the endpoint slug | Use the correct company endpoint/key pair |
| `410` | Cursor expired | Restart search with `query` |
| `422` | Invalid request shape, blank query, invalid page size, unexpected field | Fix request payload |
| `429` | Company rate limit exceeded | Retry after `Retry-After` seconds |
| `503` | Runtime dependency unavailable | Retry later and alert API operator |

Recommended client behavior:

1. Treat `cursor` as short-lived and opaque.
2. On `410`, clear pagination state and repeat the original query.
3. On `429`, back off and honor the `Retry-After` header.
4. On `5xx`, show a temporary failure state and retry with jitter.

## 7. Usage Reporting

If usage tracking is enabled, companies can fetch monthly usage:

```bash
curl 'https://<api-domain>/api/v1/<company_endpoint>/usage?month=2026-07' \
  -H 'X-API-Key: <company-api-key>'
```

The `month` query parameter must be `YYYY-MM`. If omitted, the current UTC
month is used.

## 8. Admin Diagnostics

Admin endpoints are for the API operator, not company frontend clients. They
require:

```http
X-Admin-Key: <admin-key>
```

Recent privacy-safe search events:

```bash
curl 'https://<api-domain>/api/v1/<company_endpoint>/admin/search-events?limit=20' \
  -H 'X-Admin-Key: <admin-key>'
```

Only summaries are returned: query length, status, timings, route, model
provider labels, and stage timeline. Raw query text, filters, result payloads,
credentials, and private data are not returned.

Filter failed events:

```bash
curl 'https://<api-domain>/api/v1/<company_endpoint>/admin/search-events?status=failed&limit=20' \
  -H 'X-Admin-Key: <admin-key>'
```

## 9. Gainr Legacy Frontend Compatibility

Gainr currently has additional compatibility endpoints under:

```text
/api/v1/gainr
```

These endpoints preserve the existing Gainr frontend contract.

### 9.1 Search Suggestions

```http
POST /api/v1/gainr/search-suggestions
Content-Type: application/json
X-API-Key: <GAINR_API_KEY>
```

Request:

```json
{
  "term": "Bike"
}
```

Response:

```json
{
  "status": true,
  "data": [
    {
      "value": "Bike"
    },
    {
      "value": "Bike Cargo Rider"
    }
  ]
}
```

### 9.2 Filter Data

Call this after the user selects a city.

```http
POST /api/v1/gainr/filter-data
Content-Type: application/json
X-API-Key: <GAINR_API_KEY>
```

Request:

```json
{
  "city_id": 456
}
```

Response:

```json
{
  "data": {
    "rental_duration": {
      "title": "Duration",
      "value": ["Per Hour", "Per Day", "Per Week", "Per Month"]
    },
    "ad_type": {
      "title": "Ad Type",
      "value": [
        {"id": 1, "value": "Offer Ads"},
        {"id": 2, "value": "Need Ads"}
      ]
    },
    "fee": {
      "title": "Fee Type",
      "value": [
        {"id": 1, "value": "Fixed"},
        {"id": 0, "value": "Negotiable"}
      ]
    },
    "localityList": {
      "title": "Locality",
      "value": [
        {"id": 167889, "area": "Churchgate"}
      ]
    }
  }
}
```

### 9.3 Filter Result

Use this for the Gainr search results page and infinite scroll.

```http
POST /api/v1/gainr/filter-result
Content-Type: application/json
X-API-Key: <GAINR_API_KEY>
X-User-ID: <trusted-user-id>
```

`X-User-ID` is optional. Provide it only from trusted server/session context.
It is used for recent-search history.

Request:

```json
{
  "searchTerm": "bike in Chennai under 1000",
  "filter": {
    "city_id": 456,
    "subcategory_id": "",
    "locality_id": [167889],
    "rental_duration": ["Per Day"],
    "ad_type": [1],
    "fee": [1],
    "min_fee": 100,
    "max_fee": 1000
  },
  "page": 1
}
```

Response:

```json
{
  "status": true,
  "message": "",
  "data": [
    {
      "id": 235255,
      "type": 1,
      "user_id": 297587,
      "category_type": 1,
      "parent_id": 10,
      "category_id": 20,
      "title": "Bike",
      "rental_duration": "Per Day",
      "rental_fee": 750,
      "is_rent_negotiable": 0,
      "city_id": 456,
      "locality_id": 167889,
      "description": "Good condition bike",
      "photos": "bike.jpg",
      "total_favorite": 0,
      "total_like": 0,
      "status": 1,
      "service_ad_count": 1,
      "users_rating_count": null,
      "rating_avg": null,
      "boost_ad_count": 0,
      "is_aadhar_gst_verified_count": 0,
      "city": {
        "id": 456,
        "city": "Mumbai"
      },
      "locality": {
        "id": 167889,
        "area": "Churchgate"
      },
      "ads_attributes": [],
      "user": {
        "prosper_id": "BT6310",
        "id": 297587,
        "is_aadhaar_gst_verified": 1
      },
      "is_aadhar_gst_verified": null
    }
  ],
  "current_page": 1,
  "last_page": 3,
  "image_path": "https://gainr.in/uploads/post/"
}
```

Pagination uses `page`, not cursor. Repeat the same `searchTerm` and `filter`
payload with `page` incremented.

Filter rules:

1. Empty strings, `null`, and empty arrays mean "no filter".
2. Different filter groups are combined with `AND`.
3. Multiple values inside one filter group are combined with `OR`.
4. Explicit frontend filters override inferred natural-language filters.
5. `ad_type` supports `1` for offer ads and `2` for need ads.
6. `fee` supports configured fixed/negotiable IDs; Gainr uses `1` for fixed
   and `0` for negotiable.

### 9.4 Recent Search

```http
GET /api/v1/gainr/recent-search
X-API-Key: <GAINR_API_KEY>
X-User-ID: <trusted-user-id>
```

Response:

```json
{
  "status": true,
  "data": [
    {
      "id": 3951953,
      "value": "bike",
      "is_prosper": 0
    }
  ]
}
```

Recent searches are isolated by company and trusted user ID. Anonymous requests
return an empty list.

### 9.5 Recommended Gainr Frontend Flow

1. Set the frontend API base URL:

   ```env
   VITE_SEARCH_API_BASE_URL=https://<api-domain>/api/v1/gainr
   ```

2. On city selection, call `filter-data` with the selected `city_id`.
3. While typing, call `search-suggestions` after a 250-300 ms debounce.
4. On submit or suggestion click, call `filter-result` with `page: 1`.
5. On infinite scroll, repeat `filter-result` with the same search/filter state
   and increment `page`.
6. For signed-in users, add trusted `X-User-ID` from the server/session layer so
   recent searches are recorded and isolated.

## 10. Integration Checklist

Before launch, confirm:

- The company has an endpoint slug and API key.
- `GET /auth/verify` succeeds for that slug/key pair.
- `GET /health` reports `status: "ok"` and the expected indexed count.
- First-page search succeeds with the exact frontend payload shape.
- Pagination works with either cursor or page, depending on the chosen API.
- The frontend handles `401`, `403`, `410`, `422`, `429`, and `5xx` responses.
- Public result fields match what the frontend renders.
- No secret API key is exposed in public client code unless intentionally
  mediated by deployment architecture.
- For Gainr compatibility, `filter-data`, `search-suggestions`,
  `filter-result`, and `recent-search` match the legacy frontend contract.

## 11. OpenAPI Schema

FastAPI also exposes interactive and machine-readable documentation while the
service is running:

```text
GET /docs
GET /openapi.json
```

Protect or disable those routes at the reverse proxy if deployment policy
requires it.
