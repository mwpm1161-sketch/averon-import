from __future__ import annotations

import cv2
import numpy as np

from averon_import.services.pdf_service import PdfService
from averon_import.services.recognition import RecognitionService
from averon_import.services.table_detector import TableDetectionError


def _raw_row(values: dict[str, str], source_row: int) -> dict:
    full = {
        "position": "", "name": "", "type_mark": "", "code": "",
        "manufacturer": "", "unit": "", "quantity": "", "mass": "", "note": "",
    }
    full.update(values)
    return {
        "values": full,
        "confidences": {key: 95.0 if value else 0.0 for key, value in full.items()},
        "ocr_sources": {key: "test" for key in full},
        "bbox": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 0.1},
        "source_row": source_row,
    }


def test_lowercase_continuation_is_merged():
    rows = [
        _raw_row({"name": "Клапан воздушный"}, 2),
        _raw_row({"name": "с электроприводом"}, 3),
    ]
    repaired = RecognitionService._repair_continuation_rows(rows)
    assert len(repaired) == 1
    assert repaired[0]["values"]["name"] == "Клапан воздушный с электроприводом"


def test_new_section_clears_previous_system(tmp_path):
    image_path = tmp_path / "page-1-300.png"
    cv2.imwrite(str(image_path), np.full((20, 20, 3), 255, dtype=np.uint8))
    service = RecognitionService(PdfService())

    class Detector:
        def detect(self, image, crop=None):
            return object()

    class Ocr:
        def recognize_table(self, image, table, mode="standard"):
            return [
                _raw_row({"name": "Вентиляция"}, 2),
                _raw_row({"name": "П1"}, 3),
                _raw_row({"name": "Вентилятор", "unit": "шт.", "quantity": "1"}, 4),
                _raw_row({"name": "Отопление"}, 5),
                _raw_row({"name": "Радиатор", "unit": "шт.", "quantity": "2"}, 6),
            ], {"column_count": 9, "row_count": 6}

    service.detector = Detector()
    service.ocr = Ocr()
    result = service.recognize(
        tmp_path / "unused.pdf", tmp_path, [1], None, 300, lambda *_: None
    )
    radiator = next(row for row in result["rows"] if row["name"] == "Радиатор")
    assert radiator["section"] == "Отопление"
    assert radiator["system"] == ""


def test_suggest_pages_suppresses_expected_detection_miss(tmp_path):
    image_path = tmp_path / "page-1-90.png"
    cv2.imwrite(str(image_path), np.full((20, 20, 3), 255, dtype=np.uint8))
    service = RecognitionService(PdfService())

    class Detector:
        def detect(self, image):
            raise TableDetectionError("not a specification")

    service.detector = Detector()
    result = service.suggest_pages(tmp_path / "unused.pdf", tmp_path, 1, lambda *_: None)
    assert result == {"pages": [], "errors": []}


def test_suggest_pages_reports_unexpected_detector_failure(tmp_path):
    image_path = tmp_path / "page-1-90.png"
    cv2.imwrite(str(image_path), np.full((20, 20, 3), 255, dtype=np.uint8))
    service = RecognitionService(PdfService())

    class Detector:
        def detect(self, image):
            raise RuntimeError("boom")

    service.detector = Detector()
    result = service.suggest_pages(tmp_path / "unused.pdf", tmp_path, 1, lambda *_: None)
    assert result["pages"] == []
    assert result["errors"] == [{"page": 1, "error": "boom"}]
