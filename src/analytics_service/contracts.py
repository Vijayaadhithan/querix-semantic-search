"""Company-neutral contracts for tenant-owned analytics computation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .source_schema import DatasetSpec

MetricProfile = Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class AnalyticsContract:
    """Static schema and metric capabilities registered by one adapter."""

    dataset_specs: Mapping[str, DatasetSpec]
    default_tables: Mapping[str, str]
    available_metrics: Mapping[str, tuple[str, ...]]
    company_modules: frozenset[str]
    internal_modules: frozenset[str]
    default_company_metric_profile: MetricProfile = field(default_factory=dict)
    default_internal_metric_profile: MetricProfile = field(default_factory=dict)

    def __post_init__(self) -> None:
        if set(self.dataset_specs) != set(self.default_tables):
            raise ValueError(
                "Analytics contract dataset specs and default tables must match"
            )
        supported_modules = self.company_modules | self.internal_modules
        if not supported_modules.issubset(self.available_metrics):
            raise ValueError("Analytics contract modules must have metric catalogues")
        for audience, profile, allowed in (
            (
                "company",
                self.default_company_metric_profile,
                self.company_modules,
            ),
            (
                "internal",
                self.default_internal_metric_profile,
                self.internal_modules,
            ),
        ):
            unsupported = set(profile) - allowed
            if unsupported:
                raise ValueError(
                    f"Analytics {audience} defaults contain unsupported modules"
                )
            for module, names in profile.items():
                unknown = set(names) - set(self.available_metrics[module])
                if unknown:
                    raise ValueError(
                        f"Analytics {audience} defaults contain unknown metrics"
                    )


@dataclass(slots=True)
class AnalyticsComputation:
    """Tenant-built payload consumed by shared snapshot publication plumbing."""

    reports: dict[str, dict[str, Any]]
    query_pairs: list[tuple[dict[str, Any], dict[str, Any]]]
    company_overview: dict[str, Any]
