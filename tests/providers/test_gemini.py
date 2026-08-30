import threading

import pytest
import requests

from core.settings import QUERY_EXTRACT_MODELS
from providers import gemini as gemini_client
from providers.gemini import (
    GeminiModelUnavailableError,
    GeminiProvider,
    GroqProvider,
    QueryModelUnavailableError,
    estimate_groq_request_tokens,
    query_model_limiter,
    query_model_route,
    query_model_token_limiter,
    strip_json_fence,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class HttpErrorResponse(FakeResponse):
    def __init__(self, payload, status_code):
        super().__init__(payload)
        self.status_code = status_code

    def raise_for_status(self):
        error = requests.HTTPError(f"HTTP {self.status_code}")
        error.response = self
        raise error


class MalformedJsonResponse(FakeResponse):
    def json(self):
        raise requests.exceptions.JSONDecodeError("invalid", "<html>", 0)


@pytest.fixture(autouse=True)
def isolate_query_provider_rate_limits(monkeypatch):
    monkeypatch.setattr(
        gemini_client,
        "QUERY_PROVIDER_RATE_LIMIT_CACHE",
        None,
    )
    gemini_client.QUERY_MODEL_LIMITERS.clear()
    gemini_client.QUERY_MODEL_TOKEN_LIMITERS.clear()
    yield
    gemini_client.QUERY_MODEL_LIMITERS.clear()
    gemini_client.QUERY_MODEL_TOKEN_LIMITERS.clear()


def test_configured_query_model_fallback_order():
    assert QUERY_EXTRACT_MODELS
    assert QUERY_EXTRACT_MODELS[0].startswith("groq:")
    assert all(
        model.split(":", 1)[0] in {"groq", "google"}
        for model in QUERY_EXTRACT_MODELS
        if ":" in model
    )


def test_query_model_route_and_google_family_limiter():
    assert query_model_route("groq:openai/gpt-oss-20b") == (
        "groq",
        "openai/gpt-oss-20b",
    )
    assert query_model_route("google:gemini-3.1-flash-lite") == (
        "google",
        "gemini-3.1-flash-lite",
    )
    assert query_model_route("gemma-4-26b-a4b-it") == (
        "google",
        "gemma-4-26b-a4b-it",
    )
    google_limiter = query_model_limiter("gemma-4-26b-a4b-it")
    assert google_limiter is not None
    assert google_limiter is query_model_limiter("google:gemini-3.1-flash-lite")
    assert google_limiter.scope == "query:google"
    assert query_model_limiter("groq:openai/gpt-oss-20b") is not google_limiter
    groq_token_limiter = query_model_token_limiter("groq:openai/gpt-oss-20b")
    assert groq_token_limiter is not None
    assert groq_token_limiter.scope == "query:groq"
    assert query_model_token_limiter("google:gemini-3.1-flash-lite") is None

    with pytest.raises(ValueError, match="Unsupported query model provider"):
        query_model_route("unknown:model")


def test_structured_chat_uses_generate_content_json_schema(monkeypatch):
    captured = {}
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def fake_post(url, headers, json, timeout):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": '{"query":"camera"}'}],
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(
        GeminiProvider,
        "_post",
        lambda _self, *args, **kwargs: fake_post(*args, **kwargs),
    )
    provider = GeminiProvider(
        api_key="test-key",
        base_url="https://generativelanguage.test/v1beta",
        timeout_seconds=3,
    )

    content = provider.structured_chat(
        "gemini-3.1-flash-lite",
        "system",
        "user",
        schema,
        temperature=0,
    )

    assert content == '{"query":"camera"}'
    assert captured["url"].endswith("/models/gemini-3.1-flash-lite:generateContent")
    assert captured["headers"]["X-goog-api-key"] == "test-key"
    assert captured["json"]["systemInstruction"] == {"parts": [{"text": "system"}]}
    assert captured["json"]["generationConfig"] == {
        "temperature": 0,
        "maxOutputTokens": 384,
        "responseFormat": {
            "text": {
                "mimeType": "APPLICATION_JSON",
                "schema": schema,
            }
        },
        "thinkingConfig": {"thinkingLevel": "minimal"},
    }
    assert captured["timeout"] == 3
    assert provider.last_chat_metrics["total_ms"] >= 0


def test_structured_chat_requires_api_key():
    provider = GeminiProvider(api_key="")

    with pytest.raises(QueryModelUnavailableError) as caught:
        provider.structured_chat(
            "gemma-4-26b-a4b-it",
            "system",
            "user",
            {"type": "object"},
        )
    assert caught.value.reason == "missing_api_key"
    assert provider.last_chat_metrics.get("total_tokens", 0) == 0


def test_worker_sessions_share_the_provider_connection_pool(monkeypatch):
    provider = GeminiProvider(api_key="test-key")
    sessions = []
    barrier = threading.Barrier(2, timeout=2)

    def fake_post(session, *_args, **_kwargs):
        sessions.append(session)
        barrier.wait()
        return object()

    monkeypatch.setattr(requests.Session, "post", fake_post)

    threads = [
        threading.Thread(target=provider._post, args=("https://example.test",))
        for _index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert all(
        session.adapters["https://"] is provider._http_adapter for session in sessions
    )


def test_groq_structured_chat_uses_responses_json_schema(monkeypatch):
    captured = {}
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def fake_post(url, headers, json, timeout):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return FakeResponse(
            {
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"query":"camera"}',
                            }
                        ]
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            }
        )

    monkeypatch.setattr(
        GroqProvider,
        "_post",
        lambda _self, *args, **kwargs: fake_post(*args, **kwargs),
    )
    provider = GroqProvider(
        api_key="test-key",
        base_url="https://api.groq.test/openai/v1",
        timeout_seconds=3,
    )

    content = provider.structured_chat(
        "openai/gpt-oss-20b",
        "system",
        "user",
        schema,
    )

    assert content == '{"query":"camera"}'
    assert captured["url"].endswith("/openai/v1/responses")
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["reasoning"] == {"effort": "low"}
    assert captured["json"]["max_output_tokens"] == 384
    assert captured["json"]["text"]["format"] == {
        "type": "json_schema",
        "name": "query_plan",
        "strict": True,
        "schema": schema,
    }
    assert captured["timeout"] == 3
    assert provider.last_chat_metrics["total_tokens"] == 120


def test_groq_missing_key_is_a_fallback_model_failure():
    provider = GroqProvider(api_key="")

    with pytest.raises(GeminiModelUnavailableError) as caught:
        provider.structured_chat(
            "openai/gpt-oss-20b",
            "system",
            "user",
            {"type": "object"},
        )

    assert caught.value.reason == "missing_api_key"


def test_provider_captures_google_usage_metadata(monkeypatch):
    class UsageResponse(FakeResponse):
        def json(self):
            payload = super().json()
            payload["usageMetadata"] = {
                "promptTokenCount": 120,
                "candidatesTokenCount": 30,
                "thoughtsTokenCount": 10,
                "totalTokenCount": 160,
            }
            return payload

    monkeypatch.setattr(
        GeminiProvider,
        "_post",
        lambda _self, *_args, **_kwargs: UsageResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": '{"query":"camera"}'}],
                        }
                    }
                ]
            }
        ),
    )
    provider = GeminiProvider(
        api_key="test-key",
        base_url="https://generativelanguage.test/v1beta",
    )

    provider.structured_chat(
        "model-a",
        "system",
        "user",
        {"type": "object"},
    )

    assert provider.last_chat_metrics["input_tokens"] == 120
    assert provider.last_chat_metrics["output_tokens"] == 30
    assert provider.last_chat_metrics["thought_tokens"] == 10
    assert provider.last_chat_metrics["total_tokens"] == 160


def test_google_provider_ignores_thought_parts(monkeypatch):
    monkeypatch.setattr(
        GeminiProvider,
        "_post",
        lambda _self, *_args, **_kwargs: FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "internal reasoning"},
                                {"text": '{"query":"camera"}'},
                            ]
                        }
                    }
                ]
            }
        ),
    )
    provider = GeminiProvider(api_key="test-key", timeout_seconds=3)

    assert (
        provider.structured_chat(
            "gemini-3.1-flash-lite",
            "system",
            "user",
            {"type": "object"},
        )
        == '{"query":"camera"}'
    )


def test_google_provider_falls_back_for_invalid_response_shapes(monkeypatch):
    monkeypatch.setattr(
        GeminiProvider,
        "_post",
        lambda _self, *_args, **_kwargs: FakeResponse([]),
    )
    provider = GeminiProvider(api_key="test-key", timeout_seconds=3)

    with pytest.raises(QueryModelUnavailableError) as caught:
        provider.structured_chat(
            "gemini-3.1-flash-lite",
            "system",
            "user",
            {"type": "object"},
        )
    assert caught.value.reason == "invalid_response_shape"


@pytest.mark.parametrize("provider_class", [GeminiProvider, GroqProvider])
def test_provider_falls_back_for_malformed_http_json(monkeypatch, provider_class):
    monkeypatch.setattr(
        provider_class,
        "_post",
        lambda _self, *_args, **_kwargs: MalformedJsonResponse(None),
    )
    provider = provider_class(api_key="test-key", timeout_seconds=3)

    with pytest.raises(QueryModelUnavailableError) as caught:
        provider.structured_chat(
            "test-model",
            "system",
            "user",
            {"type": "object"},
        )
    assert caught.value.reason == "invalid_response_json"


def test_google_provider_ignores_malformed_usage_metadata(monkeypatch):
    monkeypatch.setattr(
        GeminiProvider,
        "_post",
        lambda _self, *_args, **_kwargs: FakeResponse(
            {
                "candidates": [
                    {"content": {"parts": [{"text": '{"query":"camera"}'}]}}
                ],
                "usageMetadata": {
                    "promptTokenCount": "unknown",
                    "totalTokenCount": None,
                },
            }
        ),
    )
    provider = GeminiProvider(api_key="test-key", timeout_seconds=3)

    assert (
        provider.structured_chat(
            "gemini-3.1-flash-lite",
            "system",
            "user",
            {"type": "object"},
        )
        == '{"query":"camera"}'
    )
    assert provider.last_chat_metrics["input_tokens"] == 0


def test_groq_provider_falls_back_for_incomplete_response(monkeypatch):
    monkeypatch.setattr(
        GroqProvider,
        "_post",
        lambda _self, *_args, **_kwargs: FakeResponse(
            {"status": "incomplete", "output_text": '{"query":"camera"}'}
        ),
    )
    provider = GroqProvider(api_key="test-key", timeout_seconds=3)

    with pytest.raises(QueryModelUnavailableError) as caught:
        provider.structured_chat(
            "openai/gpt-oss-20b",
            "system",
            "user",
            {"type": "object"},
        )
    assert caught.value.reason == "status_incomplete"


def test_google_provider_falls_back_for_empty_candidates(monkeypatch):
    monkeypatch.setattr(
        GeminiProvider,
        "_post",
        lambda _self, *_args, **_kwargs: FakeResponse(
            {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        ),
    )
    provider = GeminiProvider(api_key="test-key", timeout_seconds=3)

    with pytest.raises(QueryModelUnavailableError) as caught:
        provider.structured_chat(
            "gemini-3.1-flash-lite",
            "system",
            "user",
            {"type": "object"},
        )
    assert caught.value.reason == "blocked_safety"


def test_groq_http_400_advances_as_provider_fallback(monkeypatch):
    monkeypatch.setattr(
        GroqProvider,
        "_post",
        lambda _self, *_args, **_kwargs: HttpErrorResponse(
            {"error": {"type": "invalid_request_error", "code": "schema_error"}},
            400,
        ),
    )
    provider = GroqProvider(api_key="test-key", timeout_seconds=3)

    with pytest.raises(QueryModelUnavailableError) as caught:
        provider.structured_chat(
            "openai/gpt-oss-20b",
            "system",
            "user",
            {"type": "object"},
        )
    assert caught.value.reason == "http_400"
    assert provider.last_chat_metrics["http_status"] == 400
    assert provider.last_chat_metrics["provider_error_type"] == (
        "invalid_request_error"
    )
    assert provider.last_chat_metrics["provider_error_code"] == "schema_error"


def test_google_http_400_remains_a_permanent_provider_error(monkeypatch):
    monkeypatch.setattr(
        GeminiProvider,
        "_post",
        lambda _self, *_args, **_kwargs: HttpErrorResponse({}, 400),
    )
    provider = GeminiProvider(api_key="test-key", timeout_seconds=3)

    with pytest.raises(RuntimeError, match="HTTP 400"):
        provider.structured_chat(
            "gemini-3.1-flash-lite",
            "system",
            "user",
            {"type": "object"},
        )


def test_groq_token_estimate_reserves_input_and_maximum_output():
    estimated = estimate_groq_request_tokens(
        "s" * 400,
        "u" * 400,
        {"type": "object", "properties": {}},
    )

    assert estimated > gemini_client.QUERY_EXTRACT_MAX_OUTPUT_TOKENS
    assert estimate_groq_request_tokens(
        "தமிழ்" * 100,
        "user",
        {"type": "object"},
    ) > estimate_groq_request_tokens(
        "ascii" * 100,
        "user",
        {"type": "object"},
    )


def test_missing_key_resets_provider_metrics():
    provider = GroqProvider(api_key="test-key", timeout_seconds=3)
    provider.last_chat_metrics = {"total_tokens": 999}
    provider.api_key = ""

    with pytest.raises(QueryModelUnavailableError):
        provider.structured_chat(
            "openai/gpt-oss-20b",
            "system",
            "user",
            {"type": "object"},
        )

    assert provider.last_chat_metrics["model"] == "openai/gpt-oss-20b"
    assert provider.last_chat_metrics.get("total_tokens", 0) == 0


def test_provider_metrics_are_thread_local():
    provider = GeminiProvider(api_key="test-key")
    provider.last_chat_metrics = {"model": "main"}
    child_value = {}

    def set_child_metrics():
        provider.last_chat_metrics = {"model": "child"}
        child_value.update(provider.last_chat_metrics)

    thread = threading.Thread(target=set_child_metrics)
    thread.start()
    thread.join()

    assert child_value == {"model": "child"}
    assert provider.last_chat_metrics == {"model": "main"}


def test_strip_json_fence_accepts_gemma_markdown_wrapper():
    assert strip_json_fence('```json\n{"query":"camera"}\n```') == (
        '{"query":"camera"}'
    )
    assert strip_json_fence('{"query":"camera"}\n```') == ('{"query":"camera"}')


def test_default_structured_chat_falls_back_for_unavailable_models(monkeypatch):
    class FakeProvider:
        def __init__(self):
            self.calls = []
            self.last_chat_metrics = {}

        def structured_chat(self, model, *_args):
            self.calls.append(model)
            if model != "model-c":
                raise GeminiModelUnavailableError(model, 429)
            self.last_chat_metrics = {
                "total_ms": 1.0,
                "load_ms": 0.0,
                "model": model,
            }
            return '{"query":"camera"}'

    provider = FakeProvider()
    monkeypatch.setattr(
        gemini_client,
        "QUERY_EXTRACT_MODELS",
        ("model-a", "model-b", "model-c"),
    )
    monkeypatch.setattr(
        gemini_client,
        "DEFAULT_GEMINI_PROVIDER",
        provider,
    )

    content = gemini_client.structured_chat(
        "model-a",
        "system",
        "user",
        {"type": "object"},
    )

    assert content == '{"query":"camera"}'
    assert provider.calls == ["model-a", "model-b", "model-c"]
    assert provider.last_chat_metrics["model"] == "model-c"
    assert provider.last_chat_metrics["attempted_models"] == [
        "model-a",
        "model-b",
        "model-c",
    ]


def test_default_structured_chat_routes_prefixed_model_to_groq(monkeypatch):
    class FakeGroqProvider:
        def __init__(self):
            self.calls = []
            self.last_chat_metrics = {}

        def structured_chat(self, model, *_args):
            self.calls.append(model)
            self.last_chat_metrics = {
                "total_ms": 1.0,
                "provider": "groq",
                "model": model,
            }
            return '{"query":"camera"}'

    groq_provider = FakeGroqProvider()
    monkeypatch.setattr(
        gemini_client,
        "QUERY_EXTRACT_MODELS",
        ("groq:openai/gpt-oss-20b", "gemini-test"),
    )
    monkeypatch.setattr(
        gemini_client,
        "DEFAULT_GROQ_PROVIDER",
        groq_provider,
    )

    content = gemini_client.structured_chat(
        "groq:openai/gpt-oss-20b",
        "system",
        "user",
        {"type": "object"},
    )

    assert content == '{"query":"camera"}'
    assert groq_provider.calls == ["openai/gpt-oss-20b"]
    assert gemini_client.last_gemini_metrics()["model"] == ("groq:openai/gpt-oss-20b")
    assert gemini_client.last_gemini_metrics()["provider"] == "groq"


def test_missing_google_key_advances_to_a_later_google_fallback(monkeypatch):
    class FakeGroqProvider:
        last_chat_metrics = {}

        def structured_chat(self, model, *_args):
            raise QueryModelUnavailableError(
                model,
                reason="missing_api_key",
                provider="groq",
            )

    class FakeGoogleProvider:
        def __init__(self):
            self.calls = []
            self.last_chat_metrics = {}

        def structured_chat(self, model, *_args):
            self.calls.append(model)
            if model == "gemini-3.1-flash-lite":
                raise QueryModelUnavailableError(
                    model,
                    reason="missing_api_key",
                    provider="google",
                )
            self.last_chat_metrics = {"model": model, "total_ms": 1.0}
            return '{"query":"camera"}'

    google_provider = FakeGoogleProvider()
    monkeypatch.setattr(
        gemini_client,
        "QUERY_EXTRACT_MODELS",
        (
            "groq:openai/gpt-oss-20b",
            "google:gemini-3.1-flash-lite",
            "google:gemma-4-26b-a4b-it",
        ),
    )
    monkeypatch.setattr(gemini_client, "DEFAULT_GROQ_PROVIDER", FakeGroqProvider())
    monkeypatch.setattr(gemini_client, "DEFAULT_GEMINI_PROVIDER", google_provider)

    assert (
        gemini_client.structured_chat(
            "groq:openai/gpt-oss-20b",
            "system",
            "user",
            {"type": "object"},
        )
        == '{"query":"camera"}'
    )
    assert google_provider.calls == [
        "gemini-3.1-flash-lite",
        "gemma-4-26b-a4b-it",
    ]


def test_total_query_deadline_stops_the_fallback_chain(monkeypatch):
    class UnexpectedProvider:
        last_chat_metrics = {}

        def structured_chat(self, *_args):
            raise AssertionError("provider should not be called after deadline")

    monkeypatch.setattr(gemini_client, "QUERY_EXTRACT_TOTAL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(
        gemini_client,
        "QUERY_EXTRACT_MODELS",
        ("google:gemini-3.1-flash-lite",),
    )
    monkeypatch.setattr(
        gemini_client,
        "DEFAULT_GEMINI_PROVIDER",
        UnexpectedProvider(),
    )

    with pytest.raises(RuntimeError, match="last_reason=total_deadline"):
        gemini_client.structured_chat(
            "google:gemini-3.1-flash-lite",
            "system",
            "user",
            {"type": "object"},
        )
    assert gemini_client.last_gemini_metrics()["failure_reason"] == "total_deadline"


def test_groq_local_budget_overflow_routes_directly_to_gemini(monkeypatch):
    class RejectingLimiter:
        def allow(self):
            return False, 42.5

    class FakeProvider:
        def __init__(self, content=None):
            self.calls = []
            self.content = content
            self.last_chat_metrics = {}

        def structured_chat(self, model, *_args):
            self.calls.append(model)
            self.last_chat_metrics = {"model": model, "total_ms": 1.0}
            return self.content

    groq_provider = FakeProvider()
    gemini_provider = FakeProvider('{"query":"camera"}')
    monkeypatch.setattr(
        gemini_client,
        "QUERY_EXTRACT_MODELS",
        ("groq:openai/gpt-oss-20b", "gemini-3.1-flash-lite"),
    )
    monkeypatch.setattr(
        gemini_client,
        "DEFAULT_GROQ_PROVIDER",
        groq_provider,
    )
    monkeypatch.setattr(
        gemini_client,
        "DEFAULT_GEMINI_PROVIDER",
        gemini_provider,
    )
    monkeypatch.setattr(
        gemini_client,
        "query_model_limiter",
        lambda model: RejectingLimiter() if model.startswith("groq:") else None,
    )

    content = gemini_client.structured_chat(
        "groq:openai/gpt-oss-20b",
        "system",
        "user",
        {"type": "object"},
    )

    assert content == '{"query":"camera"}'
    assert groq_provider.calls == []
    assert gemini_provider.calls == ["gemini-3.1-flash-lite"]
    metrics = gemini_client.last_gemini_metrics()
    assert metrics["attempted_models"] == [
        "groq:openai/gpt-oss-20b",
        "gemini-3.1-flash-lite",
    ]
    assert metrics["attempts"][0]["reason"] == "local_rate_limit"
    assert metrics["attempts"][0]["retry_after_seconds"] == 42.5


def test_groq_local_token_overflow_routes_directly_to_gemini(monkeypatch):
    class AllowingRequestLimiter:
        def allow(self):
            return True, 0.0

    class RejectingTokenLimiter:
        def __init__(self):
            self.estimates = []

        def allow(self, estimated_tokens):
            self.estimates.append(estimated_tokens)
            return False, 9.5

    class FakeProvider:
        def __init__(self, content=None):
            self.calls = []
            self.content = content
            self.last_chat_metrics = {}

        def structured_chat(self, model, *_args):
            self.calls.append(model)
            self.last_chat_metrics = {"model": model, "total_ms": 1.0}
            return self.content

    groq_provider = FakeProvider()
    gemini_provider = FakeProvider('{"query":"camera"}')
    token_limiter = RejectingTokenLimiter()
    monkeypatch.setattr(
        gemini_client,
        "QUERY_EXTRACT_MODELS",
        ("groq:openai/gpt-oss-20b", "google:gemini-3.1-flash-lite"),
    )
    monkeypatch.setattr(gemini_client, "DEFAULT_GROQ_PROVIDER", groq_provider)
    monkeypatch.setattr(gemini_client, "DEFAULT_GEMINI_PROVIDER", gemini_provider)
    monkeypatch.setattr(
        gemini_client,
        "query_model_limiter",
        lambda _model: AllowingRequestLimiter(),
    )
    monkeypatch.setattr(
        gemini_client,
        "query_model_token_limiter",
        lambda model: token_limiter if model.startswith("groq:") else None,
    )

    content = gemini_client.structured_chat(
        "groq:openai/gpt-oss-20b",
        "system",
        "user",
        {"type": "object"},
    )

    assert content == '{"query":"camera"}'
    assert groq_provider.calls == []
    assert gemini_provider.calls == ["gemini-3.1-flash-lite"]
    assert token_limiter.estimates[0] > 0
    metrics = gemini_client.last_query_model_metrics()
    assert metrics["attempts"][0]["reason"] == "local_token_limit"
    assert metrics["attempts"][0]["retry_after_seconds"] == 9.5


def test_groq_http_400_routes_to_gemini(monkeypatch):
    groq_provider = GroqProvider(api_key="test-key", timeout_seconds=3)

    class FakeGoogleProvider:
        def __init__(self):
            self.calls = []
            self.last_chat_metrics = {}

        def structured_chat(self, model, *_args):
            self.calls.append(model)
            self.last_chat_metrics = {
                "model": model,
                "provider": "google",
                "total_ms": 1.0,
            }
            return '{"query":"camera"}'

    google_provider = FakeGoogleProvider()
    monkeypatch.setattr(
        groq_provider,
        "_post",
        lambda *_args, **_kwargs: HttpErrorResponse(
            {"error": {"type": "invalid_request_error"}},
            400,
        ),
    )
    monkeypatch.setattr(
        gemini_client,
        "QUERY_EXTRACT_MODELS",
        ("groq:openai/gpt-oss-20b", "google:gemini-3.1-flash-lite"),
    )
    monkeypatch.setattr(gemini_client, "DEFAULT_GROQ_PROVIDER", groq_provider)
    monkeypatch.setattr(gemini_client, "DEFAULT_GEMINI_PROVIDER", google_provider)
    monkeypatch.setattr(gemini_client, "query_model_limiter", lambda _model: None)
    monkeypatch.setattr(
        gemini_client,
        "query_model_token_limiter",
        lambda _model: None,
    )

    content = gemini_client.structured_chat(
        "groq:openai/gpt-oss-20b",
        "system",
        "user",
        {"type": "object"},
    )

    assert content == '{"query":"camera"}'
    assert google_provider.calls == ["gemini-3.1-flash-lite"]
    metrics = gemini_client.last_query_model_metrics()
    assert metrics["attempted_models"] == [
        "groq:openai/gpt-oss-20b",
        "google:gemini-3.1-flash-lite",
    ]
    assert metrics["attempts"][0]["reason"] == "http_400"
    assert metrics["attempts"][1]["status"] == "success"


def test_provider_timeout_becomes_retryable_model_failure(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise requests.ReadTimeout("slow provider")

    monkeypatch.setattr(
        GeminiProvider,
        "_post",
        lambda _self, *args, **kwargs: timeout(*args, **kwargs),
    )
    provider = GeminiProvider(
        api_key="test-key",
        base_url="https://generativelanguage.test/v1beta",
        timeout_seconds=2,
    )

    with pytest.raises(GeminiModelUnavailableError) as caught:
        provider.structured_chat(
            "model-a",
            "system",
            "user",
            {"type": "object"},
        )

    assert caught.value.model == "model-a"
    assert caught.value.status_code is None
    assert caught.value.reason == "timeout"
    assert provider.last_chat_metrics["model"] == "model-a"


def test_default_structured_chat_advances_after_timeout(monkeypatch):
    class TimeoutThenSuccessProvider:
        def __init__(self):
            self.calls = []
            self.last_chat_metrics = {}

        def structured_chat(self, model, *_args):
            self.calls.append(model)
            self.last_chat_metrics = {
                "total_ms": 1.0,
                "load_ms": 0.0,
                "model": model,
            }
            if model == "model-a":
                raise GeminiModelUnavailableError(
                    model,
                    reason="timeout",
                )
            return '{"query":"camera"}'

    provider = TimeoutThenSuccessProvider()
    monkeypatch.setattr(
        gemini_client,
        "QUERY_EXTRACT_MODELS",
        ("model-a", "model-b", "model-c"),
    )
    monkeypatch.setattr(
        gemini_client,
        "DEFAULT_GEMINI_PROVIDER",
        provider,
    )

    content = gemini_client.structured_chat(
        "model-a",
        "system",
        "user",
        {"type": "object"},
    )

    assert content == '{"query":"camera"}'
    assert provider.calls == ["model-a", "model-b"]
    assert provider.last_chat_metrics["model"] == "model-b"
    assert provider.last_chat_metrics["attempted_models"] == [
        "model-a",
        "model-b",
    ]


def test_all_failed_models_keep_attempted_metrics(monkeypatch):
    class AlwaysUnavailableProvider:
        def __init__(self):
            self.last_chat_metrics = {}

        def structured_chat(self, model, *_args):
            self.last_chat_metrics = {
                "total_ms": 1.0,
                "load_ms": 0.0,
                "model": model,
            }
            raise GeminiModelUnavailableError(
                model,
                reason="timeout",
            )

    provider = AlwaysUnavailableProvider()
    monkeypatch.setattr(
        gemini_client,
        "QUERY_EXTRACT_MODELS",
        ("model-a", "model-b"),
    )
    monkeypatch.setattr(
        gemini_client,
        "DEFAULT_GEMINI_PROVIDER",
        provider,
    )

    with pytest.raises(RuntimeError, match="last_reason=timeout"):
        gemini_client.structured_chat(
            "model-a",
            "system",
            "user",
            {"type": "object"},
        )

    assert provider.last_chat_metrics["model"] == "model-b"
    assert provider.last_chat_metrics["attempted_models"] == [
        "model-a",
        "model-b",
    ]
    assert provider.last_chat_metrics["failure_reason"] == "timeout"


def test_permanent_provider_error_replaces_stale_aggregate_metrics(monkeypatch):
    class PermanentFailureProvider:
        def __init__(self):
            self.last_chat_metrics = {"total_tokens": 999, "model": "stale"}

        def structured_chat(self, model, *_args):
            self.last_chat_metrics = {
                "total_ms": 1.0,
                "load_ms": 0.0,
                "model": model,
            }
            raise RuntimeError("HTTP 400")

    provider = PermanentFailureProvider()
    monkeypatch.setattr(
        gemini_client,
        "QUERY_EXTRACT_MODELS",
        ("google:model-a",),
    )
    monkeypatch.setattr(
        gemini_client,
        "DEFAULT_GEMINI_PROVIDER",
        provider,
    )

    with pytest.raises(RuntimeError, match="HTTP 400"):
        gemini_client.structured_chat(
            "google:model-a",
            "system",
            "user",
            {"type": "object"},
        )

    assert provider.last_chat_metrics["model"] == "google:model-a"
    assert provider.last_chat_metrics["failure_reason"] == "provider_error"
    assert provider.last_chat_metrics["attempted_models"] == ["google:model-a"]
    assert provider.last_chat_metrics.get("total_tokens", 0) == 0
