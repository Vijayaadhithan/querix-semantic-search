import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pgvector_store import PgVectorCollection


def test_pgvector_metadata_filter_supports_chroma_where_shape():
    clause, params = PgVectorCollection._metadata_filter_sql(
        {
            "$and": [
                {"city_name": "Chennai"},
                {"subcategory_name": {"$in": ["Bike", "Car"]}},
                {"rental_fee": {"$gte": 100, "$lte": 1000}},
            ]
        }
    )

    assert "metadata ->> %s = %s" in clause
    assert "metadata ->> %s IN (%s, %s)" in clause
    assert "::double precision >=" in clause
    assert "::double precision <=" in clause
    assert params == [
        "city_name",
        "Chennai",
        "subcategory_name",
        "Bike",
        "Car",
        "rental_fee",
        r"^-?[0-9]+(\.[0-9]+)?$",
        "rental_fee",
        100.0,
        "rental_fee",
        r"^-?[0-9]+(\.[0-9]+)?$",
        "rental_fee",
        1000.0,
    ]


def test_pgvector_metadata_filter_rejects_unknown_operator():
    try:
        PgVectorCollection._metadata_filter_sql({"city_name": {"$ne": "Chennai"}})
    except ValueError as exc:
        assert "$ne" in str(exc)
    else:
        raise AssertionError("Expected unsupported pgvector operator to fail.")
