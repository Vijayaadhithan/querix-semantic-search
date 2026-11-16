"""Market supply metrics."""

import logging

LOGGER = logging.getLogger(__name__)


def market_supply_metrics(
    results, ads, cat_names, city_names, location, state_names, subcat_to_cat, subcats
) -> None:
    # Q75: Category-wise marketplace overview
    ads["parent_cat_id"] = ads["category_id"].map(subcat_to_cat)
    cat_overview = []
    for cid, name in cat_names.items():
        cat_ads = ads[ads["parent_cat_id"] == cid]
        if len(cat_ads) == 0:
            continue
        fees = cat_ads["rental_fee"].dropna()
        fees = fees[fees > 0]
        # Top city for this category
        top_city_id = (
            cat_ads["city_id"].value_counts().index[0] if len(cat_ads) > 0 else None
        )
        top_city = city_names.get(top_city_id, "N/A")
        cat_overview.append(
            {
                "category": name,
                "ad_count": int(len(cat_ads)),
                "avg_fee": round(float(fees.mean()), 0) if len(fees) > 0 else 0,
                "top_city": top_city,
            }
        )
    cat_overview.sort(key=lambda x: x["ad_count"], reverse=True)
    results["q75_marketplace_overview"] = {
        "data": cat_overview,
        "title": "Category-wise Marketplace Overview",
        "chart_type": "table",
    }

    # Q76: Geographic heatmap (ads per state)
    ads_city_state = ads.merge(
        location[["id", "state_id"]].rename(columns={"id": "city_id"}),
        on="city_id",
        how="left",
        suffixes=("", "_loc"),
    )
    state_col = (
        "state_id_loc" if "state_id_loc" in ads_city_state.columns else "state_id"
    )
    ads_by_state = ads_city_state.groupby(state_col).size()
    state_supply = {}
    for sid, count in ads_by_state.items():
        try:
            name = state_names.get(int(sid), "Unknown")
        except (TypeError, ValueError, OverflowError):
            name = "Unknown"
        state_supply[name] = int(count)
    state_supply = dict(sorted(state_supply.items(), key=lambda x: x[1], reverse=True))
    results["q76_geographic_heatmap"] = {
        "labels": list(state_supply.keys())[:20],
        "values": list(state_supply.values())[:20],
        "title": "Ad Supply by State (Geographic Heatmap)",
        "chart_type": "bar",
    }

    # Q77: Top 50 listing types (subcategory × city)
    subcat_names = subcats.set_index("id")["name"].to_dict()
    top_combos = (
        ads.groupby(["category_id", "city_id"])
        .size()
        .sort_values(ascending=False)
        .head(30)
    )
    combo_list = []
    for (scid, cid), count in top_combos.items():
        combo_list.append(
            {
                "subcategory": subcat_names.get(int(scid), f"ID:{scid}"),
                "city": city_names.get(int(cid), f"ID:{cid}"),
                "ad_count": int(count),
            }
        )
    results["q77_top_listings"] = {
        "data": combo_list,
        "title": "Top 30 Subcategory × City Combinations",
        "chart_type": "table",
    }

    # Q78: Pricing benchmark (avg fee by category)
    pricing = []
    for cid, name in cat_names.items():
        cat_ads = ads[ads["parent_cat_id"] == cid]
        fees = cat_ads["rental_fee"].dropna()
        fees = fees[(fees > 0) & (fees < 1000000)]
        if len(fees) > 0:
            pricing.append(
                {
                    "category": name,
                    "avg_fee": round(float(fees.mean()), 0),
                    "median_fee": round(float(fees.median()), 0),
                    "min_fee": round(float(fees.min()), 0),
                    "max_fee": round(float(fees.max()), 0),
                }
            )
    pricing.sort(key=lambda x: x["avg_fee"], reverse=True)
    results["q78_pricing_benchmark"] = {
        "data": pricing,
        "labels": [p["category"] for p in pricing[:15]],
        "avg_values": [p["avg_fee"] for p in pricing[:15]],
        "median_values": [p["median_fee"] for p in pricing[:15]],
        "title": "Pricing Benchmark by Category (₹)",
        "chart_type": "grouped_bar",
    }
