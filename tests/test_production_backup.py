from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_backup_includes_read_only_company_mysql_dump():
    script = (ROOT / "scripts" / "backup_production.sh").read_text()

    assert "--single-transaction" in script
    assert "--skip-lock-tables" in script
    assert "analytics-api sh -eu -c" in script
    assert "company-mysql.sql" in script
    assert "company-mysql.sql pgvector.dump storage-sqlite.tar.gz" in script
    assert "DROP DATABASE" not in script
    assert "CREATE DATABASE" not in script


def test_analytics_image_contains_mysql_dump_client():
    dockerfile = (ROOT / "Dockerfile.analytics").read_text()

    assert "default-mysql-client" in dockerfile
