import logging
import time

from core.settings import RERANK_MODEL, RERANK_TOP_K
from search.reranker import load_reranker, rerank

LOGGER = logging.getLogger("uvicorn.error")


class SearchRankingMixin:
    def ensure_reranker(self) -> float:
        if self.ranker is not None:
            return 0.0
        if self.shared_reranker is not None:
            self.ranker, load_seconds = self.shared_reranker.ensure()
            return load_seconds
        started = time.perf_counter()
        self.ranker = load_reranker()
        return time.perf_counter() - started

    def _fusion_fallback_results(
        self,
        candidates: list[dict],
        top_k: int,
    ) -> list[dict]:
        results = []
        for position, candidate in enumerate(candidates[:top_k], start=1):
            try:
                score = float(candidate.get("fusion_score"))
            except (TypeError, ValueError):
                score = 1.0 / position
            results.append(
                {
                    "id": candidate["id"],
                    "text": candidate["text"],
                    "metadata": candidate["metadata"],
                    "score": score,
                }
            )
        return results

    def rank(
        self,
        query: str,
        candidates: list[dict],
        query_plan: dict | None = None,
        top_k: int | None = None,
        trace_id: str = "-",
    ) -> dict:
        load_seconds = self.ensure_reranker()
        ranking_query = query
        if query_plan is not None:
            context = []
            keyword_query = query_plan.get("keyword_query")
            if keyword_query and keyword_query.casefold() != query.casefold():
                context.append(f"Search concepts: {keyword_query}")
            inferred = query_plan.get("inferred_categories") or {}
            category_hints = [
                value
                for value in (
                    inferred.get("main_category"),
                    inferred.get("subcategory"),
                )
                if value
            ]
            if category_hints:
                context.append(
                    "Possible catalog categories: "
                    + ", ".join(dict.fromkeys(category_hints))
                )
            domain_context = self.search_policy.rerank_context(query_plan)
            if domain_context:
                context.append(domain_context)
            if context:
                ranking_query = query + "\n" + "\n".join(context)
        started = time.perf_counter()
        LOGGER.debug(
            "[search:%s] step=rerank status=start model=%s candidates=%d top_k=%d",
            trace_id,
            getattr(self.ranker, "model_label", RERANK_MODEL),
            len(candidates),
            RERANK_TOP_K if top_k is None else top_k,
        )
        effective_top_k = RERANK_TOP_K if top_k is None else top_k

        def run_rerank():
            return rerank(
                ranking_query,
                candidates,
                self.ranker,
                effective_top_k,
                diversity_top_k=effective_top_k,
            )

        def fallback_rank(exc: Exception) -> dict:
            elapsed = time.perf_counter() - started
            attempts = list(getattr(self.ranker, "last_attempts", []))
            results = self._fusion_fallback_results(
                candidates,
                effective_top_k,
            )
            LOGGER.warning(
                "[search:%s] step=rerank status=degraded "
                "provider=fusion_fallback error_type=%s "
                "candidates=%d results=%d load_ms=%.0f duration_ms=%.0f",
                trace_id,
                type(exc).__name__,
                len(candidates),
                len(results),
                load_seconds * 1000,
                elapsed * 1000,
            )
            return {
                "results": results,
                "load_seconds": load_seconds,
                "seconds": elapsed,
                "provider": "fusion_fallback",
                "attempts": attempts,
                "degraded": True,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

        if self.shared_reranker is not None:
            with self.shared_reranker.inference_guard():
                try:
                    results = run_rerank()
                except Exception as exc:
                    return fallback_rank(exc)
        else:
            try:
                results = run_rerank()
            except Exception as exc:
                return fallback_rank(exc)
        elapsed = time.perf_counter() - started
        provider = getattr(self.ranker, "last_provider", "local")
        attempts = list(getattr(self.ranker, "last_attempts", []))
        unfiltered_count = len(results)
        cutoff = None
        if results:
            top_score = float(results[0]["score"])
            cutoffs = []
            provider_floor = self.reranker_min_score_by_provider.get(
                str(provider).casefold()
            )
            if provider_floor is not None:
                cutoffs.append(provider_floor)
            if self.reranker_relative_score_floor > 0 and top_score > 0:
                cutoffs.append(top_score * self.reranker_relative_score_floor)
            if cutoffs:
                cutoff = max(cutoffs)
                results = [
                    result for result in results if float(result["score"]) >= cutoff
                ]
        LOGGER.info(
            "[search:%s] step=rerank status=complete provider=%s results=%d "
            "pruned=%d cutoff=%s load_ms=%.0f duration_ms=%.0f",
            trace_id,
            provider,
            len(results),
            unfiltered_count - len(results),
            f"{cutoff:.6f}" if cutoff is not None else "none",
            load_seconds * 1000,
            elapsed * 1000,
        )
        return {
            "results": results,
            "load_seconds": load_seconds,
            "seconds": elapsed,
            "provider": provider,
            "attempts": attempts,
            "degraded": False,
        }
