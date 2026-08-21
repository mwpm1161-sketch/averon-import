from __future__ import annotations

from collections.abc import Iterable


class AIBatchProcessor:
    """Prepare conservative batches for AI correction.

    Keeps batching independent from providers so local and cloud models share
    the same execution path.
    """

    def __init__(self, batch_size: int = 10):
        self.batch_size = max(1, batch_size)

    def split(self, rows: list[dict]) -> Iterable[list[dict]]:
        for index in range(0, len(rows), self.batch_size):
            yield rows[index : index + self.batch_size]
