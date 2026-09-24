from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
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
    DEFAULT_SESSION_TTL_SECONDS,
    DuplicateUsername,
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
    require_admin,
    require_authenticated,
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
from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.runtime import create_sourcing_runtime
from averon_import.services.sourcing.run_history import SourcingRunHistory
from averon_import.services.workspace import WorkspaceService

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
auth_repository = AccountRepository(DATA_DIR / "auth")
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
sourcing_runtime = create_sourcing_runtime(DATA_DIR, app_settings_service, secret_store)
# Compatibility aliases for endpoints and integrations that historically used
# these module-level objects directly.
sourcing_repository = sourcing_runtime.repository
sourcing_provider = sourcing_runtime.providers["local_catalog"]
demo_store_provider = sourcing_runtime.providers["demo_store_http"]
sourcing_service = sourcing_runtime.service
human_review_service = HumanReviewService()


def _rebuild_sourcing_runtime() -> None:
    """Rebind sourcing dependencies after settings or secret changes."""

    global demo_store_provider, sourcing_provider, sourcing_repository, sourcing_runtime, sourcing_service
    sourcing_runtime = create_sourcing_runtime(DATA_DIR, app_settings_service, secret_store)
    sourcing_repository = sourcing_runtime.repository
    sourcing_provider = sourcing_runtime.providers["local_catalog"]
    demo_store_provider = sourcing_runtime.providers["demo_store_http"]
    sourcing_service = sourcing_runtime.service

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
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


@app.get("/api/me")
def me(user: CurrentUser = Depends(require_authenticated)):
    return user.public()


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
        login_rate_limiter.failed(bucket)
        return JSONResponse(generic_error, status_code=401)

    login_rate_limiter.succeeded(bucket)
    try:
        session = auth_repository.create_session(user["user_id"])
    except AccountDisabled:
        login_rate_limiter.failed(bucket)
        return JSONResponse(generic_error, status_code=401)
    except Exception as exc:
        logger.exception("Could not create account session")
        raise HTTPException(status_code=503, detail="Сервис аутентификации временно недоступен") from exc
    current_user = CurrentUser(username=user["username"], role=Role(user["role"]), user_id=user["user_id"])
    response = JSONResponse({"user": current_user.public()})
    _set_auth_cookies(response, session)
    return response


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request, _user: CurrentUser = Depends(require_authenticated)):
    if auth_config().mode == "session":
        auth_repository.delete_session(request.cookies.get("averon_session", ""))
    response = Response(status_code=204)
    _clear_auth_cookies(response)
    return response


@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
def list_account_users(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    users = auth_repository.list_users(limit=limit, offset=offset)
    return {"users": users, "limit": limit, "offset": offset}


@app.post("/api/admin/users", status_code=201)
async def create_account_user(request: Request, _admin: CurrentUser = Depends(require_admin)):
    payload = await _auth_json_object(request, allowed={"username", "password"}, required={"username", "password"})
    username = payload["username"]
    password = _password_value(payload)
    if not isinstance(username, str):
        raise _auth_json_error(422, "INVALID_USERNAME")
    try:
        user = auth_repository.create_user(username, password, role="user")
    except DuplicateUsername:
        raise _auth_json_error(409, "USERNAME_EXISTS") from None
    except ValueError:
        raise _auth_json_error(422, "INVALID_USERNAME") from None
    return {"user": user}


def _is_current_account(target_user_id: str, current_user: CurrentUser) -> bool:
    if current_user.user_id is not None:
        return current_user.user_id == target_user_id
    target = auth_repository.get_user(target_user_id)
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
    if not payload["enabled"] and _is_current_account(user_id, admin):
        raise _auth_json_error(409, "SELF_PROTECTION")
    try:
        user = auth_repository.update_enabled(user_id, enabled=payload["enabled"], version=version)
    except UserNotFound:
        raise _auth_json_error(404, "USER_NOT_FOUND") from None
    except StaleUserVersion:
        raise _auth_json_error(409, "STALE_USER_VERSION") from None
    return {"user": user}


@app.post("/api/admin/users/{user_id}/reset-password")
async def reset_account_password(user_id: str, request: Request, admin: CurrentUser = Depends(require_admin)):
    payload = await _auth_json_object(request, allowed={"password", "version"}, required={"password", "version"})
    password = _password_value(payload)
    version = _version_value(payload)
    if _is_current_account(user_id, admin):
        raise _auth_json_error(409, "SELF_PROTECTION")
    try:
        user = auth_repository.reset_password(user_id, password, version=version)
    except UserNotFound:
        raise _auth_json_error(404, "USER_NOT_FOUND") from None
    except StaleUserVersion:
        raise _auth_json_error(409, "STALE_USER_VERSION") from None
    return {"user": user}


@app.delete("/api/admin/users/{user_id}")
async def delete_account_user(user_id: str, request: Request, admin: CurrentUser = Depends(require_admin)):
    payload = await _auth_json_object(request, allowed={"version"}, required={"version"})
    version = _version_value(payload)
    if _is_current_account(user_id, admin):
        raise _auth_json_error(409, "SELF_PROTECTION")
    try:
        auth_repository.delete_user(user_id, version=version)
    except UserNotFound:
        raise _auth_json_error(404, "USER_NOT_FOUND") from None
    except StaleUserVersion:
        raise _auth_json_error(409, "STALE_USER_VERSION") from None
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


@app.get("/api/documents", dependencies=[Depends(require_authenticated)])
def list_documents(limit: int = 50):
    return {"documents": workspace_service.list_recent(limit)}


@app.get("/api/documents/{document_id}", dependencies=[Depends(require_authenticated)])
def get_document(document_id: str):
    try:
        workspace = workspace_service.get(document_id)
        metadata = workspace_service.read_json(workspace.metadata_path)
        result = workspace_service.read_json(workspace.result_path)
        return {
            **metadata,
            "has_result": bool(result),
            "has_review_decisions": workspace.review_decisions_path.is_file(),
        }
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc


@app.get("/api/documents/{document_id}/page/{page_number}", dependencies=[Depends(require_authenticated)])
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


@app.post("/api/documents/{document_id}/suggest-pages", dependencies=[Depends(require_authenticated)])
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


@app.post("/api/documents/{document_id}/recognize", dependencies=[Depends(require_authenticated)])
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


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_authenticated)])
def get_job(job_id: str):
    try:
        return job_service.get(job_id).public()
    except KeyError as exc:
        raise HTTPException(404, "Задание не найдено") from exc


@app.get("/api/documents/{document_id}/results", dependencies=[Depends(require_authenticated)])
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


@app.put("/api/documents/{document_id}/results", dependencies=[Depends(require_authenticated)])
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


@app.post("/api/documents/{document_id}/export", dependencies=[Depends(require_authenticated)])
def export(
    document_id: str,
    request: ExportRequest,
    user: CurrentUser = Depends(require_authenticated),
):
    try:
        workspace = workspace_service.get(document_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Документ не найден") from exc

    filename = (
        review_export_filename(request.filename)
        if request.review_export
        else safe_filename(request.filename)
    )
    output: Path | None = None
    temporary_output: Path | None = None
    stored_result: dict[str, Any] = {}
    try:
        try:
            stored_result = workspace_service.read_json(workspace.result_path, default={})
        except Exception as exc:
            raise RuntimeError("Не удалось прочитать состояние экспорта") from exc
        stored_result = stored_result if isinstance(stored_result, dict) else {}
        output = workspace.exports_dir / filename
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".averon-export-",
            suffix=".tmp",
            dir=workspace.exports_dir,
        )
        os.close(file_descriptor)
        temporary_output = Path(temporary_name)
        export_service.export(
            rows=request.rows,
            columns=request.columns,
            output_path=temporary_output,
            sheet_name=request.sheet_name,
            include_headers=request.include_headers,
            only_exportable=request.only_exportable,
            page_statuses=stored_result.get("page_statuses") or {},
            review_export=request.review_export,
            enforce_safety=not request.review_export,
        )
        os.replace(temporary_output, output)
        temporary_output = None
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
    return FileResponse(
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
        "error_code": report["error_code"],
        "public_message": report["public_message"],
        "row_count": report["row_count"],
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
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    return value


@app.get("/api/sourcing/providers", dependencies=[Depends(require_authenticated)])
def sourcing_providers():
    return sourcing_service.public_config()


@app.post("/api/sourcing/providers/lemana_b2b/sync", dependencies=[Depends(require_admin)])
def sync_lemana_b2b():
    provider = sourcing_service.provider("lemana_b2b")
    sync_method = getattr(provider, "sync", None)
    if not callable(sync_method):
        raise HTTPException(404, "Синхронизация Lemana PRO B2B недоступна")

    def run(progress):
        result = sync_method()
        if hasattr(result, "__dataclass_fields__"):
            from dataclasses import asdict

            return asdict(result)
        return _sourcing_payload(result)

    return job_service.submit(run).public()


@app.get("/api/sourcing/providers/etm_ipro/health", dependencies=[Depends(require_admin)])
def etm_ipro_health():
    provider = sourcing_service.provider("etm_ipro")
    health_method = getattr(provider, "health", None)
    if not callable(health_method):
        raise HTTPException(404, "Проверка ЭТМ iPRO недоступна")
    return _sourcing_payload(health_method())


@app.post("/api/sourcing/providers/etm_ipro/manufacturers/sync", dependencies=[Depends(require_admin)])
def sync_etm_ipro_manufacturers():
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "sync_manufacturers", None)
    if not callable(method):
        raise HTTPException(404, "Синхронизация производителей ЭТМ iPRO недоступна")
    return job_service.submit(lambda progress: {"count": method()}).public()


@app.get("/api/sourcing/providers/etm_ipro/manufacturers/status", dependencies=[Depends(require_authenticated)])
def etm_ipro_manufacturer_status():
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "manufacturer_status", None)
    if not callable(method):
        raise HTTPException(404, "Статус производителей ЭТМ iPRO недоступен")
    return _sourcing_payload(method())


@app.post("/api/sourcing/providers/etm_ipro/catalog/sync", dependencies=[Depends(require_admin)])
def start_etm_ipro_catalog_sync():
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "start_catalog_sync", None)
    if not callable(method):
        raise HTTPException(404, "Синхронизация каталога ЭТМ iPRO недоступна")
    return job_service.submit(lambda progress: _sourcing_payload(method())).public()


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


@app.post("/api/sourcing/providers/etm_ipro/catalog/import", dependencies=[Depends(require_admin)])
def import_etm_ipro_catalog():
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "import_completed_catalog", None)
    if not callable(method):
        raise HTTPException(404, "Импорт каталога ЭТМ iPRO недоступен")
    return job_service.submit(
        lambda progress: _sourcing_payload(method(progress))
    ).public()


@app.post("/api/sourcing/providers/etm_ipro/catalog/reindex", dependencies=[Depends(require_admin)])
def reindex_etm_ipro_catalog():
    provider = sourcing_service.provider("etm_ipro")
    method = getattr(provider, "rebuild_search_index", None)
    if not callable(method):
        raise HTTPException(404, "Переиндексация каталога ЭТМ iPRO недоступна")
    return job_service.submit(
        lambda progress: _sourcing_payload(method(progress))
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
    try:
        result = sourcing_service.search_row(
            request.row,
            provider_key=request.provider,
            limit=max(1, min(request.limit, 100)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _sourcing_payload(result)


@app.post("/api/sourcing/search-intent", dependencies=[Depends(require_authenticated)])
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


def _submit_sourcing_project_job(
    rows: list[dict[str, Any]],
    *,
    provider_key: str | None,
    limit: int,
    document_id: str | None = None,
):
    provider = sourcing_service.provider(provider_key)
    history = None
    run_id = None
    created_at = datetime.now(timezone.utc).isoformat()
    if document_id is not None:
        workspace = workspace_service.get(document_id)
        history = SourcingRunHistory(workspace.sourcing_runs_dir)
        run_id = history.new_run_id()
    eligible_total = sum(
        1 for row in rows
        if row.get("selected", True) is not False
        and row.get("row_type") in {"item", "component", "item_candidate"}
    )
    progress_state = {"current": 0, "total": eligible_total}

    def run(progress):
        telemetry: list[dict[str, Any]] = []
        catalog_version = "unknown"

        def tracked_progress(current: int, total: int, message: str) -> None:
            progress_state["current"] = current
            progress_state["total"] = total
            progress(current, total, message)

        try:
            catalog_version = sourcing_service._project_catalog_version(provider)
            result = sourcing_service.search_project(
                rows,
                provider_key=provider_key,
                limit=limit,
                progress=tracked_progress,
                telemetry=telemetry.append,
                catalog_version=catalog_version,
                ai_rerank=False,
            )
            payload = _sourcing_payload(result)
            if history is not None and run_id is not None:
                completed_at = datetime.now(timezone.utc).isoformat()
                history.write_completed(
                    run_id=run_id,
                    document_id=document_id or "",
                    created_at=created_at,
                    completed_at=completed_at,
                    provider_key=payload.get("provider_key") or provider.key,
                    provider_label=payload.get("provider_label") or provider.label,
                    catalog_version=payload.get("catalog_version") or catalog_version,
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
                    provider_key=provider.key,
                    provider_label=provider.label,
                    catalog_version=catalog_version,
                    positions_total=eligible_total,
                    progress_current=progress_state["current"],
                    progress_total=progress_state["total"],
                    exc=exc,
                )
            raise

    return job_service.submit(run).public()


@app.post("/api/sourcing/search-all", dependencies=[Depends(require_authenticated)])
def sourcing_search_all(request: SourcingProjectRequest):
    rows = [dict(row) for row in request.rows]
    provider_key = request.provider
    limit = max(1, min(request.limit, 100))
    return _submit_sourcing_project_job(rows, provider_key=provider_key, limit=limit)


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


@app.get("/api/documents/{document_id}/review", dependencies=[Depends(require_authenticated)])
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


@app.post("/api/documents/{document_id}/review/decision", dependencies=[Depends(require_authenticated)])
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


@app.post("/api/documents/{document_id}/sourcing/search", dependencies=[Depends(require_authenticated)])
def document_sourcing_search(document_id: str, request: SourcingRowRequest):
    _ensure_document(document_id)
    return sourcing_search(request)


@app.post("/api/documents/{document_id}/sourcing/search-all", dependencies=[Depends(require_authenticated)])
def document_sourcing_search_all(document_id: str, request: SourcingProjectRequest):
    _ensure_document(document_id)
    # The client sends the current selected/exportable rows, including any
    # reviewed edits.  The document id is used only to scope the operation.
    rows = [dict(row) for row in request.rows]
    return _submit_sourcing_project_job(
        rows,
        provider_key=request.provider,
        limit=max(1, min(request.limit, 100)),
        document_id=document_id,
    )


@app.get("/api/documents/{document_id}/sourcing/runs", dependencies=[Depends(require_authenticated)])
def list_sourcing_runs(document_id: str):
    _ensure_document(document_id)
    workspace = workspace_service.get(document_id)
    return {"runs": SourcingRunHistory(workspace.sourcing_runs_dir).list_public()}


@app.get("/api/documents/{document_id}/sourcing/runs/{run_id}", dependencies=[Depends(require_authenticated)])
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
