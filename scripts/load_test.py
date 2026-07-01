#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import statistics
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tenant_config import discover_tenant_profiles  # noqa: E402


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(round((len(ordered) - 1) * fraction), len(ordered) - 1)
    return ordered[index]


def send_search(
    url: str,
    api_key: str,
    payload: dict,
    timeout_seconds: float,
) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method="POST",
    )
    started = time.perf_counter()
    status = 0
    body: dict = {}
    error = ""
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status = response.status
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        status = exc.code
        error = exc.read().decode("utf-8", errors="replace")[:500]
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {
        "status": status,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "cached": bool(body.get("cached")),
        "result_cache_hit": bool(
            body.get("interpreted_query", {}).get("result_cache_hit")
        ),
        "execution_path": body.get("interpreted_query", {}).get(
            "execution_path",
            "unknown",
        ),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Concurrent load test for one authenticated company endpoint."
    )
    parser.add_argument("--company", required=True)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
    )
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        help="repeat for a mixed-query test; defaults to 'portable camera'",
    )
    args = parser.parse_args()
    if args.requests <= 0 or args.concurrency <= 0 or args.warmup < 0:
        parser.error("requests/concurrency must be positive and warmup non-negative")

    profiles = discover_tenant_profiles()
    try:
        profile = profiles[args.company]
    except KeyError:
        parser.error(
            f"unknown company {args.company!r}; "
            f"available: {', '.join(sorted(profiles)) or 'none'}"
        )
    api_key = next(
        (
            os.getenv(name, "").strip()
            for name in profile.api_key_envs
            if os.getenv(name, "").strip()
        ),
        "",
    )
    if not api_key:
        parser.error(
            "no API key configured in: "
            + ", ".join(profile.api_key_envs)
        )

    queries = args.queries or ["portable camera"]
    mapping = profile.payload.request_mapping
    query_field = mapping["query"]
    page_size_field = mapping["page_size"]
    url = (
        f"{args.base_url.rstrip('/')}/api/v1/"
        f"{profile.endpoint_slug}/search"
    )

    def payload(number: int) -> dict:
        return {
            query_field: queries[number % len(queries)],
            page_size_field: args.page_size,
        }

    for number in range(args.warmup):
        warmup_result = send_search(
            url,
            api_key,
            payload(number),
            args.timeout_seconds,
        )
        if warmup_result["status"] != 200:
            print(
                "Warmup failed:",
                warmup_result["status"],
                warmup_result["error"],
                file=sys.stderr,
            )
            return 1

    started = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                send_search,
                url,
                api_key,
                payload(number),
                args.timeout_seconds,
            )
            for number in range(args.requests)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started

    latencies = [result["elapsed_ms"] for result in results]
    statuses = Counter(result["status"] for result in results)
    paths = Counter(result["execution_path"] for result in results)
    errors = [result["error"] for result in results if result["error"]]
    print(f"endpoint: {url}")
    print(
        f"requests: {args.requests} | concurrency: {args.concurrency} | "
        f"elapsed_s: {elapsed:.2f} | rps: {args.requests / elapsed:.2f}"
    )
    print(
        "latency_ms: "
        f"min={min(latencies):.1f} "
        f"mean={statistics.fmean(latencies):.1f} "
        f"p50={percentile(latencies, 0.50):.1f} "
        f"p95={percentile(latencies, 0.95):.1f} "
        f"p99={percentile(latencies, 0.99):.1f} "
        f"max={max(latencies):.1f}"
    )
    print(f"statuses: {dict(sorted(statuses.items()))}")
    print(f"execution_paths: {dict(sorted(paths.items()))}")
    print(
        "cache: "
        f"response_cached={sum(result['cached'] for result in results)} "
        f"result_cache_hits="
        f"{sum(result['result_cache_hit'] for result in results)}"
    )
    if errors:
        print(f"errors: {len(errors)}; first={errors[0]}", file=sys.stderr)
    return 0 if statuses == {200: args.requests} else 1


if __name__ == "__main__":
    raise SystemExit(main())
