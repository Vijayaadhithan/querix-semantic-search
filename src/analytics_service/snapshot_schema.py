"""Analytics snapshot and refresh-state schema."""

import sqlite3


def initialize_snapshot_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS analytics_snapshots (
            company_id TEXT PRIMARY KEY,
            active_version TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            source_watermark TEXT,
            source_rows_json TEXT NOT NULL,
            company_dashboard_json TEXT NOT NULL,
            internal_dashboard_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS analytics_query_records (
            company_id TEXT NOT NULL,
            snapshot_version TEXT NOT NULL,
            request_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            query_text TEXT NOT NULL,
            outcome TEXT NOT NULL,
            categories_text TEXT NOT NULL,
            language TEXT NOT NULL,
            company_json TEXT NOT NULL,
            internal_json TEXT NOT NULL,
            PRIMARY KEY (
                company_id,
                snapshot_version,
                request_id
            )
        );

        CREATE INDEX IF NOT EXISTS idx_analytics_queries_page
        ON analytics_query_records (
            company_id,
            snapshot_version,
            created_at DESC,
            request_id DESC
        );

        CREATE INDEX IF NOT EXISTS idx_analytics_queries_outcome
        ON analytics_query_records (
            company_id,
            snapshot_version,
            outcome,
            created_at DESC
        );

        CREATE TABLE IF NOT EXISTS analytics_refresh_runs (
            run_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error_type TEXT,
            source_rows_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_analytics_runs_company
        ON analytics_refresh_runs (
            company_id,
            started_at DESC
        );

        CREATE TABLE IF NOT EXISTS analytics_prepared_views (
            company_id TEXT NOT NULL,
            snapshot_version TEXT NOT NULL,
            internal INTEGER NOT NULL,
            view_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (company_id, snapshot_version, internal, view_key)
        );
        """
    )
