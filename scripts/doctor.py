import argparse
import os
import sqlite3
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mysql_store import mysql_connection, quote_mysql_identifier
from postgres_store import (
    PostgresRuntimeConfig,
    postgres_connection,
    qualified_table,
)
from settings import (
    API_ADMIN_KEY,
    API_AUTH_ENABLED,
    API_CORS_ORIGINS,
    API_TENANT_ENGINE_CACHE_SIZE,
    API_TENANT_MAX_CONCURRENT_SEARCHES,
    EMBED_MODEL,
    GEMINI_API_KEY,
    MYSQL_RESULT_TABLE,
    MYSQL_TABLE,
    OLLAMA_BASE_URL,
    OPENROUTER_API_KEY,
    RERANK_PROVIDER_ORDER,
    REDIS_ENABLED,
    REDIS_URL,
    VOYAGE_API_KEY,
)
from tenant_config import discover_tenant_profiles
from vector_store import get_tenant_vector_collection


def report(name: str, ok: bool, detail: str) -> bool:
    state = "OK" if ok else "FAIL"
    print(f"[{state}] {name}: {detail}")
    return ok


def production_database_tls_status(mode: str) -> tuple[bool, str]:
    detail = {
        "require": "encrypted; server identity is not verified",
        "verify-ca": "encrypted; CA verified",
        "verify-full": "encrypted; CA and hostname verified",
    }.get(mode)
    if detail is None:
        return False, f"tls.mode={mode!r}; expected require or stronger"
    return True, detail


def check_ollama() -> bool:
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        response.raise_for_status()
        models = {
            item.get("name")
            for item in response.json().get("models", [])
        }
    except (requests.RequestException, ValueError) as exc:
        return report("Ollama", False, type(exc).__name__)
    installed = EMBED_MODEL in models
    return report(
        "Ollama",
        installed,
        (
            f"{EMBED_MODEL} installed"
            if installed
            else f"missing {EMBED_MODEL}; run: ollama pull {EMBED_MODEL}"
        ),
    )


def check_redis() -> bool:
    if not REDIS_ENABLED:
        return report("Redis", True, "disabled; process-memory fallback active")
    try:
        import redis

        client = redis.Redis.from_url(
            REDIS_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        connected = bool(client.ping())
        client.close()
    except Exception as exc:
        return report("Redis", False, type(exc).__name__)
    return report("Redis", connected, "connected")


def check_database(profile=None) -> bool:
    config = profile.database if profile else None
    search_table = config.search_table if config else MYSQL_TABLE
    result_table = config.result_table if config else MYSQL_RESULT_TABLE
    try:
        if isinstance(config, PostgresRuntimeConfig):
            connection_context = postgres_connection(config)
        else:
            connection_context = mysql_connection(config=config)
        with connection_context as connection:
            with connection.cursor() as cursor:
                tables = []
                for table in (search_table, result_table):
                    if isinstance(config, PostgresRuntimeConfig):
                        qualified = qualified_table(config, table)
                    else:
                        qualified = quote_mysql_identifier(table)
                    cursor.execute(f"SELECT COUNT(*) FROM {qualified}")
                    tables.append(f"{table}={int(cursor.fetchone()[0])}")
    except Exception as exc:
        return report(
            "Company database",
            False,
            f"{type(exc).__name__}: {exc}",
        )
    return report("Company database", True, ", ".join(tables))


def check_vectors(profile=None) -> bool:
    try:
        collection = get_tenant_vector_collection(profile)
        backend = profile.storage.vector_backend
        count = int(collection.count())
    except Exception as exc:
        return report("Vector index", False, type(exc).__name__)
    return report("Vector index", count > 0, f"{backend}, {count} vectors")


def check_reranker() -> bool:
    available = []
    for provider in RERANK_PROVIDER_ORDER:
        if provider in {"voyage", "voyage-2.5", "voyage-2.5-lite"}:
            if VOYAGE_API_KEY:
                available.append(
                    "voyage-2.5" if provider == "voyage" else provider
                )
        elif provider == "openrouter-nemotron" and OPENROUTER_API_KEY:
            available.append(provider)
    return report(
        "Reranker chain",
        bool(available),
        " -> ".join(available) if available else "no usable provider",
    )


def check_bm25(profile=None) -> bool:
    path = profile.storage.bm25_path
    if not path.exists():
        return report(
            "BM25 index",
            False,
            "missing; run src/ingest.py --company <slug> --database",
        )
    try:
        uri = f"file:{path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            count = int(
                connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            )
    except (sqlite3.Error, TypeError) as exc:
        return report("BM25 index", False, type(exc).__name__)
    return report("BM25 index", count > 0, f"{count} indexed products")


def check_production_security(profile=None) -> list[bool]:
    config = profile.database if profile else None
    cors_ok = bool(API_CORS_ORIGINS) and "*" not in API_CORS_ORIGINS
    checks = [
        report(
            "Production auth",
            API_AUTH_ENABLED,
            "enabled" if API_AUTH_ENABLED else "API_AUTH_ENABLED=false",
        ),
        report(
            "Admin API key",
            bool(API_ADMIN_KEY),
            "configured" if API_ADMIN_KEY else "missing API_ADMIN_KEY",
        ),
        report(
            "CORS origins",
            cors_ok,
            (
                ",".join(API_CORS_ORIGINS)
                if API_CORS_ORIGINS
                else "missing API_CORS_ORIGINS"
            ),
        ),
        report(
            "Redis production mode",
            REDIS_ENABLED,
            "enabled" if REDIS_ENABLED else "REDIS_ENABLED=false",
        ),
        report(
            "8 GB tenant cache limit",
            API_TENANT_ENGINE_CACHE_SIZE == 1,
            f"API_TENANT_ENGINE_CACHE_SIZE={API_TENANT_ENGINE_CACHE_SIZE}",
        ),
        report(
            "8 GB search concurrency limit",
            API_TENANT_MAX_CONCURRENT_SEARCHES == 1,
            (
                "API_TENANT_MAX_CONCURRENT_SEARCHES="
                f"{API_TENANT_MAX_CONCURRENT_SEARCHES}"
            ),
        ),
    ]
    if config is not None:
        tls_ok, tls_detail = production_database_tls_status(config.tls_mode)
        checks.append(
            report(
                "Company database TLS",
                tls_ok,
                tls_detail,
            )
        )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check local search infrastructure without exposing secrets."
    )
    parser.add_argument(
        "--company",
        required=True,
        help="tenant profile slug under configs/tenants",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any check fails",
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help=(
            "also verify production auth, admin key, CORS, Redis, TLS, "
            "and 8 GB single-host guardrails"
        ),
    )
    args = parser.parse_args()
    profiles = discover_tenant_profiles()
    try:
        profile = profiles[args.company]
    except KeyError:
        available = ", ".join(sorted(profiles)) or "none"
        print(f"Unknown company {args.company!r}; available: {available}")
        return 1

    print(f"Python: {sys.version.split()[0]}")
    if profile:
        print(f"Company: {profile.company_id}")
    api_key_configured = (
        any(
            os.getenv(name, "").strip()
            for name in profile.api_key_envs
        )
        if profile
        else True
    )
    checks = [
        report(
            "Company API key",
            not API_AUTH_ENABLED or api_key_configured,
            (
                "configured"
                if api_key_configured
                else (
                    "not required while API_AUTH_ENABLED=false"
                    if not API_AUTH_ENABLED
                    else "missing for authenticated tenant mode"
                )
            ),
        ),
        report(
            "Gemini API key",
            bool(GEMINI_API_KEY),
            "configured" if GEMINI_API_KEY else "missing in .env",
        ),
        check_ollama(),
        check_redis(),
        check_reranker(),
        check_database(profile),
        check_vectors(profile),
        check_bm25(profile),
    ]
    if args.production:
        checks.extend(check_production_security(profile))
    failed = checks.count(False)
    print(f"Doctor summary: {len(checks) - failed} passed, {failed} failed.")
    return 1 if args.strict and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
