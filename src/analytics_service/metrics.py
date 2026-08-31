"""Company-neutral metric profile validation and selection helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def validate_metric_profile(
    raw_profile: Any,
    *,
    audience: str,
    available_metrics: Mapping[str, tuple[str, ...]],
    allowed_modules: frozenset[str],
) -> dict[str, tuple[str, ...]]:
    if raw_profile is None:
        return {}
    if not isinstance(raw_profile, Mapping):
        raise ValueError(f"Analytics {audience} metric profile must be an object")
    normalized: dict[str, tuple[str, ...]] = {}
    for module, raw_names in raw_profile.items():
        module_name = str(module).strip()
        if module_name not in allowed_modules:
            raise ValueError(
                f"Analytics {audience} metric profile has unsupported "
                f"module {module_name!r}"
            )
        if isinstance(raw_names, (str, bytes)) or not isinstance(
            raw_names,
            (list, tuple),
        ):
            raise ValueError(f"Analytics metric module {module_name!r} must be a list")
        names = tuple(str(name).strip() for name in raw_names)
        if any(not name for name in names):
            raise ValueError(
                f"Analytics metric module {module_name!r} has an empty name"
            )
        if len(names) != len(set(names)):
            raise ValueError(f"Analytics metric module {module_name!r} has duplicates")
        unknown = [name for name in names if name not in available_metrics[module_name]]
        if unknown:
            raise ValueError(
                f"Analytics metric module {module_name!r} has unsupported "
                f"metrics: {', '.join(unknown)}"
            )
        normalized[module_name] = names
    return normalized


def resolve_metric_profiles(
    company_overrides: Mapping[str, tuple[str, ...]],
    internal_overrides: Mapping[str, tuple[str, ...]],
    *,
    default_company: Mapping[str, tuple[str, ...]],
    default_internal: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    return (
        {**default_company, **company_overrides},
        {**default_internal, **internal_overrides},
    )


def select_metrics(
    report: Mapping[str, Any],
    metric_names: tuple[str, ...],
) -> dict[str, Any]:
    missing = [name for name in metric_names if name not in report]
    if missing:
        raise KeyError(
            "Analytics report is missing curated metrics: " + ", ".join(missing)
        )
    return {name: report[name] for name in metric_names}


def metric_counts(profile: Mapping[str, tuple[str, ...]]) -> dict[str, int]:
    return {module: len(names) for module, names in profile.items()}
