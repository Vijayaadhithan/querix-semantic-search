from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from analytics_service.adapters_base import PassthroughCompanyAnalyticsAdapter
from analytics_service.config import load_company_analytics_config
from analytics_service.contracts import AnalyticsComputation, AnalyticsContract
from analytics_service.service import AnalyticsRefreshService
from analytics_service.source import SqlAnalyticsDataSource
from analytics_service.source_schema import DatasetSpec
from analytics_service.store import AnalyticsSnapshotStore
from tenants.plugin import AnalyticsAdapterRegistration, TenantPlugin

NEUTRAL_CONTRACT = AnalyticsContract(
    dataset_specs={
        "inventory": DatasetSpec(
            "inventory.csv",
            ("item_id", "state", "deleted_at"),
            usecols=("item_id", "state", "deleted_at"),
        )
    },
    default_tables={"inventory": "inventory_items"},
    available_metrics={"inventory": ("active_items",)},
    company_modules=frozenset({"inventory"}),
    internal_modules=frozenset(),
    default_company_metric_profile={"inventory": ("active_items",)},
)


@dataclass(frozen=True, slots=True)
class NeutralInventoryAdapter(PassthroughCompanyAnalyticsAdapter):
    def build_computation(
        self,
        data: dict[str, pd.DataFrame],
        modules: frozenset[str],
    ) -> AnalyticsComputation:
        assert modules == frozenset({"inventory"})
        inventory = data["inventory"]
        active = inventory[
            inventory["state"].eq("published") & inventory["deleted_at"].isna()
        ]
        count = int(len(active))
        return AnalyticsComputation(
            reports={
                "inventory": {
                    "active_items": {
                        "title": "Active inventory",
                        "value": count,
                    }
                }
            },
            query_pairs=[],
            query_record_count=0,
            company_overview={"active_items": count},
        )

    def metric_definitions(
        self,
        reports: dict[str, dict[str, Any]],
        profile: dict[str, tuple[str, ...]],
        *,
        audience: str,
        source_rows: dict[str, int],
    ) -> dict[str, dict[str, Any]]:
        del reports, profile, audience, source_rows
        return {
            "active_items": {
                "question": "How many inventory items are published?",
                "sources": ["inventory"],
                "available": True,
            }
        }


class NeutralSource:
    def load(self, company):
        assert tuple(company.dataset_specs) == ("inventory",)
        return {
            "inventory": pd.DataFrame(
                [
                    {"item_id": 1, "state": "published", "deleted_at": None},
                    {"item_id": 2, "state": "draft", "deleted_at": None},
                    {
                        "item_id": 3,
                        "state": "published",
                        "deleted_at": "2026-08-01 00:00:00",
                    },
                ]
            )
        }


def _neutral_adapter(company):
    return NeutralInventoryAdapter(
        company_id=company.company_id,
        plugin_name="neutral_inventory",
        analytics_contract=NEUTRAL_CONTRACT,
    )


def test_second_tenant_owns_schema_status_scope_and_metric_builder(
    tmp_path: Path,
    monkeypatch,
):
    plugin = TenantPlugin(
        name="neutral",
        analytics_adapters={
            "neutral_inventory": AnalyticsAdapterRegistration(
                factory=_neutral_adapter,
                contract_factory=lambda: NEUTRAL_CONTRACT,
            )
        },
    )
    monkeypatch.setattr(
        "analytics_service.adapters.tenant_plugins",
        lambda: {"neutral": plugin},
    )
    for name, value in {
        "NEUTRAL_HOST": "database.test",
        "NEUTRAL_PORT": "3306",
        "NEUTRAL_DATABASE": "neutral",
        "NEUTRAL_USER": "readonly",
        "NEUTRAL_PASSWORD": "test-placeholder",
    }.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / "neutral.yaml"
    path.write_text(
        """
company:
  id: neutral
database:
  backend: mysql
  host_env: NEUTRAL_HOST
  port_env: NEUTRAL_PORT
  database_env: NEUTRAL_DATABASE
  user_env: NEUTRAL_USER
  password_env: NEUTRAL_PASSWORD
  tls:
    mode: disable
analytics:
  enabled: true
  adapter: neutral_inventory
  tables:
    inventory: company_inventory
  columns:
    inventory:
      item_id: listing_id
      state: lifecycle_state
      deleted_at: removed_at
""",
        encoding="utf-8",
    )

    company = load_company_analytics_config(path)
    assert company is not None
    assert tuple(company.datasets) == ("inventory",)
    sql = SqlAnalyticsDataSource._select_sql(
        company.database,
        company.datasets["inventory"],
        company.dataset_specs["inventory"],
    )
    assert "`listing_id` AS `item_id`" in sql
    assert "`lifecycle_state` AS `state`" in sql
    assert "FROM `company_inventory`" in sql

    store = AnalyticsSnapshotStore(tmp_path / "snapshots.sqlite3")
    result = AnalyticsRefreshService(NeutralSource(), store).refresh(company)
    dashboard = store.dashboard("neutral", internal=False)

    assert result["source_rows"] == {"inventory": 3}
    assert dashboard is not None
    assert dashboard["business_overview"] == {"active_items": 1}
    assert dashboard["inventory"]["active_items"]["value"] == 1
    assert dashboard["metadata"]["modules"] == [
        "individual_queries",
        "inventory",
    ]
    assert set(dashboard) == {
        "metadata",
        "snapshot",
        "business_overview",
        "inventory",
    }
