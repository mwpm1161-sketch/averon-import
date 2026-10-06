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
from averon_import.services.one_c_history.activity import OneCHistoryActivityConflict, OneCHistoryActivityRegistry
from averon_import.services.one_c_history.xlsx_import import (
    MAX_PREVIEW_ROWS,
    MAX_UPLOAD_BYTES,
    PARSER_VERSION,
    OneCImportError,
    ParsedWorkbook,
    detect_workbook,
    header_signature,
    inspect_sheet_mapping,
    parse_workbook,
)


PREVIEW_TTL_SECONDS = 20 * 60
MAX_PENDING_PREVIEWS = 3  # Compatibility name for the per-user limit.
MAX_PENDING_PREVIEWS_PER_USER = 3
MAX_PENDING_PREVIEWS_TOTAL = 20
UPLOAD_CHUNK_BYTES = 1024 * 1024


class _PendingPreview:
    def __init__(self, *, preview_id: str, path: Path, filename: str, file_sha256: str, file_size_bytes: int, detected: dict, profile_id: str | None, profile_revision: int | None, profile_mapping_key: str | None, expires_at: float, mapping_required: bool, owner_key: str | None = None, profile_selection_required: bool = False):
        self.preview_id = preview_id
        self.path = path
        self.filename = filename
        self.file_sha256 = file_sha256
        self.file_size_bytes = file_size_bytes
        self.detected = detected
        self.profile_id = profile_id
        self.profile_revision = profile_revision
        self.profile_mapping_key = profile_mapping_key
        self.expires_at = expires_at
        self.created_at = time.monotonic()
        self.mapping_required = mapping_required
        self.owner_key = owner_key
        self.profile_selection_required = profile_selection_required
        self.analysis_key: str | None = None
        self.analysis_lock = threading.Lock()


class OneCHistoryImportService:
    """Coordinates bounded previews and atomic activation of 1C history snapshots."""

    def __init__(self, repository: OneCHistoryRepository, activity_registry: OneCHistoryActivityRegistry | None = None):
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
        self.activity_registry = activity_registry or OneCHistoryActivityRegistry()
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

    def _prune_previews(self, owner_key: str | None = None) -> None:
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
        if owner_key is not None:
            owner_previews = sorted(
                ((key, value) for key, value in self._pending.items() if value.owner_key == owner_key),
                key=lambda item: item[1].created_at,
            )
            while len(owner_previews) >= MAX_PENDING_PREVIEWS_PER_USER:
                key, record = owner_previews.pop(0)
                if not record.analysis_lock.acquire(blocking=False):
                    raise OneCImportError("Достигнут предел подготовленных отчётов. Завершите текущую проверку и повторите загрузку.")
                try:
                    self._pending.pop(key, None)
                    record.path.unlink(missing_ok=True)
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

    async def create_preview(self, upload: UploadFile, *, profile_id: str | None = None, owner_key: str | None = None) -> dict:
        path: Path | None = None
        retained = False
        preview_id: str | None = None
        try:
            filename = self._safe_filename(upload.filename)
            with self._lock:
                self._prune_previews()
                owner_count = sum(item.owner_key == owner_key for item in self._pending.values())
                if len(self._pending) >= MAX_PENDING_PREVIEWS_TOTAL and owner_count < MAX_PENDING_PREVIEWS_PER_USER:
                    raise OneCImportError("Слишком много подготовленных отчётов. Повторите загрузку позже.")
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
            profile_to_apply: ImportProfile | None = None
            profile_revision: int | None = None
            incompatible_profile = False
            profile_selection_required = False
            if profile_id:
                requested_profile = self.repository.profile(profile_id)
                if requested_profile is None:
                    raise OneCImportError("Профиль импорта не найден.")
                incompatible_profile = not (
                    requested_profile.sheet_name == detected["sheet_name"]
                    and requested_profile.header_signature == detected["header_signature"]
                    and requested_profile.parser_version in ({PARSER_VERSION, 1} if PARSER_VERSION == 2 else {PARSER_VERSION})
                )
                if incompatible_profile:
                    requested_profile = None
                else:
                    selected_profile = requested_profile
                    profile_to_apply = requested_profile
            else:
                compatible_profiles = self.repository.compatible_profiles(
                    sheet_name=detected["sheet_name"], signature=detected["header_signature"]
                )
                if len(compatible_profiles) == 1:
                    selected_profile = profile_to_apply = compatible_profiles[0]
                elif compatible_profiles:
                    semantic_keys = {self._profile_semantic_key(profile) for profile in compatible_profiles}
                    if len(semantic_keys) == 1:
                        # The mapping is unambiguous, but no individual profile
                        # was explicitly selected, so do not claim provenance.
                        profile_to_apply = compatible_profiles[0]
                    else:
                        profile_selection_required = True
                else:
                    incompatible_profile = bool(self.repository.profiles_for_sheet(detected["sheet_name"]))

            if profile_to_apply:
                detected["layout_type"] = profile_to_apply.layout_type
                old_profile_bridge = profile_to_apply.parser_version == 1 and PARSER_VERSION == 2
                detected_warehouse = detected.get("event_field_mapping", {}).get("warehouse")
                old_group_mapping = profile_to_apply.group_field_mapping or profile_to_apply.field_mapping
                old_event_mapping = profile_to_apply.event_field_mapping or profile_to_apply.field_mapping
                detected["group_field_mapping"] = (
                    {key: old_group_mapping.get(key) for key in FIELD_NAMES}
                    if profile_to_apply.layout_type == "hierarchical_grouped"
                    else {key: None for key in FIELD_NAMES}
                )
                detected["event_field_mapping"] = {key: old_event_mapping.get(key) for key in FIELD_NAMES}
                if old_profile_bridge:
                    if "warehouse" not in old_event_mapping and not detected.get("warehouse_header_ambiguous"):
                        detected["event_field_mapping"]["warehouse"] = detected_warehouse
                    detected["group_field_mapping"]["warehouse"] = None
                detected["field_mapping"] = dict(detected["event_field_mapping"])
                detected["item_name_parse_strategy"] = profile_to_apply.item_name_parse_strategy
            if selected_profile:
                profile_revision = selected_profile.revision
            group_mapping = detected["group_field_mapping"]
            event_mapping = detected["event_field_mapping"]
            mapping_required = profile_selection_required or incompatible_profile or (
                (group_mapping.get("item_name") is None if detected["layout_type"] == "hierarchical_grouped" else event_mapping.get("item_name") is None)
                or not any(
                event_mapping.get(name) is not None
                for name in ("quantity", "reported_unit_price_gross", "amount_gross")
                )
            ) or (detected.get("warehouse_header_ambiguous") and event_mapping.get("warehouse") is None)
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
                    field_mapping=event_mapping,
                    item_name_parse_strategy=detected["item_name_parse_strategy"],
                    group_header_row=detected.get("group_header_row"),
                    event_header_row=detected.get("event_header_row"),
                    group_headers=detected.get("group_headers"),
                    group_field_mapping=group_mapping,
                    event_field_mapping=event_mapping,
                    group_header_signature=detected.get("group_header_signature"),
                    event_header_signature=detected.get("event_header_signature"),
                )
            preview_id = uuid.uuid4().hex
            with self._lock:
                self._prune_previews(owner_key)
                if len(self._pending) >= MAX_PENDING_PREVIEWS_TOTAL:
                    raise OneCImportError("Слишком много подготовленных отчётов. Повторите загрузку позже.")
                self._pending[preview_id] = _PendingPreview(
                    preview_id=preview_id,
                    path=path,
                    filename=filename,
                    file_sha256=file_sha256,
                    file_size_bytes=size,
                    detected=detected,
                    profile_id=selected_profile.profile_id if selected_profile else None,
                    profile_revision=profile_revision,
                    profile_mapping_key=self._analysis_key(detected) if selected_profile else None,
                    expires_at=time.monotonic() + PREVIEW_TTL_SECONDS,
                    mapping_required=mapping_required,
                    owner_key=owner_key,
                    profile_selection_required=profile_selection_required,
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
            "group_header_row": detected.get("group_header_row"),
            "event_header_row": detected.get("event_header_row"),
            "group_headers": detected.get("group_headers", []),
            "group_field_mapping": detected.get("group_field_mapping", {}),
            "event_field_mapping": detected.get("event_field_mapping", detected["field_mapping"]),
            "group_header_signature": detected.get("group_header_signature"),
            "event_header_signature": detected.get("event_header_signature"),
            "item_name_parse_strategy": detected["item_name_parse_strategy"],
            "warehouse_header_ambiguous": bool(detected.get("warehouse_header_ambiguous")),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _normalized_mapping(mapping: dict | None) -> dict[str, int | None]:
        return {field: mapping.get(field) if mapping else None for field in FIELD_NAMES}

    @classmethod
    def _profile_semantic_key(cls, profile: ImportProfile) -> str:
        event_mapping = profile.event_field_mapping or profile.field_mapping
        group_mapping = profile.group_field_mapping or profile.field_mapping
        payload = {
            "layout_type": profile.layout_type,
            "group_field_mapping": cls._normalized_mapping(group_mapping) if profile.layout_type == "hierarchical_grouped" else None,
            "event_field_mapping": cls._normalized_mapping(event_mapping),
            "item_name_parse_strategy": profile.item_name_parse_strategy,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _semantic_import_fingerprint(cls, detected: dict, events: list | None = None) -> str:
        layout_type = detected["layout_type"]
        event_mapping = cls._normalized_mapping(
            detected.get("event_field_mapping", detected.get("field_mapping"))
        )
        group_mapping = (
            cls._normalized_mapping(detected.get("group_field_mapping"))
            if layout_type == "hierarchical_grouped" else None
        )
        payload = {
            "sheet_name": detected["sheet_name"],
            "layout_type": layout_type,
            "group_header_row": detected.get("group_header_row") if layout_type == "hierarchical_grouped" else None,
            "event_header_row": detected.get("event_header_row") or detected.get("header_row"),
            "group_field_mapping": group_mapping,
            "event_field_mapping": event_mapping,
            "flat_field_mapping": event_mapping if layout_type == "flat" else None,
            "item_name_parse_strategy": detected["item_name_parse_strategy"],
            "group_header_signature": detected.get("group_header_signature") if layout_type == "hierarchical_grouped" else None,
            "event_header_signature": detected.get("event_header_signature") or detected.get("header_signature"),
            "parser_version": PARSER_VERSION,
        }
        if events is not None:
            warehouse_rows = [
                [int(event.source_row), event.source_facts.get("warehouse")]
                for event in sorted(events, key=lambda item: int(item.source_row))
                if isinstance(getattr(event, "source_facts", None), dict)
            ]
            payload["event_warehouse_evidence_sha256"] = hashlib.sha256(json.dumps(
                warehouse_rows, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _preview_payload(self, record: _PendingPreview, parsed: ParsedWorkbook | None, size: int | None = None) -> dict:
        detected = record.detected
        warnings = list(parsed.warnings) if parsed is not None else []
        if record.profile_selection_required:
            warnings.insert(0, {
                "code": "multiple_compatible_profiles",
                "count": 1,
                "message": "Найдено несколько совместимых профилей. Выберите профиль импорта.",
            })
        elif record.mapping_required:
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
                "group_header_row": detected.get("group_header_row"),
                "event_header_row": detected.get("event_header_row"),
                "headers": detected["headers"],
                "group_headers": detected.get("group_headers", []),
                "header_signature": detected["header_signature"],
                "group_header_signature": detected.get("group_header_signature"),
                "event_header_signature": detected.get("event_header_signature"),
                "layout_type": detected["layout_type"],
                "field_mapping": detected["field_mapping"],
                "group_field_mapping": detected.get("group_field_mapping", {}),
                "event_field_mapping": detected.get("event_field_mapping", detected["field_mapping"]),
                "warehouse_header_ambiguous": bool(detected.get("warehouse_header_ambiguous")),
                "item_name_parse_strategy": detected["item_name_parse_strategy"],
                "mapping_profile_id": record.profile_id,
                "profile_mapping_required": record.mapping_required,
                "mapping_required": record.mapping_required,
                "expires_in_seconds": PREVIEW_TTL_SECONDS,
                "summary": ({**parsed.summary(), "warnings": warnings[:5], "warning_count": len(warnings)} if parsed is not None else {"warnings": warnings[:5], "warning_count": len(warnings)}),
                "sample": parsed.sample()[:MAX_PREVIEW_ROWS] if parsed is not None else [],
            }

    async def inspect_preview_sheet(
        self,
        preview_id: str,
        sheet_name: str,
        header_row: int | None = None,
        *,
        group_header_row: int | None = None,
        event_header_row: int | None = None,
        owner_key: str | None = None,
    ) -> dict:
        with self._lock:
            self._prune_previews()
            record = self._pending.get(preview_id)
        if record is None or record.owner_key != owner_key:
            raise OneCImportError("Предпросмотр истёк или уже использован. Загрузите файл повторно.")
        acquired = await asyncio.to_thread(record.analysis_lock.acquire)
        try:
            result = await asyncio.to_thread(
                inspect_sheet_mapping,
                record.path,
                sheet_name,
                group_header_row=group_header_row,
                event_header_row=event_header_row or header_row,
            )
            return {
                "sheet_names": record.detected["sheet_names"],
                "sheet_name": sheet_name,
                **{key: result[key] for key in (
                    "header_row", "group_header_row", "event_header_row", "headers",
                    "group_headers", "field_mapping", "group_field_mapping",
                    "event_field_mapping", "group_header_signature",
                    "event_header_signature", "header_signature", "layout_type",
                    "item_name_parse_strategy", "warehouse_header_ambiguous",
                )},
            }
        finally:
            if acquired:
                record.analysis_lock.release()

    async def analyze_preview(self, preview_id: str, request: PreviewMappingRequest, *, owner_key: str | None = None) -> dict:
        with self._lock:
            self._prune_previews()
            record = self._pending.get(preview_id)
        if record is None or record.owner_key != owner_key:
            raise OneCImportError("Предпросмотр истёк или уже использован. Загрузите файл повторно.")
        acquired = await asyncio.to_thread(record.analysis_lock.acquire)
        try:
            structure = await asyncio.to_thread(
                inspect_sheet_mapping,
                record.path,
                request.sheet_name,
                group_header_row=request.group_header_row,
                event_header_row=request.event_header_row or request.header_row,
            )
            header_row = structure["header_row"]
            headers = structure["headers"]
            if len(headers) > 100:
                raise OneCImportError("В листе XLSX слишком много столбцов.")
            mapping = {field: request.field_mapping.get(field) for field in FIELD_NAMES}
            if request.layout_type == "hierarchical_grouped":
                group_headers = structure["group_headers"] or headers
                group_mapping_source = request.group_field_mapping or request.field_mapping
                event_mapping_source = request.event_field_mapping or request.field_mapping
                group_mapping = {field: group_mapping_source.get(field) for field in FIELD_NAMES}
                group_mapping["warehouse"] = None
                event_mapping = {field: event_mapping_source.get(field) for field in FIELD_NAMES}
                group_row = request.group_header_row or structure.get("group_header_row") or header_row
                event_row = request.event_header_row or header_row
            else:
                group_headers = []
                group_mapping = {field: None for field in FIELD_NAMES}
                event_mapping = mapping
                group_row = None
                event_row = header_row
            if any(index is not None and index >= len(headers) for index in event_mapping.values()):
                raise OneCImportError("Сопоставление содержит столбец вне заголовков.")
            if any(index is not None and index >= len(group_headers) for index in group_mapping.values()):
                raise OneCImportError("Сопоставление полей группы содержит столбец вне заголовков.")
            group_signature = header_signature(group_headers) if group_headers else None
            event_signature = header_signature(headers)
            combined_signature = (
                hashlib.sha256(json.dumps([group_signature, event_signature], separators=(",", ":")).encode()).hexdigest()
                if group_row is not None and group_row != event_row
                else event_signature
            )
            detected = dict(record.detected)
            detected.update({
                "sheet_name": request.sheet_name,
                "header_row": header_row,
                "headers": headers,
                "group_header_row": group_row,
                "event_header_row": event_row,
                "group_headers": group_headers,
                "group_header_signature": group_signature,
                "event_header_signature": event_signature,
                "header_signature": combined_signature,
                "layout_type": request.layout_type,
                "field_mapping": event_mapping,
                "group_field_mapping": group_mapping,
                "event_field_mapping": event_mapping,
                "item_name_parse_strategy": request.item_name_parse_strategy,
                "warehouse_header_ambiguous": structure.get("warehouse_header_ambiguous", False),
            })
            if detected.get("warehouse_header_ambiguous") and event_mapping.get("warehouse") is None:
                raise OneCImportError("Выберите столбец «Склад» вручную: заголовок встречается несколько раз.")
            parsed = await asyncio.to_thread(
                parse_workbook,
                record.path,
                filename=record.filename,
                file_sha256=record.file_sha256,
                sheet_name=request.sheet_name,
                header_row=event_row,
                headers=headers,
                layout_type=request.layout_type,
                field_mapping=event_mapping,
                item_name_parse_strategy=request.item_name_parse_strategy,
                group_header_row=group_row,
                event_header_row=event_row,
                group_headers=group_headers,
                group_field_mapping=group_mapping,
                event_field_mapping=event_mapping,
                group_header_signature=group_signature,
                event_header_signature=event_signature,
            )
            record.detected = detected
            record.mapping_required = False
            record.profile_selection_required = False
            record.analysis_key = self._analysis_key(detected)
            return self._preview_payload(record, parsed)
        finally:
            if acquired:
                record.analysis_lock.release()

    def _get_preview(self, preview_id: str, owner_key: str | None) -> _PendingPreview:
        with self._lock:
            self._prune_previews()
            record = self._pending.get(preview_id)
            if record is None or record.owner_key != owner_key:
                raise OneCImportError("Предпросмотр истёк или уже использован. Загрузите файл повторно.")
            if not record.analysis_lock.acquire(blocking=False):
                raise OneCHistoryActivityConflict(
                    "ONE_C_HISTORY_UPDATE_IN_PROGRESS",
                    "Этот предпросмотр уже обрабатывается. Повторите позже.",
                    active_sourcing_count=int(self.activity_registry.status()["active_sourcing_count"]),
                )
        return record

    def _extend_preview_ttl(self, preview_id: str, pending: _PendingPreview) -> None:
        with self._lock:
            if self._pending.get(preview_id) is pending:
                pending.expires_at = time.monotonic() + PREVIEW_TTL_SECONDS

    async def import_confirmed(
        self,
        request: ImportMappingRequest,
        *,
        owner_key: str | None = None,
        actor: dict[str, str | None] | None = None,
        can_manage_profiles: bool = True,
    ) -> dict:
        if request.save_profile and not can_manage_profiles:
            raise PermissionError("Недостаточно прав для сохранения профиля импорта.")
        try:
            pending = self._get_preview(request.preview_id, owner_key)
        except OneCHistoryActivityConflict as exc:
            if actor:
                self.repository.record_attempt(exc.code, actor=actor)
            raise
        try:
            update_lease = self.activity_registry.try_begin_update()
        except OneCHistoryActivityConflict as exc:
            self._extend_preview_ttl(request.preview_id, pending)
            pending.analysis_lock.release()
            if actor:
                self.repository.record_attempt(exc.code, actor=actor)
            raise
        except Exception:
            pending.analysis_lock.release()
            raise
        file_sha256 = pending.file_sha256
        staging: Path | None = None
        committed = False
        terminal = False
        post_commit_warnings: list[str] = []
        try:
            detected = pending.detected
            legacy_mapping = {field: request.field_mapping.get(field) for field in FIELD_NAMES}
            has_role_mappings = bool(request.group_field_mapping or request.event_field_mapping)
            if has_role_mappings:
                group_mapping = {field: request.group_field_mapping.get(field) for field in FIELD_NAMES}
                event_mapping = {field: request.event_field_mapping.get(field) for field in FIELD_NAMES}
            elif legacy_mapping == detected.get("field_mapping"):
                group_mapping = dict(detected.get("group_field_mapping", {}))
                event_mapping = dict(detected.get("event_field_mapping", detected["field_mapping"]))
            else:
                group_mapping = dict(legacy_mapping)
                event_mapping = dict(legacy_mapping)
            if request.layout_type == "hierarchical_grouped":
                group_mapping["warehouse"] = None
            group_row = request.group_header_row if request.group_header_row is not None else detected.get("group_header_row")
            event_row = request.event_header_row if request.event_header_row is not None else detected.get("event_header_row", detected["header_row"])
            mapping = event_mapping
            if request.sheet_name not in (None, detected["sheet_name"]) or request.header_row not in (None, detected["header_row"]):
                raise OneCImportError("Сначала обновите предпросмотр для выбранного листа и строки заголовков.")
            if group_row != detected.get("group_header_row") or event_row != detected.get("event_header_row", detected["header_row"]):
                raise OneCImportError("Сначала обновите предпросмотр для выбранных строк заголовков.")
            requested_config = dict(detected)
            requested_config.update({
                "layout_type": request.layout_type,
                "field_mapping": mapping,
                "group_field_mapping": group_mapping,
                "event_field_mapping": event_mapping,
                "group_header_row": group_row,
                "event_header_row": event_row,
                "item_name_parse_strategy": request.item_name_parse_strategy,
            })
            if pending.mapping_required or pending.analysis_key != self._analysis_key(requested_config):
                raise OneCImportError("Сначала обновите предпросмотр для выбранного сопоставления.")
            if any(index is not None and index >= len(detected["headers"]) for index in event_mapping.values()):
                raise OneCImportError("Сопоставление содержит столбец вне заголовков.")
            if detected.get("warehouse_header_ambiguous") and event_mapping.get("warehouse") is None:
                raise OneCImportError("Выберите столбец «Склад» вручную: заголовок встречается несколько раз.")
            if any(index is not None and index >= len(detected.get("group_headers", detected["headers"])) for index in group_mapping.values()):
                raise OneCImportError("Сопоставление полей группы содержит столбец вне заголовков.")
            profile_id = request.profile_id if request.save_profile else (
                pending.profile_id if pending.profile_mapping_key == pending.analysis_key else None
            )
            profile_revision = pending.profile_revision if profile_id == pending.profile_id else None
            if request.save_profile:
                profile_name = (request.profile_name or "").strip()
                if not profile_name and profile_id:
                    profile_name = (self.repository.profile(profile_id) or ImportProfile(
                        profile_id=uuid.uuid4().hex, name="Импорт 1С", layout_type=request.layout_type,
                        sheet_name=detected["sheet_name"], header_row=detected["header_row"],
                        field_mapping={key: value for key, value in mapping.items() if value is not None},
                        group_header_row=group_row, event_header_row=event_row,
                        group_field_mapping={key: value for key, value in group_mapping.items() if value is not None},
                        event_field_mapping={key: value for key, value in event_mapping.items() if value is not None},
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
                    group_header_row=group_row,
                    event_header_row=event_row,
                    group_field_mapping={key: value for key, value in group_mapping.items() if value is not None},
                    event_field_mapping={key: value for key, value in event_mapping.items() if value is not None},
                    item_name_parse_strategy=request.item_name_parse_strategy,
                    header_signature=detected["header_signature"],
                    parser_version=PARSER_VERSION,
                )
                profile = self.repository.save_profile(profile)
                profile_id = profile.profile_id
                profile_revision = profile.revision

            parsed: ParsedWorkbook = await asyncio.to_thread(
                parse_workbook,
                pending.path,
                filename=pending.filename,
                file_sha256=file_sha256,
                sheet_name=detected["sheet_name"],
                header_row=event_row or detected["header_row"],
                headers=detected["headers"],
                layout_type=request.layout_type,
                field_mapping=mapping,
                item_name_parse_strategy=request.item_name_parse_strategy,
                group_header_row=group_row,
                event_header_row=event_row,
                group_headers=detected.get("group_headers", []),
                group_field_mapping=group_mapping,
                event_field_mapping=event_mapping,
                group_header_signature=detected.get("group_header_signature"),
                event_header_signature=detected.get("event_header_signature"),
            )
            semantic_import_fingerprint = self._semantic_import_fingerprint(requested_config, parsed.events)
            active = self.repository.active_metadata()
            if (
                active
                and active.get("sha256") == file_sha256
                and active.get("semantic_import_fingerprint") == semantic_import_fingerprint
            ):
                self.repository.record_attempt(
                    "already_active", file_sha256=file_sha256, warning_count=len(parsed.warnings),
                    actor=actor, resulting_catalog_version=self.repository.catalog_version(),
                )
                terminal = True
                return {
                    "status": "already_active",
                    "idempotent": True,
                    "active_import": active,
                    "profile_id": profile_id,
                }

            with self._import_lock:
                self.repository.record_attempt("building", file_sha256=file_sha256)
                provenance_body = {
                    "profile_id": profile_id,
                    "profile_revision": profile_revision,
                    "layout_type": request.layout_type,
                    "group_header_row": group_row,
                    "event_header_row": event_row,
                    "group_field_mapping": group_mapping,
                    "event_field_mapping": event_mapping,
                    "field_mapping": mapping,
                    "item_name_parse_strategy": request.item_name_parse_strategy,
                    "group_header_signature": detected.get("group_header_signature"),
                    "event_header_signature": detected.get("event_header_signature"),
                    "header_signature": detected["header_signature"],
                    "parser_version": PARSER_VERSION,
                }
                provenance = {
                    **provenance_body,
                    "fingerprint": hashlib.sha256(json.dumps(
                        provenance_body, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")).hexdigest(),
                }
                staging = await asyncio.to_thread(
                    self.repository.build_staging_snapshot, parsed,
                    profile_id=profile_id, mapping_provenance=provenance,
                    semantic_import_fingerprint=semantic_import_fingerprint,
                )
                post_commit_warnings.extend(self.repository.activate(staging))
                committed = True
                staging = None
            try:
                self.repository.record_attempt(
                    "succeeded", file_sha256=file_sha256, warning_count=len(parsed.warnings),
                    actor=actor, resulting_catalog_version=self.repository.catalog_version(),
                )
            except Exception:
                post_commit_warnings.append("Снимок активирован, но статус попытки импорта не удалось обновить.")
            try:
                active_metadata = self.repository.active_metadata()
            except Exception:
                active_metadata = None
                post_commit_warnings.append("Снимок активирован, но его статус не удалось прочитать.")
            terminal = True
            return {
                "status": "succeeded",
                "idempotent": False,
                "active_import": active_metadata,
                "profile_id": profile_id,
                "warnings": post_commit_warnings,
            }
        except OneCImportError as exc:
            terminal = True
            if committed:
                try:
                    active_metadata = self.repository.active_metadata()
                except Exception:
                    active_metadata = None
                try:
                    self.repository.record_attempt(
                        "succeeded_with_warning", file_sha256=file_sha256,
                        warning_count=len(parsed.warnings), actor=actor,
                        resulting_catalog_version=self.repository.catalog_version(),
                    )
                except Exception:
                    pass
                return {
                    "status": "succeeded", "idempotent": False,
                    "active_import": active_metadata, "profile_id": pending.profile_id,
                    "warnings": [*post_commit_warnings, "Снимок активирован; завершение служебного учёта потребовало проверки."],
                }
            try:
                self.repository.record_attempt(
                    "failed", file_sha256=file_sha256, error_code="IMPORT_VALIDATION_FAILED", actor=actor,
                )
            except Exception:
                pass
            raise
        except Exception as exc:
            terminal = True
            if committed:
                try:
                    active_metadata = self.repository.active_metadata()
                except Exception:
                    active_metadata = None
                try:
                    self.repository.record_attempt(
                        "succeeded_with_warning", file_sha256=file_sha256,
                        warning_count=len(parsed.warnings), actor=actor,
                        resulting_catalog_version=self.repository.catalog_version(),
                    )
                except Exception:
                    pass
                return {
                    "status": "succeeded", "idempotent": False, "active_import": active_metadata,
                    "profile_id": pending.profile_id,
                    "warnings": [*post_commit_warnings, "Снимок активирован; служебное обновление после фиксации завершилось с ошибкой."],
                }
            try:
                self.repository.record_attempt("failed", file_sha256=file_sha256, error_code="IMPORT_FAILED", actor=actor)
            except Exception:
                pass
            raise OneCImportError("Не удалось активировать историю закупок. Текущий снимок не изменён.") from exc
        finally:
            if terminal:
                with self._lock:
                    if self._pending.get(request.preview_id) is pending:
                        self._pending.pop(request.preview_id, None)
                try:
                    pending.path.unlink(missing_ok=True)
                except OSError:
                    pass
            if staging is not None:
                try:
                    staging.unlink(missing_ok=True)
                except OSError:
                    pass
            pending.analysis_lock.release()
            update_lease.release()


__all__ = ["OneCHistoryImportService"]
