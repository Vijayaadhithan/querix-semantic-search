import argparse
import sqlite3
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mysql_store import mysql_connection, quote_mysql_identifier
from settings import (
    BM25_INDEX_PATH,
    EMBED_MODEL,
    GEMINI_API_KEY,
    MYSQL_RESULT_TABLE,
    MYSQL_TABLE,
    OLLAMA_BASE_URL,
    REDIS_ENABLED,
    REDIS_URL,
)


def report(name: str, ok: bool, detail: str) -> bool:
    state = "OK" if ok else "FAIL"
    print(f"[{state}] {name}: {detail}")
    return ok


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


def check_mysql() -> bool:
    try:
        with mysql_connection() as connection:
            with connection.cursor() as cursor:
                tables = []
                for table in (MYSQL_TABLE, MYSQL_RESULT_TABLE):
                    cursor.execute(
                        f"SELECT COUNT(*) FROM {quote_mysql_identifier(table)}"
                    )
                    tables.append(f"{table}={int(cursor.fetchone()[0])}")
    except Exception as exc:
        return report("MySQL", False, type(exc).__name__)
    return report("MySQL", True, ", ".join(tables))


def check_bm25() -> bool:
    if not BM25_INDEX_PATH.exists():
        return report(
            "BM25 index",
            False,
            "missing; run src/ingest.py --mysql",
        )
    try:
        uri = f"file:{BM25_INDEX_PATH}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            count = int(
                connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            )
    except (sqlite3.Error, TypeError) as exc:
        return report("BM25 index", False, type(exc).__name__)
    return report("BM25 index", count > 0, f"{count} indexed products")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check local search infrastructure without exposing secrets."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any check fails",
    )
    args = parser.parse_args()

    print(f"Python: {sys.version.split()[0]}")
    checks = [
        report(
            "Gemini API key",
            bool(GEMINI_API_KEY),
            "configured" if GEMINI_API_KEY else "missing in .env",
        ),
        check_ollama(),
        check_redis(),
        check_mysql(),
        check_bm25(),
    ]
    failed = checks.count(False)
    print(f"Doctor summary: {len(checks) - failed} passed, {failed} failed.")
    return 1 if args.strict and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
