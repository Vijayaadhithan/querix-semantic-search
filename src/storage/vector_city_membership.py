"""Maintain one indexed city membership table per physical vector generation."""

from storage.index_layout import city_membership_table_name
from storage.postgres import postgres_connection, qualified_table, require_psycopg

_SYNC_TRIGGER = "sync_vector_city_membership"


def city_membership_table(config, table: str) -> str:
    return qualified_table(config, city_membership_table_name(table))


def has_city_membership(cursor, table_sql: str, membership_sql: str) -> bool:
    cursor.execute(
        """
        SELECT COALESCE(has_table_privilege(to_regclass(%s), 'SELECT'), FALSE)
        AND EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = to_regclass(%s)
              AND tgname = %s AND tgenabled IN ('O', 'A')
        ) AS available
        """,
        (membership_sql, table_sql, _SYNC_TRIGGER),
    )
    return bool(cursor.fetchone()["available"])


def ensure_city_membership(config, table: str) -> None:
    table_sql = qualified_table(config, table)
    membership_sql = city_membership_table(config, table)
    psycopg, _ = require_psycopg()
    with (
        postgres_connection(config, dict_rows=True) as connection,
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        if has_city_membership(cursor, table_sql, membership_sql):
            return
        # Block writes during the backfill and trigger installation. Searches
        # can keep reading the existing vector table throughout the migration.
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute(f"LOCK TABLE {table_sql} IN SHARE ROW EXCLUSIVE MODE")
        if has_city_membership(cursor, table_sql, membership_sql):
            return
        cursor.execute(
            f"""
            CREATE TABLE {membership_sql} (
                city_id TEXT NOT NULL,
                id TEXT NOT NULL,
                PRIMARY KEY (city_id, id)
            )
            """
        )
        cursor.execute(
            f"""
            INSERT INTO {membership_sql} (city_id, id)
            SELECT metadata ->> 'city_id', id
            FROM {table_sql}
            WHERE metadata ->> 'city_id' IS NOT NULL
            """
        )
        body = f"""
            BEGIN
                IF TG_OP = 'TRUNCATE' THEN
                    TRUNCATE TABLE {membership_sql};
                    RETURN NULL;
                END IF;
                IF TG_OP = 'UPDATE' THEN
                    IF OLD.id = NEW.id AND
                       (OLD.metadata ->> 'city_id') IS NOT DISTINCT FROM
                       (NEW.metadata ->> 'city_id') THEN
                        RETURN NULL;
                    END IF;
                END IF;
                IF TG_OP <> 'INSERT' THEN
                    DELETE FROM {membership_sql}
                    WHERE city_id = OLD.metadata ->> 'city_id' AND id = OLD.id;
                END IF;
                IF TG_OP <> 'DELETE' AND NEW.metadata ->> 'city_id' IS NOT NULL THEN
                    INSERT INTO {membership_sql} (city_id, id)
                    VALUES (NEW.metadata ->> 'city_id', NEW.id);
                END IF;
                RETURN NULL;
            END;
        """
        cursor.execute(
            psycopg.sql.SQL(
                "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS {}"
            ).format(psycopg.sql.SQL(membership_sql), psycopg.sql.Literal(body))
        )
        cursor.execute(
            f"CREATE TRIGGER {_SYNC_TRIGGER} "
            f"AFTER INSERT OR UPDATE OF id, metadata OR DELETE ON {table_sql} "
            f"FOR EACH ROW EXECUTE FUNCTION {membership_sql}()"
        )
        cursor.execute(
            f"CREATE TRIGGER {_SYNC_TRIGGER}_truncate AFTER TRUNCATE ON {table_sql} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {membership_sql}()"
        )
        cursor.execute(f"ANALYZE {membership_sql}")
