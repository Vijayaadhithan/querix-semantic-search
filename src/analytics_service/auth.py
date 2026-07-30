"""Analytics-only users, password verification, and server-side sessions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

INTERNAL_ADMIN = "internal_admin"
COMPANY_USER = "company_user"
VALID_ROLES = {INTERNAL_ADMIN, COMPANY_USER}
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
    return (
        f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
        f"{salt.hex()}${digest.hex()}"
    )


def _verify_password(encoded: str, password: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = (
            encoded.split("$", 5)
        )
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
        or parts[1:4]
        != [str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)]
    )


@dataclass(frozen=True, slots=True)
class AnalyticsPrincipal:
    user_id: str
    username: str
    role: str
    company_id: str | None
    session_expires_at: str

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
        max_login_attempts: int = 5,
        lock_seconds: int = 900,
        password_min_length: int = 15,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_ttl_seconds = session_ttl_seconds
        self.max_login_attempts = max_login_attempts
        self.lock_seconds = lock_seconds
        self.password_min_length = password_min_length
        self._lock = threading.Lock()
        # A real scrypt verification is performed even for an unknown account,
        # reducing the usefulness of username timing probes.
        self._dummy_password_hash = _password_hash(
            secrets.token_urlsafe(32)
        )
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
                    username_normalized TEXT NOT NULL UNIQUE,
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
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
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

    def _validate_password(self, password: str) -> None:
        if len(password) < self.password_min_length:
            raise ValueError(
                "Password must contain at least "
                f"{self.password_min_length} characters"
            )
        if len(password) > 1024:
            raise ValueError("Password is too long")

    @staticmethod
    def _validate_binding(role: str, company_id: str | None) -> str | None:
        if role not in VALID_ROLES:
            raise ValueError(f"Unsupported analytics role {role!r}")
        normalized_company = (
            company_id.strip().casefold() if company_id else None
        )
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
        now = _iso(_utc_now())
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

    def set_password(self, username: str, password: str) -> None:
        normalized = _normalize_username(username)
        self._validate_password(password)
        now = _iso(_utc_now())
        password_hash = _password_hash(password)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE analytics_users
                SET password_hash = ?,
                    password_changed_at = ?,
                    updated_at = ?,
                    failed_attempts = 0,
                    locked_until = NULL
                WHERE username_normalized = ?
                """,
                (password_hash, now, now, normalized),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ValueError("Analytics user does not exist")
            connection.execute(
                """
                UPDATE analytics_sessions
                SET revoked_at = ?
                WHERE user_id = (
                    SELECT user_id
                    FROM analytics_users
                    WHERE username_normalized = ?
                )
                  AND revoked_at IS NULL
                """,
                (now, normalized),
            )
            connection.commit()

    def set_active(self, username: str, *, active: bool) -> None:
        normalized = _normalize_username(username)
        now = _iso(_utc_now())
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE analytics_users
                SET active = ?, updated_at = ?
                WHERE username_normalized = ?
                """,
                (int(active), now, normalized),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ValueError("Analytics user does not exist")
            if not active:
                connection.execute(
                    """
                    UPDATE analytics_sessions
                    SET revoked_at = ?
                    WHERE user_id = (
                        SELECT user_id
                        FROM analytics_users
                        WHERE username_normalized = ?
                    )
                      AND revoked_at IS NULL
                    """,
                    (now, normalized),
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

    @staticmethod
    def _event(
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
                _iso(_utc_now()),
            ),
        )

    def authenticate(
        self,
        *,
        username: str,
        password: str,
        remote_address: str | None = None,
    ) -> AuthenticatedSession | None:
        try:
            normalized = _normalize_username(username)
        except ValueError:
            normalized = ""
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM analytics_users
                WHERE username_normalized = ?
                """,
                (normalized,),
            ).fetchone()
            candidate_hash = (
                row["password_hash"]
                if row is not None
                else self._dummy_password_hash
            )
            verified = _verify_password(candidate_hash, password)

            locked = False
            if row is not None and row["locked_until"]:
                try:
                    locked = datetime.fromisoformat(
                        row["locked_until"]
                    ) > now
                except ValueError:
                    locked = True

            if (
                row is None
                or not bool(row["active"])
                or locked
                or not verified
            ):
                event = "login_rejected"
                if row is not None and not locked and bool(row["active"]):
                    failed_attempts = int(row["failed_attempts"]) + 1
                    locked_until = None
                    if failed_attempts >= self.max_login_attempts:
                        locked_until = _iso(
                            now + timedelta(seconds=self.lock_seconds)
                        )
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

            token = secrets.token_urlsafe(48)
            expires_at = now + timedelta(seconds=self.session_ttl_seconds)
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
                    created_at,
                    expires_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _session_digest(token),
                    row["user_id"],
                    _iso(now),
                    _iso(expires_at),
                    _iso(now),
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
            session_expires_at=_iso(expires_at),
        )
        return AuthenticatedSession(token=token, principal=principal)

    def resolve_session(self, token: str | None) -> AnalyticsPrincipal | None:
        if not token:
            return None
        now = _utc_now()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    users.user_id,
                    users.username,
                    users.role,
                    users.company_id,
                    users.active,
                    sessions.expires_at,
                    sessions.revoked_at
                FROM analytics_sessions AS sessions
                INNER JOIN analytics_users AS users
                    ON users.user_id = sessions.user_id
                WHERE sessions.session_hash = ?
                """,
                (_session_digest(token),),
            ).fetchone()
            if (
                row is None
                or not bool(row["active"])
                or row["revoked_at"] is not None
            ):
                return None
            try:
                expires_at = datetime.fromisoformat(row["expires_at"])
            except ValueError:
                return None
            if expires_at <= now:
                return None
            connection.execute(
                """
                UPDATE analytics_sessions
                SET last_seen_at = ?
                WHERE session_hash = ?
                """,
                (_iso(now), _session_digest(token)),
            )
        return AnalyticsPrincipal(
            user_id=row["user_id"],
            username=row["username"],
            role=row["role"],
            company_id=row["company_id"],
            session_expires_at=row["expires_at"],
        )

    def revoke_session(
        self,
        token: str | None,
        *,
        remote_address: str | None = None,
    ) -> None:
        if not token:
            return
        digest = _session_digest(token)
        now = _iso(_utc_now())
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    users.user_id,
                    users.username_normalized,
                    users.company_id
                FROM analytics_sessions AS sessions
                INNER JOIN analytics_users AS users
                    ON users.user_id = sessions.user_id
                WHERE sessions.session_hash = ?
                """,
                (digest,),
            ).fetchone()
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
        now = _iso(_utc_now())
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM analytics_sessions
                WHERE expires_at <= ? OR revoked_at IS NOT NULL
                """,
                (now,),
            )
        return cursor.rowcount
