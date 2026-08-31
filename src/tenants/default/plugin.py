from tenants.plugin import AnalyticsAdapterRegistration, TenantPlugin


def _build_default_policy():
    from search.policy import DEFAULT_SEARCH_POLICY

    return DEFAULT_SEARCH_POLICY


def _build_default_analytics(company):
    from analytics_service.adapters_base import PassthroughCompanyAnalyticsAdapter

    return PassthroughCompanyAnalyticsAdapter(
        company_id=company.company_id,
        plugin_name="default",
    )


def _default_analytics_contract():
    from analytics_service.adapters_base import EMPTY_ANALYTICS_CONTRACT

    return EMPTY_ANALYTICS_CONTRACT


PLUGIN = TenantPlugin(
    name="default",
    search_policies={"default": _build_default_policy},
    analytics_adapters={
        "default": AnalyticsAdapterRegistration(
            factory=_build_default_analytics,
            contract_factory=_default_analytics_contract,
        )
    },
)
