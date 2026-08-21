from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from averon_import.core.result_schema import migrate_result


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
    def pages_dir(self) -> Path:
        path = self.root / "pages"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def exports_dir(self) -> Path:
        path = self.root / "exports"
        path.mkdir(parents=True, exist_ok=True)
        return path


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

    def read_result(self, workspace: Workspace) -> dict[str, Any] | None:
        return migrate_result(self.read_json(workspace.result_path))

    def write_result(self, workspace: Workspace, data: dict[str, Any]) -> None:
        migrated = migrate_result(data)
        self.write_json(workspace.result_path, migrated)

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
