import base64
import binascii
import os
import resource
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.settings import (
    API_DEFAULT_PAGE_SIZE,
    API_MAX_PAGE_SIZE,
    API_MAX_SESSIONS,
    API_SESSION_TTL_SECONDS,
)

PROCESS_STARTED_MONOTONIC = time.monotonic()

PUBLIC_PRODUCT_FIELDS = (
    "result_tier",
    "id",
    "type",
    "category_type",
    "parent_id",
    "category_id",
    "title",
    "slug",
    "description",
    "rental_duration",
    "rental_fee",
    "is_rent_negotiable",
    "city_id",
    "locality_id",
    "custom_locality",
    "photos",
    "total_favorite",
    "total_like",
    "users_rating_count",
    "rating_avg",
    "created_at",
    "updated_at",
)


class InvalidCursorError(ValueError):
    pass


class ExpiredCursorError(ValueError):
    pass


class SearchCapacityError(RuntimeError):
    pass


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(default=None, max_length=1000)
    cursor: str | None = Field(default=None, max_length=512)
    page_size: int = Field(
        default=API_DEFAULT_PAGE_SIZE,
        ge=1,
        le=API_MAX_PAGE_SIZE,
    )

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("query must not be blank")
        return value

    @model_validator(mode="after")
    def require_query_or_cursor(self):
        if (self.query is None) == (self.cursor is None):
            raise ValueError("provide exactly one of query or cursor")
        return self


class PaginationResponse(BaseModel):
    page_size: int
    returned: int
    offset: int
    total_results: int
    has_more: bool
    next_cursor: str | None


class SearchResponse(BaseModel):
    company_id: str | None = None
    search_id: str
    query: str
    cached: bool
    items: list[dict[str, Any]]
    interpreted_query: dict[str, Any]
    applied_filters: dict[str, Any]
    unresolved_filters: dict[str, Any]
    timings_ms: dict[str, float]
    usage: dict[str, Any] = Field(default_factory=dict)
    pagination: PaginationResponse


class HealthResponse(BaseModel):
    status: str
    app: str
    indexed_products: int
    max_result_window: int
    session_ttl_seconds: int
    reranker_model: str
    reranker_loaded: bool
    reranker_load_ms: float
    embedding_warmup: dict[str, Any]
    redis_enabled: bool
    redis_connected: bool
    query_plan_cache_backend: str
    result_cache_enabled: bool
    result_cache_ttl_seconds: int
    company_id: str | None = None


def _process_rss_mb() -> float:
    try:
        with open("/proc/self/statm", encoding="utf-8") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        maximum_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        return float(maximum_rss) / divisor


def process_monitor_status() -> dict[str, Any]:
    try:
        load_average = [round(value, 3) for value in os.getloadavg()]
    except OSError:
        load_average = []
    return {
        "pid": os.getpid(),
        "uptime_seconds": round(
            time.monotonic() - PROCESS_STARTED_MONOTONIC,
            3,
        ),
        "cpu_count": os.cpu_count(),
        "load_average": load_average,
        "rss_mb": round(_process_rss_mb(), 3),
    }


@dataclass
class SearchSession:
    search_id: str
    query: str
    items: list[dict[str, Any]]
    interpreted_query: dict[str, Any]
    applied_filters: dict[str, Any]
    unresolved_filters: dict[str, Any]
    timings_ms: dict[str, float]
    usage: dict[str, Any]
    expires_at: float
    company_id: str | None = None


class SearchSessionStore:
    def __init__(
        self,
        ttl_seconds: int = API_SESSION_TTL_SECONDS,
        max_sessions: int = API_MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if ttl_seconds <= 0:
            raise ValueError("Session TTL must be greater than zero.")
        if max_sessions <= 0:
            raise ValueError("Maximum sessions must be greater than zero.")
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self.clock = clock
        self._sessions: OrderedDict[str, SearchSession] = OrderedDict()
        self._lock = threading.Lock()

    def create(self, **values) -> SearchSession:
        with self._lock:
            now = self.clock()
            self._remove_expired(now)
            search_id = str(uuid.uuid4())
            session = SearchSession(
                search_id=search_id,
                expires_at=now + self.ttl_seconds,
                **values,
            )
            self._sessions[search_id] = session
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
            return session

    def get(self, search_id: str) -> SearchSession:
        with self._lock:
            session = self._sessions.get(search_id)
            if session is None:
                raise InvalidCursorError("The cursor is invalid.")
            if session.expires_at <= self.clock():
                del self._sessions[search_id]
                raise ExpiredCursorError(
                    "The cursor has expired. Start a new search with query."
                )
            self._sessions.move_to_end(search_id)
            return session

    def _remove_expired(self, now: float) -> None:
        expired = [
            search_id
            for search_id, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for search_id in expired:
            del self._sessions[search_id]


def encode_cursor(search_id: str, offset: int) -> str:
    raw = f"v1:{search_id}:{offset}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(cursor + padding).decode()
        version, search_id, raw_offset = decoded.split(":")
        parsed_id = str(uuid.UUID(search_id))
        offset = int(raw_offset)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidCursorError("The cursor is invalid.") from exc
    if version != "v1" or offset < 0:
        raise InvalidCursorError("The cursor is invalid.")
    return parsed_id, offset
