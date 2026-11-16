"""Pricing metrics."""

import logging

LOGGER = logging.getLogger(__name__)


def city_pricing_metrics(
    results, ads, ads_by_city, cat_names, city_state, location
) -> None:
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
