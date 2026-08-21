from __future__ import annotations

import importlib
from functools import lru_cache

from tenants.plugin import TenantPlugin

_BUILTIN_PLUGIN_MODULES = {
    "default": "tenants.default.plugin",
    "gainr": "tenants.gainr.plugin",
}


@lru_cache(maxsize=1)
def tenant_plugins() -> dict[str, TenantPlugin]:
    plugins: dict[str, TenantPlugin] = {}
    for expected_name, module_name in _BUILTIN_PLUGIN_MODULES.items():
        module = importlib.import_module(module_name)
        plugin = getattr(module, "PLUGIN", None)
        if not isinstance(plugin, TenantPlugin):
            raise RuntimeError(f"Tenant plugin module {module_name!r} has no PLUGIN.")
        if plugin.name != expected_name:
            raise RuntimeError(
                f"Tenant plugin module {module_name!r} registered {plugin.name!r}; "
                f"expected {expected_name!r}."
            )
        plugins[plugin.name] = plugin
    return plugins


def supported_tenant_plugins() -> tuple[str, ...]:
    return tuple(sorted(tenant_plugins()))


def get_tenant_plugin(name: str) -> TenantPlugin:
    normalized = name.strip().casefold() or "default"
    try:
        return tenant_plugins()[normalized]
    except KeyError as exc:
        supported = ", ".join(supported_tenant_plugins())
        raise ValueError(
            f"Unsupported tenant plugin {name!r}; expected one of: {supported}"
        ) from exc


def plugin_for_search_policy(name: str) -> TenantPlugin | None:
    normalized = name.strip().casefold()
    matches = [
        plugin
        for plugin in tenant_plugins().values()
        if normalized in plugin.search_policies
    ]
    if len(matches) > 1:
        owners = ", ".join(sorted(plugin.name for plugin in matches))
        raise RuntimeError(
            f"Search policy {normalized!r} is registered by multiple plugins: {owners}"
        )
    return matches[0] if matches else None


def tenant_logger_names() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                logger_name
                for plugin in tenant_plugins().values()
                for logger_name in plugin.logger_names
            }
        )
    )
