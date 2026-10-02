from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .activity import TenderActivityRegistry
from .parser import MAX_UPLOAD_BYTES

PREVIEW_TTL_SECONDS = 20 * 60
IDLE_TTL_SECONDS = 24 * 60 * 60
ABSOLUTE_TTL_SECONDS = 72 * 60 * 60
MAX_WORKSPACES_PER_USER = 3
MAX_WORKSPACES_GLOBAL = 20
MAX_PREVIEWS_PER_USER = 3
MAX_PREVIEWS_GLOBAL = 20
MAX_WORKSPACE_METADATA_BYTES = 10 * 1024 * 1024
MAX_TENDER_RUN_BYTES = 1024 * 1024
MAX_TENDER_RUNS_PER_WORKSPACE = 5
MAX_TENDER_STORAGE_BYTES = MAX_WORKSPACES_GLOBAL * (MAX_UPLOAD_BYTES + 2 * MAX_WORKSPACE_METADATA_BYTES)
ID_RE = re.compile(r"^[a-f0-9]{32}$")
PUBLIC_ABSOLUTE_PATH_RE = re.compile(r"(?:\b[A-Za-z]:[\\/]|\\\\[^\\\s]+\\|/(?:home|var|opt|tmp|users|mnt|srv|root|etc)/)", re.IGNORECASE)


def _sanitize_public_payload(value: Any) -> Any:
    if isinstance(value, str) and PUBLIC_ABSOLUTE_PATH_RE.search(value):
        return "[скрытый путь]"
    if isinstance(value, list):
        return [_sanitize_public_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_public_payload(item) for key, item in value.items()}
    return value


class TenderWorkspaceError(ValueError):
    def __init__(self, message: str, status_code: int = 404, code: str = "TENDER_NOT_FOUND"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class TenderWorkspaceRepository:
    """Durable owner-bound preview and confirmed tender workspaces."""

    def __init__(self, data_dir: Path, activity: TenderActivityRegistry | None = None):
        self.root = Path(data_dir) / "manual_tenders"
        self.preview_root = self.root / "previews"
        self.workspace_root = self.root / "workspaces"
        self.activity = activity or TenderActivityRegistry()
        self._lock = threading.RLock()
        for directory in (self.root, self.preview_root, self.workspace_root):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        # A process-local analysis job cannot be replayed after restart.
        # Leave the preview and source available for owner-initiated reupload.
        for path in self._preview_dirs():
            try:
                metadata = self._metadata(path)
                if metadata.get("status") in {"queued", "analyzing"}:
                    metadata.update({"status": "interrupted", "error": "Проверка была прервана перезапуском. Загрузите файл повторно."})
                    self._atomic_json(path / "workspace.json", metadata)
            except TenderWorkspaceError:
                continue

    @staticmethod
    def _validate_id(value: str) -> str:
        if not ID_RE.fullmatch(str(value or "")):
            raise TenderWorkspaceError("Тендер не найден.")
        return value

    @staticmethod
    def _metadata(path: Path) -> dict[str, Any]:
        try:
            value = json.loads((path / "workspace.json").read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("invalid metadata")
            return value
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise TenderWorkspaceError("Данные тендера повреждены.", 409, "TENDER_WORKSPACE_CORRUPT") from exc

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > MAX_WORKSPACE_METADATA_BYTES:
            raise TenderWorkspaceError("Структура книги превышает допустимый объём рабочего пространства.", 413, "TENDER_WORKSPACE_TOO_LARGE")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _remove_tree(path: Path) -> None:
        # Confirmed source files are read-only; restore delete permission only
        # inside the exact workspace that is already expired/deleting.
        if path.exists():
            for child in path.iterdir():
                if child.is_file():
                    try:
                        os.chmod(child, 0o600)
                    except OSError:
                        pass
        shutil.rmtree(path)

    def _preview_dirs(self) -> list[Path]:
        return [item for item in self.preview_root.iterdir() if item.is_dir() and ID_RE.fullmatch(item.name)]

    def _workspace_dirs(self) -> list[Path]:
        return [item for item in self.workspace_root.iterdir() if item.is_dir() and ID_RE.fullmatch(item.name)]

    @staticmethod
    def _cleanup_orphan_metadata_temps(directory: Path, *, older_than_seconds: int = 3600) -> None:
        cutoff = time.time() - older_than_seconds
        try:
            candidates = directory.glob(".workspace.json.*.tmp")
            for candidate in candidates:
                try:
                    if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                        candidate.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass

    def cleanup(self) -> dict[str, int]:
        now = _now()
        removed_preview = removed_workspaces = 0
        with self._lock:
            for path in self._preview_dirs():
                self._cleanup_orphan_metadata_temps(path)
                try:
                    metadata = self._metadata(path)
                    if datetime.fromisoformat(metadata["expires_at"]) <= now and self.activity.begin_delete(path.name):
                        try:
                            self._remove_tree(path)
                            removed_preview += 1
                        finally:
                            self.activity.finish_delete(path.name)
                except TenderWorkspaceError:
                    if self.activity.begin_delete(path.name):
                        try:
                            self._remove_tree(path)
                            removed_preview += 1
                        except OSError:
                            pass
                        finally:
                            self.activity.finish_delete(path.name)
                except (OSError, KeyError, ValueError):
                    continue
            for path in self._workspace_dirs():
                self._cleanup_orphan_metadata_temps(path)
                try:
                    metadata = self._metadata(path)
                    expired = (
                        datetime.fromisoformat(metadata["absolute_expires_at"]) <= now or
                        datetime.fromisoformat(metadata["last_access_at"]) + timedelta(seconds=IDLE_TTL_SECONDS) <= now
                    )
                    if expired and self.activity.begin_delete(path.name):
                        try:
                            self._remove_tree(path)
                            removed_workspaces += 1
                        finally:
                            self.activity.finish_delete(path.name)
                except TenderWorkspaceError:
                    if self.activity.begin_delete(path.name):
                        try:
                            self._remove_tree(path)
                            removed_workspaces += 1
                        except OSError:
                            pass
                        finally:
                            self.activity.finish_delete(path.name)
                except (OSError, KeyError, ValueError):
                    continue
            for path in self.workspace_root.iterdir():
                if path.is_dir() and re.fullmatch(r"\.[a-f0-9]{32}\.creating", path.name):
                    try:
                        self._remove_tree(path)
                    except OSError:
                        continue
        return {"previews_removed": removed_preview, "workspaces_removed": removed_workspaces}

    def list_public_workspaces(self, owner_id: str, limit: int = 10) -> dict[str, Any]:
        """Return owner-scoped workspace summaries without refreshing idle TTL."""
        bounded_limit = max(1, min(int(limit), 20))
        self.cleanup()
        now = _now()
        owned: list[tuple[float, float, str, dict[str, Any]]] = []
        with self._lock:
            for path in self._workspace_dirs():
                try:
                    metadata = self._metadata(path)
                    if metadata.get("owner_id") != owner_id:
                        continue
                    created_at = datetime.fromisoformat(metadata["created_at"])
                    last_access_at = datetime.fromisoformat(metadata["last_access_at"])
                    absolute_expires_at = datetime.fromisoformat(metadata["absolute_expires_at"])
                    if absolute_expires_at <= now or last_access_at + timedelta(seconds=IDLE_TTL_SECONDS) <= now:
                        continue
                    counts = metadata.get("counts")
                    item_count = int(counts.get("item", 0)) if isinstance(counts, dict) else 0
                    summary = _sanitize_public_payload({
                        "tender_id": path.name,
                        "filename": metadata["filename"],
                        "sheet_name": metadata["sheet_name"],
                        "item_count": max(0, item_count),
                        "created_at": metadata["created_at"],
                        "last_access_at": metadata["last_access_at"],
                        "absolute_expires_at": metadata["absolute_expires_at"],
                        "revision": metadata.get("revision"),
                    })
                    owned.append((last_access_at.timestamp(), created_at.timestamp(), path.name, summary))
                except (TenderWorkspaceError, OSError, KeyError, TypeError, ValueError):
                    continue
        owned.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return {
            "tenders": [item[3] for item in owned[:bounded_limit]],
            "active_count": len(owned),
            "limit": MAX_WORKSPACES_PER_USER,
        }

    def reserve_preview(self, owner_id: str, filename: str, size: int, digest: str) -> dict[str, Any]:
        if not filename.casefold().endswith(".xlsx") or filename.casefold().endswith(".xlsm"):
            raise TenderWorkspaceError("Поддерживаются только файлы .xlsx.", 400, "TENDER_XLSX_REQUIRED")
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            raise TenderWorkspaceError("Размер XLSX должен быть не более 5 МиБ.", 413, "TENDER_UPLOAD_TOO_LARGE")
        self.cleanup()
        with self._lock:
            previews = []
            for directory in self._preview_dirs():
                try:
                    previews.append((directory, self._metadata(directory)))
                except TenderWorkspaceError:
                    continue
            duplicate = next((item for _, item in previews if item.get("owner_id") == owner_id and item.get("source_sha256") == digest and item.get("status") in {"queued", "analyzing", "ready"}), None)
            if duplicate:
                return {**duplicate, "deduplicated": True}
            owner_count = sum(1 for _, item in previews if item.get("owner_id") == owner_id)
            if owner_count >= MAX_PREVIEWS_PER_USER or len(previews) >= MAX_PREVIEWS_GLOBAL:
                raise TenderWorkspaceError("Достигнут предел подготовленных файлов. Завершите проверку или повторите позже.", 409, "TENDER_PREVIEW_QUOTA")
            preview_id = uuid.uuid4().hex
            now = _now()
            directory = self.preview_root / preview_id
            directory.mkdir(mode=0o700)
            safe_name = Path(str(filename).replace("\\", "/")).name[:255] or "tender.xlsx"
            metadata = {
                "preview_id": preview_id, "owner_id": owner_id, "filename": safe_name,
                "source_sha256": digest, "file_size_bytes": size, "created_at": _iso(now),
                "expires_at": _iso(now + timedelta(seconds=PREVIEW_TTL_SECONDS)),
                "status": "queued", "parser_version": None, "analysis": None,
                "mapping": None, "error": None,
            }
            self._atomic_json(directory / "workspace.json", metadata)
            return {**metadata, "deduplicated": False}

    def find_preview_by_hash(self, owner_id: str, digest: str) -> dict[str, Any] | None:
        self.cleanup()
        with self._lock:
            for directory in self._preview_dirs():
                try:
                    metadata = self._metadata(directory)
                except TenderWorkspaceError:
                    continue
                if metadata.get("owner_id") == owner_id and metadata.get("source_sha256") == digest:
                    if metadata.get("status") in {"queued", "analyzing", "ready"}:
                        return metadata
        return None

    def write_preview_source(self, preview_id: str, payload: bytes) -> Path:
        directory = self.preview_root / self._validate_id(preview_id)
        source = directory / "source.xlsx"
        temporary = directory / "source.tmp"
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, source)
        return source

    def preview_path(self, preview_id: str, owner_id: str) -> tuple[Path, dict[str, Any]]:
        preview_id = self._validate_id(preview_id)
        self.cleanup()
        path = self.preview_root / preview_id
        try:
            metadata = self._metadata(path)
        except TenderWorkspaceError as exc:
            raise TenderWorkspaceError("Подготовка не найдена.") from exc
        if metadata.get("owner_id") != owner_id:
            raise TenderWorkspaceError("Подготовка не найдена.")
        if datetime.fromisoformat(metadata["expires_at"]) <= _now():
            raise TenderWorkspaceError("Срок подготовки истёк. Загрузите файл повторно.", 410, "TENDER_PREVIEW_EXPIRED")
        return path / "source.xlsx", metadata

    def update_preview(self, preview_id: str, owner_id: str, **updates: Any) -> dict[str, Any]:
        with self._lock:
            _, metadata = self.preview_path(preview_id, owner_id)
            path = self.preview_root / preview_id / "workspace.json"
            metadata.update(updates)
            self._atomic_json(path, metadata)
            return metadata

    def public_preview(self, preview_id: str, owner_id: str) -> dict[str, Any]:
        _, item = self.preview_path(preview_id, owner_id)
        analysis = item.get("analysis") or {}
        return _sanitize_public_payload({
            "preview_id": item["preview_id"], "filename": item["filename"],
            "source_sha256": item["source_sha256"], "expires_at": item["expires_at"],
            "status": item["status"], "error": item.get("error"),
            "analysis": self._public_analysis(analysis) if analysis else None,
        })

    @staticmethod
    def _public_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "parser_version", "source_sha256", "structure", "sheet_names", "sheet_name", "sheet_candidates", "header_candidates", "sheet_visibility",
            "header_row", "headers", "mapping", "mapping_required", "official_template",
            "table_name", "table_ref", "logical_right_edge", "logical_right_column",
            "future_output_columns", "counts", "item_count", "section_count", "total_count",
            "warnings", "sample_rows", "invalid_count", "invalid_examples",
        )
        return {key: analysis.get(key) for key in keys}

    def confirm(self, preview_id: str, owner_id: str, *, mapping: dict[str, int | None] | None = None) -> dict[str, Any]:
        self.cleanup()
        with self._lock:
            source_dir = self.preview_root / self._validate_id(preview_id)
            metadata = self._metadata(source_dir)
            if metadata.get("owner_id") != owner_id:
                raise TenderWorkspaceError("Подготовка не найдена.")
            if datetime.fromisoformat(metadata["expires_at"]) <= _now():
                raise TenderWorkspaceError("Срок подготовки истёк. Загрузите файл повторно.", 410, "TENDER_PREVIEW_EXPIRED")
            analysis = metadata.get("analysis")
            if not analysis or metadata.get("status") != "ready":
                raise TenderWorkspaceError("Проверка файла ещё не завершена.", 409, "TENDER_PREVIEW_NOT_READY")
            if analysis.get("mapping_required"):
                raise TenderWorkspaceError("Сначала задайте однозначное сопоставление колонок.", 409, "TENDER_MAPPING_REQUIRED")
            invalid_count = int(analysis.get("invalid_count", (analysis.get("counts") or {}).get("invalid", 0)))
            if invalid_count > 0:
                raise TenderWorkspaceError(
                    "В таблице есть строки, требующие исправления. Исправьте файл и загрузите его повторно.",
                    409, "TENDER_INVALID_ROWS",
                )
            if int(analysis.get("item_count", 0)) < 1:
                raise TenderWorkspaceError("В книге не найдены строки позиций для импорта.", 409, "TENDER_NO_ITEMS")
            workspaces = []
            for directory in self._workspace_dirs():
                try:
                    workspaces.append(self._metadata(directory))
                except TenderWorkspaceError:
                    continue
            if sum(item.get("owner_id") == owner_id for item in workspaces) >= MAX_WORKSPACES_PER_USER or len(workspaces) >= MAX_WORKSPACES_GLOBAL:
                raise TenderWorkspaceError("Достигнут лимит активных тендеров. Удалите завершённый тендер или повторите позже.", 409, "TENDER_WORKSPACE_QUOTA")
            # One source plus an old and a new bounded atomic metadata file.
            bounded_workspace_bytes = MAX_UPLOAD_BYTES + 2 * MAX_WORKSPACE_METADATA_BYTES
            existing_bytes = self._workspace_storage_bytes()
            if existing_bytes + bounded_workspace_bytes > MAX_WORKSPACES_GLOBAL * bounded_workspace_bytes:
                raise TenderWorkspaceError("Достигнут общий лимит хранилища тендеров.", 409, "TENDER_DISK_QUOTA")
            source = source_dir / "source.xlsx"
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual != metadata.get("source_sha256"):
                raise TenderWorkspaceError("Исходный XLSX изменился после проверки.", 409, "TENDER_SOURCE_CHANGED")
            tender_id = uuid.uuid4().hex
            destination = self.workspace_root / tender_id
            staging = self.workspace_root / f".{tender_id}.creating"
            staging.mkdir(mode=0o700)
            try:
                shutil.copyfile(source, staging / "source.xlsx")
                os.chmod(staging / "source.xlsx", 0o400)
                now = _now()
                frozen = {
                    "tender_id": tender_id, "owner_id": owner_id,
                    "filename": metadata["filename"], "source_sha256": actual,
                    "parser_version": analysis["parser_version"], "sheet_name": analysis["sheet_name"],
                    "header_row": analysis["header_row"], "table_name": analysis.get("table_name"),
                    "table_ref": analysis.get("table_ref"), "mapping": mapping or analysis["mapping"],
                    "source_manifest": analysis["manifest"], "rows": analysis["rows"],
                    "logical_right_edge": analysis["logical_right_edge"],
                    "future_output_columns": analysis["future_output_columns"],
                    "counts": analysis["counts"], "warnings": analysis["warnings"],
                    "revision": 1, "created_at": _iso(now), "last_access_at": _iso(now),
                    "absolute_expires_at": _iso(now + timedelta(seconds=ABSOLUTE_TTL_SECONDS)),
                }
                self._atomic_json(staging / "workspace.json", frozen)
                os.replace(staging, destination)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            shutil.rmtree(source_dir, ignore_errors=True)
            return self.public_workspace(tender_id, owner_id, touch=False)

    def public_workspace(self, tender_id: str, owner_id: str, *, touch: bool = True) -> dict[str, Any]:
        with self._lock:
            tender_id = self._validate_id(tender_id)
            path = self.workspace_root / tender_id
            metadata = self._metadata(path)
            if metadata.get("owner_id") != owner_id:
                raise TenderWorkspaceError("Тендер не найден.")
            now = _now()
            if datetime.fromisoformat(metadata["absolute_expires_at"]) <= now or datetime.fromisoformat(metadata["last_access_at"]) + timedelta(seconds=IDLE_TTL_SECONDS) <= now:
                raise TenderWorkspaceError("Срок тендера истёк.", 410, "TENDER_WORKSPACE_EXPIRED")
            source = path / "source.xlsx"
            try:
                if source.stat().st_size > MAX_UPLOAD_BYTES or hashlib.sha256(source.read_bytes()).hexdigest() != metadata.get("source_sha256"):
                    raise TenderWorkspaceError("Исходная книга тендера повреждена.", 409, "TENDER_SOURCE_CHANGED")
            except OSError as exc:
                raise TenderWorkspaceError("Исходная книга тендера отсутствует.", 409, "TENDER_SOURCE_MISSING") from exc
            if touch:
                metadata["last_access_at"] = _iso(now)
                self._atomic_json(path / "workspace.json", metadata)
            # Internal owner, manifest, and server paths are never serialized.
            return _sanitize_public_payload({
                "tender_id": tender_id, "filename": metadata["filename"],
                "source_sha256": metadata["source_sha256"], "sheet_name": metadata["sheet_name"],
                "header_row": metadata["header_row"], "mapping": metadata["mapping"],
                "rows": metadata["rows"], "counts": metadata["counts"],
                "warnings": metadata["warnings"], "logical_right_edge": metadata["logical_right_edge"],
                "future_output_columns": metadata["future_output_columns"],
                "revision": metadata["revision"], "created_at": metadata["created_at"],
                "last_access_at": metadata["last_access_at"],
                "absolute_expires_at": metadata["absolute_expires_at"],
            })

    def _workspace_storage_bytes(self) -> int:
        total = 0
        for directory in self._workspace_dirs():
            try:
                for item in directory.rglob("*"):
                    if item.is_file():
                        total += item.stat().st_size
            except OSError:
                continue
        return total

    def acquire_sourcing_workspace(self, tender_id: str, owner_id: str):
        """Atomically validate an immutable workspace and acquire its activity lease."""
        with self._lock:
            tender_id = self._validate_id(tender_id)
            path = self.workspace_root / tender_id
            metadata = self._metadata(path)
            if metadata.get("owner_id") != owner_id:
                raise TenderWorkspaceError("Тендер не найден.")
            now = _now()
            if datetime.fromisoformat(metadata["absolute_expires_at"]) <= now or datetime.fromisoformat(metadata["last_access_at"]) + timedelta(seconds=IDLE_TTL_SECONDS) <= now:
                raise TenderWorkspaceError("Срок тендера истёк.", 410, "TENDER_WORKSPACE_EXPIRED")
            source = path / "source.xlsx"
            try:
                if source.stat().st_size > MAX_UPLOAD_BYTES or hashlib.sha256(source.read_bytes()).hexdigest() != metadata.get("source_sha256"):
                    raise TenderWorkspaceError("Исходная книга тендера повреждена.", 409, "TENDER_SOURCE_CHANGED")
            except OSError as exc:
                raise TenderWorkspaceError("Исходная книга тендера отсутствует.", 409, "TENDER_SOURCE_MISSING") from exc
            lease = self.activity.acquire(tender_id)
            try:
                metadata["last_access_at"] = _iso(now)
                self._atomic_json(path / "workspace.json", metadata)
                return path, metadata, lease
            except Exception:
                lease.release()
                raise

    def reserve_tender_run_storage(self, workspace_path: Path) -> None:
        """Reserve the maximum single run size against the existing global workspace quota."""
        with self._lock:
            resolved = Path(workspace_path).resolve()
            if resolved.parent != self.workspace_root.resolve() or not resolved.is_dir():
                raise TenderWorkspaceError("Тендер не найден.")
            if self._workspace_storage_bytes() + MAX_TENDER_RUN_BYTES > MAX_TENDER_STORAGE_BYTES:
                raise TenderWorkspaceError("Достигнут общий лимит хранилища тендеров.", 409, "TENDER_DISK_QUOTA")

    def verify_sourcing_snapshot(
        self,
        workspace_path: Path,
        tender_id: str,
        owner_id: str,
        *,
        source_sha256: str,
        revision: int,
    ) -> dict[str, Any]:
        """Recheck the queued job's confirmed workspace before it calls providers."""
        with self._lock:
            path = Path(workspace_path).resolve()
            if path.parent != self.workspace_root.resolve() or path.name != self._validate_id(tender_id):
                raise TenderWorkspaceError("Тендер не найден.")
            metadata = self._metadata(path)
            if metadata.get("owner_id") != owner_id:
                raise TenderWorkspaceError("Тендер не найден.")
            if metadata.get("source_sha256") != source_sha256 or int(metadata.get("revision", -1)) != int(revision):
                raise TenderWorkspaceError("Рабочее пространство тендера изменилось до начала подбора.", 409, "TENDER_WORKSPACE_CHANGED")
            source = path / "source.xlsx"
            try:
                if source.stat().st_size > MAX_UPLOAD_BYTES or hashlib.sha256(source.read_bytes()).hexdigest() != source_sha256:
                    raise TenderWorkspaceError("Исходная книга тендера повреждена.", 409, "TENDER_SOURCE_CHANGED")
            except OSError as exc:
                raise TenderWorkspaceError("Исходная книга тендера отсутствует.", 409, "TENDER_SOURCE_MISSING") from exc
            return metadata

    def delete(self, tender_id: str, owner_id: str) -> bool:
        tender_id = self._validate_id(tender_id)
        with self._lock:
            path = self.workspace_root / tender_id
            metadata = self._metadata(path)
            if metadata.get("owner_id") != owner_id:
                raise TenderWorkspaceError("Тендер не найден.")
            if not self.activity.begin_delete(tender_id):
                raise TenderWorkspaceError("Тендер сейчас используется. Повторите удаление позже.", 409, "TENDER_WORKSPACE_BUSY")
            try:
                self._remove_tree(path)
            finally:
                self.activity.finish_delete(tender_id)
        return True

    def workspace_path(self, tender_id: str, owner_id: str) -> tuple[Path, dict[str, Any]]:
        tender_id = self._validate_id(tender_id)
        self.cleanup()
        path = self.workspace_root / tender_id
        metadata = self._metadata(path)
        if metadata.get("owner_id") != owner_id:
            raise TenderWorkspaceError("Тендер не найден.")
        return path, metadata


__all__ = [
    "TenderWorkspaceRepository", "TenderWorkspaceError", "PREVIEW_TTL_SECONDS",
    "IDLE_TTL_SECONDS", "ABSOLUTE_TTL_SECONDS", "MAX_WORKSPACES_PER_USER", "MAX_WORKSPACES_GLOBAL",
    "MAX_TENDER_RUN_BYTES", "MAX_TENDER_RUNS_PER_WORKSPACE", "MAX_TENDER_STORAGE_BYTES",
]
