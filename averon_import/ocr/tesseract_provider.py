from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from averon_import.core.constants import BASE_COLUMNS
from averon_import.core.normalizers import normalize_cell
from averon_import.services.ocr_engine import CellResult, TesseractOcrEngine
from averon_import.services.table_detector import DetectedTable, GostSpecificationDetector


@dataclass(slots=True)
class EvidenceCellResult(CellResult):
    """Tesseract candidate with the source text kept before normalization."""

    raw_text: str = ""


class EvidenceTesseractOcrProvider(TesseractOcrEngine):
    """Tesseract provider that preserves raw OCR text for later AI review.

    The legacy engine remains untouched so this boundary can be introduced
    without changing its proven preprocessing/candidate-selection behavior.
    """

    def __init__(self, detector: GostSpecificationDetector):
        super().__init__(detector)

    @staticmethod
    def _cell_from_tokens(tokens: list[tuple], key: str, source: str) -> EvidenceCellResult:
        tokens = sorted(tokens, key=lambda token: token[:4])
        raw_text = " ".join(token[4] for token in tokens)
        confidence = sum(token[5] for token in tokens) / len(tokens) if tokens else 0.0
        return EvidenceCellResult(
            text=normalize_cell(key, raw_text),
            confidence=confidence,
            source=source,
            raw_text=raw_text,
        )

    def _recognize_cell(
        self,
        gray: np.ndarray,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        key: str,
        mode: str,
    ) -> EvidenceCellResult:
        padding = 3
        cell = gray[y1 + padding:y2 - padding, x1 + padding:x2 - padding]
        if cell.size == 0:
            return EvidenceCellResult("", 0.0, "cell-empty", "")
        scale = 4.5 if key in {"unit", "quantity", "mass", "position"} else 3.0
        enlarged = cv2.resize(cell, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        variants = [self._prepare_cell_variant(enlarged, "otsu")]
        if mode == "accurate" and key in {"name", "type_mark", "manufacturer", "note"}:
            variants.append(self._prepare_cell_variant(enlarged, "adaptive"))

        language = self._language_for(key)
        psm = 6 if key in {"name", "note"} else 7
        config = self._base_config(mode) + f" --psm {psm}"
        if key in {"quantity", "mass"}:
            config += " -c tessedit_char_whitelist=0123456789.,-"
        elif key == "unit":
            config += " -c tessedit_char_whitelist=штмкгкомпл.²³-п"

        candidates: list[EvidenceCellResult] = []
        for index, prepared in enumerate(variants, start=1):
            data = self._image_to_data(prepared, lang=language, config=config, mode="accurate")
            tokens: list[str] = []
            confidences: list[float] = []
            for item_index in range(len(data.get("text", []))):
                token = str(data["text"][item_index]).strip()
                if not token:
                    continue
                tokens.append(token)
                confidences.append(self._confidence(data["conf"][item_index]))
            if tokens:
                raw_text = " ".join(tokens)
                candidates.append(
                    EvidenceCellResult(
                        normalize_cell(key, raw_text),
                        sum(confidences) / len(confidences) if confidences else 0.0,
                        f"cell-{mode}-{index}",
                        raw_text,
                    )
                )
        chosen = self._choose_candidates(key, candidates)
        if isinstance(chosen, EvidenceCellResult):
            return chosen
        return EvidenceCellResult(chosen.text, chosen.confidence, chosen.source, chosen.text)

    def recognize_table(
        self,
        image: np.ndarray,
        table: DetectedTable,
        header_rows: int = 2,
        mode: str = "standard",
    ) -> tuple[list[dict], dict]:
        if mode not in {"standard", "accurate"}:
            raise ValueError(f"Неизвестный режим OCR: {mode}")
        health = self.health()
        if not health.get("available"):
            from averon_import.services.ocr_engine import OcrUnavailableError
            raise OcrUnavailableError(health.get("error", "Tesseract не найден"))
        if not health.get("russian"):
            from averon_import.services.ocr_engine import OcrUnavailableError
            raise OcrUnavailableError("В Tesseract отсутствует русский языковой пакет rus.traineddata")
        if mode == "accurate" and not health.get("accurate_models"):
            from averon_import.services.ocr_engine import OcrUnavailableError
            raise OcrUnavailableError(
                "Точная OCR-модель не установлена. Запустите install_accurate_ocr_models.bat в папке Averon Import."
            )

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        horizontal, vertical = self.detector.line_masks(image)
        line_mask = cv2.bitwise_or(horizontal, vertical)
        line_mask = cv2.dilate(line_mask, np.ones((2, 2), np.uint8), iterations=1)
        clean = gray.copy()
        clean[line_mask > 0] = 255

        xs, ys = table.x_lines, table.y_lines
        initial = self._standard_page_passes(clean, xs, ys, header_rows)
        if mode == "accurate":
            accurate = self._accurate_column_passes(clean, xs, ys, header_rows)
            for location, candidates in accurate.items():
                initial.setdefault(location, []).extend(candidates)

        rows: list[dict] = []
        refinement_budget = 8 if mode == "accurate" else 0
        for row_index in range(header_rows, len(ys) - 1):
            values: dict[str, str] = {}
            raw_values: dict[str, str] = {}
            confidences: dict[str, float] = {}
            sources: dict[str, str] = {}
            for column_index, column in enumerate(BASE_COLUMNS):
                key = column["key"]
                candidates = initial.get((row_index, column_index), [])
                chosen = self._choose_candidates(key, candidates)
                raw_text = getattr(chosen, "raw_text", "") or chosen.text

                needs_numeric = self._needs_numeric_fallback(key, chosen.text)
                needs_refine = (
                    mode == "accurate"
                    and refinement_budget > 0
                    and self._needs_accurate_refinement(key, chosen)
                )
                if needs_numeric or needs_refine:
                    fallback = self._recognize_cell(
                        clean,
                        xs[column_index], ys[row_index],
                        xs[column_index + 1], ys[row_index + 1],
                        key, mode=mode,
                    )
                    chosen = self._choose_candidates(key, [chosen, fallback])
                    raw_text = getattr(chosen, "raw_text", "") or chosen.text
                    if needs_refine:
                        refinement_budget -= 1

                text = self._remove_noise(key, chosen.text, chosen.confidence)
                values[key] = text
                raw_values[key] = raw_text
                confidences[key] = round(chosen.confidence if text else 0.0, 1)
                sources[key] = chosen.source

            has_item_context = bool(
                values.get("name") or values.get("type_mark") or values.get("manufacturer")
            )
            if has_item_context and values.get("quantity") and not values.get("unit"):
                index = self._column_index("unit")
                fallback = self._recognize_cell(
                    clean, xs[index], ys[row_index], xs[index + 1], ys[row_index + 1],
                    "unit", mode=mode,
                )
                if fallback.text:
                    values["unit"] = self._remove_noise("unit", fallback.text, fallback.confidence)
                    raw_values["unit"] = fallback.raw_text or fallback.text
                    confidences["unit"] = round(fallback.confidence if values["unit"] else 0.0, 1)
                    sources["unit"] = fallback.source

            if has_item_context and values.get("unit") and not values.get("quantity"):
                index = self._column_index("quantity")
                fallback = self._recognize_cell(
                    clean, xs[index], ys[row_index], xs[index + 1], ys[row_index + 1],
                    "quantity", mode=mode,
                )
                if fallback.text:
                    values["quantity"] = fallback.text
                    raw_values["quantity"] = fallback.raw_text or fallback.text
                    confidences["quantity"] = round(fallback.confidence, 1)
                    sources["quantity"] = fallback.source

            if not any(value.strip() for value in values.values()):
                continue
            x1, y1, x2, y2 = xs[0], ys[row_index], xs[-1], ys[row_index + 1]
            rows.append({
                "source_row": row_index,
                "values": values,
                "raw_values": raw_values,
                "confidences": confidences,
                "ocr_sources": sources,
                "bbox": {
                    "x": x1 / table.image_width,
                    "y": y1 / table.image_height,
                    "width": (x2 - x1) / table.image_width,
                    "height": (y2 - y1) / table.image_height,
                },
            })

        return rows, {
            "x_lines": [round(x / table.image_width, 6) for x in xs],
            "y_lines": [round(y / table.image_height, 6) for y in ys],
            "column_count": table.column_count,
            "row_count": table.row_count,
            "ocr_mode": mode,
        }
