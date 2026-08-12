import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
        conditions = []
        card_alias = "sr" if self.serves_cards_from_search_ready else "a"
        if not self.serves_cards_from_search_ready:
            conditions.append("(a.deleted_at IS NULL OR TRIM(a.deleted_at) = '')")
        if self.search_active_qualified_condition:
            conditions.insert(0, self.search_active_qualified_condition)
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
                    f"(({card_alias}.type = %s AND "
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
                f"{card_alias}.type",
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
                    f"{card_alias}.is_rent_negotiable",
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
        elif fallback_term and not resolved_filters.get("categorical"):
            contains = f"%{fallback_term}%"
            conditions.append("(sr.title LIKE %s OR sr.bm25_content LIKE %s)")
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

        if self.serves_cards_from_search_ready:
            rows: list[dict] = []
            total = 0
            with self.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT sr.*,
                           sr.city_name AS __city_name,
                           sr.locality_name AS __locality_name,
                           COUNT(*) OVER () AS __eligible_total
                    FROM {self.search_table} AS sr
                    WHERE {where_clause}
                    ORDER BY {order}
                    LIMIT %s OFFSET %s
                    """,
                    (*params, page_size, offset),
                )
                rows = list(cursor.fetchall())
                if rows:
                    total = int(rows[0].pop("__eligible_total", 0) or 0)
                    for row in rows[1:]:
                        row.pop("__eligible_total", None)
                elif offset:
                    cursor.execute(
                        f"""
                        SELECT COUNT(*) AS total
                        FROM {self.search_table} AS sr
                        WHERE {where_clause}
                        """,
                        params,
                    )
                    total = int(cursor.fetchone()["total"])
            self._attach_search_ready_relations(rows)
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
                self._attach_search_ready_relations(ordered)
            else:
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
        """Filter ranked IDs, then hydrate only the requested semantic page."""
        ranked_ids = _unique(product_ids)
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
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT sr.*,
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
            if rows:
                total = int(rows[0].get("__eligible_total") or 0)
                for row in rows:
                    row.pop("__eligible_total", None)
                    row.pop("__rank_order", None)
            elif offset:
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM {self.search_table} AS sr
                    WHERE {where_clause}
                    """,
                    where_params,
                )
                total = int(cursor.fetchone()["total"])
        self._attach_search_ready_relations(rows)
        logger.info(
            "Gainr search-ready hydration rows=%s total_ms=%s",
            len(rows),
            round((time.perf_counter() - started) * 1000),
        )
        return rows, total

    @staticmethod
    def _json_list(value: Any) -> list[dict]:
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        if not isinstance(value, str) or not value.strip():
            return []
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            logger.warning("Gainr search-ready relation JSON is invalid")
            return []
        if not isinstance(parsed, list):
            return []
        return [dict(item) for item in parsed if isinstance(item, dict)]

    def _attach_search_ready_relations(self, rows: list[dict]) -> None:
        for row in rows:
            row["__ads_attributes"] = self._json_list(
                row.pop("ads_attributes_json", None)
            )
            user_id = row.get("user_id")
            if user_id in (None, ""):
                row["__user"] = None
                continue
            row["__user"] = {
                "id": user_id,
                "prosper_id": row.pop("user_prosper_id", None),
                "name": row.pop("user_name", None),
                "photo": row.pop("user_photo", None),
                "is_aadhaar_gst_verified": row.pop(
                    "user_is_aadhaar_gst_verified",
                    None,
                ),
            }

    def _attach_attributes(
        self,
        rows: list[dict],
        *,
        connection=None,
    ) -> dict[str, int]:
        if self.serves_cards_from_search_ready:
            self._attach_search_ready_relations(rows)
            return {}
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
            [row.get("user_id") for row in rows if row.get("user_id") not in (None, "")]
        )

        def fetch_attributes(active_connection) -> list[dict]:
            started = time.perf_counter()
            try:
                with active_connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT ads_id, attribute_id, value
                        FROM {quote_mysql_identifier("ads_attributes")}
                        WHERE ads_id IN ({placeholders})
                          AND (deleted_at IS NULL OR TRIM(deleted_at) = '')
                        ORDER BY id
                        """,
                        product_ids,
                    )
                    return list(cursor.fetchall())
            finally:
                timings["attributes"] = round((time.perf_counter() - started) * 1000)

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
                quote_mysql_identifier(field) for field in GAINR_USER_FIELDS
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
                timings["users"] = round((time.perf_counter() - started) * 1000)

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
                        logger.exception("Gainr service count hydration failed")
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
