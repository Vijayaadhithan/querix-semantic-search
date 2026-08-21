from search.policy import SearchPolicy
from tenants.registry import plugin_for_search_policy, tenant_plugins


def supported_search_policies() -> tuple[str, ...]:
    return tuple(
        sorted(
            policy_name
            for plugin in tenant_plugins().values()
            for policy_name in plugin.search_policies
        )
    )


def build_search_policy(name: str) -> SearchPolicy:
    normalized = name.strip().casefold()
    plugin = plugin_for_search_policy(normalized)
    if plugin is None:
        supported = ", ".join(supported_search_policies())
        raise ValueError(
            f"Unsupported search policy {name!r}; expected one of: {supported}"
        )
    return plugin.search_policies[normalized]()
