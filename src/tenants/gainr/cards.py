"""Preserve the Gainr client card and verified-user response contract."""

from __future__ import annotations

from typing import Any

from tenants.gainr.models import (
    GAINR_CARD_INTEGER_FIELDS,
    GAINR_USER_FIELDS,
    GAINR_USER_INTEGER_FIELDS,
)


def _integer(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _number(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def _user_payload(
    raw_user: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not raw_user:
        return None
    user = {}
    for field in GAINR_USER_FIELDS:
        value = raw_user.get(field)
        if isinstance(value, str) and value.strip().upper() == "NULL":
            value = None
        if field in GAINR_USER_INTEGER_FIELDS:
            value = _integer(value)
        elif field == "available_credit":
            value = _number(value)
        user[field] = value
    return user


def _card(row: dict[str, Any]) -> dict[str, Any]:
    card = {
        field: row.get(field)
        for field in (
            "id",
            "type",
            "user_id",
            "category_type",
            "parent_id",
            "slug",
            "category_id",
            "title",
            "rental_duration",
            "rental_fee",
            "is_rent_negotiable",
            "city_id",
            "locality_id",
            "description",
            "photos",
            "total_favorite",
            "total_like",
            "status",
            "service_ad_count",
            "users_rating_count",
            "rating_avg",
            "boost_ad_count",
            "is_aadhar_gst_verified_count",
        )
    }
    for field in GAINR_CARD_INTEGER_FIELDS:
        card[field] = _integer(card.get(field))
    for field in ("rental_fee", "rating_avg"):
        card[field] = _number(card.get(field))
    card["service_ad_count"] = card.get("service_ad_count") or 0
    card["boost_ad_count"] = card.get("boost_ad_count") or 0
    card["is_aadhar_gst_verified_count"] = card.get("is_aadhar_gst_verified_count") or 0
    city_id = card.get("city_id")
    locality_id = card.get("locality_id")
    city_name = row.get("__city_name")
    locality_name = row.get("__locality_name")
    attributes = [
        {
            "ads_id": _integer(attribute.get("ads_id")),
            "attribute_id": _integer(attribute.get("attribute_id")),
            "value": _integer(attribute.get("value")),
        }
        for attribute in row.get("__ads_attributes", [])
    ]
    verified_user = _user_payload(row.get("__user"))
    compact_user = None
    is_verified = False
    if verified_user is not None:
        is_verified = _integer(verified_user.get("is_aadhaar_gst_verified")) == 1
        compact_user = {
            "prosper_id": verified_user.get("prosper_id"),
            "id": verified_user.get("id"),
            "is_aadhaar_gst_verified": (verified_user.get("is_aadhaar_gst_verified")),
        }
        card["is_aadhar_gst_verified_count"] = 1 if is_verified else 0
    card.update(
        {
            "ads_attributes": attributes,
            "city": (
                {"id": city_id, "city": city_name}
                if city_id is not None and city_name
                else None
            ),
            "locality": (
                {"id": locality_id, "area": locality_name}
                if locality_id is not None and locality_name
                else None
            ),
            "favorites": None,
            "ads_likes": None,
            "user": compact_user,
            "boost_ad": None,
            "is_aadhar_gst_verified": (verified_user if is_verified else None),
        }
    )
    return card
