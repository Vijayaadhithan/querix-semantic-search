"""Read-only relevance labels and retrieval evaluation measures."""

from storage.mysql import (
    MySQLRuntimeConfig,
    mysql_active_condition,
    mysql_connection,
    quote_mysql_identifier,
)
from storage.postgres import (
    PostgresRuntimeConfig,
    postgres_connection,
    qualified_table,
    quote_postgres_identifier,
)


def reciprocal_rank(result_ids: list[str], relevant_ids: set[str]) -> float:
    for rank, product_id in enumerate(result_ids, start=1):
        if product_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def precision_at_k(
    result_ids: list[str],
    relevant_ids: set[str],
    k: int,
) -> float:
    considered = result_ids[:k]
    if not considered:
        return 0.0
    return sum(product_id in relevant_ids for product_id in considered) / len(
        considered
    )


def quote_identifier(config, value: str) -> str:
    if isinstance(config, PostgresRuntimeConfig):
        return quote_postgres_identifier(value)
    return quote_mysql_identifier(value)


def source_filter_clause(
    config,
    column: str,
    expected,
) -> tuple[str, list]:
    identifier = quote_identifier(config, column)
    if not isinstance(expected, dict):
        return f"{identifier} = %s", [expected]
    if len(expected) != 1:
        raise ValueError(f"Source filter {column!r} must contain exactly one operator.")
    operator, value = next(iter(expected.items()))
    sql_operators = {
        "$lt": "<",
        "$lte": "<=",
        "$gt": ">",
        "$gte": ">=",
        "$ne": "!=",
    }
    if operator in sql_operators:
        return f"{identifier} {sql_operators[operator]} %s", [value]
    if operator == "$lte_or_null":
        return f"({identifier} <= %s OR {identifier} IS NULL)", [value]
    if operator == "$in":
        values = list(value)
        if not values:
            return "1 = 0", []
        placeholders = ", ".join(["%s"] * len(values))
        return f"{identifier} IN ({placeholders})", values
    raise ValueError(f"Unsupported source filter operator {operator!r} for {column!r}.")


def matching_ids_from_search_table(
    config,
    result_ids: list[str],
    filters: dict,
) -> set[str]:
    if not result_ids:
        return set()
    table = (
        qualified_table(config, config.search_table)
        if isinstance(config, PostgresRuntimeConfig)
        else quote_mysql_identifier(config.search_table)
    )
    placeholders = ", ".join(["%s"] * len(result_ids))
    clauses = [
        f"{quote_identifier(config, config.search_id_column)} IN ({placeholders})"
    ]
    if isinstance(config, MySQLRuntimeConfig) and (
        active_condition := mysql_active_condition(config)
    ):
        clauses.insert(0, active_condition)
    params: list = list(result_ids)
    for column, expected in filters.items():
        clause, clause_params = source_filter_clause(
            config,
            column,
            expected,
        )
        clauses.append(clause)
        params.extend(clause_params)
    context = (
        postgres_connection(config)
        if isinstance(config, PostgresRuntimeConfig)
        else mysql_connection(config=config)
    )
    with context as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""
                SELECT {quote_identifier(config, config.search_id_column)}
                FROM {table}
                WHERE {" AND ".join(clauses)}
                """,
            params,
        )
        return {str(row[0]) for row in cursor.fetchall()}


def matching_ids_from_filter_groups(
    config,
    result_ids: list[str],
    filter_groups: list[dict],
) -> tuple[set[str], int | None]:
    for index, filters in enumerate(filter_groups):
        matching = matching_ids_from_search_table(
            config,
            result_ids,
            filters,
        )
        if matching:
            return matching, index
    return set(), None


def relevant_ids_for_case(
    profile,
    case: dict,
    all_observed_ids: list[str],
) -> tuple[set[str], int | None]:
    if case.get("expected_empty"):
        return set(), None
    filter_groups = case.get("acceptable_filters")
    if not filter_groups and (
        case.get("expected_filters") or case.get("source_filters")
    ):
        filter_groups = [case.get("expected_filters") or case.get("source_filters")]
    if filter_groups:
        return matching_ids_from_filter_groups(
            profile.database,
            all_observed_ids,
            filter_groups,
        )
    return {str(value) for value in case["relevant_ids"]}, None
