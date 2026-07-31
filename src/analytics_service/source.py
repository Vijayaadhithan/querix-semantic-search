from __future__ import annotations

import logging
import ssl
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

import pandas as pd

from .config import CompanyAnalyticsConfig, DatabaseTarget, DatasetMapping
from .source_schema import DATASET_SPECS, DatasetContractError, DatasetSpec


LOGGER = logging.getLogger(__name__)


class AnalyticsDataSource(Protocol):
    def load(self, company: CompanyAnalyticsConfig) -> dict[str, pd.DataFrame]:
        """Load one normalized analytics dataset bundle."""


def _validate_frame(
    name: str,
    frame: pd.DataFrame,
    spec: DatasetSpec,
) -> pd.DataFrame:
    missing = sorted(set(spec.required_columns) - set(frame.columns))
    if missing:
        raise DatasetContractError(
            f"{name} is missing required columns: {', '.join(missing)}"
        )
    for column in spec.numeric_columns:
        if column not in frame:
            continue
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


class CsvAnalyticsDataSource:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()

    def load(self, company: CompanyAnalyticsConfig) -> dict[str, pd.DataFrame]:
        del company
        data: dict[str, pd.DataFrame] = {}
        for name, spec in DATASET_SPECS.items():
            path = self.data_dir / spec.filename
            if not path.is_file():
                raise DatasetContractError(
                    f"Required analytics dataset not found: {path}"
                )
            try:
                frame = pd.read_csv(
                    path,
                    usecols=list(spec.usecols) if spec.usecols else None,
                    dtype=spec.dtypes,
                    low_memory=False,
                )
            except ValueError as exc:
                raise DatasetContractError(
                    f"Unable to load {name} from {path.name}: {exc}"
                ) from exc
            data[name] = _validate_frame(name, frame, spec)
            LOGGER.info(
                "Loaded analytics dataset company=%s dataset=%s rows=%d",
                "csv",
                name,
                len(frame),
            )
        return data


def _quote_mysql(identifier: str) -> str:
    return f"`{identifier.replace('`', '``')}`"


def _quote_postgres(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _mysql_ssl_context(target: DatabaseTarget) -> ssl.SSLContext | None:
    if target.tls_mode in {"disable", "prefer"}:
        return None
    if target.tls_mode == "require":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    else:
        context = ssl.create_default_context(
            cafile=target.tls_ca_file or None,
        )
        context.check_hostname = target.tls_mode == "verify-full"
    if target.tls_cert_file:
        context.load_cert_chain(
            target.tls_cert_file,
            target.tls_key_file or None,
        )
    return context


@contextmanager
def _connection(target: DatabaseTarget) -> Iterator[Any]:
    if not target.configured:
        raise RuntimeError(
            f"Analytics {target.backend} database is not configured"
        )
    if target.backend == "mysql":
        import pymysql

        options: dict[str, Any] = {
            "host": target.host,
            "port": target.port,
            "user": target.user,
            "password": target.password,
            "database": target.database,
            "charset": "utf8mb4",
            "connect_timeout": target.connect_timeout_seconds,
            "read_timeout": target.read_timeout_seconds,
            "write_timeout": target.read_timeout_seconds,
            "autocommit": True,
        }
        ssl_context = _mysql_ssl_context(target)
        if target.tls_mode == "disable":
            options["ssl_disabled"] = True
        elif ssl_context is not None:
            options["ssl"] = ssl_context
        connection = pymysql.connect(**options)
    else:
        import psycopg

        options = {
            "host": target.host,
            "port": target.port,
            "dbname": target.database,
            "user": target.user,
            "password": target.password,
            "connect_timeout": target.connect_timeout_seconds,
            "sslmode": target.tls_mode,
            "autocommit": True,
        }
        if target.tls_ca_file:
            options["sslrootcert"] = target.tls_ca_file
        if target.tls_cert_file:
            options["sslcert"] = target.tls_cert_file
        if target.tls_key_file:
            options["sslkey"] = target.tls_key_file
        connection = psycopg.connect(**options)
    try:
        yield connection
    finally:
        connection.close()


class SqlAnalyticsDataSource:
    @staticmethod
    def _select_sql(
        target: DatabaseTarget,
        mapping: DatasetMapping,
        spec: DatasetSpec,
        *,
        history_days: int | None = None,
    ) -> str:
        quote = (
            _quote_mysql
            if target.backend == "mysql"
            else _quote_postgres
        )
        canonical_columns = spec.usecols or spec.required_columns
        selected = []
        for canonical in canonical_columns:
            source = mapping.columns.get(canonical, canonical)
            selected.append(
                f"{quote(source)} AS {quote(canonical)}"
                if source != canonical
                else quote(source)
            )
        table = quote(mapping.table)
        if target.backend == "postgres":
            table = f"{quote(target.schema)}.{table}"
        sql = f"SELECT {', '.join(selected)} FROM {table}"
        if history_days is not None:
            if (
                isinstance(history_days, bool)
                or not isinstance(history_days, int)
                or not 1 <= history_days <= 3650
            ):
                raise ValueError(
                    "Analytics SQL history_days must be between 1 and 3650"
                )
            created_at = quote(
                mapping.columns.get("created_at", "created_at")
            )
            if target.backend == "mysql":
                sql += (
                    f" WHERE {created_at} >= CURRENT_TIMESTAMP "
                    f"- INTERVAL {history_days} DAY"
                )
            else:
                sql += (
                    f" WHERE {created_at} >= CURRENT_TIMESTAMP "
                    f"- INTERVAL '{history_days} days'"
                )
        return sql

    def _load_group(
        self,
        *,
        company: CompanyAnalyticsConfig,
        target: DatabaseTarget,
        names: tuple[str, ...],
    ) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        with _connection(target) as connection:
            for name in names:
                spec = DATASET_SPECS[name]
                mapping = company.datasets[name]
                sql = self._select_sql(
                    target,
                    mapping,
                    spec,
                    history_days=(
                        company.history_days
                        if name in {"search_history", "api_usage"}
                        else None
                    ),
                )
                try:
                    frame = pd.read_sql_query(sql, connection)
                except Exception as exc:
                    raise RuntimeError(
                        f"Unable to load analytics dataset {name!r} "
                        f"for company {company.company_id!r}"
                    ) from exc
                frames[name] = _validate_frame(name, frame, spec)
                LOGGER.info(
                    "Loaded analytics dataset company=%s dataset=%s rows=%d",
                    company.company_id,
                    name,
                    len(frame),
                )
        return frames

    def load(self, company: CompanyAnalyticsConfig) -> dict[str, pd.DataFrame]:
        company_names = tuple(
            name for name in DATASET_SPECS if name != "api_usage"
        )
        data = self._load_group(
            company=company,
            target=company.database,
            names=company_names,
        )
        data.update(
            self._load_group(
                company=company,
                target=company.telemetry_database,
                names=("api_usage",),
            )
        )
        return data
