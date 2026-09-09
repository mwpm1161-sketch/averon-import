from __future__ import annotations

from copy import deepcopy
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
                # Page suggestion is intentionally schema-neutral.  The
                # provider/reconstruction safety gate decides whether a
                # detected table is a supported specification later.
                if table.column_count >= 3 and table.row_count >= 5:
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
        page_statuses: dict[str, dict] = {}
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
            page_rows: list[dict] = []
            assembler_state = deepcopy(getattr(assembler, "__dict__", {}))
            try:
                assembler.begin_page(page_number)
                page_tables[str(page_number)] = page_result.geometry or {}
                page_statuses[str(page_number)] = dict(page_result.page_status or {})
                status_diagnostics = (
                    page_statuses[str(page_number)].get("diagnostics")
                    if isinstance(page_statuses[str(page_number)], dict)
                    else {}
                )
                semantic_authoritative = bool(
                    ocr_result.provider == "yandex_vision"
                    and isinstance(status_diagnostics, dict)
                    and status_diagnostics.get("semantic_authoritative")
                ) or (
                    ocr_result.provider == "yandex_vision"
                    and any(
                        isinstance(row.metadata, dict)
                        and row.metadata.get("semantic_authoritative")
                        for row in page_result.rows
                    )
                )
                if page_result.errors and not page_statuses[str(page_number)]:
                    page_statuses[str(page_number)] = {
                        "page": page_number,
                        "layout_status": "FAILED",
                        "schema_status": "UNKNOWN",
                        "output_status": "NO_SPEC_OUTPUT",
                        "blockers": ["ocr_page_error"],
                        "diagnostics": {"errors": list(page_result.errors)},
                    }
                if semantic_authoritative:
                    for raw in page_result.rows:
                        page_rows.append(
                            assembler.build_semantic_row(page_number, raw)
                        )
                else:
                    raw_rows = assembler.prepare(page_result.rows)
                    for raw in raw_rows:
                        page_rows.append(assembler.build_row(page_number, raw))
                all_rows.extend(page_rows)
            except Exception as exc:
                # A failed page is atomic: neither partially built rows nor
                # assembler context from that page may leak into the result.
                page_rows.clear()
                if hasattr(assembler, "__dict__"):
                    assembler.__dict__.clear()
                    assembler.__dict__.update(assembler_state)
                existing_status = dict(page_statuses.get(str(page_number)) or {})
                existing_status.update({
                    "page": page_number,
                    "output_status": "NO_SPEC_OUTPUT",
                })
                existing_status.setdefault("layout_status", "FAILED")
                existing_status.setdefault("schema_status", "UNKNOWN")
                blockers = list(existing_status.get("blockers") or [])
                if "assembly_error" not in blockers:
                    blockers.append("assembly_error")
                existing_status["blockers"] = blockers
                diagnostics = existing_status.get("diagnostics")
                diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
                diagnostics["assembly_error"] = str(exc)[:500]
                existing_status["diagnostics"] = diagnostics
                page_statuses[str(page_number)] = existing_status
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
            "page_statuses": page_statuses,
            "errors": errors,
            "summary": self._summary(all_rows, errors),
            "ocr_mode": ocr_mode,
            "ocr_stats": ocr_stats,
        }

    @staticmethod
    def _summary(rows: list[dict], errors: list[dict]) -> dict:
        return SpecificationRowAssembler.summary(rows, errors)
