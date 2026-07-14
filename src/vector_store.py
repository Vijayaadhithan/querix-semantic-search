from pgvector_store import PgVectorCollection
from tenant_config import TenantProfile


def get_tenant_vector_collection(
    profile: TenantProfile,
    *,
    create: bool = False,
):
    storage = profile.storage
    if storage.pgvector_database is None:
        raise RuntimeError(
            f"Tenant {profile.company_id!r} has no pgvector database config."
        )
    return PgVectorCollection(
        storage.pgvector_database,
        storage.pgvector_table,
        storage.vector_dimensions,
        hnsw_m=storage.pgvector_hnsw_m,
        hnsw_ef_construction=storage.pgvector_hnsw_ef_construction,
        hnsw_ef_search=storage.pgvector_hnsw_ef_search,
        create=create,
    )


def list_tenant_vectors(profile: TenantProfile) -> None:
    collection = get_tenant_vector_collection(profile)
    total, counts = collection.source_counts()
    print(
        f"Company: {profile.company_id} | backend: "
        f"{profile.storage.vector_backend} | vectors: {total}"
    )
    for source, count in sorted(counts.items()):
        print(f"{count:>7} vectors  {source}")


def delete_tenant_source(
    profile: TenantProfile,
    source_name: str,
) -> int:
    collection = get_tenant_vector_collection(profile)
    existing = collection.get(
        where={"source_file": source_name},
        include=[],
    )
    ids = existing.get("ids", [])
    if ids:
        collection.delete(ids=ids)
    return len(ids)


def clear_tenant_vectors(profile: TenantProfile) -> int:
    collection = get_tenant_vector_collection(profile)
    existing = collection.get(include=[])
    ids = existing.get("ids", [])
    if ids:
        collection.delete(ids=ids)
    return len(ids)
