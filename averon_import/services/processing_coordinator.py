from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from averon_import.core.constants import PROCESSING_MODES
from averon_import.services.ocr.base import OcrProvider, OcrProviderError
from averon_import.services.ocr.tesseract_adapter import TesseractOcrAdapter
from averon_import.services.recognition import RecognitionService


class ProcessingError(RuntimeError):
    """Понятная ошибка выбора/выполнения профиля обработки."""


class ProfileNotImplementedError(ProcessingError):
    """Профиль заявлен в схеме режимов, но пока не реализован."""


@dataclass(slots=True)
class ProcessOptions:
    pages_dir: Path | str
    pages: list[int]
    crop: dict | None = None
    dpi: int = 300
    ocr_mode: str = "standard"
    ai_provider: str = "off"


@dataclass(slots=True)
class ProcessingPlan:
    profile: str
    ocr: OcrProvider
    ai_provider_key: str


class CloudOCRNotConfigured:
    key = "cloud-not-configured"
    label = "Облачный OCR (Yandex Vision) не настроен"

    def available(self) -> bool:
        return False

    def health(self) -> dict:
        return {
            "available": False,
            "provider": self.key,
            "label": self.label,
            "reason": "Не заданы folder_id и/или API ключ Yandex Vision",
        }

    def recognize(
        self,
        pdf_path,
        pages_dir=None,
        pages=None,
        crop=None,
        dpi=300,
        progress: Callable[..., None] | None = None,
        cancel: threading.Event | None = None,
        ocr_mode: str = "standard",
    ):
        raise OcrProviderError(
            "Облачный режим не настроен: укажите folder_id и API ключ "
            "Yandex Vision в настройках. Для распознавания настройте Yandex Cloud."
        )


class _SmartAIIntegrationProtocol(Protocol):
    def process(self, result: dict, ai_provider: str, progress=None): ...


class ProcessingCoordinator:
    """Единая точка выбора профиля обработки и запуска распознавания.

    main.py не выбирает OCR-провайдер самостоятельно: выбор профиля
    (local/cloud/hybrid) инкапсулирован здесь. На каждое выполнение
    создаётся собственный RecognitionService с провайдером плана —
    общие изменяемые singleton'ы не используются.
    """

    def __init__(
        self,
        pdf_service,
        smart_ai: _SmartAIIntegrationProtocol,
        settings_service=None,
        providers: dict[str, OcrProvider] | None = None,
    ) -> None:
        self._pdf_service = pdf_service
        self._smart_ai = smart_ai
        self._settings_service = settings_service
        default_providers: dict[str, OcrProvider] = {
            "local": TesseractOcrAdapter(pdf_service),
            "cloud": CloudOCRNotConfigured(),
            "hybrid": CloudOCRNotConfigured(),
        }
        if providers:
            default_providers.update(providers)
        self._providers = default_providers
        self._noop_progress: Callable[..., None] = lambda *args, **kwargs: None

    @property
    def local_ocr(self) -> OcrProvider:
        return self._providers["local"]

    def available_processing_modes(self) -> list[str]:
        return list(PROCESSING_MODES)

    def mode_status(self) -> dict[str, str]:
        def status(provider: OcrProvider) -> str:
            if isinstance(provider, CloudOCRNotConfigured):
                return "not_configured"
            config_status = getattr(provider, "config_status", None)
            if callable(config_status):
                return str(config_status())
            return "ready" if provider.available() else "unavailable"

        return {mode: status(self._providers[mode]) for mode in PROCESSING_MODES}

    def ocr_health(self) -> dict:
        return self.local_ocr.health()

    def provider_health(self, mode: str) -> dict | None:
        provider = self._providers.get(mode)
        return provider.health() if provider else None

    def default_mode(self) -> str:
        settings = self._settings_service.settings if self._settings_service else None
        mode = getattr(settings, "processing_mode", None)
        return mode if mode in PROCESSING_MODES else "cloud"

    def resolve(
        self,
        processing_mode: str | None = None,
        ai_provider: str = "off",
    ) -> ProcessingPlan:
        requested = (processing_mode or "").strip().lower()
        mode = requested or self.default_mode()
        if mode == "hybrid":
            raise ProfileNotImplementedError(
                "Гибридный режим обработки пока не реализован."
            )
        if mode not in ("local", "cloud"):
            raise ProcessingError(
                f"Неизвестный режим обработки: {mode!r}. "
                f"Доступны: {', '.join(PROCESSING_MODES)}."
            )
        if mode == "cloud" and ai_provider not in ("off",):
            raise ProcessingError(
                "Облачный ИИ пока не подключён: облачный режим выполняет "
                "только Yandex Vision OCR. Используйте локальный режим для ИИ."
            )
        provider = self._providers[mode]
        if not provider.available():
            reason = getattr(provider, "label", None)
            message = (
                f"Режим «{mode}» сейчас недоступен"
                + (f": {reason}." if reason else ".")
            )
            raise ProcessingError(message)
        if mode == "local" and ai_provider not in ("off", "local"):
            raise ProcessingError(
                "В локальном режиме доступна только локальная модель ИИ "
                "или выключенный ИИ."
            )
        ai_key = ai_provider if mode == "local" else "off"
        return ProcessingPlan(profile=mode, ocr=provider, ai_provider_key=ai_key)

    def process_document(
        self,
        document_path: Path | str,
        processing_mode: str | None,
        options: ProcessOptions,
        progress: Callable[..., None] | None = None,
    ) -> dict:
        plan = self.resolve(processing_mode, ai_provider=options.ai_provider)
        on_progress = progress or self._noop_progress
        service = RecognitionService(self._pdf_service, ocr=plan.ocr)
        result = service.recognize(
            document_path,
            options.pages_dir,
            options.pages,
            options.crop,
            options.dpi,
            on_progress,
            ocr_mode=options.ocr_mode,
        )
        if plan.ai_provider_key == "off":
            result["ai"] = {
                "enabled": False,
                "provider": "off",
                "status": "disabled",
                "warnings": [],
            }
        else:
            result = self._smart_ai.process(result, plan.ai_provider_key, on_progress).result
            result["summary"] = service._summary(
                result.get("rows", []), result.get("errors", [])
            )
        return result
