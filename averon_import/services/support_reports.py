"""Persistent export incidents and authenticated support reports."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REPORT_STATUSES = ("OPEN", "IN_PROGRESS", "RESOLVED")
_FORBIDDEN_KEY_MARKERS = (
    "authorization",
    "api_key",
    "apikey",
    "password",
    "proxy",
    "secret",
    "session",
    "token",
)
_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
_OMIT = object()
logger = logging.getLogger(__name__)


class SupportError(Exception):
    """Base class for safe support persistence errors."""


class IncidentNotFound(SupportError):
    pass


class ReportNotFound(SupportError):
    pass


class ReportForbidden(SupportError):
    pass


class DuplicateReport(SupportError):
    def __init__(self, report_id: str):
        super().__init__(report_id)
        self.report_id = report_id


class StaleReport(SupportError):
    pass


class SnapshotUnavailable(SupportError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _safe_snapshot_value(value: Any, key: str = "") -> Any:
    normalized_key = key.casefold().replace("-", "_")
    if any(marker in normalized_key for marker in _FORBIDDEN_KEY_MARKERS):
        return _OMIT
    if isinstance(value, dict):
        result = {}
        for child_key, child_value in value.items():
            safe_value = _safe_snapshot_value(child_value, str(child_key))
            if safe_value is not _OMIT:
                result[str(child_key)] = safe_value
        return result
    if isinstance(value, (list, tuple)):
        return [
            None if (safe_item := _safe_snapshot_value(item, key)) is _OMIT else safe_item
            for item in value
        ]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and _ABSOLUTE_PATH_RE.match(value.strip()):
            return "[REDACTED_PATH]"
        return value
    return str(value)


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.exception("Unable to clean up support snapshot artifact")


class SupportRepository:
    """SQLite-backed support artifacts with short-lived connections."""

    def __init__(self, support_dir: Path):
        self.support_dir = Path(support_dir)
        self.db_path = self.support_dir / "support.sqlite3"
        self.incidents_dir = self.support_dir / "incidents"
        self.incidents_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, SCHEMA_VERSION}:
                raise RuntimeError("Неподдерживаемая версия support schema")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS export_incidents (
                    incident_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'export',
                    error_code TEXT NOT NULL,
                    public_message TEXT NOT NULL,
                    http_status INTEGER NOT NULL,
                    app_version TEXT NOT NULL,
                    export_kind TEXT NOT NULL,
                    requested_filename TEXT,
                    row_count INTEGER,
                    snapshot_path TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS support_reports (
                    report_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL UNIQUE,
                    reporter_fio TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (incident_id) REFERENCES export_incidents(incident_id)
                );
                CREATE INDEX IF NOT EXISTS idx_support_reports_status_created
                    ON support_reports(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_export_incidents_document
                    ON export_incidents(document_id);
                CREATE INDEX IF NOT EXISTS idx_export_incidents_created
                    ON export_incidents(created_at);
                """
            )
            if version == 0:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_export_incident(
        self,
        *,
        document_id: str,
        username: str,
        role: str,
        app_version: str,
        export_kind: str,
        requested_filename: str,
        row_count: int,
        document: dict[str, Any],
        export_request: dict[str, Any],
        page_statuses: dict[str, Any] | list[Any] | None,
        result_summary: dict[str, Any] | None,
        resolved_filename: str,
        error_code: str,
        public_message: str,
        http_status: int,
    ) -> dict[str, Any]:
        incident_id = _new_id("exp")
        created_at = _utc_now()
        snapshot = {
            "schema_version": 1,
            "incident_id": incident_id,
            "created_at": created_at,
            "app_version": app_version,
            "user": {"username": username, "role": role},
            "document_id": document_id,
            "document": document,
            "export_request": export_request,
            "page_statuses": page_statuses or {},
            "result_summary": result_summary or {},
            "resolved_filename": resolved_filename,
            "error_code": error_code,
            "public_message": public_message,
        }
        snapshot_value = _safe_snapshot_value(snapshot)
        snapshot_bytes = json.dumps(
            snapshot_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
        snapshot_name = f"{incident_id}.json"
        snapshot_path = self.incidents_dir / snapshot_name
        relative_snapshot_path = (Path("incidents") / snapshot_name).as_posix()
        temporary_path: Path | None = None
        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{incident_id}-",
                suffix=".tmp",
                dir=self.incidents_dir,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(snapshot_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, snapshot_path)
            temporary_path = None
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO export_incidents (
                        incident_id, document_id, created_at, username, role,
                        stage, error_code, public_message, http_status,
                        app_version, export_kind, requested_filename, row_count,
                        snapshot_path, snapshot_sha256
                    ) VALUES (?, ?, ?, ?, ?, 'export', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident_id,
                        document_id,
                        created_at,
                        username,
                        role,
                        error_code,
                        public_message,
                        http_status,
                        app_version,
                        export_kind,
                        requested_filename,
                        row_count,
                        relative_snapshot_path,
                        snapshot_sha256,
                    ),
                )
            return {
                "incident_id": incident_id,
                "document_id": document_id,
                "created_at": created_at,
                "username": username,
                "role": role,
                "stage": "export",
                "error_code": error_code,
                "public_message": public_message,
                "http_status": http_status,
                "app_version": app_version,
                "export_kind": export_kind,
                "requested_filename": requested_filename,
                "row_count": row_count,
                "snapshot_path": relative_snapshot_path,
                "snapshot_sha256": snapshot_sha256,
            }
        except Exception:
            if temporary_path is not None:
                _best_effort_unlink(temporary_path)
            _best_effort_unlink(snapshot_path)
            raise

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM export_incidents WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
        result = self._as_dict(row)
        if result is None:
            raise IncidentNotFound(incident_id)
        return result

    def create_report(
        self,
        *,
        incident_id: str,
        reporter_fio: str,
        description: str,
        username: str,
    ) -> dict[str, Any]:
        incident = self.get_incident(incident_id)
        if incident["username"] != username:
            raise ReportForbidden(incident_id)
        report_id = _new_id("rpt")
        now = _utc_now()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO support_reports (
                        report_id, incident_id, reporter_fio, description,
                        status, created_at, updated_at, version
                    ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, 1)
                    """,
                    (report_id, incident_id, reporter_fio, description, now, now),
                )
        except sqlite3.IntegrityError as exc:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT report_id FROM support_reports WHERE incident_id = ?",
                    (incident_id,),
                ).fetchone()
            if row is not None:
                raise DuplicateReport(str(row["report_id"])) from exc
            raise
        return self.get_report(report_id)

    def get_report(self, report_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    report.report_id,
                    report.incident_id,
                    report.reporter_fio,
                    report.description,
                    report.status,
                    report.created_at,
                    report.updated_at,
                    report.version,
                    incident.document_id,
                    incident.username,
                    incident.role,
                    incident.created_at AS incident_created_at,
                    incident.stage,
                    incident.error_code,
                    incident.public_message,
                    incident.http_status,
                    incident.app_version,
                    incident.export_kind,
                    incident.requested_filename,
                    incident.row_count,
                    incident.snapshot_sha256
                FROM support_reports AS report
                JOIN export_incidents AS incident
                  ON incident.incident_id = report.incident_id
                WHERE report.report_id = ?
                """,
                (report_id,),
            ).fetchone()
        result = self._as_dict(row)
        if result is None:
            raise ReportNotFound(report_id)
        return result

    def list_reports(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                report.report_id,
                report.incident_id,
                report.reporter_fio,
                report.status,
                report.created_at,
                report.updated_at,
                report.version,
                incident.document_id,
                incident.username,
                incident.role,
                incident.created_at AS incident_created_at,
                incident.error_code,
                incident.public_message,
                incident.http_status,
                incident.export_kind,
                incident.requested_filename,
                incident.row_count
            FROM support_reports AS report
            JOIN export_incidents AS incident
              ON incident.incident_id = report.incident_id
        """
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE report.status = ?"
            parameters.append(status)
        query += " ORDER BY report.created_at DESC LIMIT ? OFFSET ?"
        parameters.extend([limit, offset])
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def update_report_status(
        self,
        *,
        report_id: str,
        status: str,
        version: int,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE support_reports
                SET status = ?, updated_at = ?, version = version + 1
                WHERE report_id = ? AND version = ?
                """,
                (status, now, report_id, version),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM support_reports WHERE report_id = ?",
                    (report_id,),
                ).fetchone()
                if exists is None:
                    raise ReportNotFound(report_id)
                raise StaleReport(report_id)
        return self.get_report(report_id)

    def snapshot_for_report(self, report_id: str) -> dict[str, Any]:
        report = self.get_report(report_id)
        snapshot_path = self.incidents_dir / f"{report['incident_id']}.json"
        try:
            snapshot_bytes = snapshot_path.read_bytes()
            actual_hash = hashlib.sha256(snapshot_bytes).hexdigest()
            if actual_hash != report["snapshot_sha256"]:
                raise SnapshotUnavailable(report_id)
            snapshot = json.loads(snapshot_bytes.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise SnapshotUnavailable(report_id) from exc
        if not isinstance(snapshot, dict):
            raise SnapshotUnavailable(report_id)
        return snapshot


__all__ = [
    "DuplicateReport",
    "IncidentNotFound",
    "REPORT_STATUSES",
    "ReportForbidden",
    "ReportNotFound",
    "SnapshotUnavailable",
    "StaleReport",
    "SupportRepository",
]
