from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from copy import deepcopy
from typing import Any

from storage.postgres import (
    PostgresRuntimeConfig,
    postgres_connection,
    qualified_table,
    quote_postgres_identifier,
)
from storage.vector_filters import (
    FILTER_INDEX_KEYS,
    _filter_uses_index,
    _metadata_filter_sql,
    _metadata_key_literal,
    _metadata_matches_where,
    _rows_equivalent,
    _shadow_rows,
)
from storage.vector_query import _legacy_query_rows, _optimized_query_rows

# These are the categorical fields accepted by the shared planner and current
# compatibility adapters. B-tree expression indexes preserve the existing
# text-comparison semantics for both string and numeric JSON values.
DELETE_BATCH_SIZE = 10_000


class PgVectorCollection:
    """Collection-style vector interface backed by one isolated pgvector table."""

    query_limit_requires_count = False

    def __init__(
        self,
        config: PostgresRuntimeConfig,
        table: str,
        dimensions: int,
        *,
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 64,
        hnsw_ef_search: int = 100,
        query_mode: str = "legacy",
        create: bool = False,
    ):
        self.config = config
        self.table = table
        self.dimensions = dimensions
        self.hnsw_m = hnsw_m
        self.hnsw_ef_construction = hnsw_ef_construction
        self.hnsw_ef_search = hnsw_ef_search
        normalized_query_mode = str(query_mode).strip().casefold()
        if normalized_query_mode not in {"legacy", "shadow", "optimized"}:
            raise ValueError("pgvector query_mode must be legacy, shadow, or optimized")
        self.query_mode = normalized_query_mode
        self._query_state = threading.local()
        self._pending_lock = threading.Lock()
        self._pending_filtered: dict[tuple, Future] = {}
        if create:
            self.initialize()
        else:
            self._require_table()

    @property
    def name(self) -> str:
        return self.table

    @classmethod
    def table_exists(cls, config: PostgresRuntimeConfig, table: str) -> bool:
        with postgres_connection(config, dict_rows=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass(%s) AS table_name",
                    (f"{config.schema}.{table}",),
                )
                return cursor.fetchone()["table_name"] is not None

    @classmethod
    def drop_table(cls, config: PostgresRuntimeConfig, table: str) -> None:
        with postgres_connection(config) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS {qualified_table(config, table)}")

    def clone_to(self, table: str) -> PgVectorCollection:
        """Create and populate a second physical collection once.

        Daily runs reuse the inactive collection. This full copy is therefore
        a one-time migration path from the legacy single-generation layout.
        """
        if table == self.table:
            raise ValueError("Cannot clone a pgvector collection onto itself.")
        if self.table_exists(self.config, table):
            raise RuntimeError(
                f"Candidate pgvector table {self.config.schema}.{table} already exists."
            )
        with postgres_connection(self.config) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    CREATE TABLE {qualified_table(self.config, table)} (
                        id TEXT PRIMARY KEY,
                        document TEXT NOT NULL,
                        embedding vector({self.dimensions}) NOT NULL,
                        metadata JSONB NOT NULL
                    )
                    """
                )
                cursor.execute(
                    f"INSERT INTO {qualified_table(self.config, table)} "
                    f"SELECT id, document, embedding, metadata FROM {self._qualified()}"
                )
        # Build HNSW and filter indexes after the bulk copy. This avoids the
        # substantially slower row-by-row maintenance cost during bootstrap.
        candidate = PgVectorCollection(
            self.config,
            table,
            self.dimensions,
            hnsw_m=self.hnsw_m,
            hnsw_ef_construction=self.hnsw_ef_construction,
            hnsw_ef_search=self.hnsw_ef_search,
            query_mode=self.query_mode,
            create=True,
        )
        with postgres_connection(self.config) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"ANALYZE {candidate._qualified()}")
        return candidate

    def prewarm_hnsw_index(self, *, mode: str = "read") -> dict[str, Any]:
        """Load the vector heap first and the HNSW index last.

        Filtered ANN searches read heap metadata for the over-fetched HNSW
        window. Warming only the index leaves those random heap reads cold.
        Loading the index last gives it priority when PostgreSQL's shared
        buffer pool is smaller than the complete table, TOAST, and index
        working set.
        """
        normalized_mode = str(mode).strip().casefold()
        if normalized_mode not in {"buffer", "read", "prefetch"}:
            raise ValueError("pgvector prewarm mode must be buffer, read, or prefetch")
        index_name = f"{self.table}_embedding_hnsw"
        table_relation = f"{self.config.schema}.{self.table}"
        index_relation = f"{self.config.schema}.{index_name}"
        started = time.perf_counter()
        with postgres_connection(self.config, dict_rows=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
                cursor.execute(
                    """
                    SELECT pg_relation_size(%s::regclass) AS relation_bytes,
                           pg_prewarm(%s::regclass, %s) AS blocks
                    """,
                    (table_relation, table_relation, normalized_mode),
                )
                table_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT pg_relation_size(%s::regclass) AS relation_bytes,
                           pg_prewarm(%s::regclass, %s) AS blocks
                    """,
                    (index_relation, index_relation, normalized_mode),
                )
                index_row = cursor.fetchone()
        return {
            "table": table_relation,
            "table_blocks": int(table_row["blocks"] or 0),
            "table_bytes": int(table_row["relation_bytes"] or 0),
            "index": index_relation,
            "mode": normalized_mode,
            "blocks": int(index_row["blocks"] or 0),
            "bytes": int(index_row["relation_bytes"] or 0),
            "duration_ms": (time.perf_counter() - started) * 1000,
        }

    def _qualified(self) -> str:
        return qualified_table(self.config, self.table)

    def _require_table(self) -> None:
        with postgres_connection(self.config, dict_rows=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT to_regclass(%s) AS table_name
                    """,
                    (f"{self.config.schema}.{self.table}",),
                )
                if cursor.fetchone()["table_name"] is None:
                    raise RuntimeError(
                        f"pgvector table {self.config.schema}.{self.table} "
                        "does not exist. Run tenant ingestion first."
                    )

    def initialize(self) -> None:
        index_name = f"{self.table}_embedding_hnsw"
        source_index_name = f"{self.table}_source_file"
        with postgres_connection(self.config) as connection:
            with connection.cursor() as cursor:
                try:
                    cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                except Exception as exc:
                    raise RuntimeError(
                        "The PostgreSQL vector extension is unavailable or the "
                        "configured user cannot enable it. Install pgvector and "
                        "run CREATE EXTENSION vector as a database administrator."
                    ) from exc
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._qualified()} (
                        id TEXT PRIMARY KEY,
                        document TEXT NOT NULL,
                        embedding vector({self.dimensions}) NOT NULL,
                        metadata JSONB NOT NULL
                    )
                    """
                )
                cursor.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS
                    {quote_postgres_identifier(index_name)}
                    ON {self._qualified()}
                    USING hnsw (embedding vector_cosine_ops)
                    WITH (
                        m = {int(self.hnsw_m)},
                        ef_construction = {int(self.hnsw_ef_construction)}
                    )
                    """
                )
                cursor.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS
                    {quote_postgres_identifier(source_index_name)}
                    ON {self._qualified()} ((metadata ->> 'source_file'))
                    """
                )
        self.ensure_filter_indexes()

    _metadata_key_literal = staticmethod(_metadata_key_literal)

    def _filter_index_name(self, key: str) -> str:
        raw_name = f"{self.table}_filter_{key}"
        if len(raw_name) <= 63:
            return raw_name
        digest = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:10]
        return f"{raw_name[:52]}_{digest}"

    def ensure_filter_indexes(self, *, concurrently: bool = False) -> list[str]:
        """Create the expression indexes used by exact filtered retrieval."""
        created = []
        concurrency = "CONCURRENTLY " if concurrently else ""
        with postgres_connection(self.config) as connection:
            with connection.cursor() as cursor:
                for key in FILTER_INDEX_KEYS:
                    index_name = self._filter_index_name(key)
                    cursor.execute(
                        f"CREATE INDEX {concurrency}IF NOT EXISTS "
                        f"{quote_postgres_identifier(index_name)} "
                        f"ON {self._qualified()} "
                        f"((metadata ->> {self._metadata_key_literal(key)}))"
                    )
                    created.append(index_name)
        return created

    @staticmethod
    def _vector_literal(vector: list[float]) -> str:
        return json.dumps([float(value) for value in vector], separators=(",", ":"))

    _metadata_filter_sql = staticmethod(_metadata_filter_sql)

    _filter_uses_index = staticmethod(_filter_uses_index)

    def last_query_metrics(self) -> dict[str, Any]:
        return dict(getattr(self._query_state, "metrics", {}))

    def count(self) -> int:
        with postgres_connection(self.config, dict_rows=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) AS row_count FROM {self._qualified()}")
                return int(cursor.fetchone()["row_count"])

    def source_counts(self) -> tuple[int, dict[str, int]]:
        with postgres_connection(self.config, dict_rows=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT COALESCE(metadata ->> 'source_file', 'unknown')
                               AS source,
                           COUNT(*) AS vector_count
                    FROM {self._qualified()}
                    GROUP BY source
                    """
                )
                rows = cursor.fetchall()
        counts = {str(row["source"]): int(row["vector_count"]) for row in rows}
        return sum(counts.values()), counts

    def migrate_source_namespace(
        self,
        source_name: str,
        target_source_name: str,
        *,
        target_database: str,
        target_id: Callable[[str], str],
        batch_size: int = 1000,
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[int, int]:
        """Re-key one source in place without recalculating embeddings.

        Rows already present under their target IDs win. This preserves any
        embeddings freshly generated from the authoritative target database.
        """
        if source_name == target_source_name:
            raise RuntimeError("Source and target index namespaces are identical.")
        if batch_size <= 0:
            raise RuntimeError("Migration batch size must be greater than zero.")

        migrated = 0
        kept_target = 0
        while True:
            with (
                postgres_connection(
                    self.config,
                    dict_rows=True,
                    autocommit=False,
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    f"""
                        SELECT id, metadata
                        FROM {self._qualified()}
                        WHERE metadata ->> 'source_file' = %s
                        ORDER BY id
                        LIMIT %s
                        """,
                    (source_name, batch_size),
                )
                rows = cursor.fetchall()
                if not rows:
                    connection.rollback()
                    break

                prepared = []
                for row in rows:
                    metadata = dict(row["metadata"] or {})
                    primary_key = metadata.get("primary_key_value")
                    if primary_key is None:
                        raise RuntimeError(
                            "Cannot migrate a vector without "
                            "metadata.primary_key_value."
                        )
                    new_id = target_id(str(primary_key))
                    metadata["source_file"] = target_source_name
                    metadata["source_database"] = target_database
                    prepared.append((str(row["id"]), new_id, metadata))

                target_ids = [new_id for _, new_id, _ in prepared]
                cursor.execute(
                    f"SELECT id FROM {self._qualified()} WHERE id = ANY(%s)",
                    (target_ids,),
                )
                existing_targets = {
                    str(existing["id"]) for existing in cursor.fetchall()
                }
                conflicting_old_ids = [
                    old_id
                    for old_id, new_id, _ in prepared
                    if new_id in existing_targets
                ]
                if conflicting_old_ids:
                    cursor.execute(
                        f"DELETE FROM {self._qualified()} WHERE id = ANY(%s)",
                        (conflicting_old_ids,),
                    )

                updates = [
                    (new_id, json.dumps(metadata), old_id)
                    for old_id, new_id, metadata in prepared
                    if new_id not in existing_targets
                ]
                if updates:
                    cursor.executemany(
                        f"""
                            UPDATE {self._qualified()}
                            SET id = %s, metadata = %s::jsonb
                            WHERE id = %s
                            """,
                        updates,
                    )
                connection.commit()
                migrated += len(updates)
                kept_target += len(conflicting_old_ids)
                if progress is not None:
                    progress(migrated, kept_target)
        return migrated, kept_target

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        if not (len(ids) == len(documents) == len(embeddings) == len(metadatas)):
            raise ValueError("pgvector upsert arrays must have equal lengths")
        rows = []
        for doc_id, document, embedding, metadata in zip(
            ids,
            documents,
            embeddings,
            metadatas,
        ):
            if len(embedding) != self.dimensions:
                raise ValueError(
                    f"Expected {self.dimensions} embedding dimensions, "
                    f"received {len(embedding)}."
                )
            rows.append(
                (
                    str(doc_id),
                    document,
                    self._vector_literal(embedding),
                    json.dumps(metadata, ensure_ascii=False, default=str),
                )
            )
        if not rows:
            return
        with postgres_connection(self.config) as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    f"""
                    INSERT INTO {self._qualified()}
                        (id, document, embedding, metadata)
                    VALUES (%s, %s, %s::vector, %s::jsonb)
                    ON CONFLICT (id) DO UPDATE SET
                        document = EXCLUDED.document,
                        embedding = EXCLUDED.embedding,
                        metadata = EXCLUDED.metadata
                    """,
                    rows,
                )

    def update_metadatas(
        self,
        *,
        ids: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        if len(ids) != len(metadatas):
            raise ValueError("pgvector metadata update arrays must have equal lengths")
        rows = [
            (
                json.dumps(metadata, ensure_ascii=False, default=str),
                str(doc_id),
            )
            for doc_id, metadata in zip(ids, metadatas)
        ]
        if not rows:
            return
        with postgres_connection(self.config) as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    f"UPDATE {self._qualified()} "
                    "SET metadata = %s::jsonb WHERE id = %s",
                    rows,
                )

    def get(
        self,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        include: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, list]:
        include = include or []
        if limit is not None and limit <= 0:
            raise ValueError("get limit must be greater than zero")
        if offset is not None and offset < 0:
            raise ValueError("get offset must not be negative")
        conditions = []
        params: list[Any] = []
        if ids is not None:
            if not ids:
                return {"ids": [], "documents": [], "metadatas": []}
            placeholders = ", ".join(["%s"] * len(ids))
            conditions.append(f"id IN ({placeholders})")
            params.extend(str(value) for value in ids)
        for key, value in (where or {}).items():
            conditions.append("metadata ->> %s = %s")
            params.extend((str(key), str(value)))
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        selected = ["id"]
        if "documents" in include:
            selected.append("document")
        if "metadatas" in include:
            selected.append("metadata")
        with postgres_connection(self.config, dict_rows=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {', '.join(selected)} FROM {self._qualified()} "
                    f"{where_clause} ORDER BY id"
                    + (" LIMIT %s" if limit is not None else "")
                    + (" OFFSET %s" if offset is not None else ""),
                    (
                        *params,
                        *((limit,) if limit is not None else ()),
                        *((offset,) if offset is not None else ()),
                    ),
                )
                rows = cursor.fetchall()
        rows_by_id = {str(row["id"]): row for row in rows}
        ordered_rows = (
            [rows_by_id[str(value)] for value in ids if str(value) in rows_by_id]
            if ids is not None
            else rows
        )
        result = {"ids": [str(row["id"]) for row in ordered_rows]}
        if "documents" in include:
            result["documents"] = [row["document"] for row in ordered_rows]
        if "metadatas" in include:
            result["metadatas"] = [row["metadata"] for row in ordered_rows]
        return result

    _metadata_matches_where = staticmethod(_metadata_matches_where)

    _shadow_rows = staticmethod(_shadow_rows)

    _rows_equivalent = staticmethod(_rows_equivalent)

    def _legacy_query_rows(
        self,
        cursor,
        *,
        vector_literal: str,
        n_results: int,
        where_clause: str,
        filter_clause: str,
        filter_params: list[Any],
        where: dict[str, Any] | None,
        exact_filter_max_rows: int | None,
        post_filter_n_results: int | None,
    ) -> tuple[list[dict[str, Any]], str, int | None]:
        return _legacy_query_rows(
            self._qualified(),
            cursor,
            vector_literal=vector_literal,
            n_results=n_results,
            where_clause=where_clause,
            filter_clause=filter_clause,
            filter_params=filter_params,
            where=where,
            exact_filter_max_rows=exact_filter_max_rows,
            post_filter_n_results=post_filter_n_results,
        )

    def _optimized_query_rows(
        self,
        cursor,
        *,
        vector_literal: str,
        n_results: int,
        where_clause: str,
        filter_clause: str,
        filter_params: list[Any],
        where: dict[str, Any] | None,
        exact_filter_max_rows: int | None,
        post_filter_n_results: int | None,
    ) -> tuple[list[dict[str, Any]], str, int | None]:
        def execute():
            return _optimized_query_rows(
                self._qualified(),
                cursor,
                vector_literal=vector_literal,
                n_results=n_results,
                where_clause=where_clause,
                filter_clause=filter_clause,
                filter_params=filter_params,
                where=where,
                exact_filter_max_rows=exact_filter_max_rows,
                post_filter_n_results=post_filter_n_results,
            )

        if self.query_mode != "optimized" or not filter_clause:
            return execute()
        # One collection belongs to one physical generation. Share only
        # concurrent identical SQL inputs, including the actual embedding.
        key = (
            vector_literal,
            n_results,
            filter_clause,
            tuple(filter_params),
            exact_filter_max_rows,
            post_filter_n_results,
        )
        with self._pending_lock:
            pending = self._pending_filtered.get(key)
            owner = pending is None
            if owner:
                pending = self._pending_filtered[key] = Future()
        if not owner:
            self._query_state.shared_query = True
            return deepcopy(pending.result())
        try:
            result = execute()
            pending.set_result(deepcopy(result))
            return result
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self._pending_lock:
                del self._pending_filtered[key]

    def query(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int,
        where: dict[str, Any] | None = None,
        include: list[str] | None = None,
        exact_filter_max_rows: int | None = None,
        post_filter_n_results: int | None = None,
    ) -> dict[str, list[list]]:
        include = include or []
        all_ids = []
        all_documents = []
        all_metadatas = []
        all_distances = []
        query_started = time.perf_counter()
        query_mode = getattr(self, "query_mode", "legacy")
        strategy = "hnsw"
        eligible_rows: int | None = None
        legacy_ms = 0.0
        optimized_ms = 0.0
        shadow_equal: bool | str = "not_run"
        shadow_error = "none"
        shadow_legacy_rows = 0
        shadow_optimized_rows = 0
        self._query_state.shared_query = False
        with postgres_connection(self.config, dict_rows=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET hnsw.iterative_scan = strict_order")
                cursor.execute(f"SET hnsw.ef_search = {int(self.hnsw_ef_search)}")
                filter_clause, filter_params = self._metadata_filter_sql(where)
                where_clause = f"WHERE {filter_clause}" if filter_clause else ""
                for embedding in query_embeddings:
                    if len(embedding) != self.dimensions:
                        raise ValueError(
                            f"Expected {self.dimensions} query dimensions, "
                            f"received {len(embedding)}."
                        )
                    vector_literal = self._vector_literal(embedding)
                    common = {
                        "vector_literal": vector_literal,
                        "n_results": n_results,
                        "where_clause": where_clause,
                        "filter_clause": filter_clause,
                        "filter_params": filter_params,
                        "where": where,
                        "exact_filter_max_rows": exact_filter_max_rows,
                        "post_filter_n_results": post_filter_n_results,
                    }
                    if query_mode in {"legacy", "shadow"}:
                        mode_started = time.perf_counter()
                        rows, strategy, eligible_rows = self._legacy_query_rows(
                            cursor,
                            **common,
                        )
                        legacy_ms += (time.perf_counter() - mode_started) * 1000
                    if query_mode in {"optimized", "shadow"}:
                        mode_started = time.perf_counter()
                        try:
                            (
                                optimized_rows,
                                optimized_strategy,
                                optimized_eligible_rows,
                            ) = self._optimized_query_rows(cursor, **common)
                        except Exception as exc:
                            if query_mode != "shadow":
                                raise
                            optimized_rows = []
                            optimized_strategy = "failed"
                            optimized_eligible_rows = None
                            shadow_equal = False
                            shadow_error = type(exc).__name__
                        optimized_ms += (time.perf_counter() - mode_started) * 1000
                        if query_mode == "optimized":
                            rows = optimized_rows
                            strategy = optimized_strategy
                            eligible_rows = optimized_eligible_rows
                        else:
                            comparable_legacy = (
                                self._shadow_rows(rows, where, n_results)
                                if strategy == "hnsw_post_filter"
                                else rows
                            )
                            shadow_legacy_rows += len(comparable_legacy)
                            shadow_optimized_rows += len(optimized_rows)
                            equivalent = (
                                shadow_error == "none"
                                and self._rows_equivalent(
                                    comparable_legacy,
                                    optimized_rows,
                                )
                            )
                            shadow_equal = (
                                equivalent
                                if shadow_equal == "not_run"
                                else bool(shadow_equal) and equivalent
                            )
                    all_ids.append([str(row["id"]) for row in rows])
                    all_documents.append([row["document"] for row in rows])
                    all_metadatas.append([row["metadata"] for row in rows])
                    all_distances.append([float(row["distance"]) for row in rows])
        self._query_state.metrics = {
            "strategy": strategy,
            "eligible_rows": eligible_rows,
            "eligible_rows_capped": bool(
                eligible_rows is not None
                and exact_filter_max_rows is not None
                and eligible_rows == exact_filter_max_rows + 1
            ),
            "query_mode": query_mode,
            "shared_query": self._query_state.shared_query,
            "database_ms": (optimized_ms if query_mode == "optimized" else legacy_ms),
            "legacy_ms": legacy_ms,
            "optimized_ms": optimized_ms,
            "shadow_ms": (
                (time.perf_counter() - query_started) * 1000
                if query_mode == "shadow"
                else 0.0
            ),
            "shadow_equal": shadow_equal,
            "shadow_error": shadow_error,
            "shadow_legacy_rows": shadow_legacy_rows,
            "shadow_optimized_rows": shadow_optimized_rows,
        }
        result = {"ids": all_ids}
        if "documents" in include:
            result["documents"] = all_documents
        if "metadatas" in include:
            result["metadatas"] = all_metadatas
        if "distances" in include:
            result["distances"] = all_distances
        return result

    def delete(
        self,
        *,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> None:
        if ids is None and not where:
            raise ValueError("Refusing to delete pgvector rows without a selector")
        if ids is not None and not ids:
            return
        id_batches: list[list[str] | None]
        if ids is None:
            id_batches = [None]
        else:
            normalized_ids = [str(value) for value in ids]
            id_batches = [
                normalized_ids[start : start + DELETE_BATCH_SIZE]
                for start in range(0, len(normalized_ids), DELETE_BATCH_SIZE)
            ]
        with postgres_connection(self.config) as connection:
            with connection.cursor() as cursor:
                for id_batch in id_batches:
                    conditions = []
                    params: list[Any] = []
                    if id_batch is not None:
                        placeholders = ", ".join(["%s"] * len(id_batch))
                        conditions.append(f"id IN ({placeholders})")
                        params.extend(id_batch)
                    for key, value in (where or {}).items():
                        conditions.append("metadata ->> %s = %s")
                        params.extend((str(key), str(value)))
                    delete_conditions = " AND ".join(conditions)
                    cursor.execute(
                        f"DELETE FROM {self._qualified()} WHERE {delete_conditions}",
                        params,
                    )
