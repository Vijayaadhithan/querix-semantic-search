import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from core.tenant_config import TenantProfile
from storage.mysql import (
    MySQLRuntimeConfig,
    mysql_connection,
    quote_mysql_identifier,
    require_pymysql,
)
from tenants.gainr.models import (
    DURATION_ORDER,
    GAINR_USER_FIELDS,
    GainrSearchFilter,
    _unique,
)

logger = logging.getLogger("uvicorn.error")

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
        self.search_table = quote_mysql_identifier(
            self.config.search_table
        )
        self.result_table = quote_mysql_identifier(
            self.config.result_table
        )
        self.users_table = quote_mysql_identifier(
            self.profile.compatibility.users_table
        )
        self._users_table_available: bool | None = None

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

    @contextmanager
    def _connection_scope(self, connection=None):
        if connection is not None:
            yield connection
            return
        with self.connection() as active_connection:
            yield active_connection

    def suggestions(self, term: str, limit: int) -> list[str]:
        prefix = f"{term}%"
        query = f"""
            SELECT DISTINCT name AS value
            FROM {quote_mysql_identifier('sub_categories')}
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
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    (prefix, term, limit),
                )
                return [
                    str(row["value"])
                    for row in cursor.fetchall()
                    if row.get("value")
                ]

    def filter_data(self, city_id: int) -> tuple[list[str], list[dict]]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT DISTINCT rental_duration
                    FROM {self.search_table}
                    WHERE city_id = %s
                      AND rental_duration IS NOT NULL
                      AND TRIM(rental_duration) <> ''
                    """,
                    (city_id,),
                )
                durations = [
                    str(row["rental_duration"])
                    for row in cursor.fetchall()
                ]
                cursor.execute(
                    f"""
                    SELECT DISTINCT id AS locality_id,
                                    area AS locality_name
                    FROM {quote_mysql_identifier('locations')}
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

    @staticmethod
    def _append_condition(
        conditions: list[str],
        params: list[Any],
        expression: str,
        value,
    ) -> None:
        if isinstance(value, (list, tuple, set)):
            values = list(dict.fromkeys(value))
            if not values:
                return
            placeholders = ", ".join("%s" for _ in values)
            conditions.append(f"{expression} IN ({placeholders})")
            params.extend(values)
            return
        conditions.append(f"{expression} = %s")
        params.append(value)

    def _where_clause(
        self,
        resolved_filters: dict,
        request_filter: GainrSearchFilter,
        *,
        product_ids: list[Any] | None = None,
        fallback_term: str = "",
        allowed_ad_types: set[str] | None = None,
    ) -> tuple[str, list[Any]]:
        conditions = [
            "(a.deleted_at IS NULL OR TRIM(a.deleted_at) = '')"
        ]
        params: list[Any] = []
        column_map = {
            "main_category_name": "sr.main_category_name",
            "subcategory_name": "sr.subcategory_name",
            "state_name": "sr.state_name",
            "city_name": "sr.city_name",
            "locality_name": "sr.locality_name",
            "rental_duration": "sr.rental_duration",
            "main_category_id": "sr.main_category_id",
            "subcategory_id": "sr.subcategory_id",
            "state_id": "sr.state_id",
            "city_id": "sr.city_id",
            "locality_id": "sr.locality_id",
        }
        for key, value in resolved_filters.get("categorical", {}).items():
            expression = column_map.get(key)
            if expression is not None:
                self._append_condition(
                    conditions,
                    params,
                    expression,
                    value,
                )
        minimum = resolved_filters.get("min_rental_fee")
        maximum = resolved_filters.get("max_rental_fee")
        if minimum is not None or maximum is not None:
            priced_conditions = ["sr.rental_fee > 1"]
            priced_params = []
            if minimum is not None:
                priced_conditions.append("sr.rental_fee >= %s")
                priced_params.append(minimum)
            if maximum is not None:
                priced_conditions.append("sr.rental_fee <= %s")
                priced_params.append(maximum)
            priced_clause = " AND ".join(priced_conditions)
            if allowed_ad_types is not None and "2" in allowed_ad_types:
                conditions.append(
                    "((a.type = %s AND "
                    "(sr.rental_fee IS NULL OR sr.rental_fee <= 1)) "
                    f"OR ({priced_clause}))"
                )
                params.append("2")
            else:
                conditions.append(f"({priced_clause})")
            params.extend(priced_params)
        if allowed_ad_types is not None:
            self._append_condition(
                conditions,
                params,
                "a.type",
                sorted(allowed_ad_types),
            )
        if request_filter.fee:
            compatibility = self.profile.compatibility
            negotiable_values = []
            for value in request_filter.fee:
                if value == compatibility.fixed_fee_id:
                    negotiable_values.append(0)
                elif value == compatibility.negotiable_fee_id:
                    negotiable_values.append(1)
            if negotiable_values:
                self._append_condition(
                    conditions,
                    params,
                    "a.is_rent_negotiable",
                    negotiable_values,
                )
        if product_ids is not None:
            if not product_ids:
                conditions.append("1 = 0")
            else:
                self._append_condition(
                    conditions,
                    params,
                    "sr.id",
                    product_ids,
                )
        elif (
            fallback_term
            and not resolved_filters.get("categorical")
        ):
            contains = f"%{fallback_term}%"
            conditions.append(
                "(sr.title LIKE %s OR sr.bm25_content LIKE %s)"
            )
            params.extend((contains, contains))
        return " AND ".join(conditions), params

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
        offset = (page - 1) * page_size

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
                    (*params, page_size, offset),
                )
                return list(cursor.fetchall())

        def run_with_connection(fetcher):
            with self.connection() as connection:
                return fetcher(connection)

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
            rows_by_id = {
                str(row[self.config.result_id_column]): row
                for row in rows
            }
            ordered = [
                rows_by_id[str(product_id)]
                for product_id in product_ids
                if str(product_id) in rows_by_id
            ]
            # Reuse the connection that just completed the main hydration.
            # Opening relation work across three pooled remote connections can
            # make a small read pay idle-socket validation or reconnect costs.
            self._attach_attributes(ordered, connection=connection)
        return ordered

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
        """Filter, count, and hydrate one semantic page in one main query."""
        ranked_ids = _unique(product_ids)
        if not ranked_ids:
            return [], 0
        where_clause, where_params = self._where_clause(
            resolved_filters,
            request_filter,
            product_ids=ranked_ids,
            allowed_ad_types=allowed_ad_types,
        )
        rank_placeholders = ", ".join("%s" for _ in ranked_ids)
        offset = (page - 1) * page_size
        hydration_started = time.perf_counter()
        checkout_started = hydration_started
        with self.connection() as connection:
            checkout_ms = round(
                (time.perf_counter() - checkout_started) * 1000
            )
            main_started = time.perf_counter()
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT a.*, sr.city_name AS __city_name,
                           sr.locality_name AS __locality_name,
                           COUNT(*) OVER () AS __eligible_total
                    FROM {self.search_table} AS sr
                    JOIN {self.result_table} AS a ON a.id = sr.id
                    WHERE {where_clause}
                    ORDER BY FIELD(sr.id, {rank_placeholders})
                    LIMIT %s OFFSET %s
                    """,
                    (
                        *where_params,
                        *ranked_ids,
                        page_size,
                        offset,
                    ),
                )
                rows = list(cursor.fetchall())
            total = (
                int(rows[0].get("__eligible_total") or 0)
                if rows
                else 0
            )
            if not rows and page > 1:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT COUNT(DISTINCT sr.id) AS total
                        FROM {self.search_table} AS sr
                        JOIN {self.result_table} AS a ON a.id = sr.id
                        WHERE {where_clause}
                        """,
                        where_params,
                    )
                    total = int(cursor.fetchone()["total"])

            for row in rows:
                row.pop("__eligible_total", None)
            main_ms = round((time.perf_counter() - main_started) * 1000)
            # These relation reads are small primary/index lookups. Keeping
            # them on the already-proven connection is both faster and more
            # predictable than fanning out over idle remote MySQL sockets.
            relation_timings = self._attach_attributes(
                rows,
                connection=connection,
            )
        logger.info(
            "Gainr ranked hydration timing rows=%s checkout_ms=%s main_ms=%s "
            "attributes_ms=%s service_counts_ms=%s users_ms=%s total_ms=%s",
            len(rows),
            checkout_ms,
            main_ms,
            relation_timings.get("attributes", 0),
            relation_timings.get("service_counts", 0),
            relation_timings.get("users", 0),
            round((time.perf_counter() - hydration_started) * 1000),
        )
        return rows, total

    def filter_product_ids(
        self,
        product_ids: list[Any],
        resolved_filters: dict,
        request_filter: GainrSearchFilter,
        allowed_ad_types: set[str] | None,
    ) -> list[Any]:
        """Return eligible IDs in semantic rank order without hydrating cards."""
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
                search_id = quote_mysql_identifier(
                    self.config.search_id_column
                )
                cursor.execute(
                    f"""
                    SELECT sr.{search_id} AS __search_id
                    FROM {self.search_table} AS sr
                    JOIN {self.result_table} AS a ON a.id = sr.id
                    WHERE {where_clause}
                    """,
                    params,
                )
                eligible = {
                    str(row["__search_id"])
                    for row in cursor.fetchall()
                }
        return [
            product_id
            for product_id in product_ids
            if str(product_id) in eligible
        ]

    def _attach_attributes(
        self,
        rows: list[dict],
        *,
        connection=None,
    ) -> dict[str, int]:
        product_ids = [
            row.get(self.config.result_id_column)
            for row in rows
            if row.get(self.config.result_id_column) is not None
        ]
        if not product_ids:
            return {}
        placeholders = ", ".join("%s" for _ in product_ids)
        attributes = []
        service_counts = []
        users = []
        timings: dict[str, int] = {}
        user_ids = _unique(
            [
                row.get("user_id")
                for row in rows
                if row.get("user_id") not in (None, "")
            ]
        )

        def fetch_attributes(active_connection) -> list[dict]:
            started = time.perf_counter()
            try:
                with active_connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT ads_id, attribute_id, value
                        FROM {quote_mysql_identifier('ads_attributes')}
                        WHERE ads_id IN ({placeholders})
                          AND (deleted_at IS NULL OR TRIM(deleted_at) = '')
                        ORDER BY id
                        """,
                        product_ids,
                    )
                    return list(cursor.fetchall())
            finally:
                timings["attributes"] = round(
                    (time.perf_counter() - started) * 1000
                )

        def fetch_service_counts(active_connection) -> list[dict]:
            if not user_ids:
                return []
            user_placeholders = ", ".join("%s" for _ in user_ids)
            started = time.perf_counter()
            try:
                with active_connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT user_id, COUNT(*) AS service_ad_count
                        FROM {self.result_table}
                        WHERE user_id IN ({user_placeholders})
                          AND category_type = 2
                          AND status = 1
                          AND (
                              deleted_at IS NULL
                              OR TRIM(deleted_at) = ''
                          )
                        GROUP BY user_id
                        """,
                        user_ids,
                    )
                    return list(cursor.fetchall())
            finally:
                timings["service_counts"] = round(
                    (time.perf_counter() - started) * 1000
                )

        def fetch_users(active_connection) -> list[dict]:
            if not user_ids or self._users_table_available is False:
                return []
            user_placeholders = ", ".join("%s" for _ in user_ids)
            selected_fields = ", ".join(
                quote_mysql_identifier(field)
                for field in GAINR_USER_FIELDS
            )
            started = time.perf_counter()
            try:
                with active_connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT {selected_fields}
                        FROM {self.users_table}
                        WHERE id IN ({user_placeholders})
                        """,
                        user_ids,
                    )
                    return list(cursor.fetchall())
            finally:
                timings["users"] = round(
                    (time.perf_counter() - started) * 1000
                )

        def run_with_connection(fetcher) -> list[dict]:
            with self.connection() as active_connection:
                return fetcher(active_connection)

        if self.database_pool is not None and connection is None:
            with ThreadPoolExecutor(
                max_workers=3,
                thread_name_prefix="gainr-relations",
            ) as executor:
                attributes_future = executor.submit(
                    run_with_connection,
                    fetch_attributes,
                )
                service_counts_future = (
                    executor.submit(
                        run_with_connection,
                        fetch_service_counts,
                    )
                    if user_ids
                    else None
                )
                users_future = (
                    executor.submit(
                        run_with_connection,
                        fetch_users,
                    )
                    if user_ids and self._users_table_available is not False
                    else None
                )
                try:
                    attributes = attributes_future.result()
                except Exception:
                    logger.exception("Gainr ad attribute hydration failed")
                if service_counts_future is not None:
                    try:
                        service_counts = service_counts_future.result()
                    except Exception:
                        logger.exception(
                            "Gainr service count hydration failed"
                        )
                if users_future is not None:
                    try:
                        users = users_future.result()
                        self._users_table_available = True
                    except Exception as exc:
                        self._handle_user_hydration_error(exc)
        else:
            try:
                with self._connection_scope(connection) as active_connection:
                    attributes = fetch_attributes(active_connection)
                    service_counts = fetch_service_counts(active_connection)
            except Exception:
                logger.exception("Gainr ad relation hydration failed")
            if user_ids and self._users_table_available is not False:
                try:
                    with self._connection_scope(connection) as active_connection:
                        users = fetch_users(active_connection)
                    self._users_table_available = True
                except Exception as exc:
                    self._handle_user_hydration_error(exc)

        by_product: dict[str, list[dict]] = {}
        for attribute in attributes:
            by_product.setdefault(
                str(attribute.get("ads_id")),
                [],
            ).append(dict(attribute))
        counts_by_user = {
            str(item.get("user_id")): item.get("service_ad_count", 0)
            for item in service_counts
        }
        users_by_id = {
            str(item.get("id")): dict(item)
            for item in users
            if item.get("id") not in (None, "")
        }
        for row in rows:
            row["__ads_attributes"] = by_product.get(
                str(row.get(self.config.result_id_column)),
                [],
            )
            row["service_ad_count"] = counts_by_user.get(
                str(row.get("user_id")),
                0,
            )
            row["__user"] = users_by_id.get(str(row.get("user_id")))
        return timings

    def _handle_user_hydration_error(self, exc: Exception) -> None:
        if exc.args and exc.args[0] == 1146:
            self._users_table_available = False
            logger.warning(
                "Gainr users table %s is missing; user fields "
                "will remain null until it is imported and the "
                "API is restarted",
                self.profile.compatibility.users_table,
            )
        else:
            logger.exception("Gainr user hydration failed")
