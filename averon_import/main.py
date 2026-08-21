from __future__ import annotations

import argparse
import os
import re
import tempfile
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from averon_import import __version__
from averon_import.core.constants import (
    ALL_COLUMNS,
    APP_NAME,
    APP_VERSION,
    DEFAULT_EXPORT_COLUMNS,
    DEVELOPER,
    ROW_TYPES,
    STATUSES,
)
from averon_import.core.schemas import ExportRequest, RecognitionRequest, SaveRowsRequest
from averon_import.services.export_service import ExcelExportService
from averon_import.services.jobs import JobService
from averon_import.services.ocr_engine import TesseractOcrEngine
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


DATA_DIR = default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

pdf_service = PdfService()
workspace_service = WorkspaceService(DATA_DIR)
recognition_service = RecognitionService(pdf_service)
export_service = ExcelExportService()
job_service = JobService(max_workers=1)

app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_name": APP_NAME,
            "version": APP_VERSION,
            "developer": DEVELOPER,
        },
    )


@app.get("/api/health")
def health():
    return {
        "app": APP_NAME,
        "version": __version__,
        "ocr": TesseractOcrEngine.health(),
        "data_dir": str(DATA_DIR),
    }


@app.get("/api/config")
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


@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...)):
    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Поддерживаются только PDF-файлы")
    max_bytes = 250 * 1024 * 1024
    total = 0
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
        temp_path = Path(temporary.name)
        try:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, "Размер PDF превышает 250 МБ")
                temporary.write(chunk)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    try:
        inspection = pdf_service.inspect(temp_path)
        workspace = workspace_service.create(
            temp_path,
            {
                "filename": filename,
                "page_count": inspection["page_count"],
                "title": (
                    Path(filename).stem
                    if inspection["title"] == temp_path.stem
                    else inspection["title"]
                ),
                "size": total,
            },
        )
        metadata = workspace_service.read_json(workspace.metadata_path)
        return metadata
    except Exception as exc:
        raise HTTPException(400, f"Не удалось открыть PDF: {exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


@app.get("/api/documents/{document_id}")
def get_document(document_id: str):
    try:
        workspace = workspace_service.get(document_id)
        metadata = workspace_service.read_json(workspace.metadata_path)
        result = workspace_service.read_json(workspace.result_path)
        return {**metadata, "has_result": bool(result)}
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


@app.get("/api/documents/{document_id}/page/{page_number}")
def page_image(document_id: str, page_number: int, dpi: int = 110):
    try:
        workspace = workspace_service.get(document_id)
        metadata = workspace_service.read_json(workspace.metadata_path)
        if page_number < 1 or page_number > metadata["page_count"]:
            raise HTTPException(404, "Страница не найдена")
        dpi = max(72, min(dpi, 300))
        cache = workspace.pages_dir / f"page-{page_number}-{dpi}.png"
        if not cache.exists():
            pdf_service.render_page_to_path(
                workspace.pdf_path, page_number, cache, dpi=dpi
            )
        return FileResponse(cache, media_type="image/png")
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


@app.post("/api/documents/{document_id}/suggest-pages")
def suggest_pages(document_id: str):
    try:
        workspace = workspace_service.get(document_id)
        metadata = workspace_service.read_json(workspace.metadata_path)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc

    def run(progress):
        return recognition_service.suggest_pages(
            workspace.pdf_path, workspace.pages_dir, metadata["page_count"], progress
        )

    return job_service.submit(run).public()


@app.post("/api/documents/{document_id}/recognize")
def recognize(document_id: str, request: RecognitionRequest):
    try:
        workspace = workspace_service.get(document_id)
        metadata = workspace_service.read_json(workspace.metadata_path)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc

    pages = sorted(set(request.pages))
    if not pages:
        raise HTTPException(400, "Не выбраны страницы")
    invalid = [page for page in pages if page < 1 or page > metadata["page_count"]]
    if invalid:
        raise HTTPException(400, f"Некорректные страницы: {invalid}")
    crop = request.crop.model_dump() if request.crop else None

    def run(progress):
        result = recognition_service.recognize(
            workspace.pdf_path,
            workspace.pages_dir,
            pages,
            crop,
            request.dpi,
            progress,
            ocr_mode=request.ocr_mode,
        )
        workspace_service.write_json(workspace.result_path, result)
        return result

    job = job_service.submit(run)
    return job.public()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    try:
        return job_service.get(job_id).public()
    except KeyError as exc:
        raise HTTPException(404, "Задание не найдено") from exc


@app.get("/api/documents/{document_id}/results")
def get_results(document_id: str):
    try:
        workspace = workspace_service.get(document_id)
        result = workspace_service.read_json(workspace.result_path)
        if not result:
            raise HTTPException(404, "Результат распознавания отсутствует")
        return result
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


@app.put("/api/documents/{document_id}/results")
def save_results(document_id: str, request: SaveRowsRequest):
    try:
        workspace = workspace_service.get(document_id)
        existing = workspace_service.read_json(workspace.result_path, default={})
        existing["rows"] = request.rows
        existing["summary"] = recognition_service._summary(
            request.rows, existing.get("errors", [])
        )
        workspace_service.write_json(workspace.result_path, existing)
        return {"saved": True, "summary": existing["summary"]}
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


def safe_filename(value: str) -> str:
    value = Path(value or "averon_import.xlsx").name
    value = re.sub(r"[^\w\-. ()А-Яа-яЁё]", "_", value)
    if not value.lower().endswith(".xlsx"):
        value += ".xlsx"
    return value[:160]


@app.post("/api/documents/{document_id}/export")
def export(document_id: str, request: ExportRequest):
    try:
        workspace = workspace_service.get(document_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc

    filename = safe_filename(request.filename)
    output = workspace.exports_dir / filename
    try:
        export_service.export(
            rows=request.rows,
            columns=request.columns,
            output_path=output,
            sheet_name=request.sheet_name,
            include_headers=request.include_headers,
            only_exportable=request.only_exportable,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


def cli() -> None:
    parser = argparse.ArgumentParser(description="Запуск Averon Import")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not args.no_browser:
        threading.Timer(
            1.2, lambda: webbrowser.open(f"http://{args.host}:{args.port}")
        ).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    cli()
