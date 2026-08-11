import logging
from collections import Counter, defaultdict

import pandas as pd

LOGGER = logging.getLogger(__name__)


def process_part_c(data):
    LOGGER.info("Processing Part C: Cross-CSV Deep Analytics")
    results = {}

    ads = data["ads"]
    users = data["users"]
    cats = data["categories"]
    subcats = data["sub_categories"]
    states = data["states"]
    location = data["location"]
    attrs = data["attributes"]
    attr_vals = data["attribute_values"]
    ads_attrs = data["ads_attributes"]

    total_ads = len(ads)
    total_users = len(users)

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

    # Q51: User growth trend
    LOGGER.debug("Processing user growth")
    users["created_at_parsed"] = pd.to_datetime(users["created_at"], errors="coerce")
    users["reg_month"] = users["created_at_parsed"].dt.to_period("M")
    monthly_users = users.groupby("reg_month").size()
    # Take the last 36 months for readability.
    monthly_users = monthly_users.tail(36)
    results["q51_user_growth"] = {
        "labels": [str(m) for m in monthly_users.index],
        "values": [int(v) for v in monthly_users.values],
        "total_users": total_users,
        "title": "Monthly User Registration Trend",
        "chart_type": "line",
    }

    # Q52: State-wise user distribution
    state_names = states.set_index("id")["name"].to_dict()
    users["state_id"] = pd.to_numeric(users["state_id"], errors="coerce")
    users_by_state = users.groupby("state_id").size()
    state_dist = {}
    for sid, count in users_by_state.items():
        name = state_names.get(int(sid) if not pd.isna(sid) else -1, "Unknown")
        state_dist[name] = int(count)
    state_dist = dict(sorted(state_dist.items(), key=lambda x: x[1], reverse=True))
    results["q52_users_by_state"] = {
        "labels": list(state_dist.keys())[:15],
        "values": list(state_dist.values())[:15],
        "title": "User Distribution by State (Top 15)",
        "chart_type": "bar",
    }

    # Q53: Gender split
    gender_map = {1: "Male", 2: "Female", "1": "Male", "2": "Female"}
    users["gender_label"] = users["gender"].map(gender_map).fillna("Not Specified")
    gender_dist = users["gender_label"].value_counts().to_dict()
    results["q53_gender_split"] = {
        "labels": list(gender_dist.keys()),
        "values": [int(v) for v in gender_dist.values()],
        "title": "User Gender Distribution",
        "chart_type": "doughnut",
    }

    # Q54: Platform distribution
    platform_dist = users["platform"].value_counts().head(10).to_dict()
    # Map numeric to names
    platform_map = {
        "1": "iOS",
        "2": "Android",
        "3": "Web",
        1: "iOS",
        2: "Android",
        3: "Web",
    }
    platform_labeled = {}
    for k, v in platform_dist.items():
        label = platform_map.get(k, str(k))
        platform_labeled[label] = int(v)
    results["q54_platform"] = {
        "labels": list(platform_labeled.keys()),
        "values": list(platform_labeled.values()),
        "title": "User Registration Platform",
        "chart_type": "doughnut",
    }

    # Q55: Ads per user distribution
    LOGGER.debug("Processing ads-per-user")
    ads_per_user = ads.groupby("user_id").size()
    apu_dist = Counter()
    for count in ads_per_user:
        if count == 1:
            apu_dist["1 ad"] += 1
        elif count <= 3:
            apu_dist["2-3 ads"] += 1
        elif count <= 5:
            apu_dist["4-5 ads"] += 1
        elif count <= 10:
            apu_dist["6-10 ads"] += 1
        elif count <= 20:
            apu_dist["11-20 ads"] += 1
        else:
            apu_dist["20+ ads"] += 1
    results["q55_ads_per_user"] = {
        "labels": ["1 ad", "2-3 ads", "4-5 ads", "6-10 ads", "11-20 ads", "20+ ads"],
        "values": [
            apu_dist.get(k, 0)
            for k in ["1 ad", "2-3 ads", "4-5 ads", "6-10 ads", "11-20 ads", "20+ ads"]
        ],
        "avg_ads_per_user": round(float(ads_per_user.mean()), 2),
        "max_ads": int(ads_per_user.max()),
        "total_sellers": int(len(ads_per_user)),
        "title": "Ads per User Distribution (Seller Segmentation)",
        "chart_type": "bar",
    }

    # Q56: Verified vs unverified users
    verified_count = int(
        (users["is_verified"] == 1).sum() + (users["is_verified"] == "1").sum()
    )
    results["q56_verification"] = {
        "labels": ["Verified", "Not Verified"],
        "values": [verified_count, total_users - verified_count],
        "percentage": round(verified_count / total_users * 100, 1),
        "title": "User Verification Status",
        "chart_type": "doughnut",
    }

    # Q57: User retention (simplified - last activity)
    users["updated_at_parsed"] = pd.to_datetime(users["updated_at"], errors="coerce")
    users["days_since_last_activity"] = (
        pd.Timestamp.now() - users["updated_at_parsed"]
    ).dt.days
    retention_dist = Counter()
    for days in users["days_since_last_activity"].dropna():
        if days <= 30:
            retention_dist["Active (≤30 days)"] += 1
        elif days <= 90:
            retention_dist["Recent (31-90 days)"] += 1
        elif days <= 180:
            retention_dist["Dormant (91-180 days)"] += 1
        elif days <= 365:
            retention_dist["Inactive (181-365 days)"] += 1
        else:
            retention_dist["Churned (>365 days)"] += 1
    results["q57_retention"] = {
        "labels": [
            "Active (≤30 days)",
            "Recent (31-90 days)",
            "Dormant (91-180 days)",
            "Inactive (181-365 days)",
            "Churned (>365 days)",
        ],
        "values": [
            retention_dist.get(k, 0)
            for k in [
                "Active (≤30 days)",
                "Recent (31-90 days)",
                "Dormant (91-180 days)",
                "Inactive (181-365 days)",
                "Churned (>365 days)",
            ]
        ],
        "title": "User Retention / Activity Distribution",
        "chart_type": "doughnut",
    }

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
    status_dist = ads["status"].value_counts().to_dict()
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

    # Q66: Revenue potential per city
    city_revenue = []
    for cid, count in ads_by_city.head(20).items():
        info = city_state.get(cid, {})
        price = float(info.get("price", 0))
        city_revenue.append(
            {
                "city": info.get("city", f"ID:{cid}"),
                "ads": int(count),
                "price_tier": price,
                "revenue_potential": round(int(count) * price, 0),
            }
        )
    city_revenue.sort(key=lambda x: x["revenue_potential"], reverse=True)
    results["q66_revenue_by_city"] = {
        "data": city_revenue[:15],
        "labels": [c["city"] for c in city_revenue[:15]],
        "values": [c["revenue_potential"] for c in city_revenue[:15]],
        "title": "Revenue Potential by City (₹)",
        "chart_type": "bar",
    }

    # Q67: High pricing, low volume cities
    high_price_low_vol = []
    for cid in location["id"]:
        info = city_state.get(cid, {})
        price = float(info.get("price", 0))
        ad_count = int(ads_by_city.get(cid, 0))
        if price >= 75 and ad_count < 1000:
            high_price_low_vol.append(
                {
                    "city": info.get("city", f"ID:{cid}"),
                    "price_tier": price,
                    "ad_count": ad_count,
                }
            )
    high_price_low_vol.sort(key=lambda x: x["price_tier"], reverse=True)
    results["q67_undermonetized"] = {
        "data": high_price_low_vol[:20],
        "count": len(high_price_low_vol),
        "title": "High-Price Tier, Low-Volume Cities (Undermonetized)",
        "chart_type": "table",
    }

    # Q68: Premium ads by category
    premium_ads = ads[ads["premium_start_date"].notna()]
    if len(premium_ads) > 0:
        premium_by_cat = premium_ads.groupby("parent_cat_id").size()
        prem_cat = {}
        for cid, count in premium_by_cat.items():
            name = cat_names.get(cid, f"Unknown ({cid})")
            prem_cat[name] = int(count)
        prem_cat = dict(sorted(prem_cat.items(), key=lambda x: x[1], reverse=True))
        results["q68_premium_by_category"] = {
            "labels": list(prem_cat.keys())[:15],
            "values": list(prem_cat.values())[:15],
            "title": "Premium Ads by Category",
            "chart_type": "bar",
        }
    else:
        results["q68_premium_by_category"] = {
            "labels": [],
            "values": [],
            "title": "Premium Ads by Category",
            "chart_type": "bar",
        }

    # Q69: Contact view distribution
    cv = ads["user_contact_view_count"].dropna()
    cv = cv[cv > 0]
    results["q69_contact_views"] = {
        "avg": round(float(cv.mean()), 1) if len(cv) > 0 else 0,
        "median": round(float(cv.median()), 1) if len(cv) > 0 else 0,
        "max": int(cv.max()) if len(cv) > 0 else 0,
        "total": int(cv.sum()) if len(cv) > 0 else 0,
        "ads_with_views": int(len(cv)),
        "title": "Contact View Distribution",
        "chart_type": "stat",
    }

    # Q70: Ads with photos
    has_photos = int(ads["photos"].notna().sum())
    no_photos = total_ads - has_photos
    results["q70_photos"] = {
        "labels": ["With Photos", "Without Photos"],
        "values": [has_photos, no_photos],
        "percentage": round(has_photos / total_ads * 100, 1),
        "title": "Ads with Photos",
        "chart_type": "doughnut",
    }

    # Q71: Description length
    desc_lengths = ads["description"].fillna("").apply(len)
    results["q71_description_length"] = {
        "avg": round(float(desc_lengths.mean()), 0),
        "median": round(float(desc_lengths.median()), 0),
        "max": int(desc_lengths.max()),
        "zero_length": int((desc_lengths == 0).sum()),
        "zero_pct": round((desc_lengths == 0).sum() / total_ads * 100, 1),
        "title": "Ad Description Length Stats",
        "chart_type": "stat",
    }

    # Q72: Ads with keywords
    has_keywords = int(ads["keywords"].notna().sum())
    results["q72_keywords"] = {
        "labels": ["Has Keywords", "No Keywords"],
        "values": [has_keywords, total_ads - has_keywords],
        "percentage": round(has_keywords / total_ads * 100, 1),
        "title": "Ads with Keywords (SEO Readiness)",
        "chart_type": "doughnut",
    }

    # Q73: Most/least filled attributes
    LOGGER.debug("Processing attribute completeness")
    attr_names = attrs.set_index("id")["name"].to_dict()
    attr_usage = ads_attrs.groupby("attribute_id").size().sort_values(ascending=False)
    top_attrs = {}
    for aid, count in attr_usage.head(15).items():
        name = attr_names.get(aid, f"Attr {aid}")
        top_attrs[name] = int(count)
    results["q73_attribute_completeness"] = {
        "labels": list(top_attrs.keys()),
        "values": list(top_attrs.values()),
        "total_attributes": len(attrs),
        "title": "Most Used Attributes",
        "chart_type": "bar",
    }

    # Q74: Most common attribute values
    LOGGER.debug("Processing common attribute values")
    val_names = attr_vals.set_index("id")["value"].to_dict()
    top_val_ids = ads_attrs["value"].value_counts().head(20)
    top_vals = {}
    for vid, count in top_val_ids.items():
        try:
            name = val_names.get(int(vid), str(vid))
        except (TypeError, ValueError, OverflowError):
            name = str(vid)
        top_vals[str(name)[:40]] = int(count)
    results["q74_common_values"] = {
        "labels": list(top_vals.keys()),
        "values": list(top_vals.values()),
        "title": "Most Common Attribute Values",
        "chart_type": "bar",
    }

    LOGGER.info("Part C complete: %d questions processed", len(results))
    return results
