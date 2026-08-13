import re

from search.planner_catalog import normalize_filter_value
from search.policy import CategoryIntent

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
_TAMIL_LOAD_PATTERN = re.compile(r"[\u0b80-\u0bff].*(?:லாடு|லோடு|லாட்|லோட்)")
_CARGO_INTENT_TERMS = {
    "cargo",
    "freight",
    "goods",
    "load",
    "loading",
    "move",
    "moving",
    "transport",
}
_CARGO_VEHICLE_TERMS = {
    "cargo vehicle",
    "goods auto",
    "goods carrier",
    "goods vehicle",
    "lcv",
    "load auto",
    "mini truck",
    "pickup",
    "pickup truck",
    "tata ace",
    "tempo",
    "truck",
}
_PASSENGER_VEHICLE_TERMS = {
    "acting driver",
    "bike",
    "bus",
    "cab",
    "car",
    "chauffeur",
    "driver",
    "passenger",
    "scooter",
    "taxi",
    "tourist",
    "tourister",
    "traveller",
}
_MASSAGE_EQUIPMENT_TERMS = {
    "chair",
    "device",
    "equipment",
    "gun",
    "machine",
    "massager",
}
_HOUSING_TERMS = {
    "accommodation",
    "apartment",
    "bungalow",
    "flat",
    "home",
    "house",
    "residential",
    "room",
    "villa",
}
_HOUSING_RENT_TERMS = {"hire", "lease", "rent", "rental"}
_SERVICE_WRAPPER_TERMS = {"anyone", "can", "somebody", "someone", "who"}
_ACADEMIC_SUBJECT_TEACHER_PATTERN = re.compile(
    r"\b(?:account(?:ancy|ing)|biology|chemistry|computer\s+science|economics|"
    r"english|geography|history|math(?:ematic)?s?|physics|science)\s+"
    r"(?:teacher|tutor)\b"
)
_CATEGORY_INTENT_RULES = (
    (
        ("astrologer",),
        (re.compile(r"\b(?:astrologer|astrology|horoscope\s+(?:reader|service))\b"),),
    ),
    (
        ("massage therapist", "freelancer massage therapist"),
        (
            re.compile(
                r"\b(?:body\s+massage|massage\s+(?:service|services|"
                r"therapist|therapy)|masseu(?:r|se)|massage)\b"
            ),
        ),
    ),
    (
        ("plumber",),
        (
            re.compile(r"\bplumb(?:er|ing)\b"),
            re.compile(
                r"\b(?:fix|repair)(?:ing)?\s+(?:a\s+|the\s+)?"
                r"(?:leak(?:ing)?\s+)?(?:water\s+)?pipes?\b"
            ),
            re.compile(r"\b(?:leak(?:ing)?|burst|broken)\s+(?:water\s+)?pipes?\b"),
        ),
    ),
    (
        ("electrician", "electrician work and service man"),
        (
            re.compile(r"\belectrician\b"),
            re.compile(
                r"\b(?:fix|repair)(?:ing)?\s+(?:a\s+|the\s+)?"
                r"(?:electrical\s+)?wir(?:e|ing)\b"
            ),
            re.compile(
                r"\b(?:electrical\s+)?wir(?:e|ing)\s+"
                r"(?:fault|issue|repair|service|work)\b"
            ),
        ),
    ),
    (
        ("ac mechanic",),
        (
            re.compile(
                r"\b(?:ac|air[\s-]+condition(?:er|ing))\s+"
                r"(?:mechanic|repair|service|technician)\b"
            ),
        ),
    ),
    (
        ("fridge mechanic",),
        (
            re.compile(
                r"\b(?:fridge|refrigerator)\s+"
                r"(?:mechanic|repair|service|technician)\b"
            ),
        ),
    ),
    (
        ("washing machine mechanic",),
        (
            re.compile(
                r"\bwashing\s+machine\s+"
                r"(?:mechanic|repair|service|technician)\b"
            ),
        ),
    ),
    (
        ("tv mechanic",),
        (
            re.compile(
                r"\b(?:tv|television)\s+"
                r"(?:mechanic|repair|service|technician)\b"
            ),
        ),
    ),
    (
        ("mobile repair technician",),
        (
            re.compile(
                r"\b(?:mobile|phone|smartphone)\s+"
                r"(?:repair|service|technician)\b"
            ),
        ),
    ),
    (
        ("home tutor",),
        (
            re.compile(r"\bhome\s+tutor\b"),
            re.compile(r"\bteacher\s+for\s+home\s+lessons?\b"),
            re.compile(r"\btutor\s+(?:at|for)\s+home\b"),
        ),
    ),
    (
        ("teacher", "online teacher", "tutor"),
        (re.compile(r"\bteacher\b"),),
    ),
    (
        ("tutor", "home tutor", "online tutor"),
        (re.compile(r"\btutor\b"),),
    ),
    (
        ("maid",),
        (re.compile(r"\b(?:house[\s-]+maid|domestic\s+(?:help|worker))\b"),),
    ),
    (
        ("house cleaning labour", "cleaner"),
        (re.compile(r"\b(?:house|home)\s+(?:cleaner|cleaning\s+(?:help|service))\b"),),
    ),
)


def contains_phrase(text: str, phrases: set[str]) -> bool:
    normalized = " ".join(text.casefold().split())
    return any(
        re.search(
            r"(?<!\w)"
            + r"\s+".join(re.escape(token) for token in phrase.casefold().split())
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
        str(query_plan.get(key) or "") for key in ("semantic_query", "keyword_query")
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


def _cargo_transport_intent(query_plan: dict | None) -> bool:
    if not isinstance(query_plan, dict):
        return False
    query_text = " ".join(
        str(query_plan.get(key) or "") for key in ("semantic_query", "keyword_query")
    )
    return bool(_tokens(query_text) & _CARGO_INTENT_TERMS)


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

    cache_key = "gainr-marketplace-v7"

    @staticmethod
    def _is_tamil_load_transport_request(query: str) -> bool:
        return bool(_TAMIL_LOAD_PATTERN.search(query))

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

    @staticmethod
    def _is_housing_rental_request(query: str) -> bool:
        tokens = _tokens(normalize_filter_value(query))
        return bool(tokens & _HOUSING_TERMS) and bool(tokens & _HOUSING_RENT_TERMS)

    def rewrite_semantic_query(self, query: str, semantic_query: str) -> str:
        if self._is_tamil_load_transport_request(query):
            return "goods load transport truck mini truck cargo vehicle"
        if self._is_housing_rental_request(query):
            context = (
                "residential accommodation room flat apartment villa "
                "guest house home stay for rent"
            )
            if context not in normalize_filter_value(semantic_query):
                return f"{semantic_query} {context}".strip()
            return semantic_query
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
        if self._is_tamil_load_transport_request(query):
            return "goods load transport truck mini truck cargo vehicle tata ace"
        if self._is_housing_rental_request(query):
            return (
                "residential accommodation room flat apartment villa "
                "guest house home stay rent"
            )
        normalized = normalize_filter_value(query)
        rough_terrain = re.search(r"\brough\s+terrain\b", normalized) or re.search(
            r"\boff[\s-]?road\b", normalized
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
            term for term in _FUNCTIONAL_VEHICLE_KEYWORDS if term not in existing
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

    def category_intent(
        self,
        query: str,
        values: dict,
    ) -> CategoryIntent | None:
        normalized = normalize_filter_value(query)
        tokens = _tokens(normalized)
        academic_match = _ACADEMIC_SUBJECT_TEACHER_PATTERN.search(normalized)
        if academic_match is not None:
            role = "tutor" if "tutor" in academic_match.group(0) else "teacher"
            preferred = (
                ("tutor", "home tutor", "online tutor")
                if role == "tutor"
                else ("teacher", "online teacher", "tutor")
            )
            actual = next(
                (values.get(target) for target in preferred if values.get(target)),
                None,
            )
            if actual is not None:
                return CategoryIntent(
                    subcategory=actual,
                    consumed_tokens=frozenset(_tokens(academic_match.group(0))),
                    # Subject names can also be product subcategories (for
                    # example Books > Mathematics). The explicit role makes
                    # the service intent unambiguous in this narrow case.
                    override_explicit_conflict=True,
                )
        for targets, patterns in _CATEGORY_INTENT_RULES:
            if targets[0] == "massage therapist" and (
                tokens & _MASSAGE_EQUIPMENT_TERMS
            ):
                continue
            match = None
            for pattern in patterns:
                match = pattern.search(normalized)
                if match is not None:
                    break
            if match is None:
                continue
            actual = next(
                (values.get(target) for target in targets if values.get(target)),
                None,
            )
            if actual is None:
                continue
            return CategoryIntent(
                subcategory=actual,
                consumed_tokens=frozenset(
                    _tokens(match.group(0)) | (tokens & _SERVICE_WRAPPER_TERMS)
                ),
            )
        return None

    def infer_main_category(self, query: str, values: dict) -> str | None:
        if self._is_housing_rental_request(query):
            return values.get("accommodation & spaces")
        if not self._is_vehicle_travel_request(query):
            return None
        return values.get("automobiles")

    def allows_descriptive_direct_semantic(self, query: str) -> bool:
        """Let hybrid retrieval handle simple Gainr offer descriptions.

        The generic planner has already rejected locations, prices, rental
        durations, wanted-ad language, non-ASCII text, numeric constraints,
        and known spelling normalization before this hook is consulted.
        """
        return bool(re.search(r"[a-z]", normalize_filter_value(query)))

    def adjust_candidates(
        self,
        query_plan: dict,
        candidates: list[dict],
    ) -> list[dict]:
        cargo_transport = _cargo_transport_intent(query_plan)
        if not cargo_transport and not _vehicle_travel_intent(query_plan):
            return candidates
        adjusted = []
        for candidate in candidates:
            item = dict(candidate)
            text = _candidate_text(item).casefold()
            metadata = item.get("metadata") or {}
            score = float(item.get("fusion_score") or 0.0)
            if cargo_transport:
                is_cargo_vehicle = contains_phrase(text, _CARGO_VEHICLE_TERMS)
                is_passenger_vehicle = contains_phrase(
                    text,
                    _PASSENGER_VEHICLE_TERMS,
                )
                if is_cargo_vehicle:
                    score += 0.12
                if is_passenger_vehicle and not is_cargo_vehicle:
                    score -= 0.10
                if not is_cargo_vehicle and not is_passenger_vehicle:
                    score -= 0.04
                item["fusion_score"] = score
                adjusted.append(item)
                continue
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
        if _cargo_transport_intent(query_plan):
            return (
                "Tenant domain intent: the user needs goods/load transport. "
                "Prefer cargo vehicles, goods carriers, load autos, Tata Ace, "
                "mini trucks, pickup trucks, LCVs, tempos, and trucks. Passenger "
                "cars, cabs, taxis, bikes, tourist vehicles, and acting-driver-only "
                "listings are irrelevant unless the listing is explicitly suitable "
                "for carrying goods."
            )
        query_text = " ".join(
            str((query_plan or {}).get(key) or "")
            for key in ("semantic_query", "keyword_query")
        )
        if self._is_housing_rental_request(query_text):
            return (
                "Tenant domain intent: the user needs residential accommodation "
                "for rent. Prefer rooms, flats, service apartments, villas, guest "
                "houses, home stays, bungalows, cottages, and farmhouses. Demote "
                "event halls, commercial spaces, vehicles, equipment, and unrelated "
                "services."
            )
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
