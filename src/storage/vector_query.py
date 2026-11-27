"""Legacy and optimized pgvector query strategies operating on a cursor."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from storage.vector_filters import (
    _filter_uses_index,
)


def _has_city_filter(where: dict[str, Any]) -> bool:
    if "$and" in where:
        return any(_has_city_filter(child) for child in where["$and"])
    return "city_id" in where


def _legacy_query_rows(
    table_sql: str,
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
    rows = None
    strategy = "hnsw"
    eligible_rows: int | None = None
    if (
        filter_clause
        and exact_filter_max_rows is not None
        and exact_filter_max_rows > 0
        and _filter_uses_index(where)
    ):
        cursor.execute(
            f"""
            WITH eligible AS MATERIALIZED (
                SELECT id
                FROM {table_sql}
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
                FROM {table_sql} AS vectors
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
            rows = [row for row in exact_rows if row["id"] is not None]
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
            strategy = "hnsw_post_filter"
            active_where_clause = ""
            active_filter_params = []
            active_n_results = max(n_results, post_filter_n_results)
        cursor.execute(
            f"""
            SELECT id, document, metadata,
                   embedding <=> %s::vector AS distance
            FROM {table_sql}
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
    return rows, strategy, eligible_rows


def _optimized_query_rows(
    table_sql: str,
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
    city_membership_sql: str | None = None,
) -> tuple[list[dict[str, Any]], str, int | None]:
    strategy = "hnsw"
    eligible_rows: int | None = None
    indexed_filter = (
        filter_clause
        and exact_filter_max_rows is not None
        and exact_filter_max_rows > 0
        and _filter_uses_index(where)
    )
    if indexed_filter:
        # The lookup was validated for a single city equality. Other
        # predicates still use the original metadata filters and ranking.
        use_membership = (
            city_membership_sql is not None
            and where is not None
            and where.keys() == {"city_id"}
            and not isinstance(where["city_id"], dict)
        )
        # On broad cities a sequential scan detoasts thousands of metadata
        # values just to choose a ranking strategy. Scope the index preference
        # to this probe on the query's dedicated autocommit connection.
        city_probe = not use_membership and _has_city_filter(where)
        count_table = city_membership_sql if use_membership else table_sql
        count_where = "WHERE city_id = %s" if use_membership else where_clause
        with cursor.connection.transaction() if city_probe else nullcontext():
            if city_probe:
                cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute(
                f"""
                SELECT COUNT(*) AS eligible_count
                FROM (
                    SELECT 1
                    FROM {count_table}
                    {count_where}
                    LIMIT %s
                ) AS eligible
                """,
                (*filter_params, exact_filter_max_rows + 1),
            )
            eligible_rows = int(cursor.fetchone()["eligible_count"])
        if eligible_rows <= exact_filter_max_rows:
            strategy = "exact_filtered"
            cursor.execute("SET enable_indexscan = off")
            try:
                cursor.execute(
                    f"""
                    SELECT id, document, metadata,
                           embedding <=> %s::vector AS distance
                    FROM {table_sql}
                    {where_clause}
                    ORDER BY distance
                    LIMIT %s
                    """,
                    (vector_literal, *filter_params, n_results),
                )
                return cursor.fetchall(), strategy, eligible_rows
            finally:
                cursor.execute("SET enable_indexscan = on")
        if use_membership:
            # OFFSET 0 keeps this as a correlated index lookup during HNSW
            # traversal instead of letting the planner replace it with a join.
            cursor.execute(
                f"""
                SELECT v.id, v.document, v.metadata,
                       v.embedding <=> %s::vector AS distance
                FROM {table_sql} AS v
                WHERE EXISTS (
                    SELECT 1 FROM {city_membership_sql} AS m
                    WHERE m.city_id = %s AND m.id = v.id
                    OFFSET 0
                )
                ORDER BY v.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector_literal, *filter_params, vector_literal, n_results),
            )
            return cursor.fetchall(), "hnsw_city_membership", eligible_rows

    if (
        indexed_filter
        and eligible_rows is not None
        and eligible_rows > exact_filter_max_rows
    ):
        # pgvector 0.8 iterative scans continue traversing HNSW until the
        # metadata predicate has produced enough rows. This avoids both
        # the cold full-subset exact scan and the recall loss of filtering
        # a fixed global-nearest window.
        strategy = "hnsw_filtered"

    cursor.execute(
        f"""
        SELECT id, document, metadata,
               embedding <=> %s::vector AS distance
        FROM {table_sql}
        {where_clause}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (
            vector_literal,
            *filter_params,
            vector_literal,
            n_results,
        ),
    )
    return cursor.fetchall(), strategy, eligible_rows
