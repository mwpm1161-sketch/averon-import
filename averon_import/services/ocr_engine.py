from __future__ import annotations

import os
import re
import shutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from pytesseract import Output

from averon_import.core.constants import BASE_COLUMNS
from averon_import.core.normalizers import engineering_plausibility, normalize_cell
from averon_import.services.table_detector import DetectedTable, GostSpecificationDetector


class OcrUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class CellResult:
    text: str
    confidence: float
    source: str = "page"


class TesseractOcrEngine:
    """OCR adapter for Russian engineering GOST specification sheets.

    ``standard`` performs two page-level passes and is intended for fast review.
    ``accurate`` uses the official tessdata_best models, reads each table column
    independently and selectively retries doubtful cells after line removal and
    contrast enhancement. This avoids changing the table detector while making
    the text layer replaceable and extensible.
    """

    RUSSIAN_COLUMNS = {"name", "manufacturer", "unit", "note"}
    PACKAGE_DIR = Path(__file__).resolve().parent.parent
    DEFAULT_BEST_DIR = PACKAGE_DIR / "models" / "tessdata_best"
    USER_WORDS_PATH = PACKAGE_DIR / "models" / "engineering_words.txt"
    _TESSDATA_LOCK = threading.RLock()
    ACCURATE_THRESHOLDS = {
        "position": 55,
        "name": 62,
        "type_mark": 58,
        "code": 55,
        "manufacturer": 58,
        "unit": 78,
        "quantity": 82,
        "mass": 80,
        "note": 48,
    }

    def __init__(self, detector: GostSpecificationDetector):
        self.detector = detector
        self._configure_command()

    @classmethod
    def best_tessdata_dir(cls) -> Path:
        configured = os.environ.get("AVERON_BEST_TESSDATA_DIR", "").strip()
        return Path(configured).expanduser().resolve() if configured else cls.DEFAULT_BEST_DIR

    @staticmethod
    def _configure_command() -> None:
        configured = os.environ.get("TESSERACT_CMD", "").strip()
        candidates = [
            configured,
            shutil.which("tesseract") or "",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                pytesseract.pytesseract.tesseract_cmd = candidate
                return

    @classmethod
    def accurate_models_available(cls) -> bool:
        root = cls.best_tessdata_dir()
        return all((root / f"{language}.traineddata").exists() for language in ("rus", "eng"))

    @classmethod
    def health(cls) -> dict:
        cls._configure_command()
        try:
            version = str(pytesseract.get_tesseract_version()).splitlines()[0]
            languages = pytesseract.get_languages(config="")
            return {
                "available": True,
                "version": version,
                "languages": languages,
                "russian": "rus" in languages,
                "accurate_models": cls.accurate_models_available(),
                "accurate_models_dir": str(cls.best_tessdata_dir()),
            }
        except Exception as exc:  # pragma: no cover - environment dependent
            return {
                "available": False,
                "error": str(exc),
                "russian": False,
                "accurate_models": cls.accurate_models_available(),
                "accurate_models_dir": str(cls.best_tessdata_dir()),
            }

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
            raise OcrUnavailableError(health.get("error", "Tesseract не найден"))
        if not health.get("russian"):
            raise OcrUnavailableError(
                "В Tesseract отсутствует русский языковой пакет rus.traineddata"
            )
        if mode == "accurate" and not health.get("accurate_models"):
            raise OcrUnavailableError(
                "Точная OCR-модель не установлена. Запустите "
                "install_accurate_ocr_models.bat в папке Averon Import."
            )

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        horizontal, vertical = self.detector.line_masks(image)
        line_mask = cv2.bitwise_or(horizontal, vertical)
        line_mask = cv2.dilate(line_mask, np.ones((2, 2), np.uint8), iterations=1)
        clean = gray.copy()
        clean[line_mask > 0] = 255

        xs, ys = table.x_lines, table.y_lines
        if mode == "accurate":
            initial = self._standard_page_passes(clean, xs, ys, header_rows)
            accurate = self._accurate_column_passes(clean, xs, ys, header_rows)
            for location, candidates in accurate.items():
                initial.setdefault(location, []).extend(candidates)
        else:
            initial = self._standard_page_passes(clean, xs, ys, header_rows)

        rows: list[dict] = []
        refinement_budget = 8 if mode == "accurate" else 0
        for row_index in range(header_rows, len(ys) - 1):
            values: dict[str, str] = {}
            confidences: dict[str, float] = {}
            sources: dict[str, str] = {}
            for column_index, column in enumerate(BASE_COLUMNS):
                key = column["key"]
                candidates = initial.get((row_index, column_index), [])
                chosen = self._choose_candidates(key, candidates)

                needs_numeric = self._needs_numeric_fallback(key, chosen.text)
                needs_refine = (
                    mode == "accurate"
                    and refinement_budget > 0
                    and self._needs_accurate_refinement(key, chosen)
                )
                if needs_numeric or needs_refine:
                    fallback = self._recognize_cell(
                        clean,
                        xs[column_index],
                        ys[row_index],
                        xs[column_index + 1],
                        ys[row_index + 1],
                        key,
                        mode=mode,
                    )
                    chosen = self._choose_candidates(key, [chosen, fallback])
                    if needs_refine:
                        refinement_budget -= 1

                text = self._remove_noise(key, chosen.text, chosen.confidence)
                values[key] = text
                confidences[key] = round(chosen.confidence if text else 0.0, 1)
                sources[key] = chosen.source

            has_item_context = bool(
                values.get("name") or values.get("type_mark") or values.get("manufacturer")
            )
            if has_item_context and values.get("quantity") and not values.get("unit"):
                unit_index = self._column_index("unit")
                fallback = self._recognize_cell(
                    clean, xs[unit_index], ys[row_index], xs[unit_index + 1], ys[row_index + 1],
                    "unit", mode=mode,
                )
                if fallback.text:
                    values["unit"] = self._remove_noise("unit", fallback.text, fallback.confidence)
                    confidences["unit"] = round(fallback.confidence if values["unit"] else 0.0, 1)
                    sources["unit"] = fallback.source

            if has_item_context and values.get("unit") and not values.get("quantity"):
                quantity_index = self._column_index("quantity")
                fallback = self._recognize_cell(
                    clean,
                    xs[quantity_index], ys[row_index], xs[quantity_index + 1], ys[row_index + 1],
                    "quantity", mode=mode,
                )
                if fallback.text:
                    values["quantity"] = fallback.text
                    confidences["quantity"] = round(fallback.confidence, 1)
                    sources["quantity"] = fallback.source

            if not any(value.strip() for value in values.values()):
                continue
            x1, y1, x2, y2 = xs[0], ys[row_index], xs[-1], ys[row_index + 1]
            rows.append(
                {
                    "source_row": row_index,
                    "values": values,
                    "confidences": confidences,
                    "ocr_sources": sources,
                    "bbox": {
                        "x": x1 / table.image_width,
                        "y": y1 / table.image_height,
                        "width": (x2 - x1) / table.image_width,
                        "height": (y2 - y1) / table.image_height,
                    },
                }
            )

        return rows, {
            "x_lines": [round(x / table.image_width, 6) for x in xs],
            "y_lines": [round(y / table.image_height, 6) for y in ys],
            "column_count": table.column_count,
            "row_count": table.row_count,
            "ocr_mode": mode,
        }

    def _standard_page_passes(
        self, clean: np.ndarray, xs: list[int], ys: list[int], header_rows: int
    ) -> dict[tuple[int, int], list[CellResult]]:
        left, right = xs[0], xs[-1]
        first_data_line = ys[min(header_rows, len(ys) - 1)]
        bottom = ys[-1]
        crop = clean[first_data_line:bottom, left:right]
        output: dict[tuple[int, int], list[CellResult]] = {}
        for language, source in (("rus+eng", "page-mixed"), ("rus", "page-rus")):
            tokens = self._extract_words(
                crop, language, xs, ys, left, first_data_line, header_rows,
                extra_config="", source=source,
            )
            for location, cell_tokens in tokens.items():
                key = BASE_COLUMNS[location[1]]["key"]
                output.setdefault(location, []).append(self._cell_from_tokens(cell_tokens, key, source))
        return output

    def _accurate_column_passes(
        self, clean: np.ndarray, xs: list[int], ys: list[int], header_rows: int
    ) -> dict[tuple[int, int], list[CellResult]]:
        output: dict[tuple[int, int], list[CellResult]] = {}
        first_data_line = ys[min(header_rows, len(ys) - 1)]
        bottom = ys[-1]
        for column_index, column in enumerate(BASE_COLUMNS):
            key = column["key"]
            x1, x2 = xs[column_index], xs[column_index + 1]
            pad = 2
            column_crop = clean[first_data_line:bottom, max(0, x1 + pad):max(x1 + pad + 1, x2 - pad)]
            if column_crop.size == 0:
                continue
            scale = 1.55 if key in {"name", "type_mark", "manufacturer", "note"} else 2.0
            prepared = self._prepare_column(column_crop, scale=scale)
            language = self._language_for(key)
            config = self._base_config(mode="accurate") + " --psm 11 -c preserve_interword_spaces=1"
            data = self._image_to_data(
                prepared, lang=language, config=config, mode="accurate"
            )
            by_row: dict[int, list[tuple]] = {}
            count = len(data.get("text", []))
            for index in range(count):
                text = str(data["text"][index]).strip()
                if not text:
                    continue
                confidence = self._confidence(data["conf"][index])
                center_y = first_data_line + (
                    float(data["top"][index]) + float(data["height"][index]) / 2
                ) / scale
                row = int(np.searchsorted(ys, center_y) - 1)
                if not (header_rows <= row < len(ys) - 1):
                    continue
                by_row.setdefault(row, []).append(
                    (
                        int(data["block_num"][index]), int(data["par_num"][index]),
                        int(data["line_num"][index]), int(data["word_num"][index]),
                        text, confidence,
                    )
                )
            for row, tokens in by_row.items():
                output.setdefault((row, column_index), []).append(
                    self._cell_from_tokens(tokens, key, f"accurate-column-{key}")
                )
        return output

    def _extract_words(
        self,
        crop: np.ndarray,
        language: str,
        xs: list[int],
        ys: list[int],
        left: int,
        first_data_line: int,
        header_rows: int,
        extra_config: str,
        source: str,
    ) -> dict[tuple[int, int], list[tuple]]:
        data = self._image_to_data(
            crop,
            lang=language,
            config=(self._base_config("standard") + " --psm 6 -c preserve_interword_spaces=1 " + extra_config).strip(),
            mode="standard",
        )
        words_by_cell: dict[tuple[int, int], list[tuple]] = {}
        count = len(data.get("text", []))
        for index in range(count):
            text = str(data["text"][index]).strip()
            if not text:
                continue
            confidence = self._confidence(data["conf"][index])
            center_x = left + float(data["left"][index]) + float(data["width"][index]) / 2
            center_y = first_data_line + float(data["top"][index]) + float(data["height"][index]) / 2
            column = int(np.searchsorted(xs, center_x) - 1)
            row = int(np.searchsorted(ys, center_y) - 1)
            if not (0 <= column < len(BASE_COLUMNS)) or not (header_rows <= row < len(ys) - 1):
                continue
            words_by_cell.setdefault((row, column), []).append(
                (
                    int(data["block_num"][index]), int(data["par_num"][index]),
                    int(data["line_num"][index]), int(data["word_num"][index]),
                    text, confidence,
                )
            )
        return words_by_cell

    @staticmethod
    def _prepare_column(image: np.ndarray, scale: float) -> np.ndarray:
        enlarged = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        clahe = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8))
        enhanced = clahe.apply(enlarged)
        return cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    @staticmethod
    def _cell_from_tokens(tokens: list[tuple], key: str, source: str) -> CellResult:
        tokens = sorted(tokens, key=lambda token: token[:4])
        text = " ".join(token[4] for token in tokens)
        confidence = sum(token[5] for token in tokens) / len(tokens) if tokens else 0.0
        return CellResult(normalize_cell(key, text), confidence, source)

    def _choose_candidates(self, key: str, candidates: list[CellResult]) -> CellResult:
        valid = [candidate for candidate in candidates if candidate and candidate.text]
        if not valid:
            return CellResult("", 0.0, "none")
        def score(candidate: CellResult) -> float:
            source_bonus = 10.0 if candidate.source.startswith("cell-") else 2.0 if candidate.source.startswith("accurate-column") else 0.0
            return candidate.confidence + engineering_plausibility(key, candidate.text) + source_bonus
        return max(valid, key=score)

    def _needs_accurate_refinement(self, key: str, result: CellResult) -> bool:
        if not result.text:
            return key in {"unit", "quantity", "mass"}
        threshold = self.ACCURATE_THRESHOLDS.get(key, 55)
        if result.confidence < threshold:
            return True
        if key in {"quantity", "mass"}:
            return not bool(re.fullmatch(r"-?\d+(?:[.,]\d+)?", result.text))
        if key == "unit":
            return result.text not in {"шт.", "м", "м²", "м³", "кг", "компл.", "п.м.", "л", "к-т"}
        if key in {"name", "note"}:
            weird = re.findall(r"[^\w\s.,;:()\-+/×Ø№²³\"']", result.text)
            return len(weird) >= 2
        return False

    @staticmethod
    def _needs_numeric_fallback(key: str, text: str) -> bool:
        return key in {"quantity", "mass"} and bool(text) and not bool(
            re.fullmatch(r"-?\d+(?:[.,]\d+)?", text)
        )

    @staticmethod
    def _remove_noise(key: str, text: str, confidence: float) -> str:
        compact = text.strip().lower()
        if key not in {"quantity", "mass"} and compact in {"о", "o", "0", "_", "|", "."} and confidence < 72:
            return ""
        if key == "unit":
            allowed = {"шт.", "м", "м²", "м³", "кг", "компл.", "комплект", "п.м.", "л", "к-т"}
            if text.lower() not in allowed and len(text) <= 5:
                return ""
        if key == "position" and not re.search(r"[0-9IVXLСА-ЯA-Z]", text):
            return ""
        return text

    def _recognize_cell(
        self,
        gray: np.ndarray,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        key: str,
        mode: str,
    ) -> CellResult:
        padding = 3
        cell = gray[y1 + padding:y2 - padding, x1 + padding:x2 - padding]
        if cell.size == 0:
            return CellResult("", 0.0, "cell-empty")
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

        candidates: list[CellResult] = []
        for index, prepared in enumerate(variants, start=1):
            data = self._image_to_data(
                prepared, lang=language, config=config, mode="accurate"
            )
            tokens: list[str] = []
            confidences: list[float] = []
            for item_index in range(len(data.get("text", []))):
                token = str(data["text"][item_index]).strip()
                if not token:
                    continue
                tokens.append(token)
                confidences.append(self._confidence(data["conf"][item_index]))
            if tokens:
                text = normalize_cell(key, " ".join(tokens))
                confidence = sum(confidences) / len(confidences) if confidences else 0.0
                candidates.append(CellResult(text, confidence, f"cell-{mode}-{index}"))
        return self._choose_candidates(key, candidates)

    @staticmethod
    def _prepare_cell_variant(image: np.ndarray, kind: str) -> np.ndarray:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(image)
        if kind == "adaptive":
            return cv2.adaptiveThreshold(
                enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 11,
            )
        return cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    def _base_config(self, mode: str) -> str:
        # Do not pass --tessdata-dir through pytesseract's config string on
        # Windows. pytesseract uses shlex(posix=False), which keeps quote
        # characters in the argument and makes Tesseract look for a path such
        # as tessdata_best"/rus.traineddata. The accurate model directory is
        # supplied through TESSDATA_PREFIX in _tessdata_environment instead.
        return "--oem 1"

    def _image_to_data(
        self, image: np.ndarray, *, lang: str, config: str, mode: str
    ) -> dict:
        with self._tessdata_environment(mode):
            return pytesseract.image_to_data(
                image, lang=lang, config=config, output_type=Output.DICT
            )

    @contextmanager
    def _tessdata_environment(self, mode: str):
        # Environment variables are inherited by the Tesseract subprocess. A
        # process-wide lock keeps standard and accurate OCR calls from seeing
        # each other's temporary TESSDATA_PREFIX value. Never include quotes in
        # the environment value; Windows passes them as literal path symbols.
        with self._TESSDATA_LOCK:
            previous = os.environ.get("TESSDATA_PREFIX")
            try:
                if mode == "accurate":
                    os.environ["TESSDATA_PREFIX"] = str(self.best_tessdata_dir())
                yield
            finally:
                if previous is None:
                    os.environ.pop("TESSDATA_PREFIX", None)
                else:
                    os.environ["TESSDATA_PREFIX"] = previous

    @staticmethod
    def _language_for(key: str) -> str:
        if key in {"quantity", "mass"}:
            return "eng"
        if key == "unit":
            return "rus"
        if key in {"name", "note"}:
            return "rus+eng"
        return "eng+rus"

    @staticmethod
    def _column_index(key: str) -> int:
        return next(index for index, column in enumerate(BASE_COLUMNS) if column["key"] == key)

    @staticmethod
    def _confidence(value) -> float:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0
