"""Pure projection of search stages for the operator event timeline."""

from typing import Any


def monitor_timeline(
    result: dict[str, Any],
    duration_ms: float,
) -> list[dict[str, Any]]:
    query_plan = result.get("query_plan") or {}
    query_metrics = result.get("query_model_metrics") or {}
    embedding = result.get("embedding_model_metrics") or {}
    execution_path = query_plan.get("execution_path", "semantic")
    timeline: list[dict[str, Any]] = [
        {
            "step": "plan",
            "status": ("cache_hit" if result.get("plan_cache_hit") else "complete"),
            "duration_ms": round(
                float(result.get("seconds", 0.0)) * 1000,
                3,
            ),
            "execution_path": execution_path,
            "model": query_metrics.get("model") or "none",
            "resolved_filter_groups": len(result.get("resolved_filters") or {}),
            "unresolved_filters": len(result.get("unresolved_filters") or {}),
        }
    ]
    if result.get("result_cache_hit"):
        timeline.append(
            {
                "step": "result_cache",
                "status": "hit",
                "duration_ms": round(
                    float(result.get("result_cache_seconds", 0.0)) * 1000,
                    3,
                ),
                "products": len(result.get("products") or []),
            }
        )
    elif execution_path == "deterministic_filter":
        timeline.append(
            {
                "step": "fast_filter",
                "status": "complete",
                "duration_ms": round(
                    float(result.get("related_tail_seconds", 0.0)) * 1000,
                    3,
                ),
                "products": len(result.get("products") or []),
            }
        )
    else:
        vector_results = result.get("vector_results") or []
        bm25_results = result.get("bm25_results") or []
        candidates = result.get("candidates") or []
        timeline.extend(
            [
                {
                    "step": "retrieve",
                    "status": (
                        "degraded" if result.get("retrieval_degraded") else "complete"
                    ),
                    "duration_ms": round(
                        float(
                            result.get(
                                "retrieval_seconds",
                                max(
                                    float(
                                        result.get(
                                            "vector_seconds",
                                            0.0,
                                        )
                                    ),
                                    float(
                                        result.get(
                                            "bm25_seconds",
                                            0.0,
                                        )
                                    ),
                                ),
                            )
                        )
                        * 1000,
                        3,
                    ),
                    "vector_ms": round(
                        float(result.get("vector_seconds", 0.0)) * 1000,
                        3,
                    ),
                    "bm25_ms": round(
                        float(result.get("bm25_seconds", 0.0)) * 1000,
                        3,
                    ),
                    "embedding_total_ms": round(
                        float(embedding.get("total_ms", 0.0)),
                        3,
                    ),
                    "embedding_load_ms": round(
                        float(embedding.get("load_ms", 0.0)),
                        3,
                    ),
                    "parallel_ms": round(
                        float(
                            result.get(
                                "parallel_retrieval_seconds",
                                0.0,
                            )
                        )
                        * 1000,
                        3,
                    ),
                    "fusion_ms": round(
                        float(result.get("fusion_seconds", 0.0)) * 1000,
                        3,
                    ),
                    "type_lookup_ms": round(
                        float(result.get("type_lookup_seconds", 0.0)) * 1000,
                        3,
                    ),
                    "vector_query_metrics": result.get(
                        "vector_query_metrics",
                        {},
                    ),
                    "vector_results": len(vector_results),
                    "bm25_results": len(bm25_results),
                    "candidates": len(candidates),
                    "degraded_stages": result.get(
                        "degraded_stages",
                        [],
                    ),
                    "error_type": result.get("retrieval_error_type"),
                },
                {
                    "step": "rerank",
                    "status": (
                        "degraded"
                        if result.get("reranker_degraded")
                        else "complete"
                        if candidates
                        else "skipped"
                    ),
                    "duration_ms": round(
                        float(result.get("reranker_seconds", 0.0)) * 1000,
                        3,
                    ),
                    "provider": result.get(
                        "reranker_provider",
                        "none",
                    ),
                    "results": len(result.get("reranked") or []),
                    "error_type": result.get("reranker_error_type"),
                },
                {
                    "step": "related_tail",
                    "status": "complete",
                    "duration_ms": round(
                        float(
                            result.get(
                                "related_tail_seconds",
                                0.0,
                            )
                        )
                        * 1000,
                        3,
                    ),
                    "primary": len(result.get("primary_product_ids") or []),
                    "related": len(result.get("related_product_ids") or []),
                },
                {
                    "step": "database_map",
                    "status": "complete",
                    "products": len(result.get("products") or []),
                },
            ]
        )
    timeline.append(
        {
            "step": "search",
            "status": "complete",
            "duration_ms": round(duration_ms, 3),
            "products": len(result.get("products") or []),
        }
    )
    return timeline
