"""Analytics-only users, password verification, and server-side sessions."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

INTERNAL_ADMIN = "internal_admin"
COMPANY_USER = "company_user"
VALID_ROLES = {INTERNAL_ADMIN, COMPANY_USER}
COMPANY_PORTAL = "company"
INTERNAL_PORTAL = "internal"
PortalType = Literal["company", "internal"]
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 3
SCRYPT_MAXMEM = 128 * 1024 * 1024


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _normalize_username(username: str) -> str:
    normalized = username.strip().casefold()
    if not normalized or len(normalized) > 191:
        raise ValueError("Username must contain between 1 and 191 characters")
    return normalized


def _session_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=32,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def _verify_password(encoded: str, password: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt = bytes.fromhex(raw_salt)
        expected = bytes.fromhex(raw_digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=SCRYPT_MAXMEM,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _password_needs_rehash(encoded: str) -> bool:
    parts = encoded.split("$", 5)
    return (
        len(parts) != 6
        or parts[0] != "scrypt"
        or parts[1:4] != [str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)]
    )


@dataclass(frozen=True, slots=True)
class AnalyticsPrincipal:
    user_id: str
    username: str
    role: str
    company_id: str | None
    portal_type: PortalType
    session_expires_at: str
    session_max_age_seconds: int

    @property
    def internal(self) -> bool:
        return self.role == INTERNAL_ADMIN


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    token: str
    principal: AnalyticsPrincipal


class AnalyticsAuthStore:
    """Persist analytics credentials and opaque session hashes in SQLite."""

    def __init__(
        self,
        path: str | Path,
        *,
        session_ttl_seconds: int = 28_800,
        company_session_idle_seconds: int | None = None,
        company_session_absolute_seconds: int | None = None,
        internal_session_idle_seconds: int | None = None,
        internal_session_absolute_seconds: int | None = None,
        max_login_attempts: int = 5,
        lock_seconds: int = 900,
        password_min_length: int = 15,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_ttl_seconds = session_ttl_seconds
        self.company_session_idle_seconds = (
            session_ttl_seconds
            if company_session_idle_seconds is None
            else company_session_idle_seconds
        )
        self.company_session_absolute_seconds = (
            session_ttl_seconds
            if company_session_absolute_seconds is None
            else company_session_absolute_seconds
        )
        self.internal_session_idle_seconds = (
            session_ttl_seconds
            if internal_session_idle_seconds is None
            else internal_session_idle_seconds
        )
        self.internal_session_absolute_seconds = (
            session_ttl_seconds
            if internal_session_absolute_seconds is None
            else internal_session_absolute_seconds
        )
        self.max_login_attempts = max_login_attempts
        self.lock_seconds = lock_seconds
        self.password_min_length = password_min_length
        self._clock = clock
        for label, idle_seconds, absolute_seconds in (
            (
                "company",
                self.company_session_idle_seconds,
                self.company_session_absolute_seconds,
            ),
            (
                "internal",
                self.internal_session_idle_seconds,
                self.internal_session_absolute_seconds,
            ),
        ):
            if idle_seconds <= 0 or absolute_seconds < idle_seconds:
                raise ValueError(f"Invalid {label} analytics session expiration policy")
        self._lock = threading.Lock()
        # A real scrypt verification is performed even for an unknown account,
        # reducing the usefulness of username timing probes.
        self._dummy_password_hash = _password_hash(secrets.token_urlsafe(32))
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analytics_users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_normalized TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (
                        role IN ('internal_admin', 'company_user')
                    ),
                    company_id TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    password_changed_at TEXT NOT NULL,
                    last_login_at TEXT,
                    CHECK (
                        (role = 'internal_admin' AND company_id IS NULL)
                        OR
                        (role = 'company_user' AND company_id IS NOT NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS analytics_sessions (
                    session_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    portal_type TEXT NOT NULL CHECK (
                        portal_type IN ('company', 'internal')
                    ),
                    role TEXT NOT NULL CHECK (
                        role IN ('internal_admin', 'company_user')
                    ),
                    company_id TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    idle_expires_at TEXT NOT NULL,
                    absolute_expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY (user_id)
                        REFERENCES analytics_users(user_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_analytics_sessions_user
                ON analytics_sessions (user_id, expires_at);

                CREATE TABLE IF NOT EXISTS analytics_auth_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    username_normalized TEXT NOT NULL,
                    company_id TEXT,
                    event_type TEXT NOT NULL,
                    remote_address TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_analytics_auth_events_user
                ON analytics_auth_events (
                    username_normalized,
                    created_at DESC
                );
                """
            )
            self._migrate_user_identity_scope(connection)
            self._migrate_sessions(connection)

    @staticmethod
    def _create_user_identity_indexes(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_internal_username
            ON analytics_users (username_normalized)
            WHERE role = 'internal_admin';

            CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_company_username
            ON analytics_users (company_id, username_normalized)
            WHERE role = 'company_user';
            """
        )

    @classmethod
    def _migrate_user_identity_scope(cls, connection: sqlite3.Connection) -> None:
        legacy_unique_username = False
        for index in connection.execute("PRAGMA index_list(analytics_users)"):
            if not bool(index[2]) or bool(index[4]):
                continue
            columns = tuple(
                row[2]
                for row in connection.execute(
                    f"PRAGMA index_info({index[1]!r})"
                )
            )
            if columns == ("username_normalized",):
                legacy_unique_username = True
                break
        if not legacy_unique_username:
            cls._create_user_identity_indexes(connection)
            return

        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE analytics_users_scoped (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_normalized TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (
                        role IN ('internal_admin', 'company_user')
                    ),
                    company_id TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    password_changed_at TEXT NOT NULL,
                    last_login_at TEXT,
                    CHECK (
                        (role = 'internal_admin' AND company_id IS NULL)
                        OR
                        (role = 'company_user' AND company_id IS NOT NULL)
                    )
                )
                """
            )
            connection.execute(
                """
                INSERT INTO analytics_users_scoped
                SELECT * FROM analytics_users
                """
            )
            connection.execute("DROP TABLE analytics_users")
            connection.execute(
                "ALTER TABLE analytics_users_scoped RENAME TO analytics_users"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        cls._create_user_identity_indexes(connection)

    @staticmethod
    def _migrate_sessions(connection: sqlite3.Connection) -> None:
        """Add portal and sliding-expiry claims to pre-rollout databases."""

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(analytics_sessions)"
            ).fetchall()
        }
        additions = {
            "portal_type": "TEXT",
            "role": "TEXT",
            "company_id": "TEXT",
            "idle_expires_at": "TEXT",
            "absolute_expires_at": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE analytics_sessions ADD COLUMN {name} {sql_type}"
                )
        connection.execute(
            """
            UPDATE analytics_sessions
            SET portal_type = COALESCE(
                    portal_type,
                    CASE (
                        SELECT role
                        FROM analytics_users
                        WHERE analytics_users.user_id = analytics_sessions.user_id
                    )
                        WHEN 'company_user' THEN 'company'
                        ELSE 'internal'
                    END
                ),
                role = COALESCE(
                    role,
                    (
                        SELECT role
                        FROM analytics_users
                        WHERE analytics_users.user_id = analytics_sessions.user_id
                    )
                ),
                company_id = COALESCE(
                    company_id,
                    (
                        SELECT company_id
                        FROM analytics_users
                        WHERE analytics_users.user_id = analytics_sessions.user_id
                    )
                ),
                idle_expires_at = COALESCE(idle_expires_at, expires_at),
                absolute_expires_at = COALESCE(
                    absolute_expires_at,
                    expires_at
                )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_analytics_sessions_expiration
            ON analytics_sessions (
                portal_type,
                idle_expires_at,
                absolute_expires_at
            )
            """
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _session_policy(
        self,
        role: str,
    ) -> tuple[PortalType, int, int]:
        if role == COMPANY_USER:
            return (
                COMPANY_PORTAL,
                self.company_session_idle_seconds,
                self.company_session_absolute_seconds,
            )
        if role == INTERNAL_ADMIN:
            return (
                INTERNAL_PORTAL,
                self.internal_session_idle_seconds,
                self.internal_session_absolute_seconds,
            )
        raise ValueError("Unsupported analytics session role")

    def _validate_password(self, password: str) -> None:
        if len(password) < self.password_min_length:
            raise ValueError(
                f"Password must contain at least {self.password_min_length} characters"
            )
        if len(password) > 1024:
            raise ValueError("Password is too long")

    @staticmethod
    def _validate_binding(role: str, company_id: str | None) -> str | None:
        if role not in VALID_ROLES:
            raise ValueError(f"Unsupported analytics role {role!r}")
        normalized_company = company_id.strip().casefold() if company_id else None
        if role == INTERNAL_ADMIN and normalized_company is not None:
            raise ValueError("Internal users cannot be bound to a company")
        if role == COMPANY_USER and normalized_company is None:
            raise ValueError("Company users must be bound to a company")
        return normalized_company

    def create_user(
        self,
        *,
        username: str,
        password: str,
        role: str,
        company_id: str | None = None,
    ) -> dict[str, str | bool | None]:
        normalized = _normalize_username(username)
        display_username = username.strip()
        normalized_company = self._validate_binding(role, company_id)
        self._validate_password(password)
        now = _iso(self._now())
        user_id = uuid.uuid4().hex
        password_hash = _password_hash(password)
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO analytics_users (
                        user_id,
                        username,
                        username_normalized,
                        password_hash,
                        role,
                        company_id,
                        created_at,
                        updated_at,
                        password_changed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        display_username,
                        normalized,
                        password_hash,
                        role,
                        normalized_company,
                        now,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Analytics username already exists") from exc
        return {
            "user_id": user_id,
            "username": display_username,
            "role": role,
            "company_id": normalized_company,
            "active": True,
        }

    @staticmethod
    def _resolve_user_id(
        connection: sqlite3.Connection,
        *,
        username_normalized: str,
        company_id: str | None,
    ) -> str:
        if company_id is None:
            rows = connection.execute(
                """
                SELECT user_id
                FROM analytics_users
                WHERE username_normalized = ? AND role = 'internal_admin'
                """,
                (username_normalized,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT user_id
                FROM analytics_users
                WHERE username_normalized = ? AND company_id = ?
                """,
                (username_normalized, company_id.strip().casefold()),
            ).fetchall()
        if not rows:
            raise ValueError("Analytics user does not exist")
        if len(rows) != 1:
            raise ValueError("Analytics username is ambiguous; specify company_id")
        return str(rows[0]["user_id"])

    def set_password(
        self,
        username: str,
        password: str,
        *,
        company_id: str | None = None,
    ) -> None:
        normalized = _normalize_username(username)
        self._validate_password(password)
        now = _iso(self._now())
        password_hash = _password_hash(password)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user_id = self._resolve_user_id(
                connection,
                username_normalized=normalized,
                company_id=company_id,
            )
            cursor = connection.execute(
                """
                UPDATE analytics_users
                SET password_hash = ?,
                    password_changed_at = ?,
                    updated_at = ?,
                    failed_attempts = 0,
                    locked_until = NULL
                WHERE user_id = ?
                """,
                (password_hash, now, now, user_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ValueError("Analytics user does not exist")
            connection.execute(
                """
                UPDATE analytics_sessions
                SET revoked_at = ?
                WHERE user_id = ?
                  AND revoked_at IS NULL
                """,
                (now, user_id),
            )
            connection.commit()

    def set_active(
        self,
        username: str,
        *,
        active: bool,
        company_id: str | None = None,
    ) -> None:
        normalized = _normalize_username(username)
        now = _iso(self._now())
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user_id = self._resolve_user_id(
                connection,
                username_normalized=normalized,
                company_id=company_id,
            )
            cursor = connection.execute(
                """
                UPDATE analytics_users
                SET active = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (int(active), now, user_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ValueError("Analytics user does not exist")
            if not active:
                connection.execute(
                    """
                    UPDATE analytics_sessions
                    SET revoked_at = ?
                    WHERE user_id = ?
                      AND revoked_at IS NULL
                    """,
                    (now, user_id),
                )
            connection.commit()

    def list_users(self) -> list[dict[str, str | bool | None]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    user_id,
                    username,
                    role,
                    company_id,
                    active,
                    created_at,
                    last_login_at
                FROM analytics_users
                ORDER BY role, company_id, username_normalized
                """
            ).fetchall()
        return [
            {
                "user_id": row["user_id"],
                "username": row["username"],
                "role": row["role"],
                "company_id": row["company_id"],
                "active": bool(row["active"]),
                "created_at": row["created_at"],
                "last_login_at": row["last_login_at"],
            }
            for row in rows
        ]

    def _event(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row | None,
        username: str,
        event_type: str,
        remote_address: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO analytics_auth_events (
                user_id,
                username_normalized,
                company_id,
                event_type,
                remote_address,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["user_id"] if row is not None else None,
                username,
                row["company_id"] if row is not None else None,
                event_type,
                (remote_address or "")[:191],
                _iso(self._now()),
            ),
        )

    def authenticate(
        self,
        *,
        username: str,
        password: str,
        required_role: str | None = None,
        required_company_id: str | None = None,
        remote_address: str | None = None,
    ) -> AuthenticatedSession | None:
        if required_role is not None and required_role not in VALID_ROLES:
            raise ValueError("Unsupported required analytics role")
        normalized_company = (
            required_company_id.strip().casefold() if required_company_id else None
        )
        if normalized_company is not None and required_role != COMPANY_USER:
            raise ValueError("Company-bound authentication requires company_user role")
        try:
            normalized = _normalize_username(username)
        except ValueError:
            normalized = ""
        now = self._now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            clauses = ["username_normalized = ?"]
            values: list[str] = [normalized]
            if required_role is not None:
                clauses.append("role = ?")
                values.append(required_role)
            if normalized_company is not None:
                clauses.append("company_id = ?")
                values.append(normalized_company)
            rows = connection.execute(
                f"SELECT * FROM analytics_users WHERE {' AND '.join(clauses)}",
                values,
            ).fetchall()
            row = rows[0] if len(rows) == 1 else None
            candidate_hash = (
                row["password_hash"] if row is not None else self._dummy_password_hash
            )
            verified = _verify_password(candidate_hash, password)
            role_allowed = row is not None and (
                required_role is None or row["role"] == required_role
            )

            locked = False
            if row is not None and row["locked_until"]:
                try:
                    locked = datetime.fromisoformat(row["locked_until"]) > now
                except ValueError:
                    locked = True

            if (
                row is None
                or not bool(row["active"])
                or locked
                or not verified
                or not role_allowed
            ):
                event = "login_rejected"
                if (
                    row is not None
                    and not locked
                    and bool(row["active"])
                    and not verified
                ):
                    failed_attempts = int(row["failed_attempts"]) + 1
                    locked_until = None
                    if failed_attempts >= self.max_login_attempts:
                        locked_until = _iso(now + timedelta(seconds=self.lock_seconds))
                        event = "account_locked"
                    connection.execute(
                        """
                        UPDATE analytics_users
                        SET failed_attempts = ?,
                            locked_until = ?,
                            updated_at = ?
                        WHERE user_id = ?
                        """,
                        (
                            failed_attempts,
                            locked_until,
                            _iso(now),
                            row["user_id"],
                        ),
                    )
                self._event(
                    connection,
                    row=row,
                    username=normalized,
                    event_type=event,
                    remote_address=remote_address,
                )
                connection.commit()
                return None

            if _password_needs_rehash(row["password_hash"]):
                connection.execute(
                    """
                    UPDATE analytics_users
                    SET password_hash = ?, password_changed_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        _password_hash(password),
                        _iso(now),
                        row["user_id"],
                    ),
                )

            portal_type, idle_seconds, absolute_seconds = self._session_policy(
                row["role"]
            )
            token = secrets.token_urlsafe(48)
            idle_expires_at = now + timedelta(seconds=idle_seconds)
            absolute_expires_at = now + timedelta(seconds=absolute_seconds)
            expires_at = min(idle_expires_at, absolute_expires_at)
            connection.execute(
                """
                UPDATE analytics_users
                SET failed_attempts = 0,
                    locked_until = NULL,
                    last_login_at = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (_iso(now), _iso(now), row["user_id"]),
            )
            connection.execute(
                """
                INSERT INTO analytics_sessions (
                    session_hash,
                    user_id,
                    portal_type,
                    role,
                    company_id,
                    created_at,
                    expires_at,
                    last_seen_at,
                    idle_expires_at,
                    absolute_expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _session_digest(token),
                    row["user_id"],
                    portal_type,
                    row["role"],
                    row["company_id"],
                    _iso(now),
                    _iso(expires_at),
                    _iso(now),
                    _iso(idle_expires_at),
                    _iso(absolute_expires_at),
                ),
            )
            self._event(
                connection,
                row=row,
                username=normalized,
                event_type="login_success",
                remote_address=remote_address,
            )
            connection.commit()

        principal = AnalyticsPrincipal(
            user_id=row["user_id"],
            username=row["username"],
            role=row["role"],
            company_id=row["company_id"],
            portal_type=portal_type,
            session_expires_at=_iso(expires_at),
            session_max_age_seconds=max(
                0,
                math.ceil((expires_at - now).total_seconds()),
            ),
        )
        return AuthenticatedSession(token=token, principal=principal)

    def resolve_session(
        self,
        token: str | None,
        *,
        portal_type: PortalType | None = None,
    ) -> AnalyticsPrincipal | None:
        if not token:
            return None
        now = self._now()
        digest = _session_digest(token)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT
                    users.user_id,
                    users.username,
                    users.role,
                    users.company_id,
                    users.active,
                    sessions.portal_type,
                    sessions.role AS session_role,
                    sessions.company_id AS session_company_id,
                    sessions.expires_at,
                    sessions.idle_expires_at,
                    sessions.absolute_expires_at,
                    sessions.revoked_at
                FROM analytics_sessions AS sessions
                INNER JOIN analytics_users AS users
                    ON users.user_id = sessions.user_id
                WHERE sessions.session_hash = ?
                """,
                (digest,),
            ).fetchone()
            if (
                row is None
                or not bool(row["active"])
                or row["revoked_at"] is not None
                or row["session_role"] != row["role"]
                or row["session_company_id"] != row["company_id"]
            ):
                connection.rollback()
                return None
            try:
                idle_expires_at = datetime.fromisoformat(row["idle_expires_at"])
                absolute_expires_at = datetime.fromisoformat(row["absolute_expires_at"])
            except ValueError:
                connection.rollback()
                return None
            expected_portal, idle_seconds, _ = self._session_policy(row["role"])
            if (
                row["portal_type"] != expected_portal
                or (portal_type is not None and row["portal_type"] != portal_type)
                or idle_expires_at <= now
                or absolute_expires_at <= now
            ):
                connection.rollback()
                return None
            refreshed_idle_expires_at = min(
                now + timedelta(seconds=idle_seconds),
                absolute_expires_at,
            )
            expires_at = min(
                refreshed_idle_expires_at,
                absolute_expires_at,
            )
            connection.execute(
                """
                UPDATE analytics_sessions
                SET last_seen_at = ?,
                    idle_expires_at = ?,
                    expires_at = ?
                WHERE session_hash = ?
                """,
                (
                    _iso(now),
                    _iso(refreshed_idle_expires_at),
                    _iso(expires_at),
                    digest,
                ),
            )
            connection.commit()
        return AnalyticsPrincipal(
            user_id=row["user_id"],
            username=row["username"],
            role=row["role"],
            company_id=row["company_id"],
            portal_type=row["portal_type"],
            session_expires_at=_iso(expires_at),
            session_max_age_seconds=max(
                0,
                math.ceil((expires_at - now).total_seconds()),
            ),
        )

    def revoke_session(
        self,
        token: str | None,
        *,
        portal_type: PortalType | None = None,
        remote_address: str | None = None,
    ) -> None:
        if not token:
            return
        digest = _session_digest(token)
        now = _iso(self._now())
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    users.user_id,
                    users.username_normalized,
                    users.company_id,
                    sessions.portal_type
                FROM analytics_sessions AS sessions
                INNER JOIN analytics_users AS users
                    ON users.user_id = sessions.user_id
                WHERE sessions.session_hash = ?
                """,
                (digest,),
            ).fetchone()
            if (
                row is not None
                and portal_type is not None
                and row["portal_type"] != portal_type
            ):
                return
            connection.execute(
                """
                UPDATE analytics_sessions
                SET revoked_at = ?
                WHERE session_hash = ? AND revoked_at IS NULL
                """,
                (now, digest),
            )
            if row is not None:
                self._event(
                    connection,
                    row=row,
                    username=row["username_normalized"],
                    event_type="logout",
                    remote_address=remote_address,
                )

    def prune_expired_sessions(self) -> int:
        now = _iso(self._now())
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM analytics_sessions
                WHERE idle_expires_at <= ?
                   OR absolute_expires_at <= ?
                   OR revoked_at IS NOT NULL
                """,
                (now, now),
            )
        return cursor.rowcount
