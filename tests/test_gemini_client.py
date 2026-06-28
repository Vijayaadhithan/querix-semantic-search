import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gemini_client
from gemini_client import (
    GeminiModelUnavailableError,
    GeminiProvider,
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
        "gemma-4-26b-a4b-it",
        "gemma-4-31b-it",
        "gemini-3.1-flash-lite",
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

    monkeypatch.setattr("gemini_client.requests.post", fake_post)
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
    assert captured["timeout"] == 60
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
