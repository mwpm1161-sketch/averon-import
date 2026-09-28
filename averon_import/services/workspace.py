from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DOCUMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def validate_document_id(document_id: str) -> str:
    value = str(document_id or "")
    if not _DOCUMENT_ID_RE.fullmatch(value):
        raise ValueError("Недопустимый идентификатор документа")
    return value


@dataclass(slots=True)
class Workspace:
    document_id: str
    root: Path

    @property
    def pdf_path(self) -> Path:
        return self.root / "source.pdf"

    @property
    def metadata_path(self) -> Path:
        return self.root / "metadata.json"

    @property
    def result_path(self) -> Path:
        return self.root / "result.json"

    @property
    def review_decisions_path(self) -> Path:
        return self.root / "review_decisions.json"

    @property
    def pages_dir(self) -> Path:
        path = self.root / "pages"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def exports_dir(self) -> Path:
        path = self.root / "exports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def sourcing_runs_dir(self) -> Path:
        return self.root / "sourcing_runs"


class WorkspaceService:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.documents_dir = data_dir / "documents"
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        # Bounded in-memory counters for tests and local performance audits.
        # They are never logged or returned to clients.
        self.metrics = {
            "result_reads": 0,
            "result_writes": 0,
            "source_fingerprint_calculations": 0,
        }

    def create(
        self,
        source_path: Path,
        metadata: dict[str, Any],
        *,
        source_sha256: str | None = None,
    ) -> Workspace:
        document_id = uuid.uuid4().hex
        root = self.documents_dir / document_id
        root.mkdir(parents=True)
        workspace = Workspace(document_id, root)
        shutil.copy2(source_path, workspace.pdf_path)
        saved_metadata = {**metadata, "document_id": document_id}
        if source_sha256 and re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            saved_metadata["source_sha256"] = source_sha256
            self.metrics["source_fingerprint_calculations"] += 1
        self.write_json(workspace.metadata_path, saved_metadata)
        return workspace

    def get(self, document_id: str) -> Workspace:
        try:
            document_id = validate_document_id(document_id)
        except ValueError as exc:
            raise FileNotFoundError("Документ не найден") from exc
        root = self.documents_dir / document_id
        if self.documents_dir.is_symlink() or root.is_symlink() or not root.is_dir():
            raise FileNotFoundError(document_id)
        return Workspace(document_id, root)

    @property
    def tombstones_dir(self) -> Path:
        return self.data_dir / ".deleting"

    def move_to_tombstone(self, document_id: str) -> Path:
        document_id = validate_document_id(document_id)
        if self.documents_dir.is_symlink():
            raise OSError("Каталог документов не может быть символической ссылкой")
        root = self.documents_dir / document_id
        if root.is_symlink() or not root.is_dir():
            raise FileNotFoundError(document_id)
        tombstones = self.tombstones_dir
        tombstones.mkdir(parents=True, exist_ok=True)
        if tombstones.is_symlink() or not tombstones.is_dir():
            raise OSError("Каталог удаления недоступен")
        tombstone = tombstones / f"{document_id}-{uuid.uuid4().hex}"
        root.rename(tombstone)
        return tombstone

    @staticmethod
    def tree_size_bytes(root: Path) -> int:
        total = 0
        pending = [root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    info = entry.stat(follow_symlinks=False)
                    is_junction = getattr(path, "is_junction", lambda: False)()
                    if entry.is_symlink() or is_junction:
                        total += info.st_size
                    elif stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
        return total

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return safe metadata for existing document workspaces.

        Discovery is deliberately tolerant of incomplete directories.  The
        PDF is never opened and no filesystem path is returned to callers.
        """

        bounded_limit = max(1, min(int(limit), 100))
        documents: list[dict[str, Any]] = []
        if self.documents_dir.is_symlink() or not self.documents_dir.is_dir():
            return documents
        try:
            candidates = list(self.documents_dir.iterdir())
        except OSError:
            return []
        for root in candidates:
            if root.is_symlink() or not root.is_dir() or not _DOCUMENT_ID_RE.fullmatch(root.name):
                continue
            metadata_path = root / "metadata.json"
            try:
                metadata = self.read_json(metadata_path)
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("document_id") or "") != root.name:
                continue
            filename = Path(str(metadata.get("filename") or "document.pdf")).name
            if not filename.lower().endswith(".pdf"):
                filename = "document.pdf"
            result_path = root / "result.json"
            review_path = root / "review_decisions.json"
            has_result = result_path.is_file()
            try:
                has_review_decisions = review_path.is_file()
                timestamps = [metadata_path.stat().st_mtime]
                for path in (result_path, review_path):
                    if path.exists():
                        timestamps.append(path.stat().st_mtime)
                created_at = _timestamp(metadata_path.stat().st_mtime)
                updated_at = _timestamp(max(timestamps))
            except OSError:
                created_at = None
                updated_at = None
            documents.append({
                "document_id": root.name,
                "filename": filename,
                "title": str(metadata.get("title") or Path(filename).stem),
                "page_count": _safe_non_negative_int(metadata.get("page_count")),
                "size": _safe_non_negative_int(metadata.get("size")),
                "has_result": has_result,
                "has_review_decisions": has_review_decisions,
                "created_at": created_at,
                "updated_at": updated_at,
                "available": True,
            })
        documents.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return documents[:bounded_limit]

    def read_json(self, path: Path, default=None):
        if path.name == "result.json":
            self.metrics["result_reads"] += 1
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, data: Any) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
        if path.name == "result.json":
            self.metrics["result_writes"] += 1

    def source_fingerprint(self, workspace: Workspace, calculate=None) -> str:
        """Load the server-owned PDF fingerprint, lazily migrating old workspaces."""
        metadata = self.read_json(workspace.metadata_path, default={})
        if isinstance(metadata, dict):
            cached = metadata.get("source_sha256")
            if isinstance(cached, str) and re.fullmatch(r"[0-9a-f]{64}", cached):
                return cached
        calculator = calculate or _sha256_file
        fingerprint = calculator(workspace.pdf_path)
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("Не удалось вычислить контрольную сумму PDF")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["document_id"] = workspace.document_id
        metadata["source_sha256"] = fingerprint
        self.write_json(workspace.metadata_path, metadata)
        self.metrics["source_fingerprint_calculations"] += 1
        return fingerprint


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_non_negative_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
