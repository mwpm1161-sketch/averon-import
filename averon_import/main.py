from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator, model_validator
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
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
from averon_import.services.account_auth import (
    AccountDisabled,
    AccountRepository,
    AdminAccountProtected,
    DEFAULT_SESSION_TTL_SECONDS,
    DuplicateUsername,
    LazyAccountRepository,
    StaleUserVersion,
    UserNotFound,
    dummy_password_verification,
    normalize_username,
    validate_password,
    verify_password,
)
from averon_import.services.app_settings import PROCESSING_MODES, AppSettingsService
from averon_import.services.auth import (
    CurrentUser,
    Role,
    auth_config,
    configure_account_repository,
    login_bucket_key,
    login_rate_limiter,
    login_work_guard,
    require_admin,
    require_authenticated,
    require_one_c_history_import,
)
from averon_import.services.export_service import ExcelExportService
from averon_import.services.jobs import (
    DOCUMENT_PROCESSING,
    SOURCING,
    JobAdmissionError,
    JobService,
    stable_fingerprint,
)
from averon_import.services.one_c_history import (
    OneCHistoryActivityConflict,
    OneCHistoryActivityRegistry,
    OneCHistoryImportService,
    OneCHistoryRepository,
)
from averon_import.services.one_c_history.models import ImportMappingRequest, PreviewMappingRequest
from averon_import.services.one_c_history.xlsx_import import OneCImportError
from averon_import.services.manual_tenders import (
    TenderActivityConflict,
    TenderActivityRegistry,
    TenderParseError,
    TenderTemplateService,
    TenderWorkbookParser,
    TenderWorkspaceError,
    TenderWorkspaceRepository,
)
from averon_import.services.manual_tenders.sourcing import (
    TenderSourcingRowAdapter,
    TenderSourcingRunStore,
    canonical_tender_projection,
)
from averon_import.services.manual_tenders.price_export import (
    XLSX_MIME,
    TenderPriceExportRepository,
    TenderPriceResolver,
    TenderXlsxPriceExporter,
    summarize_decisions,
)
from averon_import.services.manual_tenders.parser import MAX_UPLOAD_BYTES as MAX_MANUAL_TENDER_UPLOAD_BYTES
from averon_import.services.document_mutation import DocumentMutationLocks
from averon_import.services.document_lifecycle import (
    DocumentActivityRegistry,
    DocumentUnavailable,
)
from averon_import.services.ocr.yandex_vision import YandexVisionProvider
from averon_import.services.pdf_service import PdfService
from averon_import.services.processing_coordinator import (
    ProcessOptions,
    ProcessingCoordinator,
    ProcessingError,
)
from averon_import.services.recognition import RecognitionService
from averon_import.services.review_decisions import (
    CONFIRM_FIELD_ABSENT_DECISION,
    CONFIRM_FIELD_VALUE_DECISION,
    FIELD_DECISION,
    HumanReviewService,
    RELATION_DECISION,
    REJECT_DECISION,
    REVIEW_PROJECTION_VERSION,
    ReviewDecision,
    ReviewDecisionLedgerCorrupt,
    ReviewDecisionStore,
)
from averon_import.services.review_policy import critical_blockers_for_row, refresh_rows
from averon_import.services.secrets import (
    ETM_IPRO_LOGIN,
    ETM_IPRO_PASSWORD,
    LEMANA_B2B_CLIENT_SECRET,
    YANDEX_AI_API_KEY,
    YANDEX_API_KEY,
    create_secret_store,
    resolve_secret,
)
from averon_import.services.support_reports import (
    DuplicateReport,
    IncidentNotFound,
    INCIDENT_KIND_USER_REPORTED,
    REPORT_STATUSES,
    ReportForbidden,
    ReportNotFound,
    SnapshotUnavailable,
    StaleReport,
    SupportRepository,
)
from averon_import.services.sourcing.demo_catalog import (
    DEMO_CATALOG_NOTICE,
    DEMO_CATALOG_SOURCE,
)
from averon_import.services.sourcing.models import ProductIntent, SourcingSourceMode
from averon_import.services.sourcing.runtime import create_sourcing_runtime
from averon_import.services.sourcing.run_history import SourcingRunHistory
from averon_import.services.workspace import WorkspaceService, validate_document_id

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
logger = logging.getLogger(__name__)


def _static_asset_revision(filename: str) -> str:
    return hashlib.sha256((PACKAGE_DIR / "static" / filename).read_bytes()).hexdigest()[:12]


STATIC_ASSET_REVISIONS = {
    "app": _static_asset_revision("app.js"),
    "styles": _static_asset_revision("styles.css"),
}


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
auth_repository = LazyAccountRepository(lambda: AccountRepository(DATA_DIR / "auth"))
configure_account_repository(auth_repository)
support_repository = SupportRepository(DATA_DIR / "support")

pdf_service = PdfService()
workspace_service = WorkspaceService(DATA_DIR)
recognition_service = RecognitionService(pdf_service)
export_service = ExcelExportService()
ai_service = AiCorrectionService.from_env()
smart_ai = SmartAIIntegration(service=ai_service)
job_service = JobService(max_workers=1)
app_settings_service = AppSettingsService(DATA_DIR)
one_c_history_repository = OneCHistoryRepository(DATA_DIR)
one_c_history_activity = OneCHistoryActivityRegistry()
one_c_history_service = OneCHistoryImportService(one_c_history_repository, one_c_history_activity)
tender_activity = TenderActivityRegistry()
tender_repository = TenderWorkspaceRepository(DATA_DIR, tender_activity)
tender_sourcing_runs = TenderSourcingRunStore(tender_repository)
tender_price_exports = TenderPriceExportRepository(tender_repository)
tender_price_resolver = TenderPriceResolver()
tender_xlsx_price_exporter = TenderXlsxPriceExporter()
tender_parser = TenderWorkbookParser()
tender_template_service = TenderTemplateService()
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
sourcing_runtime = create_sourcing_runtime(
    DATA_DIR,
    app_settings_service,
    secret_store,
    one_c_history_repository=one_c_history_repository,
)
# Compatibility aliases for endpoints and integrations that historically used
# these module-level objects directly.
sourcing_repository = sourcing_runtime.repository
sourcing_provider = sourcing_runtime.providers["local_catalog"]
demo_store_provider = sourcing_runtime.providers["demo_store_http"]
sourcing_service = sourcing_runtime.service
human_review_service = HumanReviewService()
document_mutation_locks = DocumentMutationLocks()
document_activity_registry = DocumentActivityRegistry()
_sourcing_runtime_lock = threading.RLock()


def require_document_activity(document_id: str):
    try:
        with document_activity_registry.lease(document_id):
            yield
    except DocumentUnavailable as exc:
        raise HTTPException(404, "Документ не найден") from exc


def _job_owner_id(user: CurrentUser | None) -> str:
    """Return an owner key from the authenticated server-side identity."""
    if not isinstance(user, CurrentUser):
        return "__internal__"
    return str(user.user_id or user.username.casefold())


def _job_admission_http_error(exc: JobAdmissionError) -> HTTPException:
    return HTTPException(
        409,
        detail={"code": exc.code, "message": exc.message},
    )


def _submit_document_job(
    document_id: str,
    run,
    *,
    lane: str = DOCUMENT_PROCESSING,
    owner_id: str = "__internal__",
    kind: str = "document_operation",
    dedupe_key: str | None = None,
    release_callbacks=(),
):
    """Transfer a lifecycle lease to the coordinator for its full job lifetime."""
    try:
        release = document_activity_registry.acquire(document_id)
    except DocumentUnavailable as exc:
        raise HTTPException(404, "Документ не найден") from exc
    callbacks = [release, *release_callbacks]
    try:
        return job_service.submit(
            run,
            lane=lane,
            kind=kind,
            owner_id=owner_id,
            document_id=document_id,
            dedupe_key=dedupe_key,
            release_callbacks=callbacks,
        )
    except JobAdmissionError as exc:
        raise _job_admission_http_error(exc) from exc
    except Exception:
        for callback in callbacks:
            callback()
        raise


def document_file_response(document_id: str, path: Path, **kwargs) -> FileResponse:
    """Keep a workspace leased until its file response has finished sending."""
    try:
        release = document_activity_registry.acquire(document_id)
    except DocumentUnavailable as exc:
        raise HTTPException(404, "Документ не найден") from exc
    try:
        return FileResponse(path, background=BackgroundTask(release), **kwargs)
    except Exception:
        release()
        raise


def _result_revision(result: dict[str, Any] | None) -> int:
    try:
        return max(0, int((result or {}).get("revision", 0)))
    except (TypeError, ValueError):
        return 0


def _review_projection_current(result: dict[str, Any], ledger_revision: int) -> bool:
    if result.get("review_projection_version") != REVIEW_PROJECTION_VERSION:
        return False
    try:
        current = int(result.get("review_ledger_revision", -1))
    except (TypeError, ValueError):
        return False
    return current == ledger_revision


def _reconcile_review_projection_locked(
    workspace,
    result: dict[str, Any],
    *,
    ledger_snapshot: tuple[list[ReviewDecision], int] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Recover a result only when its persisted ledger revision is stale.

    Callers must own the document mutation lock. The small revision marker
    keeps the steady-state path from parsing or replaying the complete ledger.
    """
    if ledger_snapshot is None:
        store = ReviewDecisionStore(workspace.review_decisions_path)
        ledger_revision = store.load_revision()
        if _review_projection_current(result, ledger_revision):
            return result, False
        decisions, ledger_revision = store.load_snapshot()
    else:
        decisions, ledger_revision = ledger_snapshot
    if _review_projection_current(result, ledger_revision):
        return result, False
    updated = human_review_service.apply_saved_decisions(
        result,
        decisions,
        _source_fingerprint(workspace),
    )
    updated["review_ledger_revision"] = ledger_revision
    updated["revision"] = _result_revision(result) + 1
    workspace_service.write_json(workspace.result_path, updated)
    return updated, True


def _source_fingerprint(workspace) -> str:
    return workspace_service.source_fingerprint(
        workspace, human_review_service.document_fingerprint
    )


def _public_document_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key != "source_sha256"}


def _rebuild_sourcing_runtime() -> None:
    """Rebind sourcing dependencies after settings or secret changes."""

    global demo_store_provider, sourcing_provider, sourcing_repository, sourcing_runtime, sourcing_service
    rebuilt = create_sourcing_runtime(
        DATA_DIR,
        app_settings_service,
        secret_store,
        one_c_history_repository=one_c_history_repository,
    )
    with _sourcing_runtime_lock:
        sourcing_runtime = rebuilt
        sourcing_repository = rebuilt.repository
        sourcing_provider = rebuilt.providers["local_catalog"]
        demo_store_provider = rebuilt.providers["demo_store_http"]
        sourcing_service = rebuilt.service

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
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
            "asset_revisions": STATIC_ASSET_REVISIONS,
        },
    )


@app.get("/api/health", dependencies=[Depends(require_authenticated)])
def health():
    return {
        "app": APP_NAME,
        "version": __version__,
        "ocr": coordinator.ocr_health(),
        "cloud_ocr": yandex_vision_provider.health(),
        "ai": ai_service.health(),
        "sourcing": sourcing_service.health(),
    }


@app.get("/api/admin/health", dependencies=[Depends(require_admin)])
def admin_health():
    payload = health()
    payload["data_dir"] = str(DATA_DIR)
    payload["settings"] = {
        "warnings": app_settings_service.warnings_snapshot(),
        "secret_backend": secret_store.backend_name,
        "secret_insecure": secret_store.is_insecure,
    }
    return payload


def _one_c_history_owner_key(user: CurrentUser) -> str:
    if user.user_id:
        return f"id:{user.user_id[:128]}"
    return f"username:{normalize_username(user.username)[:128]}"


def _one_c_history_actor(user: CurrentUser) -> dict[str, str | None]:
    return {"user_id": user.user_id, "username": user.username, "role": user.role.value}


def _one_c_history_status(*, include_audit: bool) -> dict[str, Any]:
    payload = one_c_history_repository.public_status(include_audit=include_audit)
    payload["activity"] = one_c_history_activity.status()
    return payload


def _one_c_history_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, OneCHistoryActivityConflict):
        return HTTPException(status_code=409, detail={
            "code": exc.code,
            "message": exc.message,
            "active_sourcing_count": exc.active_sourcing_count,
        })
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, OneCImportError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


@app.get("/api/admin/one-c-history", dependencies=[Depends(require_admin)])
def get_one_c_history_status(_admin: CurrentUser = Depends(require_admin)):
    return _one_c_history_status(include_audit=True)


@app.get("/api/one-c-history")
def get_capability_one_c_history_status(user: CurrentUser = Depends(require_one_c_history_import)):
    return _one_c_history_status(include_audit=user.capabilities["settings"])


def _tender_owner_key(user: CurrentUser) -> str:
    if user.user_id:
        return f"id:{user.user_id[:128]}"
    return f"username:{normalize_username(user.username)[:128]}"


def _tender_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (TenderWorkspaceError, TenderParseError)):
        return HTTPException(
            status_code=getattr(exc, "status_code", 400),
            detail={"code": getattr(exc, "code", "TENDER_XLSX_INVALID"), "message": str(exc)},
        )
    if isinstance(exc, TenderActivityConflict):
        return HTTPException(status_code=409, detail={"code": "TENDER_WORKSPACE_BUSY", "message": str(exc)})
    raise exc


def _submit_tender_parse_job(preview_id: str, owner_id: str, *, mapping: dict[str, int | None] | None = None, selected_sheet: str | None = None, selected_header_row: int | None = None):
    repository = tender_repository
    parser = tender_parser
    activity = tender_activity
    try:
        lease = activity.acquire(preview_id)
    except TenderActivityConflict as exc:
        raise _tender_http_error(exc) from exc

    def run(progress):
        try:
            path, metadata = repository.preview_path(preview_id, owner_id)
            repository.update_preview(preview_id, owner_id, status="analyzing", error=None)
            progress(0, 1, "Проверяем структуру книги")
            analysis = parser.parse(path, tender_id=preview_id, mapping_override=mapping, selected_sheet=selected_sheet, selected_header_row=selected_header_row)
            repository.update_preview(
                preview_id, owner_id, status="ready", parser_version=analysis["parser_version"],
                mapping=analysis["mapping"], analysis=analysis, error=None,
            )
            progress(1, 1, "Проверка завершена")
            return {"preview_id": preview_id, "status": "ready"}
        except Exception as exc:
            message = str(exc) if isinstance(exc, (TenderParseError, TenderWorkspaceError)) else "Не удалось безопасно проанализировать XLSX. Загрузите файл повторно или проверьте его структуру."
            try:
                repository.update_preview(preview_id, owner_id, status="failed", error=message)
            except Exception:
                pass
            if isinstance(exc, (TenderParseError, TenderWorkspaceError)):
                raise
            raise TenderParseError(message, "TENDER_PARSE_FAILED") from exc

    try:
        return job_service.submit(
            run,
            lane=DOCUMENT_PROCESSING,
            kind="manual_tender_xlsx_preview",
            owner_id=owner_id,
            document_id=preview_id,
            dedupe_key=stable_fingerprint({
                "owner": owner_id, "preview": preview_id, "mapping": mapping,
                "sheet": selected_sheet, "header_row": selected_header_row,
            }),
            release_callbacks=[lease.release],
        )
    except JobAdmissionError as exc:
        lease.release()
        raise _job_admission_http_error(exc) from exc
    except Exception:
        lease.release()
        raise


class TenderMappingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet_name: str
    header_row: StrictInt = Field(ge=1, le=50)
    mapping: dict[str, StrictInt | None]


@app.get("/api/manual-tenders/template", dependencies=[Depends(require_authenticated)])
def download_manual_tender_template():
    filename = "Averon_Шаблон_тендера_v1.xlsx"
    return Response(
        content=tender_template_service.bytes(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''Averon_%D0%A8%D0%B0%D0%B1%D0%BB%D0%BE%D0%BD_%D1%82%D0%B5%D0%BD%D0%B4%D0%B5%D1%80%D0%B0_v1.xlsx"},
    )


@app.post("/api/manual-tenders/previews", status_code=202, dependencies=[Depends(require_authenticated)])
async def create_manual_tender_preview(file: UploadFile = File(...), user: CurrentUser = Depends(require_authenticated)):
    filename = file.filename or "tender.xlsx"
    payload = bytearray()
    digest = hashlib.sha256()
    while chunk := await file.read(64 * 1024):
        payload.extend(chunk)
        if len(payload) > MAX_MANUAL_TENDER_UPLOAD_BYTES:
            raise HTTPException(413, detail={"code": "TENDER_UPLOAD_TOO_LARGE", "message": "Размер XLSX превышает 5 МиБ."})
        digest.update(chunk)
    if not payload:
        raise HTTPException(400, detail={"code": "TENDER_XLSX_EMPTY", "message": "Файл XLSX пуст."})
    owner_id = _tender_owner_key(user)
    existing = tender_repository.find_preview_by_hash(owner_id, digest.hexdigest())
    if existing:
        return {"preview_id": existing["preview_id"], "status": existing["status"], "job_id": None, "deduplicated": True}
    metadata = None
    try:
        metadata = tender_repository.reserve_preview(owner_id, filename, len(payload), digest.hexdigest())
        if metadata.get("deduplicated"):
            return {"preview_id": metadata["preview_id"], "status": metadata["status"], "job_id": None, "deduplicated": True}
        tender_repository.write_preview_source(metadata["preview_id"], bytes(payload))
        job = _submit_tender_parse_job(metadata["preview_id"], owner_id)
        return {"preview_id": metadata["preview_id"], "status": "queued", "job_id": job.id, "deduplicated": False}
    except Exception as exc:
        try:
            if metadata is not None and not metadata.get("deduplicated"):
                tender_repository.update_preview(metadata["preview_id"], owner_id, status="failed", error="Не удалось поставить файл в очередь. Повторите загрузку.")
        except Exception:
            pass
        if isinstance(exc, HTTPException):
            raise
        raise _tender_http_error(exc) from exc


@app.get("/api/manual-tenders/previews/{preview_id}", dependencies=[Depends(require_authenticated)])
def get_manual_tender_preview(preview_id: str, user: CurrentUser = Depends(require_authenticated)):
    try:
        lease = tender_activity.acquire(preview_id)
        try:
            return tender_repository.public_preview(preview_id, _tender_owner_key(user))
        finally:
            lease.release()
    except Exception as exc:
        raise _tender_http_error(exc) from exc


@app.post("/api/manual-tenders/previews/{preview_id}/mapping", status_code=202, dependencies=[Depends(require_authenticated)])
def map_manual_tender_preview(preview_id: str, request: TenderMappingBody, user: CurrentUser = Depends(require_authenticated)):
    owner_id = _tender_owner_key(user)
    try:
        request_lease = tender_activity.acquire(preview_id)
    except Exception as exc:
        raise _tender_http_error(exc) from exc
    try:
        _, metadata = tender_repository.preview_path(preview_id, owner_id)
        analysis = metadata.get("analysis") or {}
        if not analysis or analysis.get("official_template"):
            raise TenderWorkspaceError("Для официального шаблона сопоставление не требуется.", 409, "TENDER_MAPPING_NOT_ALLOWED")
        candidate = next((
            item for item in analysis.get("header_candidates", [])
            if item.get("sheet_name") == request.sheet_name and item.get("header_row") == request.header_row
        ), None)
        if candidate is None:
            raise TenderWorkspaceError("Лист или строка заголовка изменились. Загрузите файл повторно для новой проверки.", 409, "TENDER_MAPPING_SCOPE_CHANGED")
        tender_parser.validate_mapping_override(request.mapping, candidate.get("headers", []))
        previous_status = metadata.get("status", "ready")
        tender_repository.update_preview(preview_id, owner_id, status="queued", error=None)
        try:
            job = _submit_tender_parse_job(preview_id, owner_id, mapping=request.mapping, selected_sheet=request.sheet_name, selected_header_row=request.header_row)
        except HTTPException as exc:
            tender_repository.update_preview(preview_id, owner_id, status=previous_status, error=None)
            raise exc
        return {"preview_id": preview_id, "status": "queued", "job_id": job.id}
    except Exception as exc:
        raise _tender_http_error(exc) from exc
    finally:
        request_lease.release()


@app.post("/api/manual-tenders/previews/{preview_id}/confirm", status_code=201, dependencies=[Depends(require_authenticated)])
def confirm_manual_tender_preview(preview_id: str, user: CurrentUser = Depends(require_authenticated)):
    try:
        lease = tender_activity.acquire(preview_id)
        try:
            return tender_repository.confirm(preview_id, _tender_owner_key(user))
        finally:
            lease.release()
    except Exception as exc:
        raise _tender_http_error(exc) from exc


@app.get("/api/manual-tenders/{tender_id}", dependencies=[Depends(require_authenticated)])
def get_manual_tender(tender_id: str, user: CurrentUser = Depends(require_authenticated)):
    try:
        lease = tender_activity.acquire(tender_id)
        try:
            return tender_repository.public_workspace(tender_id, _tender_owner_key(user))
        finally:
            lease.release()
    except Exception as exc:
        raise _tender_http_error(exc) from exc


@app.delete("/api/manual-tenders/{tender_id}", dependencies=[Depends(require_authenticated)])
def delete_manual_tender(tender_id: str, user: CurrentUser = Depends(require_authenticated)):
    try:
        tender_repository.delete(tender_id, _tender_owner_key(user))
        return {"deleted": True}
    except Exception as exc:
        raise _tender_http_error(exc) from exc


@app.post("/api/admin/one-c-history/previews", dependencies=[Depends(require_admin)])
@app.post("/api/one-c-history/previews")
async def create_one_c_history_preview(
    file: UploadFile = File(...),
    profile_id: str | None = Form(default=None),
    user: CurrentUser = Depends(require_one_c_history_import),
):
    try:
        return await one_c_history_service.create_preview(
            file, profile_id=profile_id, owner_key=_one_c_history_owner_key(user),
        )
    except Exception as exc:
        raise _one_c_history_http_error(exc) from exc


@app.post("/api/admin/one-c-history/previews/{preview_id}/mapping", dependencies=[Depends(require_admin)])
@app.post("/api/one-c-history/previews/{preview_id}/mapping")
async def analyze_one_c_history_preview(
    preview_id: str,
    request: PreviewMappingRequest,
    user: CurrentUser = Depends(require_one_c_history_import),
):
    try:
        return await one_c_history_service.analyze_preview(
            preview_id, request, owner_key=_one_c_history_owner_key(user),
        )
    except Exception as exc:
        raise _one_c_history_http_error(exc) from exc


@app.get("/api/admin/one-c-history/previews/{preview_id}/sheets/{sheet_name}", dependencies=[Depends(require_admin)])
@app.get("/api/one-c-history/previews/{preview_id}/sheets/{sheet_name}")
async def inspect_one_c_history_sheet(
    preview_id: str,
    sheet_name: str,
    header_row: int | None = Query(default=None, ge=1, le=50),
    group_header_row: int | None = Query(default=None, ge=1, le=50),
    event_header_row: int | None = Query(default=None, ge=1, le=50),
    user: CurrentUser = Depends(require_one_c_history_import),
):
    try:
        return await one_c_history_service.inspect_preview_sheet(
            preview_id, sheet_name, header_row,
            group_header_row=group_header_row,
            event_header_row=event_header_row,
            owner_key=_one_c_history_owner_key(user),
        )
    except Exception as exc:
        raise _one_c_history_http_error(exc) from exc


@app.post("/api/admin/one-c-history/imports", dependencies=[Depends(require_admin)])
@app.post("/api/one-c-history/imports")
async def confirm_one_c_history_import(
    request: ImportMappingRequest,
    user: CurrentUser = Depends(require_one_c_history_import),
):
    try:
        return await one_c_history_service.import_confirmed(
            request,
            owner_key=_one_c_history_owner_key(user),
            actor=_one_c_history_actor(user),
            can_manage_profiles=user.capabilities["settings"],
        )
    except Exception as exc:
        raise _one_c_history_http_error(exc) from exc


@app.delete("/api/admin/one-c-history/profiles/{profile_id}", dependencies=[Depends(require_admin)])
def delete_one_c_history_profile(profile_id: str):
    if not one_c_history_repository.delete_profile(profile_id):
        raise HTTPException(status_code=404, detail="Профиль импорта не найден.")
    return {"deleted": True}


@app.get("/api/me")
def me(user: CurrentUser = Depends(require_authenticated)):
    return {**user.public(), "auth_mode": auth_config().mode}


def _auth_json_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


async def _auth_json_object(request: Request, *, allowed: set[str], required: set[str]) -> dict[str, Any]:
    """Read small auth payloads without reflecting submitted secrets in errors."""

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 8192:
            raise _auth_json_error(400, "INVALID_REQUEST")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _auth_json_error(400, "INVALID_REQUEST") from None
    if not isinstance(payload, dict) or set(payload) - allowed or required - set(payload):
        raise _auth_json_error(400, "INVALID_REQUEST")
    return payload


def _account_repository_call(method_name: str, *args: Any, **kwargs: Any) -> Any:
    """Resolve the lazy repository and execute its operation in the caller's worker."""

    return getattr(auth_repository, method_name)(*args, **kwargs)


def _password_value(payload: dict[str, Any]) -> str:
    value = payload.get("password")
    if not isinstance(value, str):
        raise _auth_json_error(422, "INVALID_PASSWORD")
    try:
        validate_password(value)
    except ValueError:
        raise _auth_json_error(422, "INVALID_PASSWORD") from None
    return value


def _set_auth_cookies(response: Response, session: dict[str, str]) -> None:
    max_age = DEFAULT_SESSION_TTL_SECONDS
    response.set_cookie(
        "averon_session",
        session["token"],
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        "averon_csrf",
        session["csrf_token"],
        max_age=max_age,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    for name, http_only in (("averon_session", True), ("averon_csrf", False)):
        response.set_cookie(
            name,
            "",
            max_age=0,
            httponly=http_only,
            secure=True,
            samesite="strict",
            path="/",
            expires=0,
        )


@app.post("/api/auth/login")
async def login(request: Request):
    if auth_config().mode != "session":
        raise HTTPException(status_code=404, detail="Not found")
    payload = await _auth_json_object(request, allowed={"username", "password"}, required={"username", "password"})
    username = payload["username"]
    password = payload["password"]
    bucket = login_bucket_key(username)
    retry_after = login_rate_limiter.retry_after(bucket)
    generic_error = {"detail": "Неверный логин или пароль"}
    if retry_after:
        return JSONResponse(generic_error, status_code=429, headers={"Retry-After": str(retry_after)})
    if not login_work_guard.try_acquire():
        return JSONResponse(generic_error, status_code=429, headers={"Retry-After": "1"})
    try:
        result = await run_in_threadpool(_authenticate_and_create_session, username, password)
    except Exception as exc:
        logger.exception("Account login operation failed")
        raise HTTPException(status_code=503, detail="Сервис аутентификации временно недоступен") from exc
    finally:
        login_work_guard.release()
    if result is None:
        login_rate_limiter.failed(bucket)
        return JSONResponse(generic_error, status_code=401)
    login_rate_limiter.succeeded(bucket)
    public_user, session = result
    response = JSONResponse({"user": public_user})
    _set_auth_cookies(response, session)
    return response


def _authenticate_and_create_session(username: object, password: object) -> tuple[dict[str, Any], dict[str, str]] | None:
    user = auth_repository.get_auth_user(username) if isinstance(username, str) else None
    if isinstance(password, str) and len(password) <= 128:
        if user is None:
            dummy_password_verification(password)
            password_matches = False
        else:
            password_matches = verify_password(password, user["password_hash"])
    else:
        dummy_password_verification(password if isinstance(password, str) else "")
        password_matches = False

    if user is None or not bool(user["enabled"]) or not password_matches:
        return None
    try:
        session = auth_repository.create_session(user["user_id"])
    except AccountDisabled:
        return None
    public_user = CurrentUser(
        username=user["username"], role=Role(user["role"]), user_id=user["user_id"]
    ).public()
    return public_user, session


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request, _user: CurrentUser = Depends(require_authenticated)):
    if auth_config().mode == "session":
        try:
            auth_repository.delete_session(request.cookies.get("averon_session", ""))
        except Exception as exc:
            logger.exception("Could not invalidate account session")
            raise HTTPException(status_code=503, detail="Сервис аутентификации временно недоступен") from exc
    response = Response(status_code=204)
    _clear_auth_cookies(response)
    return response


@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
def list_account_users(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    try:
        users = auth_repository.list_users(limit=limit, offset=offset)
    except Exception as exc:
        logger.exception("Could not list app-owned accounts")
        raise HTTPException(status_code=503, detail="Сервис аутентификации временно недоступен") from exc
    return {"users": users, "limit": limit, "offset": offset}


@app.post("/api/admin/users", status_code=201)
async def create_account_user(request: Request, _admin: CurrentUser = Depends(require_admin)):
    payload = await _auth_json_object(request, allowed={"username", "password"}, required={"username", "password"})
    username = payload["username"]
    password = _password_value(payload)
    if not isinstance(username, str):
        raise _auth_json_error(422, "INVALID_USERNAME")
    try:
        user = await run_in_threadpool(_account_repository_call, "create_user", username, password, role="user")
    except DuplicateUsername:
        raise _auth_json_error(409, "USERNAME_EXISTS") from None
    except ValueError:
        raise _auth_json_error(422, "INVALID_USERNAME") from None
    except Exception as exc:
        logger.exception("Could not create app-owned account")
        raise HTTPException(status_code=503, detail="Сервис аутентификации временно недоступен") from exc
    return {"user": user}


async def _is_current_account(target_user_id: str, current_user: CurrentUser) -> bool:
    if current_user.user_id is not None:
        return current_user.user_id == target_user_id
    try:
        target = await run_in_threadpool(_account_repository_call, "get_user", target_user_id)
    except Exception as exc:
        logger.exception("Could not resolve account for self-protection check")
        raise HTTPException(status_code=503, detail="Сервис аутентификации временно недоступен") from exc
    if target is None:
        return False
    try:
        return normalize_username(target["username"]) == normalize_username(current_user.username)
    except ValueError:
        return False


def _version_value(payload: dict[str, Any]) -> int:
    value = payload.get("version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _auth_json_error(422, "INVALID_VERSION")
    return value


@app.patch("/api/admin/users/{user_id}")
async def update_account_user(user_id: str, request: Request, admin: CurrentUser = Depends(require_admin)):
    payload = await _auth_json_object(request, allowed={"enabled", "version"}, required={"enabled", "version"})
    if not isinstance(payload["enabled"], bool):
        raise _auth_json_error(422, "INVALID_ENABLED")
    version = _version_value(payload)
    if not payload["enabled"] and await _is_current_account(user_id, admin):
        raise _auth_json_error(409, "SELF_PROTECTION")
    try:
        user = await run_in_threadpool(
            _account_repository_call,
            "update_enabled",
            user_id,
            enabled=payload["enabled"],
            version=version,
        )
    except UserNotFound:
        raise _auth_json_error(404, "USER_NOT_FOUND") from None
    except StaleUserVersion:
        raise _auth_json_error(409, "STALE_USER_VERSION") from None
    except AdminAccountProtected:
        raise _auth_json_error(409, "ADMIN_ACCOUNT_PROTECTED") from None
    except Exception as exc:
        logger.exception("Could not update app-owned account")
        raise HTTPException(status_code=503, detail="Сервис аутентификации временно недоступен") from exc
    return {"user": user}


@app.post("/api/admin/users/{user_id}/reset-password")
async def reset_account_password(user_id: str, request: Request, admin: CurrentUser = Depends(require_admin)):
    payload = await _auth_json_object(request, allowed={"password", "version"}, required={"password", "version"})
    password = _password_value(payload)
    version = _version_value(payload)
    if await _is_current_account(user_id, admin):
        raise _auth_json_error(409, "SELF_PROTECTION")
    try:
        user = await run_in_threadpool(
            _account_repository_call,
            "reset_password",
            user_id,
            password,
            version=version,
        )
    except UserNotFound:
        raise _auth_json_error(404, "USER_NOT_FOUND") from None
    except StaleUserVersion:
        raise _auth_json_error(409, "STALE_USER_VERSION") from None
    except AdminAccountProtected:
        raise _auth_json_error(409, "ADMIN_ACCOUNT_PROTECTED") from None
    except Exception as exc:
        logger.exception("Could not reset app-owned account password")
        raise HTTPException(status_code=503, detail="Сервис аутентификации временно недоступен") from exc
    return {"user": user}


@app.delete("/api/admin/users/{user_id}")
async def delete_account_user(user_id: str, request: Request, admin: CurrentUser = Depends(require_admin)):
    payload = await _auth_json_object(request, allowed={"version"}, required={"version"})
    version = _version_value(payload)
    if await _is_current_account(user_id, admin):
        raise _auth_json_error(409, "SELF_PROTECTION")
    try:
        await run_in_threadpool(_account_repository_call, "delete_user", user_id, version=version)
    except UserNotFound:
        raise _auth_json_error(404, "USER_NOT_FOUND") from None
    except StaleUserVersion:
        raise _auth_json_error(409, "STALE_USER_VERSION") from None
    except AdminAccountProtected:
        raise _auth_json_error(409, "ADMIN_ACCOUNT_PROTECTED") from None
    except Exception as exc:
        logger.exception("Could not delete app-owned account")
        raise HTTPException(status_code=503, detail="Сервис аутентификации временно недоступен") from exc
    return Response(status_code=204)


@app.get("/api/config", dependencies=[Depends(require_authenticated)])
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


class LemanaB2BSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool | None = None
    environment: Literal["test", "prod"] | None = None
    client_id: str | None = None
    region_id: int | None = None
    request_timeout_s: float | None = None


class EtmIproSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool | None = None
    environment: Literal["test", "prod"] | None = None
    warehouse_codes: list[str] | str | None = None
    request_timeout_s: float | None = None
    base_url_override: str | None = None
    max_live_candidates: int | None = None


class SourcingSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str | None = None
    demo_store_base_url: str | None = None
    lemana_b2b: LemanaB2BSettingsUpdate | None = None
    etm_ipro: EtmIproSettingsUpdate | None = None


class SettingsUpdate(BaseModel):
    """API keys are write-only and are stored in separate SecretStore entries."""

    model_config = ConfigDict(extra="ignore")

    processing_mode: Literal["local", "cloud", "hybrid"] | None = None
    local: LocalSettingsUpdate | None = None
    yandex: YandexSettingsUpdate | None = None
    pipeline: PipelineSettingsUpdate | None = None
    sourcing: SourcingSettingsUpdate | None = None
    api_key: str | None = None
    ai_api_key: str | None = None
    # Write-only: stored in SecretStore, never in settings.json or responses.
    lemana_client_secret: str | None = None
    etm_login: str | None = None
    etm_password: str | None = None
    delete_yandex_api_key: bool = False
    delete_yandex_ai_api_key: bool = False
    delete_lemana_client_secret: bool = False
    delete_etm_login: bool = False
    delete_etm_password: bool = False


def _settings_public() -> dict:
    payload = app_settings_service.public()
    vision_key_configured = resolve_secret(
        os.environ.get("AVERON_YANDEX_VISION_API_KEY"),
        secret_store,
        YANDEX_API_KEY,
    ) is not None
    ai_key_configured = resolve_secret(
        os.environ.get("AVERON_YANDEX_AI_API_KEY"),
        secret_store,
        YANDEX_AI_API_KEY,
    ) is not None
    # Keep the legacy field as the Vision/OCR status.  It must not mean
    # "either credential is present".
    payload["yandex"]["api_key_configured"] = vision_key_configured
    payload["yandex"]["vision_api_key_configured"] = vision_key_configured
    payload["yandex"]["ai_api_key_configured"] = ai_key_configured
    payload["sourcing"]["lemana_b2b"]["client_secret_configured"] = resolve_secret(
        os.environ.get("AVERON_LEMANA_B2B_CLIENT_SECRET"),
        secret_store,
        LEMANA_B2B_CLIENT_SECRET,
    ) is not None
    payload["sourcing"]["etm_ipro"]["login_configured"] = resolve_secret(
        os.environ.get("AVERON_ETM_IPRO_LOGIN"), secret_store, ETM_IPRO_LOGIN
    ) is not None
    payload["sourcing"]["etm_ipro"]["password_configured"] = resolve_secret(
        os.environ.get("AVERON_ETM_IPRO_PASSWORD"), secret_store, ETM_IPRO_PASSWORD
    ) is not None
    payload["secret_backend"] = secret_store.backend_name
    payload["secret_insecure"] = secret_store.is_insecure
    return payload


@app.get("/api/settings", dependencies=[Depends(require_admin)])
def get_settings():
    return _settings_public()


@app.put("/api/settings", dependencies=[Depends(require_admin)])
def put_settings(request: SettingsUpdate):
    global demo_store_provider, sourcing_provider, sourcing_repository, sourcing_runtime, sourcing_service

    if request.api_key is not None and request.api_key.strip():
        secret_store.set(YANDEX_API_KEY, request.api_key.strip())
    if request.ai_api_key is not None and request.ai_api_key.strip():
        secret_store.set(YANDEX_AI_API_KEY, request.ai_api_key.strip())
    if request.lemana_client_secret is not None and request.lemana_client_secret.strip():
        secret_store.set(LEMANA_B2B_CLIENT_SECRET, request.lemana_client_secret.strip())
    if request.etm_login is not None and request.etm_login.strip():
        secret_store.set(ETM_IPRO_LOGIN, request.etm_login.strip())
    if request.etm_password is not None and request.etm_password.strip():
        secret_store.set(ETM_IPRO_PASSWORD, request.etm_password.strip())
    if request.delete_yandex_api_key:
        secret_store.delete(YANDEX_API_KEY)
    if request.delete_yandex_ai_api_key:
        secret_store.delete(YANDEX_AI_API_KEY)
    if request.delete_lemana_client_secret:
        secret_store.delete(LEMANA_B2B_CLIENT_SECRET)
    if request.delete_etm_login:
        secret_store.delete(ETM_IPRO_LOGIN)
    if request.delete_etm_password:
        secret_store.delete(ETM_IPRO_PASSWORD)
    patch = request.model_dump(
        exclude_none=True,
        exclude={
            "api_key",
            "ai_api_key",
            "lemana_client_secret",
            "delete_yandex_api_key",
            "delete_yandex_ai_api_key",
            "delete_lemana_client_secret",
            "etm_login",
            "etm_password",
            "delete_etm_login",
            "delete_etm_password",
        },
    )
    patch = {key: value for key, value in patch.items() if value is not None}
    try:
        app_settings_service.update(patch)
        _rebuild_sourcing_runtime()
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()))
        raise HTTPException(400, f"Недопустимые настройки {location}: {first.get('msg', '')}") from exc
    return _settings_public()


@app.delete("/api/settings/yandex-api-key", dependencies=[Depends(require_admin)])
def delete_yandex_api_key():
    secret_store.delete(YANDEX_API_KEY)
    return {"deleted": True}


@app.delete("/api/settings/yandex-ai-api-key", dependencies=[Depends(require_admin)])
def delete_yandex_ai_api_key():
    secret_store.delete(YANDEX_AI_API_KEY)
    return {"deleted": True}


@app.delete("/api/settings/lemana-client-secret", dependencies=[Depends(require_admin)])
def delete_lemana_client_secret():
    secret_store.delete(LEMANA_B2B_CLIENT_SECRET)
    _rebuild_sourcing_runtime()
    return {"deleted": True}


@app.delete("/api/settings/etm-ipro-login", dependencies=[Depends(require_admin)])
def delete_etm_ipro_login():
    secret_store.delete(ETM_IPRO_LOGIN)
    _rebuild_sourcing_runtime()
    return {"deleted": True}


@app.delete("/api/settings/etm-ipro-password", dependencies=[Depends(require_admin)])
def delete_etm_ipro_password():
    secret_store.delete(ETM_IPRO_PASSWORD)
    _rebuild_sourcing_runtime()
    return {"deleted": True}


@app.post("/api/documents", dependencies=[Depends(require_authenticated)])
async def upload_document(file: UploadFile = File(...)):
    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Поддерживаются только PDF-файлы")
    max_bytes = 250 * 1024 * 1024
    total = 0
    source_digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
        temp_path = Path(temporary.name)
        try:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, "Размер PDF превышает 250 МБ")
                source_digest.update(chunk)
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
            source_sha256=source_digest.hexdigest(),
        )
        metadata = workspace_service.read_json(workspace.metadata_path)
        return _public_document_metadata(metadata)
    except Exception as exc:
        raise HTTPException(400, f"Не удалось открыть PDF: {exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


@app.get("/api/documents", dependencies=[Depends(require_authenticated)])
def list_documents(limit: int = 50):
    return {"documents": workspace_service.list_recent(limit)}


@app.delete("/api/admin/documents/{document_id}", dependencies=[Depends(require_admin)])
def delete_document_workspace(document_id: str):
    try:
        document_id = validate_document_id(document_id)
    except ValueError as exc:
        raise HTTPException(404, "Документ не найден") from exc
    if not document_activity_registry.begin_delete(document_id):
        raise HTTPException(
            409,
            "Документ сейчас используется. Повторите удаление после завершения операции.",
        )

    tombstone: Path | None = None
    try:
        with document_mutation_locks.for_document(document_id):
            # Re-check under the canonical mutation lock after blocking new
            # lifecycle leases. The directory rename is atomic on this volume.
            workspace_service.get(document_id)
            tombstone = workspace_service.move_to_tombstone(document_id)
    except FileNotFoundError as exc:
        document_activity_registry.cancel_delete(document_id)
        raise HTTPException(404, "Документ не найден") from exc
    except OSError as exc:
        document_activity_registry.cancel_delete(document_id)
        logger.exception("Unable to move document workspace to deletion tombstone")
        raise HTTPException(500, "Не удалось безопасно удалить документ.") from exc

    freed_bytes: int | None = None
    try:
        try:
            freed_bytes = workspace_service.tree_size_bytes(tombstone)
        except OSError:
            logger.exception("Unable to determine deleted workspace size: %s", tombstone)
        shutil.rmtree(tombstone)
    except OSError as exc:
        document_activity_registry.finish_delete(document_id)
        logger.exception("Workspace tombstone requires administrator cleanup: %s", tombstone)
        content = {
            "detail": "Документ удалён из списка, но не удалось полностью очистить его файлы.",
            "error": {"code": "DOCUMENT_REMOVED_CLEANUP_PENDING", "deleted": True},
        }
        if freed_bytes is not None:
            content["freed_bytes"] = freed_bytes
        return JSONResponse(status_code=500, content=content)
    document_activity_registry.finish_delete(document_id)
    return {"deleted": True, "document_id": document_id, "freed_bytes": freed_bytes}


@app.get(
    "/api/documents/{document_id}",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def get_document(document_id: str):
    try:
        workspace = workspace_service.get(document_id)
        metadata = workspace_service.read_json(workspace.metadata_path)
        if not isinstance(metadata, dict):
            raise HTTPException(404, "Метаданные документа недоступны")
        return {
            **_public_document_metadata(metadata),
            "has_result": workspace.result_path.is_file(),
            "has_review_decisions": workspace.review_decisions_path.is_file(),
        }
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(409, "Метаданные документа повреждены") from exc


@app.get(
    "/api/documents/{document_id}/page/{page_number}",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
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
        return document_file_response(document_id, cache, media_type="image/png")
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


@app.post(
    "/api/documents/{document_id}/suggest-pages",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def suggest_pages(
    document_id: str,
    user: CurrentUser = Depends(require_authenticated),
):
    try:
        workspace = workspace_service.get(document_id)
        metadata = workspace_service.read_json(workspace.metadata_path)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc

    def run(progress):
        return recognition_service.suggest_pages(
            workspace.pdf_path, workspace.pages_dir, metadata["page_count"], progress
        )

    return _submit_document_job(
        document_id,
        run,
        kind="suggest_pages",
        owner_id=_job_owner_id(user),
        dedupe_key=stable_fingerprint({"kind": "suggest_pages", "document_id": document_id}),
    ).public()


@app.post(
    "/api/documents/{document_id}/recognize",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def recognize(
    document_id: str,
    request: RecognitionRequest,
    user: CurrentUser = Depends(require_authenticated),
):
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

    with document_mutation_locks.for_document(document_id):
        before_recognition = workspace_service.read_json(workspace.result_path, default={})
        recognition_base_revision = _result_revision(before_recognition)

    def run(progress):
        result = coordinator.process_document(
            workspace.pdf_path, request.processing_mode, options, progress
        )
        with document_mutation_locks.for_document(document_id):
            current = workspace_service.read_json(workspace.result_path, default={})
            if _result_revision(current) != recognition_base_revision:
                raise RuntimeError(
                    "Документ изменён во время распознавания. Новый результат не применён."
                )
            store = ReviewDecisionStore(workspace.review_decisions_path)
            decisions, ledger_revision = store.load_snapshot()
            document_fingerprint = _source_fingerprint(workspace)
            result = human_review_service.apply_saved_decisions(
                result, decisions, document_fingerprint
            )
            result["review_ledger_revision"] = ledger_revision
            result["review_projection_version"] = REVIEW_PROJECTION_VERSION
            result["revision"] = recognition_base_revision + 1
            workspace_service.write_json(workspace.result_path, result)
        return result

    job = _submit_document_job(
        document_id,
        run,
        kind="recognition",
        owner_id=_job_owner_id(user),
        dedupe_key=stable_fingerprint({
            "kind": "recognition",
            "document_id": document_id,
            "base_revision": recognition_base_revision,
            "pages": pages,
            "crop": crop,
            "dpi": request.dpi,
            "processing_mode": request.processing_mode,
            "ocr_mode": request.ocr_mode,
            "ai_provider": request.ai_provider,
        }),
    )
    return job.public()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: CurrentUser = Depends(require_authenticated)):
    try:
        return job_service.get_public(job_id, owner_id=_job_owner_id(user))
    except KeyError as exc:
        raise HTTPException(
            404,
            detail={"code": "JOB_NOT_FOUND", "message": "Задание не найдено"},
        ) from exc


@app.get(
    "/api/documents/{document_id}/results",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def get_results(document_id: str):
    try:
        workspace = workspace_service.get(document_id)
        with document_mutation_locks.for_document(document_id):
            result = workspace_service.read_json(workspace.result_path)
            if not result:
                raise HTTPException(404, "Результат распознавания отсутствует")
            result, _recovered = _reconcile_review_projection_locked(workspace, result)
            return result
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc
    except ReviewDecisionLedgerCorrupt as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(
            409,
            "Сохранённый результат повреждён. Повторите распознавание документа.",
        ) from exc


def _restore_server_owned_review_state(
    requested_rows: list[dict[str, Any]], canonical_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep OCR evidence and human audit state server-owned across row saves."""
    canonical_by_id = {
        str(row.get("id")): row
        for row in canonical_rows
        if row.get("id") is not None
    }
    if len(requested_rows) != len(canonical_rows):
        raise HTTPException(409, "Набор строк изменился. Обновите результат документа.")

    evidence_fields = (
        "id",
        "page",
        "physical_row_refs",
        "ocr_metadata",
        "value_candidates",
        "review_reasons",
        "review_reason",
        "critical_blockers",
        "critical_fields",
        "secondary_conflict_fields",
        "human_verified_fields",
        "human_verified_field_values",
        "human_confirmed_absent_fields",
        "human_rejected_candidates",
        "human_verified_relations",
        "human_review",
        "verification_state",
        "semantic_authoritative",
        "semantic_required_critical_fields",
        "semantic_structural_impact",
        "semantic_review",
        "semantic_state",
    )
    editable_fields = (
        "position",
        "name",
        "type_mark",
        "code",
        "manufacturer",
        "unit",
        "quantity",
        "mass",
        "note",
        "section",
        "system",
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for submitted in requested_rows:
        row_id = str(submitted.get("id") or "")
        canonical = canonical_by_id.get(row_id)
        if canonical is None or row_id in seen:
            raise HTTPException(409, "Строка больше не соответствует сохранённому результату.")
        seen.add(row_id)
        row = dict(submitted)
        for key in evidence_fields:
            if key in canonical:
                canonical_value = canonical[key]
                if key == "human_confirmed_absent_fields" and isinstance(canonical_value, dict):
                    canonical_value = {
                        field: dict(record) if isinstance(record, dict) else record
                        for field, record in canonical_value.items()
                    }
                    submitted_absences = submitted.get(key)
                    if isinstance(submitted_absences, dict):
                        for field, record in canonical_value.items():
                            requested_record = submitted_absences.get(field)
                            if (
                                isinstance(record, dict)
                                and isinstance(requested_record, dict)
                                and requested_record.get("invalidated") is True
                                and requested_record.get("decision_key") == record.get("decision_key")
                                and requested_record.get("decision_id") == record.get("decision_id")
                                and requested_record.get("evidence_fingerprint") == record.get("evidence_fingerprint")
                            ):
                                record["invalidated"] = True
                row[key] = canonical_value
            else:
                row.pop(key, None)
        edited = list(canonical.get("edited_fields") or [])
        for key in editable_fields:
            if row.get(key) != canonical.get(key) and key not in edited:
                edited.append(key)
        row["edited_fields"] = edited
        if critical_blockers_for_row(canonical):
            # A client status/type edit cannot turn a blocked OCR row into a
            # verified or non-output row and thereby bypass strict export.
            row["row_type"] = canonical.get("row_type")
            row["status"] = "review"
        result.append(row)
    if seen != set(canonical_by_id):
        raise HTTPException(409, "Набор строк изменился. Обновите результат документа.")
    return result


@app.put(
    "/api/documents/{document_id}/results",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def save_results(document_id: str, request: SaveRowsRequest):
    try:
        workspace = workspace_service.get(document_id)
        with document_mutation_locks.for_document(document_id):
            existing = workspace_service.read_json(workspace.result_path, default={})
            if not existing:
                raise HTTPException(404, "Результат распознавания отсутствует")
            current_revision = _result_revision(existing)
            if request.expected_revision != current_revision:
                raise HTTPException(409, "Документ изменён. Обновите данные перед сохранением.")
            existing["rows"] = refresh_rows(
                _restore_server_owned_review_state(
                    request.rows,
                    existing.get("rows") or [],
                )
            )
            store = ReviewDecisionStore(workspace.review_decisions_path)
            decisions, ledger_revision = store.load_snapshot()
            existing = human_review_service.apply_saved_decisions(
                existing,
                decisions,
                _source_fingerprint(workspace),
            )
            rows = existing.get("rows") or []
            existing["summary"] = recognition_service._summary(
                rows, existing.get("errors", [])
            )
            existing["review_ledger_revision"] = ledger_revision
            existing["review_projection_version"] = REVIEW_PROJECTION_VERSION
            existing["revision"] = current_revision + 1
            workspace_service.write_json(workspace.result_path, existing)
            return {
                "saved": True,
                "revision": existing["revision"],
                "summary": existing["summary"],
                "result": existing,
            }
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc
    except ReviewDecisionLedgerCorrupt as exc:
        raise HTTPException(409, str(exc)) from exc


def safe_filename(value: str) -> str:
    value = Path(value or "averon_import.xlsx").name
    value = re.sub(r"[^\w\-. ()А-Яа-яЁё]", "_", value)
    if not value.lower().endswith(".xlsx"):
        value += ".xlsx"
    return value[:160]


def review_export_filename(value: str) -> str:
    filename = safe_filename(value)
    path = Path(filename)
    if path.stem.lower().endswith("_review"):
        return filename
    return safe_filename(f"{path.stem}_review.xlsx")


def _safe_document_snapshot_metadata(workspace) -> dict[str, Any]:
    try:
        metadata = workspace_service.read_json(workspace.metadata_path, default={})
    except (OSError, ValueError, TypeError):
        metadata = {}
    metadata = metadata if isinstance(metadata, dict) else {}
    filename = Path(str(metadata.get("filename") or "document.pdf")).name
    if not filename.lower().endswith(".pdf"):
        filename = "document.pdf"
    try:
        page_count = max(0, int(metadata.get("page_count") or 0))
    except (TypeError, ValueError):
        page_count = 0
    try:
        size = max(0, int(metadata.get("size") or 0))
    except (TypeError, ValueError):
        size = 0
    return {
        "filename": filename,
        "title": str(metadata.get("title") or Path(filename).stem)[:500],
        "page_count": page_count,
        "size": size,
    }


def _safe_export_validation_message(error: ValueError) -> str:
    message = str(error).strip()
    if not message or len(message) > 500 or re.search(r"(?:[A-Za-z]:[\\/]|\\\\|/)", message):
        return "Не удалось выполнить проверку экспорта."
    return message


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.exception("Unable to clean up temporary export artifact")


def _export_error_response(
    *,
    status_code: int,
    error_code: str,
    message: str,
    incident_id: str | None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": message,
            "error": {
                "code": error_code,
                "incident_id": incident_id,
                "reportable": incident_id is not None,
            },
        },
    )


def _record_export_incident(
    *,
    document_id: str,
    user: CurrentUser,
    request: ExportRequest,
    workspace,
    stored_result: dict[str, Any],
    resolved_filename: str,
    error_code: str,
    public_message: str,
    http_status: int,
) -> str | None:
    try:
        incident = support_repository.create_export_incident(
            document_id=document_id,
            username=user.username,
            role=user.role.value,
            app_version=APP_VERSION,
            export_kind="review" if request.review_export else "production",
            requested_filename=safe_filename(request.filename),
            row_count=len(request.rows),
            document=_safe_document_snapshot_metadata(workspace),
            export_request=request.model_dump(mode="json"),
            page_statuses=stored_result.get("page_statuses") or {},
            result_summary=stored_result.get("summary") or {},
            resolved_filename=resolved_filename,
            error_code=error_code,
            public_message=public_message,
            http_status=http_status,
        )
        return str(incident["incident_id"])
    except Exception:
        logger.exception("Unable to persist export failure incident for document %s", document_id)
        return None


@app.post(
    "/api/documents/{document_id}/export",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def export(
    document_id: str,
    request: ExportRequest,
    user: CurrentUser = Depends(require_authenticated),
):
    try:
        workspace = workspace_service.get(document_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc

    try:
        with document_mutation_locks.for_document(document_id):
            stored_result = workspace_service.read_json(workspace.result_path, default={})
            if not isinstance(stored_result, dict) or not stored_result:
                raise HTTPException(404, "Результат распознавания отсутствует")
            # Export requires audit integrity even when the result and revision
            # marker agree. Validate the complete persisted ledger before taking
            # the revision-fenced snapshot; malformed history must fail closed.
            ledger_snapshot = ReviewDecisionStore(
                workspace.review_decisions_path
            ).load_snapshot()
            stored_result, _recovered = _reconcile_review_projection_locked(
                workspace, stored_result, ledger_snapshot=ledger_snapshot
            )
            current_revision = _result_revision(stored_result)
            if request.expected_revision != current_revision:
                raise HTTPException(
                    409,
                    "Документ изменён. Обновите данные перед экспортом.",
                )
            page_statuses = stored_result.get("page_statuses") or {}
            # Production exports always use canonical saved rows. Review exports
            # intentionally preserve the browser's inspection snapshot, including
            # unsaved edits, while fencing it to the current document revision.
            export_rows = (
                request.rows
                if request.review_export
                else stored_result.get("rows") or []
            )
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc
    except ReviewDecisionLedgerCorrupt as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(
            409,
            "Не удалось проверить сохранённое состояние документа перед экспортом.",
        ) from exc

    filename = (
        review_export_filename(request.filename)
        if request.review_export
        else safe_filename(request.filename)
    )
    output: Path | None = None
    temporary_output: Path | None = None
    try:
        output = workspace.exports_dir / filename
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".averon-export-",
            suffix=".tmp",
            dir=workspace.exports_dir,
        )
        os.close(file_descriptor)
        temporary_output = Path(temporary_name)
        export_service.export(
            rows=export_rows,
            columns=request.columns,
            output_path=temporary_output,
            sheet_name=request.sheet_name,
            include_headers=request.include_headers,
            only_exportable=request.only_exportable,
            page_statuses=page_statuses,
            review_export=request.review_export,
            enforce_safety=not request.review_export,
        )
        os.replace(temporary_output, output)
        temporary_output = None
    except HTTPException:
        raise
    except ValueError as exc:
        public_message = _safe_export_validation_message(exc)
        incident_id = _record_export_incident(
            document_id=document_id,
            user=user,
            request=request,
            workspace=workspace,
            stored_result=stored_result,
            resolved_filename=filename,
            error_code="EXPORT_VALIDATION_FAILED",
            public_message=public_message,
            http_status=400,
        )
        return _export_error_response(
            status_code=400,
            error_code="EXPORT_VALIDATION_FAILED",
            message=public_message,
            incident_id=incident_id,
        )
    except Exception:
        logger.exception("Unexpected export failure for document %s", document_id)
        public_message = "Не удалось сформировать Excel."
        incident_id = _record_export_incident(
            document_id=document_id,
            user=user,
            request=request,
            workspace=workspace,
            stored_result=stored_result,
            resolved_filename=filename,
            error_code="EXPORT_FAILED",
            public_message=public_message,
            http_status=500,
        )
        return _export_error_response(
            status_code=500,
            error_code="EXPORT_FAILED",
            message=public_message,
            incident_id=incident_id,
        )
    finally:
        if temporary_output is not None:
            _best_effort_unlink(temporary_output)
    return document_file_response(
        document_id,
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


class SupportReportCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1, max_length=128)
    reporter_fio: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def normalize_text(self):
        self.incident_id = self.incident_id.strip()
        self.reporter_fio = self.reporter_fio.strip()
        self.description = self.description.strip()
        if len(self.reporter_fio) < 3:
            raise ValueError("ФИО должно содержать не менее 3 символов")
        if len(self.description) < 20:
            raise ValueError("Описание должно содержать не менее 20 символов")
        return self


class SupportReportStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["OPEN", "IN_PROGRESS", "RESOLVED"]
    version: int = Field(ge=1)


class SupportIncidentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    stage: Literal["document", "pages", "recognition", "review", "export"]


def _document_available(document_id: str) -> bool:
    try:
        return workspace_service.get(document_id).root.is_dir()
    except FileNotFoundError:
        return False


def _public_support_report(report: dict[str, Any]) -> dict[str, Any]:
    incident_id = str(report["incident_id"])
    incident = {
        "incident_id": incident_id,
        "document_id": report["document_id"],
        "created_at": report["incident_created_at"],
        "username": report["username"],
        "role": report["role"],
        "incident_kind": report["incident_kind"],
        "stage": report["stage"],
        "error_code": report["error_code"],
        "public_message": report["public_message"],
        "http_status": report["http_status"],
        "app_version": report["app_version"],
        "export_kind": report["export_kind"],
        "requested_filename": report["requested_filename"],
        "row_count": report["row_count"],
        "snapshot_sha256": report["snapshot_sha256"],
        "document_available": _document_available(str(report["document_id"])),
    }
    return {
        "report_id": report["report_id"],
        "incident_id": incident_id,
        "reporter_fio": report["reporter_fio"],
        "description": report.get("description"),
        "status": report["status"],
        "created_at": report["created_at"],
        "updated_at": report["updated_at"],
        "version": report["version"],
        "incident": incident,
    }


def _public_support_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_id": report["report_id"],
        "incident_id": report["incident_id"],
        "reporter_fio": report["reporter_fio"],
        "status": report["status"],
        "created_at": report["created_at"],
        "updated_at": report["updated_at"],
        "version": report["version"],
        "document_id": report["document_id"],
        "username": report["username"],
        "incident_kind": report["incident_kind"],
        "error_code": report["error_code"],
        "public_message": report["public_message"],
        "row_count": report["row_count"],
    }


@app.post("/api/support/incidents", dependencies=[Depends(require_authenticated)])
def create_support_incident(
    request: SupportIncidentCreateRequest,
    user: CurrentUser = Depends(require_authenticated),
):
    try:
        with document_activity_registry.lease(request.document_id):
            workspace = workspace_service.get(request.document_id)
            if not workspace.root.is_dir():
                raise FileNotFoundError(request.document_id)

            try:
                stored_result = workspace_service.read_json(workspace.result_path, default={})
            except (OSError, ValueError, TypeError):
                stored_result = {}
            stored_result = stored_result if isinstance(stored_result, dict) else {}
            rows = stored_result.get("rows")
            page_statuses = stored_result.get("page_statuses")
            result_summary = stored_result.get("summary")
            rows = rows if isinstance(rows, list) else []
            page_statuses = page_statuses if isinstance(page_statuses, (dict, list)) else {}
            result_summary = result_summary if isinstance(result_summary, dict) else {}

            try:
                incident = support_repository.create_user_reported_incident(
                    document_id=request.document_id,
                    username=user.username,
                    role=user.role.value,
                    app_version=APP_VERSION,
                    stage=request.stage,
                    document=_safe_document_snapshot_metadata(workspace),
                    rows=rows,
                    page_statuses=page_statuses,
                    result_summary=result_summary,
                )
            except Exception as exc:
                logger.exception("Unable to persist user-reported support incident")
                raise HTTPException(500, "Не удалось сохранить контекст обращения") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc
    return {
        "incident_id": incident["incident_id"],
        "incident_kind": INCIDENT_KIND_USER_REPORTED,
    }


@app.post("/api/support/reports", dependencies=[Depends(require_authenticated)])
def create_support_report(
    request: SupportReportCreateRequest,
    user: CurrentUser = Depends(require_authenticated),
):
    try:
        report = support_repository.create_report(
            incident_id=request.incident_id,
            reporter_fio=request.reporter_fio,
            description=request.description,
            username=user.username,
        )
    except IncidentNotFound as exc:
        raise HTTPException(404, "Инцидент не найден") from exc
    except ReportForbidden as exc:
        raise HTTPException(403, "Недостаточно прав для этого инцидента") from exc
    except DuplicateReport as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Отчёт по этому инциденту уже существует",
                "error": {"code": "SUPPORT_REPORT_EXISTS", "report_id": exc.report_id},
            },
        )
    return _public_support_report(report)


@app.get("/api/admin/support/reports", dependencies=[Depends(require_admin)])
def list_support_reports(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
):
    if status is not None and status not in REPORT_STATUSES:
        raise HTTPException(400, "Недопустимый статус отчёта")
    reports = support_repository.list_reports(limit=limit, offset=offset, status=status)
    return {
        "reports": [_public_support_summary(report) for report in reports],
        "limit": limit,
        "offset": offset,
        "status": status,
    }


@app.get("/api/admin/support/reports/{report_id}", dependencies=[Depends(require_admin)])
def get_support_report(report_id: str):
    try:
        return _public_support_report(support_repository.get_report(report_id))
    except ReportNotFound as exc:
        raise HTTPException(404, "Отчёт не найден") from exc


@app.get("/api/admin/support/reports/{report_id}/snapshot", dependencies=[Depends(require_admin)])
def get_support_snapshot(report_id: str):
    try:
        return support_repository.snapshot_for_report(report_id)
    except ReportNotFound as exc:
        raise HTTPException(404, "Отчёт не найден") from exc
    except SnapshotUnavailable as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Снимок инцидента недоступен",
                "error": {"code": "SNAPSHOT_UNAVAILABLE"},
            },
        )


@app.patch("/api/admin/support/reports/{report_id}", dependencies=[Depends(require_admin)])
def update_support_report(report_id: str, request: SupportReportStatusRequest):
    try:
        report = support_repository.update_report_status(
            report_id=report_id,
            status=request.status,
            version=request.version,
        )
    except ReportNotFound as exc:
        raise HTTPException(404, "Отчёт не найден") from exc
    except StaleReport as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Версия отчёта устарела",
                "error": {"code": "STALE_REPORT_VERSION"},
            },
        )
    return _public_support_report(report)


class SourcingRowRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    row: dict[str, Any]
    provider: str | None = None
    limit: int = 20
    source_mode: SourcingSourceMode = SourcingSourceMode.PROVIDER_ONLY


class SourcingIntentRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: ProductIntent
    provider: str | None = None
    limit: int = 20
    source_mode: SourcingSourceMode = SourcingSourceMode.PROVIDER_ONLY


class SourcingProjectRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[dict[str, Any]]
    provider: str | None = None
    limit: int = 20
    source_mode: SourcingSourceMode = SourcingSourceMode.PROVIDER_ONLY


class ManualTenderSourcingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_row_ids: list[StrictStr] = Field(min_length=1, max_length=500)
    source_mode: SourcingSourceMode
    provider: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    limit: StrictInt = Field(default=20, ge=1, le=100)

    @field_validator("source_row_ids")
    @classmethod
    def _validate_source_row_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("source_row_ids must not contain duplicates")
        if any(not re.fullmatch(r"[a-f0-9]{32}", item) for item in value):
            raise ValueError("source_row_ids contains an invalid identifier")
        return value

    @field_validator("provider")
    @classmethod
    def _trim_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        result = value.strip()
        if not result:
            raise ValueError("provider must not be blank")
        return result


class ManualTenderPriceExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_historical_prices: StrictBool = False
    historical_decision_confirmed: StrictBool = False
    allow_partial: StrictBool = False


def _tender_run_snapshot(tender_id: str, owner_id: str):
    path, metadata, lease = tender_repository.acquire_sourcing_workspace(tender_id, owner_id)
    return path, metadata, lease


def _tender_sourcing_integrity_fingerprint(metadata: dict[str, Any]) -> str:
    return stable_fingerprint({
        key: metadata.get(key)
        for key in (
            "rows", "mapping", "source_manifest", "logical_right_edge",
            "future_output_columns", "counts", "sheet_name", "header_row",
        )
    })


def _submit_manual_tender_sourcing(
    tender_id: str,
    request: ManualTenderSourcingRequest,
    user: CurrentUser,
) -> dict[str, Any]:
    tender_lease = history_lease = None
    repository = tender_repository
    run_store = tender_sourcing_runs
    try:
        workspace_path, metadata, tender_lease = repository.acquire_sourcing_workspace(tender_id, _tender_owner_key(user))
        selected_ids = list(request.source_row_ids)
        source_rows = metadata.get("rows")
        if not isinstance(source_rows, list):
            raise TenderWorkspaceError("Строки тендера повреждены.", 409, "TENDER_WORKSPACE_CORRUPT")
        rows_by_id = {
            str(row.get("source_row_id") or ""): row
            for row in source_rows if isinstance(row, dict)
        }
        if len(rows_by_id) != len(source_rows):
            raise TenderWorkspaceError("Строки тендера повреждены.", 409, "TENDER_WORKSPACE_CORRUPT")
        unknown = [row_id for row_id in selected_ids if row_id not in rows_by_id]
        if unknown:
            raise TenderWorkspaceError("Одна или несколько выбранных строк не найдены в тендере.", 400, "TENDER_SOURCE_ROW_UNKNOWN")
        non_items = [row_id for row_id in selected_ids if rows_by_id[row_id].get("row_type") != "item"]
        if non_items:
            raise TenderWorkspaceError("Можно выбрать только строки с позициями.", 400, "TENDER_SOURCE_ROW_NOT_ITEM")
        adapted_rows = TenderSourcingRowAdapter.selected_rows(source_rows, selected_ids)
        integrity_fingerprint = _tender_sourcing_integrity_fingerprint(metadata)

        with _sourcing_runtime_lock:
            runtime = sourcing_runtime
            service = sourcing_service if sourcing_service is not runtime.service else runtime.service
        try:
            service.provider(request.provider)
        except ValueError as exc:
            raise HTTPException(400, "Выбранный источник предложений недоступен.") from exc

        source_mode = SourcingSourceMode(request.source_mode)
        captured_history_version: str | None = None
        if source_mode != SourcingSourceMode.PROVIDER_ONLY:
            try:
                history_lease = one_c_history_activity.acquire_sourcing()
            except OneCHistoryActivityConflict as exc:
                raise _one_c_history_http_error(exc) from exc
            captured_history_version = one_c_history_repository.catalog_version()

        created_at = datetime.now(timezone.utc).isoformat()
        progress_state = {"current": 0, "total": len(adapted_rows)}

        def run(progress):
            durable = run_store.create_running(
                workspace_path, metadata,
                source_mode=source_mode.value,
                provider=request.provider,
                selected_ids=selected_ids,
                history_catalog_version=captured_history_version,
            )
            run_id = durable["run_id"]

            def tracked_progress(current: int, total: int, message: str) -> None:
                progress_state["current"] = current
                progress_state["total"] = total
                progress(current, total, message)

            try:
                current_metadata = repository.verify_sourcing_snapshot(
                    workspace_path, tender_id, _tender_owner_key(user),
                    source_sha256=str(metadata["source_sha256"]),
                    revision=int(metadata["revision"]),
                )
                if _tender_sourcing_integrity_fingerprint(current_metadata) != integrity_fingerprint:
                    raise TenderWorkspaceError(
                        "Данные тендера изменились до начала подбора.", 409, "TENDER_WORKSPACE_CHANGED",
                    )
                if source_mode == SourcingSourceMode.PROVIDER_ONLY:
                    result = service.search_project(
                        adapted_rows, provider_key=request.provider, limit=request.limit,
                        progress=tracked_progress, ai_rerank=False,
                    )
                else:
                    result = service.search_project_routed(
                        adapted_rows, source_mode=source_mode, provider_key=request.provider,
                        limit=request.limit, progress=tracked_progress, ai_rerank=False,
                    )
                    _assert_project_history_version(result, captured_history_version)

                payload = _sourcing_payload(result)
                if not isinstance(payload, dict):
                    raise TenderWorkspaceError("Результаты подбора имеют неверный формат.", 409, "TENDER_RESULT_INVALID")
                selected_sources = [rows_by_id[row_id] for row_id in selected_ids]
                projection = canonical_tender_projection(payload, selected_sources, selected_ids)
                summary = {
                    key: int(payload.get(key) or 0)
                    for key in (
                        "positions_total", "positions_processed", "positions_matched",
                        "positions_alternatives", "positions_review", "positions_without_offers",
                        "positions_history_matched", "positions_provider_matched",
                        "positions_fallback_called", "positions_history_review",
                        "positions_history_no_match", "positions_history_unavailable",
                    )
                }
                version = str(payload.get("catalog_version") or captured_history_version or "")[:120] or None
                run_store.complete(
                    workspace_path, run_id, summary=summary,
                    catalog_version=version,
                    history_catalog_version=captured_history_version,
                    rows=projection,
                )
                payload["run_id"] = run_id
                payload["tender_id"] = tender_id
                payload["run_created_at"] = created_at
                payload["run_completed_at"] = datetime.now(timezone.utc).isoformat()
                return payload
            except Exception as exc:
                code = "TENDER_RESULT_CORRELATION_FAILED" if getattr(exc, "code", "") == "TENDER_RESULT_CORRELATION_FAILED" else "SOURCING_FAILED"
                run_store.fail(
                    workspace_path, run_id, code=code,
                    progress_current=progress_state["current"], progress_total=progress_state["total"],
                )
                raise

        dedupe_key = stable_fingerprint({
            "kind": "manual_tender_sourcing",
            "tender_id": tender_id,
            "source_sha256": metadata["source_sha256"],
            "workspace_revision": int(metadata["revision"]),
            "source_row_ids": sorted(selected_ids),
            "source_mode": source_mode.value,
            "provider": request.provider,
            "limit": request.limit,
        })
        releases = [tender_lease.release]
        if history_lease is not None:
            releases.append(history_lease.release)
        try:
            job = job_service.submit(
                run, lane=SOURCING, kind="manual_tender_sourcing",
                owner_id=_job_owner_id(user), dedupe_key=dedupe_key,
                release_callbacks=releases,
            )
        except JobAdmissionError as exc:
            raise _job_admission_http_error(exc) from exc
        return job.public()
    except Exception as exc:
        if tender_lease is not None:
            tender_lease.release()
        if history_lease is not None:
            history_lease.release()
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, (TenderWorkspaceError, TenderActivityConflict)):
            raise _tender_http_error(exc) from exc
        raise


@app.post("/api/manual-tenders/{tender_id}/sourcing", status_code=202, dependencies=[Depends(require_authenticated)])
def start_manual_tender_sourcing(
    tender_id: str,
    request: ManualTenderSourcingRequest,
    user: CurrentUser = Depends(require_authenticated),
):
    return _submit_manual_tender_sourcing(tender_id, request, user)


@app.get("/api/manual-tenders/{tender_id}/runs", dependencies=[Depends(require_authenticated)])
def list_manual_tender_runs(tender_id: str, user: CurrentUser = Depends(require_authenticated)):
    lease = None
    try:
        path, metadata, lease = _tender_run_snapshot(tender_id, _tender_owner_key(user))
        return tender_sourcing_runs.list_public(path, metadata["tender_id"])
    except Exception as exc:
        if isinstance(exc, (TenderWorkspaceError, TenderActivityConflict)):
            raise _tender_http_error(exc) from exc
        raise
    finally:
        if lease is not None:
            lease.release()


@app.get("/api/manual-tenders/{tender_id}/runs/{run_id}", dependencies=[Depends(require_authenticated)])
def get_manual_tender_run(
    tender_id: str,
    run_id: str,
    user: CurrentUser = Depends(require_authenticated),
):
    lease = None
    try:
        path, metadata, lease = _tender_run_snapshot(tender_id, _tender_owner_key(user))
        return tender_sourcing_runs.get_public(path, metadata["tender_id"], run_id)
    except Exception as exc:
        if isinstance(exc, (TenderWorkspaceError, TenderActivityConflict)):
            raise _tender_http_error(exc) from exc
        raise
    finally:
        if lease is not None:
            lease.release()


def _price_export_confirmation_error(code: str, message: str, summary: dict[str, Any]) -> HTTPException:
    return HTTPException(409, detail={"code": code, "message": message, "summary": summary})


def _submit_manual_tender_price_export(
    tender_id: str,
    run_id: str,
    request: ManualTenderPriceExportRequest,
    user: CurrentUser,
) -> dict[str, Any]:
    lease = None
    owner_id = _tender_owner_key(user)
    try:
        workspace_path, workspace, lease = tender_repository.acquire_sourcing_workspace(tender_id, owner_id)
        run = tender_sourcing_runs.get_public(workspace_path, tender_id, run_id)
        # Resolve history once with consent to identify whether explicit consent
        # is needed, then resolve using only the submitted policy options.
        with_history = tender_price_resolver.resolve_run(
            workspace, run, tender_id=tender_id, run_id=run_id,
            include_historical_prices=True,
        )
        history_summary = summarize_decisions(with_history)
        if (
            history_summary["historical_count"]
            and not request.include_historical_prices
            and not request.historical_decision_confirmed
        ):
            raise _price_export_confirmation_error(
                "TENDER_EXPORT_HISTORICAL_CONFIRMATION_REQUIRED",
                "В выбранном запуске есть исторические цены 1С. Выберите, включать их или продолжить без них.",
                history_summary,
            )
        decisions = tender_price_resolver.resolve_run(
            workspace, run, tender_id=tender_id, run_id=run_id,
            include_historical_prices=request.include_historical_prices,
        )
        summary = summarize_decisions(decisions)
        if summary["priced_count"] == 0:
            raise _price_export_confirmation_error(
                "TENDER_EXPORT_NO_ELIGIBLE_PRICES",
                "В выбранном запуске нет безопасных цен для экспорта.",
                summary,
            )
        if summary["blank_count"] and not request.allow_partial:
            raise _price_export_confirmation_error(
                "TENDER_EXPORT_PARTIAL_CONFIRMATION_REQUIRED",
                "Часть выбранных строк останется без цены. Подтвердите частичный экспорт.",
                summary,
            )

        source_sha = str(workspace["source_sha256"])
        revision = int(workspace["revision"])
        owner_job_id = _job_owner_id(user)

        def run_export(progress):
            progress(0, 1, "Повторно проверяем запуск и книгу")
            current_workspace = tender_repository.verify_sourcing_snapshot(
                workspace_path, tender_id, owner_id,
                source_sha256=source_sha, revision=revision,
            )
            current_run = tender_sourcing_runs.get_public(workspace_path, tender_id, run_id)
            current_decisions = tender_price_resolver.resolve_run(
                current_workspace, current_run, tender_id=tender_id, run_id=run_id,
                include_historical_prices=request.include_historical_prices,
            )
            current_summary = summarize_decisions(current_decisions)
            if current_summary["priced_count"] == 0 or (
                current_summary["blank_count"] and not request.allow_partial
            ):
                raise TenderWorkspaceError(
                    "Условия безопасного экспорта изменились. Повторите проверку.",
                    409, "TENDER_EXPORT_POLICY_CHANGED",
                )
            progress(0, 1, "Формируем копию исходной книги")
            artifact = tender_price_exports.create_completed(
                workspace_path, current_workspace, current_run, current_decisions,
                owner_id=owner_id, allow_partial=request.allow_partial,
                include_historical_prices=request.include_historical_prices,
                builder=tender_xlsx_price_exporter,
            )
            progress(1, 1, "Экспорт готов")
            return artifact

        dedupe_key = stable_fingerprint({
            "kind": "manual_tender_price_export",
            "tender_id": tender_id,
            "run_id": run_id,
            "source_sha256": source_sha,
            "workspace_revision": revision,
            "policy_revision": "xlsx-price-export-v1",
            "allow_partial": request.allow_partial,
            "include_historical_prices": request.include_historical_prices,
        })
        try:
            job = job_service.submit(
                run_export,
                lane=DOCUMENT_PROCESSING,
                kind="manual_tender_price_export",
                owner_id=owner_job_id,
                dedupe_key=dedupe_key,
                release_callbacks=[lease.release],
            )
            lease = None  # JobService now owns release, including dedupe/admission.
            return job.public()
        except JobAdmissionError as exc:
            raise _job_admission_http_error(exc) from exc
    except Exception as exc:
        if lease is not None:
            lease.release()
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, (TenderWorkspaceError, TenderActivityConflict)):
            raise _tender_http_error(exc) from exc
        raise


@app.post(
    "/api/manual-tenders/{tender_id}/runs/{run_id}/export",
    status_code=202,
    dependencies=[Depends(require_authenticated)],
)
def start_manual_tender_price_export(
    tender_id: str,
    run_id: str,
    request: ManualTenderPriceExportRequest,
    user: CurrentUser = Depends(require_authenticated),
):
    return _submit_manual_tender_price_export(tender_id, run_id, request, user)


@app.get(
    "/api/manual-tenders/{tender_id}/exports",
    dependencies=[Depends(require_authenticated)],
)
def list_manual_tender_price_exports(
    tender_id: str,
    user: CurrentUser = Depends(require_authenticated),
):
    lease = None
    try:
        workspace_path, _workspace, lease = tender_repository.acquire_sourcing_workspace(
            tender_id, _tender_owner_key(user),
        )
        return tender_price_exports.list_public(workspace_path, tender_id, _tender_owner_key(user))
    except Exception as exc:
        if isinstance(exc, (TenderWorkspaceError, TenderActivityConflict)):
            raise _tender_http_error(exc) from exc
        raise
    finally:
        if lease is not None:
            lease.release()


@app.get(
    "/api/manual-tenders/{tender_id}/exports/{export_id}",
    dependencies=[Depends(require_authenticated)],
)
def get_manual_tender_price_export(
    tender_id: str,
    export_id: str,
    user: CurrentUser = Depends(require_authenticated),
):
    lease = None
    try:
        workspace_path, _workspace, lease = tender_repository.acquire_sourcing_workspace(
            tender_id, _tender_owner_key(user),
        )
        return tender_price_exports.get_public(
            workspace_path, tender_id, export_id, _tender_owner_key(user),
        )
    except Exception as exc:
        if isinstance(exc, (TenderWorkspaceError, TenderActivityConflict)):
            raise _tender_http_error(exc) from exc
        raise
    finally:
        if lease is not None:
            lease.release()


@app.get(
    "/api/manual-tenders/{tender_id}/exports/{export_id}/download",
    dependencies=[Depends(require_authenticated)],
)
def download_manual_tender_price_export(
    tender_id: str,
    export_id: str,
    user: CurrentUser = Depends(require_authenticated),
):
    tender_lease = export_lease = None
    try:
        workspace_path, _workspace, tender_lease = tender_repository.acquire_sourcing_workspace(
            tender_id, _tender_owner_key(user),
        )
        path, record, export_lease = tender_price_exports.acquire_download(
            workspace_path, tender_id, export_id, _tender_owner_key(user),
        )

        def release_downloads() -> None:
            if export_lease is not None:
                export_lease()
            if tender_lease is not None:
                tender_lease.release()

        return FileResponse(
            path,
            media_type=XLSX_MIME,
            filename=str(record["filename"]),
            background=BackgroundTask(release_downloads),
        )
    except Exception as exc:
        if export_lease is not None:
            export_lease()
        if tender_lease is not None:
            tender_lease.release()
        if isinstance(exc, (TenderWorkspaceError, TenderActivityConflict)):
            raise _tender_http_error(exc) from exc
        raise


def _sourcing_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    return value


def _submit_sourcing_maintenance(
    run,
    *,
    owner_id: str,
    kind: str,
    provider_key: str,
):
    try:
        return job_service.submit(
            run,
            lane=SOURCING,
            kind=kind,
            owner_id=owner_id,
            dedupe_key=stable_fingerprint({"kind": kind, "provider": provider_key}),
            fail_if_lane_occupied=True,
        )
    except JobAdmissionError as exc:
        raise _job_admission_http_error(exc) from exc


@app.get("/api/sourcing/providers", dependencies=[Depends(require_authenticated)])
def sourcing_providers():
    return sourcing_service.public_config()


@app.get("/api/sourcing/history-status", dependencies=[Depends(require_authenticated)])
def sourcing_history_status():
    status = dict(one_c_history_repository.sourcing_status())
    status["activity"] = one_c_history_activity.status()
    return status


@app.post("/api/sourcing/providers/lemana_b2b/sync")
def sync_lemana_b2b(admin: CurrentUser = Depends(require_admin)):
    service = sourcing_service
    provider = service.provider("lemana_b2b")
    sync_method = getattr(provider, "sync", None)
    if not callable(sync_method):
        raise HTTPException(404, "Синхронизация Lemana PRO B2B недоступна")

    def run(progress):
        result = sync_method()
        if hasattr(result, "__dataclass_fields__"):
            from dataclasses import asdict

            return asdict(result)
        return _sourcing_payload(result)

    return _submit_sourcing_maintenance(
        run,
        owner_id=_job_owner_id(admin),
        kind="provider_maintenance:lemana_b2b:sync",
        provider_key="lemana_b2b",
    ).public()


@app.get("/api/sourcing/providers/etm_ipro/health", dependencies=[Depends(require_admin)])
def etm_ipro_health():
    provider = sourcing_service.provider("etm_ipro")
    health_method = getattr(provider, "health", None)
    if not callable(health_method):
        raise HTTPException(404, "Проверка ЭТМ iPRO недоступна")
    return _sourcing_payload(health_method())


@app.post("/api/sourcing/providers/etm_ipro/manufacturers/sync")
def sync_etm_ipro_manufacturers(admin: CurrentUser = Depends(require_admin)):
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "sync_manufacturers", None)
    if not callable(method):
        raise HTTPException(404, "Синхронизация производителей ЭТМ iPRO недоступна")
    return _submit_sourcing_maintenance(
        lambda progress: {"count": method()},
        owner_id=_job_owner_id(admin),
        kind="provider_maintenance:etm_ipro:manufacturers_sync",
        provider_key="etm_ipro",
    ).public()


@app.get("/api/sourcing/providers/etm_ipro/manufacturers/status", dependencies=[Depends(require_authenticated)])
def etm_ipro_manufacturer_status():
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "manufacturer_status", None)
    if not callable(method):
        raise HTTPException(404, "Статус производителей ЭТМ iPRO недоступен")
    return _sourcing_payload(method())


@app.post("/api/sourcing/providers/etm_ipro/catalog/sync")
def start_etm_ipro_catalog_sync(admin: CurrentUser = Depends(require_admin)):
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "start_catalog_sync", None)
    if not callable(method):
        raise HTTPException(404, "Синхронизация каталога ЭТМ iPRO недоступна")
    return _submit_sourcing_maintenance(
        lambda progress: _sourcing_payload(method()),
        owner_id=_job_owner_id(admin),
        kind="provider_maintenance:etm_ipro:catalog_sync",
        provider_key="etm_ipro",
    ).public()


@app.get("/api/sourcing/providers/etm_ipro/catalog/status", dependencies=[Depends(require_admin)])
def etm_ipro_catalog_status():
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "catalog_sync_status", None)
    if not callable(method):
        raise HTTPException(404, "Статус каталога ЭТМ iPRO недоступен")
    payload = _sourcing_payload(method())
    if isinstance(payload, dict):
        payload["snapshot_ready"] = bool(payload.get("url"))
        payload.pop("url", None)
    return payload


@app.post("/api/sourcing/providers/etm_ipro/catalog/import")
def import_etm_ipro_catalog(admin: CurrentUser = Depends(require_admin)):
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "import_completed_catalog", None)
    if not callable(method):
        raise HTTPException(404, "Импорт каталога ЭТМ iPRO недоступен")
    return _submit_sourcing_maintenance(
        lambda progress: _sourcing_payload(method(progress)),
        owner_id=_job_owner_id(admin),
        kind="provider_maintenance:etm_ipro:catalog_import",
        provider_key="etm_ipro",
    ).public()


@app.post("/api/sourcing/providers/etm_ipro/catalog/reindex")
def reindex_etm_ipro_catalog(admin: CurrentUser = Depends(require_admin)):
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "rebuild_search_index", None)
    if not callable(method):
        raise HTTPException(404, "Переиндексация каталога ЭТМ iPRO недоступна")
    return _submit_sourcing_maintenance(
        lambda progress: _sourcing_payload(method(progress)),
        owner_id=_job_owner_id(admin),
        kind="provider_maintenance:etm_ipro:catalog_reindex",
        provider_key="etm_ipro",
    ).public()


@app.get("/api/sourcing/catalog/stats", dependencies=[Depends(require_authenticated)])
def sourcing_catalog_stats():
    return sourcing_repository.stats()


@app.get("/api/admin/openapi.json", dependencies=[Depends(require_admin)])
def admin_openapi():
    return get_openapi(title=APP_NAME, version=APP_VERSION, routes=app.routes)


@app.get("/api/admin/docs", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_docs():
    return get_swagger_ui_html(
        openapi_url="/api/admin/openapi.json",
        title=f"{APP_NAME} API docs",
    )


@app.get("/api/admin/redoc", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_redoc():
    return get_redoc_html(
        openapi_url="/api/admin/openapi.json",
        title=f"{APP_NAME} API reference",
    )


@app.get("/demo-catalog", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
def demo_catalog(request: Request):
    offers = sourcing_repository.list_by_source(DEMO_CATALOG_SOURCE, limit=500)
    return templates.TemplateResponse(
        request=request,
        name="demo_catalog.html",
        context={"offers": offers, "notice": DEMO_CATALOG_NOTICE},
    )


@app.get("/demo-catalog/products/{offer_id}", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
def demo_catalog_product(request: Request, offer_id: str):
    offer = sourcing_repository.get_by_id(offer_id)
    if offer is None or offer.data_provenance.get("source") != DEMO_CATALOG_SOURCE:
        raise HTTPException(404, "Демонстрационное предложение не найдено")
    return templates.TemplateResponse(
        request=request,
        name="demo_product.html",
        context={"offer": offer, "notice": DEMO_CATALOG_NOTICE},
    )


@app.post("/api/sourcing/understand", dependencies=[Depends(require_authenticated)])
def sourcing_understand(request: SourcingRowRequest):
    understanding = sourcing_service.understand_row_result(request.row)
    return {
        "intent": _sourcing_payload(understanding.resolved_intent),
        "understanding": _sourcing_payload(understanding),
        "warnings": list(understanding.warnings),
        "notices": [_sourcing_payload(notice) for notice in understanding.notices],
    }


@app.post("/api/sourcing/search", dependencies=[Depends(require_authenticated)])
def sourcing_search(request: SourcingRowRequest):
    lease = None
    try:
        if request.source_mode != SourcingSourceMode.PROVIDER_ONLY:
            lease = one_c_history_activity.acquire_sourcing()
        result = sourcing_service.search_row_routed(
            request.row,
            source_mode=request.source_mode,
            provider_key=request.provider,
            limit=max(1, min(request.limit, 100)),
        )
        return _sourcing_payload(result)
    except OneCHistoryActivityConflict as exc:
        raise _one_c_history_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if lease is not None:
            lease.release()


@app.post("/api/sourcing/search-intent", dependencies=[Depends(require_authenticated)])
def sourcing_search_intent(request: SourcingIntentRequest):
    if request.source_mode != SourcingSourceMode.PROVIDER_ONLY:
        raise HTTPException(
            400,
            "Поиск по истории требует исходную строку; используйте /api/sourcing/search.",
        )
    try:
        result = sourcing_service.search_intent(
            request.intent,
            provider_key=request.provider,
            limit=max(1, min(request.limit, 100)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _sourcing_payload(result)


def _submit_sourcing_project_job(
    rows: list[dict[str, Any]],
    *,
    provider_key: str | None,
    limit: int,
    source_mode: SourcingSourceMode = SourcingSourceMode.PROVIDER_ONLY,
    document_id: str | None = None,
    owner_id: str = "__internal__",
):
    source_mode = SourcingSourceMode(source_mode)
    # Keep the accepted job on the runtime that existed at admission. A later
    # settings refresh may replace the module globals while this job is queued.
    with _sourcing_runtime_lock:
        runtime = sourcing_runtime
        service = (
            sourcing_service
            if sourcing_service is not runtime.service
            else runtime.service
        )
    provider = (
        service.provider(provider_key)
        if source_mode == SourcingSourceMode.PROVIDER_ONLY
        else None
    )
    history = None
    run_id = None
    created_at = datetime.now(timezone.utc).isoformat()
    document_revision = 0
    if document_id is not None:
        workspace = workspace_service.get(document_id)
        document_revision = _result_revision(
            workspace_service.read_json(workspace.result_path, default={})
        )
        history = SourcingRunHistory(workspace.sourcing_runs_dir)
        run_id = history.new_run_id()
    eligible_total = sum(
        1 for row in rows
        if row.get("selected", True) is not False
        and row.get("row_type") in {"item", "component", "item_candidate"}
    )
    progress_state = {"current": 0, "total": eligible_total}
    history_lease = None

    def run(progress):
        telemetry: list[dict[str, Any]] = []
        catalog_version = "unknown"
        captured_history_version: str | None = None

        def tracked_progress(current: int, total: int, message: str) -> None:
            progress_state["current"] = current
            progress_state["total"] = total
            progress(current, total, message)

        try:
            if source_mode == SourcingSourceMode.PROVIDER_ONLY:
                result = service.search_project(
                    rows,
                    provider_key=provider_key,
                    limit=limit,
                    progress=tracked_progress,
                    telemetry=telemetry.append,
                    ai_rerank=False,
                )
                catalog_version = result.catalog_version or "unknown"
            else:
                captured_history_version = one_c_history_repository.catalog_version()
                catalog_version = captured_history_version or "unknown"
                result = service.search_project_routed(
                    rows,
                    source_mode=source_mode,
                    provider_key=provider_key,
                    limit=limit,
                    progress=tracked_progress,
                    telemetry=telemetry.append,
                    ai_rerank=False,
                )
                _assert_project_history_version(result, captured_history_version)
                catalog_version = result.catalog_version or catalog_version
            payload = _sourcing_payload(result)
            if history is not None and run_id is not None:
                completed_at = datetime.now(timezone.utc).isoformat()
                history.write_completed(
                    run_id=run_id,
                    document_id=document_id or "",
                    created_at=created_at,
                    completed_at=completed_at,
                    provider_key=payload.get("provider_key") or (provider.key if provider is not None else ""),
                    provider_label=payload.get("provider_label") or (provider.label if provider is not None else ""),
                    catalog_version=payload.get("catalog_version") or catalog_version,
                    source_mode=source_mode.value,
                    result=result,
                    row_telemetry=telemetry,
                )
                payload["run_id"] = run_id
                payload["run_created_at"] = created_at
                payload["run_completed_at"] = completed_at
            return payload
        except Exception as exc:
            if history is not None and run_id is not None:
                history.write_failed(
                    run_id=run_id,
                    document_id=document_id or "",
                    created_at=created_at,
                    provider_key=provider.key if provider is not None else "",
                    provider_label=provider.label if provider is not None else "",
                    catalog_version=catalog_version,
                    positions_total=eligible_total,
                    progress_current=progress_state["current"],
                    progress_total=progress_state["total"],
                    exc=exc,
                    source_mode=source_mode.value,
                )
            raise
    if source_mode != SourcingSourceMode.PROVIDER_ONLY:
        try:
            history_lease = one_c_history_activity.acquire_sourcing()
        except OneCHistoryActivityConflict as exc:
            raise _one_c_history_http_error(exc) from exc
    try:
        dedupe_key = stable_fingerprint({
            "kind": "project_sourcing",
            "document_id": document_id,
            "document_revision": document_revision,
            "source_mode": source_mode.value,
            "provider": provider_key,
            "limit": max(1, min(limit, 100)),
            "rows": rows,
        })
        releases = [history_lease.release] if history_lease is not None else []
        if document_id is not None:
            job = _submit_document_job(
                document_id,
                run,
                lane=SOURCING,
                owner_id=owner_id,
                kind="project_sourcing",
                dedupe_key=dedupe_key,
                release_callbacks=releases,
            )
        else:
            try:
                job = job_service.submit(
                    run,
                    lane=SOURCING,
                    kind="project_sourcing",
                    owner_id=owner_id,
                    dedupe_key=dedupe_key,
                    release_callbacks=releases,
                )
            except JobAdmissionError as exc:
                raise _job_admission_http_error(exc) from exc
        return job.public()
    except Exception:
        if history_lease is not None:
            history_lease.release()
        raise


def _assert_project_history_version(result: Any, captured_version: str | None) -> None:
    payload = _sourcing_payload(result)
    if not isinstance(payload, dict):
        raise RuntimeError("Project sourcing result has an invalid shape")
    rows = payload.get("results", [])
    if not isinstance(rows, list):
        raise RuntimeError("Project sourcing rows have an invalid shape")
    versions: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        route = row.get("route")
        if isinstance(route, dict):
            version = route.get("history_catalog_version")
            if isinstance(version, str) and version:
                versions.add(version)
    if len(versions) > 1 or (versions and versions != {captured_version}):
        raise RuntimeError("A project sourcing run returned mixed 1C history snapshots")


@app.post("/api/sourcing/search-all")
def sourcing_search_all(
    request: SourcingProjectRequest,
    user: CurrentUser = Depends(require_authenticated),
):
    rows = [dict(row) for row in request.rows]
    provider_key = request.provider
    limit = max(1, min(request.limit, 100))
    return _submit_sourcing_project_job(
        rows,
        provider_key=provider_key,
        limit=limit,
        source_mode=request.source_mode,
        owner_id=_job_owner_id(user),
    )


def _ensure_document(document_id: str) -> None:
    try:
        workspace_service.get(document_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page: int
    physical_refs: list[dict[str, Any]]
    decision: Literal[
        FIELD_DECISION,
        RELATION_DECISION,
        REJECT_DECISION,
        CONFIRM_FIELD_VALUE_DECISION,
        CONFIRM_FIELD_ABSENT_DECISION,
    ]
    field: str | None = None
    relation: str | None = None
    candidate_value: str | None = None
    confirmed_value: str | None = None
    target: dict[str, Any] = Field(default_factory=dict)


@app.get(
    "/api/documents/{document_id}/review",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def get_review_decisions(document_id: str):
    try:
        workspace = workspace_service.get(document_id)
        with document_mutation_locks.for_document(document_id):
            decisions = ReviewDecisionStore(workspace.review_decisions_path).load()
            fingerprint = _source_fingerprint(workspace)
        return {
            "document_fingerprint": fingerprint,
            "decisions": [item.model_dump(mode="json") for item in decisions if item.document_fingerprint == fingerprint],
        }
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc
    except ReviewDecisionLedgerCorrupt as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post(
    "/api/documents/{document_id}/review/decision",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def save_review_decision(document_id: str, request: ReviewDecisionRequest):
    if not request.physical_refs:
        raise HTTPException(400, "Для строки отсутствует связанное OCR-доказательство")
    try:
        workspace = workspace_service.get(document_id)
        with document_mutation_locks.for_document(document_id):
            result = workspace_service.read_json(workspace.result_path, default={})
            if not result:
                raise HTTPException(404, "Результат распознавания отсутствует")
            result, _recovered = _reconcile_review_projection_locked(workspace, result)
            current_revision = _result_revision(result)
            fingerprint = _source_fingerprint(workspace)
            decision = human_review_service.create_decision(
                result,
                document_fingerprint=fingerprint,
                page=request.page,
                physical_refs=request.physical_refs,
                decision=request.decision,
                field=request.field,
                relation=request.relation,
                candidate_value=request.candidate_value,
                confirmed_value=request.confirmed_value,
                target=request.target,
            )
            store = ReviewDecisionStore(workspace.review_decisions_path)
            updated, changed_rows, affected_pages, applied = (
                human_review_service.apply_decision_incremental(result, decision)
            )
            if not applied:
                raise ValueError("Решение больше не соответствует текущему OCR-доказательству")
            _decisions, ledger_revision, ledger_changed = store.upsert_snapshot(decision)
            canonical_changed = bool(changed_rows) or ledger_changed
            if canonical_changed:
                updated["review_ledger_revision"] = ledger_revision
                updated["review_projection_version"] = REVIEW_PROJECTION_VERSION
                updated["revision"] = current_revision + 1
                workspace_service.write_json(workspace.result_path, updated)
            else:
                updated = result
            page_statuses = updated.get("page_statuses") or {}
            changed_page_statuses = {
                key: value
                for key, value in page_statuses.items()
                if int((value or {}).get("page") or key) in affected_pages
            }
            return {
                "saved": True,
                "decision": decision.model_dump(mode="json"),
                "result_patch": {
                    "rows": changed_rows,
                    "page_statuses": changed_page_statuses,
                    "summary": updated.get("summary") or {},
                    "revision": _result_revision(updated),
                    "review_ledger_revision": int(updated.get("review_ledger_revision", ledger_revision)),
                },
            }
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc
    except ReviewDecisionLedgerCorrupt as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post(
    "/api/documents/{document_id}/sourcing/search",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def document_sourcing_search(document_id: str, request: SourcingRowRequest):
    _ensure_document(document_id)
    return sourcing_search(request)


@app.post(
    "/api/documents/{document_id}/sourcing/search-all",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def document_sourcing_search_all(
    document_id: str,
    request: SourcingProjectRequest,
    user: CurrentUser = Depends(require_authenticated),
):
    _ensure_document(document_id)
    # The client sends the current selected/exportable rows, including any
    # reviewed edits.  The document id is used only to scope the operation.
    rows = [dict(row) for row in request.rows]
    return _submit_sourcing_project_job(
        rows,
        provider_key=request.provider,
        limit=max(1, min(request.limit, 100)),
        source_mode=request.source_mode,
        document_id=document_id,
        owner_id=_job_owner_id(user),
    )


@app.get(
    "/api/documents/{document_id}/sourcing/runs",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def list_sourcing_runs(document_id: str):
    _ensure_document(document_id)
    workspace = workspace_service.get(document_id)
    return {"runs": SourcingRunHistory(workspace.sourcing_runs_dir).list_public()}


@app.get(
    "/api/documents/{document_id}/sourcing/runs/{run_id}",
    dependencies=[Depends(require_authenticated), Depends(require_document_activity)],
)
def get_sourcing_run(document_id: str, run_id: str):
    _ensure_document(document_id)
    workspace = workspace_service.get(document_id)
    record = SourcingRunHistory(workspace.sourcing_runs_dir).get(run_id)
    if record is None or record.get("document_id") != document_id:
        raise HTTPException(404, "Запуск подбора не найден")
    return record


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
