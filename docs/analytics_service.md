# Daily company analytics service

The analytics backend is a separate deployable service. It is not imported by
the semantic-search API image and it never calculates dashboards on an HTTP
request.

## Service and image

Docker Compose service: `analytics-api`.

Dockerfile: `Dockerfile.analytics`.

Default local address: `http://127.0.0.1:8010`.

The image has two entrypoints:

```bash
# Long-running read-only snapshot API.
python -m analytics_service

# One-shot daily SQL extraction, calculation, and atomic publication.
python -m analytics_service.refresh --company gainr
```

The snapshot database defaults to
`storage/analytics/snapshots.sqlite3`. It uses WAL mode, versioned query
records, and an atomic active-version switch. A failed refresh records a
failed run but does not replace or delete the last completed dashboard.

## Data and audience boundaries

Company dashboard modules:

- Search Intelligence (13 curated metrics)
- Individual Queries
- Deep Analytics (10 curated metrics)
- Market Intelligence (8 curated metrics)

Internal dashboard modules:

- the same curated company modules for one explicitly selected company;
- API Performance (15 curated metrics);
- provider and model attempts;
- token usage;
- failures, fallbacks, execution paths, and latency;

The internal service does not aggregate analytics across companies. The
companies endpoint is only a tenant/snapshot inventory. Every dashboard and
query request names exactly one company.

The original exploratory reports remain in the domain source for future
validation, but only the curated catalogue in `metrics.py` is persisted or
returned. Misleading estimates, duplicated reports, provider-specific reports,
and niche Gainr-only questions are excluded.

Company query records include query classification and search outcome. They do
not include provider names, model names, execution paths, tokens, attempts,
failure reasons, or internal latency diagnostics. Internal query records retain
the full operational projection.

The analytics service reads business data and tenant-facing query history from
the company database. API telemetry can use the company database initially or
a separate internal database. `request_id` correlates the two sources.

## Daily refresh

All modules refresh once per day. The production timer starts
`scripts/run_scheduled_ingestion.sh` at approximately 03:00 Asia/Kolkata.
That script now performs these operations in order:

1. upload pending search analytics;
2. build and atomically publish the company analytics snapshot;
3. prune expired analytics login sessions;
4. run search ingestion;
5. restart and warm the search API.

If step 2 fails, the script logs the failure, keeps the prior snapshot active,
and continues search ingestion.

The production deployment builds both images. It performs an analytics refresh
only when no prior snapshot exists, then starts the analytics API and verifies
its readiness. Ordinary deployments do not recalculate an existing snapshot.

## API endpoints

Company endpoints accept either a company-bound analytics login session or the
tenant's `X-API-Key`. The API-key path exists for server-to-server callers; a
browser frontend must use the login session and must not contain the API key.
Analytics uses a separate product key such as `GAINR_ANALYTICS_API_KEY`; it
does not reuse or alter the semantic-search API's `GAINR_API_KEY`.

```text
GET /api/v1/{company}/analytics/dashboard
GET /api/v1/{company}/analytics/queries
GET /api/v1/{company}/analytics/status
```

Authentication endpoints:

```text
POST /api/v1/analytics/auth/login
GET  /api/v1/analytics/auth/me
POST /api/v1/analytics/auth/logout
```

Internal endpoints require an `internal_admin` analytics session:

```text
GET /api/v1/admin/analytics/companies
GET /api/v1/admin/analytics/{company}/dashboard
GET /api/v1/admin/analytics/{company}/queries
```

Individual-query filters:

```text
limit
cursor
query
outcome
category
language
from
to
```

The query endpoint uses a stable descending `(created_at, request_id)` cursor.
It never uses offset pagination.

Analytics responses set `Cache-Control: private, no-store` and must remain
behind authenticated TLS termination.

Example:

```bash
curl -fsS \
  "http://127.0.0.1:8010/api/v1/gainr/analytics/queries?outcome=zero_result&limit=50" \
  -H "X-API-Key: $GAINR_ANALYTICS_API_KEY" | jq
```

Create the first internal user interactively:

```console
docker compose run --rm analytics-api \
  python -m analytics_service.users create \
    --username analytics-admin \
    --role internal_admin
```

Create a company-bound dashboard user:

```console
docker compose run --rm analytics-api \
  python -m analytics_service.users create \
    --username gainr-owner \
    --role company_user \
    --company gainr
```

Passwords are read from a hidden interactive prompt, never from the command
line. `--password-stdin` is available for secret-manager automation. User
management also supports `list`, `set-password`, `set-active`, and
`prune-sessions`.

For local HTTP only, set `ANALYTICS_SESSION_COOKIE_SECURE=false`. Production
must use `true` behind HTTPS. Login returns an opaque server-side session in an
`HttpOnly`, `SameSite=Strict` cookie. Passwords use salted scrypt hashes;
sessions are stored only as SHA-256 token digests. Five failed logins lock the
account for 15 minutes by default. Password changes and account disablement
revoke active sessions.

Browser login example:

```console
curl -c /tmp/querix-analytics-cookies \
  -H "Content-Type: application/json" \
  -d '{"username":"gainr-owner","password":"your password"}' \
  http://127.0.0.1:8010/api/v1/analytics/auth/login

curl -b /tmp/querix-analytics-cookies \
  http://127.0.0.1:8010/api/v1/gainr/analytics/dashboard
```

Internal users log in through the same endpoint, then call one company-scoped
internal route:

```console
curl -b /tmp/querix-analytics-cookies \
  http://127.0.0.1:8010/api/v1/admin/analytics/gainr/dashboard
```

## Tenant SQL configuration

Analytics-enabled tenant YAML files define normalized source tables:

```yaml
analytics:
  enabled: true
  endpoint_slug: gainr
  api_key_envs:
    - GAINR_ANALYTICS_API_KEY
  history_days: 90
  tables:
    search_history: semantic_search_history
    api_usage: semantic_search_api_usage
    ads: ads
    users: users
    categories: categories
    sub_categories: sub_categories
    states: states
    location: location
    attributes: attributes
    attribute_values: attribute_values
    ads_attributes: ads_attributes
  telemetry:
    use_company_database: true
```

For a company with different source column names, add canonical-to-source
mappings:

```yaml
analytics:
  columns:
    ads:
      id: product_id
      user_id: seller_id
      category_id: subcategory_id
      title: product_name
      rental_fee: price
      created_at: listed_at
```

The SQL loader selects only the columns used by the analytics contract.
MySQL and PostgreSQL company sources are supported. Source credentials should
be read-only because the daily builder does not modify company tables.
`history_days` limits query history and API telemetry at the SQL source; the
catalogue and user tables remain available for longer market trends.

To move API telemetry to an internal database:

```yaml
analytics:
  telemetry:
    use_company_database: false
    database:
      backend: postgres
      host_env: GAINR_ANALYTICS_HOST
      port_env: GAINR_ANALYTICS_PORT
      database_env: GAINR_ANALYTICS_DATABASE
      user_env: GAINR_ANALYTICS_USER
      password_env: GAINR_ANALYTICS_PASSWORD
      schema: public
      tls:
        mode: verify-full
        ca_file_env: GAINR_ANALYTICS_TLS_CA_FILE
```

## Local verification

Build the standalone image:

```bash
docker compose build analytics-api
```

Test with the existing standalone CSV export:

```bash
docker compose run --rm --no-deps \
  -v /absolute/path/to/Analytrics:/analytics-data:ro \
  -e ANALYTICS_SNAPSHOT_DB_PATH=/tmp/analytics-test.sqlite3 \
  analytics-api \
  python -m analytics_service.refresh \
    --company gainr \
    --csv-data-dir /analytics-data
```

Run against the configured company SQL database and publish to persistent
storage:

```bash
docker compose run --rm --no-deps analytics-api \
  python -m analytics_service.refresh --company gainr
```

Start the API:

```bash
docker compose up -d --no-deps analytics-api
curl -fsS http://127.0.0.1:8010/api/v1/ready | jq
```

The service reports `not_ready` until every configured analytics company has a
completed snapshot.

## Snapshot backup

`scripts/backup_production.sh` already backs up every SQLite database below
`storage/` through SQLite's online backup API. The analytics snapshot is
therefore included without adding another backup mechanism.
