from search.retrieval import vector_search


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
            "metadatas": [[{"primary_key_value": "1"}]],
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
