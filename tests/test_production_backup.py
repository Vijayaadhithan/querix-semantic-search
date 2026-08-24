from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_backup_excludes_company_mysql_dump():
    script = (ROOT / "scripts" / "backup_production.sh").read_text()

    assert "company-mysql.sql" not in script
    assert "mariadb-dump" not in script
    assert "mysqldump" not in script
    assert "sha256sum pgvector.dump storage-sqlite.tar.gz" in script


def test_analytics_image_does_not_install_mysql_dump_client():
    dockerfile = (ROOT / "Dockerfile.analytics").read_text()

    assert "default-mysql-client" not in dockerfile
