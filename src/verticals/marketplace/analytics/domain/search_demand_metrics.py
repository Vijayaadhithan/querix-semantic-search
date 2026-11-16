"""Search demand metrics."""

import logging
import re
from collections import Counter

from .report_utils import _percentage
from .utils import (
    classify_search_query,
    detect_language,
    detect_rental_duration,
    extract_brands,
    extract_locations,
    is_route_query,
)

LOGGER = logging.getLogger(__name__)


def category_demand_metrics(results, queries) -> object:
    # Q1: Top searched categories
    cat_counts = Counter()
    query_cats = []
    for q in queries:
        cats = classify_search_query(q)
        query_cats.append(cats)
        for c in cats:
            cat_counts[c] += 1
    top_cats = cat_counts.most_common(20)
    results["q1_category_distribution"] = {
        "labels": [c[0] for c in top_cats],
        "values": [c[1] for c in top_cats],
        "title": "Top Searched Categories",
        "chart_type": "bar",
    }

    # Q2: Products vs Services vs Accommodation %
    type_counts = {"Products": 0, "Services": 0, "Accommodation": 0, "Other": 0}
    for cats in query_cats:
        is_service = "Services & Professionals" in cats
        is_accom = "Home & Accommodation" in cats
        is_product = any(
            c
            not in [
                "Services & Professionals",
                "Home & Accommodation",
                "Other / Uncategorized",
            ]
            for c in cats
        )
        if is_service:
            type_counts["Services"] += 1
        elif is_accom:
            type_counts["Accommodation"] += 1
        elif is_product:
            type_counts["Products"] += 1
        else:
            type_counts["Other"] += 1
    results["q2_product_vs_service"] = {
        "labels": list(type_counts.keys()),
        "values": list(type_counts.values()),
        "title": "Products vs Services vs Accommodation",
        "chart_type": "doughnut",
    }

    # Q3: Top trending search terms (word frequency)
    word_freq = Counter()
    stop_words = {
        "for",
        "in",
        "on",
        "of",
        "to",
        "and",
        "the",
        "a",
        "an",
        "or",
        "is",
        "at",
        "by",
        "from",
        "with",
        "per",
        "if",
        "any",
        "all",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "it",
        "its",
        "am",
        "be",
        "no",
        "not",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "will",
        "can",
        "could",
        "should",
        "would",
        "may",
        "shall",
        "here",
        "there",
        "one",
        "this",
        "that",
        "these",
        "those",
        "-",
        "&",
    }
    for q in queries:
        words = re.findall(r"[a-zA-Z]+", q.lower())
        for w in words:
            if w not in stop_words and len(w) > 2:
                word_freq[w] += 1
    top_terms = word_freq.most_common(30)
    results["q3_trending_terms"] = {
        "labels": [t[0] for t in top_terms],
        "values": [t[1] for t in top_terms],
        "title": "Top 30 Trending Search Terms",
        "chart_type": "bar",
    }
    return query_cats


def search_preferences_metrics(results, api, queries, sh, total_q) -> object:
    # Q4: Brand/model mentions
    brand_counts = Counter()
    for q in queries:
        brands = extract_brands(q)
        for b in brands:
            brand_counts[b] += 1
    top_brands = brand_counts.most_common(20)
    results["q4_brand_mentions"] = {
        "labels": [b[0] for b in top_brands],
        "values": [b[1] for b in top_brands],
        "title": "Most Searched Brands/Models",
        "chart_type": "bar",
    }

    # Q5: Location mentions in queries
    loc_counts = Counter()
    for q in queries:
        locs = extract_locations(q)
        for location in locs:
            loc_counts[location] += 1
    top_locs = loc_counts.most_common(20)
    results["q5_location_mentions"] = {
        "labels": [location[0] for location in top_locs],
        "values": [location[1] for location in top_locs],
        "title": "Most Mentioned Locations in Searches",
        "chart_type": "bar",
    }

    # Q6: Rental duration preferences
    dur_counts = Counter()
    for q in queries:
        dur = detect_rental_duration(q)
        dur_counts[dur] += 1
    results["q6_rental_duration"] = {
        "labels": list(dur_counts.keys()),
        "values": list(dur_counts.values()),
        "title": "Rental Duration Preferences",
        "chart_type": "doughnut",
    }

    # Q7: Zero-result queries
    merged = sh.merge(
        api,
        on="request_id",
        how="left",
    )
    successful = (
        merged["status"]
        .fillna("")
        .astype(str)
        .str.casefold()
        .isin({"success", "successful", "ok"})
    )
    successful_searches = merged[successful]
    zero_results = successful_searches[successful_searches["total_results"] == 0]
    zero_queries = zero_results["query_text"].dropna().tolist()
    results["q7_zero_results"] = {
        "queries": zero_queries[:50],
        "total_zero": len(zero_queries),
        "total_searches": len(successful_searches),
        "total_text_requests": total_q,
        "failed_text_requests": total_q - len(successful_searches),
        "percentage": _percentage(len(zero_queries), len(successful_searches)),
        "title": "Searches with Zero Results (Unmet Demand)",
        "chart_type": "stat",
    }

    # Q8: Route/trip searches
    route_queries = [q for q in queries if is_route_query(q)]
    results["q8_route_searches"] = {
        "queries": route_queries,
        "count": len(route_queries),
        "percentage": _percentage(len(route_queries), total_q),
        "title": "Route/Trip-Based Searches",
        "chart_type": "list",
    }

    # Q9: Top searched professions
    profession_kws = {
        "Acting Driver": ["acting driver", "driver"],
        "Massage Therapist": ["massage", "therapist", "massager"],
        "Cook / Parotta Master": ["cook", "parotta", "chef", "caterer", "biriyani"],
        "Electrician": ["electrician", "electrical"],
        "Photographer": ["photographer", "photography"],
        "Trainer / Instructor": ["trainer", "instructor", "training"],
        "Tutor / Teacher": ["tutor", "tuition", "teacher"],
        "Nurse / Medical": ["nurse", "doctor", "medical", "psychiatrist"],
        "Carpenter / Painter": ["carpenter", "painter", "mason"],
        "Plumber": ["plumber"],
        "Helper / Companion": ["helper", "companion"],
        "Writer / Editor": ["writer", "editor", "data entry", "typing"],
        "Cleaner / Climber": ["cleaner", "climber"],
        "Delivery / Logistics": ["delivery", "loading", "unloading"],
    }
    prof_counts = Counter()
    for q in queries:
        ql = q.lower()
        for prof, kws in profession_kws.items():
            if any(kw in ql for kw in kws):
                prof_counts[prof] += 1
                break
    top_profs = prof_counts.most_common(15)
    results["q9_professions"] = {
        "labels": [p[0] for p in top_profs],
        "values": [p[1] for p in top_profs],
        "title": "Most Searched Professions/Services",
        "chart_type": "bar",
    }

    # Q10: Language patterns
    lang_counts = Counter()
    for q in queries:
        lang = detect_language(q)
        lang_counts[lang] += 1
    results["q10_language"] = {
        "labels": list(lang_counts.keys()),
        "values": list(lang_counts.values()),
        "title": "Search Language Distribution",
        "note": (
            "Likely Tamil (Latin script) means Tamil words typed with English "
            "letters. It is a cautious text-pattern estimate, not a confirmed "
            "language identity."
        ),
        "chart_type": "doughnut",
    }
    return zero_queries
