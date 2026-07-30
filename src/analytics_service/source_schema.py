"""CSV contracts used by the data access layer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    filename: str
    required_columns: tuple[str, ...]
    usecols: tuple[str, ...] | None = None
    dtypes: dict[str, str] | None = None


ADS_COLUMNS = (
    "id",
    "user_id",
    "category_type",
    "parent_id",
    "category_id",
    "title",
    "rental_duration",
    "rental_fee",
    "is_rent_negotiable",
    "city_id",
    "locality_id",
    "photos",
    "total_favorite",
    "total_like",
    "user_contact_view_count",
    "user_viewed_count",
    "actual_view_count",
    "status",
    "keywords",
    "top_start_date",
    "top_end_date",
    "premium_start_date",
    "premium_end_date",
    "created_at",
    "deleted_at",
    "description",
)

USER_COLUMNS = (
    "id",
    "state_id",
    "city_id",
    "gender",
    "platform",
    "device_platform",
    "role",
    "is_verified",
    "status",
    "created_at",
    "updated_at",
    "user_type",
    "deleted_at",
)

DATASET_SPECS: dict[str, DatasetSpec] = {
    "search_history": DatasetSpec(
        "semantic_search_history.csv",
        ("id", "request_id", "query_text", "created_at"),
        usecols=("id", "request_id", "query_text", "created_at"),
    ),
    "api_usage": DatasetSpec(
        "semantic_search_api_usage.csv",
        (
            "id",
            "request_id",
            "execution_path",
            "result_count",
            "total_results",
            "status",
            "duration_ms",
            "attempts_json",
            "created_at",
        ),
        usecols=(
            "id",
            "request_id",
            "company_id",
            "execution_path",
            "result_count",
            "total_results",
            "status",
            "api_call_count",
            "input_tokens",
            "output_tokens",
            "thought_tokens",
            "total_tokens",
            "duration_ms",
            "attempts_json",
            "created_at",
        ),
    ),
    "categories": DatasetSpec(
        "categories.csv",
        ("id", "name", "cat_group"),
        usecols=("id", "name", "cat_group"),
    ),
    "sub_categories": DatasetSpec(
        "sub_categories.csv",
        ("id", "categoryId", "name"),
        usecols=("id", "categoryId", "name"),
    ),
    "states": DatasetSpec(
        "states.csv",
        ("id", "name"),
        usecols=("id", "name"),
    ),
    "location": DatasetSpec(
        "location.csv",
        ("id", "city", "state_id", "price"),
        usecols=("id", "city", "state_id", "price"),
    ),
    "attributes": DatasetSpec(
        "attributes.csv",
        ("id", "name"),
        usecols=("id", "name"),
    ),
    "attribute_values": DatasetSpec(
        "attribute_values.csv",
        ("id", "attributeId", "value"),
        usecols=("id", "attributeId", "value"),
    ),
    "ads_attributes": DatasetSpec(
        "ads_attributes.csv",
        ("ads_id", "attribute_id", "value"),
        usecols=("ads_id", "attribute_id", "value"),
    ),
    "ads": DatasetSpec(
        "ads.csv",
        ("id", "user_id", "category_id", "title", "created_at"),
        usecols=ADS_COLUMNS,
        dtypes={"rental_fee": "float64", "status": "str"},
    ),
    "users": DatasetSpec(
        "users.csv",
        ("id", "state_id", "city_id", "created_at"),
        usecols=USER_COLUMNS,
    ),
}


class DatasetContractError(ValueError):
    """Raised when an input CSV is missing or violates its column contract."""
