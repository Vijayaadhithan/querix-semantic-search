"""Tenant-registered normalized dataset contracts."""

from __future__ import annotations

from dataclasses import dataclass


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

    def __post_init__(self) -> None:
        if self.database not in {"company", "telemetry"}:
            raise ValueError("Analytics dataset database must be company or telemetry")


class DatasetContractError(ValueError):
    """Raised when an analytics input violates its tenant-owned contract."""
