"""Physical index names shared by configuration validation and ingestion."""

from __future__ import annotations

import hashlib
from pathlib import Path


def shadow_vector_table(base: str) -> str:
    if len(base) + 3 <= 63:
        return f"{base}__b"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    return f"{base[:51]}_{digest}_b"


def shadow_bm25_path(base: Path) -> Path:
    return base.parent / "generations" / "b" / base.name


def index_manifest_path(base: Path) -> Path:
    return base.parent / "index-generations.json"
