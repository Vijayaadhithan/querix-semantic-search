"""Shared query-provider deadlines, HTTP pooling, metrics, and response validation."""

import contextlib
import contextvars
import json
import threading
import time

import requests
from requests.adapters import HTTPAdapter

FALLBACK_HTTP_STATUSES = {404, 408, 429, 500, 502, 503, 504}

_QUERY_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "query_provider_deadline",
    default=None,
)


def pooled_http_adapter() -> HTTPAdapter:
    """Return an application-lifetime, thread-safe urllib3 connection pool."""
    return HTTPAdapter(
        pool_connections=16,
        pool_maxsize=16,
        max_retries=0,
        # A provider call has its own deadline. Waiting indefinitely for a
        # connection would bypass that deadline.
        pool_block=False,
    )


def thread_http_session(
    state: threading.local,
    adapter: HTTPAdapter,
) -> requests.Session:
    session = getattr(state, "http_session", None)
    if session is None:
        session = requests.Session()
        # Sessions remain thread-local, while urllib3's thread-safe pool is
        # shared so worker-thread changes do not force a fresh TLS connection.
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        state.http_session = session
    return session


class QueryModelUnavailableError(RuntimeError):
    def __init__(
        self,
        model: str,
        status_code: int | None = None,
        reason: str | None = None,
        provider: str | None = None,
    ):
        self.model = model
        self.provider = provider
        self.status_code = status_code
        self.reason = reason or (
            f"http_{status_code}" if status_code is not None else "unavailable"
        )
        super().__init__(f"Query model '{model}' is unavailable ({self.reason}).")


def _effective_timeout(default_seconds: float, model: str) -> float:
    deadline = _QUERY_DEADLINE.get()
    if deadline is None:
        return default_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise QueryModelUnavailableError(
            model,
            reason="total_deadline",
        )
    return min(default_seconds, remaining)


def _http_status_error(
    model: str,
    provider: str,
    exc: requests.HTTPError,
) -> QueryModelUnavailableError | RuntimeError:
    response = exc.response
    status_code = response.status_code if response is not None else 0
    if status_code in FALLBACK_HTTP_STATUSES or (
        provider == "groq" and status_code == 400
    ):
        return QueryModelUnavailableError(
            model,
            provider=provider,
            status_code=status_code,
        )
    return RuntimeError(
        f"Cannot extract a structured query with {provider} model "
        f"'{model}' (HTTP {status_code})."
    )


def _http_error_metrics(exc: requests.HTTPError) -> dict[str, int | str]:
    response = exc.response
    if response is None:
        return {"http_status": 0}
    metrics: dict[str, int | str] = {"http_status": int(response.status_code)}
    with contextlib.suppress(
        requests.exceptions.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            for source, target in (
                ("type", "provider_error_type"),
                ("code", "provider_error_code"),
                ("param", "provider_error_param"),
            ):
                value = error.get(source)
                if value is not None:
                    metrics[target] = str(value)[:128]
    return metrics


def _structured_json_text(
    text: str,
    *,
    model: str,
    provider: str,
) -> str:
    cleaned = strip_json_fence(text)
    try:
        parsed = json.loads(cleaned)
    except (TypeError, ValueError) as exc:
        raise QueryModelUnavailableError(
            model,
            provider=provider,
            reason="invalid_json",
        ) from exc
    if not isinstance(parsed, dict):
        raise QueryModelUnavailableError(
            model,
            provider=provider,
            reason="invalid_response_shape",
        )
    return cleaned


def _response_payload(
    response: requests.Response,
    *,
    model: str,
    provider: str,
) -> dict:
    try:
        payload = response.json()
    except (requests.RequestException, TypeError, ValueError) as exc:
        raise QueryModelUnavailableError(
            model,
            provider=provider,
            reason="invalid_response_json",
        ) from exc
    if not isinstance(payload, dict):
        raise QueryModelUnavailableError(
            model,
            provider=provider,
            reason="invalid_response_shape",
        )
    return payload


def _metric_int(value: object) -> int:
    with contextlib.suppress(TypeError, ValueError):
        return int(value or 0)
    return 0


def _timeout_reason() -> str:
    deadline = _QUERY_DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        return "total_deadline"
    return "timeout"


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


class QueryProviderTransport:
    """Share transport mechanics while providers own their wire contracts."""

    def __init__(self, api_key: str, base_url: str, timeout_seconds: float):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._state = threading.local()
        self._http_adapter = pooled_http_adapter()

    @property
    def last_chat_metrics(self) -> dict[str, object]:
        return getattr(self._state, "last_chat_metrics", {})

    @last_chat_metrics.setter
    def last_chat_metrics(self, value: dict[str, object]) -> None:
        self._state.last_chat_metrics = value

    def _post(self, *args, **kwargs):
        return thread_http_session(self._state, self._http_adapter).post(
            *args, **kwargs
        )
