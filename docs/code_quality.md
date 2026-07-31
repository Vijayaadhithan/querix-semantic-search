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
| `src/tenants/<company>/` | Company-specific compatibility contracts and policies |
| `src/core/` | Validated process configuration and tenant configuration |
| `scripts/` | Operator commands and repository validation utilities |
| `tests/` | Tests arranged to mirror runtime package ownership |

New company-specific behavior belongs under `src/tenants/<company>/`. Generic
search or analytics logic must not import from a company frontend repository.
Secrets, generated credentials, SQLite files, caches, and raw source data stay
outside Git and outside Docker build contexts.

## Required local and CI checks

Run the same deterministic checks before every push:

```bash
uv lock --check
uv sync --frozen --group analytics
uv run --frozen --group analytics ruff check src scripts tests
uv run --frozen --group analytics python scripts/check_markdown.py
uv run --frozen --group analytics python -m compileall -q src scripts tests
uv run --frozen --group analytics pytest -q
docker compose config --quiet
git diff --check
```

Ruff enforces import ordering, unused imports and variables, critical syntax
errors, and stale suppression comments. The Markdown checker validates every
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
