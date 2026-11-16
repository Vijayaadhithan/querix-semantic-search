import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
from typing import Any

from core.tenant_config import TenantProfile
from storage.mysql import (
    MySQLRuntimeConfig,
    mysql_active_condition,
    mysql_connection,
    quote_mysql_identifier,
    require_pymysql,
)
from tenants.gainr.models import (
    DURATION_ORDER,
    GainrSearchFilter,
    _unique,
)
from tenants.gainr.relations import GainrRelationHydrator
from tenants.gainr.sql_filters import build_where_clause

logger = logging.getLogger("uvicorn.error")

_TRANSIENT_MYSQL_READ_ERROR_CODES = {2006, 2013}
_BOOST_ADS_TABLE = quote_mysql_identifier("boost_ads")


def _transient_mysql_read_error_code(exc: Exception) -> int | None:
    """Return a retryable disconnect code without hiding other SQL failures."""
    if exc.__class__.__name__ != "OperationalError" or not exc.args:
        return None
    code = exc.args[0]
    return code if code in _TRANSIENT_MYSQL_READ_ERROR_CODES else None


def retry_transient_mysql_read(method):
    """Retry one idempotent Gainr read after MySQL drops its socket mid-query."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            error_code = _transient_mysql_read_error_code(exc)
            if error_code is None:
                raise
            logger.warning(
                "Gainr MySQL read retry operation=%s error_code=%s attempt=2/2",
                method.__name__,
                error_code,
            )
            return method(self, *args, **kwargs)

    return wrapped


class GainrDatabaseRepository:
    """Gainr-only read adapter over its search-ready and result tables."""

    def __init__(self, profile: TenantProfile, database_pool=None):
        if not isinstance(profile.database, MySQLRuntimeConfig):
            raise RuntimeError(
                "The gainr_legacy adapter currently requires Gainr's MySQL "
                "database profile."
            )
        self.profile = profile
        self.config = profile.database
        self.database_pool = database_pool
        self.search_table = quote_mysql_identifier(self.config.search_table)
        self.result_table = quote_mysql_identifier(self.config.result_table)
        self.users_table = quote_mysql_identifier(
            self.profile.compatibility.users_table
        )
        self.search_active_condition = mysql_active_condition(self.config)
        self.search_active_qualified_condition = mysql_active_condition(
            self.config,
            table_alias="sr",
        )
        self.search_active_filter = (
            f"AND {self.search_active_condition}"
            if self.search_active_condition
            else ""
        )
        self.search_active_qualified_filter = (
            f"AND {self.search_active_qualified_condition}"
            if self.search_active_qualified_condition
            else ""
        )
        self.serves_cards_from_search_ready = (
            self.profile.compatibility.serves_cards_from_search_ready
        )
        # Keep the extra count/page connection bounded under a request burst.
        # Other searches fall back to one sequential connection immediately.
        self._parallel_catalog_slots = threading.BoundedSemaphore(
            min(2, max(1, self.config.pool_max_size // 2))
        )
        self._relations = GainrRelationHydrator(
            profile,
            lambda: self.connection(),
            database_pool,
        )

    @contextmanager
    def connection(self):
        if self.database_pool is not None:
            with self.database_pool.connection() as connection:
                yield connection
            return
        pymysql = require_pymysql()
        with mysql_connection(
            cursorclass=pymysql.cursors.DictCursor,
            config=self.config,
        ) as connection:
            yield connection

    @retry_transient_mysql_read
    def active_boosted_ad_ids(self) -> set[str]:
        """Read today's active boosts from Gainr's authoritative table."""
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT DISTINCT ads_id
                FROM {_BOOST_ADS_TABLE}
                WHERE deleted_at IS NULL
                  AND start_date <= CURDATE()
                  AND end_date >= CURDATE()
                """
            )
            return {str(row["ads_id"]) for row in cursor.fetchall()}

    @retry_transient_mysql_read
    def suggestions(self, term: str, limit: int) -> list[str]:
        prefix = f"{term}%"
        if self.serves_cards_from_search_ready:
            query = f"""
                SELECT DISTINCT subcategory_name AS value
                FROM {self.search_table}
                WHERE subcategory_name IS NOT NULL
                  AND TRIM(subcategory_name) <> ''
                  AND subcategory_name LIKE %s
                  {self.search_active_filter}
                ORDER BY
                    CASE WHEN LOWER(subcategory_name) = LOWER(%s)
                         THEN 0 ELSE 1 END,
                    subcategory_name
                LIMIT %s
            """
            with self.connection() as connection, connection.cursor() as cursor:
                cursor.execute(query, (prefix, term, limit))
                return [
                    str(row["value"]) for row in cursor.fetchall() if row.get("value")
                ]
        query = f"""
            SELECT DISTINCT name AS value
            FROM {quote_mysql_identifier("sub_categories")}
            WHERE name IS NOT NULL
              AND TRIM(name) <> ''
              AND name LIKE %s
              AND status = 1
              AND (deleted_at IS NULL OR TRIM(deleted_at) = '')
            ORDER BY
                CASE WHEN LOWER(name) = LOWER(%s) THEN 0 ELSE 1 END,
                name
            LIMIT %s
        """
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                query,
                (prefix, term, limit),
            )
            return [str(row["value"]) for row in cursor.fetchall() if row.get("value")]

    @retry_transient_mysql_read
    def filter_data(self, city_id: int) -> tuple[list[str], list[dict]]:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                    SELECT DISTINCT rental_duration
                    FROM {self.search_table}
                    WHERE city_id = %s
                      {self.search_active_filter}
                      AND rental_duration IS NOT NULL
                      AND TRIM(rental_duration) <> ''
                    """,
                (city_id,),
            )
            durations = [str(row["rental_duration"]) for row in cursor.fetchall()]
            if self.serves_cards_from_search_ready:
                cursor.execute(
                    f"""
                    SELECT DISTINCT locality_id,
                                    locality_name
                    FROM {self.search_table}
                    WHERE city_id = %s
                      {self.search_active_filter}
                      AND locality_id IS NOT NULL
                      AND locality_name IS NOT NULL
                      AND TRIM(locality_name) <> ''
                    ORDER BY locality_name
                    """,
                    (city_id,),
                )
            else:
                cursor.execute(
                    f"""
                        SELECT DISTINCT id AS locality_id,
                                        area AS locality_name
                        FROM {quote_mysql_identifier("locations")}
                        WHERE city_id = %s
                          AND id IS NOT NULL
                          AND area IS NOT NULL
                          AND TRIM(area) <> ''
                          AND (deleted_at IS NULL OR TRIM(deleted_at) = '')
                        ORDER BY area
                        """,
                    (city_id,),
                )
            localities = [
                {
                    "id": int(row["locality_id"]),
                    "area": str(row["locality_name"]),
                }
                for row in cursor.fetchall()
            ]
        durations = sorted(
            _unique(durations),
            key=lambda value: (
                DURATION_ORDER.get(value, len(DURATION_ORDER)),
                value.casefold(),
            ),
        )
        return durations, localities

    def _where_clause(
        self,
        resolved_filters: dict,
        request_filter: GainrSearchFilter,
        *,
        product_ids: list[Any] | None = None,
        fallback_term: str = "",
        allowed_ad_types: set[str] | None = None,
    ) -> tuple[str, list[Any]]:
        return build_where_clause(
            resolved_filters,
            request_filter,
            serves_cards_from_search_ready=self.serves_cards_from_search_ready,
            result_table=self.result_table,
            search_active_qualified_condition=self.search_active_qualified_condition,
            compatibility=self.profile.compatibility,
            product_ids=product_ids,
            fallback_term=fallback_term,
            allowed_ad_types=allowed_ad_types,
        )

    @retry_transient_mysql_read
    def search_catalog(
        self,
        resolved_filters: dict,
        request_filter: GainrSearchFilter,
        *,
        search_term: str,
        page: int,
        page_size: int,
        sort_order: str | None,
        allowed_ad_types: set[str] | None,
        boosted_ids: set[str] | None = None,
    ) -> tuple[list[dict], int]:
        where_clause, params = self._where_clause(
            resolved_filters,
            request_filter,
            fallback_term=search_term,
            allowed_ad_types=allowed_ad_types,
        )
        join = (
            f"FROM {self.search_table} AS sr "
            f"JOIN {self.result_table} AS a ON a.id = sr.id "
        )
        order = {
            "price_asc": (
                "CASE WHEN sr.rental_fee IS NULL OR sr.rental_fee <= 1 "
                "THEN 1 ELSE 0 END, sr.rental_fee ASC, sr.id DESC"
            ),
            "price_desc": (
                "CASE WHEN sr.rental_fee IS NULL OR sr.rental_fee <= 1 "
                "THEN 1 ELSE 0 END, sr.rental_fee DESC, sr.id DESC"
            ),
        }.get(sort_order, "sr.updated_at DESC, sr.id DESC")
        boosted_ids = sorted(boosted_ids or (), key=int)
        boost_params: tuple[str, ...] = ()
        if boosted_ids:
            placeholders = ", ".join("%s" for _ in boosted_ids)
            order = f"CASE WHEN sr.id IN ({placeholders}) THEN 0 ELSE 1 END, {order}"
            boost_params = tuple(boosted_ids)
        offset = (page - 1) * page_size

        def fetch_search_ready_total(connection) -> int:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM {self.search_table} AS sr
                    WHERE {where_clause}
                    """,
                    params,
                )
                return int(cursor.fetchone()["total"])

        def fetch_search_ready_rows(connection) -> list[dict]:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT sr.*,
                           sr.city_name AS __city_name,
                           sr.locality_name AS __locality_name
                    FROM {self.search_table} AS sr
                    WHERE {where_clause}
                    ORDER BY {order}
                    LIMIT %s OFFSET %s
                    """,
                    (*params, *boost_params, page_size, offset),
                )
                return list(cursor.fetchall())

        def run_with_connection(fetcher):
            with self.connection() as connection:
                return fetcher(connection)

        if self.serves_cards_from_search_ready:
            parallel = self.database_pool is not None and (
                self._parallel_catalog_slots.acquire(blocking=False)
            )
            try:
                if parallel:
                    with ThreadPoolExecutor(
                        max_workers=2,
                        thread_name_prefix="gainr-catalog",
                    ) as executor:
                        total_future = executor.submit(
                            run_with_connection,
                            fetch_search_ready_total,
                        )
                        rows_future = executor.submit(
                            run_with_connection,
                            fetch_search_ready_rows,
                        )
                        total = total_future.result()
                        rows = rows_future.result()
                else:
                    with self.connection() as connection:
                        total = fetch_search_ready_total(connection)
                        rows = fetch_search_ready_rows(connection)
            finally:
                if parallel:
                    self._parallel_catalog_slots.release()
            self._relations.attach_search_ready(rows)
            return rows, total

        def fetch_total(connection) -> int:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(DISTINCT sr.id) AS total "
                    f"{join} WHERE {where_clause}",
                    params,
                )
                return int(cursor.fetchone()["total"])

        def fetch_rows(connection) -> list[dict]:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT a.*, sr.city_name AS __city_name,
                           sr.locality_name AS __locality_name
                    {join}
                    WHERE {where_clause}
                    ORDER BY {order}
                    LIMIT %s OFFSET %s
                    """,
                    (*params, *boost_params, page_size, offset),
                )
                return list(cursor.fetchall())

        if self.database_pool is not None:
            with ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="gainr-catalog",
            ) as executor:
                total_future = executor.submit(
                    run_with_connection,
                    fetch_total,
                )
                rows_future = executor.submit(
                    run_with_connection,
                    fetch_rows,
                )
                total = total_future.result()
                rows = rows_future.result()
        else:
            with self.connection() as connection:
                total = fetch_total(connection)
                rows = fetch_rows(connection)
        self._attach_attributes(rows)
        return rows, total

    @retry_transient_mysql_read
    def count_filter_variants(
        self,
        variants: dict[str, tuple[dict, GainrSearchFilter, set[str] | None]],
        *,
        product_ids: list[Any] | None = None,
        fallback_term: str = "",
    ) -> dict[str, int]:
        """Count zero-result counterfactuals without changing source data."""
        if not variants:
            return {}
        join = (
            f"FROM {self.search_table} AS sr "
            if self.serves_cards_from_search_ready
            else (
                f"FROM {self.search_table} AS sr "
                f"JOIN {self.result_table} AS a ON a.id = sr.id "
            )
        )
        counts: dict[str, int] = {}
        with self.connection() as connection, connection.cursor() as cursor:
            for label, (resolved, request_filter, allowed_types) in variants.items():
                where_clause, params = self._where_clause(
                    resolved,
                    request_filter,
                    product_ids=product_ids,
                    fallback_term=fallback_term,
                    allowed_ad_types=allowed_types,
                )
                cursor.execute(
                    f"SELECT /* querix:gainr_zero_result_diagnostics */ "
                    f"COUNT(DISTINCT sr.id) AS total "
                    f"{join} WHERE {where_clause}",
                    params,
                )
                counts[label] = int(cursor.fetchone()["total"])
        return counts

    @retry_transient_mysql_read
    def hydrate_filtered(
        self,
        product_ids: list[Any],
        resolved_filters: dict,
        request_filter: GainrSearchFilter,
        allowed_ad_types: set[str] | None,
    ) -> list[dict]:
        if not product_ids:
            return []
        where_clause, params = self._where_clause(
            resolved_filters,
            request_filter,
            product_ids=product_ids,
            allowed_ad_types=allowed_ad_types,
        )
        with self.connection() as connection:
            with connection.cursor() as cursor:
                if self.serves_cards_from_search_ready:
                    cursor.execute(
                        f"""
                        SELECT sr.*,
                               sr.city_name AS __city_name,
                               sr.locality_name AS __locality_name
                        FROM {self.search_table} AS sr
                        WHERE {where_clause}
                        """,
                        params,
                    )
                else:
                    cursor.execute(
                        f"""
                        SELECT a.*, sr.city_name AS __city_name,
                               sr.locality_name AS __locality_name
                        FROM {self.search_table} AS sr
                        JOIN {self.result_table} AS a ON a.id = sr.id
                        WHERE {where_clause}
                        """,
                        params,
                    )
                rows = list(cursor.fetchall())
            rows_by_id = {str(row[self.config.result_id_column]): row for row in rows}
            ordered = [
                rows_by_id[str(product_id)]
                for product_id in product_ids
                if str(product_id) in rows_by_id
            ]
            if self.serves_cards_from_search_ready:
                self._relations.attach_search_ready(ordered)
            else:
                # Reuse the connection that just completed the main hydration.
                # Opening relation work across three pooled remote connections can
                # make a small read pay idle-socket validation or reconnect costs.
                self._attach_attributes(ordered, connection=connection)
        return ordered

    @retry_transient_mysql_read
    def hydrate_ranked_page(
        self,
        product_ids: list[Any],
        resolved_filters: dict,
        request_filter: GainrSearchFilter,
        allowed_ad_types: set[str] | None,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[dict], int]:
        """Filter ranked IDs, then hydrate only the requested semantic page."""
        # Retrieval and cached results can mix integer and string IDs. MariaDB
        # cannot use the ID range lookup for a mixed-type IN list, so bind one
        # consistent type while preserving the first occurrence of each ID.
        ranked_ids = _unique([str(product_id) for product_id in product_ids])
        if not ranked_ids:
            return [], 0
        where_clause, where_params = self._where_clause(
            resolved_filters,
            request_filter,
            product_ids=ranked_ids,
            allowed_ad_types=allowed_ad_types,
        )
        offset = (page - 1) * page_size
        if self.serves_cards_from_search_ready:
            return self._hydrate_ranked_search_ready_page(
                ranked_ids,
                where_clause,
                where_params,
                offset=offset,
                page_size=page_size,
            )
        hydration_started = time.perf_counter()
        checkout_started = hydration_started
        parallel_relations = (
            self.database_pool is not None and self.config.pool_max_size >= 12
        )
        with self.connection() as connection:
            checkout_ms = round((time.perf_counter() - checkout_started) * 1000)
            cards_started = time.perf_counter()
            rows = []
            total = 0
            rank_placeholders = ", ".join("%s" for _ in ranked_ids)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT page_ad.*,
                           ranked.__city_name,
                           ranked.__locality_name,
                           ranked.__eligible_total
                    FROM (
                        SELECT sr.id AS __ranked_id,
                               sr.city_name AS __city_name,
                               sr.locality_name AS __locality_name,
                               COUNT(*) OVER () AS __eligible_total,
                               FIELD(
                                   sr.id, {rank_placeholders}
                               ) AS __rank_order
                        FROM {self.search_table} AS sr
                        JOIN {self.result_table} AS a ON a.id = sr.id
                        WHERE {where_clause}
                        ORDER BY __rank_order
                        LIMIT %s OFFSET %s
                    ) AS ranked
                    JOIN {self.result_table} AS page_ad
                      ON page_ad.id = ranked.__ranked_id
                    ORDER BY ranked.__rank_order
                    """,
                    (*ranked_ids, *where_params, page_size, offset),
                )
                rows = list(cursor.fetchall())
            if rows:
                total = int(rows[0].get("__eligible_total") or 0)
                for row in rows:
                    row.pop("__eligible_total", None)
            elif offset:
                # A page beyond the last result has no window row carrying the
                # total. Preserve the legacy pagination contract for that rare
                # case with a count-only fallback.
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT COUNT(*) AS total
                        FROM {self.search_table} AS sr
                        JOIN {self.result_table} AS a ON a.id = sr.id
                        WHERE {where_clause}
                        """,
                        where_params,
                    )
                    total = int(cursor.fetchone()["total"])
            cards_ms = round((time.perf_counter() - cards_started) * 1000)
            eligibility_ms = 0
            relation_timings = (
                {}
                if parallel_relations
                else self._attach_attributes(rows, connection=connection)
            )
        if parallel_relations:
            # Three independent relation reads each pay remote-MySQL latency.
            # A >=12 connection tenant pool can serve four bounded searches
            # without exceeding its configured pool limit.
            relation_timings = self._attach_attributes(rows)
        logger.info(
            "Gainr ranked hydration timing rows=%s checkout_ms=%s "
            "eligibility_ms=%s cards_ms=%s "
            "attributes_ms=%s service_counts_ms=%s users_ms=%s total_ms=%s",
            len(rows),
            checkout_ms,
            eligibility_ms,
            cards_ms,
            relation_timings.get("attributes", 0),
            relation_timings.get("service_counts", 0),
            relation_timings.get("users", 0),
            round((time.perf_counter() - hydration_started) * 1000),
        )
        return rows, total

    def _hydrate_ranked_search_ready_page(
        self,
        ranked_ids: list[Any],
        where_clause: str,
        where_params: list[Any],
        *,
        offset: int,
        page_size: int,
    ) -> tuple[list[dict], int]:
        started = time.perf_counter()
        rank_placeholders = ", ".join("%s" for _ in ranked_ids)
        rows: list[dict] = []
        total = 0
        checkout_started = time.perf_counter()
        with self.connection() as connection, connection.cursor() as cursor:
            checkout_ms = (time.perf_counter() - checkout_started) * 1000
            query_started = time.perf_counter()
            cursor.execute(
                f"""
                SELECT /* querix:gainr_ranked_search_ready_page */ sr.*,
                       sr.city_name AS __city_name,
                       sr.locality_name AS __locality_name,
                       COUNT(*) OVER () AS __eligible_total,
                       FIELD(sr.id, {rank_placeholders}) AS __rank_order
                FROM {self.search_table} AS sr
                WHERE {where_clause}
                ORDER BY __rank_order
                LIMIT %s OFFSET %s
                """,
                (*ranked_ids, *where_params, page_size, offset),
            )
            rows = list(cursor.fetchall())
            query_ms = (time.perf_counter() - query_started) * 1000
            if rows:
                total = int(rows[0].get("__eligible_total") or 0)
                for row in rows:
                    row.pop("__eligible_total", None)
                    row.pop("__rank_order", None)
            elif offset:
                count_started = time.perf_counter()
                cursor.execute(
                    f"""
                    SELECT /* querix:gainr_ranked_search_ready_count */
                           COUNT(*) AS total
                    FROM {self.search_table} AS sr
                    WHERE {where_clause}
                    """,
                    where_params,
                )
                total = int(cursor.fetchone()["total"])
                query_ms += (time.perf_counter() - count_started) * 1000
        decode_started = time.perf_counter()
        self._relations.attach_search_ready(rows)
        relation_decode_ms = (time.perf_counter() - decode_started) * 1000
        total_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "Gainr search-ready hydration "
            "digest=gainr_ranked_search_ready_page rows=%s "
            "checkout_ms=%s query_ms=%s relation_decode_ms=%s total_ms=%s",
            len(rows),
            round(checkout_ms),
            round(query_ms),
            round(relation_decode_ms),
            round(total_ms),
        )
        return rows, total

    def _attach_attributes(
        self, rows: list[dict], *, connection=None
    ) -> dict[str, int]:
        return self._relations.attach(rows, connection=connection)
