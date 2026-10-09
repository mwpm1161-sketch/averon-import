"""Explicit terminal-v2 storage; production v1 lifecycle stays independent."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from .durable_projection import DurableProjectionCode, DurableProjectionError, encode_tender_sourcing_run_v2
from .durable_read import (
    MAX_DURABLE_V2_BYTES, DurableTenderReadCode, DurableTenderReadError,
    DurableTenderSourcingRunV1, DurableTenderSourcingRunV2, _unique_object, read_tender_sourcing_run,
)
from .repository import (
    IDLE_TTL_SECONDS, ID_RE, MAX_TENDER_RUN_BYTES, MAX_UPLOAD_BYTES,
    MAX_WORKSPACE_METADATA_BYTES, TenderWorkspaceError, TenderWorkspaceRepository,
)


class DurableV2StorageCode(str, Enum):
    INVALID_INPUT = "V2_RUN_INVALID_INPUT"
    NON_TERMINAL = "V2_RUN_NON_TERMINAL"
    ALREADY_EXISTS = "V2_RUN_ALREADY_EXISTS"
    CORRUPT = "V2_RUN_CORRUPT"
    UNSUPPORTED_VERSION = "V2_RUN_UNSUPPORTED_VERSION"
    METADATA_MISMATCH = "V2_RUN_METADATA_MISMATCH"
    TOO_LARGE = "V2_RUN_TOO_LARGE"
    STORAGE_FAILED = "V2_RUN_STORAGE_FAILED"
    WORKSPACE_INVALID = "V2_RUN_WORKSPACE_INVALID"


class DurableV2StorageError(TenderWorkspaceError):
    def __init__(self, code: DurableV2StorageCode):
        super().__init__("Durable v2 storage operation failed.", 413 if code == DurableV2StorageCode.TOO_LARGE else 409, code)


def _bounded_bytes(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise DurableV2StorageError(DurableV2StorageCode.CORRUPT)
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise DurableV2StorageError(DurableV2StorageCode.TOO_LARGE)
    return payload


def _workspace_metadata(repository: TenderWorkspaceRepository, workspace_path: Path) -> tuple[Path, dict]:
    """Validate server-owned confirmed metadata without touching TTL or repairing it."""
    try:
        path = Path(workspace_path).resolve(strict=True)
        if (path.parent != repository.workspace_root.resolve() or not ID_RE.fullmatch(path.name)
                or not path.is_dir()):
            raise ValueError
        metadata = json.loads(_bounded_bytes(path / "workspace.json", MAX_WORKSPACE_METADATA_BYTES),
                              object_pairs_hook=_unique_object)
        if (type(metadata) is not dict or metadata.get("tender_id") != path.name
                or not isinstance(metadata.get("owner_id"), str) or not metadata["owner_id"]
                or "preview_id" in metadata or type(metadata.get("revision")) is not int
                or not 1 <= metadata["revision"] <= 1_000_000_000
                or not isinstance(metadata.get("source_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", metadata["source_sha256"])):
            raise ValueError
        expires = datetime.fromisoformat(metadata["absolute_expires_at"])
        accessed = datetime.fromisoformat(metadata["last_access_at"])
        now = datetime.now(timezone.utc)
        if expires.utcoffset() is None or accessed.utcoffset() is None:
            raise ValueError
        if expires <= now or accessed + timedelta(seconds=IDLE_TTL_SECONDS) <= now:
            raise ValueError
        source = _bounded_bytes(path / "source.xlsx", MAX_UPLOAD_BYTES)
        if not source or hashlib.sha256(source).hexdigest() != metadata["source_sha256"]:
            raise ValueError
        runs = path / "runs"
        if os.path.lexists(runs) and (not runs.is_dir() or runs.resolve() != path / "runs"):
            raise ValueError
        return path, metadata
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise DurableV2StorageError(DurableV2StorageCode.WORKSPACE_INVALID) from None


def _correlate(run: DurableTenderSourcingRunV2, metadata: dict, run_id: str) -> None:
    if (getattr(run, "run_id", None) != run_id or getattr(run, "tender_id", None) != metadata["tender_id"]
            or getattr(run, "source_sha256", None) != metadata["source_sha256"]
            or getattr(run, "workspace_revision", None) != metadata["revision"]):
        raise DurableV2StorageError(DurableV2StorageCode.METADATA_MISMATCH)
    if getattr(run, "status", None) == "running":
        raise DurableV2StorageError(DurableV2StorageCode.NON_TERMINAL)


def _decode(payload: bytes):
    try:
        return read_tender_sourcing_run(payload)
    except DurableTenderReadError as exc:
        code = {DurableTenderReadCode.TOO_LARGE: DurableV2StorageCode.TOO_LARGE,
                DurableTenderReadCode.UNSUPPORTED_VERSION: DurableV2StorageCode.UNSUPPORTED_VERSION}.get(
                    exc.code, DurableV2StorageCode.CORRUPT)
        raise DurableV2StorageError(code) from None


def read_durable_v2(repository: TenderWorkspaceRepository, workspace_path: Path, run_id: str) -> DurableTenderSourcingRunV2:
    try:
        if type(run_id) is not str or not ID_RE.fullmatch(run_id):
            raise DurableV2StorageError(DurableV2StorageCode.INVALID_INPUT)
        path, metadata = _workspace_metadata(repository, workspace_path)
        # Read within the repository bound so even a legal v1 file larger than
        # the v2 budget receives the explicit version error. The approved
        # discriminator independently enforces the lower v2 byte limit.
        run = _decode(_bounded_bytes(path / "runs" / f"{run_id}.json", MAX_TENDER_RUN_BYTES))
        if type(run) is not DurableTenderSourcingRunV2:
            raise DurableV2StorageError(DurableV2StorageCode.UNSUPPORTED_VERSION)
        _correlate(run, metadata, run_id)
        return run
    except DurableV2StorageError:
        raise
    except OSError:
        raise DurableV2StorageError(DurableV2StorageCode.STORAGE_FAILED) from None


def read_any_path(repository: TenderWorkspaceRepository, path: Path, tender_id: str) -> dict | DurableTenderSourcingRunV2:
    """Housekeeping only: preserve v1 shape and strictly correlate terminal v2."""
    try:
        run = _decode(_bounded_bytes(path, MAX_TENDER_RUN_BYTES))
        if isinstance(run, DurableTenderSourcingRunV1):
            payload = run.payload
            if payload.get("tender_id") != tender_id:
                raise DurableV2StorageError(DurableV2StorageCode.METADATA_MISMATCH)
            return payload
        _, metadata = _workspace_metadata(repository, path.parent.parent)
        if run.tender_id != tender_id:
            raise DurableV2StorageError(DurableV2StorageCode.METADATA_MISMATCH)
        _correlate(run, metadata, path.stem)
        return run
    except DurableV2StorageError:
        raise
    except OSError:
        raise DurableV2StorageError(DurableV2StorageCode.STORAGE_FAILED) from None


def _atomic_create(path: Path, encoded: bytes) -> None:
    """Same-directory fsynced temporary, followed by atomic no-clobber publication."""
    temporary = None
    descriptor = None
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        os.chmod(temporary, 0o600)
        stream = os.fdopen(descriptor, "wb")
        descriptor = None  # stream now owns the descriptor, including on write failure
        with stream:
            if stream.write(encoded) != len(encoded):
                raise OSError
            stream.flush()
            os.fsync(stream.fileno())
        # os.replace would overwrite a target created by another writer after
        # the initial check. A same-filesystem link publishes complete bytes
        # atomically and fails if any target already exists (including v1).
        os.link(temporary, path)
    except FileExistsError:
        raise DurableV2StorageError(DurableV2StorageCode.ALREADY_EXISTS) from None
    except OSError:
        raise DurableV2StorageError(DurableV2StorageCode.STORAGE_FAILED) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def persist_durable_v2(repository: TenderWorkspaceRepository, workspace_path: Path, run: DurableTenderSourcingRunV2) -> None:
    if type(run) is not DurableTenderSourcingRunV2:
        raise DurableV2StorageError(DurableV2StorageCode.INVALID_INPUT)
    if type(getattr(run, "run_id", None)) is not str or not ID_RE.fullmatch(run.run_id):
        raise DurableV2StorageError(DurableV2StorageCode.INVALID_INPUT)
    path, metadata = _workspace_metadata(repository, workspace_path)
    _correlate(run, metadata, run.run_id)
    target = path / "runs" / f"{run.run_id}.json"
    if os.path.lexists(target):
        raise DurableV2StorageError(DurableV2StorageCode.ALREADY_EXISTS)
    try:
        encoded = encode_tender_sourcing_run_v2(run)
    except DurableProjectionError as exc:
        code = (DurableV2StorageCode.TOO_LARGE if exc.code == DurableProjectionCode.TOO_LARGE
                else DurableV2StorageCode.INVALID_INPUT)
        raise DurableV2StorageError(code) from None
    except Exception:
        raise DurableV2StorageError(DurableV2StorageCode.INVALID_INPUT) from None
    if len(encoded) > min(MAX_DURABLE_V2_BYTES, MAX_TENDER_RUN_BYTES):
        raise DurableV2StorageError(DurableV2StorageCode.TOO_LARGE)
    repository.reserve_tender_run_storage(path)
    _atomic_create(target, encoded)
