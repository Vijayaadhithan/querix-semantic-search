from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
import threading
import time
from contextlib import contextmanager
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from mysql_store import (
    MySQLRuntimeConfig,
    mysql_connection,
    quote_mysql_identifier,
    require_pymysql,
)
from tenant_config import TenantProfile


logger = logging.getLogger(__name__)
PERFORMANCE_LOGGER = logging.getLogger("uvicorn.error")

DURATION_ORDER = {
    value: index
    for index, value in enumerate(
        ("Per Hour", "Per Day", "Per Week", "Per Month", "Per Ride")
    )
}
GAINR_CARD_INTEGER_FIELDS = (
    "id",
    "type",
    "user_id",
    "category_type",
    "parent_id",
    "category_id",
    "is_rent_negotiable",
    "city_id",
    "locality_id",
    "total_favorite",
    "total_like",
    "status",
    "service_ad_count",
    "boost_ad_count",
    "is_aadhar_gst_verified_count",
)
GAINR_USER_FIELDS = (
    "id",
    "prosper_id",
    "name",
    "photo",
    "email",
    "available_credit",
    "provider",
    "provider_id",
    "phone",
    "state_id",
    "city_id",
    "gender",
    "location",
    "availability",
    "role",
    "email_verified_at",
    "mobile_verified_at",
    "privacy_enabled",
    "fwd_otp",
    "fwd_is_verified",
    "valid_till",
    "is_verified",
    "gst",
    "aadhar",
    "platform",
    "fcm_token",
    "device_platform",
    "created_at",
    "updated_at",
    "status",
    "prosper_page_view_count",
    "edit_photo",
    "profile_communication",
    "contact_view_count",
    "reg_geo_city",
    "reg_geo_latitude",
    "reg_geo_longitude",
    "reg_device_details",
    "last_geo_city",
    "last_geo_latitude",
    "last_geo_longitude",
    "last_device_details",
    "user_type",
    "deleted_at",
    "is_aadhaar_gst_verified",
    "upi_id",
    "survey_language_id",
    "is_survey_personal_completed",
    "delete_user_remark",
    "trip_flag",
    "contact_view_plan_id",
    "contact_views_count",
    "free_contact_start_date",
    "contact_plan_start_date",
)
GAINR_USER_INTEGER_FIELDS = (
    "id",
    "phone",
    "state_id",
    "city_id",
    "gender",
    "privacy_enabled",
    "fwd_is_verified",
    "is_verified",
    "platform",
    "device_platform",
    "status",
    "prosper_page_view_count",
    "contact_view_count",
    "user_type",
    "is_aadhaar_gst_verified",
    "survey_language_id",
    "is_survey_personal_completed",
    "trip_flag",
    "contact_view_plan_id",
    "contact_views_count",
)


def _unique(values: list[Any]) -> list[Any]:
    return list(dict.fromkeys(values))


class GainrSuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str = Field(min_length=1, max_length=250)

    @field_validator("term")
    @classmethod
    def normalize_term(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("term must not be blank")
        return value


class GainrFilterDataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city_id: int = Field(gt=0)


class GainrSearchFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city_id: int | None = Field(default=None, gt=0)
    subcategory_id: int | str | None = ""
    locality_id: list[int] = Field(default_factory=list)
    rental_duration: list[str] = Field(default_factory=list)
    ad_type: list[int] = Field(default_factory=list)
    fee: list[int] = Field(default_factory=list)
    min_fee: float | None = Field(default=None, ge=0)
    max_fee: float | None = Field(default=None, ge=0)

    @field_validator("subcategory_id", mode="before")
    @classmethod
    def normalize_subcategory(cls, value):
        if value in (None, ""):
            return ""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return ""
            if value.isdigit():
                return int(value)
            raise ValueError(
                "subcategory_id must be a numeric ID or an empty string"
            )
        return value

    @field_validator("locality_id")
    @classmethod
    def validate_localities(cls, values: list[int]) -> list[int]:
        if any(value <= 0 for value in values):
            raise ValueError("locality_id values must be positive")
        return _unique(values)

    @field_validator("rental_duration")
    @classmethod
    def normalize_durations(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()) for value in values]
        if any(not value for value in normalized):
            raise ValueError("rental_duration values must not be blank")
        return _unique(normalized)

    @field_validator("ad_type")
    @classmethod
    def validate_ad_types(cls, values: list[int]) -> list[int]:
        values = _unique(values)
        if any(value not in {1, 2} for value in values):
            raise ValueError("ad_type supports only 1 (offer) and 2 (need)")
        return values

    @field_validator("fee")
    @classmethod
    def validate_fee_types(cls, values: list[int]) -> list[int]:
        return _unique(values)

    @model_validator(mode="after")
    def validate_fee_range(self):
        if (
            self.min_fee is not None
            and self.max_fee is not None
            and self.min_fee > self.max_fee
        ):
            raise ValueError("min_fee must not be greater than max_fee")
        return self


class GainrFilterResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    searchTerm: str = Field(default="", max_length=1000)
    filter: GainrSearchFilter = Field(default_factory=GainrSearchFilter)
    page: int = Field(default=1, ge=1)

    @field_validator("searchTerm")
    @classmethod
    def normalize_search_term(cls, value: str) -> str:
        return " ".join(value.split())


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
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(DISTINCT sr.id) AS total "
                    f"{join} WHERE {where_clause}",
                    params,
                )
                total = int(cursor.fetchone()["total"])
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
                rows = list(cursor.fetchall())
            self._attach_attributes(rows, connection=connection)
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
            self._attach_attributes(ordered, connection=connection)
        return ordered

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
    ) -> None:
        product_ids = [
            row.get(self.config.result_id_column)
            for row in rows
            if row.get(self.config.result_id_column) is not None
        ]
        if not product_ids:
            return
        placeholders = ", ".join("%s" for _ in product_ids)
        attributes = []
        service_counts = []
        users = []
        user_ids = _unique(
            [
                row.get("user_id")
                for row in rows
                if row.get("user_id") not in (None, "")
            ]
        )
        try:
            with self._connection_scope(connection) as active_connection:
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
                    attributes = cursor.fetchall()
                    if user_ids:
                        user_placeholders = ", ".join(
                            "%s" for _ in user_ids
                        )
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
                        service_counts = cursor.fetchall()
        except Exception:
            logger.exception("Gainr ad relation hydration failed")
        if user_ids and self._users_table_available is not False:
            user_placeholders = ", ".join("%s" for _ in user_ids)
            selected_fields = ", ".join(
                quote_mysql_identifier(field)
                for field in GAINR_USER_FIELDS
            )
            try:
                with self._connection_scope(connection) as active_connection:
                    with active_connection.cursor() as cursor:
                        cursor.execute(
                            f"""
                            SELECT {selected_fields}
                            FROM {self.users_table}
                            WHERE id IN ({user_placeholders})
                            """,
                            user_ids,
                        )
                        users = list(cursor.fetchall())
                self._users_table_available = True
            except Exception as exc:
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


class GainrCompatibilityService:
    def __init__(
        self,
        profile: TenantProfile,
        product_search_service,
        shared_cache=None,
        repository: GainrDatabaseRepository | None = None,
    ):
        if profile.compatibility.adapter != "gainr_legacy":
            raise ValueError(
                f"Tenant {profile.company_id!r} has not enabled gainr_legacy"
            )
        self.profile = profile
        self.product_search_service = product_search_service
        self.engine = product_search_service.engine
        self.shared_cache = shared_cache
        self.repository = repository or GainrDatabaseRepository(
            profile,
            getattr(self.engine, "database_pool", None),
        )
        self._memory_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def parse_filter_result(
        self,
        payload: dict[str, Any],
    ) -> GainrFilterResultRequest:
        normalized = copy.deepcopy(payload)
        raw_filter = normalized.get("filter")
        if isinstance(raw_filter, dict):
            compatibility = self.profile.compatibility
            for configured, canonical in (
                (compatibility.min_fee_field, "min_fee"),
                (compatibility.max_fee_field, "max_fee"),
            ):
                if configured in raw_filter:
                    configured_value = raw_filter.pop(configured)
                    raw_filter.setdefault(canonical, configured_value)
        request = GainrFilterResultRequest.model_validate(normalized)
        supported_fee_ids = {
            self.profile.compatibility.fixed_fee_id,
            self.profile.compatibility.negotiable_fee_id,
        }
        unsupported = sorted(set(request.filter.fee) - supported_fee_ids)
        if unsupported:
            raise ValueError(f"Unsupported fee filter IDs: {unsupported}")
        return request

    def _cache_key(self, namespace: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"{self.profile.company_id}:{namespace}:{digest}"

    def _get_cached(self, key: str) -> dict[str, Any] | None:
        if self.shared_cache is not None:
            cached = self.shared_cache.get_json("gainr_compat", key)
            if cached is not None:
                return cached
        with self._lock:
            cached = self._memory_cache.get(key)
            if cached is None:
                return None
            expires_at, value = cached
            if expires_at <= time.monotonic():
                del self._memory_cache[key]
                return None
            return copy.deepcopy(value)

    def _set_cached(
        self,
        key: str,
        value: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        if self.shared_cache is not None:
            self.shared_cache.set_json(
                "gainr_compat",
                key,
                value,
                ttl_seconds,
            )
        with self._lock:
            self._memory_cache[key] = (
                time.monotonic() + ttl_seconds,
                copy.deepcopy(value),
            )

    def search_suggestions(
        self,
        request: GainrSuggestionRequest,
    ) -> dict[str, Any]:
        normalized = request.term.casefold()
        key = self._cache_key("suggestions", normalized)
        cached = self._get_cached(key)
        if cached is not None:
            return cached
        values = self.repository.suggestions(
            request.term,
            self.profile.compatibility.suggestions_limit,
        )
        response = {
            "status": True,
            "data": [{"value": value} for value in values],
        }
        self._set_cached(key, response, 300)
        return response

    def filter_data(
        self,
        request: GainrFilterDataRequest,
    ) -> dict[str, Any]:
        key = self._cache_key("filter_data", str(request.city_id))
        cached = self._get_cached(key)
        if cached is not None:
            return cached
        durations, localities = self.repository.filter_data(request.city_id)
        compatibility = self.profile.compatibility
        response = {
            "data": {
                "rental_duration": {
                    "title": "Duration",
                    "value": durations,
                },
                "ad_type": {
                    "title": "Ad Type",
                    "value": [
                        {"id": 1, "value": "Offer Ads"},
                        {"id": 2, "value": "Need Ads"},
                    ],
                },
                "fee": {
                    "title": "Fee Type",
                    "value": [
                        {
                            "id": compatibility.fixed_fee_id,
                            "value": "Fixed",
                        },
                        {
                            "id": compatibility.negotiable_fee_id,
                            "value": "Negotiable",
                        },
                    ],
                },
                "localityList": {
                    "title": "Locality",
                    "value": localities,
                },
            }
        }
        self._set_cached(key, response, 900)
        return response

    def _effective_plan(
        self,
        request: GainrFilterResultRequest,
    ) -> tuple[dict, dict, dict]:
        if request.searchTerm:
            planned = self.engine.plan(request.searchTerm)
        else:
            planned = {
                "query_plan": {
                    "semantic_query": "",
                    "keyword_query": "",
                    "target_ad_type": "offer",
                    "sort_order": None,
                    "execution_path": "deterministic_filter",
                    "inferred_categories": {},
                },
                "resolved_filters": {"categorical": {}},
                "unresolved_filters": {},
                "query_model_metrics": {},
                "seconds": 0.0,
                "plan_cache_hit": False,
            }
        auto_filters = copy.deepcopy(planned["resolved_filters"])
        effective = copy.deepcopy(auto_filters)
        categorical = effective.setdefault("categorical", {})
        ignored: dict[str, Any] = {}
        explicit = request.filter
        replacements = (
            (
                explicit.city_id,
                ("state_name", "city_name", "locality_name"),
                "city_id",
            ),
            (
                explicit.subcategory_id
                if explicit.subcategory_id not in ("", None)
                else None,
                ("main_category_name", "subcategory_name"),
                "subcategory_id",
            ),
            (
                explicit.locality_id or None,
                ("state_name", "city_name", "locality_name"),
                "locality_id",
            ),
            (
                explicit.rental_duration or None,
                ("rental_duration",),
                "rental_duration",
            ),
        )
        for value, auto_keys, structured_key in replacements:
            if value is None:
                continue
            # Frontend ID filters are authoritative. Clear both the inferred
            # field and related inferred parents/children; otherwise a user
            # changing Camera to Car can retain Audio & Video as the parent,
            # or changing Chennai to Bengaluru can retain Tamil Nadu/locality
            # constraints and incorrectly produce an empty result set.
            for auto_key in auto_keys:
                if auto_key in categorical:
                    ignored[auto_key] = categorical.pop(auto_key)
            categorical[structured_key] = value
        for field_name, value in (
            ("min_rental_fee", explicit.min_fee),
            ("max_rental_fee", explicit.max_fee),
        ):
            if value is None:
                continue
            if field_name in effective:
                ignored[field_name] = effective[field_name]
            effective[field_name] = value
        if explicit.ad_type:
            ignored["target_ad_type"] = planned["query_plan"].get(
                "target_ad_type"
            )
        return planned, effective, {
            "auto_filters": auto_filters,
            "ignored_auto_filters": ignored,
        }

    def filter_results(
        self,
        request: GainrFilterResultRequest,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        request_started = time.perf_counter()
        engine_ms = 0.0
        database_ms = 0.0
        eligibility_ms = 0.0
        hydration_ms = 0.0
        usage_ms = 0.0
        trace_id = "-"
        eligibility_source = "database"
        planned, effective, meta = self._effective_plan(request)
        planning_ms = (time.perf_counter() - request_started) * 1000
        page_size = self.profile.compatibility.page_size
        execution_path = planned["query_plan"].get(
            "execution_path",
            "semantic",
        )
        allowed_ad_types = (
            {str(value) for value in request.filter.ad_type}
            if request.filter.ad_type
            else {
                "2"
                if planned["query_plan"].get("target_ad_type") == "wanted"
                else "1"
            }
        )
        database_only_filters = bool(
            effective.get("categorical")
            or effective.get("min_rental_fee") is not None
            or effective.get("max_rental_fee") is not None
            or request.filter.fee
        )
        if execution_path == "deterministic_filter":
            database_started = time.perf_counter()
            rows, total = self.repository.search_catalog(
                effective,
                request.filter,
                search_term=request.searchTerm,
                page=request.page,
                page_size=page_size,
                sort_order=planned["query_plan"].get("sort_order"),
                allowed_ad_types=allowed_ad_types,
            )
            database_ms = (
                time.perf_counter() - database_started
            ) * 1000
            route = "deterministic"
            usage_store = self.product_search_service.usage_store
            usage_started = time.perf_counter()
            if usage_store is not None:
                usage_store.record(
                    company_id=self.profile.company_id,
                    provider="internal",
                    model="deterministic_filter",
                    operation="search",
                    status="success",
                )
            usage_ms = (time.perf_counter() - usage_started) * 1000
            usage = {
                "tracked": usage_store is not None,
                "model_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "breakdown": [],
            }
            window_limited = False
        else:
            engine_started = time.perf_counter()
            result = self.product_search_service.run_engine_search(
                request.searchTerm,
                limit=self.product_search_service.max_results,
                ranking_window=(
                    self.profile.compatibility.semantic_ranked_window
                ),
                planned_result=planned,
                resolved_filters=effective,
                allowed_ad_types=allowed_ad_types,
                # The compatibility repository checks current eligibility and
                # hydrates the requested 20-row page. Avoid fetching the full
                # semantic result window first unless a price sort needs the
                # engine's complete row set. Unfiltered searches retain the
                # engine rows because they are cheaper than another remote-DB
                # eligibility round trip and can be reused below.
                hydrate_products=(
                    not database_only_filters
                    or planned["query_plan"].get("sort_order")
                    in {"price_asc", "price_desc"}
                ),
            )
            engine_ms = (time.perf_counter() - engine_started) * 1000
            trace_id = str(result.get("trace_id") or "-")
            eligibility_started = time.perf_counter()
            if database_only_filters or not result.get("products"):
                eligible_ids = self.repository.filter_product_ids(
                    result.get("product_ids", []),
                    effective,
                    request.filter,
                    allowed_ad_types,
                )
            else:
                eligibility_source = "engine_rows"
                id_column = self.repository.config.result_id_column
                current_rows = {
                    str(row[id_column]): row
                    for row in result.get("products", [])
                    if row.get(id_column) is not None
                }
                eligible_ids = []
                for product_id in result.get("product_ids", []):
                    row = current_rows.get(str(product_id))
                    if row is None:
                        continue
                    deleted_at = row.get("deleted_at")
                    if deleted_at is not None and str(deleted_at).strip():
                        continue
                    if (
                        allowed_ad_types is not None
                        and str(row.get("type")) not in allowed_ad_types
                    ):
                        continue
                    eligible_ids.append(product_id)
            eligibility_ms = (
                time.perf_counter() - eligibility_started
            ) * 1000
            total = len(eligible_ids)
            start = (request.page - 1) * page_size
            hydration_started = time.perf_counter()
            rows = self.repository.hydrate_filtered(
                eligible_ids[start : start + page_size],
                effective,
                request.filter,
                allowed_ad_types,
            )
            hydration_ms = (
                time.perf_counter() - hydration_started
            ) * 1000
            window_limited = (
                len(result.get("product_ids", []))
                >= self.product_search_service.max_results
            )
            route = "semantic"
            usage_started = time.perf_counter()
            usage = self.product_search_service._record_usage(result)
            usage_ms = (time.perf_counter() - usage_started) * 1000
        card_mapping_started = time.perf_counter()
        cards = [self._card(row) for row in rows]
        card_mapping_ms = (
            time.perf_counter() - card_mapping_started
        ) * 1000
        response: dict[str, Any] = {
            "status": True,
            "message": "",
            "data": cards,
            "current_page": request.page,
            "last_page": max(1, math.ceil(total / page_size)),
            "image_path": self.profile.compatibility.image_path,
        }
        if self.profile.compatibility.emit_search_meta:
            response["search_meta"] = {
                "route": route,
                **meta,
                "explicit_filters": request.filter.model_dump(),
                "effective_filters": effective,
                "total_results": total,
                "result_window_limited": (
                    route == "semantic" and window_limited
                ),
                "usage": usage,
            }
        recent_started = time.perf_counter()
        if request.page == 1 and request.searchTerm:
            self.remember_search(user_id, request.searchTerm)
        recent_ms = (time.perf_counter() - recent_started) * 1000
        duration_ms = (time.perf_counter() - request_started) * 1000
        PERFORMANCE_LOGGER.info(
            "[search:%s] step=compat_response status=complete route=%s "
            "engine_ms=%.0f database_ms=%.0f eligibility_source=%s "
            "eligibility_ms=%.0f "
            "hydration_ms=%.0f response_map_ms=%.0f usage_ms=%.0f "
            "recent_ms=%.0f products=%d duration_ms=%.0f",
            trace_id,
            route,
            engine_ms,
            database_ms,
            eligibility_source,
            eligibility_ms,
            hydration_ms,
            card_mapping_ms,
            usage_ms,
            recent_ms,
            len(cards),
            duration_ms,
        )
        if route == "deterministic":
            self.product_search_service.record_external_search(
                request.searchTerm,
                execution_path="deterministic_filter",
                duration_ms=duration_ms,
                products=len(cards),
                timeline=[
                    {
                        "step": "plan",
                        "status": "complete",
                        "duration_ms": round(planning_ms, 3),
                        "execution_path": "deterministic_filter",
                    },
                    {
                        "step": "database_filter",
                        "status": "complete",
                        "duration_ms": round(database_ms, 3),
                        "page_rows": len(rows),
                        "total_results": total,
                    },
                    {
                        "step": "response_map",
                        "status": "complete",
                        "duration_ms": round(card_mapping_ms, 3),
                        "products": len(cards),
                    },
                    {
                        "step": "filter_result",
                        "status": "complete",
                        "duration_ms": round(duration_ms, 3),
                        "products": len(cards),
                    },
                ],
            )
        return response

    @staticmethod
    def _integer(value):
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _number(value):
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        return int(number) if number.is_integer() else number

    def _user_payload(
        self,
        raw_user: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not raw_user:
            return None
        user = {}
        for field in GAINR_USER_FIELDS:
            value = raw_user.get(field)
            if isinstance(value, str) and value.strip().upper() == "NULL":
                value = None
            if field in GAINR_USER_INTEGER_FIELDS:
                value = self._integer(value)
            elif field == "available_credit":
                value = self._number(value)
            user[field] = value
        return user

    def _card(self, row: dict[str, Any]) -> dict[str, Any]:
        card = {
            field: row.get(field)
            for field in (
                "id",
                "type",
                "user_id",
                "category_type",
                "parent_id",
                "slug",
                "category_id",
                "title",
                "rental_duration",
                "rental_fee",
                "is_rent_negotiable",
                "city_id",
                "locality_id",
                "description",
                "photos",
                "total_favorite",
                "total_like",
                "status",
                "service_ad_count",
                "users_rating_count",
                "rating_avg",
                "boost_ad_count",
                "is_aadhar_gst_verified_count",
            )
        }
        for field in GAINR_CARD_INTEGER_FIELDS:
            card[field] = self._integer(card.get(field))
        for field in ("rental_fee", "rating_avg"):
            card[field] = self._number(card.get(field))
        card["service_ad_count"] = card.get("service_ad_count") or 0
        card["boost_ad_count"] = card.get("boost_ad_count") or 0
        card["is_aadhar_gst_verified_count"] = (
            card.get("is_aadhar_gst_verified_count") or 0
        )
        city_id = card.get("city_id")
        locality_id = card.get("locality_id")
        city_name = row.get("__city_name")
        locality_name = row.get("__locality_name")
        attributes = [
            {
                "ads_id": self._integer(attribute.get("ads_id")),
                "attribute_id": self._integer(
                    attribute.get("attribute_id")
                ),
                "value": self._integer(attribute.get("value")),
            }
            for attribute in row.get("__ads_attributes", [])
        ]
        verified_user = self._user_payload(row.get("__user"))
        compact_user = None
        is_verified = False
        if verified_user is not None:
            is_verified = (
                self._integer(
                    verified_user.get("is_aadhaar_gst_verified")
                )
                == 1
            )
            compact_user = {
                "prosper_id": verified_user.get("prosper_id"),
                "id": verified_user.get("id"),
                "is_aadhaar_gst_verified": (
                    verified_user.get("is_aadhaar_gst_verified")
                ),
            }
            card["is_aadhar_gst_verified_count"] = (
                1 if is_verified else 0
            )
        card.update(
            {
                "ads_attributes": attributes,
                "city": (
                    {"id": city_id, "city": city_name}
                    if city_id is not None and city_name
                    else None
                ),
                "locality": (
                    {"id": locality_id, "area": locality_name}
                    if locality_id is not None and locality_name
                    else None
                ),
                "favorites": None,
                "ads_likes": None,
                "user": compact_user,
                "boost_ad": None,
                "is_aadhar_gst_verified": (
                    verified_user if is_verified else None
                ),
            }
        )
        return card

    @staticmethod
    def _recent_scope(user_id: str | None) -> str | None:
        if user_id is None:
            return None
        normalized = user_id.strip()
        if not normalized or len(normalized) > 128:
            return None
        return normalized

    def remember_search(self, user_id: str | None, value: str) -> None:
        scope = self._recent_scope(user_id)
        if scope is None:
            return
        value = " ".join(value.split())
        if not value:
            return
        key = self._cache_key("recent", scope)
        cached = self._get_cached(key)
        items = list(cached.get("items", [])) if cached else []
        items = [
            item
            for item in items
            if str(item.get("value", "")).casefold() != value.casefold()
        ]
        item_id = int(time.time() * 1000)
        existing_ids = {
            int(item["id"])
            for item in items
            if str(item.get("id", "")).isdigit()
        }
        while item_id in existing_ids:
            item_id += 1
        items.insert(
            0,
            {
                "id": item_id,
                "value": value,
                "is_prosper": int(
                    bool(re.fullmatch(r"[A-Za-z]{2}\d+", value))
                ),
            },
        )
        items = items[: self.profile.compatibility.recent_limit]
        payload = {"items": items}
        self._set_cached(
            key,
            payload,
            self.profile.compatibility.recent_ttl_seconds,
        )

    def recent_searches(self, user_id: str | None) -> dict[str, Any]:
        scope = self._recent_scope(user_id)
        if scope is None:
            return {"status": True, "data": []}
        key = self._cache_key("recent", scope)
        cached = self._get_cached(key) or {"items": []}
        return {
            "status": True,
            "data": list(cached.get("items", [])),
        }
