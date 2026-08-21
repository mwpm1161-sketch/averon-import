from __future__ import annotations

from fastapi import APIRouter, Depends

from averon_import.api.dependencies import AppServices, get_services
from averon_import.core.constants import (
    ALL_COLUMNS,
    APP_NAME,
    APP_VERSION,
    DEFAULT_EXPORT_COLUMNS,
    DEVELOPER,
    ROW_TYPES,
    STATUSES,
)
from averon_import.services.ocr_engine import TesseractOcrEngine

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health(services: AppServices = Depends(get_services)):
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "ocr": TesseractOcrEngine.health(),
        "data_dir": str(services.workspace.data_dir),
    }


@router.get("/config")
def config():
    return {
        "columns": ALL_COLUMNS,
        "default_export_columns": DEFAULT_EXPORT_COLUMNS,
        "row_types": ROW_TYPES,
        "statuses": STATUSES,
        "developer": DEVELOPER,
        "ocr_modes": {
            "standard": "Стандартный",
            "accurate": "Точный инженерный",
        },
    }
