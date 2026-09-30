"""Disk-backed, replayable query records for bounded snapshot construction."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from typing import Any, TextIO

from analytics_service.store import json_dumps


class QueryRecordSpool:
    """Hold query payloads on disk while reports make sequential passes."""

    def __init__(self) -> None:
        self._file: TextIO = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        self._count = 0

    def append(self, record: dict[str, Any]) -> None:
        self._file.write(json_dumps(record) + "\n")
        self._count += 1

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[dict[str, Any]]:
        # Callers perform sequential passes; each pass starts at the first row.
        self._file.flush()
        self._file.seek(0)
        for line in self._file:
            yield json.loads(line)

    def close(self) -> None:
        self._file.close()
