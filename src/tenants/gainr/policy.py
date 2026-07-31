import re

from search.planner_catalog import normalize_filter_value

_VEHICLE_INTENT_TERMS = {
    "automobile",
    "automobiles",
    "bike",
    "cab",
    "car",
    "driver",
    "suv",
    "taxi",
    "traveller",
    "transport",
    "travel",
    "vehicle",
    "van",
}
_VEHICLE_USE_TERMS = {
    "comfort",
    "comfortable",
    "daily",
    "day",
    "distance",
    "drive",
    "hire",
    "long",
    "monthly",
    "ride",
    "rent",
    "rental",
    "safe",
    "safety",
    "tour",
    "trip",
    "weekly",
}
_VEHICLE_SERVICE_TERMS = {
    "buffing",
    "cleaning",
    "consultant",
    "detailer",
    "detailing",
    "insurance",
    "mechanic",
    "modification",
    "modifier",
    "polish",
    "polisher",
    "polishing",
    "repair",
    "service",
    "wash",
}
_PLANNER_VEHICLE_SERVICE_TERMS = _VEHICLE_SERVICE_TERMS | {
    "audit",
    "auditor",
    "inspection",
    "officer",
    "trainer",
    "training",
}
_USABLE_VEHICLE_TERMS = {
    "acting driver",
    "bike",
    "bus",
    "cab",
    "car",
    "chauffeur",
    "driver",
    "innova",
    "light motor vehicle",
    "mini truck",
    "pickup",
    "sedan",
    "suv",
    "taxi",
    "tempo traveller",
    "tourist vehicle",
    "traveller",
    "truck",
    "van",
}
_FUNCTIONAL_VEHICLE_KEYWORDS = (
    "vehicle",
    "rental",
    "car",
    "cab",
    "taxi",
    "driver",
    "van",
    "bus",
    "traveller",
    "long",
    "distance",
    "travel",
)


def contains_phrase(text: str, phrases: set[str]) -> bool:
    normalized = " ".join(text.casefold().split())
    return any(
        re.search(
            r"(?<!\w)"
            + r"\s+".join(
                re.escape(token)
                for token in phrase.casefold().split()
            )
            + r"(?!\w)",
            normalized,
        )
        for phrase in phrases
        if phrase.strip()
    )


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def _vehicle_travel_intent(query_plan: dict | None) -> bool:
    if not isinstance(query_plan, dict):
        return False
    query_text = " ".join(
        str(query_plan.get(key) or "")
        for key in ("semantic_query", "keyword_query")
    )
    query_tokens = _tokens(query_text)
    if query_tokens & _VEHICLE_SERVICE_TERMS:
        return False
    if not (query_tokens & _VEHICLE_INTENT_TERMS):
        return False
    return bool(query_tokens & _VEHICLE_USE_TERMS) or contains_phrase(
        query_text,
        {"long distance", "outstation", "road trip"},
    )


def _candidate_text(candidate: dict) -> str:
    metadata = candidate.get("metadata") or {}
    parts = [str(candidate.get("text") or "")]
    for key in (
        "content_title",
        "title",
        "main_category_name",
        "subcategory_name",
        "description",
    ):
        value = metadata.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts)


class GainrSearchPolicy:
    """Gainr marketplace interpretation without coupling it to the engine."""

    cache_key = "gainr-vehicle-v1"

    @staticmethod
    def _is_vehicle_travel_request(query: str) -> bool:
        normalized = normalize_filter_value(query)
        tokens = set(re.findall(r"[^\W_]+", normalized))
        if tokens & _PLANNER_VEHICLE_SERVICE_TERMS:
            return False
        if not (tokens & _VEHICLE_INTENT_TERMS):
            return False
        return bool(tokens & _VEHICLE_USE_TERMS) or bool(
            re.search(
                r"\b(?:long[\s-]+distance|outstation|road[\s-]+trip)\b",
                normalized,
            )
        )

    def rewrite_semantic_query(self, query: str, semantic_query: str) -> str:
        if not self._is_vehicle_travel_request(query):
            return semantic_query
        context = (
            "comfortable and safe long-distance travel using a usable vehicle "
            "rental or driver"
        )
        if context in normalize_filter_value(semantic_query):
            return semantic_query
        return f"{semantic_query} {context}".strip()

    def rewrite_keyword_query(self, query: str, keyword_query: str) -> str:
        normalized = normalize_filter_value(query)
        rough_terrain = (
            re.search(r"\brough\s+terrain\b", normalized)
            or re.search(r"\boff[\s-]?road\b", normalized)
        )
        vehicle_context = re.search(
            r"\b(?:vehicle|driv(?:e|ing)|recreational)\b",
            normalized,
        )
        if rough_terrain and vehicle_context:
            concepts = "off-road vehicle ATV 4x4"
            if "atv" not in normalize_filter_value(keyword_query):
                return f"{keyword_query} {concepts}".strip()
        if not self._is_vehicle_travel_request(query):
            return keyword_query
        keyword_query = re.sub(
            r"\b(?:comfort|comfortable|safe|safety|secure|security)\b",
            " ",
            keyword_query,
            flags=re.IGNORECASE,
        )
        keyword_query = " ".join(keyword_query.split())
        keyword_query = re.sub(
            r"(?:\b(?:and|with)\b\s*)+$",
            "",
            keyword_query,
            flags=re.IGNORECASE,
        ).strip()
        existing = set(re.findall(r"[^\W_]+", keyword_query.casefold()))
        additions = [
            term
            for term in _FUNCTIONAL_VEHICLE_KEYWORDS
            if term not in existing
        ]
        return " ".join([keyword_query, *additions]).strip()

    def infer_subcategory(self, query: str, values: dict) -> str | None:
        normalized = normalize_filter_value(query)
        is_rough_terrain = bool(
            re.search(r"\brough\s+terrain\b", normalized)
            or re.search(r"\boff[\s-]?road\b", normalized)
        )
        has_vehicle_context = bool(
            re.search(
                r"\b(?:vehicle|driv(?:e|ing)|recreational)\b",
                normalized,
            )
        )
        if not (is_rough_terrain and has_vehicle_context):
            return None
        for preferred in ("atv bike", "quad bike", "dirt bike"):
            actual = values.get(preferred)
            if actual is not None:
                return actual
        return None

    def infer_main_category(self, query: str, values: dict) -> str | None:
        if not self._is_vehicle_travel_request(query):
            return None
        return values.get("automobiles")

    def adjust_candidates(
        self,
        query_plan: dict,
        candidates: list[dict],
    ) -> list[dict]:
        if not _vehicle_travel_intent(query_plan):
            return candidates
        adjusted = []
        for candidate in candidates:
            item = dict(candidate)
            text = _candidate_text(item).casefold()
            metadata = item.get("metadata") or {}
            score = float(item.get("fusion_score") or 0.0)
            is_automobile = (
                str(metadata.get("main_category_name") or "").casefold()
                == "automobiles"
            )
            is_usable_vehicle = contains_phrase(
                text,
                _USABLE_VEHICLE_TERMS,
            )
            if is_automobile:
                score += 0.035
            if is_usable_vehicle:
                score += 0.025
            if contains_phrase(text, _VEHICLE_SERVICE_TERMS):
                score -= 0.08
            if not is_automobile and not is_usable_vehicle:
                score -= 0.06
            item["fusion_score"] = score
            adjusted.append(item)
        return sorted(
            adjusted,
            key=lambda item: float(item.get("fusion_score") or 0.0),
            reverse=True,
        )

    def rerank_context(self, query_plan: dict | None) -> str | None:
        if not _vehicle_travel_intent(query_plan):
            return None
        return (
            "Tenant domain intent: the user wants a usable vehicle or driver "
            "for travel/transport. Prefer car, cab, driver, van, bus, "
            "traveller, bike, truck, or other vehicle rental listings. Treat "
            "comfort and safety as desired vehicle qualities; generic safety "
            "officers, trainers, auditors, and safety services are irrelevant. "
            "Demote services about vehicles such as detailing, polishing, "
            "modification, insurance, cleaning, repair, or consulting unless "
            "the user explicitly asks for those services."
        )
