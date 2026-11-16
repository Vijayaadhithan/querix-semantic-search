"""User metrics."""

import logging
from collections import Counter

import pandas as pd

LOGGER = logging.getLogger(__name__)


def user_population_metrics(results, ads, states, total_users, users) -> None:
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
