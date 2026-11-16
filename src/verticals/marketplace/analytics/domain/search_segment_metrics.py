"""Search segment metrics."""

import logging
from collections import Counter

from .report_utils import _percentage
from .utils import (
    is_b2b_query,
)

LOGGER = logging.getLogger(__name__)


def segment_demand_metrics(results, queries, total_q) -> None:
    # Q18: Vehicle-specific searches
    vehicle_queries = []
    for q in queries:
        ql = q.lower()
        vehicle_kws = [
            "car",
            "bike",
            "truck",
            "van",
            "bus",
            "auto",
            "scooter",
            "tata",
            "maruti",
            "mahindra",
            "eicher",
            "bajaj",
            "honda",
            "yamaha",
            "royal enfield",
            "hyundai",
            "tvs",
            "piaggio",
            "vehicle",
            "two wheeler",
            "three wheeler",
            "four wheeler",
            "lcv",
        ]
        if any(kw in ql for kw in vehicle_kws):
            vehicle_queries.append(q)
    vehicle_types = Counter()
    for q in vehicle_queries:
        ql = q.lower()
        if any(
            w in ql
            for w in [
                "car",
                "alto",
                "swift",
                "ertiga",
                "innova",
                "civic",
                "xcent",
                "baleno",
                "eeco",
                "omni",
                "qualis",
            ]
        ):
            vehicle_types["Cars"] += 1
        elif any(
            w in ql
            for w in [
                "bike",
                "two wheeler",
                "rx",
                "r15",
                "hunter",
                "unicorn",
                "hf deluxe",
                "tnt",
                "splendor",
            ]
        ):
            vehicle_types["Bikes"] += 1
        elif any(w in ql for w in ["truck", "lcv", "container", "open body", "eicher"]):
            vehicle_types["Trucks / LCV"] += 1
        elif any(w in ql for w in ["auto", "ape", "three wheeler", "qute"]):
            vehicle_types["Autos / 3-Wheeler"] += 1
        elif any(w in ql for w in ["van", "bus", "winger", "seater"]):
            vehicle_types["Vans / Buses"] += 1
        elif any(w in ql for w in ["scooter", "dio", "scooty", "pep"]):
            vehicle_types["Scooters"] += 1
        elif any(
            w in ql for w in ["tata ace", "ace", "dost", "intra", "bolero", "pickup"]
        ):
            vehicle_types["Mini Trucks / Pickup"] += 1
        else:
            vehicle_types["Other Vehicle"] += 1
    results["q18_vehicle_searches"] = {
        "labels": list(vehicle_types.keys()),
        "values": list(vehicle_types.values()),
        "total": len(vehicle_queries),
        "percentage": _percentage(len(vehicle_queries), total_q),
        "sample_queries": vehicle_queries[:20],
        "title": "Vehicle Rental Searches Breakdown",
        "chart_type": "doughnut",
    }

    # Q19: Food industry searches
    food_queries = []
    for q in queries:
        ql = q.lower()
        food_kws = [
            "cook",
            "chef",
            "parotta",
            "biriyani",
            "caterer",
            "food",
            "kitchen",
            "restaurant",
            "hotel",
            "bar",
            "stove",
            "cooking",
            "veg",
        ]
        if any(kw in ql for kw in food_kws):
            food_queries.append(q)
    results["q19_food_searches"] = {
        "queries": food_queries,
        "count": len(food_queries),
        "percentage": _percentage(len(food_queries), total_q),
        "title": "Food Industry Searches",
        "chart_type": "list",
    }

    # Q20: B2B demand
    b2b_queries = [q for q in queries if is_b2b_query(q)]
    results["q20_b2b_demand"] = {
        "queries": b2b_queries,
        "count": len(b2b_queries),
        "percentage": _percentage(len(b2b_queries), total_q),
        "title": "B2B Demand Indicators",
        "chart_type": "list",
    }

    # New Additions for Advanced Search Analytics
