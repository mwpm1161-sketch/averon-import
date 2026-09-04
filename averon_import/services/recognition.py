from __future__ import annotations

from pathlib import Path
from typing import Callable

import cv2

from averon_import.services.ocr import OcrProvider, TesseractOcrAdapter
from averon_import.services.pdf_service import PdfService
from averon_import.services.row_assembler import SpecificationRowAssembler
from averon_import.services.review_policy import critical_field_count
from averon_import.services.table_detector import GostSpecificationDetector

ProgressCallback = Callable[[int, int, str], None]


class RecognitionService:
    def __init__(self, pdf_service: PdfService, ocr: OcrProvider | None = None):
        self.pdf_service = pdf_service
        self.detector = GostSpecificationDetector()
        self.ocr: OcrProvider = ocr or TesseractOcrAdapter(pdf_service)


    def suggest_pages(
        self,
        pdf_path: Path,
        pages_dir: Path,
        page_count: int,
        progress: ProgressCallback,
        dpi: int = 90,
    ) -> dict:
        candidates: list[int] = []
        errors: list[dict] = []
        for page_number in range(1, page_count + 1):
            progress(page_number - 1, page_count, f"Анализ страницы {page_number}")
            image_path = pages_dir / f"page-{page_number}-{dpi}.png"
            if not image_path.exists():
                self.pdf_service.render_page_to_path(pdf_path, page_number, image_path, dpi=dpi)
            image = cv2.imread(str(image_path))
            try:
                table = self.detector.detect(image)
                if table.column_count == 9 and table.row_count >= 5:
                    candidates.append(page_number)
            except Exception as exc:
                # A page without a matching table is normal and is not treated
                # as a user-facing error. Keep only unexpected image failures.
                if image is None:
                    errors.append({"page": page_number, "error": str(exc)})
            progress(page_number, page_count, f"Проверена страница {page_number}")
        return {"pages": candidates, "errors": errors}

    def recognize(
        self,
        pdf_path: Path,
        pages_dir: Path,
        pages: list[int],
        crop: dict | None,
        dpi: int,
        progress: ProgressCallback,
        ocr_mode: str = "standard",
    ) -> dict:
        all_rows: list[dict] = []
        page_tables: dict[str, dict] = {}
        errors: list[dict] = []
        assembler = SpecificationRowAssembler()
        pages = sorted({int(page) for page in pages})
        if not pages:
            raise ValueError("Не выбраны страницы для распознавания.")

        ocr_result = self.ocr.recognize(
            pdf_path,
            pages,
            pages_dir=pages_dir,
            dpi=dpi,
            crop=crop,
            mode=ocr_mode,
            progress=progress,
        )
        total = len(pages)
        for page_result in ocr_result.pages:
            page_number = page_result.page
            try:
                assembler.begin_page(page_number)
                raw_rows = assembler.prepare(page_result.rows)
                page_tables[str(page_number)] = page_result.geometry or {}
                for raw in raw_rows:
                    all_rows.append(assembler.build_row(page_number, raw))
            except Exception as exc:
                errors.append({"page": page_number, "error": str(exc)})
            for message in page_result.errors:
                errors.append({"page": page_number, "error": message})

        if errors and not all_rows and len(errors) == total:
            unique_errors = []
            for item in errors:
                message = str(item.get("error", "Неизвестная ошибка")).strip()
                if message and message not in unique_errors:
                    unique_errors.append(message)
            detail = "; ".join(unique_errors[:3])
            raise RuntimeError(
                "Не удалось обработать ни одной выбранной страницы. "
                f"Причина: {detail or 'неизвестная ошибка'}"
            )

        ocr_stats = dict(getattr(ocr_result, "stats", {}) or {})
        ocr_stats["unresolved_critical"] = sum(
            critical_field_count(row) for row in all_rows
        )
        return {
            "pages": pages,
            "rows": all_rows,
            "page_tables": page_tables,
            "errors": errors,
            "summary": self._summary(all_rows, errors),
            "ocr_mode": ocr_mode,
            "ocr_stats": ocr_stats,
        }

    @staticmethod
    def _summary(rows: list[dict], errors: list[dict]) -> dict:
        return SpecificationRowAssembler.summary(rows, errors)
