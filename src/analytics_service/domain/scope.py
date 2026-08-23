"""Shared Gainr marketplace scope rules for company-facing analytics."""

from __future__ import annotations

import pandas as pd

ACTIVE_STATUS_CODES = frozenset({"1", "8"})


def active_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Return non-deleted rows in Gainr's configured active statuses."""
    active = frame[frame["deleted_at"].isna()].copy()
    statuses = active["status"].fillna("").astype(str).str.strip()
    return active[statuses.isin(ACTIVE_STATUS_CODES)].copy()


def active_ads(frame: pd.DataFrame) -> pd.DataFrame:
    return active_rows(frame)


def active_users(frame: pd.DataFrame) -> pd.DataFrame:
    return active_rows(frame)
