from __future__ import annotations

from typing import Any, Protocol

import numpy as np

from averon_import.services.table_detector import DetectedTable


class OcrProvider(Protocol):
    """Stable boundary between the recognition pipeline and an OCR engine."""

    def recognize_table(
        self,
        image: np.ndarray,
        table: DetectedTable,
        header_rows: int = 2,
        mode: str = "standard",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ...
