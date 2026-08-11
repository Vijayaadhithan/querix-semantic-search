import logging
from collections import Counter

import pandas as pd

LOGGER = logging.getLogger(__name__)


def process_part_d(data):
    LOGGER.info("Processing Part D: Customer-Facing Market Intelligence")
    results = {}

    ads = data["ads"]
    cats = data["categories"]
    subcats = data["sub_categories"]
    location = data["location"]
    states = data["states"]
    users = data["users"]

    cat_names = cats.set_index("id")["name"].to_dict()
    subcat_to_cat = subcats.set_index("id")["categoryId"].to_dict()
    city_names = location.set_index("id")["city"].to_dict()
    state_names = states.set_index("id")["name"].to_dict()

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

    # Q79: Temporal listing patterns
    LOGGER.debug("Processing temporal patterns")
    ads["created_at_parsed"] = pd.to_datetime(ads["created_at"], errors="coerce")
    ads["listing_month"] = ads["created_at_parsed"].dt.to_period("M")
    monthly_ads = ads.groupby("listing_month").size()
    monthly_ads = monthly_ads.tail(36)
    results["q79_temporal_patterns"] = {
        "labels": [str(m) for m in monthly_ads.index],
        "values": [int(v) for v in monthly_ads.values],
        "title": "Monthly Ad Listing Trend (Last 36 Months)",
        "chart_type": "line",
    }

    # Q80: Monthly active listings
    # Approximate: ads created and not deleted
    active_monthly = (
        ads[ads["deleted_at"].isna()].groupby("listing_month").size().tail(36)
    )
    results["q80_active_listings"] = {
        "labels": [str(m) for m in active_monthly.index],
        "values": [int(v) for v in active_monthly.values],
        "title": "Monthly Active Listings Trend",
        "chart_type": "line",
    }

    # Q81: New user acquisition by state (last 12 months)
    if "created_at_parsed" not in users.columns:
        users["created_at_parsed"] = pd.to_datetime(
            users["created_at"], errors="coerce"
        )
    recent_users = users[
        users["created_at_parsed"] >= pd.Timestamp.now() - pd.Timedelta(days=365)
    ]
    recent_by_state = recent_users.groupby("state_id").size()
    state_growth = {}
    for sid, count in recent_by_state.items():
        try:
            name = state_names.get(int(sid), "Unknown")
        except (TypeError, ValueError, OverflowError):
            name = "Unknown"
        state_growth[name] = int(count)
    state_growth = dict(sorted(state_growth.items(), key=lambda x: x[1], reverse=True))
    results["q81_user_acquisition_by_state"] = {
        "labels": list(state_growth.keys())[:15],
        "values": list(state_growth.values())[:15],
        "title": "New User Acquisition by State (Last 12 Months)",
        "chart_type": "bar",
    }

    # Q82: Engagement score (views + favorites + likes)
    ads["engagement"] = (
        ads["actual_view_count"].fillna(0)
        + ads["total_favorite"].fillna(0) * 5
        + ads["total_like"].fillna(0) * 3
    )
    top_engaged = ads.nlargest(15, "engagement")[
        ["title", "engagement", "actual_view_count", "total_favorite", "total_like"]
    ]
    results["q82_engagement"] = {
        "data": [
            {
                "title": str(r["title"])[:60],
                "score": int(r["engagement"]),
                "views": int(r.get("actual_view_count", 0)),
                "favorites": int(r.get("total_favorite", 0)),
                "likes": int(r.get("total_like", 0)),
            }
            for _, r in top_engaged.iterrows()
        ],
        "avg_engagement": round(float(ads["engagement"].mean()), 1),
        "title": "Top Engaged Ads (Engagement Score = Views + 5×Favorites + 3×Likes)",
        "chart_type": "table",
    }

    # Q83: Registration to first ad time (simplified)
    # Join first ad created_at with user registration
    first_ad = ads.groupby("user_id")["created_at_parsed"].min().reset_index()
    first_ad.columns = ["user_id", "first_ad_date"]
    if "created_at_parsed" not in users.columns:
        users["created_at_parsed"] = pd.to_datetime(
            users["created_at"], errors="coerce"
        )
    merged = first_ad.merge(
        users[["id", "created_at_parsed"]].rename(
            columns={"id": "user_id", "created_at_parsed": "reg_date"}
        ),
        on="user_id",
        how="inner",
    )
    merged["days_to_first_ad"] = (merged["first_ad_date"] - merged["reg_date"]).dt.days
    valid = merged["days_to_first_ad"].dropna()
    valid = valid[(valid >= 0) & (valid < 3650)]
    activation_dist = Counter()
    for d in valid:
        if d == 0:
            activation_dist["Same day"] += 1
        elif d <= 7:
            activation_dist["1-7 days"] += 1
        elif d <= 30:
            activation_dist["8-30 days"] += 1
        elif d <= 90:
            activation_dist["31-90 days"] += 1
        elif d <= 365:
            activation_dist["91-365 days"] += 1
        else:
            activation_dist["365+ days"] += 1
    results["q83_activation"] = {
        "labels": [
            "Same day",
            "1-7 days",
            "8-30 days",
            "31-90 days",
            "91-365 days",
            "365+ days",
        ],
        "values": [
            activation_dist.get(k, 0)
            for k in [
                "Same day",
                "1-7 days",
                "8-30 days",
                "31-90 days",
                "91-365 days",
                "365+ days",
            ]
        ],
        "avg_days": round(float(valid.mean()), 1) if len(valid) > 0 else 0,
        "median_days": round(float(valid.median()), 1) if len(valid) > 0 else 0,
        "title": "Time from Registration to First Ad (User Activation)",
        "chart_type": "bar",
    }

    # Q84: Seller concentration
    ads_per_user = ads.groupby("user_id").size().sort_values(ascending=False)
    total_ad_count = int(ads_per_user.sum())
    top_10_pct_count = int(len(ads_per_user) * 0.1)
    top_10_pct_ads = int(ads_per_user.head(top_10_pct_count).sum())
    top_1_pct_count = max(1, int(len(ads_per_user) * 0.01))
    top_1_pct_ads = int(ads_per_user.head(top_1_pct_count).sum())
    results["q84_seller_concentration"] = {
        "total_sellers": int(len(ads_per_user)),
        "total_ads": total_ad_count,
        "top_10_pct_sellers": top_10_pct_count,
        "top_10_pct_ads": top_10_pct_ads,
        "top_10_pct_share": round(top_10_pct_ads / total_ad_count * 100, 1),
        "top_1_pct_sellers": top_1_pct_count,
        "top_1_pct_ads": top_1_pct_ads,
        "top_1_pct_share": round(top_1_pct_ads / total_ad_count * 100, 1),
        "title": "Seller Concentration Analysis",
        "chart_type": "stat",
    }

    LOGGER.info("Part D complete: %d questions processed", len(results))
    return results
