"""Tenant-registered normalized dataset contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    filename: str
    required_columns: tuple[str, ...]
    usecols: tuple[str, ...] | None = None
    dtypes: dict[str, str] | None = None
    numeric_columns: tuple[str, ...] = ()
    database: str = "company"
    timestamps_are_utc: bool = False
    history_window: bool = False
    lightweight_columns: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.database not in {"company", "telemetry"}:
            raise ValueError("Analytics dataset database must be company or telemetry")
        available = set(self.usecols or self.required_columns)
        for column, projection in self.lightweight_columns.items():
            if column not in available or projection not in {"presence", "length"}:
                raise ValueError("Invalid analytics lightweight column projection")


class DatasetContractError(ValueError):
    """Raised when an analytics input violates its tenant-owned contract."""
