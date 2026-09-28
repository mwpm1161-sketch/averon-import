from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from fastapi import UploadFile

from averon_import.services.one_c_history.models import (
    FIELD_NAMES,
    ImportMappingRequest,
    ImportProfile,
    PreviewMappingRequest,
)
from averon_import.services.one_c_history.repository import OneCHistoryRepository
from averon_import.services.one_c_history.xlsx_import import (
    MAX_PREVIEW_ROWS,
    MAX_UPLOAD_BYTES,
    PARSER_VERSION,
    OneCImportError,
    ParsedWorkbook,
    detect_workbook,
    header_signature,
    inspect_sheet,
    parse_workbook,
)


PREVIEW_TTL_SECONDS = 20 * 60
MAX_PENDING_PREVIEWS = 3
UPLOAD_CHUNK_BYTES = 1024 * 1024


class _PendingPreview:
    def __init__(self, *, preview_id: str, path: Path, filename: str, file_sha256: str, file_size_bytes: int, detected: dict, profile_id: str | None, expires_at: float, mapping_required: bool):
        self.preview_id = preview_id
        self.path = path
        self.filename = filename
        self.file_sha256 = file_sha256
        self.file_size_bytes = file_size_bytes
        self.detected = detected
        self.profile_id = profile_id
        self.expires_at = expires_at
        self.created_at = time.monotonic()
        self.mapping_required = mapping_required
        self.analysis_key: str | None = None
        self.analysis_lock = threading.Lock()


class OneCHistoryImportService:
    """Coordinates bounded previews and atomic activation of 1C history snapshots."""

    def __init__(self, repository: OneCHistoryRepository):
        self.repository = repository
        self.temp_root = repository.root / ".pending"
        self.temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.temp_root.chmod(0o700)
        except OSError:
            pass
        self._pending: dict[str, _PendingPreview] = {}
        self._lock = threading.RLock()
        self._import_lock = threading.Lock()
        self._remove_stale_temp_files(include_normalized=True)

    def _remove_stale_temp_files(self, *, include_normalized: bool = False) -> None:
        now = time.time()
        active = {record.path.resolve() for record in self._pending.values()}
        paths = list(self.temp_root.glob("pending-*.xlsx"))
        if include_normalized:
            paths.extend(self.temp_root.glob("averon-onec-normalized-*.xlsx"))
        for path in paths:
            try:
                if path.resolve() not in active and now - path.stat().st_mtime > PREVIEW_TTL_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _prune_previews(self) -> None:
        now = time.monotonic()
        expired = [key for key, value in self._pending.items() if value.expires_at <= now]
        for key in expired:
            record = self._pending.get(key)
            if record is None or not record.analysis_lock.acquire(blocking=False):
                continue
            try:
                self._pending.pop(key, None)
                record.path.unlink(missing_ok=True)
            finally:
                record.analysis_lock.release()
        if len(self._pending) >= MAX_PENDING_PREVIEWS:
            oldest = sorted(self._pending.items(), key=lambda item: item[1].created_at)
            excess = len(self._pending) - MAX_PENDING_PREVIEWS + 1
            removed = 0
            for key, record in oldest:
                if removed >= excess:
                    break
                if not record.analysis_lock.acquire(blocking=False):
                    continue
                try:
                    self._pending.pop(key, None)
                    record.path.unlink(missing_ok=True)
                    removed += 1
                finally:
                    record.analysis_lock.release()
        self._remove_stale_temp_files()

    @staticmethod
    def _safe_filename(filename: str | None) -> str:
        name = Path(str(filename or "report.xlsx").replace("\\", "/")).name
        cleaned = "".join(char for char in name if char.isprintable() and char not in "<>:\"|?*")
        if not cleaned.lower().endswith(".xlsx"):
            raise OneCImportError("Загрузите файл в формате .xlsx.")
        if len(cleaned) > 255:
            raise OneCImportError("Имя файла слишком длинное.")
        return cleaned or "report.xlsx"

    async def create_preview(self, upload: UploadFile, *, profile_id: str | None = None) -> dict:
        path: Path | None = None
        retained = False
        preview_id: str | None = None
        try:
            filename = self._safe_filename(upload.filename)
            with self._lock:
                self._prune_previews()
            fd, temp_name = tempfile.mkstemp(prefix="pending-", suffix=".xlsx", dir=self.temp_root)
            path = Path(temp_name)
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "wb") as destination:
                while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise OneCImportError("Размер XLSX превышает допустимый предел 25 МБ.")
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if size == 0:
                raise OneCImportError("Файл XLSX пуст.")
            detected = await asyncio.to_thread(detect_workbook, path)
            file_sha256 = digest.hexdigest()
            selected_profile: ImportProfile | None = None
            incompatible_profile = False
            if profile_id:
                selected_profile = self.repository.profile(profile_id)
                if selected_profile is None:
                    raise OneCImportError("Профиль импорта не найден.")
                incompatible_profile = not (
                    selected_profile.sheet_name == detected["sheet_name"]
                    and selected_profile.header_signature == detected["header_signature"]
                    and selected_profile.parser_version == PARSER_VERSION
                )
                if incompatible_profile:
                    selected_profile = None
            else:
                selected_profile = self.repository.compatible_profile(
                    sheet_name=detected["sheet_name"], signature=detected["header_signature"]
                )
                incompatible_profile = bool(self.repository.profiles_for_sheet(detected["sheet_name"]) and selected_profile is None)

            if selected_profile:
                detected["layout_type"] = selected_profile.layout_type
                detected["header_row"] = selected_profile.header_row
                detected["field_mapping"] = {key: selected_profile.field_mapping.get(key) for key in FIELD_NAMES}
                detected["item_name_parse_strategy"] = selected_profile.item_name_parse_strategy
            preview_mapping = detected["field_mapping"]
            mapping_required = incompatible_profile or preview_mapping.get("item_name") is None or not any(
                preview_mapping.get(name) is not None
                for name in ("quantity", "reported_unit_price_gross", "amount_gross")
            )
            parsed = None
            if not mapping_required:
                parsed = await asyncio.to_thread(
                    parse_workbook,
                    path,
                    filename=filename,
                    file_sha256=file_sha256,
                    sheet_name=detected["sheet_name"],
                    header_row=detected["header_row"],
                    headers=detected["headers"],
                    layout_type=detected["layout_type"],
                    field_mapping=preview_mapping,
                    item_name_parse_strategy=detected["item_name_parse_strategy"],
                )
            preview_id = uuid.uuid4().hex
            with self._lock:
                self._prune_previews()
                self._pending[preview_id] = _PendingPreview(
                    preview_id=preview_id,
                    path=path,
                    filename=filename,
                    file_sha256=file_sha256,
                    file_size_bytes=size,
                    detected=detected,
                    profile_id=selected_profile.profile_id if selected_profile else None,
                    expires_at=time.monotonic() + PREVIEW_TTL_SECONDS,
                    mapping_required=mapping_required,
                )
                record = self._pending[preview_id]
                retained = True
            if parsed is not None:
                record.analysis_key = self._analysis_key(detected)
            return self._preview_payload(record, parsed, size)
        except Exception as exc:
            if path is not None:
                path.unlink(missing_ok=True)
            if preview_id is not None:
                with self._lock:
                    self._pending.pop(preview_id, None)
            self.repository.record_attempt("failed", error_code=getattr(exc, "code", "PREVIEW_FAILED"))
            if isinstance(exc, OneCImportError):
                raise
            raise OneCImportError("Не удалось подготовить предпросмотр XLSX.") from exc
        finally:
            if not retained and path is not None:
                path.unlink(missing_ok=True)
            await upload.close()

    @staticmethod
    def _analysis_key(detected: dict) -> str:
        payload = {
            "sheet_name": detected["sheet_name"],
            "header_row": detected["header_row"],
            "layout_type": detected["layout_type"],
            "field_mapping": detected["field_mapping"],
            "item_name_parse_strategy": detected["item_name_parse_strategy"],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _preview_payload(self, record: _PendingPreview, parsed: ParsedWorkbook | None, size: int | None = None) -> dict:
        detected = record.detected
        warnings = list(parsed.warnings) if parsed is not None else []
        if record.mapping_required:
            warnings.insert(0, {
                "code": "mapping_required",
                "count": 1,
                "message": "Проверьте строку заголовков и сопоставьте наименование и хотя бы одно числовое поле отчёта.",
            })
        return {
                "preview_id": record.preview_id,
                "filename": record.filename,
                "file_size_bytes": record.file_size_bytes if size is None else size,
                "sha256": record.file_sha256,
                "sheet_names": detected["sheet_names"],
                "sheet_name": detected["sheet_name"],
                "header_row": detected["header_row"],
                "headers": detected["headers"],
                "header_signature": detected["header_signature"],
                "layout_type": detected["layout_type"],
                "field_mapping": detected["field_mapping"],
                "item_name_parse_strategy": detected["item_name_parse_strategy"],
                "mapping_profile_id": record.profile_id,
                "profile_mapping_required": record.mapping_required,
                "mapping_required": record.mapping_required,
                "expires_in_seconds": PREVIEW_TTL_SECONDS,
                "summary": ({**parsed.summary(), "warnings": warnings[:5], "warning_count": len(warnings)} if parsed is not None else {"warnings": warnings[:5], "warning_count": len(warnings)}),
                "sample": parsed.sample()[:MAX_PREVIEW_ROWS] if parsed is not None else [],
            }

    async def inspect_preview_sheet(self, preview_id: str, sheet_name: str, header_row: int | None = None) -> dict:
        with self._lock:
            self._prune_previews()
            record = self._pending.get(preview_id)
        if record is None:
            raise OneCImportError("Предпросмотр истёк или уже использован. Загрузите файл повторно.")
        acquired = await asyncio.to_thread(record.analysis_lock.acquire)
        try:
            result = await asyncio.to_thread(inspect_sheet, record.path, sheet_name, header_row)
            selected_row, headers, mapping, layout_type = result
            return {
                "sheet_names": record.detected["sheet_names"],
                "sheet_name": sheet_name,
                "header_row": selected_row,
                "headers": headers,
                "field_mapping": mapping,
                "layout_type": layout_type,
            }
        finally:
            if acquired:
                record.analysis_lock.release()

    async def analyze_preview(self, preview_id: str, request: PreviewMappingRequest) -> dict:
        with self._lock:
            self._prune_previews()
            record = self._pending.get(preview_id)
        if record is None:
            raise OneCImportError("Предпросмотр истёк или уже использован. Загрузите файл повторно.")
        acquired = await asyncio.to_thread(record.analysis_lock.acquire)
        try:
            header_row, headers, _, _ = await asyncio.to_thread(
                inspect_sheet, record.path, request.sheet_name, request.header_row
            )
            if len(headers) > 100:
                raise OneCImportError("В листе XLSX слишком много столбцов.")
            mapping = {field: request.field_mapping.get(field) for field in FIELD_NAMES}
            if any(index is not None and index >= len(headers) for index in mapping.values()):
                raise OneCImportError("Сопоставление содержит столбец вне заголовков.")
            detected = dict(record.detected)
            detected.update({
                "sheet_name": request.sheet_name,
                "header_row": header_row,
                "headers": headers,
                "header_signature": header_signature(headers),
                "layout_type": request.layout_type,
                "field_mapping": mapping,
                "item_name_parse_strategy": request.item_name_parse_strategy,
            })
            parsed = await asyncio.to_thread(
                parse_workbook,
                record.path,
                filename=record.filename,
                file_sha256=record.file_sha256,
                sheet_name=request.sheet_name,
                header_row=header_row,
                headers=headers,
                layout_type=request.layout_type,
                field_mapping=mapping,
                item_name_parse_strategy=request.item_name_parse_strategy,
            )
            record.detected = detected
            record.mapping_required = False
            record.analysis_key = self._analysis_key(detected)
            return self._preview_payload(record, parsed)
        finally:
            if acquired:
                record.analysis_lock.release()

    def _consume_preview(self, preview_id: str) -> _PendingPreview:
        with self._lock:
            self._prune_previews()
            record = self._pending.pop(preview_id, None)
        if record is None:
            raise OneCImportError("Предпросмотр истёк или уже использован. Загрузите файл повторно.")
        return record

    async def import_confirmed(self, request: ImportMappingRequest) -> dict:
        pending = self._consume_preview(request.preview_id)
        await asyncio.to_thread(pending.analysis_lock.acquire)
        file_sha256 = pending.file_sha256
        staging: Path | None = None
        try:
            detected = pending.detected
            mapping = {field: request.field_mapping.get(field) for field in FIELD_NAMES}
            if request.sheet_name not in (None, detected["sheet_name"]) or request.header_row not in (None, detected["header_row"]):
                raise OneCImportError("Сначала обновите предпросмотр для выбранного листа и строки заголовков.")
            requested_config = dict(detected)
            requested_config.update({
                "layout_type": request.layout_type,
                "field_mapping": mapping,
                "item_name_parse_strategy": request.item_name_parse_strategy,
            })
            if pending.mapping_required or pending.analysis_key != self._analysis_key(requested_config):
                raise OneCImportError("Сначала обновите предпросмотр для выбранного сопоставления.")
            if any(index is not None and index >= len(detected["headers"]) for index in mapping.values()):
                raise OneCImportError("Сопоставление содержит столбец вне заголовков.")
            profile_id = request.profile_id if request.save_profile else pending.profile_id
            if request.save_profile:
                profile_name = (request.profile_name or "").strip()
                if not profile_name and profile_id:
                    profile_name = (self.repository.profile(profile_id) or ImportProfile(
                        profile_id=uuid.uuid4().hex, name="Импорт 1С", layout_type=request.layout_type,
                        sheet_name=detected["sheet_name"], header_row=detected["header_row"],
                        field_mapping={key: value for key, value in mapping.items() if value is not None},
                        item_name_parse_strategy=request.item_name_parse_strategy,
                        header_signature=detected["header_signature"], parser_version=PARSER_VERSION,
                    )).name
                if not profile_name:
                    raise OneCImportError("Укажите название профиля сопоставления.")
                if profile_id and self.repository.profile(profile_id) is None:
                    raise OneCImportError("Профиль для обновления не найден.")
                profile = ImportProfile(
                    profile_id=profile_id or uuid.uuid4().hex,
                    name=profile_name,
                    layout_type=request.layout_type,
                    sheet_name=detected["sheet_name"],
                    header_row=detected["header_row"],
                    field_mapping={key: value for key, value in mapping.items() if value is not None},
                    item_name_parse_strategy=request.item_name_parse_strategy,
                    header_signature=detected["header_signature"],
                    parser_version=PARSER_VERSION,
                )
                profile = self.repository.save_profile(profile)
                profile_id = profile.profile_id

            parsed: ParsedWorkbook = await asyncio.to_thread(
                parse_workbook,
                pending.path,
                filename=pending.filename,
                file_sha256=file_sha256,
                sheet_name=detected["sheet_name"],
                header_row=detected["header_row"],
                headers=detected["headers"],
                layout_type=request.layout_type,
                field_mapping=mapping,
                item_name_parse_strategy=request.item_name_parse_strategy,
            )
            active = self.repository.active_metadata()
            if active and active.get("sha256") == file_sha256:
                self.repository.record_attempt("already_active", file_sha256=file_sha256, warning_count=len(parsed.warnings))
                return {
                    "status": "already_active",
                    "idempotent": True,
                    "active_import": active,
                    "profile_id": profile_id,
                }

            with self._import_lock:
                self.repository.record_attempt("building", file_sha256=file_sha256)
                staging = await asyncio.to_thread(
                    self.repository.build_staging_snapshot, parsed, profile_id=profile_id
                )
                self.repository.activate(staging)
                staging = None
            self.repository.record_attempt("succeeded", file_sha256=file_sha256, warning_count=len(parsed.warnings))
            return {
                "status": "succeeded",
                "idempotent": False,
                "active_import": self.repository.active_metadata(),
                "profile_id": profile_id,
            }
        except OneCImportError as exc:
            self.repository.record_attempt("failed", file_sha256=file_sha256, error_code="IMPORT_VALIDATION_FAILED")
            raise
        except Exception as exc:
            self.repository.record_attempt("failed", file_sha256=file_sha256, error_code="IMPORT_FAILED")
            raise OneCImportError("Не удалось активировать историю закупок. Предыдущий снимок сохранён.") from exc
        finally:
            pending.path.unlink(missing_ok=True)
            if staging is not None:
                staging.unlink(missing_ok=True)
            pending.analysis_lock.release()


__all__ = ["OneCHistoryImportService"]
