"""Listing metrics."""

import logging

import pandas as pd

LOGGER = logging.getLogger(__name__)


def listing_completeness_metrics(
    results, ads, ads_attrs, attr_vals, attrs, total_ads
) -> None:
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
    description = ads["description"]
    desc_lengths = (
        description.fillna(0)
        if pd.api.types.is_numeric_dtype(description)
        else description.fillna("").apply(len)
    )
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
    scoped_attrs = ads_attrs[ads_attrs["ads_id"].isin(set(ads["id"]))]
    attr_usage = (
        scoped_attrs.groupby("attribute_id").size().sort_values(ascending=False)
    )
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
    top_val_ids = scoped_attrs["value"].value_counts().head(20)
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
