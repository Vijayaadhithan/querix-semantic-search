import contextlib
import contextvars
import json
import logging
import threading
import time
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from core.request_limit import RequestWindowLimiter
from core.settings import (
    GEMINI_API_BASE_URL,
    GEMINI_API_KEY,
    GEMINI_QUERY_RPM,
    GEMINI_THINKING_LEVEL,
    GROQ_API_BASE_URL,
    GROQ_API_KEY,
    GROQ_QUERY_RPM,
    GROQ_TIMEOUT_SECONDS,
    QUERY_EXTRACT_MAX_OUTPUT_TOKENS,
    QUERY_EXTRACT_MODELS,
    QUERY_EXTRACT_TIMEOUT_SECONDS,
    QUERY_EXTRACT_TOTAL_TIMEOUT_SECONDS,
    REDIS_ENABLED,
    REDIS_KEY_PREFIX,
    REDIS_URL,
)
from storage.redis import RedisJsonCache

FALLBACK_HTTP_STATUSES = {404, 408, 429, 500, 502, 503, 504}
LOGGER = logging.getLogger("uvicorn.error")

_QUERY_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "query_provider_deadline",
    default=None,
)


def _provider_rate_limit_cache():
    if not REDIS_ENABLED:
        return None
    try:
        return RedisJsonCache(REDIS_URL, REDIS_KEY_PREFIX)
    except RuntimeError as exc:
        LOGGER.warning("%s Query-provider limits will use process memory.", exc)
        return None


QUERY_PROVIDER_RATE_LIMIT_CACHE = _provider_rate_limit_cache()
QUERY_MODEL_LIMITERS: dict[str, RequestWindowLimiter] = {}
QUERY_MODEL_LIMITERS_LOCK = threading.Lock()


def query_model_route(model: str) -> tuple[str, str]:
    """Return the provider and provider-local model ID for a configured model."""
    normalized = str(model).strip()
    if normalized.startswith("groq:"):
        provider, provider_model = "groq", normalized.split(":", 1)[1]
    elif normalized.startswith("google:"):
        provider, provider_model = "google", normalized.split(":", 1)[1]
    elif ":" in normalized:
        raise ValueError(
            f"Unsupported query model provider prefix in {normalized!r}. "
            "Use groq: or google:."
        )
    else:
        # Keep unprefixed Gemini/Gemma IDs backwards-compatible. They are all
        # Google models and must share Google's request budget.
        provider, provider_model = "google", normalized
    if not provider_model:
        raise ValueError(f"Query model {normalized!r} has no model ID.")
    return provider, provider_model


def query_model_limiter(model: str) -> RequestWindowLimiter | None:
    provider, _provider_model = query_model_route(model)
    if provider == "groq":
        requests_per_minute = GROQ_QUERY_RPM
    elif provider == "google":
        requests_per_minute = GEMINI_QUERY_RPM
    else:
        return None
    with QUERY_MODEL_LIMITERS_LOCK:
        # Quotas are configured per provider, so every model routed to the
        # same provider must consume the same local/Redis request budget.
        limiter = QUERY_MODEL_LIMITERS.get(provider)
        if limiter is None:
            limiter = RequestWindowLimiter(
                requests_per_minute,
                redis_cache=QUERY_PROVIDER_RATE_LIMIT_CACHE,
                scope=f"query:{provider}",
            )
            QUERY_MODEL_LIMITERS[provider] = limiter
        return limiter


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


# Backwards-compatible name for callers that imported the original exception.
GeminiModelUnavailableError = QueryModelUnavailableError


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
    if status_code in FALLBACK_HTTP_STATUSES:
        return QueryModelUnavailableError(
            model,
            provider=provider,
            status_code=status_code,
        )
    return RuntimeError(
        f"Cannot extract a structured query with {provider} model "
        f"'{model}' (HTTP {status_code})."
    )


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


class GeminiProvider:
    def __init__(
        self,
        api_key: str = GEMINI_API_KEY,
        base_url: str = GEMINI_API_BASE_URL,
        timeout_seconds: float = QUERY_EXTRACT_TIMEOUT_SECONDS,
    ):
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
        return thread_http_session(
            self._state,
            self._http_adapter,
        ).post(*args, **kwargs)

    def structured_chat(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
        temperature: float = 0,
    ) -> str:
        started = time.perf_counter()
        metrics: dict[str, float | int | str | list] = {
            "load_ms": 0.0,
            "model": model,
            "provider": "google",
        }
        try:
            if not self.api_key:
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason="missing_api_key",
                )
            timeout_seconds = _effective_timeout(self.timeout_seconds, model)
            response = self._post(
                (f"{self.base_url}/models/{quote(model, safe='.-')}:generateContent"),
                headers={
                    "Content-Type": "application/json",
                    "X-goog-api-key": self.api_key,
                },
                json={
                    "systemInstruction": {
                        "parts": [{"text": system_prompt}],
                    },
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": user_prompt}],
                        }
                    ],
                    "generationConfig": {
                        "temperature": temperature,
                        "maxOutputTokens": QUERY_EXTRACT_MAX_OUTPUT_TOKENS,
                        "responseFormat": {
                            "text": {
                                "mimeType": "APPLICATION_JSON",
                                "schema": schema,
                            }
                        },
                        "thinkingConfig": {
                            "thinkingLevel": GEMINI_THINKING_LEVEL,
                        },
                    },
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = _response_payload(
                response,
                model=model,
                provider="google",
            )
            usage = payload.get("usageMetadata") or {}
            if not isinstance(usage, dict):
                usage = {}
            metrics.update(
                {
                    "input_tokens": _metric_int(usage.get("promptTokenCount")),
                    "output_tokens": _metric_int(usage.get("candidatesTokenCount")),
                    "thought_tokens": _metric_int(usage.get("thoughtsTokenCount")),
                    "total_tokens": _metric_int(usage.get("totalTokenCount")),
                }
            )
            candidates = payload.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                prompt_feedback = payload.get("promptFeedback") or {}
                block_reason = (
                    prompt_feedback.get("blockReason")
                    if isinstance(prompt_feedback, dict)
                    else None
                )
                reason = (
                    f"blocked_{str(block_reason).casefold()}"
                    if block_reason
                    else "empty_candidates"
                )
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason=reason,
                )
            candidate = candidates[0]
            if not isinstance(candidate, dict):
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason="invalid_candidate",
                )
            finish_reason = str(candidate.get("finishReason") or "").upper()
            if finish_reason and finish_reason not in {"STOP", "UNSPECIFIED"}:
                metrics["finish_reason"] = finish_reason
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason=f"finish_{finish_reason.casefold()}",
                )
            content = candidate.get("content")
            if not isinstance(content, dict):
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason="invalid_candidate_content",
                )
            parts = content.get("parts") or []
            text = "".join(
                str(part.get("text", ""))
                for part in parts
                if isinstance(part, dict)
                and not part.get("thought")
                and part.get("text") is not None
            ).strip()
            if not text:
                raise QueryModelUnavailableError(
                    model,
                    provider="google",
                    reason="empty_response",
                )
            return _structured_json_text(
                text,
                model=model,
                provider="google",
            )
        except requests.HTTPError as exc:
            error = _http_status_error(model, "google", exc)
            raise error from exc
        except QueryModelUnavailableError:
            raise
        except requests.Timeout as exc:
            raise QueryModelUnavailableError(
                model,
                provider="google",
                reason=_timeout_reason(),
            ) from exc
        except requests.ConnectionError as exc:
            raise QueryModelUnavailableError(
                model,
                provider="google",
                reason="connection_error",
            ) from exc
        except (
            requests.RequestException,
            TypeError,
            ValueError,
        ) as exc:
            raise RuntimeError(
                f"Cannot extract a structured query with Google model '{model}'."
            ) from exc
        finally:
            metrics["total_ms"] = (time.perf_counter() - started) * 1000
            self.last_chat_metrics = metrics


DEFAULT_GEMINI_PROVIDER = GeminiProvider()


class GroqProvider:
    def __init__(
        self,
        api_key: str = GROQ_API_KEY,
        base_url: str = GROQ_API_BASE_URL,
        timeout_seconds: float = GROQ_TIMEOUT_SECONDS,
    ):
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
        return thread_http_session(
            self._state,
            self._http_adapter,
        ).post(*args, **kwargs)

    @staticmethod
    def _output_text(payload: dict) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        parts = []
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "output_text":
                    parts.append(str(content.get("text", "")))
        text = "".join(parts).strip()
        if not text:
            raise ValueError("Groq returned an empty response.")
        return text

    def structured_chat(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
        temperature: float = 0,
    ) -> str:
        started = time.perf_counter()
        metrics: dict[str, float | int | str | list] = {
            "load_ms": 0.0,
            "model": model,
            "provider": "groq",
        }
        request_body = {
            "model": model,
            "instructions": system_prompt,
            "input": user_prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "query_plan",
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": QUERY_EXTRACT_MAX_OUTPUT_TOKENS,
        }
        if model.startswith("openai/gpt-oss-"):
            request_body["reasoning"] = {"effort": "low"}
        elif temperature > 0:
            request_body["temperature"] = temperature
        try:
            if not self.api_key:
                raise QueryModelUnavailableError(
                    model,
                    provider="groq",
                    reason="missing_api_key",
                )
            timeout_seconds = _effective_timeout(self.timeout_seconds, model)
            response = self._post(
                f"{self.base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Groq-Beta": "inference-metrics",
                },
                json=request_body,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = _response_payload(
                response,
                model=model,
                provider="groq",
            )
            usage = payload.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            metrics.update(
                {
                    "input_tokens": _metric_int(usage.get("input_tokens")),
                    "output_tokens": _metric_int(usage.get("output_tokens")),
                    "total_tokens": _metric_int(usage.get("total_tokens")),
                }
            )
            metadata = payload.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            for key, value in metadata.items():
                if key.endswith("_time"):
                    with contextlib.suppress(TypeError, ValueError):
                        metrics[f"groq_{key}_ms"] = float(value) * 1000
            status = str(payload.get("status") or "").casefold()
            if status and status != "completed":
                raise QueryModelUnavailableError(
                    model,
                    provider="groq",
                    reason=f"status_{status}",
                )
            try:
                text = self._output_text(payload)
            except ValueError as exc:
                raise QueryModelUnavailableError(
                    model,
                    provider="groq",
                    reason="empty_response",
                ) from exc
            return _structured_json_text(
                text,
                model=model,
                provider="groq",
            )
        except requests.HTTPError as exc:
            error = _http_status_error(model, "groq", exc)
            raise error from exc
        except QueryModelUnavailableError:
            raise
        except requests.Timeout as exc:
            raise QueryModelUnavailableError(
                model,
                provider="groq",
                reason=_timeout_reason(),
            ) from exc
        except requests.ConnectionError as exc:
            raise QueryModelUnavailableError(
                model,
                provider="groq",
                reason="connection_error",
            ) from exc
        except (
            requests.RequestException,
            TypeError,
            ValueError,
        ) as exc:
            raise RuntimeError(
                f"Cannot extract a structured query with Groq model '{model}'."
            ) from exc
        finally:
            metrics["total_ms"] = (time.perf_counter() - started) * 1000
            self.last_chat_metrics = metrics


DEFAULT_GROQ_PROVIDER = GroqProvider()


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def structured_chat(
    model: str,
    system_prompt: str,
    user_prompt: str,
    schema: dict,
    temperature: float = 0,
) -> str:
    if not QUERY_EXTRACT_MODELS:
        raise RuntimeError("No query extraction models are configured.")
    models = QUERY_EXTRACT_MODELS if model == QUERY_EXTRACT_MODELS[0] else (model,)
    attempted_models: list[str] = []
    attempts: list[dict[str, object]] = []
    started = time.perf_counter()
    deadline = time.monotonic() + QUERY_EXTRACT_TOTAL_TIMEOUT_SECONDS
    last_error: QueryModelUnavailableError | None = None
    deadline_token = _QUERY_DEADLINE.set(deadline)

    def publish_metrics(
        current_attempt: dict[str, object] | None = None,
        *,
        failure_reason: str | None = None,
    ) -> None:
        aggregate = dict(current_attempt or {})
        aggregate.update(
            {
                "total_ms": (time.perf_counter() - started) * 1000,
                "attempted_models": list(attempted_models),
                "attempts": [dict(attempt) for attempt in attempts],
            }
        )
        if failure_reason:
            aggregate["failure_reason"] = failure_reason
        DEFAULT_GEMINI_PROVIDER.last_chat_metrics = aggregate

    try:
        for position, candidate_model in enumerate(models, start=1):
            attempted_models.append(candidate_model)
            provider_name, provider_model = query_model_route(candidate_model)
            provider = (
                DEFAULT_GROQ_PROVIDER
                if provider_name == "groq"
                else DEFAULT_GEMINI_PROVIDER
            )
            LOGGER.debug(
                "step=query_model status=attempt model=%s provider=%s position=%d/%d",
                candidate_model,
                provider_name,
                position,
                len(models),
            )
            if time.monotonic() >= deadline:
                last_error = QueryModelUnavailableError(
                    candidate_model,
                    provider=provider_name,
                    reason="total_deadline",
                )
                attempt_metrics = {
                    "load_ms": 0.0,
                    "total_ms": 0.0,
                    "model": candidate_model,
                    "provider": provider_name,
                    "status": "fallback",
                    "reason": last_error.reason,
                }
                attempts.append(attempt_metrics)
                publish_metrics(attempt_metrics, failure_reason=last_error.reason)
                break

            limiter = query_model_limiter(candidate_model)
            if limiter is not None:
                allowed, retry_after = limiter.allow()
                if not allowed:
                    last_error = QueryModelUnavailableError(
                        candidate_model,
                        provider=provider_name,
                        reason="local_rate_limit",
                    )
                    attempt_metrics = {
                        "load_ms": 0.0,
                        "total_ms": 0.0,
                        "model": candidate_model,
                        "provider": provider_name,
                        "status": "fallback",
                        "reason": last_error.reason,
                        "retry_after_seconds": retry_after,
                    }
                    attempts.append(attempt_metrics)
                    publish_metrics(
                        attempt_metrics,
                        failure_reason=last_error.reason,
                    )
                    LOGGER.info(
                        "step=query_model status=fallback model=%s reason=%s "
                        "retry_after=%.1fs next_model=%s",
                        candidate_model,
                        last_error.reason,
                        retry_after,
                        models[position] if position < len(models) else "none",
                    )
                    continue
            try:
                content = provider.structured_chat(
                    provider_model,
                    system_prompt,
                    user_prompt,
                    schema,
                    temperature,
                )
                attempt_metrics = {
                    **provider.last_chat_metrics,
                    "model": candidate_model,
                    "provider": provider_name,
                    "status": "success",
                    "attempt_number": position,
                }
                attempts.append(attempt_metrics)
                publish_metrics(attempt_metrics)
                LOGGER.debug(
                    "step=query_model status=success model=%s duration_ms=%.0f",
                    candidate_model,
                    DEFAULT_GEMINI_PROVIDER.last_chat_metrics["total_ms"],
                )
                return content
            except QueryModelUnavailableError as exc:
                last_error = exc
                attempt_metrics = {
                    **provider.last_chat_metrics,
                    "model": candidate_model,
                    "provider": exc.provider or provider_name,
                    "status": "fallback",
                    "reason": exc.reason,
                    "attempt_number": position,
                }
                attempts.append(attempt_metrics)
                publish_metrics(attempt_metrics, failure_reason=exc.reason)
                LOGGER.warning(
                    "step=query_model status=fallback model=%s reason=%s next_model=%s",
                    candidate_model,
                    exc.reason,
                    (models[position] if position < len(models) else "none"),
                )
            except RuntimeError:
                attempt_metrics = {
                    **provider.last_chat_metrics,
                    "model": candidate_model,
                    "provider": provider_name,
                    "status": "failed",
                    "reason": "provider_error",
                    "attempt_number": position,
                }
                attempts.append(attempt_metrics)
                publish_metrics(
                    attempt_metrics,
                    failure_reason="provider_error",
                )
                LOGGER.exception(
                    "step=query_model status=failed model=%s reason=provider_error",
                    candidate_model,
                )
                raise
        LOGGER.error(
            "step=query_model status=failed attempted_models=%s reason=%s",
            ",".join(attempted_models),
            last_error.reason if last_error is not None else "unknown",
        )
        raise RuntimeError(
            "All configured query models are unavailable "
            f"(last_reason={last_error.reason if last_error else 'unknown'})."
        ) from last_error
    finally:
        _QUERY_DEADLINE.reset(deadline_token)


def last_query_model_metrics() -> dict[str, object]:
    return dict(DEFAULT_GEMINI_PROVIDER.last_chat_metrics)


def last_gemini_metrics() -> dict[str, object]:
    """Backward-compatible alias for the provider-neutral metrics accessor."""
    return last_query_model_metrics()
