from tenants.plugin import TenantPlugin


def _build_default_policy():
    from search.policy import DEFAULT_SEARCH_POLICY

    return DEFAULT_SEARCH_POLICY


def _build_default_analytics(company):
    from analytics_service.adapters_base import PassthroughCompanyAnalyticsAdapter

    return PassthroughCompanyAnalyticsAdapter(
        company_id=company.company_id,
        plugin_name="default",
    )


PLUGIN = TenantPlugin(
    name="default",
    search_policies={"default": _build_default_policy},
    analytics_adapters={"default": _build_default_analytics},
)
