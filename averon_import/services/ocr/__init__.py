"""OCR provider package: neutral contract plus registered adapters."""

from averon_import.services.ocr.base import (
    OcrProvider,
    OcrProviderError,
    OcrResult,
    OcrRow,
    PageOcrResult,
    ProgressCallback,
)
from averon_import.services.ocr.reconstruction import reconstruct_page_rows
from averon_import.services.ocr.tesseract_adapter import TesseractOcrAdapter
from averon_import.services.ocr.yandex_vision import YandexVisionProvider

_PROVIDER_FACTORIES: dict[str, type] = {"tesseract": TesseractOcrAdapter}


def register_provider(key: str, factory: type) -> None:
    _PROVIDER_FACTORIES[key] = factory


def create_ocr_provider(key: str, *args, **kwargs):
    try:
        factory = _PROVIDER_FACTORIES[key]
    except KeyError:
        raise ValueError(f"Неизвестный OCR-провайдер: {key}") from None
    return factory(*args, **kwargs)


__all__ = [
    "OcrProvider",
    "OcrProviderError",
    "OcrResult",
    "OcrRow",
    "PageOcrResult",
    "ProgressCallback",
    "TesseractOcrAdapter",
    "YandexVisionProvider",
    "reconstruct_page_rows",
    "register_provider",
    "create_ocr_provider",
]
