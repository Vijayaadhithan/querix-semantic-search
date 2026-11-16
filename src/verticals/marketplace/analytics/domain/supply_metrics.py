"""Supply metrics."""

import logging
from collections import Counter, defaultdict

import pandas as pd

LOGGER = logging.getLogger(__name__)


def supply_distribution_metrics(results, ads, cats, location, subcats) -> tuple:
    # Q47: Ads per category (searched categories vs actual supply)
    # Map category_id to sub_categories, then to categories
    subcat_to_cat = subcats.set_index("id")["categoryId"].to_dict()
    cat_names = cats.set_index("id")["name"].to_dict()

    ads["parent_cat_id"] = ads["category_id"].map(subcat_to_cat)
    ads_by_cat = ads.groupby("parent_cat_id").size()
    cat_supply = {}
    for cid, count in ads_by_cat.items():
        name = cat_names.get(cid, f"Unknown ({cid})")
        cat_supply[name] = int(count)
    cat_supply = dict(sorted(cat_supply.items(), key=lambda x: x[1], reverse=True))
    results["q47_supply_by_category"] = {
        "labels": list(cat_supply.keys())[:20],
        "values": list(cat_supply.values())[:20],
        "title": "Ad Supply by Category (Top 20)",
        "chart_type": "bar",
    }

    # Q48: Ads per city
    city_names = location.set_index("id")["city"].to_dict()
    ads_by_city = ads.groupby("city_id").size()
    city_supply = {}
    for cid, count in ads_by_city.items():
        name = city_names.get(cid, f"Unknown ({cid})")
        city_supply[name] = int(count)
    city_supply = dict(sorted(city_supply.items(), key=lambda x: x[1], reverse=True))
    results["q48_supply_by_city"] = {
        "labels": list(city_supply.keys())[:25],
        "values": list(city_supply.values())[:25],
        "title": "Ad Supply by City (Top 25)",
        "chart_type": "bar",
    }

    # Q49: Search-to-listing ratio (simplified — top categories)
    results["q49_search_to_listing"] = {
        "note": "Cross-reference with Part A Q1 categories",
        "supply_data": dict(list(cat_supply.items())[:15]),
        "title": "Supply Volume per Category",
        "chart_type": "comparison_table",
    }

    # Q50: Categories with few ads (potential gaps)
    low_supply_cats = {k: v for k, v in cat_supply.items() if v < 5000}
    low_supply_cats = dict(sorted(low_supply_cats.items(), key=lambda x: x[1]))
    results["q50_low_supply_categories"] = {
        "labels": list(low_supply_cats.keys())[:15],
        "values": list(low_supply_cats.values())[:15],
        "title": "Categories with Lowest Supply",
        "chart_type": "bar",
    }
    return ads_by_city, cat_names


def inventory_metrics(
    results, ads, ads_by_city, all_ads, cats, location, subcats, total_ads
) -> object:
    # Q58: Total ads by category group (Products vs Services)
    cat_group = cats.set_index("id")["cat_group"].to_dict()
    ads["cat_group"] = ads["parent_cat_id"].map(cat_group)
    group_map = {1: "Products", 2: "Services"}
    ads["group_label"] = ads["cat_group"].map(group_map).fillna("Unknown")
    group_dist = ads["group_label"].value_counts().to_dict()
    results["q58_products_vs_services"] = {
        "labels": list(group_dist.keys()),
        "values": [int(v) for v in group_dist.values()],
        "title": "Products vs Services (Ad Supply)",
        "chart_type": "doughnut",
    }

    # Q59: Top 20 most listed subcategories
    subcat_names = subcats.set_index("id")["name"].to_dict()
    ads_by_subcat = (
        ads.groupby("category_id").size().sort_values(ascending=False).head(20)
    )
    results["q59_top_subcategories"] = {
        "labels": [
            subcat_names.get(int(sid), f"ID:{sid}") for sid in ads_by_subcat.index
        ],
        "values": [int(v) for v in ads_by_subcat.values],
        "title": "Top 20 Most Listed Subcategories",
        "chart_type": "bar",
    }

    # Q60: Rental fee distribution
    LOGGER.debug("Processing rental fees")
    fees = ads["rental_fee"].dropna()
    fees = fees[fees > 0]
    fee_bins = [0, 100, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]
    fee_dist = Counter()
    for f in fees:
        for i in range(len(fee_bins) - 1):
            if fee_bins[i] <= f < fee_bins[i + 1]:
                fee_dist[f"₹{fee_bins[i]}-{fee_bins[i + 1]}"] += 1
                break
        else:
            fee_dist[f"₹{fee_bins[-1]}+"] += 1
    ordered_labels = [
        f"₹{fee_bins[i]}-{fee_bins[i + 1]}" for i in range(len(fee_bins) - 1)
    ] + [f"₹{fee_bins[-1]}+"]
    results["q60_rental_fee_distribution"] = {
        "labels": ordered_labels,
        "values": [fee_dist.get(k, 0) for k in ordered_labels],
        "avg_fee": round(float(fees.mean()), 0),
        "median_fee": round(float(fees.median()), 0),
        "title": "Rental Fee Distribution",
        "chart_type": "bar",
    }

    # Q61: Negotiable %
    negotiable = int(
        (ads["is_rent_negotiable"] == 1).sum()
        + (ads["is_rent_negotiable"] == "1").sum()
    )
    results["q61_negotiable"] = {
        "labels": ["Negotiable", "Fixed"],
        "values": [negotiable, total_ads - negotiable],
        "percentage": round(negotiable / total_ads * 100, 1),
        "title": "Negotiable vs Fixed Pricing",
        "chart_type": "doughnut",
    }

    # Q62: Ad status distribution
    status_dist = all_ads["status"].value_counts().to_dict()
    status_map = {
        "1": "Active",
        "8": "Active",
        "0": "Inactive",
        "2": "Pending",
        "3": "Rejected",
        "4": "Expired",
        "5": "Deleted",
        "6": "Blocked",
        "7": "Draft",
        1: "Active",
        8: "Active",
        0: "Inactive",
        2: "Pending",
        3: "Rejected",
    }
    status_labeled = defaultdict(int)
    for k, v in status_dist.items():
        label = status_map.get(k, f"Status {k}")
        status_labeled[label] += int(v)
    results["q62_ad_status"] = {
        "labels": list(status_labeled.keys()),
        "values": list(status_labeled.values()),
        "title": "Ad Status Distribution",
        "chart_type": "doughnut",
    }

    # Q63: Most viewed/favorited/liked ads
    top_viewed = ads.nlargest(10, "actual_view_count")[
        ["title", "actual_view_count"]
    ].to_dict("records")
    top_fav = ads.nlargest(10, "total_favorite")[["title", "total_favorite"]].to_dict(
        "records"
    )
    top_liked = ads.nlargest(10, "total_like")[["title", "total_like"]].to_dict(
        "records"
    )
    results["q63_top_ads"] = {
        "top_viewed": [
            {
                "title": str(r.get("title", ""))[:60],
                "value": int(r.get("actual_view_count", 0)),
            }
            for r in top_viewed
        ],
        "top_favorited": [
            {
                "title": str(r.get("title", ""))[:60],
                "value": int(r.get("total_favorite", 0)),
            }
            for r in top_fav
        ],
        "top_liked": [
            {
                "title": str(r.get("title", ""))[:60],
                "value": int(r.get("total_like", 0)),
            }
            for r in top_liked
        ],
        "title": "Top Performing Ads",
        "chart_type": "tables",
    }

    # Q64: City-wise ad density
    city_state = location.set_index("id")[["city", "state_id", "price"]].to_dict(
        "index"
    )
    city_density = []
    for cid, count in ads_by_city.head(30).items():
        info = city_state.get(cid, {})
        city_density.append(
            {
                "city": info.get("city", f"ID:{cid}"),
                "ads": int(count),
                "price_tier": float(info.get("price", 0)),
                "state_id": int(info.get("state_id", 0))
                if not pd.isna(info.get("state_id", 0))
                else 0,
            }
        )
    results["q64_city_density"] = {
        "data": city_density[:25],
        "title": "City-wise Ad Density with Pricing Tiers",
        "chart_type": "table",
    }

    # Q65: Premium/Top ads adoption
    has_premium = int(ads["premium_start_date"].notna().sum())
    has_top = int(ads["top_start_date"].notna().sum())
    results["q65_premium_adoption"] = {
        "labels": ["Premium Ads", "Top Ads", "Regular Ads"],
        "values": [has_premium, has_top, total_ads - max(has_premium, has_top)],
        "premium_pct": round(has_premium / total_ads * 100, 2),
        "top_pct": round(has_top / total_ads * 100, 2),
        "title": "Premium & Top Ads Adoption",
        "chart_type": "doughnut",
    }
    return city_state
