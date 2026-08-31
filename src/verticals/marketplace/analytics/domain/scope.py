"""Reusable marketplace scope predicates supplied by each tenant."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True, slots=True)
class MarketplaceScope:
    active_ad_statuses: frozenset[str]
    active_user_statuses: frozenset[str]
    status_column: str = "status"
    deleted_at_column: str = "deleted_at"

    def active_rows(
        self,
        frame: pd.DataFrame,
        *,
        statuses: frozenset[str],
    ) -> pd.DataFrame:
        active = frame[frame[self.deleted_at_column].isna()].copy()
        normalized = active[self.status_column].fillna("").astype(str).str.strip()
        return active[normalized.isin(statuses)].copy()

    def active_ads(self, frame: pd.DataFrame) -> pd.DataFrame:
        return self.active_rows(frame, statuses=self.active_ad_statuses)

    def active_users(self, frame: pd.DataFrame) -> pd.DataFrame:
        return self.active_rows(frame, statuses=self.active_user_statuses)
