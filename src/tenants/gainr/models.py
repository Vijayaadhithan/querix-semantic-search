from typing import Any

from pydantic import (
    AliasChoices,
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
    "is_aadhaar_gst_verified",
)
GAINR_USER_INTEGER_FIELDS = (
    "id",
    "is_aadhaar_gst_verified",
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
    # These names are part of Gainr's existing web/mobile request contract.
    # They deliberately remain confined to the Gainr compatibility adapter.
    category_id: int | str | None = ""
    subcategory_id: int | str | None = ""
    category_type: int | str | None = ""
    locality_id: list[int] = Field(default_factory=list)
    rental_duration: list[str] = Field(default_factory=list)
    ad_type: list[int] = Field(default_factory=list)
    fee: list[int] = Field(default_factory=list)
    attribute_value: list[int | str] = Field(default_factory=list)
    sort_by: int | str | None = ""
    min_fee: float | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("min_fee", "fee_min"),
    )
    max_fee: float | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("max_fee", "fee_max"),
    )

    @field_validator(
        "category_id",
        "subcategory_id",
        "category_type",
        "sort_by",
        mode="before",
    )
    @classmethod
    def normalize_optional_numeric_id(cls, value):
        if value in (None, ""):
            return ""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return ""
            if value.isdigit():
                return int(value)
            raise ValueError("filter IDs must be numeric or an empty string")
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

    @field_validator("attribute_value", mode="before")
    @classmethod
    def normalize_attribute_values(cls, values):
        if values in (None, ""):
            return []
        if not isinstance(values, list):
            raise ValueError("attribute_value must be a list")
        normalized = []
        for value in values:
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
                if value.isdigit():
                    value = int(value)
            normalized.append(value)
        return _unique(normalized)

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
