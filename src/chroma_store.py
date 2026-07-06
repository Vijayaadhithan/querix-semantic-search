import sqlite3
from collections import Counter
from pathlib import Path

import chromadb

from settings import CHROMA_DIR, COLLECTION_NAME, EMBED_MODEL


def get_collection(
    create: bool = False,
    *,
    chroma_dir=CHROMA_DIR,
    collection_name=COLLECTION_NAME,
):
    client = chromadb.PersistentClient(path=str(chroma_dir))
    if create:
        collection = client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        return client, collection
    try:
        return client, client.get_collection(collection_name)
    except Exception as exc:
        raise RuntimeError(
            f"No vector collection {collection_name!r} found. Run the "
            "tenant ingestion job first."
        ) from exc


def list_indexed_documents(
    *,
    chroma_dir=CHROMA_DIR,
    collection_name=COLLECTION_NAME,
) -> None:
    total, counts = chroma_source_counts(
        chroma_dir=chroma_dir,
        collection_name=collection_name,
    )

    print(f"Collection: {collection_name} ({total} chunks)\n")
    for filename, count in sorted(counts.items(), key=lambda item: item[0].lower()):
        print(f"{count:>5} chunks  {filename}")


def chroma_source_counts(
    *,
    chroma_dir=CHROMA_DIR,
    collection_name=COLLECTION_NAME,
) -> tuple[int, Counter]:
    """Read source counts from Chroma metadata without loading vector rows."""
    database_path = Path(chroma_dir) / "chroma.sqlite3"
    if not database_path.is_file():
        raise RuntimeError(
            f"No Chroma metadata database found at {database_path}. "
            "Run the tenant ingestion job first."
        )
    try:
        with sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
        ) as connection:
            collection = connection.execute(
                "SELECT id FROM collections WHERE name = ? LIMIT 1",
                (collection_name,),
            ).fetchone()
            if collection is None:
                raise RuntimeError(
                    f"No vector collection {collection_name!r} found. "
                    "Run the tenant ingestion job first."
                )
            rows = connection.execute(
                """
                SELECT COALESCE(metadata.string_value, 'unknown') AS source,
                       COUNT(*) AS vector_count
                FROM embeddings
                JOIN segments
                  ON segments.id = embeddings.segment_id
                LEFT JOIN embedding_metadata AS metadata
                  ON metadata.id = embeddings.id
                 AND metadata.key = 'source_file'
                WHERE segments.collection = ?
                  AND segments.scope = 'METADATA'
                GROUP BY source
                """,
                (collection[0],),
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(
            "Unable to read Chroma metadata for the list operation. "
            "Verify that this deployment uses the supported Chroma 1.x "
            "persistent schema."
        ) from exc
    counts = Counter({str(source): int(count) for source, count in rows})
    return sum(counts.values()), counts


def confirm(message: str, assume_yes: bool = False) -> bool:
    return assume_yes or input(f"{message} [y/N]: ").strip().lower() in {"y", "yes"}


def delete_indexed_document(
    filename: str,
    assume_yes: bool = False,
    *,
    chroma_dir=CHROMA_DIR,
    collection_name=COLLECTION_NAME,
) -> None:
    _, collection = get_collection(
        chroma_dir=chroma_dir,
        collection_name=collection_name,
    )
    existing = collection.get(where={"source_file": filename}, include=[])
    count = len(existing["ids"])
    if not count:
        raise RuntimeError(f"'{filename}' is not present in the collection.")
    if not confirm(
        f"Delete {count} indexed chunks for '{filename}'? The source file will be kept.",
        assume_yes,
    ):
        print("Cancelled.")
        return
    collection.delete(where={"source_file": filename})
    print(f"Deleted {count} chunks for '{filename}'.")


def clear_collection(
    assume_yes: bool = False,
    *,
    chroma_dir=CHROMA_DIR,
    collection_name=COLLECTION_NAME,
) -> None:
    client, collection = get_collection(
        chroma_dir=chroma_dir,
        collection_name=collection_name,
    )
    count = collection.count()
    if not confirm(
        f"Delete the entire '{collection_name}' collection ({count} chunks)?",
        assume_yes,
    ):
        print("Cancelled.")
        return
    client.delete_collection(collection_name)
    print(f"Deleted collection '{collection_name}'. Source files were kept.")


def source_is_current(
    collection,
    filename: str,
    ids: list[str],
    documents: list[str],
) -> bool:
    existing = collection.get(
        where={"source_file": filename},
        include=["documents", "metadatas"],
    )
    if len(existing["ids"]) != len(ids):
        return False

    stored_documents = dict(zip(existing["ids"], existing["documents"]))
    expected_documents = dict(zip(ids, documents))
    models_match = all(
        metadata.get("embedding_model") == EMBED_MODEL
        for metadata in existing["metadatas"]
    )
    return models_match and stored_documents == expected_documents


def mysql_current_ids(
    collection,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict],
) -> set[str]:
    if not ids:
        return set()
    existing = collection.get(ids=ids, include=["documents", "metadatas"])
    expected = {
        doc_id: {
            "document": document,
            "hash": metadata.get("source_content_hash"),
        }
        for doc_id, document, metadata in zip(ids, documents, metadatas)
    }
    current = set()
    for doc_id, document, metadata in zip(
        existing["ids"],
        existing["documents"],
        existing["metadatas"],
    ):
        if metadata.get("embedding_model") != EMBED_MODEL:
            continue
        expected_row = expected.get(doc_id, {})
        hash_matches = metadata.get("source_content_hash") == expected_row.get("hash")
        document_matches = document == expected_row.get("document")
        if hash_matches or document_matches:
            current.add(doc_id)
    return current
