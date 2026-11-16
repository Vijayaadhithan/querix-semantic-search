import re

from search.planner_catalog import (
    normalize_filter_value,
    normalize_transliterated_query,
)
from search.policy import CategoryIntent
from tenants.gainr.policy_rules import (
    _ACADEMIC_SUBJECT_TEACHER_PATTERN,
    _AMBIGUOUS_VEHICLE_SERVICE_PATTERN,
    _AUTOMOTIVE_MECHANIC_PATTERN,
    _BABY_PHOTOGRAPHER_PATTERN,
    _BIRIYANI_MASTER_PATTERN,
    _CARGO_INTENT_TERMS,
    _CARGO_VEHICLE_TERMS,
    _CATEGORY_INTENT_RULES,
    _FACTORY_ELECTRICIAN_PATTERN,
    _FUNCTIONAL_VEHICLE_KEYWORDS,
    _GAINR_TRANSLITERATED_QUERY_REWRITES,
    _GAINR_USER_GENDER_PATTERNS,
    _HOUSING_RENT_TERMS,
    _HOUSING_TERMS,
    _MASSAGE_EQUIPMENT_TERMS,
    _PAINTING_EQUIPMENT_TERMS,
    _PASSENGER_VEHICLE_TERMS,
    _PLANNER_VEHICLE_SERVICE_TERMS,
    _SERVICE_WRAPPER_TERMS,
    _TAMIL_LOAD_PATTERN,
    _USABLE_VEHICLE_TERMS,
    _VEHICLE_INTENT_TERMS,
    _VEHICLE_PAINTING_PATTERN,
    _VEHICLE_SERVICE_TERMS,
    _VEHICLE_USE_TERMS,
)
from verticals.marketplace.policy import MarketplaceSearchPolicy


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


class GainrSearchPolicy(MarketplaceSearchPolicy):
    """Gainr marketplace interpretation without coupling it to the engine."""

    cache_key = "gainr-marketplace-v14"

    def normalize_query(
        self,
        query: str,
        query_aliases: dict[str, str] | None = None,
    ) -> str:
        return normalize_transliterated_query(
            query,
            query_aliases,
            _GAINR_TRANSLITERATED_QUERY_REWRITES,
        )

    def extract_user_gender_filter(self, query: str) -> int | None:
        matches = {
            gender
            for gender, pattern in _GAINR_USER_GENDER_PATTERNS.items()
            if pattern.search(query)
        }
        if 3 in matches:
            # "trans woman" contains another gender word but maps to the
            # dedicated Gainr value unambiguously.
            return 3
        return next(iter(matches)) if len(matches) == 1 else None

    def planner_instructions(self) -> str:
        return (
            super().planner_instructions()
            + " Gainr is a rental advertisement marketplace containing products, "
            "equipment, properties, vehicles, and services. Queries may use "
            "romanized/transliterated Indian-language wording. Resolve the intended "
            "meaning before selecting a catalog value; Tamil 'veetu vela kaari' "
            "means a house maid or domestic worker, not a car. Keep rental duration "
            "and rental-fee semantics. Canonical min_rental_fee and max_rental_fee "
            "are the internal lower and upper price bounds. Once a duration or price "
            "is extracted, remove it from semantic_query and keyword_query. A person "
            "hiring an available listing targets offer ads; a supplier searching for "
            "customers targets wanted ads."
        )

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
        normalized = normalize_filter_value(query)
        if _FACTORY_ELECTRICIAN_PATTERN.search(normalized):
            context = "commercial industrial electrician factory wiring"
            if context not in normalize_filter_value(semantic_query):
                return f"{semantic_query} {context}".strip()
            return semantic_query
        if _BABY_PHOTOGRAPHER_PATTERN.search(normalized):
            context = "baby newborn function baby shoot photographer"
            if context not in normalize_filter_value(semantic_query):
                return f"{semantic_query} {context}".strip()
            return semantic_query
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
        normalized = normalize_filter_value(query)
        if _FACTORY_ELECTRICIAN_PATTERN.search(normalized):
            return "commercial industrial electrician factory wiring"
        if _BABY_PHOTOGRAPHER_PATTERN.search(normalized):
            return "baby newborn function baby shoot photographer"
        if self._is_housing_rental_request(query):
            return (
                "residential accommodation room flat apartment villa "
                "guest house home stay rent"
            )
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
        biriyani_master_match = _BIRIYANI_MASTER_PATTERN.search(normalized)
        if biriyani_master_match is not None:
            actual = values.get("biriyani master")
            if actual is not None:
                return CategoryIntent(
                    subcategory=actual,
                    consumed_tokens=frozenset(_tokens(biriyani_master_match.group(0))),
                    # Queries commonly contain the catalogue child "Master"
                    # as a literal suffix. The full role phrase is more
                    # specific and must win over that generic child.
                    override_explicit_conflict=True,
                )
        automotive_mechanic_match = _AUTOMOTIVE_MECHANIC_PATTERN.search(normalized)
        if automotive_mechanic_match is not None:
            actual = values.get("mechanic")
            if actual is not None:
                return CategoryIntent(
                    subcategory=actual,
                    consumed_tokens=frozenset(
                        _tokens(automotive_mechanic_match.group(0))
                    ),
                    main_category="Automotive Professionals",
                    # "Car" is a valid product child, but in an explicit
                    # repair/mechanic phrase it names the service subject.
                    override_explicit_conflict=True,
                )
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
        vehicle_painting_match = _VEHICLE_PAINTING_PATTERN.search(normalized)
        if vehicle_painting_match is not None and not (
            tokens & _PAINTING_EQUIPMENT_TERMS
        ):
            actual = next(
                (
                    values.get(target)
                    for target in ("painter", "body shop mechanic")
                    if values.get(target)
                ),
                None,
            )
            if actual is not None:
                return CategoryIntent(
                    subcategory=actual,
                    consumed_tokens=frozenset(_tokens(vehicle_painting_match.group(0))),
                    main_category="Automotive Professionals",
                    override_explicit_conflict=True,
                    # Automotive painting spans Painter and Body Shop Mechanic.
                    # Prefer Painter without excluding the related service child.
                    relax_subcategory=True,
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
                main_category=(
                    "Automotive Professionals"
                    if targets[0] in {"acting driver", "female acting driver"}
                    else None
                ),
                override_explicit_conflict=(
                    targets[0] in {"acting driver", "female acting driver"}
                    and bool(tokens & {"bike", "car", "vehicle"})
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
        """Bypass planning only for descriptions with tenant-owned rewrites.

        Generic short phrases can still conceal product-versus-service or
        brand-versus-category intent, so they need the hosted planner.
        """
        return self._is_vehicle_travel_request(
            query
        ) or self._is_housing_rental_request(query)

    def allows_decisive_marketplace_direct_semantic(self, query: str) -> bool:
        # The shared planner has already proved both an explicit catalogue
        # match and decisive offer/wanted perspective before using this hook.
        return bool(query.strip())

    def requires_hosted_planner(self, query: str) -> bool:
        # Compound vehicle/service wording such as "car painting" must decide
        # whether the listing is a vehicle or a service before hard category
        # filters are applied.
        return bool(
            _AMBIGUOUS_VEHICLE_SERVICE_PATTERN.search(normalize_filter_value(query))
        )

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
