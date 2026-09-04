from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from averon_import.services.ocr.base import OcrResult, OcrRow, PageOcrResult
from averon_import.services.ocr.tesseract_adapter import TesseractOcrAdapter
from averon_import.services.pdf_service import PdfService
from averon_import.services.processing_coordinator import (
    CloudOCRNotConfigured,
    ProcessOptions,
    ProcessingCoordinator,
    ProcessingError,
    ProfileNotImplementedError,
)
from averon_import.services.recognition import RecognitionService


class StubProvider:
    key = "stub"
    label = "Stub OCR"

    def __init__(self, available: bool = True, rows: list[OcrRow] | None = None):
        self._available = available
        self.rows = rows if rows is not None else [_row()]
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._available

    def health(self) -> dict:
        return {"available": self._available, "provider": self.key}

    def recognize(
        self,
        pdf_path: Path,
        pages: list[int],
        *,
        pages_dir=None,
        dpi=None,
        crop=None,
        mode=None,
        progress=None,
        cancel: threading.Event | None = None,
    ) -> OcrResult:
        self.calls.append({"pages": list(pages), "mode": mode, "dpi": dpi})
        return OcrResult(
            provider=self.key,
            pages=[PageOcrResult(page=page, rows=list(self.rows)) for page in pages],
        )


class FakeSmartAI:
    def __init__(self):
        self.seen_keys: list[str] = []

    def process(self, result: dict, ai_provider: str, progress=None):
        self.seen_keys.append(ai_provider)
        return SimpleNamespace(result=result)


class StubSettings:
    def __init__(self, processing_mode: str):
        self.settings = SimpleNamespace(processing_mode=processing_mode)


def _row() -> OcrRow:
    return OcrRow(
        source_row=2,
        values={"position": "1", "name": "Вентиль", "quantity": "5"},
        confidences={"position": 90.0, "name": 80.0, "quantity": 70.0},
        sources={"position": "test"},
        bbox={"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
    )


def _coordinator(providers=None, settings_service=None) -> ProcessingCoordinator:
    return ProcessingCoordinator(
        PdfService(),
        smart_ai=FakeSmartAI(),
        settings_service=settings_service,
        providers=providers,
    )


def _options(**overrides) -> ProcessOptions:
    defaults = {
        "pages_dir": Path("pages"),
        "pages": [1],
        "ai_provider": "off",
    }
    defaults.update(overrides)
    return ProcessOptions(**defaults)


def test_local_profile_resolves_tesseract_adapter():
    coordinator = _coordinator()
    plan = coordinator.resolve("local")
    assert plan.profile == "local"
    assert isinstance(plan.ocr, TesseractOcrAdapter)
    assert plan.ai_provider_key == "off"


def test_cloud_profile_returns_clear_error_not_crash():
    coordinator = _coordinator()
    with pytest.raises(ProcessingError, match="недоступен"):
        coordinator.resolve("cloud")
    with pytest.raises(ProcessingError, match="Yandex Vision"):
        coordinator.process_document(Path("doc.pdf"), "cloud", _options())


def test_cloud_profile_forces_ai_off_and_rejects_cloud_llm():
    coordinator = _coordinator(providers={"cloud": StubProvider()})
    plan = coordinator.resolve("cloud")
    assert plan.profile == "cloud"
    assert plan.ai_provider_key == "off"
    with pytest.raises(ProcessingError, match="Облачный ИИ пока не подключён"):
        coordinator.resolve("cloud", ai_provider="yandex")


def test_hybrid_profile_is_not_implemented():
    coordinator = _coordinator()
    with pytest.raises(ProfileNotImplementedError, match="не реализован"):
        coordinator.resolve("hybrid")
    with pytest.raises(ProfileNotImplementedError):
        coordinator.process_document(Path("doc.pdf"), "hybrid", _options())


def test_unknown_mode_is_rejected():
    coordinator = _coordinator()
    with pytest.raises(ProcessingError, match="Неизвестный режим"):
        coordinator.resolve("quantum")


def test_settings_processing_mode_influences_resolver():
    cloud_settings = StubSettings("cloud")
    coordinator = _coordinator(settings_service=cloud_settings)
    with pytest.raises(ProcessingError, match="недоступен"):
        coordinator.resolve(None)

    local_settings = StubSettings("local")
    local_coordinator = _coordinator(settings_service=local_settings)
    assert isinstance(local_coordinator.resolve(None).ocr, TesseractOcrAdapter)

    explicit = _coordinator(settings_service=cloud_settings)
    assert isinstance(explicit.resolve("local").ocr, TesseractOcrAdapter)


def test_default_without_settings_is_cloud():
    cloud = StubProvider()
    coordinator = _coordinator(providers={"cloud": cloud})
    plan = coordinator.resolve(None)
    assert plan.profile == "cloud"
    assert plan.ocr is cloud


def test_local_mode_rejects_cloud_llm_combination():
    coordinator = _coordinator(providers={"cloud": StubProvider()})
    with pytest.raises(ProcessingError, match="локальн"):
        coordinator.resolve("local", ai_provider="yandex")
    assert coordinator.resolve("local", ai_provider="local").ai_provider_key == "local"


def test_process_document_does_not_mutate_shared_state():
    pdf_service = PdfService()
    legacy_singleton = RecognitionService(pdf_service)
    marker_before = legacy_singleton.ocr
    stub = StubProvider()
    coordinator = ProcessingCoordinator(
        pdf_service,
        smart_ai=FakeSmartAI(),
        providers={"local": stub},
    )
    result = coordinator.process_document(Path("doc.pdf"), "local", _options())
    assert len(stub.calls) == 1
    assert legacy_singleton.ocr is marker_before
    assert result["rows"][0]["position"] == "1"


def test_process_document_local_uses_injected_provider_and_assembles_rows():
    stub = StubProvider()
    coordinator = _coordinator(providers={"local": stub})
    first = coordinator.process_document(Path("doc.pdf"), "local", _options(pages=[1, 2]))
    second = coordinator.process_document(Path("doc.pdf"), "local", _options(pages=[3]))
    assert [call["pages"] for call in stub.calls] == [[1, 2], [3]]
    assert len(first["rows"]) == 2
    assert first["summary"]["total_rows"] == 2
    assert len(second["rows"]) == 1
    assert second["ai"] == {
        "enabled": False,
        "provider": "off",
        "status": "disabled",
        "warnings": [],
    }


def test_process_document_routes_ai_provider_key():
    smart_ai = FakeSmartAI()
    coordinator = ProcessingCoordinator(
        PdfService(),
        smart_ai=smart_ai,
        providers={"local": StubProvider()},
    )
    result = coordinator.process_document(
        Path("doc.pdf"), "local", _options(ai_provider="local")
    )
    assert smart_ai.seen_keys == ["local"]
    assert result["summary"]["total_rows"] == 1


def test_mode_status_reflects_placeholder_providers():
    coordinator = _coordinator()
    status = coordinator.mode_status()
    assert status["cloud"] == "not_configured"
    assert status["hybrid"] == "not_configured"

    ready_coordinator = _coordinator(
        providers={
            "local": StubProvider(available=True),
            "cloud": StubProvider(available=True),
            "hybrid": StubProvider(available=True),
        }
    )
    ready_status = ready_coordinator.mode_status()
    assert ready_status == {"local": "ready", "cloud": "ready", "hybrid": "ready"}

    broken_coordinator = _coordinator(providers={"local": StubProvider(available=False)})
    assert broken_coordinator.mode_status()["local"] == "unavailable"


def test_available_processing_modes_list():
    coordinator = _coordinator()
    assert coordinator.available_processing_modes() == ["local", "cloud", "hybrid"]


def test_cloud_placeholder_recognize_raises_provider_error():
    placeholder = CloudOCRNotConfigured()
    assert placeholder.available() is False
    assert placeholder.health()["available"] is False
    with pytest.raises(Exception, match="не настроен"):
        placeholder.recognize(Path("doc.pdf"), [1])
