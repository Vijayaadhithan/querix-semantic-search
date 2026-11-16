"""Analytics account and session schema initialization and migrations."""

import sqlite3


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


def _migrate_user_identity_scope(connection: sqlite3.Connection) -> None:
    legacy_unique_username = False
    for index in connection.execute("PRAGMA index_list(analytics_users)"):
        if not bool(index[2]) or bool(index[4]):
            continue
        columns = tuple(
            row[2] for row in connection.execute(f"PRAGMA index_info({index[1]!r})")
        )
        if columns == ("username_normalized",):
            legacy_unique_username = True
            break
    if not legacy_unique_username:
        _create_user_identity_indexes(connection)
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
    _create_user_identity_indexes(connection)


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


def initialize_auth_schema(connection: sqlite3.Connection) -> None:
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
    _migrate_user_identity_scope(connection)
    _migrate_sessions(connection)
