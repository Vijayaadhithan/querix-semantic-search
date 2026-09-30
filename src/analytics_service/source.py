from __future__ import annotations

import logging
import sqlite3
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

import pandas as pd
from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from .config import CompanyAnalyticsConfig, DatabaseTarget, DatasetMapping
from .source_schema import DatasetContractError, DatasetSpec

LOGGER = logging.getLogger(__name__)
SOURCE_BATCH_SIZE = 2_000


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
        data: dict[str, pd.DataFrame] = {}
        for name, spec in company.dataset_specs.items():
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


def _connect_args(target: DatabaseTarget) -> dict[str, Any]:
    if target.backend == "mysql":
        options: dict[str, Any] = {
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
        return options

    options = {
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
    return options


@contextmanager
def _connection(target: DatabaseTarget) -> Iterator[Connection]:
    if not target.configured:
        raise RuntimeError(f"Analytics {target.backend} database is not configured")
    driver = "mysql+pymysql" if target.backend == "mysql" else "postgresql+psycopg"
    url = URL.create(
        driver,
        username=target.user,
        password=target.password,
        host=target.host,
        port=target.port,
        database=target.database,
    )
    engine = create_engine(
        url,
        connect_args=_connect_args(target),
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


class SqlAnalyticsDataSource:
    @staticmethod
    def _select_sql(
        target: DatabaseTarget,
        mapping: DatasetMapping,
        spec: DatasetSpec,
        *,
        history_days: int | None = None,
    ) -> str:
        quote = _quote_mysql if target.backend == "mysql" else _quote_postgres
        canonical_columns = spec.usecols or spec.required_columns
        selected = []
        for canonical in canonical_columns:
            source = mapping.columns.get(canonical, canonical)
            quoted_source = quote(source)
            projection = spec.lightweight_columns.get(canonical)
            if projection == "presence":
                selected.append(
                    f"CASE WHEN {quoted_source} IS NULL THEN NULL ELSE 1 END "
                    f"AS {quote(canonical)}"
                )
                continue
            if projection == "length":
                selected.append(f"CHAR_LENGTH({quoted_source}) AS {quote(canonical)}")
                continue
            selected.append(
                f"{quoted_source} AS {quote(canonical)}"
                if source != canonical
                else quoted_source
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
            created_at = quote(mapping.columns.get("created_at", "created_at"))
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
                spec = company.dataset_specs[name]
                mapping = company.datasets[name]
                sql = self._select_sql(
                    target,
                    mapping,
                    spec,
                    history_days=(
                        company.history_days if spec.history_window else None
                    ),
                )
                try:
                    frame = pd.read_sql_query(text(sql), connection)
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
        data: dict[str, pd.DataFrame] = {}
        for database_name, target in (
            ("company", company.database),
            ("telemetry", company.telemetry_database),
        ):
            names = tuple(
                name
                for name, spec in company.dataset_specs.items()
                if spec.database == database_name
            )
            if names:
                data.update(
                    self._load_group(
                        company=company,
                        target=target,
                        names=names,
                    )
                )
        return data

    @contextmanager
    def stage(self, company: CompanyAnalyticsConfig) -> Iterator[SqlAnalyticsStage]:
        """Copy read-only SQL results to a temporary local store in batches."""

        with TemporaryDirectory(prefix="analytics-source-") as directory:
            stage = SqlAnalyticsStage(Path(directory) / "source.sqlite3", company)
            try:
                stage.ingest(self)
                yield stage
            finally:
                stage.close()


class SqlAnalyticsStage:
    def __init__(self, path: Path, company: CompanyAnalyticsConfig) -> None:
        self.company = company
        self.connection = sqlite3.connect(path)
        self.source_rows: dict[str, int] = {}
        self.watermarks: list[pd.Timestamp] = []

    def _replace_chunks(self, name: str, chunks: Iterator[pd.DataFrame]) -> None:
        spec = self.company.dataset_specs[name]
        quoted_name = '"' + name.replace('"', '""') + '"'
        self.connection.execute(f"DROP TABLE IF EXISTS {quoted_name}")
        lightweight_dtypes = dict.fromkeys(spec.lightweight_columns, "INTEGER")
        count = 0
        for chunk in chunks:
            _validate_frame(name, chunk, spec)
            chunk.to_sql(
                name,
                self.connection,
                if_exists="append",
                index=False,
                dtype=lightweight_dtypes,
            )
            count += len(chunk)
        if count == 0:
            columns = spec.usecols or spec.required_columns
            pd.DataFrame(columns=columns).to_sql(
                name,
                self.connection,
                if_exists="replace",
                index=False,
                dtype=lightweight_dtypes,
            )
        self.source_rows[name] = count
        LOGGER.info(
            "Staged analytics dataset company=%s dataset=%s rows=%d",
            self.company.company_id,
            name,
            count,
        )

    def replace(self, name: str, chunks: Iterator[pd.DataFrame]) -> None:
        """Replace a staged dataset; useful for isolated synthetic benchmarks."""

        if name not in self.company.dataset_specs:
            raise ValueError(f"Unknown analytics dataset: {name}")
        self._replace_chunks(name, chunks)

    def ingest(self, source: SqlAnalyticsDataSource) -> None:
        for database_name, target in (
            ("company", self.company.database),
            ("telemetry", self.company.telemetry_database),
        ):
            names = tuple(
                name
                for name, spec in self.company.dataset_specs.items()
                if spec.database == database_name
            )
            if not names:
                continue
            with _connection(target) as connection:
                for name in names:
                    spec = self.company.dataset_specs[name]
                    sql = source._select_sql(
                        target,
                        self.company.datasets[name],
                        spec,
                        history_days=(
                            self.company.history_days if spec.history_window else None
                        ),
                    )
                    try:
                        streaming = connection.execution_options(stream_results=True)
                        chunks = pd.read_sql_query(
                            text(sql), streaming, chunksize=SOURCE_BATCH_SIZE
                        )
                        self._replace_chunks(name, chunks)
                    except Exception as exc:
                        raise RuntimeError(
                            f"Unable to stage analytics dataset {name!r} "
                            f"for company {self.company.company_id!r}"
                        ) from exc

    def load(
        self,
        names: tuple[str, ...],
        *,
        normalize: Any,
    ) -> dict[str, pd.DataFrame]:
        frames = {
            name: _validate_frame(
                name,
                pd.read_sql_query(
                    'SELECT * FROM "' + name.replace('"', '""') + '"',
                    self.connection,
                ),
                self.company.dataset_specs[name],
            )
            for name in names
        }
        frames = normalize(
            frames,
            timezone_name=self.company.timezone,
            dataset_specs=self.company.dataset_specs,
        )
        for frame in frames.values():
            if frame.empty or "created_at" not in frame:
                continue
            parsed = pd.to_datetime(
                frame["created_at"], utc=True, errors="coerce"
            ).dropna()
            if not parsed.empty:
                self.watermarks.append(parsed.max())
        return frames

    @property
    def source_watermark(self) -> str | None:
        return max(self.watermarks).isoformat() if self.watermarks else None

    def close(self) -> None:
        self.connection.close()
