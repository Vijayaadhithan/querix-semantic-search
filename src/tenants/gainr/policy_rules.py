"""Gainr query vocabulary and compiled interpretation rules."""

import re

_GAINR_USER_GENDER_PATTERNS = {
    1: re.compile(
        r"(?<!\w)(?:male|man|men|gentleman|boy|boys|purush|aadmi|admi|"
        r"ladka|aan|aambala|ஆண்|ஆண்கள்|पुरुष|आदमी|लड़का)(?!\w)",
        re.IGNORECASE,
    ),
    2: re.compile(
        r"(?<!\w)(?:female|woman|women|lady|ladies|girl|girls|mahila|"
        r"ladki|ladkiyan|aurat|kaam\s+wali|ponnu|pennu|penn|pengal|"
        r"பெண்|பெண்கள்|பெண்மணி|महिला|महिलाओं|लड़की|लड़कियां|औरत|औरतें)(?!\w)",
        re.IGNORECASE,
    ),
    3: re.compile(
        r"(?<!\w)(?:transgender|trans\s+(?:woman|man|person)|trans|hijra|"
        r"kinnar|திருநங்கை|திருநம்பி|हिजड़ा|किन्नर)(?!\w)",
        re.IGNORECASE,
    ),
}

_GAINR_TRANSLITERATED_QUERY_REWRITES = (
    (
        re.compile(
            r"(?<!\w)ve{1,2}t{1,2}u\s+ve(?:lai|la)\s*kaa?ri(?!\w)",
            re.IGNORECASE,
        ),
        "house maid domestic worker",
    ),
    (
        re.compile(
            r"(?<!\w)(?:ghar\s+k[ai]\s+)?kaam\s+wali\s+bai(?!\w)",
            re.IGNORECASE,
        ),
        "house maid domestic worker",
    ),
    (
        re.compile(
            r"(?<!\w)(?:kalyanathuku|kalyanathukku|"
            r"kalyaanathuku|kalyaanathukku)(?!\w)",
            re.IGNORECASE,
        ),
        "for wedding",
    ),
)

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

_FACTORY_ELECTRICIAN_PATTERN = re.compile(
    r"\b(?:factory|industrial|commercial)\b.*\b(?:electrician|wir(?:e|ing))\b"
    r"|\b(?:electrician|wir(?:e|ing))\b.*\b(?:factory|industrial|commercial)\b"
)

_BABY_PHOTOGRAPHER_PATTERN = re.compile(
    r"\b(?:photographer|photography|photo\s*shoot)\b.*\b"
    r"(?:baby|babies|newborn|infant)\b"
    r"|\b(?:baby|babies|newborn|infant)\b.*\b"
    r"(?:photographer|photography|photo\s*shoot|functions?)\b"
)

_BIRIYANI_MASTER_PATTERN = re.compile(r"\bb(?:ir|ri|iri)yani\s+(?:cooking\s+)?master\b")

_AUTOMOTIVE_MECHANIC_PATTERN = re.compile(
    r"\b(?:car|automobile|four[\s-]+wheeler)\b"
    r"(?:\s+[a-z0-9'-]+){0,3}\s+"
    r"\b(?:breakdown|mechanic|repair(?:er|ing)?|servic(?:e|ing))\b"
    r"|\b(?:breakdown|mechanic|repair(?:er|ing)?|servic(?:e|ing))\b"
    r"(?:\s+[a-z0-9'-]+){0,3}\s+"
    r"\b(?:car|automobile|four[\s-]+wheeler)\b"
    r"|\bmechanic\b(?:\s+[a-z0-9'-]+){0,3}\s+"
    r"\b(?:breakdown|road[\s-]*side|assistance|assitance)\b"
)

_AMBIGUOUS_VEHICLE_SERVICE_PATTERN = re.compile(
    r"\b(?:bike|car|vehicle)\b(?:\s+[a-z0-9'-]+){0,2}\s+"
    r"\b(?:acting\s+driver|driver|paint(?:ing|er)?|repair|mechanic|"
    r"cleaning|wash|detailing|service)\b"
    r"|\b(?:acting\s+driver|driver|paint(?:ing|er)?|repair|mechanic|"
    r"cleaning|wash|detailing|service)\b(?:\s+[a-z0-9'-]+){0,2}\s+"
    r"\b(?:bike|car|vehicle)\b"
)

_VEHICLE_PAINTING_PATTERN = re.compile(
    r"\b(?:bike|car|vehicle)\b(?:\s+[a-z0-9'-]+){0,2}\s+"
    r"\b(?:paint(?:ing|er)?|body\s+work)\b"
    r"|\b(?:paint(?:ing|er)?|body\s+work)\b(?:\s+[a-z0-9'-]+){0,2}\s+"
    r"\b(?:bike|car|vehicle)\b"
)

_PAINTING_EQUIPMENT_TERMS = {
    "booth",
    "equipment",
    "gun",
    "machine",
    "sprayer",
}

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
        ("female acting driver", "acting driver"),
        (
            re.compile(
                r"\b(?:female|lady|woman)\s+(?:car\s+|four[\s-]+wheeler\s+)?"
                r"driver\b"
            ),
        ),
    ),
    (
        ("acting driver", "acting driver agent"),
        (re.compile(r"\b(?:acting\s+|car\s+|four[\s-]+wheeler\s+)?driver\b"),),
    ),
    (
        ("care taker",),
        (re.compile(r"\bcare[\s-]*taker\b"),),
    ),
    (
        ("bartender",),
        (re.compile(r"\bbar[\s-]*tender\b"),),
    ),
    (
        ("sales executive",),
        (
            re.compile(r"\bsales[\s-]*(?:executive|person|personnel)\b"),
            re.compile(r"\bsalesperson\b"),
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
