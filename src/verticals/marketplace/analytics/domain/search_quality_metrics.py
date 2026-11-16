"""Search quality metrics."""

import logging
from collections import Counter

import pandas as pd

from .config import TYPO_INDICATORS
from .report_utils import _percentage
from .search.insights import build_additional_search_insights
from .utils import (
    detect_rental_duration,
    extract_brands,
    extract_locations,
    has_typo,
    is_gibberish,
)

LOGGER = logging.getLogger(__name__)


def query_quality_metrics(results, queries, query_cats, sh, total_q) -> None:
    # Q11: Typo/spelling errors
    typo_queries = [(q, True) for q in queries if has_typo(q)]
    results["q11_typos"] = {
        "count": len(typo_queries),
        "percentage": _percentage(len(typo_queries), total_q),
        "examples": [t[0] for t in typo_queries[:20]],
        "known_typos": [{"typo": t, "correction": c} for t, c in TYPO_INDICATORS],
        "title": "Searches with Spelling Errors",
        "chart_type": "stat",
    }

    # Q12: Query length distribution
    query_lengths = [len(q.split()) for q in queries]
    char_lengths = [len(q) for q in queries]
    len_dist = Counter()
    for query_length in query_lengths:
        if query_length <= 1:
            len_dist["1 word"] += 1
        elif query_length <= 2:
            len_dist["2 words"] += 1
        elif query_length <= 3:
            len_dist["3 words"] += 1
        elif query_length <= 5:
            len_dist["4-5 words"] += 1
        elif query_length <= 8:
            len_dist["6-8 words"] += 1
        else:
            len_dist["9+ words"] += 1
    results["q12_query_length"] = {
        "labels": [
            "1 word",
            "2 words",
            "3 words",
            "4-5 words",
            "6-8 words",
            "9+ words",
        ],
        "values": [
            len_dist.get(k, 0)
            for k in [
                "1 word",
                "2 words",
                "3 words",
                "4-5 words",
                "6-8 words",
                "9+ words",
            ]
        ],
        "avg_words": (
            round(sum(query_lengths) / len(query_lengths), 1) if query_lengths else 0
        ),
        "avg_chars": (
            round(sum(char_lengths) / len(char_lengths), 1) if char_lengths else 0
        ),
        "max_words": max(query_lengths, default=0),
        "min_words": min(query_lengths, default=0),
        "title": "Query Length Distribution",
        "chart_type": "bar",
    }

    # Q13: Gibberish detection
    gibberish = [q for q in queries if is_gibberish(q)]
    results["q13_gibberish"] = {
        "count": len(gibberish),
        "percentage": _percentage(len(gibberish), total_q),
        "examples": gibberish[:15],
        "title": "Gibberish/Accidental Queries",
        "chart_type": "stat",
    }

    # Q14: Location specificity
    loc_specific = [q for q in queries if extract_locations(q)]
    results["q14_location_specificity"] = {
        "count": len(loc_specific),
        "percentage": _percentage(len(loc_specific), total_q),
        "title": "Searches with Location Specificity",
        "chart_type": "stat",
    }

    # Q15: Search volume over time
    sh["created_at"] = pd.to_datetime(sh["created_at"])
    sh["hour"] = sh["created_at"].dt.hour
    sh["minute"] = sh["created_at"].dt.minute
    hourly = sh.groupby("hour").size().reindex(range(24), fill_value=0)
    results["q15_search_volume"] = {
        "labels": [f"{h}:00" for h in range(24)],
        "values": hourly.tolist(),
        "title": "Search Volume by Hour of Day",
        "chart_type": "line",
    }

    # Q16: New subcategory suggestions
    uncat = [
        q for q, cats in zip(queries, query_cats) if "Other / Uncategorized" in cats
    ]
    results["q16_new_subcategories"] = {
        "queries": uncat[:30],
        "count": len(uncat),
        "title": "Queries Suggesting New Categories",
        "chart_type": "list",
    }

    # Q17: High-demand cities (from searches)
    results["q17_high_demand_cities"] = results["q5_location_mentions"].copy()
    results["q17_high_demand_cities"]["title"] = (
        "High-Demand Cities from Search Queries"
    )


def unfulfilled_demand_metrics(results, data, zero_queries) -> None:
    # Q85: Zero Results in High-Demand Cities
    zero_loc_counts = Counter()
    for q in zero_queries:
        locs = extract_locations(q)
        for location in locs:
            zero_loc_counts[location] += 1
    top_zero_locs = zero_loc_counts.most_common(15)
    results["q85_zero_results_cities"] = {
        "labels": [location[0] for location in top_zero_locs],
        "values": [location[1] for location in top_zero_locs],
        "title": "Unmet Demand Locations (Zero Results)",
        "chart_type": "bar",
    }

    # Q86: Most Common Unfulfilled Brands
    zero_brand_counts = Counter()
    for q in zero_queries:
        brands = extract_brands(q)
        for b in brands:
            zero_brand_counts[b] += 1
    top_zero_brands = zero_brand_counts.most_common(15)
    results["q86_unfulfilled_brands"] = {
        "labels": [b[0] for b in top_zero_brands],
        "values": [b[1] for b in top_zero_brands],
        "title": "Most Common Unfulfilled Brands",
        "chart_type": "bar",
    }

    # Q87: Zero results by Rental Duration
    zero_dur_counts = Counter()
    for q in zero_queries:
        dur = detect_rental_duration(q)
        zero_dur_counts[dur] += 1
    results["q87_unfulfilled_duration"] = {
        "labels": list(zero_dur_counts.keys()),
        "values": list(zero_dur_counts.values()),
        "title": "Unmet Demand by Rental Duration",
        "chart_type": "doughnut",
    }

    results.update(build_additional_search_insights(data))
    LOGGER.info("Part A complete: %d questions processed", len(results))
