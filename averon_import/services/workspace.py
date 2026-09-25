from __future__ import annotations

import hashlib
import json
import shutil
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
        self._mutation_locks: dict[str, threading.RLock] = {}
        self._mutation_locks_guard = threading.Lock()

    def create(self, source_path: Path, metadata: dict[str, Any]) -> Workspace:
        document_id = uuid.uuid4().hex
        root = self.documents_dir / document_id
        root.mkdir(parents=True)
        workspace = Workspace(document_id, root)
        shutil.copy2(source_path, workspace.pdf_path)
        self.write_json(workspace.metadata_path, {"document_id": document_id, **metadata})
        return workspace

    def get(self, document_id: str) -> Workspace:
        root = self.documents_dir / document_id
        if not root.exists():
            raise FileNotFoundError(document_id)
        return Workspace(document_id, root)

    @contextmanager
    def mutation_lock(self, document_id: str):
        """Serialize canonical mutations for one document inside this process.

        Averon is currently served by one application process.  Keeping a
        per-document lock prevents concurrent review/save requests from
        overwriting each other while allowing unrelated documents to proceed.
        Result revisions still protect clients from stale writes.
        """

        with self._mutation_locks_guard:
            lock = self._mutation_locks.setdefault(str(document_id), threading.RLock())
        with lock:
            yield

    @staticmethod
    def has_result(workspace: Workspace) -> bool:
        try:
            return workspace.result_path.is_file() and workspace.result_path.stat().st_size > 2
        except OSError:
            return False

    def source_fingerprint(self, workspace: Workspace) -> str:
        """Return the immutable source PDF SHA-256, caching legacy workspaces.

        New uploads persist this value at creation time.  Existing pilot
        workspaces compute it once on first use instead of re-reading the PDF
        for every result/review request.
        """

        metadata = self.read_json(workspace.metadata_path, default={})
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        cached = str(metadata.get("source_sha256") or "").strip().lower()
        if len(cached) == 64 and all(char in "0123456789abcdef" for char in cached):
            return cached

        digest = hashlib.sha256()
        with workspace.pdf_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        fingerprint = digest.hexdigest()

        # A second request may have filled the cache while the digest was
        # being calculated.  Serialize only the tiny metadata update and
        # preserve every unrelated metadata field.
        with self.mutation_lock(workspace.document_id):
            latest = self.read_json(workspace.metadata_path, default={})
            latest = dict(latest) if isinstance(latest, dict) else {}
            existing = str(latest.get("source_sha256") or "").strip().lower()
            if len(existing) == 64 and all(char in "0123456789abcdef" for char in existing):
                return existing
            latest["source_sha256"] = fingerprint
            self.write_json(workspace.metadata_path, latest)
        return fingerprint

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return safe metadata for existing document workspaces.

        Discovery is deliberately tolerant of incomplete directories.  The
        PDF is never opened and no filesystem path is returned to callers.
        """

        bounded_limit = max(1, min(int(limit), 100))
        documents: list[dict[str, Any]] = []
        try:
            candidates = list(self.documents_dir.iterdir())
        except OSError:
            return []
        for root in candidates:
            if not root.is_dir() or not root.name or len(root.name) > 128:
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
            available = True
            availability_error = ""
            # Listing recent workspaces is a hot startup path.  Do not parse
            # potentially large result.json files merely to determine whether
            # a result exists; full validation happens only when the document
            # is explicitly opened.
            try:
                has_result = result_path.is_file() and result_path.stat().st_size > 2
            except OSError:
                has_result = False
                available = False
                availability_error = "Данные результата недоступны"
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
                "available": available,
                **({"availability_error": availability_error} if availability_error else {}),
            })
        documents.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return documents[:bounded_limit]

    @staticmethod
    def read_json(path: Path, default=None):
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_json(path: Path, data: Any) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


def _safe_non_negative_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
