from storage.index_generations import (
    candidate_generation,
    generation_manifest_path,
    promote_candidate,
    read_generation_state,
    record_candidate_ready,
    resolve_generation,
    restore_active_slot,
)


def test_generation_manifest_defaults_to_existing_storage(tmp_path):
    from core.tenant_config import TenantStorageConfig

    storage = TenantStorageConfig(
        bm25_path=tmp_path / "company" / "bm25.sqlite3",
        pgvector_table="company_search_vectors",
    )
    # A minimal dataclass wrapper keeps the test independent of API/database
    # profile construction.
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Profile:
        company_id: str
        storage: TenantStorageConfig

    profile = Profile("company", storage)

    state = read_generation_state(profile)  # type: ignore[arg-type]
    active = resolve_generation(profile)  # type: ignore[arg-type]
    candidate = candidate_generation(profile)  # type: ignore[arg-type]

    assert state["active_slot"] == "a"
    assert not generation_manifest_path(profile).exists()  # type: ignore[arg-type]
    assert active.profile.storage.bm25_path == storage.bm25_path
    assert active.profile.storage.pgvector_table == "company_search_vectors"
    assert candidate.slot == "b"
    assert candidate.profile.storage.bm25_path == (
        storage.bm25_path.parent / "generations" / "b" / "bm25.sqlite3"
    )
    assert candidate.profile.storage.pgvector_table == "company_search_vectors__b"


def test_candidate_promotion_and_restore_are_atomic_state_transitions(tmp_path):
    from dataclasses import dataclass

    from core.tenant_config import TenantStorageConfig

    @dataclass(frozen=True)
    class Profile:
        company_id: str
        storage: TenantStorageConfig

    profile = Profile(
        "company",
        TenantStorageConfig(
            bm25_path=tmp_path / "company" / "bm25.sqlite3",
            pgvector_table="company_search_vectors",
        ),
    )

    record_candidate_ready(
        profile,  # type: ignore[arg-type]
        slot="b",
        generation="run-1",
        validation={"source_rows": 10, "vectors": 10, "bm25": 10},
    )
    promoted = promote_candidate(
        profile,  # type: ignore[arg-type]
        slot="b",
        generation="run-1",
    )

    assert promoted["active_slot"] == "b"
    assert resolve_generation(profile).generation == "run-1"  # type: ignore[arg-type]
    assert candidate_generation(profile).slot == "a"  # type: ignore[arg-type]

    restored = restore_active_slot(profile, "a")  # type: ignore[arg-type]
    assert restored["active_slot"] == "a"
