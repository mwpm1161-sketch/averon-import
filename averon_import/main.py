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
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from typing import Any, Literal

from averon_import import __version__
from averon_import.ai import AiCorrectionService, SmartAIIntegration
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
from averon_import.services.app_settings import (
    PROCESSING_MODES,
    AppSettingsService,
)
from averon_import.services.export_service import ExcelExportService
from averon_import.services.jobs import JobService
from averon_import.services.ocr.yandex_vision import YandexVisionProvider
from averon_import.services.pdf_service import PdfService
from averon_import.services.processing_coordinator import (
    ProcessOptions,
    ProcessingCoordinator,
    ProcessingError,
)
from averon_import.services.recognition import RecognitionService
from averon_import.services.review_decisions import (
    FIELD_DECISION,
    HumanReviewService,
    RELATION_DECISION,
    REJECT_DECISION,
    ReviewDecisionStore,
)
from averon_import.services.review_policy import refresh_rows
from averon_import.services.secrets import (
    YANDEX_API_KEY,
    create_secret_store,
    resolve_secret,
)
from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.catalog_repository import CatalogRepository
from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.product_understanding import SourcingAIService
from averon_import.services.sourcing.providers.local_catalog import LocalCatalogProvider
from averon_import.services.sourcing.runtime import create_sourcing_ai_transport
from averon_import.services.sourcing.service import SourcingService
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
ai_service = AiCorrectionService.from_env()
smart_ai = SmartAIIntegration(service=ai_service)
job_service = JobService(max_workers=1)
app_settings_service = AppSettingsService(DATA_DIR)
secret_store = create_secret_store(DATA_DIR)
yandex_vision_provider = YandexVisionProvider(
    settings_service=app_settings_service,
    secret_store=secret_store,
    cache_dir=DATA_DIR / "ocr_cache",
)
coordinator = ProcessingCoordinator(
    pdf_service,
    smart_ai=smart_ai,
    settings_service=app_settings_service,
    providers={"cloud": yandex_vision_provider},
)
sourcing_repository = CatalogRepository(DATA_DIR / "sourcing" / "catalog.sqlite3")
sourcing_provider = LocalCatalogProvider(sourcing_repository)
sourcing_ai_transport = create_sourcing_ai_transport(app_settings_service, secret_store)
sourcing_service = SourcingService(
    {sourcing_provider.key: sourcing_provider},
    default_provider=(
        app_settings_service.settings.sourcing.provider
        if app_settings_service.settings.sourcing.provider in {sourcing_provider.key}
        else sourcing_provider.key
    ),
    ai=SourcingAIService(sourcing_ai_transport),
    cache=SourcingCache(DATA_DIR / "sourcing" / "cache.json"),
)
human_review_service = HumanReviewService()

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
        "ocr": coordinator.ocr_health(),
        "cloud_ocr": yandex_vision_provider.health(),
        "ai": ai_service.health(),
        "sourcing": sourcing_service.health(),
        "data_dir": str(DATA_DIR),
        "settings": {
            "warnings": app_settings_service.warnings_snapshot(),
            "secret_backend": secret_store.backend_name,
            "secret_insecure": secret_store.is_insecure,
        },
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
        "ai": ai_service.public_config(),
        "sourcing": sourcing_service.public_config(),
        "settings": {
            "processing_mode": app_settings_service.settings.processing_mode,
            "processing_modes": list(PROCESSING_MODES),
            "available_processing_modes": coordinator.available_processing_modes(),
            "processing_status": coordinator.mode_status(),
            "warnings": app_settings_service.warnings_snapshot(),
        },
    }


class LocalSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str | None = None
    model: str | None = None


class YandexSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    folder_id: str | None = None
    vision_model: str | None = None
    llm_model: str | None = None
    vision_base_url: str | None = None
    llm_base_url: str | None = None
    chunk_pages: int | None = None
    request_timeout_s: float | None = None
    operation_timeout_s: float | None = None
    language_codes: list[str] | None = None


class PipelineSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool | None = None
    rules_enabled: bool | None = None
    validation_enabled: bool | None = None
    batch_size: int | None = None
    min_confidence: float | None = None


class SourcingSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str | None = None


class SettingsUpdate(BaseModel):
    """api_key is write-only: it goes to the SecretStore and is never returned."""

    model_config = ConfigDict(extra="ignore")

    processing_mode: Literal["local", "cloud", "hybrid"] | None = None
    local: LocalSettingsUpdate | None = None
    yandex: YandexSettingsUpdate | None = None
    pipeline: PipelineSettingsUpdate | None = None
    sourcing: SourcingSettingsUpdate | None = None
    api_key: str | None = None
    delete_yandex_api_key: bool = False


def _settings_public() -> dict:
    payload = app_settings_service.public()
    api_key_configured = None
    for env_name in ("AVERON_YANDEX_VISION_API_KEY", "AVERON_YANDEX_AI_API_KEY"):
        raw = os.environ.get(env_name)
        if raw and raw.strip():
            api_key_configured = resolve_secret(raw, secret_store, YANDEX_API_KEY)
            break
    if api_key_configured is None:
        api_key_configured = resolve_secret(None, secret_store, YANDEX_API_KEY)
    payload["yandex"]["api_key_configured"] = (
        api_key_configured is not None
    )
    payload["secret_backend"] = secret_store.backend_name
    payload["secret_insecure"] = secret_store.is_insecure
    return payload


@app.get("/api/settings")
def get_settings():
    return _settings_public()


@app.put("/api/settings")
def put_settings(request: SettingsUpdate):
    if request.api_key is not None and request.api_key.strip():
        secret_store.set(YANDEX_API_KEY, request.api_key.strip())
    if request.delete_yandex_api_key:
        secret_store.delete(YANDEX_API_KEY)
    patch = request.model_dump(exclude_none=True, exclude={"api_key", "delete_yandex_api_key"})
    patch = {key: value for key, value in patch.items() if value is not None}
    try:
        app_settings_service.update(patch)
        sourcing_service.set_ai(
            SourcingAIService(create_sourcing_ai_transport(app_settings_service, secret_store))
        )
        if app_settings_service.settings.sourcing.provider in sourcing_service.providers:
            sourcing_service.default_provider = app_settings_service.settings.sourcing.provider
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()))
        raise HTTPException(400, f"Недопустимые настройки {location}: {first.get('msg', '')}") from exc
    return _settings_public()


@app.delete("/api/settings/yandex-api-key")
def delete_yandex_api_key():
    secret_store.delete(YANDEX_API_KEY)
    return {"deleted": True}


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

    if request.ai_provider != "off":
        try:
            ai_service.ensure_provider(request.ai_provider)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    try:
        coordinator.resolve(request.processing_mode, ai_provider=request.ai_provider)
    except ProcessingError as exc:
        raise HTTPException(400, str(exc)) from exc

    options = ProcessOptions(
        pages_dir=workspace.pages_dir,
        pages=pages,
        crop=crop,
        dpi=request.dpi,
        ocr_mode=request.ocr_mode,
        ai_provider=request.ai_provider,
    )

    def run(progress):
        result = coordinator.process_document(
            workspace.pdf_path, request.processing_mode, options, progress
        )
        document_fingerprint = human_review_service.document_fingerprint(workspace.pdf_path)
        result = human_review_service.apply_saved_decisions(
            result,
            ReviewDecisionStore(workspace.review_decisions_path).load(),
            document_fingerprint,
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
        result = human_review_service.apply_saved_decisions(
            result,
            ReviewDecisionStore(workspace.review_decisions_path).load(),
            human_review_service.document_fingerprint(workspace.pdf_path),
        )
        workspace_service.write_json(workspace.result_path, result)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


@app.put("/api/documents/{document_id}/results")
def save_results(document_id: str, request: SaveRowsRequest):
    try:
        workspace = workspace_service.get(document_id)
        existing = workspace_service.read_json(workspace.result_path, default={})
        rows = refresh_rows(request.rows)
        existing["rows"] = rows
        existing = human_review_service.apply_saved_decisions(
            existing,
            ReviewDecisionStore(workspace.review_decisions_path).load(),
            human_review_service.document_fingerprint(workspace.pdf_path),
        )
        rows = existing.get("rows") or []
        existing["summary"] = recognition_service._summary(
            rows, existing.get("errors", [])
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
        stored_result = workspace_service.read_json(workspace.result_path, default={})
        export_service.export(
            rows=request.rows,
            columns=request.columns,
            output_path=output,
            sheet_name=request.sheet_name,
            include_headers=request.include_headers,
            only_exportable=request.only_exportable,
            page_statuses=stored_result.get("page_statuses") or {},
            review_export=request.review_export,
            enforce_safety=not request.review_export,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


class SourcingRowRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    row: dict[str, Any]
    provider: str | None = None
    limit: int = 20


class SourcingIntentRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: ProductIntent
    provider: str | None = None
    limit: int = 20


class SourcingProjectRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[dict[str, Any]]
    provider: str | None = None
    limit: int = 20


def _sourcing_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


@app.get("/api/sourcing/providers")
def sourcing_providers():
    return sourcing_service.public_config()


@app.get("/api/sourcing/catalog/stats")
def sourcing_catalog_stats():
    return sourcing_repository.stats()


@app.post("/api/sourcing/understand")
def sourcing_understand(request: SourcingRowRequest):
    intent, warnings = sourcing_service.understand_row_with_warnings(request.row)
    return {"intent": _sourcing_payload(intent), "warnings": warnings}


@app.post("/api/sourcing/search")
def sourcing_search(request: SourcingRowRequest):
    try:
        result = sourcing_service.search_row(
            request.row,
            provider_key=request.provider,
            limit=max(1, min(request.limit, 100)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _sourcing_payload(result)


@app.post("/api/sourcing/search-intent")
def sourcing_search_intent(request: SourcingIntentRequest):
    try:
        result = sourcing_service.search_intent(
            request.intent,
            provider_key=request.provider,
            limit=max(1, min(request.limit, 100)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _sourcing_payload(result)


@app.post("/api/sourcing/search-all")
def sourcing_search_all(request: SourcingProjectRequest):
    try:
        result = sourcing_service.search_project(
            request.rows,
            provider_key=request.provider,
            limit=max(1, min(request.limit, 100)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _sourcing_payload(result)


def _ensure_document(document_id: str) -> None:
    try:
        workspace_service.get(document_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page: int
    physical_refs: list[dict[str, Any]]
    decision: Literal[FIELD_DECISION, RELATION_DECISION, REJECT_DECISION]
    field: str | None = None
    relation: str | None = None
    candidate_value: str | None = None
    target: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/documents/{document_id}/review")
def get_review_decisions(document_id: str):
    try:
        workspace = workspace_service.get(document_id)
        decisions = ReviewDecisionStore(workspace.review_decisions_path).load()
        fingerprint = human_review_service.document_fingerprint(workspace.pdf_path)
        return {
            "document_fingerprint": fingerprint,
            "decisions": [item.model_dump(mode="json") for item in decisions if item.document_fingerprint == fingerprint],
        }
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


@app.post("/api/documents/{document_id}/review/decision")
def save_review_decision(document_id: str, request: ReviewDecisionRequest):
    try:
        workspace = workspace_service.get(document_id)
        result = workspace_service.read_json(workspace.result_path, default={})
        if not result:
            raise HTTPException(404, "Результат распознавания отсутствует")
        fingerprint = human_review_service.document_fingerprint(workspace.pdf_path)
        decision = human_review_service.create_decision(
            result,
            document_fingerprint=fingerprint,
            page=request.page,
            physical_refs=request.physical_refs,
            decision=request.decision,
            field=request.field,
            relation=request.relation,
            candidate_value=request.candidate_value,
            target=request.target,
        )
        store = ReviewDecisionStore(workspace.review_decisions_path)
        decisions = store.upsert(decision)
        updated = human_review_service.apply_saved_decisions(result, decisions, fingerprint)
        workspace_service.write_json(workspace.result_path, updated)
        return {
            "saved": True,
            "decision": decision.model_dump(mode="json"),
            "result": updated,
        }
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/documents/{document_id}/sourcing/search")
def document_sourcing_search(document_id: str, request: SourcingRowRequest):
    _ensure_document(document_id)
    return sourcing_search(request)


@app.post("/api/documents/{document_id}/sourcing/search-all")
def document_sourcing_search_all(document_id: str, request: SourcingProjectRequest):
    _ensure_document(document_id)
    # The client sends the current selected/exportable rows, including any
    # reviewed edits.  The document id is used only to scope the operation.
    return sourcing_search_all(request)


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
