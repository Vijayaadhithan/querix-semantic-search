from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from typing import Any

from postgres_store import (
    PostgresRuntimeConfig,
    postgres_connection,
    qualified_table,
    quote_postgres_identifier,
)


# These are the categorical fields accepted by the Gainr planner and explicit
# API compatibility layer. B-tree expression indexes preserve the existing
# text-comparison semantics for both string and numeric JSON values.
FILTER_INDEX_KEYS = (
    "main_category_name",
    "subcategory_name",
    "state_name",
    "city_name",
    "locality_name",
    "rental_duration",
    "city_id",
    "subcategory_id",
    "locality_id",
)


class PgVectorCollection:
    """Collection-style vector interface backed by one isolated pgvector table."""

    def __init__(
        self,
        config: PostgresRuntimeConfig,
        table: str,
        dimensions: int,
        *,
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 64,
        hnsw_ef_search: int = 100,
        create: bool = False,
    ):
        self.config = config
        self.table = table
        self.dimensions = dimensions
        self.hnsw_m = hnsw_m
        self.hnsw_ef_construction = hnsw_ef_construction
        self.hnsw_ef_search = hnsw_ef_search
        self._query_state = threading.local()
        if create:
            self.initialize()
        else:
            self._require_table()

    @property
    def name(self) -> str:
        return self.table

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

    @staticmethod
    def _metadata_key_literal(key: str) -> str:
        if "\x00" in key:
            raise ValueError("PostgreSQL metadata keys cannot contain null bytes")
        return "'" + key.replace("'", "''") + "'"

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

    @classmethod
    def _metadata_filter_sql(cls, where: dict[str, Any] | None) -> tuple[str, list[Any]]:
        if not where:
            return "", []
        if "$and" in where:
            clauses = []
            params: list[Any] = []
            for child in where["$and"]:
                clause, child_params = cls._metadata_filter_sql(child)
                if clause:
                    clauses.append(f"({clause})")
                    params.extend(child_params)
            return " AND ".join(clauses), params
        clauses = []
        params: list[Any] = []
        for key, expected in where.items():
            key_literal = cls._metadata_key_literal(str(key))
            if not isinstance(expected, dict):
                clauses.append(f"metadata ->> {key_literal} = %s")
                params.append(str(expected))
                continue
            for operator, value in expected.items():
                if operator == "$in":
                    values = [str(item) for item in value]
                    if not values:
                        clauses.append("FALSE")
                    else:
                        placeholders = ", ".join(["%s"] * len(values))
                        clauses.append(
                            f"metadata ->> {key_literal} IN ({placeholders})"
                        )
                        params.extend(values)
                elif operator in {"$gte", "$lte"}:
                    comparator = ">=" if operator == "$gte" else "<="
                    clauses.append(
                        f"(metadata ->> {key_literal}) ~ %s "
                        f"AND (metadata ->> {key_literal})::double precision "
                        f"{comparator} %s"
                    )
                    params.extend(
                        (
                            r"^-?[0-9]+(\.[0-9]+)?$",
                            float(value),
                        )
                    )
                else:
                    raise ValueError(f"Unsupported pgvector metadata operator {operator!r}")
        return " AND ".join(clauses), params

    @classmethod
    def _filter_uses_index(cls, where: dict[str, Any] | None) -> bool:
        if not where:
            return False
        if "$and" in where:
            return any(cls._filter_uses_index(child) for child in where["$and"])
        return any(str(key) in FILTER_INDEX_KEYS for key in where)

    def last_query_metrics(self) -> dict[str, Any]:
        return dict(getattr(self._query_state, "metrics", {}))

    def count(self) -> int:
        with postgres_connection(self.config, dict_rows=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) AS row_count FROM {self._qualified()}"
                )
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
        counts = {
            str(row["source"]): int(row["vector_count"])
            for row in rows
        }
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
            with postgres_connection(
                self.config,
                dict_rows=True,
                autocommit=False,
            ) as connection:
                with connection.cursor() as cursor:
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
                        prepared.append(
                            (str(row["id"]), new_id, metadata)
                        )

                    target_ids = [new_id for _, new_id, _ in prepared]
                    cursor.execute(
                        f"SELECT id FROM {self._qualified()} "
                        "WHERE id = ANY(%s)",
                        (target_ids,),
                    )
                    existing_targets = {
                        str(existing["id"])
                        for existing in cursor.fetchall()
                    }
                    conflicting_old_ids = [
                        old_id
                        for old_id, new_id, _ in prepared
                        if new_id in existing_targets
                    ]
                    if conflicting_old_ids:
                        cursor.execute(
                            f"DELETE FROM {self._qualified()} "
                            "WHERE id = ANY(%s)",
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
        if not (
            len(ids)
            == len(documents)
            == len(embeddings)
            == len(metadatas)
        ):
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
        where_clause = (
            f"WHERE {' AND '.join(conditions)}" if conditions else ""
        )
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
        strategy = "hnsw"
        eligible_rows: int | None = None
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
                    rows = None
                    if (
                        filter_clause
                        and exact_filter_max_rows is not None
                        and exact_filter_max_rows > 0
                        and self._filter_uses_index(where)
                    ):
                        # The limited ID CTE is cheap with the expression
                        # indexes. If every eligible ID fits under the bound,
                        # rank the complete set exactly. Broader subsets use
                        # either bounded post-filter HNSW (when requested) or
                        # filtered HNSW without dropping filter predicates.
                        cursor.execute(
                            f"""
                            WITH eligible AS MATERIALIZED (
                                SELECT id
                                FROM {self._qualified()}
                                {where_clause}
                                LIMIT %s
                            ),
                            eligibility AS (
                                SELECT COUNT(*) AS eligible_count
                                FROM eligible
                            ),
                            ranked AS (
                                SELECT vectors.id,
                                       vectors.document,
                                       vectors.metadata,
                                       vectors.embedding <=> %s::vector AS distance
                                FROM {self._qualified()} AS vectors
                                JOIN eligible USING (id)
                                WHERE (
                                    SELECT eligible_count FROM eligibility
                                ) <= %s
                                ORDER BY vectors.embedding <=> %s::vector
                                LIMIT %s
                            )
                            SELECT ranked.id,
                                   ranked.document,
                                   ranked.metadata,
                                   ranked.distance,
                                   eligibility.eligible_count
                            FROM eligibility
                            LEFT JOIN ranked ON TRUE
                            ORDER BY ranked.distance NULLS LAST
                            """,
                            (
                                *filter_params,
                                exact_filter_max_rows + 1,
                                vector_literal,
                                exact_filter_max_rows,
                                vector_literal,
                                n_results,
                            ),
                        )
                        exact_rows = cursor.fetchall()
                        eligible_rows = int(exact_rows[0]["eligible_count"])
                        if eligible_rows <= exact_filter_max_rows:
                            strategy = "exact_filtered"
                            rows = [
                                row
                                for row in exact_rows
                                if row["id"] is not None
                            ]
                    if rows is None:
                        active_where_clause = where_clause
                        active_filter_params = filter_params
                        active_n_results = n_results
                        if (
                            eligible_rows is not None
                            and exact_filter_max_rows is not None
                            and eligible_rows > exact_filter_max_rows
                            and post_filter_n_results is not None
                            and post_filter_n_results > 0
                        ):
                            # Broad metadata-filtered HNSW can degrade into a
                            # long iterative scan. Retrieve a bounded ordinary
                            # HNSW window; retrieval.py applies the original
                            # predicate exactly before returning candidates.
                            strategy = "hnsw_post_filter"
                            active_where_clause = ""
                            active_filter_params = []
                            active_n_results = max(
                                n_results,
                                post_filter_n_results,
                            )
                        cursor.execute(
                            f"""
                            SELECT id, document, metadata,
                                   embedding <=> %s::vector AS distance
                            FROM {self._qualified()}
                            {active_where_clause}
                            ORDER BY embedding <=> %s::vector
                            LIMIT %s
                            """,
                            (
                                vector_literal,
                                *active_filter_params,
                                vector_literal,
                                active_n_results,
                            ),
                        )
                        rows = cursor.fetchall()
                    all_ids.append([str(row["id"]) for row in rows])
                    all_documents.append([row["document"] for row in rows])
                    all_metadatas.append([row["metadata"] for row in rows])
                    all_distances.append(
                        [float(row["distance"]) for row in rows]
                    )
        self._query_state.metrics = {
            "strategy": strategy,
            "eligible_rows": eligible_rows,
            "database_ms": (time.perf_counter() - query_started) * 1000,
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
        conditions = []
        params: list[Any] = []
        if ids is not None:
            if not ids:
                return
            placeholders = ", ".join(["%s"] * len(ids))
            conditions.append(f"id IN ({placeholders})")
            params.extend(str(value) for value in ids)
        for key, value in (where or {}).items():
            conditions.append("metadata ->> %s = %s")
            params.extend((str(key), str(value)))
        if not conditions:
            raise ValueError("Refusing to delete pgvector rows without a selector")
        with postgres_connection(self.config) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM {self._qualified()} "
                    f"WHERE {' AND '.join(conditions)}",
                    params,
                )
