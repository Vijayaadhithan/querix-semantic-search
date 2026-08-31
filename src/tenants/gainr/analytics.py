"""Gainr-owned analytics schemas, scopes, metrics, and computation adapter."""

from analytics_service.contracts import AnalyticsContract
from verticals.marketplace.analytics.adapter import MarketplaceAnalyticsAdapter
from verticals.marketplace.analytics.domain.scope import MarketplaceScope
from verticals.marketplace.analytics.metrics import (
    AVAILABLE_METRICS,
    COMPANY_MODULES,
    DEFAULT_COMPANY_METRIC_PROFILE,
    DEFAULT_INTERNAL_METRIC_PROFILE,
    INTERNAL_MODULES,
)

from .analytics_schema import GAINR_DATASET_SPECS, GAINR_DEFAULT_TABLES

GAINR_ACTIVE_STATUS_CODES = frozenset({"1", "8"})
GAINR_MARKETPLACE_SCOPE = MarketplaceScope(
    active_ad_statuses=GAINR_ACTIVE_STATUS_CODES,
    active_user_statuses=GAINR_ACTIVE_STATUS_CODES,
)
GAINR_ANALYTICS_CONTRACT = AnalyticsContract(
    dataset_specs=GAINR_DATASET_SPECS,
    default_tables=GAINR_DEFAULT_TABLES,
    available_metrics=AVAILABLE_METRICS,
    company_modules=COMPANY_MODULES,
    internal_modules=INTERNAL_MODULES,
    default_company_metric_profile=DEFAULT_COMPANY_METRIC_PROFILE,
    default_internal_metric_profile=DEFAULT_INTERNAL_METRIC_PROFILE,
)


class GainrAnalyticsAdapter(MarketplaceAnalyticsAdapter):
    """Gainr's registered marketplace analytics implementation."""

    analytics_contract = GAINR_ANALYTICS_CONTRACT
    scope = GAINR_MARKETPLACE_SCOPE
    marketplace_name = "Gainr"
