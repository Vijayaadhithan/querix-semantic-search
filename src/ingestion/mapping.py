from __future__ import annotations

from typing import Any

from core.tenant_config import TenantIngestionConfig


def canonicalize_search_ready_row(
    row: dict[str, Any],
    config: TenantIngestionConfig,
) -> dict[str, Any]:
    """Project company-owned source fields into the shared retrieval contract."""
    canonical = dict(row)
    for target, source in config.field_mapping.items():
        if source in row:
            canonical[target] = row[source]
    for target, value in config.field_defaults.items():
        if canonical.get(target) is None:
            canonical[target] = value
    return canonical
