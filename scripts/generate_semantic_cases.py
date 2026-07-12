#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mysql_store import mysql_connection, quote_mysql_identifier  # noqa: E402
from postgres_store import (  # noqa: E402
    PostgresRuntimeConfig,
    postgres_connection,
    qualified_table,
    quote_postgres_identifier,
)
from tenant_config import discover_tenant_profiles  # noqa: E402


DEFAULT_OUTPUT = ROOT / "eval" / "gainr_semantic_cases.generated.json"

CASE_TEMPLATES = [
    {
        "name": "daily_car_chennai",
        "query": "comfortable car for a day in Chennai",
        "filters": {
            "main_category_name": "Automobiles",
            "subcategory_name": "Car",
            "city_name": "Chennai",
            "rental_duration": "Per Day",
            "type": 1,
        },
    },
    {
        "name": "hourly_driver_chennai",
        "query": "acting driver for a few hours in Chennai",
        "filters": {
            "main_category_name": "Automotive Professionals",
            "subcategory_name": "Acting Driver",
            "city_name": "Chennai",
            "rental_duration": "Per Hour",
            "type": 1,
        },
    },
    {
        "name": "electrician_hourly_chennai",
        "query": "person to fix electrical wiring in Chennai by the hour",
        "filters": {
            "main_category_name": "Personal & Home Services",
            "subcategory_name": "Electrician",
            "city_name": "Chennai",
            "rental_duration": "Per Hour",
            "type": 1,
        },
    },
    {
        "name": "hourly_tutor_chennai",
        "query": "teacher for home lessons in Chennai for one hour",
        "filters": {
            "main_category_name": "Education Field",
            "subcategory_name": "Tutor",
            "city_name": "Chennai",
            "rental_duration": "Per Hour",
            "type": 1,
        },
        "acceptable_filters": [
            {
                "main_category_name": "Education Field",
                "subcategory_name": "Tutor",
                "city_name": "Chennai",
                "rental_duration": "Per Hour",
                "type": 1,
            },
            {
                "main_category_name": "Education Field",
                "subcategory_name": "Teacher",
                "city_name": "Chennai",
                "rental_duration": "Per Hour",
                "type": 1,
            },
        ],
    },
    {
        "name": "technician_hourly_chennai",
        "query": "technical support person in Chennai for hourly hire",
        "filters": {
            "main_category_name": "IT & ITES Services",
            "subcategory_name": "Technician",
            "city_name": "Chennai",
            "rental_duration": "Per Hour",
            "type": 1,
        },
    },
    {
        "name": "monthly_fiction_books_mumbai",
        "query": "fiction books available monthly in Mumbai",
        "filters": {
            "main_category_name": "Books",
            "subcategory_name": "Fiction",
            "city_name": "Mumbai",
            "rental_duration": "Per Month",
            "type": 1,
        },
    },
    {
        "name": "apartment_hourly_coimbatore",
        "query": "apartment space for a few hours in Coimbatore",
        "filters": {
            "main_category_name": "Accommodation & Spaces",
            "subcategory_name": "Apartment",
            "city_name": "Coimbatore",
            "rental_duration": "Per Hour",
            "type": 1,
        },
    },
    {
        "name": "computer_repair_hourly_puducherry",
        "query": "laptop and computer repair service by the hour in Puducherry",
        "filters": {
            "main_category_name": "Computer & Accessories",
            "subcategory_name": "Others",
            "city_name": "Puducherry",
            "rental_duration": "Per Hour",
            "type": 1,
        },
    },
    {
        "name": "wanted_mechanic_chennai",
        "query": "people needing a mechanic for hourly hire in Chennai",
        "filters": {
            "main_category_name": "Automotive Professionals",
            "subcategory_name": "Mechanic",
            "city_name": "Chennai",
            "rental_duration": "Per Hour",
            "type": 2,
        },
        "acceptable_filters": [
            {
                "main_category_name": "Automotive Professionals",
                "subcategory_name": "Mechanic",
                "city_name": "Chennai",
                "rental_duration": "Per Hour",
                "type": 2,
            },
            {
                "main_category_name": "Automotive Professionals",
                "subcategory_name": "Mechanic",
                "city_name": "Chennai",
                "rental_duration": "Per Hour",
                "type": 1,
            },
        ],
    },
    {
        "name": "daily_car_cuddalore",
        "query": "car rental for a day near Cuddalore",
        "filters": {
            "main_category_name": "Automobiles",
            "subcategory_name": "Car",
            "city_name": "Cuddalore",
            "rental_duration": "Per Day",
            "type": 1,
        },
    },
]


def table_name(config) -> str:
    if isinstance(config, PostgresRuntimeConfig):
        return qualified_table(config, config.search_table)
    return quote_mysql_identifier(config.search_table)


def connection_context(config):
    if isinstance(config, PostgresRuntimeConfig):
        return postgres_connection(config)
    return mysql_connection(config=config)


def quote_identifier(config, value: str) -> str:
    if isinstance(config, PostgresRuntimeConfig):
        return quote_postgres_identifier(value)
    return quote_mysql_identifier(value)


def fetch_relevant_ids(config, filters: dict[str, Any], limit: int) -> list[str]:
    table = table_name(config)
    clauses = []
    params: list[Any] = []
    for column, value in filters.items():
        clauses.append(f"{quote_identifier(config, column)} = %s")
        params.append(value)
    where = " AND ".join(clauses)
    with connection_context(config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {quote_identifier(config, config.search_id_column)}
                FROM {table}
                WHERE {where}
                ORDER BY {quote_identifier(config, "updated_at")} DESC,
                         {quote_identifier(config, config.search_id_column)} DESC
                LIMIT %s
                """,
                (*params, limit),
            )
            return [str(row[0]) for row in cursor.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate labelled Gainr semantic retrieval cases from live DB rows."
    )
    parser.add_argument("--company", default="gainr")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--relevant-limit", type=int, default=12)
    args = parser.parse_args()
    if args.relevant_limit <= 0:
        parser.error("--relevant-limit must be positive")

    profiles = discover_tenant_profiles()
    try:
        profile = profiles[args.company]
    except KeyError:
        available = ", ".join(sorted(profiles)) or "none"
        parser.error(f"unknown company {args.company!r}; available: {available}")

    cases = []
    skipped = []
    for template in CASE_TEMPLATES:
        relevant_ids = fetch_relevant_ids(
            profile.database,
            template["filters"],
            args.relevant_limit,
        )
        if not relevant_ids:
            skipped.append(template["name"])
            continue
        cases.append(
            {
                "name": template["name"],
                "query": template["query"],
                "relevant_ids": relevant_ids,
                "source_filters": template["filters"],
                **(
                    {"acceptable_filters": template["acceptable_filters"]}
                    if template.get("acceptable_filters")
                    else {}
                ),
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(cases)} cases to {args.output}")
    if skipped:
        print("Skipped without matching DB rows: " + ", ".join(skipped))
    for case in cases:
        print(
            f"- {case['name']}: {case['query']} "
            f"({len(case['relevant_ids'])} relevant IDs)"
        )
    return 0 if cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
