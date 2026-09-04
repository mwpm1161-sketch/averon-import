from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

from averon_import.services.ocr import (
    OcrProviderError,
    OcrResult,
    OcrRow,
    PageOcrResult,
    TesseractOcrAdapter,
    create_ocr_provider,
)
from averon_import.services.pdf_service import PdfService
from averon_import.services.recognition import RecognitionService


def _write_png(path: Path) -> None:
    image = np.full((40, 60, 3), 255, dtype=np.uint8)
    cv2.imwrite(str(path), image)


class FakeEngine:
    def __init__(self, rows=None, geometry=None, error: Exception | None = None):
        self.detector = _FakeDetector()
        self.rows = rows if rows is not None else [_raw_row()]
        self.geometry = geometry or {"x_lines": [0.0], "ocr_mode": "standard"}
        self.error = error
        self.calls: list[dict] = []

    def recognize_table(self, image, table, header_rows=2, mode="standard"):
        self.calls.append({"mode": mode, "image_shape": None if image is None else image.shape})
        if self.error is not None:
            raise self.error
        return self.rows, dict(self.geometry)

    def health(self) -> dict:
        return {"available": True, "russian": True}


class _FakeDetector:
    def __init__(self):
        self.seen_crops: list = []

    def detect(self, image, crop=None):
        self.seen_crops.append(crop)
        return object()


class FakePdfService:
    def __init__(self):
        self.rendered: list[tuple[int, int]] = []

    def render_page_to_path(self, pdf_path, page_number, output_path, dpi=220):
        self.rendered.append((page_number, dpi))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _write_png(output_path)
        return output_path


def _raw_row() -> dict:
    return {
        "source_row": 2,
        "values": {"position": "1", "name": "Вентиль", "quantity": "5"},
        "confidences": {"position": 90.0, "name": 80.0, "quantity": 70.0},
        "ocr_sources": {"position": "page-mixed", "name": "page-rus"},
        "bbox": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
    }


def test_adapter_maps_engine_output_to_dto(tmp_path):
    adapter = TesseractOcrAdapter(FakePdfService(), engine=FakeEngine())
    result = adapter.recognize(
        Path("doc.pdf"), [1],
        pages_dir=tmp_path, dpi=300, crop={"passed": "crop"}, mode="accurate",
    )
    assert isinstance(result, OcrResult)
    assert result.provider == "tesseract"
    page = result.page(1)
    assert page.geometry == {"x_lines": [0.0], "ocr_mode": "standard"}
    raw = page.as_raw_rows()[0]
    assert set(raw) == {"source_row", "values", "confidences", "ocr_sources", "bbox"}
    assert raw["values"]["name"] == "Вентиль"
    assert raw["ocr_sources"]["position"] == "page-mixed"
    assert adapter.engine.detector.seen_crops == [{"passed": "crop"}]


def test_adapter_renders_only_missing_pages(tmp_path):
    pdf_service = FakePdfService()
    pre_rendered = tmp_path / "page-1-300.png"
    _write_png(pre_rendered)
    adapter = TesseractOcrAdapter(pdf_service, engine=FakeEngine())
    adapter.recognize(Path("doc.pdf"), [1, 2], pages_dir=tmp_path, dpi=300)
    assert pdf_service.rendered == [(2, 300)]


def test_adapter_progress_messages_match_legacy_flow(tmp_path):
    events: list[tuple] = []
    adapter = TesseractOcrAdapter(FakePdfService(), engine=FakeEngine())
    adapter.recognize(
        Path("doc.pdf"), [3, 7], pages_dir=tmp_path,
        progress=lambda current, total, message: events.append((current, total, message)),
    )
    assert events == [
        (0, 2, "Подготовка страницы 3"),
        (1, 2, "Распознана страница 3"),
        (1, 2, "Подготовка страницы 7"),
        (2, 2, "Распознана страница 7"),
    ]


def test_adapter_isolates_page_errors(tmp_path):
    engine = FakeEngine(error=RuntimeError("Tesseract не найден"))
    adapter = TesseractOcrAdapter(FakePdfService(), engine=engine)
    result = adapter.recognize(Path("doc.pdf"), [1, 2], pages_dir=tmp_path)
    assert result.pages[0].errors == ["Tesseract не найден"]
    assert result.pages[0].rows == []
    assert result.pages[1].errors == ["Tesseract не найден"]


def test_adapter_cancel_between_pages(tmp_path):
    cancel = threading.Event()
    cancel.set()
    adapter = TesseractOcrAdapter(FakePdfService(), engine=FakeEngine())
    with pytest.raises(OcrProviderError):
        adapter.recognize(Path("doc.pdf"), [1], pages_dir=tmp_path, cancel=cancel)


def test_recognition_service_accepts_injected_provider():
    class StubProvider:
        key = "stub"

        def recognize(self, pdf_path, pages, **kwargs):
            return OcrResult(provider=self.key, stats={
                "primary_requests": 2,
                "secondary_requests": 1,
            }, pages=[
                PageOcrResult(page=p, rows=[OcrRow(
                    source_row=index,
                    values={"position": str(index), "name": "Насос", "quantity": "2"},
                    confidences={"position": 90.0, "name": 88.0, "quantity": 95.0},
                    sources={}, bbox={},
                )])
                for index, p in enumerate(pages)
            ])

    service = RecognitionService(FakePdfService(), ocr=StubProvider())
    progress: list[str] = []
    result = service.recognize(
        Path("doc.pdf"), Path("pages"), [1, 2], None, 300,
        lambda c, t, m: progress.append(m), ocr_mode="standard",
    )
    assert len(result["rows"]) == 2
    first = result["rows"][0]
    assert first["row_type"] == "item" and first["page"] == 1
    assert first["status"] in {"recognized", "review"} and first["edited"] is False
    assert result["summary"]["total_rows"] == 2
    assert result["errors"] == []
    assert result["ocr_mode"] == "standard"
    assert result["ocr_stats"] == {
        "primary_requests": 2,
        "secondary_requests": 1,
        "unresolved_critical": 0,
    }
    assert result["page_tables"] == {"1": {}, "2": {}}
    assert service.ocr.key == "stub"


def test_recognition_service_raises_when_all_pages_fail():
    class FailingProvider:
        key = "failing"

        def recognize(self, pdf_path, pages, **kwargs):
            return OcrResult(provider=self.key, pages=[
                PageOcrResult(page=p, errors=["нет движка"]) for p in pages
            ])

    service = RecognitionService(FakePdfService(), ocr=FailingProvider())
    with pytest.raises(RuntimeError, match="Не удалось обработать ни одной"):
        service.recognize(
            Path("doc.pdf"), Path("pages"), [1], None, 300,
            lambda c, t, m: None,
        )


def test_default_construction_preserves_legacy_behaviour():
    from averon_import.services.table_detector import GostSpecificationDetector

    service = RecognitionService(PdfService())
    assert service.ocr.key == "tesseract"
    assert isinstance(service.ocr, TesseractOcrAdapter)
    assert isinstance(service.detector, GostSpecificationDetector)


def test_registry_creates_tesseract_adapter():
    adapter = create_ocr_provider("tesseract", FakePdfService())
    assert isinstance(adapter, TesseractOcrAdapter)
    with pytest.raises(ValueError, match="Неизвестный OCR-провайдер"):
        create_ocr_provider("nope")
