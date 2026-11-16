"""Versioned planner snapshots for repeatable retrieval evaluation."""

import json
from copy import deepcopy
from pathlib import Path

from search.engine import ProductSearchEngine

SNAPSHOT_VERSION = 1


def plan_snapshot_document(
    company_id: str,
    planner_fingerprint: str,
) -> dict:
    return {
        "version": SNAPSHOT_VERSION,
        "company": company_id,
        "planner_fingerprint": planner_fingerprint,
        "plans": {},
    }


def load_plan_snapshot(
    path: Path | None,
    *,
    company_id: str,
    planner_fingerprint: str,
    refresh: bool,
) -> dict:
    expected = plan_snapshot_document(company_id, planner_fingerprint)
    if path is None or refresh or not path.exists():
        return expected
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key, value in expected.items():
        if key == "plans":
            continue
        if payload.get(key) != value:
            raise ValueError(
                f"Plan snapshot {path} has stale {key!r}; rerun with --refresh-plans."
            )
    if not isinstance(payload.get("plans"), dict):
        raise ValueError(f"Plan snapshot {path} has no plans object.")
    return payload


def snapshot_planned_result(planned: dict) -> dict:
    return {
        "query_plan": deepcopy(planned["query_plan"]),
        "resolved_filters": deepcopy(planned["resolved_filters"]),
        "unresolved_filters": deepcopy(planned["unresolved_filters"]),
        "query_model_metrics": deepcopy(planned.get("query_model_metrics") or {}),
        "seconds": float(planned.get("seconds", 0.0)),
        "plan_cache_hit": bool(planned.get("plan_cache_hit")),
    }


def planned_result_for_case(
    engine: ProductSearchEngine,
    case: dict,
    snapshot: dict,
) -> tuple[dict, str]:
    name = str(case["name"])
    query = str(case["query"])
    cached = snapshot["plans"].get(name)
    if cached is not None:
        if cached.get("query") != query:
            raise ValueError(
                f"Plan snapshot query changed for case {name!r}; "
                "rerun with --refresh-plans."
            )
        planned = deepcopy(cached["planned_result"])
        planned["seconds"] = 0.0
        planned["plan_cache_hit"] = True
        return planned, "snapshot"
    planned = engine.plan(query)
    snapshot["plans"][name] = {
        "query": query,
        "planned_result": snapshot_planned_result(planned),
    }
    return planned, "live"


def planner_record(planned: dict, source: str) -> dict:
    metrics = deepcopy(planned.get("query_model_metrics") or {})
    return {
        "source": source,
        "execution_path": planned["query_plan"].get(
            "execution_path",
            "semantic",
        ),
        "model": metrics.get("model", "none"),
        "attempted_models": metrics.get("attempted_models", []),
        "attempts": metrics.get("attempts", []),
        "fallbacks": sum(
            attempt.get("status") == "fallback"
            for attempt in metrics.get("attempts", [])
        ),
    }
