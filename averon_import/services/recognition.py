from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Callable

import cv2

from averon_import.core.constants import BASE_COLUMNS
from averon_import.core.review import critical_confidence
from averon_import.ocr.base import OcrProvider
from averon_import.services.ocr_engine import TesseractOcrEngine
from averon_import.services.pdf_service import PdfService
from averon_import.services.table_detector import (
    GostSpecificationDetector,
    TableDetectionError,
)

ProgressCallback = Callable[[int, int, str], None]


class RecognitionService:
    SECTION_WORDS = (
        "вентиляц",
        "кондиционир",
        "отоплен",
        "теплоснабжен",
        "холодоснабжен",
    )
    SYSTEM_RE = re.compile(r"^(?:[ПВКЕВBPK]{1,4}\s*\d+(?:[.,]\d+)?|К\d+(?:\.\d+)*)$", re.I)

    def __init__(
        self,
        pdf_service: PdfService,
        *,
        detector: GostSpecificationDetector | None = None,
        ocr: OcrProvider | None = None,
    ):
        self.pdf_service = pdf_service
        self.detector = detector or GostSpecificationDetector()
        self.ocr: OcrProvider = ocr or TesseractOcrEngine(self.detector)

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
            if image is None:
                errors.append({"page": page_number, "error": "Не удалось прочитать изображение страницы"})
                progress(page_number, page_count, f"Проверена страница {page_number}")
                continue
            try:
                table = self.detector.detect(image)
                if table.column_count == 9 and table.row_count >= 5:
                    candidates.append(page_number)
            except TableDetectionError:
                pass
            except Exception as exc:
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
        current_section = ""
        current_system = ""
        component_block_active = False

        total = len(pages)
        for index, page_number in enumerate(pages, start=1):
            progress(index - 1, total, f"Подготовка страницы {page_number}")
            image_path = pages_dir / f"page-{page_number}-{dpi}.png"
            if not image_path.exists():
                self.pdf_service.render_page_to_path(
                    pdf_path, page_number, image_path, dpi=dpi
                )
            image = cv2.imread(str(image_path))
            try:
                table = self.detector.detect(image, crop=crop)
                raw_rows, geometry = self.ocr.recognize_table(image, table, mode=ocr_mode)
                raw_rows = self._repair_continuation_rows(raw_rows)
                page_tables[str(page_number)] = geometry
                for raw in raw_rows:
                    values = raw["values"]
                    row_type = self._classify_row(values)
                    name = values.get("name", "").strip()
                    position = values.get("position", "").strip()
                    nonempty_fields = [
                        key for key, value in values.items() if str(value).strip()
                    ]
                    if (
                        row_type == "note"
                        and current_section
                        and len(nonempty_fields) == 1
                        and nonempty_fields[0] in {"name", "position"}
                        and len(name or position) <= 5
                    ):
                        row_type = "system"

                    has_independent_amount = bool(
                        values.get("unit") or values.get("quantity") or values.get("manufacturer")
                    )
                    if component_block_active and not has_independent_amount and row_type not in {"section", "system"}:
                        if values.get("name") or values.get("type_mark"):
                            row_type = "component"
                    if component_block_active and has_independent_amount:
                        component_block_active = False
                    if row_type == "item" and "компл" in name.lower() and values.get("quantity"):
                        component_block_active = True

                    if row_type == "section":
                        current_section = name.rstrip(":*") or position
                        current_system = ""
                    elif row_type == "system":
                        current_system = name or position

                    confidence_values = [
                        value
                        for key, value in raw["confidences"].items()
                        if values.get(key, "").strip()
                    ]
                    confidence = (
                        round(sum(confidence_values) / len(confidence_values), 1)
                        if confidence_values
                        else 0.0
                    )
                    status = self._status_for(values, confidence, row_type)
                    if row_type == "system":
                        system_text = (name or position).replace(" ", "")
                        if not self.SYSTEM_RE.fullmatch(system_text):
                            status = "review"
                    evidence = {
                        key: {
                            "raw_text": raw.get("raw_values", {}).get(key, values.get(key, "")),
                            "normalized_text": values.get(key, ""),
                            "final_text": values.get(key, ""),
                            "confidence": raw.get("confidences", {}).get(key, 0.0),
                            "source": raw.get("ocr_sources", {}).get(key, "none"),
                        }
                        for key in (column["key"] for column in BASE_COLUMNS)
                    }
                    row = {
                        "id": uuid.uuid4().hex,
                        **values,
                        "section": current_section,
                        "system": current_system,
                        "row_type": row_type,
                        "page": page_number,
                        "confidence": confidence,
                        "critical_confidence": critical_confidence(values, raw["confidences"]),
                        "status": status,
                        "bbox": raw["bbox"],
                        "confidences": raw["confidences"],
                        "ocr_sources": raw.get("ocr_sources", {}),
                        "ocr_evidence": evidence,
                        "source_row": raw["source_row"],
                        "edited": False,
                    }
                    all_rows.append(row)
            except Exception as exc:
                errors.append({"page": page_number, "error": str(exc)})
            progress(index, total, f"Распознана страница {page_number}")

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

        return {
            "pages": pages,
            "rows": all_rows,
            "page_tables": page_tables,
            "errors": errors,
            "summary": self._summary(all_rows, errors),
            "ocr_mode": ocr_mode,
        }

    @staticmethod
    def _repair_continuation_rows(raw_rows: list[dict]) -> list[dict]:
        repaired: list[dict] = []
        for current in raw_rows:
            values = current.get("values", {})
            name = str(values.get("name", "")).strip()
            independent = any(str(values.get(key, "")).strip() for key in (
                "position", "type_mark", "code", "manufacturer", "unit", "quantity", "mass"
            ))
            starts_component = bool(re.match(r"^[\-–—•]", name))
            secondary_only = (
                not name
                and any(str(values.get(key, "")).strip() for key in ("type_mark", "code", "note"))
            )
            starts_lower = bool(name and name[:1].islower())
            previous_punct = bool(
                repaired
                and str(repaired[-1].get("values", {}).get("name", "")).rstrip().endswith((",", ";", "-"))
            )
            should_merge = bool(
                repaired and not independent and not starts_component
                and (secondary_only or starts_lower or previous_punct)
            )
            if not should_merge:
                repaired.append(current)
                continue

            previous = repaired[-1]
            for key, value in values.items():
                value = str(value).strip()
                if not value:
                    continue
                old = str(previous["values"].get(key, "")).strip()
                previous["values"][key] = f"{old} {value}".strip()
                old_conf = float(previous.get("confidences", {}).get(key, 0) or 0)
                new_conf = float(current.get("confidences", {}).get(key, 0) or 0)
                previous.setdefault("confidences", {})[key] = round(
                    (old_conf + new_conf) / (2 if old_conf and new_conf else 1), 1
                )
                raw_value = str(current.get("raw_values", {}).get(key, "")).strip()
                if raw_value:
                    old_raw = str(previous.setdefault("raw_values", {}).get(key, "")).strip()
                    previous["raw_values"][key] = f"{old_raw} {raw_value}".strip()
            first_box = previous.get("bbox", {})
            second_box = current.get("bbox", {})
            if first_box and second_box:
                bottom = max(
                    first_box.get("y", 0) + first_box.get("height", 0),
                    second_box.get("y", 0) + second_box.get("height", 0),
                )
                first_box["height"] = bottom - first_box.get("y", 0)
            previous["source_row"] = f"{previous.get('source_row')}+{current.get('source_row')}"
        return repaired

    def _classify_row(self, values: dict[str, str]) -> str:
        name = values.get("name", "").strip()
        position = values.get("position", "").strip()
        quantity = values.get("quantity", "").strip()
        unit = values.get("unit", "").strip()
        type_mark = values.get("type_mark", "").strip()
        manufacturer = values.get("manufacturer", "").strip()

        low = name.lower()
        if any(word in low for word in self.SECTION_WORDS) and not quantity:
            return "section"
        if (
            self.SYSTEM_RE.fullmatch(name.replace(" ", ""))
            or self.SYSTEM_RE.fullmatch(position.replace(" ", ""))
        ) and not quantity and not unit:
            return "system"
        if re.match(r"^[\-–—•]", name):
            return "component"
        if quantity or unit or type_mark or manufacturer:
            return "item"
        if name or position:
            return "note"
        return "skip"

    @staticmethod
    def _status_for(values: dict[str, str], confidence: float, row_type: str) -> str:
        if row_type in {"section", "system", "note", "component"}:
            return "recognized" if confidence >= 55 else "review"
        critical_present = bool(values.get("name")) and bool(
            values.get("quantity") or values.get("unit")
        )
        if not critical_present:
            return "review" if values.get("name") else "unrecognized"
        return "recognized" if confidence >= 68 else "review"

    @staticmethod
    def _summary(rows: list[dict], errors: list[dict]) -> dict:
        status_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for row in rows:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
            type_counts[row["row_type"]] = type_counts.get(row["row_type"], 0) + 1
        return {
            "total_rows": len(rows),
            "status_counts": status_counts,
            "type_counts": type_counts,
            "page_errors": len(errors),
        }
