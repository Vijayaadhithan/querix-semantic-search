"""Versioned per-tenant search-index generations.

The configured pgvector table and BM25 path are slot ``a`` for backwards
compatibility. Slot ``b`` is derived deterministically. A small atomically
written manifest selects the active slot; ingestion may mutate the inactive
slot without placing the serving slot behind its consistency gate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.tenant_config import TenantProfile

MANIFEST_SCHEMA_VERSION = 1
SLOTS = ("a", "b")


@dataclass(frozen=True)
class ResolvedIndexGeneration:
    slot: str
    generation: str
    profile: TenantProfile
    manifest_path: Path


def generation_manifest_path(profile: TenantProfile) -> Path:
    return profile.storage.bm25_path.parent / "index-generations.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _slot_b_table(base: str) -> str:
    suffix = "__b"
    if len(base) + len(suffix) <= 63:
        return f"{base}{suffix}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    return f"{base[:51]}_{digest}_b"


def profile_for_slot(profile: TenantProfile, slot: str) -> TenantProfile:
    if slot not in SLOTS:
        raise ValueError(f"Unsupported index-generation slot: {slot!r}")
    if slot == "a":
        storage = profile.storage
    else:
        base_path = profile.storage.bm25_path
        storage = replace(
            profile.storage,
            pgvector_table=_slot_b_table(profile.storage.pgvector_table),
            bm25_path=(base_path.parent / "generations" / "b" / base_path.name),
        )
    return replace(profile, storage=storage)


def default_generation_state(profile: TenantProfile) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "company_id": profile.company_id,
        "active_slot": "a",
        "updated_at": _utc_now(),
        "slots": {
            "a": {
                "generation": "legacy-a",
                "status": "active",
            },
            "b": {
                "generation": None,
                "status": "empty",
            },
        },
    }


def _validate_state(profile: TenantProfile, state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise RuntimeError("Index-generation manifest must be a JSON object.")
    if state.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise RuntimeError("Unsupported index-generation manifest schema.")
    if state.get("company_id") != profile.company_id:
        raise RuntimeError("Index-generation manifest company does not match tenant.")
    if state.get("active_slot") not in SLOTS:
        raise RuntimeError("Index-generation manifest has an invalid active slot.")
    slots = state.get("slots")
    if not isinstance(slots, dict) or any(slot not in slots for slot in SLOTS):
        raise RuntimeError("Index-generation manifest is missing a slot.")
    return state


def read_generation_state(profile: TenantProfile) -> dict[str, Any]:
    path = generation_manifest_path(profile)
    if not path.exists():
        return default_generation_state(profile)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read index-generation manifest: {path}") from exc
    return _validate_state(profile, state)


def _atomic_write(profile: TenantProfile, state: dict[str, Any]) -> None:
    path = generation_manifest_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def ensure_generation_state(profile: TenantProfile) -> dict[str, Any]:
    path = generation_manifest_path(profile)
    state = read_generation_state(profile)
    if not path.exists():
        _atomic_write(profile, state)
    return state


def resolve_generation(
    profile: TenantProfile,
    slot: str | None = None,
) -> ResolvedIndexGeneration:
    state = read_generation_state(profile)
    selected = slot or str(state["active_slot"])
    if selected not in SLOTS:
        raise RuntimeError(f"Unknown index-generation slot {selected!r}.")
    slot_state = state["slots"][selected]
    generation = str(slot_state.get("generation") or f"uninitialized-{selected}")
    return ResolvedIndexGeneration(
        slot=selected,
        generation=generation,
        profile=profile_for_slot(profile, selected),
        manifest_path=generation_manifest_path(profile),
    )


def candidate_generation(profile: TenantProfile) -> ResolvedIndexGeneration:
    state = read_generation_state(profile)
    slot = "b" if state["active_slot"] == "a" else "a"
    return resolve_generation(profile, slot)


def record_candidate_ready(
    profile: TenantProfile,
    *,
    slot: str,
    generation: str,
    validation: dict[str, Any],
) -> dict[str, Any]:
    state = ensure_generation_state(profile)
    if slot == state["active_slot"]:
        raise RuntimeError("Cannot mark the active index slot as a candidate.")
    updated = json.loads(json.dumps(state))
    updated["slots"][slot] = {
        "generation": generation,
        "status": "ready",
        "validated_at": _utc_now(),
        "validation": validation,
    }
    updated["updated_at"] = _utc_now()
    _atomic_write(profile, updated)
    return updated


def promote_candidate(
    profile: TenantProfile,
    *,
    slot: str,
    generation: str,
) -> dict[str, Any]:
    state = ensure_generation_state(profile)
    previous = str(state["active_slot"])
    candidate = state["slots"].get(slot, {})
    if slot == previous:
        raise RuntimeError("Candidate slot is already active.")
    if candidate.get("status") != "ready":
        raise RuntimeError("Candidate index generation has not passed validation.")
    if candidate.get("generation") != generation:
        raise RuntimeError("Candidate generation changed before promotion.")
    updated = json.loads(json.dumps(state))
    updated["active_slot"] = slot
    updated["previous_slot"] = previous
    updated["promoted_at"] = _utc_now()
    updated["updated_at"] = updated["promoted_at"]
    updated["slots"][slot]["status"] = "active"
    updated["slots"][previous]["status"] = "standby"
    _atomic_write(profile, updated)
    return updated


def restore_active_slot(profile: TenantProfile, slot: str) -> dict[str, Any]:
    """Restore a previous active pointer after a failed live reload."""
    if slot not in SLOTS:
        raise ValueError(f"Unsupported index-generation slot: {slot!r}")
    state = ensure_generation_state(profile)
    updated = json.loads(json.dumps(state))
    current = str(updated["active_slot"])
    updated["active_slot"] = slot
    updated["previous_slot"] = current
    updated["updated_at"] = _utc_now()
    updated["slots"][slot]["status"] = "active"
    if current != slot:
        updated["slots"][current]["status"] = "ready"
    _atomic_write(profile, updated)
    return updated


def seed_candidate_from_active(profile: TenantProfile) -> dict[str, Any]:
    """Create the inactive physical indexes from the active generation once."""
    from storage.vector import get_tenant_vector_collection

    active = resolve_generation(profile)
    candidate = candidate_generation(profile)
    vector_config = candidate.profile.storage.pgvector_database
    if vector_config is None:
        raise RuntimeError("Candidate generation has no pgvector database config.")

    from storage.pgvector import PgVectorCollection

    state = ensure_generation_state(profile)
    candidate_state = state["slots"][candidate.slot]
    vector_exists = PgVectorCollection.table_exists(
        vector_config,
        candidate.profile.storage.pgvector_table,
    )
    bm25_exists = candidate.profile.storage.bm25_path.is_file()
    if vector_exists != bm25_exists:
        if candidate_state.get("status") != "empty":
            raise RuntimeError(
                "Candidate generation is incomplete: pgvector and BM25 "
                "existence differ."
            )
        if vector_exists:
            PgVectorCollection.drop_table(
                vector_config,
                candidate.profile.storage.pgvector_table,
            )
        if bm25_exists:
            candidate.profile.storage.bm25_path.unlink(missing_ok=True)
        vector_exists = False
        bm25_exists = False
    if vector_exists:
        return {
            "seeded": False,
            "slot": candidate.slot,
            "vectors": get_tenant_vector_collection(
                candidate.profile,
                create=False,
            ).count(),
        }

    active_collection = get_tenant_vector_collection(active.profile, create=False)
    candidate_collection = active_collection.clone_to(
        candidate.profile.storage.pgvector_table
    )
    source_bm25 = active.profile.storage.bm25_path
    if not source_bm25.is_file():
        raise RuntimeError(f"Active BM25 index does not exist: {source_bm25}")
    candidate_bm25 = candidate.profile.storage.bm25_path
    candidate_bm25.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source_bm25.resolve()}?mode=ro"
    with (
        sqlite3.connect(source_uri, uri=True) as source,
        sqlite3.connect(candidate_bm25) as destination,
    ):
        source.backup(destination)
    return {
        "seeded": True,
        "slot": candidate.slot,
        "vectors": candidate_collection.count(),
    }
