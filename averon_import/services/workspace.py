from __future__ import annotations

import json
import shutil
import uuid
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
            has_result = False
            if result_path.exists():
                try:
                    has_result = isinstance(self.read_json(result_path), dict)
                except (OSError, ValueError, TypeError):
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
