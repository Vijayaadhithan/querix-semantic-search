from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

DURATION_ORDER = {
    value: index
    for index, value in enumerate(
        ("Per Hour", "Per Day", "Per Week", "Per Month", "Per Ride")
    )
}
GAINR_CARD_INTEGER_FIELDS = (
    "id",
    "type",
    "user_id",
    "category_type",
    "parent_id",
    "category_id",
    "is_rent_negotiable",
    "city_id",
    "locality_id",
    "total_favorite",
    "total_like",
    "status",
    "service_ad_count",
    "boost_ad_count",
    "is_aadhar_gst_verified_count",
)
GAINR_USER_FIELDS = (
    "id",
    "prosper_id",
    "name",
    "photo",
    "email",
    "available_credit",
    "provider",
    "provider_id",
    "phone",
    "state_id",
    "city_id",
    "gender",
    "location",
    "availability",
    "role",
    "email_verified_at",
    "mobile_verified_at",
    "privacy_enabled",
    "fwd_otp",
    "fwd_is_verified",
    "valid_till",
    "is_verified",
    "gst",
    "aadhar",
    "platform",
    "fcm_token",
    "device_platform",
    "created_at",
    "updated_at",
    "status",
    "prosper_page_view_count",
    "edit_photo",
    "profile_communication",
    "contact_view_count",
    "reg_geo_city",
    "reg_geo_latitude",
    "reg_geo_longitude",
    "reg_device_details",
    "last_geo_city",
    "last_geo_latitude",
    "last_geo_longitude",
    "last_device_details",
    "user_type",
    "deleted_at",
    "is_aadhaar_gst_verified",
    "upi_id",
    "survey_language_id",
    "is_survey_personal_completed",
    "delete_user_remark",
    "trip_flag",
    "contact_view_plan_id",
    "contact_views_count",
    "free_contact_start_date",
    "contact_plan_start_date",
)
GAINR_USER_INTEGER_FIELDS = (
    "id",
    "phone",
    "state_id",
    "city_id",
    "gender",
    "privacy_enabled",
    "fwd_is_verified",
    "is_verified",
    "platform",
    "device_platform",
    "status",
    "prosper_page_view_count",
    "contact_view_count",
    "user_type",
    "is_aadhaar_gst_verified",
    "survey_language_id",
    "is_survey_personal_completed",
    "trip_flag",
    "contact_view_plan_id",
    "contact_views_count",
)


def _unique(values: list[Any]) -> list[Any]:
    return list(dict.fromkeys(values))


class GainrSuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str = Field(min_length=1, max_length=250)

    @field_validator("term")
    @classmethod
    def normalize_term(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("term must not be blank")
        return value


class GainrFilterDataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city_id: int = Field(gt=0)


class GainrSearchFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city_id: int | None = Field(default=None, gt=0)
    subcategory_id: int | str | None = ""
    locality_id: list[int] = Field(default_factory=list)
    rental_duration: list[str] = Field(default_factory=list)
    ad_type: list[int] = Field(default_factory=list)
    fee: list[int] = Field(default_factory=list)
    min_fee: float | None = Field(default=None, ge=0)
    max_fee: float | None = Field(default=None, ge=0)

    @field_validator("subcategory_id", mode="before")
    @classmethod
    def normalize_subcategory(cls, value):
        if value in (None, ""):
            return ""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return ""
            if value.isdigit():
                return int(value)
            raise ValueError("subcategory_id must be a numeric ID or an empty string")
        return value

    @field_validator("locality_id")
    @classmethod
    def validate_localities(cls, values: list[int]) -> list[int]:
        if any(value <= 0 for value in values):
            raise ValueError("locality_id values must be positive")
        return _unique(values)

    @field_validator("rental_duration")
    @classmethod
    def normalize_durations(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()) for value in values]
        if any(not value for value in normalized):
            raise ValueError("rental_duration values must not be blank")
        return _unique(normalized)

    @field_validator("ad_type")
    @classmethod
    def validate_ad_types(cls, values: list[int]) -> list[int]:
        values = _unique(values)
        if any(value not in {1, 2} for value in values):
            raise ValueError("ad_type supports only 1 (offer) and 2 (need)")
        return values

    @field_validator("fee")
    @classmethod
    def validate_fee_types(cls, values: list[int]) -> list[int]:
        return _unique(values)

    @model_validator(mode="after")
    def validate_fee_range(self):
        if (
            self.min_fee is not None
            and self.max_fee is not None
            and self.min_fee > self.max_fee
        ):
            raise ValueError("min_fee must not be greater than max_fee")
        return self


class GainrFilterResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    searchTerm: str = Field(default="", max_length=1000)
    filter: GainrSearchFilter = Field(default_factory=GainrSearchFilter)
    page: int = Field(default=1, ge=1)

    @field_validator("searchTerm")
    @classmethod
    def normalize_search_term(cls, value: str) -> str:
        return " ".join(value.split())
