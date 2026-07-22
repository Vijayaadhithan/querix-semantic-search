import sys
import threading
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gemini_client
from gemini_client import (
    GeminiModelUnavailableError,
    GeminiProvider,
    GroqProvider,
    strip_json_fence,
)
from settings import QUERY_EXTRACT_MODELS


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_configured_query_model_fallback_order():
    assert QUERY_EXTRACT_MODELS == (
        "groq:openai/gpt-oss-20b",
        "gemini-3.1-flash-lite",
        "gemma-4-26b-a4b-it",
        "gemma-4-31b-it",
    )


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
    )

    content = provider.structured_chat(
        "gemma-4-26b-a4b-it",
        "system",
        "user",
        schema,
        temperature=0,
    )

    assert content == '{"query":"camera"}'
    assert captured["url"].endswith(
        "/models/gemma-4-26b-a4b-it:generateContent"
    )
    assert captured["headers"]["X-goog-api-key"] == "test-key"
    assert captured["json"]["systemInstruction"] == {
        "parts": [{"text": "system"}]
    }
    assert captured["json"]["generationConfig"] == {
        "temperature": 0,
        "responseMimeType": "application/json",
        "responseJsonSchema": schema,
    }
    assert captured["timeout"] == 10
    assert provider.last_chat_metrics["total_ms"] >= 0


def test_structured_chat_requires_api_key():
    provider = GeminiProvider(api_key="")

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        provider.structured_chat(
            "gemma-4-26b-a4b-it",
            "system",
            "user",
            {"type": "object"},
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
    assert captured["json"]["text"]["format"] == {
        "type": "json_schema",
        "name": "query_plan",
        "strict": True,
        "schema": schema,
    }
    assert captured["timeout"] == 5
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
    assert strip_json_fence('{"query":"camera"}\n```') == (
        '{"query":"camera"}'
    )


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
    assert gemini_client.last_gemini_metrics()["model"] == (
        "groq:openai/gpt-oss-20b"
    )
    assert gemini_client.last_gemini_metrics()["provider"] == "groq"


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
