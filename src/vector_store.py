from pgvector_store import PgVectorCollection
from database_store import database_backend, database_source_name
from document_processing import mysql_document_id
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
        query_mode=storage.pgvector_query_mode,
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


def migrate_tenant_source(
    profile: TenantProfile,
    source_name: str,
    *,
    batch_size: int = 1000,
) -> tuple[int, int, str]:
    collection = get_tenant_vector_collection(profile)
    database = profile.database
    target_source = database_source_name(database)
    namespace = database.index_namespace or database.database
    backend = database_backend(database)

    def target_id(primary_key: str) -> str:
        return mysql_document_id(
            database.search_table,
            primary_key,
            database=namespace,
            company_id=profile.company_id,
            backend=backend,
        )

    migrated, kept_target = collection.migrate_source_namespace(
        source_name,
        target_source,
        target_database=database.database,
        target_id=target_id,
        batch_size=batch_size,
        progress=lambda migrated, kept: print(
            "Source migration progress: "
            f"migrated={migrated} kept_target={kept}",
            flush=True,
        ),
    )
    return migrated, kept_target, target_source


def clear_tenant_vectors(profile: TenantProfile) -> int:
    collection = get_tenant_vector_collection(profile)
    existing = collection.get(include=[])
    ids = existing.get("ids", [])
    if ids:
        collection.delete(ids=ids)
    return len(ids)
