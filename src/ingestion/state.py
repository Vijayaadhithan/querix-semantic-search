from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ingestion_state_path(bm25_path: Path | str) -> Path:
    return Path(bm25_path).parent / ".ingestion-state.json"


def ingestion_manifest_path(bm25_path: Path | str) -> Path:
    return Path(bm25_path).parent / "ingestion-manifest.json"


def read_ingestion_state(path: Path | str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    state_path = Path(path)
    if not state_path.exists():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "invalid"}
    return payload if isinstance(payload, dict) else {"status": "invalid"}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def begin_ingestion(path: Path, *, company_id: str) -> None:
    previous = read_ingestion_state(path) or {}
    _atomic_write(
        path,
        {
            "run_id": previous.get("run_id") or uuid4().hex,
            "company_id": company_id,
            "status": "running",
            "started_at": previous.get("started_at") or utc_now(),
            "updated_at": utc_now(),
        },
    )


def fail_ingestion(path: Path, error: Exception) -> None:
    payload = read_ingestion_state(path) or {}
    payload.update(
        {
            "status": "failed",
            "updated_at": utc_now(),
            "error_type": type(error).__name__,
            "error": str(error),
        }
    )
    _atomic_write(path, payload)


def complete_ingestion(path: Path) -> None:
    payload = read_ingestion_state(path) or {}
    payload.update({"status": "complete", "finished_at": utc_now()})
    _atomic_write(path.parent / "ingestion-manifest.json", payload)
    path.unlink(missing_ok=True)
