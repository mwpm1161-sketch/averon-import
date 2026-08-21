from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from averon_import.api.dependencies import AppServices
from averon_import.api.documents import router as documents_router
from averon_import.api.export import router as export_router
from averon_import.api.recognition import router as recognition_router
from averon_import.api.system import router as system_router
from averon_import.core.constants import APP_NAME, APP_VERSION, DEVELOPER
from averon_import.ocr.tesseract_provider import EvidenceTesseractOcrProvider
from averon_import.services.export_service import ExcelExportService
from averon_import.services.jobs import JobService
from averon_import.services.pdf_service import PdfService
from averon_import.services.recognition import RecognitionService
from averon_import.services.workspace import WorkspaceService

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent


def default_data_dir() -> Path:
    configured = os.environ.get("AVERON_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return (root / "Averon Import" / "data").resolve()
    return (PROJECT_DIR / "data").resolve()


def create_services(data_dir: Path) -> AppServices:
    pdf = PdfService()
    recognition = RecognitionService(pdf)
    recognition.ocr = EvidenceTesseractOcrProvider(recognition.detector)
    return AppServices(
        pdf=pdf,
        workspace=WorkspaceService(data_dir),
        recognition=recognition,
        export=ExcelExportService(),
        jobs=JobService(max_workers=1),
    )


def create_app(data_dir: Path | None = None) -> FastAPI:
    resolved_data_dir = (data_dir or default_data_dir()).resolve()
    resolved_data_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url="/api/docs")
    app.state.services = create_services(resolved_data_dir)
    app.state.templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    app.include_router(system_router)
    app.include_router(documents_router)
    app.include_router(recognition_router)
    app.include_router(export_router)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return app.state.templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "app_name": APP_NAME,
                "version": APP_VERSION,
                "developer": DEVELOPER,
            },
        )

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    return app
