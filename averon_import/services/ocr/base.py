"""Provider-neutral OCR contract.

The protocol is document-oriented: a provider receives a PDF path and page
numbers and returns structured rows in the internal Averon DTO. Image
rendering, table detection and cloud/API specifics stay inside adapters.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

ProgressCallback = Callable[[int, int, str], None]


class OcrProviderError(RuntimeError):
    """A provider failed to deliver OCR output for reasons worth reporting."""


@dataclass(slots=True)
class OcrRow:
    """One recognized table row in the legacy Averon cell format."""

    source_row: int
    values: dict[str, str]
    confidences: dict[str, float]
    sources: dict[str, str]
    bbox: dict

    def as_dict(self) -> dict:
        return {
            "source_row": self.source_row,
            "values": dict(self.values),
            "confidences": dict(self.confidences),
            "ocr_sources": dict(self.sources),
            "bbox": dict(self.bbox),
        }


@dataclass(slots=True)
class PageOcrResult:
    page: int
    rows: list[OcrRow] = field(default_factory=list)
    geometry: dict | None = None
    errors: list[str] = field(default_factory=list)
    provides_confidence: bool = True

    def as_raw_rows(self) -> list[dict]:
        return [row.as_dict() for row in self.rows]


@dataclass(slots=True)
class OcrResult:
    provider: str
    pages: list[PageOcrResult] = field(default_factory=list)

    def page(self, number: int) -> PageOcrResult | None:
        for item in self.pages:
            if item.page == number:
                return item
        return None


class OcrProvider(Protocol):
    key: str
    label: str

    def available(self) -> bool:
        """Cheap readiness check (configuration present)."""
        ...

    def health(self) -> dict:
        """Detailed status for /api/health."""
        ...

    def recognize(
        self,
        pdf_path: Path,
        pages: list[int],
        *,
        pages_dir: Path | None = None,
        dpi: int | None = None,
        crop: dict | None = None,
        mode: str | None = None,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> OcrResult:
        """Recognize the requested pages of the document."""
        ...
