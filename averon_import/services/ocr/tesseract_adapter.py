"""Adapter that serves the existing Tesseract engine through OcrProvider.

The adapter owns the local orchestration (page rendering, table detection,
engine invocation) so that ``ocr_engine.py`` itself stays untouched.
"""

from __future__ import annotations

import threading
from pathlib import Path

import cv2

from averon_import.services.ocr.base import (
    OcrProviderError,
    OcrResult,
    OcrRow,
    PageOcrResult,
    ProgressCallback,
)
from averon_import.services.ocr_engine import TesseractOcrEngine
from averon_import.services.pdf_service import PdfService
from averon_import.services.table_detector import GostSpecificationDetector


class TesseractOcrAdapter:
    key = "tesseract"
    label = "Tesseract OCR"

    def __init__(
        self,
        pdf_service: PdfService,
        engine: TesseractOcrEngine | None = None,
        detector: GostSpecificationDetector | None = None,
    ):
        self.pdf_service = pdf_service
        self.engine = engine or TesseractOcrEngine(detector or GostSpecificationDetector())

    def available(self) -> bool:
        return bool(self.health().get("available"))

    def health(self) -> dict:
        return self.engine.health()

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
        if pages_dir is None:
            raise ValueError("TesseractOcrAdapter требует pages_dir для кэша страниц")
        dpi = dpi or 300
        mode = mode or "standard"
        total = len(pages)
        page_results: list[PageOcrResult] = []
        for index, page_number in enumerate(pages, start=1):
            if cancel is not None and cancel.is_set():
                raise OcrProviderError("Операция отменена")
            if progress:
                progress(index - 1, total, f"Подготовка страницы {page_number}")
            image_path = pages_dir / f"page-{page_number}-{dpi}.png"
            if not image_path.exists():
                self.pdf_service.render_page_to_path(pdf_path, page_number, image_path, dpi=dpi)
            image = cv2.imread(str(image_path))
            page_result = PageOcrResult(page=page_number)
            try:
                table = self.engine.detector.detect(image, crop=crop)
                raw_rows, geometry = self.engine.recognize_table(image, table, mode=mode)
                page_result.geometry = geometry
                page_result.rows = [
                    OcrRow(
                        source_row=raw["source_row"],
                        values=dict(raw["values"]),
                        confidences=dict(raw["confidences"]),
                        sources=dict(raw.get("ocr_sources", {})),
                        bbox=dict(raw["bbox"]),
                    )
                    for raw in raw_rows
                ]
            except Exception as exc:
                page_result.errors.append(str(exc))
            page_results.append(page_result)
            if progress:
                progress(index, total, f"Распознана страница {page_number}")
        return OcrResult(provider=self.key, pages=page_results)
