from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
import threading
import time
from typing import Any

from core.tenant_config import TenantProfile
from tenants.gainr.models import (
    GAINR_CARD_INTEGER_FIELDS,
    GAINR_USER_FIELDS,
    GAINR_USER_INTEGER_FIELDS,
    GainrFilterDataRequest,
    GainrFilterResultRequest,
    GainrSuggestionRequest,
)
from tenants.gainr.models import (
    GainrSearchFilter as GainrSearchFilter,
)
from tenants.gainr.repository import GainrDatabaseRepository

PERFORMANCE_LOGGER = logging.getLogger("uvicorn.error")

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

    def parse_search_suggestions(
        self,
        payload: dict[str, Any],
    ) -> GainrSuggestionRequest:
        return GainrSuggestionRequest.model_validate(payload)

    def parse_filter_data(
        self,
        payload: dict[str, Any],
    ) -> GainrFilterDataRequest:
        return GainrFilterDataRequest.model_validate(payload)

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
        # Gainr receives the selected location as structured frontend IDs on
        # mobile, web, and every other supported client. Chat text must never
        # create a competing hard geographic filter.
        for auto_key in ("state_name", "city_name", "locality_name"):
            if auto_key in categorical:
                ignored[auto_key] = categorical.pop(auto_key)
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
        speculative_starter = getattr(
            self.engine,
            "start_speculative_embedding",
            None,
        )
        speculative_embedding_future = (
            speculative_starter(request.searchTerm)
            if request.searchTerm and speculative_starter is not None
            else None
        )
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
            if speculative_embedding_future is not None:
                speculative_embedding_future.cancel()
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
            analytics_result = {
                **planned,
                "result_cache_hit": False,
                "embedding_model_metrics": {},
                "reranker_attempts": [],
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
                **(
                    {
                        "speculative_embedding_future": (
                            speculative_embedding_future
                        )
                    }
                    if speculative_embedding_future is not None
                    else {}
                ),
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
            if database_only_filters or not result.get("products"):
                eligibility_source = "ranked_page"
                hydration_started = time.perf_counter()
                rows, total = self.repository.hydrate_ranked_page(
                    result.get("product_ids", []),
                    effective,
                    request.filter,
                    allowed_ad_types,
                    page=request.page,
                    page_size=page_size,
                )
                hydration_ms = (
                    time.perf_counter() - hydration_started
                ) * 1000
            else:
                eligibility_source = "engine_rows"
                eligibility_started = time.perf_counter()
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
            analytics_result = {
                **result,
                "query_model_metrics": (
                    result.get("query_model_metrics")
                    or planned.get("query_model_metrics")
                    or {}
                ),
                "plan_cache_hit": bool(planned.get("plan_cache_hit")),
            }
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
        analytics_result["_analytics_timings_ms"] = {
            "total_server_ms": duration_ms,
            "planning_ms": planning_ms,
            "engine_total_ms": engine_ms,
            "database_filter_ms": database_ms,
            "eligibility_ms": eligibility_ms,
            "hydration_ms": hydration_ms,
            "response_mapping_ms": card_mapping_ms,
            "usage_recording_ms": usage_ms,
            "recent_search_ms": recent_ms,
        }
        # Analytics uses client-selected structured filters, never location
        # terms inferred from chat text.
        analytics_result["resolved_filters"] = copy.deepcopy(effective)
        analytics_result["_analytics_target_ad_type"] = (
            "offer"
            if allowed_ad_types == {"1"}
            else "wanted"
            if allowed_ad_types == {"2"}
            else "offer_and_wanted"
        )
        self.product_search_service.record_search_analytics(
            request.searchTerm,
            analytics_result,
            duration_ms=duration_ms,
            result_count=len(cards),
            total_results=total,
        )
        PERFORMANCE_LOGGER.info(
            "[search:%s] step=compat_response status=complete route=%s "
            "planning_ms=%.0f engine_ms=%.0f database_ms=%.0f "
            "eligibility_source=%s "
            "eligibility_ms=%.0f "
            "hydration_ms=%.0f response_map_ms=%.0f usage_ms=%.0f "
            "recent_ms=%.0f products=%d duration_ms=%.0f",
            trace_id,
            route,
            planning_ms,
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
