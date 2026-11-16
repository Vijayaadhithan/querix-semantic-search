# Code quality and repository structure

This document defines the repository's maintainability checks and the boundary
for safe refactoring. Search and analytics are separate products and separate
Docker images; shared infrastructure must not turn them into a single runtime.

## Package ownership

| Directory | Responsibility |
|---|---|
| `src/api/` | Search HTTP contracts, routes, lifecycle, and tenant service pool |
| `src/analytics_service/` | Snapshot refresh, analytics authentication, and analytics HTTP API |
| `src/search/` | Query planning, retrieval, ranking, and search orchestration |
| `src/storage/` | Database, pgvector, Redis, usage, and search-history adapters |
| `src/verticals/<vertical>/` | Reusable domain behavior shared by tenants |
| `src/tenants/<tenant>/` | Client-specific compatibility contracts and policies |
| `src/core/` | Validated process configuration and tenant configuration |
| `src/operations/` | Reusable workload configuration, without CLI/file-I/O coupling |
| `scripts/` | Operator commands and repository validation utilities |
| `tests/` | Tests arranged to mirror runtime package ownership |

Reusable domain behavior belongs under `src/verticals/<vertical>/`; client-only
behavior belongs under `src/tenants/<tenant>/`. Generic search or analytics
logic must not import from a client frontend repository or from a specific
tenant package. Secrets, generated credentials, SQLite files, caches, and raw
source data stay outside Git and outside Docker build contexts.

## Required local and CI checks

Run the same deterministic checks before every push:

```bash
uv lock --check
uv sync --frozen --group analytics
uv run --frozen --group analytics ruff check src scripts tests
uv run --frozen --group analytics ruff format --check src scripts tests
uv run --frozen --group analytics python scripts/check_markdown.py
uv run --frozen --group analytics python -m compileall -q src scripts tests
uv run --frozen --group analytics pytest -q
python3 scripts/render_service_env.py
docker compose config --quiet
git diff --check
```

Ruff enforces import ordering, unused imports and variables, critical syntax
errors, 88-column formatting, Python 3.12 modernization, safe simplifications,
and stale suppression comments. The Markdown checker validates every
repository-local documentation target and heading anchor. External URLs are not
called during CI, keeping the gate deterministic and independent of third-party
availability.

## Dead code policy

An import is not removed solely because a static tool cannot see a dynamic
consumer. Compatibility exports and test seams must be made explicit with
`__all__` or a narrowly scoped suppression and a reason. Everything else that
has no runtime, test, CLI, or documented integration consumer should be removed
with its obsolete tests and documentation in the same change.

Generated `__pycache__`, test caches, local databases, credentials, benchmark
output, and operating-system metadata are ignored. They are not source code and
must never be committed.

## Large-file and refactoring policy

File size is an audit signal, not an automatic rewrite trigger. Split a module
when it owns multiple unrelated responsibilities, changes for unrelated
reasons, or cannot be tested through stable public boundaries. Preserve public
imports and HTTP contracts during incremental moves.

Large test files may remain cohesive when they exercise one public component,
but new tests should be placed in the matching package directory. Large runtime
modules should delegate pure calculations, storage access, or role-specific
routes to focused modules rather than adding more responsibilities to an
application factory.

Refactors must pass the full gate and should not trigger ingestion, index
rebuilds, migrations, or unrelated container recreation unless the changed
contract explicitly requires one.

## Refactoring boundaries

The analytics application factory wires dependencies and middleware. HTTP
handlers live in `src/analytics_service/routes/`, grouped by authentication,
company, internal, and system responsibilities. Shared portal authorization
and cookie policy live in `access.py`; prepared dashboard views, fallback
calculations, and query responses live in `dashboard.py`. Each application owns
its dashboard cache and dependencies, keeping tenant and test instances isolated.

Both analytics stores use `src/analytics_service/sqlite.py` for connection
configuration and cleanup. Connections use autocommit; callers retain ownership
of explicit transactions. Cleanup also runs when connection configuration fails.

Search orchestration stays in `src/api/service.py`. Pure operator timelines and
telemetry projections live in `monitoring.py` and `telemetry.py`; existing service
methods delegate to them. MySQL telemetry DDL and schema checks live in
`src/storage/search_analytics_schema.py`, with public provisioning functions
re-exported from `search_analytics.py` for existing operator commands.

The cleanup removed an uncalled dashboard count method, the unused default
adapter alias, and obsolete dashboard-output and gibberish-pattern settings.
Framework callbacks, Pydantic validators, protocol methods, and documented
compatibility exports remain even when text searches find no direct callers.

The [repository-wide audit](repository_audit.md) records the completed source,
script, test, and deployment-configuration review, extracted responsibilities,
validation evidence, and reasons for retaining large cohesive components. Public
facades use explicit exports so moving implementations does not break existing
imports or test seams. Further changes should follow these ownership boundaries
and validate public schemas as well as runtime behavior.
