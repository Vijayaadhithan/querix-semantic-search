"""Authoritative relations and ETL-projected relation hydration for Gainr cards."""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from core.tenant_config import TenantProfile
from storage.mysql import (
    quote_mysql_identifier,
)
from tenants.gainr.models import (
    GAINR_USER_FIELDS,
    _unique,
)

logger = logging.getLogger("uvicorn.error")


class GainrRelationHydrator:
    """Enrich card rows with bounded relation queries and user-table fallback."""

    def __init__(self, profile: TenantProfile, connection_factory, database_pool):
        self.profile = profile
        self.config = profile.database
        self.connection = connection_factory
        self.database_pool = database_pool
        self.result_table = quote_mysql_identifier(self.config.result_table)
        self.users_table = quote_mysql_identifier(profile.compatibility.users_table)
        self.serves_cards_from_search_ready = (
            profile.compatibility.serves_cards_from_search_ready
        )
        self._users_table_available: bool | None = None

    @contextmanager
    def _connection_scope(self, connection=None):
        if connection is not None:
            yield connection
            return
        with self.connection() as active_connection:
            yield active_connection

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

    def attach_search_ready(self, rows: list[dict]) -> None:
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

    def attach(
        self,
        rows: list[dict],
        *,
        connection=None,
    ) -> dict[str, int]:
        if self.serves_cards_from_search_ready:
            self.attach_search_ready(rows)
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
