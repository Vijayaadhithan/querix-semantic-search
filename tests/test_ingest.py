import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ingest import EMBED_MODEL, chunk_id, chunk_text, source_is_current


def test_chunk_text_uses_overlap():
    assert chunk_text("one two three four five", 3, 1) == [
        "one two three",
        "three four five",
        "five",
    ]


@pytest.mark.parametrize("chunk_size,overlap", [(0, 0), (3, -1), (3, 3), (3, 4)])
def test_chunk_text_rejects_invalid_settings(chunk_size, overlap):
    with pytest.raises(ValueError):
        chunk_text("some text", chunk_size, overlap)


def test_chunk_ids_are_stable_and_source_specific():
    assert chunk_id("a.pdf", 1, 0) == chunk_id("a.pdf", 1, 0)
    assert chunk_id("a.pdf", 1, 0) != chunk_id("b.pdf", 1, 0)


class FakeCollection:
    def __init__(self, ids, documents, model=EMBED_MODEL):
        self.data = {
            "ids": ids,
            "documents": documents,
            "metadatas": [{"embedding_model": model} for _ in ids],
        }

    def get(self, **_kwargs):
        return self.data


def test_source_is_current_matches_ids_text_and_model():
    collection = FakeCollection(["id-1"], ["stored text"])
    assert source_is_current(collection, "source.pdf", ["id-1"], ["stored text"])
    assert not source_is_current(collection, "source.pdf", ["id-1"], ["new text"])


def test_source_is_current_rejects_different_embedding_model():
    collection = FakeCollection(["id-1"], ["stored text"], model="another-model")
    assert not source_is_current(collection, "source.pdf", ["id-1"], ["stored text"])
