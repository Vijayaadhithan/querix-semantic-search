from search.retrieval import VECTOR_EXACT_FILTER_MAX_ROWS, vector_search


class SqlLimitCollection:
    query_limit_requires_count = False

    def __init__(self):
        self.options = None

    def count(self):
        raise AssertionError("SQL-backed vector search must not count per query")

    def query(self, **options):
        self.options = options
        return {
            "ids": [["1"]],
            "documents": [["camera"]],
            "metadatas": [[{
                "primary_key_value": "1",
                "source_file": "ads",
                "city_id": 456,
                "subcategory_id": 12,
            }]],
            "distances": [[0.1]],
        }


def test_sql_vector_search_skips_redundant_count_round_trip():
    collection = SqlLimitCollection()
    metrics = {}

    results = vector_search(
        "camera",
        collection,
        top_k=20,
        candidate_k=80,
        query_embedding=[0.1, 0.2],
        metrics=metrics,
    )

    assert [row["id"] for row in results] == ["1"]
    assert collection.options["n_results"] == 80
    assert metrics["count_ms"] == 0.0


def test_city_exact_limit_applies_only_to_structured_city_filter():
    collection = SqlLimitCollection()
    common = {
        "query_embedding": [0.1, 0.2],
        "source_name": "ads",
        "post_filter_metadata": "adaptive",
        "city_exact_filter_max_rows": 10_000,
    }

    vector_search(
        "camera",
        collection,
        resolved_filters={"categorical": {"city_id": 456}},
        **common,
    )
    assert collection.options["exact_filter_max_rows"] == 10_000
    assert collection.options["where"] == {"city_id": 456}

    vector_search(
        "camera",
        collection,
        resolved_filters={"categorical": {"subcategory_id": 12}},
        **common,
    )
    assert collection.options["exact_filter_max_rows"] == (
        VECTOR_EXACT_FILTER_MAX_ROWS
    )
