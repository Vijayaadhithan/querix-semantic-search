import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ollama_client import (
    OllamaProvider,
    normalize_keep_alive,
    ollama_timing_metrics,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_ollama_timing_metrics_converts_nanoseconds_to_milliseconds():
    assert ollama_timing_metrics(
        {
            "total_duration": 2_500_000,
            "load_duration": 500_000,
            "prompt_eval_duration": 1_000_000,
            "eval_duration": 750_000,
        }
    ) == {
        "total_ms": 2.5,
        "load_ms": 0.5,
        "prompt_eval_ms": 1.0,
        "eval_ms": 0.75,
    }


def test_preload_embedding_model_uses_configured_keep_alive(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(
            {
                "embeddings": [[0.1, 0.2]],
                "total_duration": 4_000_000,
                "load_duration": 0,
            }
        )

    monkeypatch.setattr("ollama_client.requests.post", fake_post)
    provider = OllamaProvider(
        base_url="http://ollama.test",
        keep_alive="-1",
    )

    metrics = provider.preload_embedding_model()

    assert captured["url"] == "http://ollama.test/api/embed"
    assert captured["json"] == {
        "model": provider.embedding_model,
        "input": ["startup warmup"],
        "keep_alive": -1,
    }
    assert metrics["total_ms"] == 4.0


def test_keep_alive_normalizes_integer_environment_values():
    assert normalize_keep_alive("-1") == -1
    assert normalize_keep_alive("0") == 0
    assert normalize_keep_alive("3600") == 3600
    assert normalize_keep_alive("24h") == "24h"
    assert normalize_keep_alive("-1m") == "-1m"


def test_embedding_error_includes_ollama_response(monkeypatch):
    class ErrorResponse:
        def raise_for_status(self):
            error = requests.HTTPError("400 Client Error")
            error.response = self
            raise error

        def json(self):
            return {"error": "invalid duration -1"}

    monkeypatch.setattr(
        "ollama_client.requests.post",
        lambda *_args, **_kwargs: ErrorResponse(),
    )
    provider = OllamaProvider(
        base_url="http://ollama.test",
        keep_alive="-1",
    )

    try:
        provider.embed_texts(["test"])
    except RuntimeError as exc:
        assert "Ollama error: invalid duration -1" in str(exc)
    else:
        raise AssertionError("Expected the Ollama request to fail")
